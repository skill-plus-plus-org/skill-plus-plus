"""Deciding whether two traces are the same workflow.

This is deliberately *lexical* — sequence and token overlap over normalised
step shapes, no embeddings, no model call. It runs inside a hook, where a
network round-trip would be unacceptable, and it only needs to be good enough
to cluster candidates. The semantic judgement ("these two really are the same
thing") happens later, at review time, where an agent is already in the loop
and can read both entries (README 3.5).
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .ledger import Entry


def _tokens(signature: str) -> list[str]:
    return [t for t in signature.split(" | ") if t]


def similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity between two workflow signatures."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0

    # Order-sensitive: a workflow is a sequence, not a bag of commands.
    order = SequenceMatcher(None, ta, tb).ratio()
    # Order-insensitive: tolerates a step being moved or interleaved.
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    return 0.6 * order + 0.4 * jaccard


def find_match(signature: str, entries: list[Entry], threshold: float) -> Entry | None:
    """Best entry above *threshold*, or None."""
    best: tuple[float, Entry] | None = None
    for entry in entries:
        if entry.status == "dismissed":
            continue
        score = similarity(signature, entry.signature)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, entry)
    return best[1] if best else None
