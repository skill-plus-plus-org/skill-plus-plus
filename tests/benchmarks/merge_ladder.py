#!/usr/bin/env python3
"""Score what matching embeds, one rendering at a time.

    python3 tests/benchmarks/merge_ladder.py R0          # today's conversation text
    python3 tests/benchmarks/merge_ladder.py R0 R1 R2    # a ladder, one row each

Every live session is banked with merging off, so each episode is its own entry.
Each rendering then turns an entry into the text that would be embedded, and the
fold is replayed in the order the sessions happened, at every floor from 0.50 to
0.99 — the same comparison `matching.find_same` makes, against each entry's
first run.

One row per rendering:

* **danger** — the highest score between two *different* procedures.
* **floor** — the lowest floor with no wrong merge in the replay.
* **merged** — correct merges at that floor, of every same-procedure pair.
* **margin** — floor minus danger. Negative means the replay avoided the wrong
  merge only because of fold order, not because the scores separate.
* **2x2 gap** — runs 4, 5, 6 and 8: two procedures on two files. Worst
  same-procedure score minus best different-procedure score; positive means the
  text follows the procedure rather than the material.
* **3x** — families with an entry holding three or more runs at that floor.

A step on the ladder changes one thing. Nothing here decides; it prints.
"""

from __future__ import annotations

import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fixtures" / "sessions"))

import recurrence                                   # noqa: E402
from skillpp.config import Config                   # noqa: E402
from skillpp.ledger import Entry, Ledger            # noqa: E402
from skillpp.local import cosine, embed             # noqa: E402
from skillpp.matching import turns_text             # noqa: E402

# Two procedures on two source files — the pairs that separate procedure from material.
PRESENTATION = {"article": "3e035b46", "handoff": "bfb9ecdc"}
POST = {"article": "45a6f1a8", "handoff": "cb233c48"}

FILE = re.compile(r"\b[\w./-]+\.(md|py|json|pptx|ts|tsx|js|yaml|yml|txt|pdf|docx|xlsx)\b")


def r0(entry: Entry) -> str:
    """Shipped: `User: prompt` / `Agent: reply` per turn."""
    return turns_text(entry.turns)


RENDERINGS = {
    "R0": r0,
}


def bank_apart() -> tuple[list[dict], Ledger]:
    root = Path(tempfile.mkdtemp()) / "skillpp"
    config = Config(root)
    config.ensure_dirs()
    config.match_floor = config.match_floor_turns = 2.0   # nothing merges
    rows = recurrence.fold_all(config)
    return rows, Ledger(config)


def replay(rows: list[dict], vec: dict, floor: float) -> list[dict]:
    reps: list[str] = []
    out = []
    for row in rows:
        best = max(((cosine(vec[row["entry"]], vec[r]), r) for r in reps), default=(-1.0, None))
        if best[0] >= floor:
            out.append({**row, "entry": best[1]})
        else:
            reps.append(row["entry"])
            out.append(dict(row))
    return out


def score(name: str, render, rows: list[dict], ledger: Ledger, cache: dict) -> dict:
    vec = {}
    for row in rows:
        text = render(ledger.get(row["entry"])) or "(empty)"
        if text not in cache:
            cache[text] = embed(text)
        vec[row["entry"]] = cache[text]

    fam = {r["entry"]: r["family"] for r in rows}
    ids = [r["entry"] for r in rows]
    danger = max(cosine(vec[a], vec[b]) for i, a in enumerate(ids) for b in ids[i + 1:]
                 if fam[a] != fam[b])

    floor, merged, should, big = None, 0, 0, []
    for step in range(50, 100):
        f = step / 100
        ev = recurrence.evaluate(replay(rows, vec, f))
        if ev["pairs"]["wrong"] == 0:
            out = replay(rows, vec, f)
            sizes = Counter((o["family"], o["entry"]) for o in out)
            floor, merged, should = f, ev["pairs"]["correct"], ev["pairs"]["should_merge"]
            big = sorted({family for (family, _), n in sizes.items() if n >= 3})
            break

    by_tag = {r["episode"].split("#")[0]: r["entry"] for r in rows}
    p = {k: vec[by_tag[t]] for k, t in PRESENTATION.items()}
    q = {k: vec[by_tag[t]] for k, t in POST.items()}
    same = [cosine(p["article"], p["handoff"]), cosine(q["article"], q["handoff"])]
    diff = [cosine(p["article"], q["article"]), cosine(p["handoff"], q["handoff"]),
            cosine(p["article"], q["handoff"]), cosine(p["handoff"], q["article"])]
    return {"name": name, "danger": danger, "floor": floor, "merged": merged,
            "should": should, "margin": (floor - danger) if floor else None,
            "gap": min(same) - max(diff), "same": same, "diff": diff, "big": big}


def main(argv: list[str]) -> int:
    names = argv or list(RENDERINGS)
    unknown = [n for n in names if n not in RENDERINGS]
    if unknown:
        print(f"unknown rendering(s): {unknown}; known: {list(RENDERINGS)}", file=sys.stderr)
        return 1
    rows, ledger = bank_apart()
    sessions = len({r["episode"].split("#")[0] for r in rows})
    print(f"{len(rows)} episodes from {sessions} live sessions\n")
    print(f"{'step':6} {'danger':>7} {'floor':>6} {'merged':>8} {'margin':>7} {'2x2 gap':>8}  3x")
    cache: dict = {}
    for name in names:
        s = score(name, RENDERINGS[name], rows, ledger, cache)
        margin = f"{s['margin']:+.3f}" if s["margin"] is not None else "   n/a"
        print(f"{name:6} {s['danger']:7.3f} {s['floor'] or 0:6.2f} {s['merged']:>3}/{s['should']:<4} "
              f"{margin:>7} {s['gap']:+8.3f}  {', '.join(s['big']) or '-'}")
        print(f"       2x2 same procedure {' '.join(f'{x:.3f}' for x in s['same'])} | "
              f"different {' '.join(f'{x:.3f}' for x in s['diff'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
