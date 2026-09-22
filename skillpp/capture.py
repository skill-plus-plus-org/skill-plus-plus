"""Hook handlers — the capture layer.

Wired to Claude Code hooks (docs/design.md §8):

* ``UserPromptSubmit`` records stated intent — the half of the picture a raw
  command log can never recover.
* ``PostToolUse`` records what actually ran.
* ``SessionEnd`` stamps the session and spawns the fold; the judge and the
  embeddings run in that detached worker, never in the hook.
* ``SessionStart`` sweeps whatever an earlier session left behind.

**Every handler is fail-safe.** A hook that raises could disrupt the
developer's session, so all errors are swallowed to a log file and the process
always exits 0. Capture is never worth breaking someone's work over.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import socket
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .ledger import Entry, Ledger, new_id, STATUS_CANDIDATE
from .local import LocalModelUnavailable
from .normalize import parameterize
from .sanitize import scrub, scrub_obj
from .segment import (PROMPT_TOOL, feeds_a_write, is_prompt, segment,
                      was_judged)
from .summary import clip_title

# Tool inputs worth keeping. Anything else is recorded by name only.
#
# `Read`'s path is kept because `_substantive` below needs it: read-then-edit is
# how a procedure names the file it operates on, and the rule that keeps those
# reads compares the read's `file_path` against the write's. Without the field
# that comparison is against `None`, and the rule never fires.
#
# Keeping the path does not let exploration into a signature: a read that leads
# nowhere is still dropped by `_substantive`. Only the read that fed an edit
# stays, and it needs the path to be recognised.
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
# Steps that are never the reason a workflow is proposed: looking around, the
# agent's own bookkeeping (`TodoWrite`, `Task`), and `PROMPT_TOOL`, the
# sentinel `handle_prompt` writes at a task boundary. `_substantive` keeps a
# `Read` from this set when it feeds a write.
#
# Deliberately not `segment.is_read_only`, which also counts read-only shell
# commands: those are often the check a procedure exists to perform. On the
# recorded P-F sessions, writing a post to a word limit, it would drop every
# `wc -w`; on C-F1, the `git diff` before the commit.
_NOISE_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch",
                PROMPT_TOOL}


def _substantive(steps: list[dict]) -> list[dict]:
    """The steps that are the work: everything outside `_NOISE_TOOLS`, plus a
    `Read` that feeds a write (`segment.feeds_a_write`).

    That read names the file the procedure operates on. On the recorded
    sessions it is about twice as common as a read inside a run of reads, so
    dropping it would discard more of the procedure than of the exploration. A
    read that leads nowhere still goes.
    """
    keep: list[dict] = []
    for index, step in enumerate(steps):
        tool = step.get("tool")
        if tool not in _NOISE_TOOLS:
            keep.append(step)
            continue
        # The same predicate `trim_leading_exploration` asks, so a read kept
        # here is not cut again for opening an episode.
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


def _session_file(config: Config, session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    return config.sessions_dir / f"{safe}.json"


@functools.lru_cache(maxsize=512)
def project_of(cwd: str) -> str:
    """The project a working directory belongs to: its git repo's root.

    Candidates and skills are bound to one project, and a session started in a
    subfolder of a repo is still that repo's work. Without a repo above it, the
    folder itself is the project; with no folder at all, there is none ("").
    Only stat calls, no git: this runs for every entry a fold compares.
    """
    if not cwd:
        return ""
    path = Path(cwd).expanduser()
    for folder in (path, *path.parents):
        if (folder / ".git").exists():
            return str(folder)
    return str(path)


def projects_of(entry) -> set[str]:
    """Every project an entry was seen in. One, except for entries banked
    before candidates were bound to a project, which may hold several."""
    return {project_of(cwd) for cwd in entry.projects} or {""}


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
    """What the tool said back, flattened, scrubbed and bounded.

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
    # Scrubbed before the cut, for the reason `_clip` gives.
    flat = scrub(" ".join(text.split()))
    return flat if limit is None else flat[:limit]


# A hook fires just after the call it reports, so the last rows of the
# transcript hold everything it looks for. Reading the whole file on every
# tool call would cost more the longer the session runs.
_TRANSCRIPT_TAIL = 40


