"""Hook handlers — the capture layer.

Wired to Claude Code hooks (README 8):

* ``UserPromptSubmit`` records stated intent — the half of the picture a raw
  command log can never recover.
* ``PostToolUse`` records what actually ran.
* ``SessionEnd`` / ``Stop`` folds the session into the ledger.

**Every handler is fail-safe.** A hook that raises could disrupt the
developer's session, so all errors are swallowed to a log file and the process
always exits 0. Capture is never worth breaking someone's work over.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .ledger import Entry, Ledger, make_id, STATUS_CANDIDATE
from .normalize import parameterize, signature
from .recurrence import find_match
from .sanitize import scrub, scrub_obj
from .segment import PROMPT_TOOL, segment

# Tool inputs worth keeping. Anything else is recorded by name only.
_KEEP_INPUT = {
    "Bash": ("command", "description"),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("file_path",),
}
# Pure exploration: recorded, but never the reason a workflow is proposed.
# UserPrompt is the segmentation sentinel written by handle_prompt — a task
# boundary, never a step of the workflow itself.
_NOISE_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch",
                PROMPT_TOOL}
# How far ahead to look for the write a `Read` fed. Read-then-edit is usually
# adjacent; a couple of steps of slack covers a read, a check, then the edit.
_READ_FEEDS_WINDOW = 3


def _substantive(steps: list[dict]) -> list[dict]:
    """The steps that are the work, keeping a `Read` that fed a write.

    `_NOISE_TOOLS` drops every `Read` as pure exploration. Measured over 621
    real `Read` calls, that is backwards: 38.5% are immediately followed by a
    write to the *same file* and only 18.2% sit inside a run of reads. So the
    rule discards twice as many procedure inputs as exploration — and the step
    it discards is the one that says which file the procedure operates on.

    Read-then-edit stays. A read that leads nowhere still goes, which is the
    case the original rule was written for.
    """
    keep: list[dict] = []
    for index, step in enumerate(steps):
        tool = step.get("tool")
        if tool not in _NOISE_TOOLS:
            keep.append(step)
            continue
        if tool != "Read":
            continue
        path = (step.get("input") or {}).get("file_path")
        if not path:
            continue
        ahead = steps[index + 1:index + 1 + _READ_FEEDS_WINDOW]
        if any(s.get("tool") in ("Edit", "Write", "NotebookEdit")
               and (s.get("input") or {}).get("file_path") == path for s in ahead):
            keep.append(step)
    return keep


def log_error(config: Config, message: str) -> None:
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        with config.log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
    except OSError:
        pass


def note_pending_check(config: Config, entry_id: str, session_id: str,
                       reason: str = "") -> None:
    """Record that this entry was touched, for a near-miss check run later.

    No judgement here, and deliberately so: the whole point of the queue is
    that `SessionEnd` decides nothing. Whether this entry is the same procedure
    as an existing one is asked at the start of a later session, by a process
    nothing is waiting on, which is what lets that check use a floor far below
    the one a live `skillpp merge` can afford.

    One short append, never raises. Same shape as `decisions.record` for the
    same reason — a hook that fails loudly is worse than a hook that forgets.
    """
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(timezone.utc).replace(
                   microsecond=0).isoformat(),
               "entry_id": entry_id, "session_id": session_id,
               "reason": reason}
        with config.pending_checks_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _session_file(config: Config, session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    return config.sessions_dir / f"{safe}.json"


def _load_session(config: Config, session_id: str) -> dict:
    path = _session_file(config, session_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"session_id": session_id, "started": time.time(), "prompts": [], "steps": []}


def _save_session(config: Config, session: dict) -> None:
    path = _session_file(config, session["session_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _failed(response) -> bool:
    """Best-effort detection of a failed tool call across payload shapes."""
    if isinstance(response, dict):
        for key in ("is_error", "isError", "error"):
            if response.get(key):
                return True
        for key in ("exit_code", "exitCode", "returncode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return True
        text = " ".join(str(v) for v in response.values() if isinstance(v, str))
    else:
        text = str(response or "")
    lowered = text[:400].lower()
    return any(marker in lowered for marker in (
        "command failed", "error:", "traceback", "fatal:", "no such file",
        "permission denied", "exit code 1",
    ))


# Envelopes the harness injects into the prompt stream. They are not something
# a developer typed, and one of them became a candidate titled
# `<task-notification>`.
# `<command-message>` and `<!-- attach` were missing, and both reached the
# ledger: a real replay produced candidates titled
# `<command-message>caveman:caveman</command-message>` and `<!-- attach -->`.
_ENVELOPE_PREFIXES = ("<task-notification", "<system-reminder",
                      "<local-command", "<command-name", "<command-message",
                      "<!-- attach")


def handle_prompt(config: Config, payload: dict) -> None:
    """UserPromptSubmit — capture stated intent."""
    session_id = str(payload.get("session_id", "unknown"))
    prompt = scrub(str(payload.get("prompt", "")).strip())
    if not prompt or prompt.startswith(_ENVELOPE_PREFIXES):
        return
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))
    if len(session["prompts"]) < 40:
        session["prompts"].append(prompt[: config.max_field_chars])
    # Also record the prompt *in the step stream*, so the interleaving of what
    # was asked and what ran survives to segmentation. Hooks fire in real time,
    # so position alone carries the ordering — no timestamps needed. The
    # sentinel is in _NOISE_TOOLS, so it never reaches a signature.
    if len(session["steps"]) < config.max_steps_per_session:
        session["steps"].append({
            "tool": PROMPT_TOOL,
            "input": {"text": prompt[: config.max_field_chars]},
            "failed": False,
            "t": round(time.time(), 1),
        })
    _save_session(config, session)


def handle_tool(config: Config, payload: dict) -> None:
    """PostToolUse — capture what ran, scrubbed before it touches disk."""
    session_id = str(payload.get("session_id", "unknown"))
    tool = str(payload.get("tool_name", "")) or "unknown"
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))

    if len(session["steps"]) >= config.max_steps_per_session:
        return

    raw_input = payload.get("tool_input") or {}
    if not isinstance(raw_input, dict):
        raw_input = {"value": raw_input}

    # A Skill invocation is how tiering learns what is actually used (README 6).
    if tool == "Skill":
        from .lifecycle import record_use
        record_use(config, str(raw_input.get("skill", "")))
    keep = _KEEP_INPUT.get(tool)
    if keep:
        kept = {k: raw_input.get(k) for k in keep if raw_input.get(k) is not None}
    elif tool.startswith("mcp__"):
        kept = {k: v for k, v in list(raw_input.items())[:5]
                if isinstance(v, (str, int, float, bool))}
    else:
        kept = {}

    step = {
        "tool": tool,
        "input": scrub_obj(kept, config.max_field_chars),
        "failed": _failed(payload.get("tool_response")),
        "t": round(time.time(), 1),
    }
    session["steps"].append(step)
    _save_session(config, session)


def handle_session_end(config: Config, payload: dict) -> dict:
    """SessionEnd / Stop — summarise the session into the ledger."""
    session_id = str(payload.get("session_id", "unknown"))
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}

    session = _load_session(config, session_id)
    try:
        result = fold_session(config, session)
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return result


def fold_session(config: Config, session: dict, *, force: bool = False,
                 source: str = "capture") -> dict:
    """Turn a finished session into ledger entries — one per task.

    A session holding several unrelated tasks used to become a single entry
    whose signature described none of them, which is why a workflow performed
    three times never reached the recurrence threshold. It is now cut into
    episodes first (see ``skillpp.segment``) and each is folded separately.

    The return value keeps the shape callers already expect — the last folded
    episode's result — with an added ``episodes`` key listing every outcome. A
    session that segments into one episode returns exactly what it always did.
    """
    episodes = segment(session.get("steps", []), config.min_episode_steps,
                       config.max_markerless_steps)

    # An episode with no completion marker, in a session that did segment, is a
    # fragment with nothing to show for itself. Recording it would recreate the
    # mega-candidates segmentation exists to remove.
    # `force` is an explicit "save this" from a person, so the guards that exist
    # to suppress uninteresting captures do not apply. They protect against a
    # detector banking noise; they should not overrule someone who has read the
    # work and asked for it. Same rule as dictation.
    foldable = episodes if force else [e for e in episodes if not e.flagged]

    results = []
    for episode in foldable:
        result = _fold_steps(config, session, episode.steps,
                             source=source)
        result["ended_by"] = episode.ended_by
        # Queue every entry this session touched, merged or created alike. A
        # merged one can still be a near-miss against a *third* entry the
        # lexical pass never related to either.
        if result.get("id"):
            note_pending_check(config, result["id"],
                               session.get("session_id", ""),
                               result.get("status", ""))
        results.append(result)

    flagged = [e for e in episodes if e.flagged]
    if not results:
        return {"status": "too-thin", "steps": 0, "episodes": [],
                "flagged": len(flagged)}

    summary = dict(results[-1])
    summary["episodes"] = results
    summary["flagged"] = len(flagged)
    return summary


def _fold_steps(config: Config, session: dict, steps: list[dict],
                source: str = "capture") -> dict:
    """Fold one episode's steps into a new or updated ledger entry."""
    cwd = session.get("cwd") or ""
    substantive = _substantive(steps)
    if len(substantive) < 2:
        return {"status": "too-thin", "steps": len(substantive)}

    # Parameterise before fingerprinting so machine-specific paths do not
    # fragment otherwise-identical workflows.
    for step in substantive:
        payload = step.get("input") or {}
        for key, value in list(payload.items()):
            if isinstance(value, str):
                payload[key] = parameterize(value, cwd)

    sig = signature(substantive)
    if not sig:
        return {"status": "no-signature"}

    ledger = Ledger(config)
    existing = find_match(sig, list(ledger.all()), config.similarity_threshold)
    intents = _intents_for(session, steps)

    deps_mcp = sorted({s["tool"] for s in substantive if s["tool"].startswith("mcp__")})
    deps_cli = sorted(_cli_dependencies(substantive))

    if existing:
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        if cwd and cwd not in existing.projects:
            existing.projects.append(cwd)
        sid = session.get("session_id", "")
        if sid:
            if sid not in existing.sessions:
                existing.sessions.append(sid)
            # The size of the session union, never a sum — the same rule
            # `similar.fold_into` states and `test_occurrences_count_sessions_
            # not_sightings` pins. It was corrected there and not here, and a
            # session cut into episodes is exactly where the difference shows:
            # several episodes of one session matching this entry each bumped
            # the count while `sessions` deduplicated, so a count that is
            # documented as "distinct sessions" outran the number of sessions
            # that existed. Measured on 76 real sessions: 25 of 467 entries
            # claimed more occurrences than they had sessions, the worst x154
            # against 17 — enough to clear a threshold of 3 inside one sitting,
            # which is the one thing that threshold exists to prevent.
            existing.occurrences = max(len(existing.sessions),
                                       existing.occurrences)
        else:
            # Nothing to deduplicate on. A payload with no session id is
            # malformed, and counting it is the lesser of two wrongs.
            existing.occurrences += 1
        for intent in intents:
            if intent not in existing.intents:
                existing.intents.append(intent)
        del existing.intents[8:]
        # Keep a few variants so divergence and conditional-step detection
        # have something to compare (README 4).
        if len(existing.variants) < 4:
            existing.variants.append(substantive)
        existing.deps_mcp = sorted(set(existing.deps_mcp) | set(deps_mcp))
        existing.deps_cli = sorted(set(existing.deps_cli) | set(deps_cli))
        ledger.save(existing)
        return {"status": "merged", "id": existing.id,
                "occurrences": existing.occurrences,
                "ready": existing.ready(config.recurrence_threshold)}

    entry = Entry(
        id=make_id(sig),
        signature=sig,
        title=_title_for(intents, substantive),
        status=STATUS_CANDIDATE,
        occurrences=1,
        projects=[cwd] if cwd else [],
        sessions=[session.get("session_id", "")] if session.get("session_id") else [],
        intents=intents,
        steps=substantive,
        variants=[substantive],
        deps_mcp=deps_mcp,
        deps_cli=deps_cli,
        source=source,
    )
    ledger.save(entry)
    return {"status": "created", "id": entry.id, "occurrences": 1,
            "ready": False, "source": source}


