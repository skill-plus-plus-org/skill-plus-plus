#!/usr/bin/env python3
"""Score recurrence: do repeated runs of one procedure become one candidate?

    python3 tests/fixtures/sessions/recurrence.py

`score.py` folds each live session into its own ledger, so it can never see
whether two sessions of the same procedure were unified — and nothing else
asserted it. This folds every live session into **one** ledger, in the order the
sessions actually started, through the real `fold_session`, and compares where
each banked episode landed against `truth.families`.

Ground truth is one label per banked episode, written by hand. Same label, same
procedure: "add a tutorial-card eval case" and "add a glossary fact eval
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
from skill_plus_plus.capture import fold_session       # noqa: E402
from skill_plus_plus.config import Config              # noqa: E402

BANKED = ("created", "merged")


def gap_labels(doc: dict, banked: int, families: list, subjects: list) -> tuple[list, list]:
    """Labels for what a known gap actually banked.

    An episode keeps its family only where its steps are exactly one planned
    task: a missed cut that merged two tasks gets a label of its own, so it
    counts neither as a correct merge nor for either family. The cuts made are
    read from the stored verdicts, the planned ones from `truth.boundary_after`.
    """
    from skill_plus_plus.segment import is_prompt
    work = [s for s in doc["steps"] if not is_prompt(s)]
    made = [n for n, s in enumerate(work, 1) if s.get("end") is True and n < len(work)]
    planned = doc["truth"].get("boundary_after") or []
    ranges = lambda cuts: list(zip([0, *cuts], [*cuts, len(work)]))
    wanted = {r: i for i, r in enumerate(ranges(planned))}
    got = ranges(made)
    if len(got) != banked:
        return ([f"unlabelled:{doc['tag']}#{n}" for n in range(1, banked + 1)],
                [None] * banked)
    fam, sub = [], []
    for n, r in enumerate(got, 1):
        if r in wanted and wanted[r] < len(families):
            fam.append(families[wanted[r]])
            sub.append(subjects[wanted[r]])
        else:
            fam.append(f"unlabelled:{doc['tag']}#{n}")
            sub.append(None)
    return fam, sub


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
        families = list(doc["truth"].get("families", []))
        subjects = list(doc["truth"].get("subjects") or [])
        subjects += [None] * (len(families) - len(subjects))
        # A known gap banks what a correct run would not. Its extra episodes get
        # a label of their own, so they can never score as a correct merge and
        # show up as a wrong one if they join another family's entry.
        if doc.get("expected_fail") and len(episodes) != len(families):
            families, subjects = gap_labels(doc, len(episodes), families, subjects)
        if len(families) != len(episodes):
            raise SystemExit(
                f"{doc['tag']}: {len(episodes)} banked episode(s) but "
                f"{len(families)} family label(s) — fix the fixture truth first")
        for n, (episode, family, subject) in enumerate(zip(episodes, families, subjects), 1):
            rows.append({"episode": f"{doc['tag']}#{n}", "family": family,
                         "entry": episode["id"], "status": episode["status"],
                         "subject": subject, "kind": doc.get("kind"),
                         "level": doc["truth"].get("level")})
    return rows


def evaluate(rows: list[dict]) -> dict:
    by_family = defaultdict(set)
    by_entry = defaultdict(set)
    for r in rows:
        by_family[r["family"]].add(r["entry"])
        by_entry[r["entry"]].add(r["family"])

    correct = wrong = missed = 0
    levels = defaultdict(lambda: {"should": 0, "merged": 0})
    for a, b in combinations(rows, 2):
        same_family = a["family"] == b["family"]
        same_entry = a["entry"] == b["entry"]
        correct += same_family and same_entry
        wrong += (not same_family) and same_entry
        missed += same_family and not same_entry
        if same_family:
            cell = levels[(pair_level(a, b), a.get("kind"))]
            cell["should"] += 1
            cell["merged"] += same_entry

    return {
        "families": {f: len(ids) for f, ids in sorted(by_family.items())},
        "sizes": {f: sum(1 for r in rows if r["family"] == f) for f in by_family},
        "wrong_merges": {e: sorted(fs) for e, fs in by_entry.items() if len(fs) > 1},
        "pairs": {"correct": correct, "wrong": wrong, "missed": missed,
                  "should_merge": correct + missed},
        "levels": dict(levels),
    }


# How alike two runs of one procedure are, which is what makes merging them
# easy or hard: the same prompts, the same goal driven differently, or the same
# procedure on another subject. Set per session in the catalogue.
LEVELS = ("identical", "same-goal", "different subject")


def pair_level(a: dict, b: dict) -> str:
    if a.get("level") and a.get("level") == b.get("level"):
        return a["level"]
    return "different subject"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config = Config(Path(tmp) / "skill-plus-plus")
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

    kinds = sorted({k for _, k in result["levels"]} - {None}) or [None]
    if any(k for k in kinds):
        print("\nmerged pairs by how alike the runs are")
        print(f"  {'level':<20}" + "".join(f"{k:>14}" for k in kinds))
        for level in LEVELS:
            cells = [result["levels"].get((level, k)) for k in kinds]
            if not any(cells):
                continue
            print(f"  {level:<20}" + "".join(
                f"{(str(c['merged']) + '/' + str(c['should'])) if c else '–':>14}" for c in cells))

    # Only families that repeat can be unified or not; a one-run family is in
    # a single entry by construction and would flatter the count.
    repeated = [f for f, n in result["sizes"].items() if n > 1]
    unified = sum(1 for f in repeated if result["families"][f] == 1)
    print(f"\nrepeated procedures unified: {unified}/{len(repeated)}   "
          f"entries mixing families: {len(result['wrong_merges'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
