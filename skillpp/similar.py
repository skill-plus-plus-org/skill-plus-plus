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


# Not one of `decisions._TRUTH`'s labels on purpose. That dict scores a ranker's
# hint against what a person decided; a fold is neither the ranker's opinion nor
# a person's, so counting it there would corrupt `skillpp accuracy`.
AUTO_MERGED = "auto-merged"


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


def run_background_check(config, *, timeout: float | None = None) -> dict:
    """Fold the queued entries that are the same procedure.

    Folds without asking. The gate this used to have was redundant with the one
    the pipeline already enforces further down: a folded entry is still only a
    *candidate*, and `fold_into` keeps both sides' intents and variants rather
    than discarding the loser's evidence. So a wrong fold arrives at
    `skillpp review` as a candidate whose intents plainly do not belong
    together, and it still needs an explicit `promote` to become anything. The
    person is in the loop at the proposal, which is where they were always going
    to look.

    Every fold is appended to `decisions.jsonl`, which is the only place the
    dropped entry's identity survives once its file is gone.

    Only a clean pass drains the queue. A timeout or an unreachable model leaves
    it exactly as it was, so the next session retries the whole backlog instead
    of dropping the pairs this run never reached.
    """
    from . import decisions
    from .ledger import Ledger, STATUS_CANDIDATE, STATUS_COVERED, STATUS_PROMOTED

    queued = _read_queue_ids(config)
    if not queued:
        return {"status": "empty"}

    ledger = Ledger(config)
    # Promoted skills are compared against, not just candidates. Leaving them
    # out meant a skill became invisible the moment it was promoted: capture's
    # `find_match` does see it, but decides lexically at 0.85, and measured over
    # 5,995 real pairs nothing reaches 0.85 at all — the highest is 0.814. So a
    # promoted skill could never match again by either route, its occurrence
    # count froze at promotion, and every later run of the same work opened a
    # fresh candidate. `lifecycle.reconcile` still documents the opposite
    # ("it keeps matching future occurrences of the same work"); it does not.
    entries = [e for e in ledger.all()
               if e.status in (STATUS_CANDIDATE, STATUS_PROMOTED)]
    pairs = near_misses(entries, floor=config.queued_near_miss_floor,
                        ceiling=config.similarity_threshold)
    # Only pairs involving something this queue actually named. The rest were
    # compared on an earlier pass and were not the same procedure then either.
    pairs = [p for p in pairs if p[0].id in queued or p[1].id in queued]

    budget = timeout if timeout is not None else config.background_timeout_seconds
    deadline = time.monotonic() + budget
    vectors: dict[str, list[float]] = {}
    # An entry already folded this pass is gone from the ledger, so a later pair
    # naming it would fold a stale object over a saved one. Same guard
    # `cmd_merge` has, for the same reason: folds must not chain.
    folded: set[str] = set()
    merged, checked = 0, 0
    timed_out = unreachable = False

    for a, b, lex in pairs:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        if a.id in folded or b.id in folded:
            continue
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
        if score < config.embed_floor:
            continue

        # A promoted skill is never folded away, and never absorbs a candidate
        # by deletion. The skill already exists; what this match means is that
        # it was used again. So reinforce it — the count is the evidence it is
        # still live — and mark the candidate as covered rather than proposing
        # work a skill already does.
        skill = (a if a.status == STATUS_PROMOTED else
                 b if b.status == STATUS_PROMOTED else None)
        if skill is not None:
            other = b if skill is a else a
            if other.status == STATUS_PROMOTED:
                continue  # two skills: not ours to reconcile
            for sid in other.sessions:
                if sid not in skill.sessions:
                    skill.sessions.append(sid)
            skill.occurrences = max(len(skill.sessions), skill.occurrences)
            skill.last_seen = other.last_seen or skill.last_seen
            other.status = STATUS_COVERED
            other.notes = (f"covered by promoted skill {skill.id} "
                           f"(embedding {score:.3f})\n" + (other.notes or "")).strip()
            ledger.save(skill)
            ledger.save(other)
            folded.add(other.id)
            merged += 1
            decisions.record(
                config, skill, AUTO_MERGED,
                note=f"used again — {other.id} ({other.title[:60]}) is covered "
                     f"by this skill; lexical {lex:.3f}, embedding {score:.3f}")
            continue

        fold_into(a, b)
        ledger.save(a)
        ledger.delete(b.id)
        folded.add(b.id)
        merged += 1
        # The dropped entry's file is now gone; this line is the only record
        # that it existed and what it was called.
        decisions.record(
            config, a, AUTO_MERGED,
            note=f"absorbed {b.id} ({b.title[:80]}) — lexical {lex:.3f}, "
                 f"embedding {score:.3f}")

    if not timed_out and not unreachable:
        _drain_queue(config)
    return {"status": "checked", "pairs_considered": len(pairs),
            "pairs_checked": checked, "merged": merged,
            "timed_out": timed_out, "model_unreachable": unreachable}