# -- dictation -------------------------------------------------------------

# Split on explicit sequencing markers, plus a comma that is followed by a new
# actor or a "then" — "you search online, then give me the summary".
_STEP_SPLIT_RE = re.compile(
    r"\s*(?:->|→|=>|;|\.\s+|\bthen\b|\bafter that\b|\bnext\b|"
    r",\s*(?=(?:you|i|we|then|and then|give|send|return|check)\b))\s*",
    re.IGNORECASE)


_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LEAD_IN_RE = re.compile(
    r"(?i)\b(return|output|reply|respond|answer|give\s+me|send|look\s+like|"
    r"format|structure|template)\b[^.]*:\s*$")
_EXAMPLE_LEAD_RE = re.compile(
    r"(?i)\b(look(?:s|ed)?\s+like|for\s+example|e\.g\.|such\s+as|"
    r"something\s+like|sample)\b")


def parse_dictation(text: str) -> list[str]:
    """Break a dictated workflow into ordered steps.

    A bulleted list is ambiguous: it can be the procedure, or it can be the
    output format introduced by "Return:". Prose alongside it decides which —
    substantial prose means the bullets are a format spec, not the steps.
    """
    text = text.strip()
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets = [_BULLET_RE.sub("", line) for line in lines if _BULLET_RE.match(line)]
    prose = " ".join(line for line in lines if not _BULLET_RE.match(line)).strip()
    prose_body = _LEAD_IN_RE.sub("", prose).strip()

    if bullets and len(prose_body) < 40:
        return bullets  # the bullets are the procedure

    steps = [p.strip(" ,.;") for p in _STEP_SPLIT_RE.split(prose_body) if p.strip(" ,.;")]
    if bullets:
        # Keep it visible rather than silently dropping it — but "it should look
        # like: …" introduces a filled-in *example*, not a template. Calling that
        # a format would hand the agent one week's content as the spec.
        label = "Example output" if _EXAMPLE_LEAD_RE.search(prose) else "Output format"
        steps.append(f"{label}: " + " / ".join(bullets))
    return steps or bullets