def _transcript_rows(path: str | None, tail: int | None = None) -> Iterator[dict]:
    """The transcript's rows, parsed; with *tail*, only the last that many.

    Best effort: no path or an unreadable file is no rows, and a line that is
    not JSON is skipped.
    """
    if not path:
        return
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in (fh.readlines()[-tail:] if tail else fh):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _narration(payload: dict) -> tuple[str, str]:
    """What the assistant said around this step: `(lead_in, closes_previous)`.

    The command stream only implies completion; the narration around it states
    it in plain language — `tests/benchmarks/boundaries.py` relies on exactly
    this when a person is asked to label a boundary, and capture never had it.

    Two pieces, because text written between two tool calls does not all belong
    to the second one. A *prompt* arriving in the gap splits it: what came
    before the prompt reports the work that just finished, and what came after
    introduces the work about to start.

    Merged, a report of finished work lands on the first step of the next
    task, describing work that has not happened yet — an investigation's whole
    deliverable can end up stored inside the task after it.

    The hook payload carries `transcript_path`. Everything here is best effort:
    a missing or unreadable transcript costs the narration and nothing else.
    """
    # The text *preceding* the most recent tool call, which is this one — the
    # hook fires after the call, so the transcript already holds it. Taking the
    # text that follows instead returns nothing live, and on a finished
    # transcript gives every step the session's closing words.
    pending: list[str] = []
    before_last_call: list[str] = []
    # Text that was already closed off by a prompt before this call ran. Held
    # separately so it can be attributed backwards rather than to this step.
    closing: list[str] = []
    closes_previous: list[str] = []
    for row in _transcript_rows(payload.get("transcript_path"), _TRANSCRIPT_TAIL):
        message = row.get("message") or {}
        # A real prompt, not an envelope the harness injected. Everything said
        # before it belongs to the step that preceded it.
        if row.get("type") == "user" and isinstance(message.get("content"), str):
            text = message["content"].strip()
            if text and not text.startswith(_ENVELOPE_PREFIXES):
                closing, pending = pending, []
            continue
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
                before_last_call, pending = pending, []
                # Only the *first* call after the prompt carries the closing
                # text back; by the second, the hook for the first has already
                # attributed it. Captured before clearing, because this call may
                # be the last one in the window and is the one being reported.
                closes_previous, closing = closing, []
    return _clip(before_last_call), _clip(closes_previous)


