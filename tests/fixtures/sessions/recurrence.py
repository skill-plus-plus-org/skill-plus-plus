#!/usr/bin/env python3
"""Score recurrence: do repeated runs of one procedure become one candidate?

    python3 tests/fixtures/sessions/recurrence.py

`score.py` folds each live session into its own ledger, so it can never see
whether two sessions of the same procedure were unified — and nothing else
asserted it. This folds every live session into **one** ledger, in the order the
sessions actually started, through the real `fold_session`, and compares where
each banked episode landed against `truth.families`.

Ground truth is one label per banked episode, written by hand. Same label, same
procedure: "add a walkthrough-card eval case" and "add a handbook fact eval
case" are both `add-eval-case`, with the kind of case as a variation.

Scored three ways, because each hides something the others show:

* **families** — how many entries each family's episodes were spread across.
  One is the target. More is a missed merge.
* **wrong merges** — every entry holding episodes from more than one family,
  named. Zero is the target.
* **pairs** — over every pair of banked episodes: same family and same entry is a
  correct merge, different family and same entry a wrong one, same family and
  different entries a miss.

Verdicts are baked into the fixtures, so folding asks no boundary model. What
decides merging is whatever matcher `fold_session` uses at the time this runs —
which is the point: it measures the pipeline as it stands.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from collections import defaultdict
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import score as live_score                     # noqa: E402
from skillpp.capture import fold_session       # noqa: E402
from skillpp.config import Config              # noqa: E402

BANKED = ("created", "merged")


def fold_all(config: Config) -> list[dict]:
    """Fold every fixture into *config*'s ledger. One row per banked episode."""
    docs = sorted(live_score.load(None), key=lambda d: (d.get("started", ""), d["tag"]))
    rows = []
    for doc in docs:
        result = fold_session(config, {"session_id": doc["tag"], "cwd": "",
                                       "prompts": [],
                                       "steps": copy.deepcopy(doc["steps"])})
        episodes = [e for e in (result.get("episodes") or [result])
                    if e.get("status") in BANKED]
        families = doc["truth"].get("families", [])
        if len(families) != len(episodes):
            raise SystemExit(
                f"{doc['tag']}: {len(episodes)} banked episode(s) but "
                f"{len(families)} family label(s) — fix the fixture truth first")
        for n, (episode, family) in enumerate(zip(episodes, families), 1):
            rows.append({"episode": f"{doc['tag']}#{n}", "family": family,
                         "entry": episode["id"], "status": episode["status"]})
    return rows


def evaluate(rows: list[dict]) -> dict:
    by_family = defaultdict(set)
    by_entry = defaultdict(set)
    for r in rows:
        by_family[r["family"]].add(r["entry"])
        by_entry[r["entry"]].add(r["family"])

    correct = wrong = missed = 0
    for a, b in combinations(rows, 2):
        same_family = a["family"] == b["family"]
        same_entry = a["entry"] == b["entry"]
        correct += same_family and same_entry
        wrong += (not same_family) and same_entry
        missed += same_family and not same_entry

    return {
        "families": {f: len(ids) for f, ids in sorted(by_family.items())},
        "sizes": {f: sum(1 for r in rows if r["family"] == f) for f in by_family},
        "wrong_merges": {e: sorted(fs) for e, fs in by_entry.items() if len(fs) > 1},
        "pairs": {"correct": correct, "wrong": wrong, "missed": missed,
                  "should_merge": correct + missed},
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config = Config(Path(tmp) / "skillpp")
        config.ensure_dirs()
        rows = fold_all(config)
    result = evaluate(rows)

    sessions = len({r["episode"].split("#")[0] for r in rows})
    print(f"{len(rows)} banked episodes from {sessions} live sessions, folded into one ledger\n")
    print("family                         runs  entries")
    for family, entries in result["families"].items():
        runs = result["sizes"][family]
        flag = "" if entries == 1 else "   <-- split across entries"
        print(f"  {family:<28} {runs:>4}  {entries:>7}{flag}")

    p = result["pairs"]
    print(f"\npairs that should merge: {p['should_merge']}   "
          f"merged {p['correct']}, missed {p['missed']}")
    print(f"wrong merges (different families in one entry): {p['wrong']} pair(s)")
    for entry, families in result["wrong_merges"].items():
        print(f"  {entry}: {', '.join(families)}")

    # Only families that repeat can be unified or not; a one-run family is in
    # a single entry by construction and would flatter the count.
    repeated = [f for f, n in result["sizes"].items() if n > 1]
    unified = sum(1 for f in repeated if result["families"][f] == 1)
    print(f"\nrepeated procedures unified: {unified}/{len(repeated)}   "
          f"entries mixing families: {len(result['wrong_merges'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
