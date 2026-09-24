"""Shared pieces for deciding and applying "same procedure".

`fold_into` moves one entry's evidence into another (`skill-plus-plus merge`). What is
embedded to decide a merge lives in `skill_plus_plus.matching`.

This module used to hold a background pass: signature similarity as a filter,
then an embedding for the pairs that got through. The filter was lexical and
asymmetric, and hid pairs an embedding rates 0.98 alike behind a score of
0.365. Matching now happens once, by embedding, when an episode is saved.
"""

from __future__ import annotations


def fold_into(keep, drop) -> None:
    """Fold *drop*'s evidence into *keep*, in place.

    Occurrences count every recognition, so they add up; sessions are a
    union.
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
    # Both entries' recognitions, summed: the count is how often the procedure
    # was recognized, not in how many sessions (see capture._fold_steps).
    keep.occurrences += drop.occurrences
    if drop.last_seen > keep.last_seen:
        keep.last_seen = drop.last_seen



# Not one of `decisions._TRUTH`'s labels on purpose. That dict scores a ranker's
# hint against what a person decided; a fold is neither the ranker's opinion nor
# a person's, so counting it there would corrupt `skill-plus-plus accuracy`.
AUTO_MERGED = "auto-merged"
