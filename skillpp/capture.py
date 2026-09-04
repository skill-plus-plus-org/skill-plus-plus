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
from .segment import PROMPT_TOOL, feeds_a_write, is_prompt, segment

# Tool inputs worth keeping. Anything else is recorded by name only.
#
# `Read`'s path is kept because `_substantive` below needs it: read-then-edit is
# how a procedure names the file it operates on, and the rule that keeps those
# reads compares the read's `file_path` against the write's. Without the field
# that comparison is against `None`, so the rule could never fire — measured on
# a real session, four `Read` steps captured and none kept, while the same
# session replayed from its transcript kept three.
#
# It was left out deliberately once, on the grounds that a `Read` is exploration
# and exploration should not reach a signature. That holds for a read that leads
# nowhere, and `_substantive` already drops those. It does not hold for the read
# that fed the edit.
#
# A step that keeps only a path cannot be described. Asked what the `Write` that
# produced COVERAGE.md did, a model shown `{"file_path": ".../COVERAGE.md"}`
# answered "Analyze the existing coverage report" — it wrote one. Every field
# here is bounded by `max_field_chars`, so widening it costs length, not shape.
_KEEP_INPUT = {
    "Bash": ("command", "description"),
    "Write": ("file_path", "content"),
    "Edit": ("file_path", "old_string", "new_string"),
    "NotebookEdit": ("file_path", "new_source"),
    "Read": ("file_path",),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path"),
    "WebFetch": ("url", "prompt"),
    "WebSearch": ("query",),
    "Task": ("description", "subagent_type"),
    "Skill": ("skill", "args"),
}

# How much of a tool's reply to keep. It is the only record of what a step
# *did* — `_failed` reads it for a boolean and everything else was dropped, so
# "File created successfully at …" and "no matches found" were both invisible
# downstream. Short on purpose: the head of a reply says what happened, and the
# tail is usually payload.
_RESPONSE_CHARS = 400
# Pure exploration: recorded, but never the reason a workflow is proposed.
# UserPrompt is the segmentation sentinel written by handle_prompt — a task
# boundary, never a step of the workflow itself.
_NOISE_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch",
                PROMPT_TOOL}


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
        # One predicate, shared with `trim_leading_exploration`, which used to
        # cut these again whenever one opened an episode.
        if feeds_a_write(steps, index):
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