def fold_dictation(config: Config, text: str, title: str = "") -> dict:
    """Create a ledger candidate from a description the developer typed.

    Dictated candidates bypass the recurrence threshold: it exists to filter
    noise, and an explicit request is not noise.
    """
    cleaned = scrub(text.strip())
    steps = parse_dictation(cleaned)
    if not steps:
        return {"status": "empty"}

    normalized = [re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip() for s in steps]
    sig = "dictated | " + " | ".join(n for n in normalized if n)

    ledger = Ledger(config)
    existing = find_match(sig, [e for e in ledger.all() if e.source == "dictated"],
                          config.similarity_threshold)
    if existing:
        existing.occurrences += 1
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        ledger.save(existing)
        return {"status": "merged", "id": existing.id, "ready": True}

    entry = Entry(
        id=make_id(sig),
        signature=sig,
        # The whole description makes a more useful title than its first step.
        title=(title or cleaned.replace("\n", " "))[:70],
        status=STATUS_CANDIDATE,
        occurrences=1,
        source="dictated",
        intents=[cleaned[: config.max_field_chars]],
        steps=[{"tool": "Stated", "input": {"text": s}} for s in steps],
    )
    ledger.save(entry)
    return {"status": "created", "id": entry.id, "ready": True,
            "steps": len(entry.steps)}


