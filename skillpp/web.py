"""A local page for deciding what becomes a skill.

One list: the candidates recognized often enough to be worth a decision, and
what was decided about them. Accept promotes, Decline dismisses, and an
accepted candidate gets a Draft button that has the developer's agent
write a draft. Everything else the ledger holds stays in the CLI.

Every action runs an existing command — `skillpp promote`, `skillpp dismiss`,
`skillpp draft --apply` — as a subprocess, so the page cannot drift from what
the commands do and there is no second copy of their rules to keep in step.

Binds to 127.0.0.1 with no authentication; it must never be exposed.
Dependency-free on purpose: `http.server` and one self-contained page.
"""

from __future__ import annotations

import io
import json
import math
import re
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .ledger import (STATUS_CANDIDATE, STATUS_DISMISSED, STATUS_PROMOTED,
                     Ledger)
from .lifecycle import parse_frontmatter
from .normalize import parameterize
from .summary import cached_summary, load_summaries, summaries_path
from .sanitize import scrub
from .segment import is_read_only
from .signals import DESTRUCTIVE

CLI = Path(__file__).resolve().parent.parent / "bin" / "skillpp"
PROMPTS = Path(__file__).resolve().parent / "prompts"
# A session id reaches `_find_transcript` as a glob, so it is checked before it
# gets there rather than trusted because the page sent it.
_SAFE_SESSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# `skillpp draft` gives the agent 900 seconds by default. A job still marked
# running well past that died with the server that started it.
DRAFT_STALE_SECONDS = 1200

_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Written by this page or by the agent's editor, never part of the skill.
# Bookkeeping beside a draft, never part of the skill. `agent.log` is the drafting
# agent's transcript: it names local paths and it is not a file anyone who
# installs the skill should receive.
_NOT_SKILL_FILES = ("status.json", "downloaded.json", "agent.log")

_jobs: dict[str, threading.Thread] = {}
# Which server process started a run. A run marked running by a server that is
# no longer the one serving died with it, so it is shown as failed at once
# instead of spinning until DRAFT_STALE_SECONDS.
_BOOT = f"{time.time():.6f}-{id(_jobs)}"
_jobs_lock = threading.Lock()


def _run(config: Config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), "--root", str(config.root),
                           *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def _draft_dir(config: Config, entry_id: str) -> Path:
    return config.root / "drafts" / entry_id


def _status_path(config: Config, entry_id: str) -> Path:
    return _draft_dir(config, entry_id) / "status.json"


