"""Shared pieces for deciding and applying "same procedure".

`as_text` is what gets embedded for an entry (`skillpp.matching`), and
`fold_into` moves one entry's evidence into another (`skillpp merge`).

This module used to hold a background pass: signature similarity as a filter,
then an embedding for the pairs that got through. The filter was lexical and
asymmetric, and hid pairs an embedding rates 0.98 alike behind a score of
0.365. Matching now happens once, by embedding, when an episode is saved.
"""

from __future__ import annotations


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



# Not one of `decisions._TRUTH`'s labels on purpose. That dict scores a ranker's
# hint against what a person decided; a fold is neither the ranker's opinion nor
# a person's, so counting it there would corrupt `skillpp accuracy`.
AUTO_MERGED = "auto-merged"