def _intents_for(session: dict, steps: list[dict]) -> list[str]:
    """Stated intent for one episode.

    Prefer the prompts recorded *inside* this episode's span: a session's first
    prompt describes its first task, so using it for every episode is how three
    deploy sessions ended up titled after whatever happened to come first.
    Falls back to the session-wide buffer for sessions captured before the
    sentinel existed, or that never segmented.
    """
    leading: list[str] = []
    trailing: list[str] = []
    seen_work = False
    for step in steps:
        if step.get("tool") != PROMPT_TOOL:
            if step.get("tool") not in _NOISE_TOOLS:
                seen_work = True
            continue
        text = str((step.get("input") or {}).get("text", ""))
        if text:
            (trailing if seen_work else leading).append(text)

    # The operative prompt is the last one before any work started — an episode
    # can absorb earlier prompts that produced nothing (a question the developer
    # asked and dropped), and those must not become the title.
    if leading:
        own = [leading[-1]] + leading[:-1] + trailing
    else:
        own = trailing
    if own:
        return own[:5]
    return list(session.get("prompts", []))[:5]


def _cli_dependencies(steps: list[dict]) -> set[str]:
    """Programs the workflow shells out to — declared deps (README 5)."""
    common = {"cd", "ls", "echo", "cat", "true", "false", "export", "source"}
    # Shell grammar, not programs. A real skill declared `requires_cli: ["\\",
    # "do", "done", "for", "grep"]` — it was telling the reader to install `do`
    # and `done`, because a `for f in *.md; do …; done` loop splits on `;` into
    # chunks whose first token is a keyword. Anything a shell would parse as
    # syntax cannot be a PATH dependency.
    # A loop or conditional *header* contains no program at all — `for f in
    # *.md` names a variable and a glob — so the whole chunk goes. A keyword
    # that merely precedes a command does not: `do npm test` still depends on
    # npm, so those are stepped past instead.
    headers = {"for", "while", "until", "if", "elif", "case", "select"}
    keywords = {"do", "done", "then", "else", "fi", "esac", "in", "function",
                "time", "coproc", "!", "{", "}", "[[", "]]", "\\"}
    found: set[str] = set()
    for step in steps:
        if step.get("tool") != "Bash":
            continue
        command = str((step.get("input") or {}).get("command", ""))
        for chunk in command.replace("&&", ";").replace("||", ";").split(";"):
            tokens = chunk.strip().split()
            if not tokens:
                continue
            # Step past leading shell grammar rather than discarding the
            # chunk: `do npm test` would otherwise lose `npm` along with `do`.
            index = 0
            while index < len(tokens) and (tokens[index] in keywords
                                           or not tokens[index].strip("\\")):
                index += 1
            if index < len(tokens) and tokens[index] in headers:
                continue
            if index >= len(tokens):
                continue
            program = tokens[index]
            if "=" in program or program.startswith(("$", "(")):
                continue
            if "/" in program:
                # A path-invoked script is a file in the repo, not a PATH
                # dependency. Staleness checking covers those instead.
                continue
            if program and program not in common and program.isascii():
                found.add(program)
    return found


