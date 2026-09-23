"""What a person decided, appended once and never rewritten.

The ledger holds current state: a candidate is `promoted` or it is not. That is
enough to run the tool and useless for measuring it, because the moment a status
changes the previous judgement is gone.

This log keeps the pair that matters — **what the ranker thought, and what the
person decided** — so accuracy on real work accumulates without anyone
maintaining a fixture. A benchmark whose ground truth was written by whoever
wrote the detector measures internal consistency; this measures the thing
itself.

Append-only, one JSON object per line, and never read by the capture path. If it
is lost, nothing breaks and only the evidence is gone.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# What a person did. `promoted` and `dismissed` are direct labels. `reopened`
# is the interesting one: it means a model parked something a person wanted
# back, which is a false drop caught in the act.
PROMOTED = "promoted"
DISMISSED = "dismissed"
REOPENED = "reopened"
PARKED = "parked"


def record(config, entry, decision: str, note: str = "") -> None:
    """Append one decision. Never raises — losing a label must not fail a command."""
    row = {
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "id": entry.id,
        "decision": decision,
        # The ranker's opinion at the moment of the decision, which is what
        # makes the line scorable rather than merely historical.
        "hint": entry.hint,
        "occurrences": entry.occurrences,
        "steps": len(entry.steps),
        "title": entry.title[:120],
        "source": entry.source,
        "note": note,
    }
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        with config.decisions_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(config) -> list[dict]:
    """Every decision, oldest first. Unreadable lines are skipped, not fatal."""
    path = Path(config.decisions_file)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# A person promoting something means it was a method; dismissing it means it was
# not. Reopening means a model parked something they wanted, which is a `method`
# label and a recorded miss. Parking is the model's own act and is never truth.
_TRUTH = {PROMOTED: True, DISMISSED: False, REOPENED: True}


def score(config) -> dict:
    """How often the ranker agreed with the person, on real decisions only.

    Counted per candidate, latest decision winning, because a candidate parked
    then reopened then promoted is one judgement with a history, not three.
    """
    latest = {}
    for row in read(config):
        if row.get("decision") in _TRUTH:
            latest[row["id"]] = row
    hits = misses = unranked = 0
    detail = []
    for row in latest.values():
        truth = _TRUTH[row["decision"]]
        hint = row.get("hint") or ""
        if not hint:
            unranked += 1
            continue
        agreed = (hint == "method") == truth
        hits += agreed
        misses += not agreed
        if not agreed:
            detail.append({"id": row["id"], "title": row["title"],
                           "hint": hint, "decision": row["decision"]})
    return {"judged": len(latest), "scored": hits + misses, "agreed": hits,
            "disagreed": misses, "unranked": unranked, "misses": detail}