def _reply_text(response, limit: int | None = _RESPONSE_CHARS) -> str:
    """What the tool said back, flattened and bounded.

    Whatever shape the harness uses — a string, a dict of fields, a list of
    content blocks — reduce it to the leading text. `_failed` already inspects
    this object for a verdict; this keeps the part a reader (or a model) needs
    to know what actually happened.

    *limit* of ``None`` returns the whole thing. The describer wants that: what
    is stored is cut to `_RESPONSE_CHARS`, and reading the stored field back
    would cap the model below the budget measured to help it.
    """
    if isinstance(response, str):
        text = response
    elif isinstance(response, dict):
        for key in ("stdout", "output", "content", "result", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        else:
            text = " ".join(str(v) for v in response.values()
                            if isinstance(v, (str, int, float)))
    elif isinstance(response, list):
        text = " ".join(str(b.get("text", b)) if isinstance(b, dict) else str(b)
                        for b in response)
    else:
        text = str(response or "")
    flat = " ".join(text.split())
    return flat if limit is None else flat[:limit]


def _narration(payload: dict) -> str:
    """What the assistant said just before this step, from the transcript.

    The command stream only implies completion; the narration around it states
    it in plain language — `tests/benchmarks/boundaries.py` relies on exactly
    this when a person is asked to label a boundary, and capture never had it.

    The hook payload carries `transcript_path`. Everything here is best effort:
    a missing or unreadable transcript costs the narration and nothing else.
    """
    path = payload.get("transcript_path")
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            rows = fh.readlines()[-40:]
    except OSError:
        return ""
    # The text *preceding* the most recent tool call, which is this one — the
    # hook fires after the call, so the transcript already holds it. Taking the
    # text that follows instead returns nothing live, and on a finished
    # transcript returns that session's closing words for every step: three
    # consecutive steps were once given the same sentence, describing a file
    # written long after the first of them ran.
    pending: list[str] = []
    before_last_call: list[str] = []
    for line in rows:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = row.get("message") or {}
        if row.get("type") != "assistant" or not isinstance(
                message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            # `thinking` as well as `text`, which `boundaries.py` also takes.
            # Collecting only `text` returned nothing on real transcripts: what
            # precedes a tool call is usually the reasoning that chose it, and
            # that is the part worth having.
            if kind in ("text", "thinking") and (block.get(kind) or "").strip():
                pending.append(block[kind])
            elif kind == "tool_use":
                before_last_call = pending
                pending = []
    text = " ".join(" ".join(before_last_call).split())
    if len(text) > _RESPONSE_CHARS:
        text = text[:_RESPONSE_CHARS].rsplit(" ", 1)[0] + " …"
    return text


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
    # What the tool said back, and what the assistant said going in. Scrubbed
    # like everything else, and empty rather than absent when unavailable, so a
    # reader can tell "nothing was said" from "this shape was never recorded".
    # Named by who produced them. `reply`/`said` said nothing about the actor,
    # and in a list of four hundred steps that is the first question a reader
    # has. `serves` is the hierarchy: which of the developer's requests this
    # step is working on, so a flat list can still be read as nested work.
    step["serves"] = sum(1 for s in session["steps"] if is_prompt(s)) or 1
    returned = scrub(_reply_text(payload.get("tool_response")))
    if returned:
        step["tool_returned"] = returned
    note = scrub(_narration(payload))
    if note:
        step["assistant_note"] = note
    # One sentence saying what this step did and the part it plays, written now
    # while the reply and the narration are still here. Everything downstream —
    # `is_marker`'s git verbs, `is_read_only`'s command list, `render_step`'s
    # four tool shapes — is guessing at this from a command string.
    if config.describe_steps:
        try:
            from .boundary import describe_in_session
            # The flattened reply, not `step["tool_returned"]` — that one is
            # already cut to storage size, and reading it back would cap the
            # describer below the budget that was measured to help.
            summary = describe_in_session(config, session, step,
                                          reply=_reply_text(
                                              payload.get("tool_response"),
                                              limit=None))
            if summary:
                step["summary"] = summary
                session["summarised_by"] = config.local_model
        except Exception as exc:  # noqa: BLE001 - a hook never raises at a dev
            log_error(config, f"describe failed: {type(exc).__name__}: {exc}")
    # Did this end the task? Asked now, while the span behind it is still what
    # the developer was doing, and recorded so `segment` reads a boolean instead
    # of re-deriving an ending from a vocabulary of git verbs.
    #
    # The `try` wraps the judgement only. A model that is missing, slow or
    # incoherent costs the verdict, never the step — losing the step would lose
    # the work, which is the one thing capture exists to prevent.
    # The key is written only when the model actually answered. `end: None` on
    # every step would read downstream as "this stream was judged, and nothing
    # ended" — a session with no boundaries at all — when what happened is that
    # nothing was asked. Absent means unjudged, and `segment` falls back to the
    # vocabulary rules for the whole stream, which is the right behaviour when
    # Ollama is not running.
    if config.judge_boundaries:
        try:
            from .boundary import judge_in_session
            verdict = judge_in_session(config, session, step)
            if verdict is not None:
                step["end"] = verdict
        except Exception as exc:  # noqa: BLE001 - a hook never raises at a dev
            log_error(config, f"boundary judge failed: {type(exc).__name__}: {exc}")
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
        return _within_budget(own)
    return _within_budget(list(session.get("prompts", [])))


# A count is the wrong bound. Five prompts is below what an ordinary directed
# task takes: the session this was measured on ran seven turns, and the one the
# cap dropped was "check the docstring — confirm TOPICS is still the single
# source of truth", which is the verification step the procedure exists to
# perform. Budget by characters instead, so a task keeps its shape while a
# runaway session still cannot bloat an entry.
_INTENT_BUDGET_CHARS = 2000


def _within_budget(prompts: list[str]) -> list[str]:
    """As many stated intents as fit the budget, in order, never fewer than one."""
    out: list[str] = []
    spent = 0
    for text in prompts:
        if out and spent + len(text) > _INTENT_BUDGET_CHARS:
            break
        out.append(text)
        spent += len(text)
    return out


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


# `git commit -m "…"`, `-m '…'`, and the heredoc form an agent writing a long
# message uses. Narrow on purpose: an unrecognised form falls through to the
# prompt rather than being guessed at.
# `git -C <path> commit` and `git -c user.email=… commit` are commits: the
# subject must be found past git's global options, not only immediately after
# `git`. A real session committed this way and the entry was titled from a
# prompt instead.
#
# Only the first line of the message is taken, and deliberately without
# matching the heredoc's closing delimiter: `max_field_chars` truncates a long
# commit body at 2000 characters, so the terminator is often not there to
# match. The subject always is.
_COMMIT_HEREDOC_RE = re.compile(
    r"\bgit\b[^\n]*?\bcommit\b[^\n]*<<-?['\"]?\w+['\"]?\r?\n([^\n]+)")
_COMMIT_INLINE_RE = re.compile(
    r"""\bgit\b[^\n]*?\bcommit\b[^\n]*?-m\s+(["'])([^\n]+?)\1""")
# Conventional Commits: `test(eval): ` is provenance, not a task name.
_CONVENTIONAL_RE = re.compile(r"\A[a-z]+(?:\([^)]*\))?!?:\s*")


def _subject_of(steps: list[dict]) -> str:
    """The last commit's subject line, if a commit is what ended this episode.

    A commit message is written after the work and says what it accomplished;
    the opening prompt is written before and says what was wrong. On the
    session this was measured against, the prompt gave the entry the title
    "Looks good — commit" while the commit said "add walkthrough-card case for
    Desk Booking". The second is the name of a procedure; the first is not.
    """
    for step in reversed(steps):
        if step.get("tool") != "Bash" or step.get("failed"):
            continue
        command = str((step.get("input") or {}).get("command", ""))
        # Not `"git commit" in command`: global options sit between the two
        # words, and `git -C <path> commit` is how an agent commits without
        # cd-ing first. The regexes below already allow for it.
        if "commit" not in command:
            continue
        match = _COMMIT_HEREDOC_RE.search(command)
        body = match.group(1) if match else ""
        if not body:
            match = _COMMIT_INLINE_RE.search(command)
            body = match.group(2) if match else ""
        body = body.strip()
        if not body:
            continue
        return _CONVENTIONAL_RE.sub("", body).strip()
    return ""


def _title_for(intents: list[str], steps: list[dict]) -> str:
    subject = _subject_of(steps)
    if subject:
        return (subject[:70] + "…") if len(subject) > 70 else subject
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