def _trailing_narration(payload: dict) -> str:
    """Everything said after the last tool call, for a session that is ending.

    `_narration` attributes closing words backwards when a *prompt* closes them
    off, which is what happens mid-session. A task that ends the session has no
    following prompt and no following tool call, so its completion report would
    be dropped — the same defect at the other boundary.

    Best effort in the same way, and doubly so: `SessionEnd` may not carry
    `transcript_path` at all, in which case this costs the note and nothing
    else.
    """
    pending: list[str] = []
    for row in _transcript_rows(payload.get("transcript_path"), _TRANSCRIPT_TAIL):
        message = row.get("message") or {}
        if row.get("type") != "assistant" or not isinstance(
                message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in ("text", "thinking") and (block.get(kind) or "").strip():
                pending.append(block[kind])
            elif kind == "tool_use":
                pending = []
    return _clip(pending)


def _clip(parts: list[str]) -> str:
    # Scrubbed before the cut: a cut can leave a secret shorter than the
    # patterns that recognise it, and backing off to the last space does not
    # help when there is none before the cut, in a path or a line of JSON.
    text = scrub(" ".join(" ".join(parts).split()))
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


# How much of the agent's reply to one prompt a candidate keeps. The reply is
# where a conversation-driven procedure lives — the proposal, the fact-check,
# the question before building. The longest reply in the recorded runs was
# about 7.6k characters.
_REPLY_CHARS = 8000
# A `user` row with string content that is not something the person typed: a
# tool's image result is recorded this way.
_NOT_A_PROMPT = ("[Image",)


def _transcript_turns(path: str | None) -> list[tuple[str, list[str]]]:
    """Each real prompt in a transcript, with the text the agent replied.

    `text` blocks only, never `thinking`: the reply is what the person read.
    Best effort — no path or an unreadable file is no turns.
    """
    turns: list[tuple[str, list[str]]] = []
    for row in _transcript_rows(path):
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "user" and isinstance(content, str):
            text = content.strip()
            if text and not text.startswith(_ENVELOPE_PREFIXES + _NOT_A_PROMPT):
                turns.append((text, []))
        elif row.get("type") == "assistant" and turns and isinstance(content, list):
            turns[-1][1].extend(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and (block.get("text") or "").strip())
    return turns


def _reply_before(path: str | None, current: str | None = None) -> str:
    """The agent's reply to the latest finished prompt, scrubbed and clipped.

    *current* is a prompt just submitted. Its row may already be in the
    transcript when the hook runs, and then the finished prompt is the one
    before it.
    """
    turns = _transcript_turns(path)
    if turns and current is not None and turns[-1][0] == current.strip():
        turns = turns[:-1]
    if not turns:
        return ""
    text = scrub("\n\n".join(part.strip() for part in turns[-1][1]))
    if len(text) > _REPLY_CHARS:
        text = text[:_REPLY_CHARS].rsplit(" ", 1)[0] + " …"
    return text


def _attach_reply(session: dict, path: str | None, current: str | None = None) -> None:
    """Give the last prompt marker its reply, once."""
    marker = next((s for s in reversed(session.get("steps", [])) if is_prompt(s)), None)
    if marker is None or marker.get("reply"):
        return
    reply = _reply_before(path, current)
    if reply:
        marker["reply"] = reply


def handle_prompt(config: Config, payload: dict) -> None:
    """UserPromptSubmit — capture stated intent."""
    session_id = str(payload.get("session_id", "unknown"))
    prompt = scrub(str(payload.get("prompt", "")).strip())
    if not prompt or prompt.startswith(_ENVELOPE_PREFIXES):
        return
    session = _load_session(config, session_id)
    session.setdefault("cwd", payload.get("cwd", ""))
    if payload.get("transcript_path"):
        session["transcript"] = payload["transcript_path"]
    # The previous prompt's reply is complete now that the next one arrived.
    _attach_reply(session, session.get("transcript"),
                  current=str(payload.get("prompt", "")))
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
    if payload.get("transcript_path"):
        session["transcript"] = payload["transcript_path"]

    if len(session["steps"]) >= config.max_steps_per_session:
        return

    raw_input = payload.get("tool_input") or {}
    if not isinstance(raw_input, dict):
        raw_input = {"value": raw_input}

    # A Skill invocation is how tiering learns what is actually used (docs/design.md §6).
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
    returned = _reply_text(payload.get("tool_response"))
    if returned:
        step["tool_returned"] = returned
    note, closes_previous = _narration(payload)
    if note:
        step["assistant_note"] = note
    # What was said after the previous step and before the prompt that follows
    # it — that step's own completion report, not this one's lead-in. Written
    # backwards onto the step it describes. `setdefault`, because the hook may
    # fire again for the same gap and the first attribution is the right one.
    if closes_previous:
        earlier = [s for s in session["steps"] if not is_prompt(s)]
        if earlier:
            earlier[-1].setdefault("closing_note", closes_previous)
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
    # No judging here any more. Boundaries are asked about once per prompt at
    # session end (`handle_session_end`), because the question needs the steps
    # that came *after* the gap and those do not exist yet. That also takes a
    # synchronous model call off every single tool call.
    session["steps"].append(step)
    _save_session(config, session)


def handle_session_end(config: Config, payload: dict) -> dict:
    """SessionEnd / Stop — summarise the session into the ledger."""
    session_id = str(payload.get("session_id", "unknown"))
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}

    session = _load_session(config, session_id)
    # Where the tasks divide. One question per prompt, asked now because it
    # needs the steps that followed each gap. A model that is missing, slow or
    # incoherent costs the verdicts, never the steps — and a session with no
    # verdicts is offline, kept rather than banked.
    #
    # Not asked again once folding has begun (`folded`): the episodes already
    # banked are recorded by position, and a second judgement can cut
    # differently and make those positions name other steps.
    # The last reply first: the judge can be shown what the assistant said, and
    # live it must see the same as the benchmark, which has every reply. The
    # keep path already attaches before judging.
    _attach_reply(session, payload.get("transcript_path") or session.get("transcript"))

    if config.judge_boundaries and "folded" not in session:
        try:
            from .boundary import judge_session
            judge_session(config, session)
        except Exception as exc:  # noqa: BLE001 - a hook never raises at a dev
            log_error(config, f"boundary judge failed: {type(exc).__name__}: {exc}")

    # The last task's own completion report, said after its final tool call.
    # Attached before folding so the episode carries it.
    trailing = _trailing_narration(payload)
    if trailing:
        work = [s for s in session.get("steps", []) if not is_prompt(s)]
        if work:
            work[-1].setdefault("closing_note", trailing)
            _save_session(config, session)
    result = fold_session(config, session, persist=True)
    # Offline: the session was never folded, so this file is the only copy of
    # the work. Losing the step is the one thing capture exists to prevent —
    # being offline costs the candidate, never the record.
    #
    # Stamped rather than merely left behind: a live session has a file too, and
    # counting those as held would report work lost from a session still being
    # written. `skillpp stats` reads this key, not the glob.
    if result.get("status") == "offline":
        session["held"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": result.get("reason", ""),
        }
        _save_session(config, session)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    return result