def _write_status(config: Config, entry_id: str, **status) -> None:
    path = _status_path(config, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    tmp.replace(path)


def _read_status(config: Config, entry_id: str) -> dict:
    try:
        return json.loads(_status_path(config, entry_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _tail(text: str, lines: int = 4) -> str:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def row_state(config: Config, entry) -> dict:
    """What a row shows, read from the ledger and the draft directory only."""
    if entry.status == STATUS_DISMISSED:
        # Not "declined": that is the agent refusing to draft, shown with a retry.
        return {"state": "dismissed"}
    if entry.status != STATUS_PROMOTED:
        # Seen too few times to judge yet: listed, but it cannot be accepted or
        # declined until it recurs. The threshold exists to filter noise.
        if not entry.ready(config.recurrence_threshold):
            return {"state": "collecting"}
        return {"state": "undecided"}
    if entry.skill_path and Path(entry.skill_path).expanduser().exists():
        return {"state": "installed", "path": entry.skill_path}
    status = _read_status(config, entry.id)
    orphaned = status.get("boot") not in (None, _BOOT)
    fresh = (time.time() - status.get("started", 0) < DRAFT_STALE_SECONDS
             and not orphaned)
    drafted = sorted(p for p in _draft_dir(config, entry.id).rglob("SKILL.md")
                     if ".revisions" not in p.parts)
    if status.get("state") == "running":
        if fresh:
            return {"state": "creating"}
        return {"state": "failed", "message": "the server restarted while the draft ran"
                if orphaned else "the draft run did not finish"}
    if drafted and status.get("state") == "revising" and fresh:
        return {"state": "revising", "path": str(drafted[0])}
    if drafted:
        stale = status.get("state") == "revising"
        message = ("the server restarted while the revision ran" if stale and orphaned else
                   "the revision did not finish" if stale else
                   status.get("message", "") if status.get("state") == "revise-failed"
                   else "")
        return {"state": "drafted", "path": str(drafted[0]), "message": message}
    if status.get("state") in ("failed", "declined"):
        return {"state": status["state"], "message": status.get("message", "")}
    return {"state": "accepted"}


def days_left(config: Config, entry, now=None) -> int | None:
    """Days until `skillpp expire` would delete this candidate, or None if it
    never would. Mirrors `Ledger.expire`: only a candidate still collecting
    expires, `candidate_ttl_days` after it was last recognized — so every new
    recognition resets the clock, and one that reaches the threshold keeps."""
    from datetime import datetime, timedelta, timezone
    from .ledger import _parse_ts

    if entry.status != STATUS_CANDIDATE or entry.ready(config.recurrence_threshold):
        return None
    now = now or datetime.now(timezone.utc)
    seconds = (_parse_ts(entry.last_seen) + timedelta(days=config.candidate_ttl_days)
               - now).total_seconds()
    return max(0, math.ceil(seconds / 86400))    # part of a day left is a day left


# `AskUserQuestion` keeps nothing of its input once scrubbed, but what came back
# names each question and the answer given: `"<question>"="<answer>"`.
_ASKED = re.compile(r'"([^"]+)"="([^"]*)"')
# A connector's MCP server can be named by a bare id; its tool name still reads.
_ID_SERVER = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-")


def _squash(text) -> str:
    return " ".join(str(text or "").split())


def _asked(step: dict) -> str:
    """What a question to the developer asked and what they chose."""
    return "; ".join(f"{q} → {a}" for q, a in
                     _ASKED.findall(str(step.get("tool_returned") or "")))


def step_label(step: dict) -> str:
    """What one step did, in a few words and without a model: the agent's own
    description of a shell command where it wrote one — written at call time,
    for a reader — otherwise the tool and what it touched."""
    tool = str(step.get("tool", "?"))
    payload = step.get("input") or {}
    note = _squash(payload.get("description") or payload.get("text"))
    if note:
        return note
    if tool == "AskUserQuestion":
        return _asked(step) or "a question"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        leaf = parts[-1].replace("_", " ")
        server = parts[1] if len(parts) > 2 else ""
        return f"{server}: {leaf}" if server and not _ID_SERVER.match(server) else leaf
    target = payload.get("file_path") or payload.get("path") or payload.get("pattern")
    if target:
        return f"{tool} {Path(str(target)).name or target}"
    if tool == "Bash":
        return _squash(payload.get("command"))[:80]
    if tool == "Skill" and payload.get("skill"):
        return f"Skill {payload['skill']}"
    return tool


def step_outline(steps: list[dict], limit: int = 30) -> list[str]:
    """One line per step, in order — what the page shows when the steps cannot
    be placed under the requests they served (`step_groups`). Consecutive
    repeats collapse to one line."""
    lines: list[str] = []
    for step in steps:
        note = step_label(step)
        if step.get("tool") == "AskUserQuestion":
            note = f"Asked you: {note}"
        if not lines or lines[-1] != note:
            lines.append(note)
    return lines[:limit] + ([f"… {len(lines) - limit} more"] if len(lines) > limit else [])


def _placement(entry) -> list[int] | None:
    """Which request each step served, as an index into `entry.turns` — or
    None when that cannot be told for certain.

    Every step records `serves`, the number of prompts its session had seen
    when it ran. A turn records no number of its own, and an episode seldom
    starts at its session's first prompt, so the two are lined up through the
    agent's words: what it said while serving a request is part of that
    request's reply. Every anchor must agree, or nothing is placed: steps
    shifted under the wrong requests would read worse than the flat list.
    """
    turns, steps = entry.turns or [], entry.steps or []
    if not turns or not steps:
        return None
    if len(turns) == 1:
        return [0] * len(steps)
    try:
        serves = [int(step["serves"]) for step in steps]
    except (KeyError, TypeError, ValueError):
        return None
    if serves != sorted(serves):
        return None
    cwd = (entry.projects or [None])[0]
    replies = [_squash(turn.get("reply")) for turn in turns]
    offsets = set()
    for step, number in zip(steps, serves):
        for said in (step.get("assistant_note"), step.get("closing_note")):
            # Parameterised as the reply was, so a path in it still matches.
            anchor = _squash(parameterize(str(said or ""), cwd))[:60]
            if len(anchor) < 12:
                continue
            hits = [i for i, reply in enumerate(replies) if anchor in reply]
            if len(hits) == 1:
                offsets.add(hits[0] - number)
    if len(offsets) != 1:
        return None
    offset = offsets.pop()
    placed = [number + offset for number in serves]
    return placed if placed[0] >= 0 and placed[-1] < len(turns) else None


def _step_item(step: dict) -> dict:
    tool = str(step.get("tool", ""))
    payload = step.get("input") or {}
    if tool == "AskUserQuestion":
        kind = "ask"
    elif is_read_only(step):
        kind = "look"                   # a lookup that failed only looked
    else:
        kind = "fail" if step.get("failed") else "do"
    command = str(payload.get("command", "")) if tool == "Bash" else ""
    target = payload.get("file_path") if tool in ("Write", "Edit", "NotebookEdit") else ""
    return {"kind": kind, "text": step_label(step),
            # The same test that lists a run's destructive commands for the draft.
            "warn": bool(command and DESTRUCTIVE.search(command)),
            "verb": tool if target else "", "file": Path(str(target)).name if target else ""}


def _fold(items: list[dict]) -> list[dict]:
    """Lookups made back to back become one line, and so does one change made
    again and again; every other change keeps a line of its own."""
    lines: list[dict] = []
    for item in items:
        last = lines[-1] if lines else None
        if last and item["kind"] == last["kind"] == "look":
            names = last["names"]
            if names[-1][0] == item["text"]:
                names[-1][1] += 1
            else:
                names.append([item["text"], 1])
            last["steps"] += 1
            continue
        if (last and item["kind"] == last["kind"] and item["warn"] == last["warn"]
                and len(last["names"]) == 1 and last["names"][0][0] == item["text"]):
            last["names"][0][1] += 1
            last["steps"] += 1
            continue
        lines.append({**item, "steps": 1, "names": [[item["text"], 1]]})
    for line in lines:
        line["text"] = " · ".join(text if n == 1 else f"{text} ×{n}"
                                  for text, n in line.pop("names"))
    return lines


def _digest(lines: list[dict]) -> tuple[list[dict], int, int]:
    """The one line a closed request shows: what changed, in order — file
    changes with one verb counted together, so four writes read "Write 4
    files" — and the lookups and failures only as numbers."""
    parts: list[dict] = []
    run: list[dict] = []
    looks = failed = 0

    def flush() -> None:
        if not run:
            return
        files = list(dict.fromkeys(line["file"] for line in run))
        count = sum(line["steps"] for line in run)
        text = (f"{run[0]['verb']} {files[0]}" + (f" ×{count}" if count > 1 else "")
                if len(files) == 1 else f"{run[0]['verb']} {len(files)} files")
        parts.append({"text": text, "warn": False})
        run.clear()

    for line in lines:
        if line["kind"] == "look":
            looks += line["steps"]
        elif line["kind"] == "fail":
            failed += line["steps"]
        elif line["kind"] == "do" and line["verb"]:
            if run and run[0]["verb"] != line["verb"]:
                flush()
            run.append(line)
        else:
            flush()
            parts.append({"text": "asked you" if line["kind"] == "ask" else line["text"],
                          "warn": line["warn"]})
    flush()
    return parts, looks, failed


_REQUEST_CHARS = 1200


def step_groups(entry) -> list[dict] | None:
    """The steps under the request each one served, the developer's own words
    as the headings — or None, and the page shows `step_outline` instead.

    Per request: `lines` in order, for when it is opened, and a `digest` of
    what changed, for the one line shown while it is closed. Most of a run is
    looking around; that is what folding hides, not what it drops.
    """
    placed = _placement(entry)
    if placed is None:
        return None
    served: list[list[dict]] = [[] for _ in entry.turns]
    for step, index in zip(entry.steps, placed):
        served[index].append(_step_item(step))
    groups = []
    for turn, items in zip(entry.turns, served):
        request = str(turn.get("prompt") or "").strip()
        if len(request) > _REQUEST_CHARS:
            request = request[:_REQUEST_CHARS].rstrip() + "…"
        lines = _fold(items)
        digest, looks, failed = _digest(lines)
        for line in lines:
            del line["verb"], line["file"]
        groups.append({"request": request, "tools": len(items), "lines": lines,
                       "digest": digest, "looks": looks, "failed": failed})
    return groups


# The cache and the model call live in `skillpp.summary`, because capture asks
# the same question when it banks a candidate (`capture._name_from_model`) and
# stores the sentence there, so an opened row usually needs no model at all.
_summaries_path = summaries_path
_cached_summary = cached_summary


def summarise(config: Config, entry_id: str) -> dict:
    """One sentence from the local model saying what a candidate's work did.

    Normally already cached by the fold that banked the entry. Asked here only
    when it is not — entries banked while the model was down, and entries from
    before capture named them — then cached per entry and keyed on step count,
    so the page never waits on a model twice and a candidate that grows is
    described again.
    """
    from .local import LocalModelUnavailable
    from .summary import name_and_sentence, store_summary

    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    cached = _cached_summary(load_summaries(config), entry)
    if cached:
        return {"ok": True, "summary": cached}

    try:
        _, text = name_and_sentence(config, entry)
    except LocalModelUnavailable as exc:
        return {"ok": False, "error": f"no local model: {exc}"}
    if not text:
        return {"ok": False, "error": "the model returned nothing"}

    store_summary(config, entry, text)
    return {"ok": True, "summary": text}


def collect_state(config: Config) -> dict:
    """The rows the page lists. Reads files only: no model, no agent.

    Every candidate, promoted and dismissed entry, most-seen first. `ready`
    splits the page: recognized often enough to decide on, or still collecting.
    """
    threshold = config.recurrence_threshold
    summaries = load_summaries(config)
    rows = []
    for entry in Ledger(config).all():
        if entry.status not in (STATUS_CANDIDATE, STATUS_PROMOTED, STATUS_DISMISSED):
            continue
        rows.append({"id": entry.id, "title": entry.title,
                     "occurrences": entry.occurrences,
                     "ready": entry.occurrences >= threshold or entry.ready(threshold),
                     "days_left": days_left(config, entry),
                     **row_state(config, entry),
                     "seen": seen_runs(entry),
                     "outline": step_outline(entry.steps),
                     "groups": step_groups(entry),
                     "summary": _cached_summary(summaries, entry)})
    # Expired last of all. Otherwise most-recognized first; at the same count,
    # the one closest to expiring first, then rows that never expire.
    rows.sort(key=lambda r: (r["days_left"] == 0,
                             -r["occurrences"],
                             r["days_left"] is None,
                             r["days_left"] if r["days_left"] is not None else 0,
                             (r["title"] or "").lower()))
    return {"threshold": threshold, "ttl": config.candidate_ttl_days,
            "rows": rows, "drafts": list_drafts(config)}


def seen_runs(entry) -> list[dict]:
    """Where this candidate was recognized, newest recognition last.

    `entry.seen` is the record; entries banked before it existed fall back to
    the session ids alone, with no time rather than a guessed one — `created`
    and `last_seen` only bound the range, and printing either against every run
    would be inventing provenance.
    """
    if entry.seen:
        return [{"session": s.get("session", ""), "at": s.get("at", "")}
                for s in entry.seen if s.get("session")]
    return [{"session": sid, "at": ""} for sid in entry.sessions if sid]


# A transcript is 2.9MB at the median and 25MB at the worst, nearly all of it
# tool payloads. What a person wants when they ask where a pattern came from is
# the conversation, so the page renders that and never serves the file.
_TRANSCRIPT_TURNS = 60
_TRANSCRIPT_PROMPT = 2000
_TRANSCRIPT_REPLY = 4000


def _by_prefix(session_id: str) -> str | None:
    """A transcript whose name *starts* with this id, when exactly one does.

    Entries banked from the live-session fixtures carry the short tag the
    fixture is filed under (`f32d548f`) rather than the full uuid, and the
    fixtures are real sessions whose transcripts are still on disk. Accepted
    only when the prefix picks out a single file: a match that is ambiguous is
    not provenance.
    """
    hits = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}*.jsonl"))
    return str(hits[0]) if len(hits) == 1 else None


def transcript(config: Config, session_id: str) -> dict:
    """The conversation of one recorded session, for the "Seen in" list.

    Read straight from Claude Code's own transcript rather than from the entry,
    because a candidate keeps the turns of the run that created it and nothing
    of the runs that merged into it — which are exactly the ones a person opens
    this to see.
    """
    from .capture import _find_transcript, _transcript_turns

    if not _SAFE_SESSION.match(session_id or ""):
        return {"ok": False, "error": "not a session id"}
    path = _find_transcript(session_id) or _by_prefix(session_id)
    if not path:
        return {"ok": False, "error": "no transcript on disk for this session"}
    turns = _transcript_turns(path)
    if not turns:
        return {"ok": False, "error": "the transcript holds no conversation"}
    # Scrubbed on the way out: `sanitize.scrub` runs over captured steps, and
    # this text has never been through it.
    out = [{"prompt": scrub(prompt)[:_TRANSCRIPT_PROMPT],
            "reply": scrub("\n\n".join(reply))[:_TRANSCRIPT_REPLY]}
           for prompt, reply in turns[:_TRANSCRIPT_TURNS]]
    return {"ok": True, "turns": out, "total": len(turns)}


def _skill_name(entry, skill_md: Path) -> str:
    """The folder name the skill unpacks to: its frontmatter name, if safe."""
    name = str(parse_frontmatter(skill_md.read_text(encoding="utf-8")).get("name") or "")
    return name if _SAFE_NAME.match(name) else entry.id


def _draft_files(config: Config, entry_id: str) -> list[Path]:
    root = _draft_dir(config, entry_id)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.name not in _NOT_SKILL_FILES
                  and not p.name.endswith(".tmp")
                  # `.revisions/` holds the versions before each revision.
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


# `## Known gaps` was the scaffold's name for the same section and nothing read
# it, so a draft carrying four unanswered questions rendered no answer fields
# and downloaded freely. One name now — the old one stays matchable for drafts
# written before the rename.
_QUESTIONS_HEADING = re.compile(r"^##\s+(?:Open questions|Known gaps)\s*$",
                                re.IGNORECASE)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def split_open_questions(text: str) -> tuple[list[str], str]:
    """The draft's `## Open questions` and the SKILL.md without that section.

    The drafting agent cannot ask, so it writes what it could not tell from the
    runs there (`skillpp/commands/skillpp-draft.md`, *Ask through open questions*). Those are gaps in the
    skill, not part of it: the page shows them as answer fields, hides the
    section from the rendered draft, and refuses the download while any remain.
    The section ends at the next heading or horizontal rule.
    """
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _QUESTIONS_HEADING.match(line)), None)
    if start is None:
        return [], text
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^(#{1,6}\s|-{3,}\s*$|\*{3,}\s*$|_{3,}\s*$)", lines[i]):
            end = i
            break
    questions: list[str] = []
    for line in lines[start + 1:end]:
        item = _ITEM.match(line)
        if item:
            questions.append(item.group(1).strip())
        elif line.strip() and questions:
            questions[-1] += " " + line.strip()
    remaining = "\n".join(lines[:start] + lines[end:])
    return [q for q in questions if q], remaining


def _skill_digest(skill_md: Path) -> str:
    import hashlib
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()


def record_download(config: Config, entry_id: str) -> None:
    """Remember that this version of the draft was downloaded.

    Kept as a digest of SKILL.md, so a draft revised after its download counts
    as not downloaded again: the copy someone has is no longer this one.
    """
    from datetime import datetime, timezone
    entry = Ledger(config).get(entry_id)
    if not entry:
        return
    skill_md = Path(row_state(config, entry)["path"])
    path = _draft_dir(config, entry.id) / "downloaded.json"
    path.write_text(json.dumps({
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sha256": _skill_digest(skill_md)}), encoding="utf-8")


def _downloaded_at(config: Config, entry_id: str, skill_md: Path) -> str:
    try:
        record = json.loads((_draft_dir(config, entry_id) / "downloaded.json")
                            .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return record.get("at", "") if record.get("sha256") == _skill_digest(skill_md) else ""


def list_drafts(config: Config) -> list[dict]:
    """Every finished draft, with the SKILL.md text to review."""
    from datetime import datetime, timezone
    drafts = []
    for entry in Ledger(config).all():
        state = row_state(config, entry)
        if state["state"] not in ("drafted", "revising"):
            continue
        skill_md = Path(state["path"])
        text = skill_md.read_text(encoding="utf-8")
        front = parse_frontmatter(text)
        questions, shown = split_open_questions(text)
        drafts.append({
            "id": entry.id, "title": entry.title,
            "name": _skill_name(entry, skill_md),
            "description": str(front.get("description") or ""),
            "body": shown,
            "questions": questions,
            "files": [str(p.relative_to(_draft_dir(config, entry.id)))
                      for p in _draft_files(config, entry.id)],
            "revising": state["state"] == "revising",
            "message": state.get("message", ""),
            "downloaded_at": _downloaded_at(config, entry.id, skill_md),
            # When SKILL.md was last written, by the draft or a revision. Drafts
            # are listed newest first: the one just asked for is the one looked
            # for, and a name alone did not tell drafts apart.
            "drafted_at": datetime.fromtimestamp(
                skill_md.stat().st_mtime, timezone.utc).isoformat(),
        })
    drafts.sort(key=lambda d: d["drafted_at"], reverse=True)
    return drafts


def draft_zip(config: Config, entry_id: str) -> tuple[str, bytes] | None:
    """A draft as `<skill-name>.zip`, holding `<skill-name>/SKILL.md` and any
    files beside it, so it unpacks straight into a skills directory.

    Only a ledger entry with a finished draft is served; the id is looked up,
    never joined into a path from the request.
    """
    entry = Ledger(config).get(entry_id)
    if not entry or row_state(config, entry)["state"] != "drafted":
        return None
    skill_md = Path(row_state(config, entry)["path"])
    if split_open_questions(skill_md.read_text(encoding="utf-8"))[0]:
        return None                  # open questions first; see split_open_questions
    root = _draft_dir(config, entry.id)
    name = _skill_name(entry, skill_md)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in _draft_files(config, entry.id):
            archive.write(path, f"{name}/{path.relative_to(root)}")
    return f"{name}.zip", buffer.getvalue()


def _decide(config: Config, entry_id: str, command: str) -> dict:
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    if row_state(config, entry)["state"] != "undecided":
        return {"ok": False, "error": f"{entry.id} is already {entry.status}"}
    proc = _run(config, command, entry.id)
    if proc.returncode != 0:
        return {"ok": False, "error": _tail(proc.stderr or proc.stdout)}
    return {"ok": True}


def accept(config: Config, entry_id: str) -> dict:
    """Promote: `skillpp promote <id>`, with no skill file yet."""
    return _decide(config, entry_id, "promote")


def decline(config: Config, entry_id: str) -> dict:
    """Dismiss: `skillpp dismiss <id>`."""
    return _decide(config, entry_id, "dismiss")


def reinstate(config: Config, entry_id: str) -> dict:
    """Reinstate a declined candidate: `skillpp reopen <id>`."""
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    if entry.status != STATUS_DISMISSED:
        return {"ok": False, "error": f"{entry.id} is not declined"}
    proc = _run(config, "reopen", entry.id)
    if proc.returncode != 0:
        return {"ok": False, "error": _tail(proc.stderr or proc.stdout)}
    return {"ok": True}


def _draft_job(config: Config, entry_id: str, note: str = "") -> None:
    try:
        # `--note=` rather than `--note <text>`: argparse takes a note such as
        # `--dry-run` for an option and refuses the whole run.
        proc = _run(config, "draft", entry_id, "--apply",
                    *([f"--note={note}"] if note else []))
        said = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            _write_status(config, entry_id, state="failed", message=_tail(said))
        elif not sorted(_draft_dir(config, entry_id).rglob("SKILL.md")):
            # `skillpp draft` exits 0 without a file only for a stated decline.
            _write_status(config, entry_id, state="declined", message=_tail(said, 1))
        else:
            _write_status(config, entry_id, state="ready")
    except Exception as exc:  # noqa: BLE001 - the thread must record, not raise
        _write_status(config, entry_id, state="failed",
                      message=f"{type(exc).__name__}: {exc}")
    finally:
        with _jobs_lock:
            _jobs.pop(entry_id, None)


MAX_NOTE = 2000


def create_skill(config: Config, entry_id: str, note: str = "") -> dict:
    """Draft: `skillpp draft <id> --apply`, in the background.

    Only for an accepted candidate, and one run at a time per candidate. The
    draft lands in `<root>/drafts/<id>/` and is never installed from here. A
    note, when given, reaches the agent as `--note`: what the developer wants
    it to look out for. Without one the draft is written from the run alone.
    """
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        return {"ok": False, "error": f"keep the note under {MAX_NOTE} characters"}
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    with _jobs_lock:
        state = row_state(config, entry)["state"]
        if entry.id in _jobs or state == "creating":
            return {"ok": False, "error": "already running"}
        if state not in ("accepted", "failed", "declined"):
            return {"ok": False,
                    "error": f"cannot create a skill for a row that is {state}"}
        _write_status(config, entry.id, state="running", started=time.time(), boot=_BOOT)
        job = threading.Thread(target=_draft_job, args=(config, entry.id, note),
                               daemon=True)
        _jobs[entry.id] = job
    job.start()
    return {"ok": True}


MAX_INSTRUCTION = 2000


def _revise_job(config: Config, entry_id: str, instruction: str) -> None:
    try:
        proc = _run(config, "revise", entry_id, "--instruction", instruction, "--apply")
        if proc.returncode != 0:
            _write_status(config, entry_id, state="revise-failed",
                          message=_tail((proc.stderr or "") or (proc.stdout or ""), 2))
        else:
            _write_status(config, entry_id, state="ready")
    except Exception as exc:  # noqa: BLE001 - the thread must record, not raise
        _write_status(config, entry_id, state="revise-failed",
                      message=f"{type(exc).__name__}: {exc}")
    finally:
        with _jobs_lock:
            _jobs.pop(entry_id, None)


def revise(config: Config, entry_id: str, instruction: str,
           limit: int = MAX_INSTRUCTION) -> dict:
    """Revise: `skillpp revise <id> --instruction … --apply`, in the background."""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "say what to change"}
    if len(instruction) > limit:
        return {"ok": False, "error": f"keep it under {limit} characters"}
    entry = Ledger(config).get(entry_id)
    if not entry:
        return {"ok": False, "error": "no such entry"}
    with _jobs_lock:
        state = row_state(config, entry)["state"]
        if entry.id in _jobs or state == "revising":
            return {"ok": False, "error": "already running"}
        if state != "drafted":
            return {"ok": False, "error": f"no draft to revise ({state})"}
        _write_status(config, entry.id, state="revising", started=time.time(), boot=_BOOT)
        job = threading.Thread(target=_revise_job, args=(config, entry.id, instruction),
                               daemon=True)
        _jobs[entry.id] = job
    job.start()
    return {"ok": True}


MAX_ANSWERS = 6000


def answer_questions(config: Config, entry_id: str, answers: list) -> dict:
    """Send answers to a draft's open questions: one `skillpp revise` run that
    folds each answer into the skill and removes the questions it answers."""
    pairs = [(str(a.get("question", "")).strip(), str(a.get("answer", "")).strip())
             for a in answers if isinstance(a, dict)]
    pairs = [(q, a) for q, a in pairs if q and a]
    if not pairs:
        return {"ok": False, "error": "answer at least one question"}
    text = "\n\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)
    if len(text) > MAX_ANSWERS:
        return {"ok": False, "error": f"keep the answers under {MAX_ANSWERS} characters"}
    instruction = (
        "The developer answered open questions from the draft. For each answer, "
        "change the skill where it applies so it no longer needs the question, then "
        "remove that question from the `## Open questions` section. Remove the "
        "section when it is empty. Leave unanswered questions as they are.\n\n" + text)
    return revise(config, entry_id, instruction, limit=MAX_ANSWERS + 1000)


def make_handler(config: Config):
    actions = {
        "/api/accept": lambda p: accept(config, str(p.get("id", ""))),
        "/api/summary": lambda p: summarise(config, str(p.get("id", ""))),
        "/api/transcript": lambda p: transcript(config, str(p.get("session", ""))),
        "/api/decline": lambda p: decline(config, str(p.get("id", ""))),
        "/api/reinstate": lambda p: reinstate(config, str(p.get("id", ""))),
        "/api/create": lambda p: create_skill(config, str(p.get("id", "")),
                                              str(p.get("note") or "")),
        "/api/revise": lambda p: revise(config, str(p.get("id", "")),
                                        str(p.get("instruction", ""))),
        "/api/answer": lambda p: answer_questions(config, str(p.get("id", "")),
                                                  p.get("answers") or []),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass                                    # quiet: this is a local page

        def _send(self, code, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _refused(self, post: bool = False) -> bool:
            """Turn away a request that another web page made, and say so.

            Loopback keeps other machines out, not other pages in the same
            browser. Any site the developer has open could POST here, and a
            POST starts `claude -p` with Write and Edit — so:

            - `Host` must name this server. A page on a domain that resolves
              to 127.0.0.1 (DNS rebinding) is same-origin as far as the
              browser can tell, and only its `Host` gives it away.
            - A POST must be JSON. Browsers send `text/plain` and form bodies
              cross-site without asking; a JSON body makes them ask first,
              and nothing here answers that preflight.
            - A POST's `Origin`, when the browser sends one, must be this page.

            Tools that send neither header, like `curl` or the tests, pass.
            """
            port = self.server.server_address[1]
            ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host")
            if host is not None and host not in ours:
                self._send(403, json.dumps({"error": "unknown host"}))
                return True
            if not post:
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in ours}:
                self._send(403, json.dumps({"error": "cross-origin request"}))
                return True
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._send(415, json.dumps({"error": "send application/json"}))
                return True
            return False

        def do_GET(self):
            if self._refused():
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._send(200, json.dumps(collect_state(config)))
            if url.path == "/api/draft.zip":
                entry_id = (parse_qs(url.query).get("id") or [""])[0]
                found = draft_zip(config, entry_id)
                if not found:
                    waiting = any(d["id"] == entry_id and d["questions"]
                                  for d in list_drafts(config))
                    if waiting:
                        return self._send(409, json.dumps(
                            {"error": "answer the open questions first"}))
                    return self._send(404, json.dumps({"error": "no such draft"}))
                filename, data = found
                record_download(config, entry_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self._refused(post=True):
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"error": "bad json"}))
            action = actions.get(self.path)
            if not action:
                return self._send(404, json.dumps({"error": "not found"}))
            self._send(200, json.dumps(action(payload)))

    return Handler


def serve(config: Config, skills_dir: Path | None = None, port: int = 8765,
          open_browser: bool = True) -> ThreadingHTTPServer:
    """Serve on loopback only. Never bind anywhere else: there is no auth."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(config))
    if open_browser:
        threading.Thread(target=webbrowser.open, daemon=True,
                         args=[f"http://127.0.0.1:{httpd.server_port}/"]).start()
    return httpd


# Raw: the page script holds regular expressions, and their backslashes must
# reach the browser unchanged.
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>skillpp</title>
<style>
 :root{--bg:#0c0d10;--panel:#12141a;--surface:#171922;--line:#262935;
   --fg:#eceef2;--dim:#9da3b4;--muted:#63697a;
   --ok:#34d399;--okbg:rgba(16,185,129,.12);--okline:rgba(16,185,129,.4);
   --no:#fb7185;--nobg:rgba(244,63,94,.12);--noline:rgba(244,63,94,.4);
   --go:#38bdf8;--gobg:rgba(56,189,248,.12);--goline:rgba(56,189,248,.4);
   --sans:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;
   --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 var(--sans);
   -webkit-font-smoothing:antialiased}
 header{display:flex;justify-content:space-between;align-items:center;gap:12px;
   padding:14px 24px;border-bottom:1px solid var(--line);background:var(--panel);
   font-size:12px;color:var(--dim)}
 header b{font:600 14px var(--mono);color:var(--fg)}
 main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
 .row{display:flex;align-items:center;gap:16px;padding:14px 16px;margin-bottom:8px;
   background:var(--panel);border:1px solid var(--line);border-radius:8px}
 .title{flex:1;min-width:0;font:600 13.5px var(--mono);white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 .seen{font:12px var(--mono);color:var(--muted);white-space:nowrap}
 .acts{display:flex;align-items:center;gap:8px;flex-shrink:0;min-height:30px}
 button{font:500 12px var(--mono);padding:6px 12px;border-radius:5px;cursor:pointer;
   background:var(--surface);color:var(--dim);border:1px solid var(--line)}
 button:disabled{opacity:.5;cursor:default}
 button.accept:hover{color:var(--ok);border-color:var(--okline);background:var(--okbg)}
 button.decline:hover{color:var(--no);border-color:var(--noline);background:var(--nobg)}
 button.reinstate:hover{color:var(--fg);border-color:var(--dim)}
 button.create{color:#0c0d10;border-color:var(--go);background:var(--go);font-weight:600}
 button.create:hover{filter:brightness(1.1)}
 .state{font:12px var(--mono);color:var(--dim);white-space:nowrap}
 .state.ok{color:var(--ok)} .state.no{color:var(--no)}
 .msg{font:12px var(--mono);color:var(--no);max-width:260px;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
 .spin{display:inline-block;width:10px;height:10px;margin-right:6px;border-radius:50%;
   border:2px solid var(--line);border-top-color:var(--go);animation:s 1s linear infinite;
   vertical-align:-1px}
 @keyframes s{to{transform:rotate(360deg)}}
 .empty{color:var(--muted);padding:32px 0;text-align:center}
 .cand{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
 .cand.ready{border-left:3px solid #fbbf24;background:linear-gradient(90deg,rgba(251,191,36,.07),var(--panel) 40%)}
 .cand.accepted{border-left:3px solid var(--go);background:linear-gradient(90deg,rgba(56,189,248,.07),var(--panel) 40%)}
 .new{font:600 10.5px var(--mono);color:var(--go);white-space:nowrap}
 .new::before{content:"\25CF";margin-right:4px}
 nav .new{margin-left:6px}
 .draft.fresh{border-color:var(--goline)}
 .badge{font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;
   border-radius:4px;white-space:nowrap;border:1px solid}
 .badge.ready{color:#fbbf24;border-color:rgba(251,191,36,.4);background:rgba(251,191,36,.1)}
 .badge.accepted{color:var(--go);border-color:var(--goline);background:var(--gobg)}
 .badge.drafted{color:var(--go);border-color:var(--goline);background:var(--gobg)}
 .cand.drafted{border-left:3px solid var(--go);background:linear-gradient(90deg,rgba(56,189,248,.07),var(--panel) 40%)}
 .draft.review{border-left:3px solid var(--go);background:linear-gradient(90deg,rgba(56,189,248,.07),var(--panel) 40%)}
 .draft.downloaded{border-left:3px solid var(--ok);background:linear-gradient(90deg,rgba(16,185,129,.07),var(--panel) 40%)}
 .badge.review{color:var(--go);border-color:var(--goline);background:var(--gobg)}
 .badge.downloaded{color:var(--ok);border-color:var(--okline);background:var(--okbg)}
 .badge.declined{color:var(--no);border-color:var(--noline);background:var(--nobg)}
 .clock{font:12px var(--mono);color:var(--muted);white-space:nowrap}
 .clock:empty{display:none}
 .collecting .clock{display:inline-block;width:110px;text-align:right;flex-shrink:0}
 .collecting .row .clock:empty{display:none}    /* no clock: buttons sit by the count */
 .thead{display:flex;align-items:center;gap:16px;padding:0 17px 6px 17px;
   font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
 .thead .title{font:inherit;color:inherit}
 .thead .count{font:inherit;min-width:34px;text-align:right;flex-shrink:0}
 .thead .acts{min-height:0}
 .count.reached{color:var(--ok)}
 .clock.soon{color:#fbbf24}
 .clock.gone{color:var(--no)}
 .count{font:600 13px var(--mono);min-width:34px;text-align:right;flex-shrink:0;order:99}
 .section{display:flex;align-items:baseline;gap:10px;margin:22px 0 10px}
 .section:first-child{margin-top:0}
 .section h2{margin:0;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--fg)}
 .section span{font-size:12px;color:var(--muted)}
 .cand>.row{margin:0;border:0;background:none;cursor:pointer}
 .cand.open .chev{transform:rotate(90deg)}
 .cand .body{display:none;border-top:1px solid var(--line);padding:12px 16px 14px 44px}
 .cand.open .body{display:block}
 .cand .body h3{margin:0 0 6px;font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.05em;
   color:var(--muted)}
 .cand .body h3 + .sum{margin-bottom:14px}
 .sum{margin:0 0 10px;font-size:13.5px;color:var(--fg)}
 .sum.pending{color:var(--muted);font-style:italic}
 .outline{margin:0 0 10px;padding-left:20px;font-size:13px;color:#cbd2e1}
 .outline li{margin:2px 0}
 .cand .body h3 .meta{font:400 11px var(--mono);text-transform:none;letter-spacing:0;margin-left:6px}
 .reqs{list-style:none;margin:0 0 14px;padding:0}
 .req{border-top:1px solid var(--line)}
 .req:first-child{border-top:0}
 .req>button{display:flex;gap:10px;align-items:flex-start;width:100%;text-align:left;background:none;
   border:0;border-radius:0;padding:7px 0;color:inherit;font:inherit;cursor:pointer}
 .req>button:hover .p{color:#fff}
 .req .n{flex-shrink:0;width:20px;height:20px;margin-top:1px;border-radius:50%;border:1px solid var(--line);
   font:11px/18px var(--mono);text-align:center;color:var(--muted)}
 .req.open .n{border-color:var(--dim);color:var(--fg)}
 .req .what{min-width:0;flex:1}
 .req .p{display:block;font:13px/1.5 var(--sans);color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .req.open .p{white-space:pre-wrap;overflow-wrap:anywhere}
 .req.quiet .p{color:var(--muted)}
 .req .dg{display:block;font:12px/1.5 var(--sans);color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .req.open:not(.quiet) .dg{display:none}
 .req .chg{color:#cbd2e1} .req .lk{color:var(--muted)} .req .bad{color:var(--no)} .req .warn{color:#fbbf24}
 .req .lines{margin:0 0 10px 30px;padding:2px 0 2px 12px;border-left:2px solid var(--line)}
 .req .ln{display:grid;grid-template-columns:70px minmax(0,1fr);gap:8px;font:12.5px/1.55 var(--sans);color:#cbd2e1}
 .req .ln .k{font:11px/1.95 var(--mono);color:var(--muted)}
 .req .ln.look .t{color:var(--muted)} .req .ln.ask .t{color:var(--go)}
 .req .ln.fail .t{color:var(--no)} .req .ln.warn .t{color:#fbbf24}
 .runs{list-style:none;margin:0;padding:0;font-size:13px}
 .runs li{margin:2px 0}
 .run{background:none;border:0;padding:0;font:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
   color:var(--accent);cursor:pointer;text-decoration:underline dotted}
 .run:hover{text-decoration:underline}
 .when{color:var(--muted);margin-left:10px}
 .convo{margin:6px 0 12px;padding:8px 12px;border-left:2px solid var(--line);max-height:340px;overflow:auto}
 .convo .turn{margin:0 0 10px}
 .convo .who{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
 .convo p{margin:2px 0;font-size:13px;white-space:pre-wrap;color:#cbd2e1}
 .convo .more{color:var(--muted);font-style:italic}
 nav{display:flex;gap:4px}
 nav button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;
   padding:4px 10px;color:var(--dim)}
 nav button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--fg)}
 a.state{text-decoration:none;cursor:pointer}
 .draft{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
 .draft .row{margin:0;border:0;background:none;cursor:pointer}
 .chev{color:var(--muted);font:12px var(--mono);width:12px;transition:transform .15s}
 .draft.open .chev{transform:rotate(90deg)}
 .draft .desc{padding:0 16px 12px 44px;color:var(--dim);font-size:12.5px;margin:0}
 .draft .ago{color:var(--muted);font-size:12px;white-space:nowrap}
 .draft .body{display:none;border-top:1px solid var(--line);padding:14px 16px}
 .draft.open .body{display:block}
 .md{color:#cbd2e1;font-size:13.5px;line-height:1.6;max-width:760px}
 .md h1{font-size:18px;margin:18px 0 8px;color:var(--fg)}
 .md h2{font-size:15px;margin:20px 0 6px;color:var(--fg)}
 .md h3,.md h4,.md h5,.md h6{font-size:13.5px;margin:16px 0 4px;color:var(--fg)}
 .md p{margin:6px 0} .md li>p{margin:2px 0}
 .md ul,.md ol{margin:6px 0;padding-left:22px} .md li{margin:3px 0}
 .md code{font:12px var(--mono);background:var(--surface);border:1px solid var(--line);
   border-radius:4px;padding:1px 5px}
 .md pre{margin:8px 0;padding:12px;background:var(--bg);border:1px solid var(--line);
   border-radius:6px;overflow:auto}
 .md pre code{background:none;border:0;padding:0;white-space:pre;font-size:12px;line-height:1.55}
 .md blockquote{margin:8px 0;padding-left:12px;border-left:2px solid var(--line);color:var(--dim)}
 .md hr{border:0;border-top:1px solid var(--line);margin:16px 0}
 .md .link{text-decoration:underline dotted;color:var(--fg)}
 .md table.fm{border-collapse:collapse;margin:0 0 12px;font:11.5px var(--mono);width:100%}
 .md .fm th{text-align:left;color:var(--muted);font-weight:500;padding:3px 12px 3px 0;
   white-space:nowrap;vertical-align:top;width:1%}
 .md .fm td{color:var(--dim);padding:3px 0;word-break:break-word}
 .files{font:11.5px var(--mono);color:var(--muted);margin:0 0 10px}
 .revise{margin:16px 0 0;padding-top:12px;border-top:1px solid var(--line);text-align:right}
 .revise .bar{justify-content:flex-end}
 .revise .err{text-align:right}
 .revise textarea{text-align:left}
 .revise textarea{width:100%;min-height:72px;resize:vertical;font:13px/1.5 var(--sans);
   color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;
   padding:10px;margin:0 0 8px}
 .revise .bar{display:flex;gap:8px;align-items:center}
 .revise .err{font:12px var(--mono);color:var(--no);margin:0 0 8px}
 .questions{margin:0 0 16px;padding:12px 14px;border:1px solid rgba(251,191,36,.35);
   background:rgba(251,191,36,.06);border-radius:8px}
 .questions h4{margin:0 0 4px;font:600 12px var(--mono);color:#fbbf24;text-transform:uppercase;
   letter-spacing:.04em}
 .questions .hint{margin:0 0 10px;font-size:12px;color:var(--dim)}
 .questions label{display:block;margin:10px 0 4px;font-size:13px;color:var(--fg)}
 .questions code{font:12px var(--mono);background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
 .questions textarea{width:100%;min-height:52px;resize:vertical;font:13px/1.5 var(--sans);
   color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px}
 .cand .note{border-top:1px solid var(--line);padding:12px 16px 14px 44px}
 .note label{display:block;margin:0 0 2px;font-size:13px;color:var(--fg)}
 .note .hint{margin:0 0 8px;font-size:12px;color:var(--dim)}
 .note textarea{width:100%;min-height:72px;resize:vertical;font:13px/1.5 var(--sans);
   color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;
   padding:10px;margin:0 0 8px}
 .note .bar{display:flex;gap:8px;align-items:center}
 .blocked{font:500 12px var(--mono);padding:6px 12px;border-radius:5px;color:var(--muted);
   border:1px solid var(--line);white-space:nowrap;cursor:not-allowed}
 a.download{font:500 12px var(--mono);padding:6px 12px;border-radius:5px;text-decoration:none;
   color:var(--ok);border:1px solid var(--okline);background:var(--okbg);white-space:nowrap}
 @media(max-width:640px){.row{flex-wrap:wrap}.title{flex-basis:100%}}
</style></head><body>
<header><span style="display:flex;align-items:center;gap:18px"><b>skillpp</b>
<nav id="nav"></nav></span><span id="where"></span></header>
<main id="list"></main>
<script>
let S = {rows:[], drafts:[]}, busy = new Set(), timer = null;
// A candidate with a draft is reviewed in the Drafts tab, not listed here.
const inDrafts = r => ["drafted", "revising"].includes(r.state);
let view = "candidates", open = new Set(), writing = new Set(), drafts = {}, answers = {};
let openRows = new Set(), summarising = new Set(), summaryError = {};
let noting = new Set(), notes = {};
let openRuns = new Set(), convos = {}, convoError = {}, openReqs = new Set();
// Every POST says it is JSON: the server refuses anything else, because a
// body without that header is one another site could send cross-site.
const post = (path, body) => fetch(path, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function actions(r){
  const id = esc(r.id), off = busy.has(r.id) ? " disabled" : "";
  switch(r.state){
    case "collecting": return "";
    case "undecided": return `<button class="accept" data-act="accept" data-id="${id}"${off}>Promote</button>
      <button class="decline" data-act="decline" data-id="${id}"${off}>Dismiss</button>`;
    case "accepted": return draftButton(r);
    case "creating": return `<span class="state"><span class="spin"></span>Creating skill…</span>`;
    case "drafted": return `<a class="state ok" data-goto="${id}" title="Review in Drafts">Review</a>`;
    case "revising": return `<span class="state"><span class="spin"></span>Revising…</span>`;
    case "installed": return `<span class="state ok" title="${esc(r.path)}">Skill installed</span>`;
    case "failed": case "declined":
      return `<span class="msg" title="${esc(r.message)}">${r.state==="declined" ? "Agent declined" : "Failed"}: ${esc(r.message)}</span>
        ${draftButton(r)}`;
    case "dismissed": return `<button class="reinstate" data-act="reinstate" data-id="${id}"${off}>Reinstate</button>`;
    default: return "";
  }
}

// Draft Skill asks before it starts: an optional note tells the agent what to
// look out for. While the note is open, its own buttons stand in for this one.
function draftButton(r){
  if(noting.has(r.id)) return "";
  const off = busy.has(r.id) ? " disabled" : "";
  return `<button class="create" data-draft-open="${esc(r.id)}"${off}>Draft Skill</button>`;
}

function noteBlock(r){
  if(!noting.has(r.id) || !["accepted", "failed", "declined"].includes(r.state)) return "";
  const id = esc(r.id), off = busy.has(r.id) ? " disabled" : "";
  return `<div class="note">
    <label for="note-${id}">What should the agent look out for?</label>
    <p class="hint">Optional. It goes to the agent along with the recorded run. Leave it empty and the draft is written from the run alone.</p>
    <textarea id="note-${id}" data-note="${id}" placeholder="e.g. checking every command against DEPLOY.md is the point; the slide styling is not">${esc(notes[r.id] || "")}</textarea>
    <div class="bar"><button class="create" data-draft-send="${id}"${off}>Start drafting</button>
    <button data-draft-cancel="${id}"${off}>Cancel</button></div></div>`;
}

async function startDraft(id){
  busy.add(id); render();
  try {
    const note = (notes[id] || "").trim();
    const r = await (await post("/api/create", {id, note})).json();
    if(!r.ok){ alert(r.error || "failed"); return; }
    // The note stays in `notes`, so a retry after a failed run starts from it.
    noting.delete(id);
  } finally { busy.delete(id); await load(); }
}

// Which drafts this viewer has looked at, per version: a finished revision is
// news again. Kept in the browser, because it is one viewer's attention and
// nothing the ledger needs. On a first visit what already exists is not news.
const SEEN_KEY = "skillpp.seen-drafts";
let seen = null;
const stamp = iso => Date.parse(iso) || 0;
function saveSeen(){ try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)); } catch(e){} }
function syncSeen(){
  if(seen) return;
  try { seen = JSON.parse(localStorage.getItem(SEEN_KEY)); } catch(e){ seen = null; }
  if(seen && seen.cards) return;
  seen = {cards: {}, tab: 0};
  S.drafts.forEach(d => { seen.cards[d.id] = d.drafted_at; seen.tab = Math.max(seen.tab, stamp(d.drafted_at)); });
  saveSeen();
}
const isNew = d => !d.revising && seen && seen.cards[d.id] !== d.drafted_at;
const newSinceTab = () => S.drafts.filter(d => isNew(d) && stamp(d.drafted_at) > seen.tab).length;
function markTabSeen(){
  const top = Math.max(seen.tab, ...S.drafts.map(d => stamp(d.drafted_at)));
  if(top !== seen.tab){ seen.tab = top; saveSeen(); }
}
function markCardSeen(id){
  const d = S.drafts.find(x => x.id === id);
  if(d && seen.cards[id] !== d.drafted_at){ seen.cards[id] = d.drafted_at; saveSeen(); }
}

function renderNav(){
  const nav = document.getElementById("nav");
  nav.innerHTML = [["candidates","Candidates",S.rows.filter(r => !inDrafts(r)).length],["drafts","Drafts",S.drafts.length]]
    .map(([k,l,n]) => {
      const fresh = k === "drafts" ? newSinceTab() : 0;
      return `<button data-view="${k}" aria-selected="${view===k}">${l} (${n})${fresh
        ? `<span class="new" title="${fresh} new draft${fresh===1?"":"s"} since you last looked">${fresh} new</span>` : ""}</button>`;
    }).join("");
  nav.querySelectorAll("[data-view]").forEach(b => b.onclick = () => { view = b.dataset.view; render(); });
}

// Markdown for reviewing a SKILL.md. Everything is escaped first; only the
// tags written here reach the page. Links show their text and never navigate.
function mdInline(raw){
  const codes = [];
  let t = raw.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "@@code" + (codes.length - 1) + "@@"; });
  // A backtick left unmatched means a code span was cut in two — a command
  // that ran over a line break. Emphasis would then eat its asterisks: a real
  // draft rendered `rm -f slide-*.jpg` as `rm -f slide-.jpg`. Show it as
  // written rather than guess; text on the page must never go missing.
  if(t.includes("`")) return t.replace(/@@code(\d+)@@/g, (m, i) => "`" + codes[i] + "`")
    .split("`").map(esc).join("`");
  t = esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<![*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, "<em>$1</em>")
    .replace(/(?<![_\w])_([^_\s][^_]*?)_(?![_\w])/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<span class="link" title="$2">$1</span>');
  return t.replace(/@@code(\d+)@@/g, (m, i) => `<code>${esc(codes[i])}</code>`);
}

function mdFrontmatter(fm){
  const rows = [];
  let parent = "";
  for(const line of fm.split("\n")){
    const m = line.match(/^(\s*)([\w.-]+):\s*(.*)$/);
    if(!m) continue;
    const [, indent, key, value] = m;
    if(!indent && !value){ parent = key; continue; }
    if(!indent) parent = "";
    const name = indent && parent ? `${parent}.${key}` : key;
    rows.push(`<tr><th>${esc(name)}</th><td>${esc(value.replace(/^"(.*)"$/, "$1"))}</td></tr>`);
  }
  return rows.length ? `<table class="fm">${rows.join("")}</table>` : "";
}

function md(src){
  let body = src.replace(/\r\n/g, "\n"), head = "";
  const fm = body.match(/^---\n([\s\S]*?)\n---\n?/);
  if(fm){ head = mdFrontmatter(fm[1]); body = body.slice(fm[0].length); }
  const out = [], lines = body.split("\n"), lists = [];
  let para = [];
  const flush = () => { if(para.length){ out.push(`<p>${mdInline(para.join(" "))}</p>`); para = []; } };
  const closeTo = indent => {
    while(lists.length && lists[lists.length - 1].indent > indent){
      out.push(`</li></${lists.pop().type}>`);
    }
  };
  for(let i = 0; i < lines.length; i++){
    const line = lines[i], indent = line.match(/^\s*/)[0].length, text = line.trim();
    if(text.startsWith("```")){
      flush();
      if(indent === 0) closeTo(-1);
      const code = [];
      for(i++; i < lines.length && !lines[i].trim().startsWith("```"); i++){
        code.push(lines[i].slice(Math.min(indent, lines[i].match(/^\s*/)[0].length)));
      }
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    if(!text){ flush(); continue; }
    const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if(item){
      flush();
      const type = /\d/.test(item[2]) ? "ol" : "ul";
      closeTo(indent);
      const top = lists[lists.length - 1];
      if(top && top.indent === indent && top.type === type){
        out.push("</li><li>");
      } else {
        if(top && top.indent === indent) out.push(`</li></${lists.pop().type}>`);
        lists.push({type, indent});
        out.push(`<${type}><li>`);
      }
      para.push(item[3]);
      continue;
    }
    if(lists.length && indent > 0){ para.push(text); continue; }
    const h = text.match(/^(#{1,6})\s+(.*)$/);
    const rule = /^(-{3,}|\*{3,}|_{3,})$/.test(text);
    const q = text.match(/^>\s?(.*)$/);
    if(h || rule || q || lists.length){ flush(); closeTo(-1); }
    if(h){ const n = h[1].length; out.push(`<h${n}>${mdInline(h[2])}</h${n}>`); continue; }
    if(rule){ out.push("<hr>"); continue; }
    if(q){ out.push(`<blockquote>${mdInline(q[1])}</blockquote>`); continue; }
    para.push(text);
  }
  flush(); closeTo(-1);
  return head + out.join("\n");
}

function questionsBlock(d){
  if(!d.questions.length || d.revising) return "";
  const id = esc(d.id), given = answers[d.id] || {};
  return `<div class="questions"><h4>Open questions</h4>
    <p class="hint">The agent could not tell these from the recorded runs. Answer them to finish the skill; it downloads once none are left.</p>
    ${d.questions.map((q, i) => `<label for="q-${id}-${i}">${i + 1}. ${mdInline(q)}</label>
      <textarea id="q-${id}-${i}" data-answer="${id}" data-index="${i}">${esc(given[i] || "")}</textarea>`).join("")}
    <div class="bar" style="margin-top:10px"><button class="create" data-answer-send="${id}">Send answers</button></div>
  </div>`;
}

function reviseBlock(d){
  const id = esc(d.id);
  const err = d.message ? `<p class="err">Last revision: ${esc(d.message)}</p>` : "";
  if(d.revising) return `<div class="revise"><span class="state"><span class="spin"></span>Revising… the draft below updates when the agent is done</span></div>`;
  if(!writing.has(d.id)) return `<div class="revise">${err}<button class="create" data-revise-open="${id}">Revise</button></div>`;
  return `<div class="revise">${err}
    <textarea data-instruction="${id}" placeholder="What should change? e.g. also cover handbook fact cases, not only walkthrough cards">${esc(drafts[d.id] || "")}</textarea>
    <div class="bar"><button class="create" data-revise-send="${id}">Send to agent</button>
    <button data-revise-cancel="${id}">Cancel</button></div></div>`;
}

function renderDrafts(list){
  const card = d => `<div class="draft ${d.downloaded_at ? "downloaded" : "review"} ${open.has(d.id)?"open":""} ${isNew(d)?"fresh":""}">
      <div class="row" data-toggle="${esc(d.id)}">
        <span class="chev">›</span>
        <span class="title" title="${esc(d.title)}">${esc(d.name)}</span>
        ${isNew(d) ? `<span class="new" title="Written since you last opened it">New</span>` : ""}
        <span class="ago" title="${esc(when(d.drafted_at))}">${d.revising ? "revising" : "drafted " + ago(d.drafted_at)}</span>
        <span class="badge ${d.downloaded_at ? "downloaded" : "review"}">${d.downloaded_at ? "Downloaded" : "To review"}</span>
        <span class="acts">${d.questions.length
          ? `<span class="blocked" title="Answer the open questions first">${d.questions.length} open question${d.questions.length===1?"":"s"}</span>`
          : `<a class="download" href="/api/draft.zip?id=${encodeURIComponent(d.id)}" download="${esc(d.name)}.zip">${d.downloaded_at ? "Download again" : "Download skill"}</a>`}</span>
      </div>
      <p class="desc">${esc(d.description)}</p>
      <div class="body">
        <p class="files">${d.files.map(esc).join(" · ")}</p>
        ${questionsBlock(d)}
        <div class="md">${md(d.body)}</div>
        ${reviseBlock(d)}
      </div></div>`;
  const toReview = S.drafts.filter(d => !d.downloaded_at), downloaded = S.drafts.filter(d => d.downloaded_at);
  list.innerHTML = S.drafts.length ? `
    <div class="section"><h2>To review</h2><span>Drafts you have not downloaded yet.</span></div>
    ${toReview.length ? toReview.map(card).join("") : `<p class="empty">Everything has been downloaded.</p>`}
    <div class="section"><h2>Downloaded</h2><span>Drafts you downloaded. Revising one moves it back to review.</span></div>
    ${downloaded.length ? downloaded.map(card).join("") : `<p class="empty">Nothing downloaded yet.</p>`}`
    : `<p class="empty">No drafts yet. Promote a candidate, then Draft Skill.</p>`;
  list.querySelectorAll("a.download").forEach(a => a.addEventListener("click", () => setTimeout(load, 1000)));
  list.querySelectorAll("[data-revise-open]").forEach(b => b.onclick = () => {
    writing.add(b.dataset.reviseOpen); render();
    const box = document.querySelector(`[data-instruction="${CSS.escape(b.dataset.reviseOpen)}"]`);
    if(box) box.focus();
  });
  list.querySelectorAll("[data-revise-cancel]").forEach(b => b.onclick = () => {
    writing.delete(b.dataset.reviseCancel); delete drafts[b.dataset.reviseCancel]; render();
  });
  list.querySelectorAll("[data-instruction]").forEach(t => t.oninput = () => { drafts[t.dataset.instruction] = t.value; });
  list.querySelectorAll("[data-answer]").forEach(t => t.oninput = () => {
    (answers[t.dataset.answer] ||= {})[t.dataset.index] = t.value;
  });
  list.querySelectorAll("[data-answer-send]").forEach(b => b.onclick = async () => {
    const id = b.dataset.answerSend, d = S.drafts.find(x => x.id === id), given = answers[id] || {};
    const payload = d.questions.map((question, i) => ({question, answer: (given[i] || "").trim()}))
      .filter(a => a.answer);
    if(!payload.length){ alert("Answer at least one question."); return; }
    b.disabled = true;
    const r = await (await post("/api/answer", {id, answers: payload})).json();
    if(!r.ok){ alert(r.error || "failed"); b.disabled = false; return; }
    delete answers[id]; await load();
  });
  list.querySelectorAll("[data-revise-send]").forEach(b => b.onclick = async () => {
    const id = b.dataset.reviseSend, instruction = (drafts[id] || "").trim();
    if(!instruction) return;
    b.disabled = true;
    const r = await (await post("/api/revise", {id, instruction})).json();
    if(!r.ok){ alert(r.error || "failed"); b.disabled = false; return; }
    writing.delete(id); delete drafts[id]; await load();
  });
  list.querySelectorAll("[data-toggle]").forEach(h => h.onclick = ev => {
    if(ev.target.closest("a, button, textarea")) return;
    const id = h.dataset.toggle;
    open.has(id) ? open.delete(id) : open.add(id);
    h.parentElement.classList.toggle("open");
    markCardSeen(id);
    h.parentElement.classList.remove("fresh");
    h.querySelector(".new")?.remove();
  });
}

function candidateBody(r){
  const sum = r.summary ? `<p class="sum">${esc(r.summary)}</p>`
    : summaryError[r.id] ? `<p class="sum pending">No summary: ${esc(summaryError[r.id])}</p>`
    : `<p class="sum pending">Summarising…</p>`;
  return `<h3>Summary</h3>${sum}${stepsBlock(r)}${seenIn(r)}`;
}

// The steps under the request each one served, every request closed until
// clicked: your words, then one line of what changed, the lookups only as a
// count. Opened, it lists the whole sequence in order. A candidate whose steps
// cannot be placed for certain keeps the flat list (`step_groups` in web.py).
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const KINDS = {look: "looked", do: "did", ask: "asked you", fail: "failed"};

function stepsBlock(r){
  if(!r.groups) return `<h3>Steps</h3><ol class="outline">${r.outline.map(l => `<li>${esc(l)}</li>`).join("")}</ol>`;
  const calls = r.groups.reduce((n, g) => n + g.tools, 0);
  return `<h3>Steps <span class="meta">${plural(r.groups.length, "request")} · ${plural(calls, "tool call")}</span></h3>
    <ol class="reqs">${r.groups.map((g, i) => requestItem(r, g, i)).join("")}</ol>`;
}

function requestItem(r, g, i){
  const key = `${r.id}:${i}`, isOpen = openReqs.has(key);
  const said = [
    ...g.digest.map(d => `<span class="${d.warn ? "warn" : "chg"}">${esc(d.text)}</span>`),
    g.looks ? `<span class="lk">${plural(g.looks, "lookup")}</span>` : "",
    g.failed ? `<span class="bad">${g.failed} failed</span>` : "",
  ].filter(Boolean).join(`<span class="lk"> · </span>`) || `<span class="lk">answered, no tools</span>`;
  // Cut to one line while closed; the whole of it on hover.
  const whole = [...g.digest.map(d => d.text), g.looks ? plural(g.looks, "lookup") : "",
                 g.failed ? `${g.failed} failed` : ""].filter(Boolean).join(" · ");
  const lines = isOpen && g.lines.length ? `<div class="lines">${g.lines.map(l =>
    `<div class="ln ${l.kind}${l.warn ? " warn" : ""}"><span class="k">${KINDS[l.kind]}</span><span class="t">${esc(l.text)}</span></div>`).join("")}</div>` : "";
  return `<li class="req${isOpen ? " open" : ""}${g.tools ? "" : " quiet"}">
    <button data-req="${esc(key)}" aria-expanded="${isOpen}"><span class="n">${i + 1}</span>
      <span class="what"><span class="p">${esc(g.request)}</span><span class="dg" title="${esc(whole)}">${said}</span></span></button>${lines}</li>`;
}

// Where the work actually happened. The session id is the link: clicking it
// reads Claude Code's own transcript and shows the conversation, because the
// candidate only keeps the turns of the run that created it.
// "drafted 5 min ago": enough to find the draft just asked for.
function ago(iso){
  const s = (Date.now() - new Date(iso)) / 1000;
  if(isNaN(s)) return "";
  if(s < 60) return "just now";
  if(s < 3600) return `${Math.floor(s / 60)} min ago`;
  if(s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const days = Math.floor(s / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

function when(iso){
  if(!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString([], {day:"numeric", month:"short",
                                               hour:"2-digit", minute:"2-digit"});
}

function seenIn(r){
  if(!r.seen || !r.seen.length) return "";
  const line = s => {
    const open = openRuns.has(s.session);
    const body = !open ? ""
      : convoError[s.session] ? `<div class="convo"><p class="more">${esc(convoError[s.session])}</p></div>`
      : convos[s.session] ? renderConvo(convos[s.session])
      : `<div class="convo"><p class="more">Reading the transcript…</p></div>`;
    return `<li><button class="run" data-run="${esc(s.session)}">${esc(s.session)}</button>
      <span class="when">${esc(when(s.at))}</span>${body}</li>`;
  };
  return `<h3>Seen in</h3><ul class="runs">${r.seen.map(line).join("")}</ul>`;
}

function renderConvo(c){
  const turns = c.turns.map(t => `<div class="turn"><span class="who">You</span><p>${esc(t.prompt)}</p>
      ${t.reply ? `<span class="who">Agent</span><p>${esc(t.reply)}</p>` : ""}</div>`).join("");
  const more = c.total > c.turns.length
    ? `<p class="more">… ${c.total - c.turns.length} more turns in the transcript</p>` : "";
  return `<div class="convo">${turns}${more}</div>`;
}

async function fetchConvo(session){
  if(convos[session] || convoError[session]) return;
  try {
    const res = await (await post("/api/transcript", {session})).json();
    if(res.ok){ convos[session] = res; } else { convoError[session] = res.error || "failed"; }
  } catch(e){ convoError[session] = String(e); }
  if(view === "candidates") render();
}

async function fetchSummary(id){
  const r = S.rows.find(x => x.id === id);
  if(!r || r.summary || summarising.has(id) || summaryError[id]) return;
  summarising.add(id);
  try {
    const res = await (await post("/api/summary", {id})).json();
    const row = S.rows.find(x => x.id === id);
    if(res.ok){ if(row) row.summary = res.summary; } else { summaryError[id] = res.error || "failed"; }
  } catch(e){ summaryError[id] = String(e); }
  finally { summarising.delete(id); if(view === "candidates") render(); }
}

// While a draft runs, the page polls every 5 seconds and redraws the whole
// list, which took the cursor out of whatever box was being typed in — a note
// for the next draft, an answer, a revision. Put it back where it was.
function render(){
  syncSeen();
  if(view === "drafts") markTabSeen();
  const t = document.activeElement;
  const typing = t && t.tagName === "TEXTAREA" ? {
    at: [...t.attributes].filter(a => a.name.startsWith("data-"))
      .map(a => `[${a.name}="${CSS.escape(a.value)}"]`).join(""),
    start: t.selectionStart, end: t.selectionEnd, top: t.scrollTop} : null;
  paint();
  const back = typing && typing.at && document.querySelector("textarea" + typing.at);
  if(back){ back.focus(); back.setSelectionRange(typing.start, typing.end); back.scrollTop = typing.top; }
}

function paint(){
  document.getElementById("where").textContent = `ready at ${S.threshold}×`;
  renderNav();
  const list = document.getElementById("list");
  if(view === "drafts") return renderDrafts(list);
  const highlight = r => r.state === "dismissed" ? "declined"
    : ["drafted", "revising", "installed"].includes(r.state) ? "drafted"
    : ["undecided", "collecting"].includes(r.state) ? (r.ready ? "ready" : "")
    : "accepted";
  const card = r => `<div class="cand ${highlight(r)} ${openRows.has(r.id)?"open":""}" title="${
    {ready: `${S.threshold}× reached: ready to decide`, accepted: "promoted", drafted: "drafted", declined: "dismissed"}[highlight(r)] || ""}">
      <div class="row" data-row="${esc(r.id)}">
      <span class="chev">›</span>
      <span class="title" title="${esc(r.title)}">${esc(r.title) || "(untitled)"}</span>
      ${highlight(r) ? `<span class="badge ${highlight(r)}">${{ready: "Pending", accepted: "Promoted", drafted: "Drafted", declined: "Dismissed"}[highlight(r)]}</span>` : ""}
      <span class="acts">${actions(r)}</span>
      ${r.days_left === null ? `<span class="clock"></span>` : `<span class="clock ${r.days_left === 0 ? "gone" : r.days_left <= 3 ? "soon" : ""}"
        title="Deleted by skillpp expire ${S.ttl} days after it was last recognized, unless it reaches ${S.threshold}× first">${r.days_left === 0 ? "⏱ expired" : `⏱ ${r.days_left}d`}</span>`}
      <span class="seen count ${r.occurrences >= S.threshold ? "reached" : ""}" title="recognized ${r.occurrences} time${r.occurrences===1?"":"s"}">${r.occurrences}×</span></div>
      ${noteBlock(r)}
      <div class="body">${candidateBody(r)}</div></div>`;
  const declined = S.rows.filter(r => r.state === "dismissed");
  const promoted = S.rows.filter(r => !["undecided", "collecting", "dismissed"].includes(r.state) && !inDrafts(r));
  const open_ = S.rows.filter(r => ["undecided", "collecting"].includes(r.state));
  const ready = open_.filter(r => r.ready), collecting = open_.filter(r => !r.ready);
  list.innerHTML = `
    ${promoted.length ? `<div class="section"><h2>Promoted</h2><span>Candidates you promoted. Draft a skill from them; the draft appears in the Drafts tab.</span></div>
    ${promoted.map(card).join("")}` : ""}
    <div class="section"><h2>Still collecting</h2><span>Work skillpp saw you repeat. Once something is seen ${S.threshold}×, you can promote or dismiss it.</span></div>
    ${ready.length + collecting.length ? `<div class="collecting">
      <div class="thead"><span class="chev"></span><span class="title">Title</span>
        <span class="acts"></span><span class="clock">Time to expire</span><span class="count">Count</span></div>
      ${ready.concat(collecting).map(card).join("")}</div>` : `<p class="empty">No candidates yet.</p>`}
    ${declined.length ? `<div class="section"><h2>Dismissed</h2><span>Candidates you dismissed. Reinstate one to bring it back.</span></div>
    ${declined.map(card).join("")}` : ""}`;
  list.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, b.dataset.id));
  list.querySelectorAll("[data-draft-open]").forEach(b => b.onclick = () => {
    noting.add(b.dataset.draftOpen); render();
    const box = document.querySelector(`[data-note="${CSS.escape(b.dataset.draftOpen)}"]`);
    if(box) box.focus();
  });
  list.querySelectorAll("[data-note]").forEach(t => t.oninput = () => { notes[t.dataset.note] = t.value; });
  list.querySelectorAll("[data-draft-cancel]").forEach(b => b.onclick = () => {
    noting.delete(b.dataset.draftCancel); delete notes[b.dataset.draftCancel]; render();
  });
  list.querySelectorAll("[data-draft-send]").forEach(b => b.onclick = () => startDraft(b.dataset.draftSend));
  list.querySelectorAll("[data-row]").forEach(h => h.onclick = ev => {
    if(ev.target.closest("a, button")) return;
    const id = h.dataset.row;
    if(openRows.has(id)){ openRows.delete(id); } else { openRows.add(id); fetchSummary(id); }
    h.parentElement.classList.toggle("open");
  });
  list.querySelectorAll("[data-run]").forEach(b => b.onclick = () => {
    const s = b.dataset.run;
    if(openRuns.has(s)){ openRuns.delete(s); } else { openRuns.add(s); fetchConvo(s); }
    render();
  });
  list.querySelectorAll("[data-req]").forEach(b => b.onclick = () => {
    const k = b.dataset.req;
    openReqs.has(k) ? openReqs.delete(k) : openReqs.add(k);
    render();
  });
  list.querySelectorAll("[data-goto]").forEach(a => a.onclick = () => {
    view = "drafts"; open.add(a.dataset.goto); markCardSeen(a.dataset.goto); render();
  });
}

async function act(what, id){
  busy.add(id); render();
  try {
    const r = await (await post("/api/" + what, {id})).json();
    if(!r.ok) alert(r.error || "failed");
  } finally { busy.delete(id); await load(); }
}

async function load(){
  S = await (await fetch("/api/state")).json();
  render();
  clearTimeout(timer);
  if(S.rows.some(r => r.state === "creating" || r.state === "revising")) timer = setTimeout(load, 5000);
}
load();
</script>
</body></html>
"""
