"""Deciding whether two candidates are the same procedure, by embedding.

Lexical similarity does most of this job and does it free. Measured against a
release procedure repeated with realistic variation: identical steps with
different arguments 1.000, one step reordered 0.880, an extra verification step
1.000, two exploration steps prepended 1.000 once the trim has run. None of
those need a model.

It fails on exactly one shape — **the same procedure with a step served by a
different tool**. `npm test` and `pytest -q` in an otherwise identical release
share not one token and score 0.786, just under the 0.85 threshold, which is the
worst place for a signal to land: too low to merge, too high to be obviously
unrelated. An embedding separates that pair at 0.912, against 0.451 for a
genuinely different procedure.

So this is a tie-breaker, not a replacement. It is consulted only for pairs
already in the near-miss band, which keeps almost every comparison free.

**It does not run in a hook.** Matching happens during `SessionEnd`, and a hook
that waits on a model adds that wait to every session and fails when the model
is absent. This is reached from `skillpp merge`, which a person runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .local import (DEFAULT_HOST, LocalModelUnavailable, cosine, embed)
from .normalize import signature
from .recurrence import similarity


def as_text(entry) -> str:
    """An entry as one line an embedding model can read.

    Intent first because it says what the work was *for*, then the steps in
    order. Raw commands, not fingerprints: reducing `python3 -m unittest
    discover` to `python3` removes the very token that makes it recognisable as
    a test run.
    """
    intent = (entry.intents or [entry.title or ""])[0]
    steps = []
    for step in entry.steps[:30]:
        payload = step.get("input") or {}
        body = (payload.get("command") or payload.get("file_path")
                or ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2]))
        steps.append(f"{step.get('tool', '?')}: {str(body)[:120]}")
    return f"{intent}\n" + "\n".join(steps)


def near_misses(entries, *, floor: float, ceiling: float):
    """Pairs whose lexical similarity sits below the merge threshold but above
    *floor* — the only pairs worth spending a model call on."""
    pairs = []
    sigs = {e.id: (e.signature or signature(e.steps)) for e in entries}
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            score = similarity(sigs[a.id], sigs[b.id])
            if floor <= score < ceiling:
                pairs.append((a, b, score))
    return sorted(pairs, key=lambda p: -p[2])


def same_procedure(a, b, *, model: str, host: str = DEFAULT_HOST,
                   floor: float = 0.80) -> tuple[bool | None, float, str]:
    """Are *a* and *b* the same procedure?

    Returns ``(verdict, score, why)``. ``None`` means no opinion — the model was
    unreachable — and callers must treat that as "leave them separate". Merging
    two candidates on a failed call would be irreversible in the way that
    matters: the second one's evidence is folded into the first.
    """
    try:
        va, vb = embed(as_text(a), model=model, host=host), \
                 embed(as_text(b), model=model, host=host)
    except LocalModelUnavailable as exc:
        return None, 0.0, f"no embedding model ({exc}); leaving both"
    score = cosine(va, vb)
    if score >= floor:
        return True, score, f"same shape at {score:.3f}"
    return False, score, f"different shape at {score:.3f}"


def fold_into(keep, drop) -> None:
    """Fold *drop*'s evidence into *keep*, in place.

    Occurrences count *sessions*, so the arithmetic is a union rather than a
    sum: two sightings inside one session are one occurrence, which is the
    correction this project already had to make once.
    """
    for sid in drop.sessions:
        if sid not in keep.sessions:
            keep.sessions.append(sid)
    for project in drop.projects:
        if project not in keep.projects:
            keep.projects.append(project)
    for intent in drop.intents:
        if intent not in keep.intents:
            keep.intents.append(intent)
    del keep.intents[8:]
    if drop.steps and len(keep.variants) < 4:
        keep.variants.append(drop.steps)
    # NOT `occurrences + 1`. That was the first version and it is the exact
    # double-count this project corrected once before: two sightings inside
    # one session are one occurrence, so the count is the size of the
    # session union, never a sum. A test pins both directions.
    keep.occurrences = max(len(keep.sessions), keep.occurrences)
    if drop.last_seen > keep.last_seen:
        keep.last_seen = drop.last_seen


# --------------------------------------------------------------------------
# the queued pass: the same check, moved off the SessionEnd path
# --------------------------------------------------------------------------
#
# `near_misses` above is bounded below by `near_miss_floor` because the only
# caller was `skillpp merge`, which a person types and then waits on. That floor
# is the wrong number for a pass nobody is waiting on, and the gap is not
# academic: one procedure done three times scored 0.46-0.50 against itself, so
# the three entries it left behind were never shown to an embedding at all. They
# sat at x1 each, and the recurrence threshold they should have reached together
# was never reached by any of them.
#
# So the check moves. `SessionEnd` records which entry it touched and decides
# nothing. The start of the next session spawns a detached process that runs the
# same comparison over a much wider band and writes a report. Folding stays where
# it was: behind an explicit `--apply` on a command a person runs.


def maybe_spawn_background_check(config) -> dict:
    """SessionStart's whole job: anything queued? then fire and forget.

    Never waits, never talks to a model, never prints. The queue check is a
    `stat`, and the spawn does not join — so this costs a session nothing
    measurable whether the queue is empty or holds a hundred entries.
    """
    path = config.pending_checks_file
    try:
        if not path.exists() or path.stat().st_size == 0:
            return {"status": "empty"}
    except OSError:
        return {"status": "empty"}
    try:
        proc = _spawn_background_process(config)
    except OSError as exc:
        return {"status": "spawn-failed", "why": str(exc)}
    return {"status": "spawned", "pid": proc.pid}


def _spawn_background_process(config) -> subprocess.Popen:
    """Start the queued pass detached, and do not wait for it.

    `start_new_session` matters: without it the child is in the hook's process
    group, so the terminal closing — or Claude Code reaping the hook — can
    signal a process that is midway through an embedding call.
    """
    package_root = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "skillpp",
            "--root", str(config.root), "background-merge-check"]
    return subprocess.Popen(
        argv, cwd=str(package_root),
        env={**os.environ, "PYTHONPATH": str(package_root)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def _read_queue_ids(config) -> set[str]:
    """Entry ids waiting to be checked. Unreadable lines are skipped."""
    path = config.pending_checks_file
    ids: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ids
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry_id = json.loads(line).get("entry_id")
        except json.JSONDecodeError:
            continue
        if entry_id:
            ids.add(str(entry_id))
    return ids


def _drain_queue(config) -> None:
    """Empty the queue, atomically.

    The one write here worth protecting: a half-truncated queue loses track of
    what still needs checking, silently, and nothing downstream would report it.
    """
    path = config.pending_checks_file
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text("", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _write_report(config, **fields) -> None:
    report = {"generated_at": datetime.now(timezone.utc).replace(
                  microsecond=0).isoformat(),
              "floor": config.queued_near_miss_floor,
              "ceiling": config.similarity_threshold,
              "embed_floor": config.embed_floor,
              **fields}
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        config.near_miss_report_file.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_near_miss_report(config) -> dict | None:
    """The last report, or None if there is not a readable one."""
    try:
        return json.loads(
            config.near_miss_report_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_background_check(config, *, timeout: float | None = None) -> dict:
    """Check the queued entries for near-misses and write a report.

    Reports; never folds. That asymmetry is why this needs no lock — two of
    these racing compute the same free local answer twice and the later write
    wins, which is waste rather than a wrong result.

    Only a clean pass drains the queue. A timeout or an unreachable model leaves
    it exactly as it was, so the next session retries the whole backlog instead
    of dropping the pairs this run never reached.
    """
    from .ledger import Ledger, STATUS_CANDIDATE

    queued = _read_queue_ids(config)
    if not queued:
        return {"status": "empty"}

    entries = [e for e in Ledger(config).all() if e.status == STATUS_CANDIDATE]
    pairs = near_misses(entries, floor=config.queued_near_miss_floor,
                        ceiling=config.similarity_threshold)
    # Only pairs involving something this queue actually named. The rest were
    # already offered on an earlier pass and declined by whoever read it.
    pairs = [p for p in pairs if p[0].id in queued or p[1].id in queued]

    budget = timeout if timeout is not None else config.background_timeout_seconds
    deadline = time.monotonic() + budget
    vectors: dict[str, list[float]] = {}
    found, checked = [], 0
    timed_out = unreachable = False

    for a, b, lex in pairs:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        try:
            for entry in (a, b):
                if entry.id not in vectors:
                    vectors[entry.id] = embed(
                        as_text(entry), model=config.embed_model,
                        host=config.ollama_url)
        except LocalModelUnavailable:
            # One dead host means every remaining pair is dead too. Stopping
            # here is the difference between failing in a second and burning
            # the whole budget rediscovering the same outage.
            unreachable = True
            break
        checked += 1
        score = cosine(vectors[a.id], vectors[b.id])
        if score >= config.embed_floor:
            found.append({"a": a.id, "a_title": a.title,
                          "b": b.id, "b_title": b.title,
                          "lexical": round(lex, 3),
                          "embedding": round(score, 3),
                          "why": f"same shape at {score:.3f}"})

    _write_report(config, candidates=found, pairs_considered=len(pairs),
                  pairs_checked=checked, timed_out=timed_out,
                  model_unreachable=unreachable)
    if not timed_out and not unreachable:
        _drain_queue(config)
    return {"status": "checked", "pairs_considered": len(pairs),
            "pairs_checked": checked, "found": len(found),
            "timed_out": timed_out, "model_unreachable": unreachable}
