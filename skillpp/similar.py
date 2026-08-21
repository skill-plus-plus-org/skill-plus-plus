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
