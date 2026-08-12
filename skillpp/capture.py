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

# Tool inputs worth keeping. Anything else is recorded by name only.
_KEEP_INPUT = {
    "Bash": ("command", "description"),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("file_path",),
}
# Pure exploration: recorded, but never the reason a workflow is proposed.
_NOISE_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch"}


def log_error(config: Config, message: str) -> None:
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        with config.log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
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


def handle_prompt(config: Config, payload: dict) -> None:
    """UserPromptSubmit — capture stated intent."""
    session_id = str(payload.get("session_id", "unknown"))
    prompt = scrub(str(payload.get("prompt", "")).strip())
    if not prompt:
        return
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))
    if len(session["prompts"]) < 40:
        session["prompts"].append(prompt[: config.max_field_chars])
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


def fold_session(config: Config, session: dict) -> dict:
    """Turn a finished session into a new or updated ledger entry."""
    cwd = session.get("cwd") or ""
    steps = session.get("steps", [])
    substantive = [s for s in steps if s.get("tool") not in _NOISE_TOOLS]
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
    intents = [p for p in session.get("prompts", [])][:5]

    deps_mcp = sorted({s["tool"] for s in substantive if s["tool"].startswith("mcp__")})
    deps_cli = sorted(_cli_dependencies(substantive))

    if existing:
        existing.occurrences += 1
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        if cwd and cwd not in existing.projects:
            existing.projects.append(cwd)
        sid = session.get("session_id", "")
        if sid and sid not in existing.sessions:
            existing.sessions.append(sid)
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
    )
    ledger.save(entry)
    return {"status": "created", "id": entry.id, "occurrences": 1, "ready": False}


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


def _cli_dependencies(steps: list[dict]) -> set[str]:
    """Programs the workflow shells out to — declared deps (README 5)."""
    common = {"cd", "ls", "echo", "cat", "true", "false", "export", "source"}
    found: set[str] = set()
    for step in steps:
        if step.get("tool") != "Bash":
            continue
        command = str((step.get("input") or {}).get("command", ""))
        for chunk in command.replace("&&", ";").replace("||", ";").split(";"):
            tokens = chunk.strip().split()
            if not tokens:
                continue
            program = tokens[0]
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