def mark_ending(config: Config, session_id: str, transcript: str | None = None) -> dict:
    """Record that a session ended. The fold itself happens elsewhere.

    Nothing slow runs in the hook. One cold model call can reach
    `boundary.DEFAULT_TIMEOUT`, Claude Code gives a hook about a minute and
    less when the app quits, and a killed hook leaves the work waiting for
    `fold_pending`'s idle rule.

    The stamp is written here, by the hook, and not by the worker: a worker that
    never starts must still leave a trace, and that trace is what lets
    `fold_pending` tell "launched and died" from "still running".
    """
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}
    session = _load_session(config, session_id)
    session.setdefault("session_id", session_id)
    if transcript:
        session["transcript"] = transcript
    session["ending"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_session(config, session)
    return {"status": "ending", "session": session_id}


def fold_session_now(config: Config, session_id: str, *,
                     transcript: str | None = None) -> dict:
    """Fold one ended session, alone. The only way into `handle_session_end`.

    Both the detached worker and the pending sweep come through here, which is
    what stops the two of them banking the same session twice and counting
    every episode in it twice.
    """
    path = _session_file(config, session_id)
    if not path.exists():
        return {"status": "no-session"}
    with _locked(_lock_file(config, session_id), _FOLD_LOCK_SECONDS,
                 "fold-session") as got:
        if not got:
            # Someone is already on it. Touch nothing: the holder owns the file.
            return {"status": "folding", "session": session_id}
        session = _load_session(config, session_id)
        return handle_session_end(config, {
            "session_id": session_id,
            "transcript_path": (transcript or session.get("transcript")
                                or _find_transcript(session_id)),
        })

# How long a session file may sit untouched before it counts as ended. A
# session that never fires `SessionEnd` — a CLI window closed, an app killed —
# otherwise waits forever. Long, because a desktop session can be picked up again
# hours later, and folding it early would bank half the work.
PENDING_IDLE_HOURS = 12.0
# A `fold-pending` run older than this is assumed dead, not still folding.
_PENDING_LOCK_SECONDS = 3600
# A session stamped `ending` whose worker has not taken the lock by now was
# launched and died — the app was force-quit, or the machine went to sleep
# mid-fold. Short, because the point of stamping is to recover in minutes
# instead of waiting out `PENDING_IDLE_HOURS`.
_FOLD_GRACE_SECONDS = 120.0
# A ceiling on one session's fold. Bounds the damage a recycled pid can do:
# past this the lock is stale whatever `os.kill` says.
_FOLD_LOCK_SECONDS = 1800


def _lock_file(config: Config, session_id: str) -> Path:
    """The lock guarding one session's fold, beside the session it guards.

    `fold_pending` globs `*.json`, so a `.lock` sibling is never mistaken for a
    session, and a directory listing shows at a glance what is being folded.
    """
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    return config.sessions_dir / f"{safe}.lock"


def _lock_alive(path: Path, max_age: float) -> bool:
    """Is the process that took this lock still working?

    The worker is started with `start_new_session=True`, so it is nobody's
    child and `waitpid` is not available to anyone — `os.kill(pid, 0)` is the
    only liveness signal left, which is why the pid is written into the lock.

    Every ambiguous case answers **alive**: unreadable contents, no pid, a lock
    taken on another machine. Waiting costs at most *max_age*; folding a session
    a live worker is already folding counts its episodes twice, and
    `occurrences` cannot be un-incremented without hand-editing the ledger.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age > max_age:
        return False
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        pid = int(held["pid"])
        host = str(held.get("host", ""))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return True
    if host and host != socket.gethostname():
        # Another machine's pid says nothing about this one. Age it out instead.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, still running.
        return True
    except OSError:
        return True
    return True


def _acquire_lock(path: Path, max_age: float, what: str) -> bool:
    """Take the lock, breaking it once if the holder is gone."""
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 2 or _lock_alive(path, max_age):
                return False
            try:
                path.unlink()
            except OSError:
                return False
            # One retry, never a loop: two processes that keep breaking each
            # other's locks would livelock instead of one of them folding.
            continue
        except OSError:
            return False
        try:
            os.write(fd, json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(), "what": what,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }).encode("utf-8"))
        except OSError:
            pass
        finally:
            os.close(fd)
        return True
    return False


@contextlib.contextmanager
def _locked(path: Path, max_age: float, what: str):
    """Yield whether the lock was taken; release it however the body ends."""
    got = _acquire_lock(path, max_age, what)
    try:
        yield got
    finally:
        if got:
            try:
                path.unlink()
            except OSError:
                pass


def _find_transcript(session_id: str) -> str | None:
    """The transcript for a session recorded before capture kept its path."""
    for path in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        return str(path)
    return None


def fold_pending(config: Config, *, exclude: str | None = None,
                 idle_hours: float = PENDING_IDLE_HOURS) -> list[dict]:
    """Bank the sessions that ended without being banked.

    `SessionEnd` stamps `ending` and spawns a worker, so this sweep is the
    safety net rather than the normal path. It catches a worker that was
    launched and died with the app, a session held because no model answered,
    and one where `SessionEnd` never fired at all.
    Everything goes through `fold_session_now`, so the judge, the fold, the
    per-session lock and the held stamp are the ones a normal end uses, and
    `folded` keeps a partial retry from counting an episode twice. *exclude* is
    the session that is starting, which is live by definition.
    """
    results = []
    with _locked(config.root / "fold-pending.lock", _PENDING_LOCK_SECONDS,
                 "fold-pending") as got:
        if not got:
            return [{"status": "locked"}]
        for path in sorted(config.sessions_dir.glob("*.json")):
            sid = path.stem
            if sid == exclude:
                continue
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
                idle = (time.time() - path.stat().st_mtime) / 3600
            except (OSError, json.JSONDecodeError):
                continue
            if not _is_pending(session, idle, idle_hours):
                results.append({"session": sid, "status": "live"})
                continue
            # Checked before folding only so the outcome can say "folding"
            # rather than "locked"; `fold_session_now` takes the lock itself,
            # so nothing rests on this being race-free.
            if _lock_alive(_lock_file(config, sid), _FOLD_LOCK_SECONDS):
                results.append({"session": sid, "status": "folding"})
                continue
            results.append({"session": sid, **fold_session_now(config, sid)})
        _sweep_orphan_locks(config)
    return results


def _is_pending(session: dict, idle: float, idle_hours: float) -> bool:
    """Has this session ended without being banked?

    Three rules, covering three different failures, none of them redundant:

    * ``held`` — a fold that ran and could not reach a model.
    * ``ending`` past the grace — a fold that was launched and died with it.
      Without the stamp it would look like a live session and wait out the idle
      rule.
    * idle — the only rule that catches a session where `SessionEnd` never
      fired at all: a window closed, a laptop shut down.
    """
    if session.get("held"):
        return True
    ending = session.get("ending")
    if ending:
        try:
            stamped = datetime.fromisoformat(str(ending))
        except ValueError:
            # A corrupt stamp may delay a fold; it must never cause one.
            stamped = None
        if stamped is not None:
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            waited = (datetime.now(timezone.utc) - stamped).total_seconds()
            if waited > _FOLD_GRACE_SECONDS:
                return True
    return idle >= idle_hours


def _sweep_orphan_locks(config: Config) -> None:
    """Drop lock files whose session is gone and whose holder is not running.

    A fold that is SIGKILLed never runs the `finally` that releases its lock,
    and the session file it banked is already unlinked — so without this the
    lock outlives everything it was guarding.
    """
    for lock in config.sessions_dir.glob("*.lock"):
        if lock.with_suffix(".json").exists():
            continue
        if _lock_alive(lock, _FOLD_LOCK_SECONDS):
            continue
        try:
            lock.unlink()
        except OSError:
            pass


def fold_session(config: Config, session: dict, *, force: bool = False,
                 source: str = "capture", persist: bool = False) -> dict:
    """Turn a finished session into ledger entries — one per task.

    The session is cut into episodes first (see ``skillpp.segment``) and each
    is folded separately: as one entry, a session holding several unrelated
    tasks gets a signature that describes none of them, and never recurs.

    Returns the last folded episode's result, with ``episodes`` listing every
    outcome; a session that is one episode returns just that episode's result.
    """
    # No verdicts means no local model answered, which means skillpp is offline
    # for this session. Banking anyway would mean guessing the boundaries from
    # git verbs — measured worse than making no cuts at all, and worse again
    # than the judge. Reported rather than returned empty, because a silent
    # nothing is indistinguishable from a session that held no work.
    # No tool call at all is a conversation, not an unjudged session: there is
    # no step a verdict could sit on. Read as offline, every chat-only session
    # would be held forever under "<model> did not answer" with the model up.
    if not [s for s in session.get("steps", []) if not is_prompt(s)]:
        return {"status": "too-thin", "steps": 0, "episodes": [], "flagged": 0}
    if not was_judged(session.get("steps", [])) and not force:
        return {"status": "offline", "steps": len(session.get("steps", [])),
                "episodes": [], "flagged": 0,
                # Held either way, but a developer who switched judging off
                # should not be told the model is down.
                "reason": (f"no verdicts: {config.local_model} did not answer"
                           if config.judge_boundaries else
                           "no verdicts: judging is off (SKILLPP_JUDGE=0); "
                           "sessions wait until it is back on")}

    episodes = segment(session.get("steps", []), config.min_episode_steps)

    # An episode with no completion marker, in a session that did segment, is a
    # fragment with nothing to show for itself. Recording it would recreate the
    # mega-candidates segmentation exists to remove.
    # `force` is an explicit "save this" from a person, so the guards that exist
    # to suppress uninteresting captures do not apply. They protect against a
    # detector banking noise; they should not overrule someone who has read the
    # work and asked for it. Same rule as dictation.
    foldable = episodes if force else [e for e in episodes if not e.flagged]

    # Matching needs the embedding model. Checked once, before anything is
    # saved, so an outage cannot leave half a session folded and the rest held.
    # A captured session is held, the same as one with no boundary verdicts. An
    # explicit keep is a person saying "save this", so it banks unmatched.
    from .matching import model_reachable
    wants_match = config.match_candidates
    can_match = bool(foldable) and wants_match and model_reachable(config)
    if foldable and wants_match and not can_match and not force:
        return {"status": "offline", "steps": len(session.get("steps", [])),
                "episodes": [], "flagged": 0,
                "reason": f"no embeddings: {config.embed_model} did not answer"}

    # Which episodes are already in the ledger. An outage can still land between
    # the check above and the last episode, and a fold that stops there has
    # banked the episodes before it. Without this list the retry banks them
    # again, and `occurrences` counts every recognition, so one run would count
    # twice — enough to reach the threshold on its own.
    folded = session.setdefault("folded", [])
    results = []
    for index, episode in enumerate(foldable):
        if index in folded:
            continue
        try:
            result = _fold_steps(config, session, episode.steps,
                                 source=source, match=can_match)
        except LocalModelUnavailable as exc:
            if not force:
                # Held, not crashed: `handle_session_end` stamps and saves the
                # file, with `folded` and the verdicts, so a retry resumes here.
                return {"status": "offline", "steps": len(session.get("steps", [])),
                        "episodes": results, "flagged": 0,
                        "reason": (f"embeddings stopped after {len(folded)} of "
                                   f"{len(foldable)} episodes: {exc}")}
            # An explicit keep never loses work. The rest is banked unmatched,
            # as it is when the model is down before a keep starts, and
            # `skillpp merge` compares those first.
            can_match = False
            result = _fold_steps(config, session, episode.steps,
                                 source=source, match=False)
        result["ended_by"] = episode.ended_by
        results.append(result)
        folded.append(index)
        # Written out per episode, not once at the end. `folded` is what stops
        # a retry re-banking what is already banked, and until this it only
        # reached disk on the offline path — so a worker killed mid-fold lost
        # it, and the retry counted those episodes a second time. `occurrences`
        # counts every recognition, so one crash could reach the threshold on
        # its own. One small write against an embedding call is free.
        if persist:
            _save_session(config, session)

    flagged = [e for e in episodes if e.flagged]
    if not results:
        return {"status": "too-thin", "steps": 0, "episodes": [],
                "flagged": len(flagged)}

    summary = dict(results[-1])
    summary["episodes"] = results
    summary["flagged"] = len(flagged)
    return summary


def _turns(steps: list[dict], cwd: str = "") -> list[dict]:
    """The run as a conversation: each prompt, the agent's reply, what it used.

    This is what a draft is written from. Raw tool steps bury the procedure
    under tool internals and session paths; the prompts and replies carry it —
    the review checkpoints and how each check was done. The one fact only a
    tool call holds is *which skill* built the result, hence `used`.
    """
    turns: list[dict] = []
    for step in steps:
        if is_prompt(step):
            turns.append({
                "prompt": parameterize(str((step.get("input") or {}).get("text", "")), cwd),
                "reply": parameterize(str(step.get("reply", "")), cwd),
                "used": []})
            continue
        if not turns:
            continue
        tool = str(step.get("tool", ""))
        if tool == "Skill":
            used = f"skill {(step.get('input') or {}).get('skill', '')}"
        elif tool.startswith("mcp__"):
            used = f"mcp {tool}"
        else:
            continue
        if used not in turns[-1]["used"]:
            turns[-1]["used"].append(used)
    return turns


def _fold_steps(config: Config, session: dict, steps: list[dict],
                source: str = "capture", *, match: bool = True) -> dict:
    """Fold one episode's steps into a new or updated ledger entry.

    *match* False banks a new entry without comparing it to anything, marked
    `unmatched` — only for an explicit keep when no embedding model answered.
    """
    cwd = session.get("cwd") or ""
    substantive = _substantive(steps)
    if len(substantive) < 2:
        return {"status": "too-thin", "steps": len(substantive)}

    # Parameterise before matching so machine-specific paths do not make
    # otherwise-identical workflows look different.
    for step in substantive:
        payload = step.get("input") or {}
        for key, value in list(payload.items()):
            if isinstance(value, str):
                payload[key] = parameterize(value, cwd)

    ledger = Ledger(config)
    intents = _intents_for(session, steps)
    turns = _turns(steps, cwd)
    # Same procedure or not, decided by an embedding against every entry of any
    # status in this project (`matching.find_same`). Only this project's: a
    # candidate, and the skill made from it, belong to the repo the work was
    # done in, so the same steps in another repo are another candidate.
    # Raises if the model is unreachable; the caller has already checked it is
    # up, so that is a mid-fold outage.
    existing = None
    if match:
        from .matching import find_same
        here = project_of(cwd)
        pool = [e for e in ledger.all() if here in projects_of(e)]
        hit = find_same(substantive, pool, config, turns=turns)
        existing = hit[0] if hit else None

    deps_mcp = sorted({s["tool"] for s in substantive if s["tool"].startswith("mcp__")})
    deps_cli = sorted(_cli_dependencies(substantive))

    if existing:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        existing.last_seen = now
        if cwd and cwd not in existing.projects:
            existing.projects.append(cwd)
        sid = session.get("session_id", "")
        if sid and sid not in existing.sessions:
            existing.sessions.append(sid)
        # Every recognition counts, repeats inside one session included — the
        # count is how often the procedure was recognized, and `sessions` keeps
        # where. So one long sitting can reach the recurrence threshold on its
        # own. Deliberate: a procedure repeated within a session is a repeat.
        existing.occurrences += 1
        # Every recognition, not every distinct session: the count above works
        # the same way, and two sightings inside one session are two lines.
        existing.seen.append({"session": session.get("session_id", ""), "at": now})
        for intent in intents:
            if intent not in existing.intents:
                existing.intents.append(intent)
        del existing.intents[8:]
        # Keep a few variants so divergence and conditional-step detection
        # have something to compare (docs/design.md §4).
        if len(existing.variants) < 4:
            existing.variants.append(substantive)
        existing.deps_mcp = sorted(set(existing.deps_mcp) | set(deps_mcp))
        existing.deps_cli = sorted(set(existing.deps_cli) | set(deps_cli))
        ledger.save(existing)
        return {"status": "merged", "id": existing.id,
                "occurrences": existing.occurrences,
                "ready": existing.ready(config.recurrence_threshold)}

    title, title_source = _title_for(intents, substantive)
    entry = Entry(
        id=new_id(config.ledger_dir),
        title=title,
        title_source=title_source,
        status=STATUS_CANDIDATE,
        occurrences=1,
        projects=[cwd] if cwd else [],
        sessions=[session.get("session_id", "")] if session.get("session_id") else [],
        seen=[{"session": session.get("session_id", ""),
               "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}],
        intents=intents,
        steps=substantive,
        turns=turns,
        variants=[substantive],
        deps_mcp=deps_mcp,
        deps_cli=deps_cli,
        source=source,
        unmatched=not match,
    )
    if config.name_candidates:
        _name_from_model(config, entry)
    ledger.save(entry)
    if match:
        # Cache its vector now, while the model is known to be up, so the next
        # episode compares against it without embedding it again.
        from .matching import remember
        try:
            remember(entry, config)
        except LocalModelUnavailable:
            pass
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

    stated = [{"tool": "Stated", "input": {"text": s}} for s in steps]
    intents = [cleaned[: config.max_field_chars]]

    ledger = Ledger(config)
    # Only against other dictated entries: a description and a captured run are
    # different evidence, and folding one into the other would count a plan as
    # a recurrence. No model means no comparison — the description is still
    # banked, marked unmatched, because typing it was an explicit request.
    from .matching import find_same
    unmatched = False
    try:
        hit = find_same(stated,
                        [e for e in ledger.all() if e.source == "dictated"], config)
    except LocalModelUnavailable:
        hit, unmatched = None, True
    existing = hit[0] if hit else None
    if existing:
        existing.occurrences += 1
        existing.last_seen = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        ledger.save(existing)
        return {"status": "merged", "id": existing.id, "ready": True}

    entry = Entry(
        id=new_id(config.ledger_dir),
        # The whole description makes a more useful title than its first step.
        title=(title or cleaned.replace("\n", " "))[:70],
        status=STATUS_CANDIDATE,
        occurrences=1,
        source="dictated",
        intents=intents,
        steps=stated,
        unmatched=unmatched,
    )
    ledger.save(entry)
    if not unmatched:
        from .matching import remember
        try:
            remember(entry, config)
        except LocalModelUnavailable:
            pass
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


# A count is the wrong bound: an ordinary directed task can run past five
# prompts, and a count drops the last ones, which is where the closing check
# lives. Budget by characters instead, so a task keeps its shape while a
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


# A heredoc body is not shell. `python3 - <<'EOF' … EOF` carries Python whose
# `;` and `&&` mean nothing to a shell, and splitting on them produced
# requirements like `print('deps` and `frontend` — three of the junk entries on
# one real candidate came from inside a single heredoc.
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _strip_heredocs(command: str) -> str:
    """The command with every heredoc body removed.

    An unterminated heredoc takes the rest of the string with it:
    `max_field_chars` truncates a long command at 2000 characters, so the
    terminator is usually not there to match — the same reason
    `_COMMIT_HEREDOC_RE` does not look for one.
    """
    out: list[str] = []
    rest = command
    while True:
        match = _HEREDOC_RE.search(rest)
        if not match:
            out.append(rest)
            return "".join(out)
        line_end = rest.find("\n", match.end())
        if line_end == -1:
            out.append(rest[:match.start()])
            return "".join(out)
        out.append(rest[:match.start()])
        body = rest[line_end + 1:]
        closer = re.search(rf"^\s*{re.escape(match.group(2))}\s*$", body,
                           re.MULTILINE)
        if not closer:
            return "".join(out)
        rest = body[closer.end():]


def _shell_chunks(command: str) -> list[str]:
    """Split on shell operators, never inside quotes or parentheses.

    Walked character by character rather than `str.split`, because the code a
    program is handed is a quoted argument: `node -e "require('p');
    console.log('ok')"` is one command, and splitting its body on `;` asked the
    reader to install `console.log('ok')"`. Nothing here needs to know which
    flags carry code — staying inside the quotes is enough. It also stops `&&`
    inside a commit message being rewritten as a separator.
    """
    chunks: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(command):
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char == "\\" and index + 1 < len(command):
            current.append(char)
            current.append(command[index + 1])
            index += 2
            continue
        elif char in "({":
            depth += 1
            current.append(char)
        elif char in ")}":
            depth = max(0, depth - 1)
            current.append(char)
        elif depth == 0 and char in ";|&\n":
            chunks.append("".join(current))
            current = []
            # `&&` and `||` are one separator, not two empty commands.
            if index + 1 < len(command) and command[index + 1] == char:
                index += 1
        else:
            current.append(char)
        index += 1
    chunks.append("".join(current))
    return [c for c in chunks if c.strip()]


def _cli_dependencies(steps: list[dict]) -> set[str]:
    """Programs the workflow shells out to — declared deps (docs/design.md §5)."""
    from .lifecycle import COREUTILS, PROGRAM_RE, SHELL_BUILTINS
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
        for chunk in _shell_chunks(_strip_heredocs(command)):
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
            # A declared dependency is something a reader might have to
            # install. A token that is not shaped like a program name never
            # was one — `')`, `','const`, `console.log('ok')"` all reached a
            # real skill — and a builtin or a coreutil is on every machine, so
            # naming it in a list headed "if a requirement is missing, stop"
            # is noise `check_dependencies` can never act on.
            if not PROGRAM_RE.match(program):
                continue
            if program in SHELL_BUILTINS or program in COREUTILS:
                continue
            if program.isascii():
                found.add(program)
    return found


# `git commit -m "…"`, `-m '…'`, and the heredoc form an agent writing a long
# message uses. Narrow on purpose: an unrecognised form falls through to the
# prompt rather than being guessed at.
# `git -C <path> commit` and `git -c user.email=… commit` are commits: the
# subject must be found past git's global options, not only immediately after
# `git`.
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
    the opening prompt is written before and says what was wrong, or just
    "Looks good — commit". A commit subject is the name of a procedure; a
    prompt like that is not.
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


def _title_for(intents: list[str], steps: list[dict]) -> tuple[str, str]:
    """A title and where it came from, without asking a model.

    Only the commit subject is a name of the work; the rest are strings capture
    observed and has to reuse, which is what `_name_from_model` replaces when a
    local model answers. Cut the way a model's name is (`summary.clip_title`),
    so every title on the review page ends at a word.
    """
    subject = _subject_of(steps)
    if subject:
        return clip_title(subject), "commit"
    if intents:
        return clip_title(intents[0].strip().splitlines()[0]), "prompt"
    for step in steps:
        if step.get("tool") == "Bash":
            cmd = str((step.get("input") or {}).get("command", "")).strip()
            if cmd:
                return clip_title(cmd), "command"
    return "captured workflow", "command"


def _name_from_model(config: Config, entry: Entry) -> None:
    """Let the local model name the procedure, and cache its sentence.

    Runs once, when the entry is banked, because the title is what the review
    page lists *before* anything is opened — there is no lazy moment for it.
    The commit subject is left alone: it was written after the work, by the
    person doing it, and measured better than anything derived from a prompt.

    Never fatal. A fold that cannot reach the model keeps the title it derived
    and stays `prompt`/`command`, which is what `skillpp retitle` looks for.
    """
    from .summary import name_and_sentence, store_summary
    try:
        name, sentence = name_and_sentence(config, entry)
    except LocalModelUnavailable as exc:
        log_error(config, f"naming failed: {exc}")
        return
    if name and entry.title_source != "commit":
        entry.title, entry.title_source = name, "model"
    if sentence:
        store_summary(config, entry, sentence)