def _title_for(intents: list[str], steps: list[dict]) -> str:
    if intents:
        first = intents[0].strip().splitlines()[0]
        return (first[:70] + "…") if len(first) > 70 else first
    for step in steps:
        if step.get("tool") == "Bash":
            cmd = str((step.get("input") or {}).get("command", "")).strip()
            if cmd:
                return (cmd[:70] + "…") if len(cmd) > 70 else cmd
    return "captured workflow"


def keep_current(config: Config, session_id: str | None = None) -> dict:
    """Fold the in-flight session now, instead of waiting for it to end.

    `SessionEnd` is otherwise the only thing that folds, so without this there
    is no way to say "that thing I just did is worth keeping" without closing
    the session. That matters more than it looks: recurrence is the automatic
    route to a candidate and it has never fired on real work, which leaves an
    explicit save as the only path from work to skill.

    Guards are bypassed — an explicit save is not noise — and the buffer is
    cleared afterwards so the rest of the session accumulates fresh rather than
    being folded twice.
    """
    if session_id:
        path = _session_file(config, session_id)
        if not path.exists():
            return {"status": "no-session"}
    else:
        newest = sorted(config.sessions_dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if not newest:
            return {"status": "no-session"}
        path = newest[0]
        session_id = path.stem

    session = _load_session(config, session_id)
    # `handle_prompt` writes a UserPrompt sentinel into the step stream, so the
    # raw list is never empty once anything has been said. Count the work.
    from .segment import is_prompt
    if not [s for s in session.get("steps", []) if not is_prompt(s)]:
        return {"status": "nothing-yet"}
    result = fold_session(config, session, force=True, source="kept")
    try:
        path.unlink()
    except OSError:
        pass
    return result
