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
* **safe** — the first floor above the danger line (a hundredth above it). The
  floor a change would ship at: every wrong pair scores below it.
* **merged** — correct merges in the replay at the safe floor, of every
  same-procedure pair, and **wrong** merges there (0 by construction, printed
  as a check).
* **luck** — the lowest floor with no wrong merge in the replay. Below *safe*,
  it held only because of the order the sessions were folded in; shown, never
  chosen. (An earlier "margin" column subtracted the danger line from it, and
  read worse exactly when a change pulled same-procedure pairs up.)
* **2x2 gap** — runs 4, 5, 6 and 8: two procedures on two files. Worst
  same-procedure score minus best different-procedure score; positive means the
  text follows the procedure rather than the material.
* **3x** — families with an entry holding three or more runs at the safe floor.

A step on the ladder changes one thing. Nothing here decides; it prints.
"""

from __future__ import annotations

import math
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


def r1(entry: Entry) -> str:
    """R0 with every file name, in prompts and replies, replaced by `<file>`."""
    return FILE.sub("<file>", r0(entry))


LIST_ITEM = re.compile(r"^\s*([-*•+]|\d+[.)])\s+")
BOLD_LINE = re.compile(r"^\s*\*\*[^*].*\*\*:?\s*$")


def strip_deliverable(reply: str) -> str:
    """The reply without what it delivered: the agent's own sentences stay.

    Dropped: text between `---` separators (a drafted post), fenced code,
    table rows, headings, whole-line bold labels (slide titles), and list items
    with any lines indented under them.
    """
    out: list[str] = []
    in_fence = in_rule = in_item = False
    for line in reply.splitlines():
        text = line.strip()
        if text.startswith("```"):
            in_fence = not in_fence
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", text):
            in_rule = not in_rule
            continue
        if in_fence or in_rule:
            continue
        if not text:
            in_item = False
            continue
        if (text.startswith("|") or text.startswith("#") or BOLD_LINE.match(line)
                or LIST_ITEM.match(line) or (in_item and line[:1].isspace())):
            in_item = bool(LIST_ITEM.match(line)) or in_item
            continue
        in_item = False
        out.append(text)
    return "\n".join(out)


def r2(entry: Entry) -> str:
    """R1, with each reply's deliverable blocks removed.

    Measured, not kept: the two-procedures-two-files gap rose to +0.098, but the
    danger line moved to a coding pair (add-eval-case ~ compare-adk-docs, 0.854)
    whose replies carry their topic in plain sentences this cannot see, so the
    safe floor rose to 0.86 and merges fell from 12 to 10.
    """
    turns = [{**t, "reply": strip_deliverable(t.get("reply", ""))} for t in entry.turns]
    return FILE.sub("<file>", turns_text(turns))


RENDERINGS = {
    "R0": r0,
    "R1": r1,
    "R2": r2,
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

    luck = None
    for step in range(50, 100):
        if recurrence.evaluate(replay(rows, vec, step / 100))["pairs"]["wrong"] == 0:
            luck = step / 100
            break
    safe = math.floor(danger * 100) / 100 + 0.01
    out = replay(rows, vec, safe)
    ev = recurrence.evaluate(out)
    sizes = Counter((o["family"], o["entry"]) for o in out)
    big = sorted({family for (family, _), n in sizes.items() if n >= 3})

    by_tag = {r["episode"].split("#")[0]: r["entry"] for r in rows}
    p = {k: vec[by_tag[t]] for k, t in PRESENTATION.items()}
    q = {k: vec[by_tag[t]] for k, t in POST.items()}
    same = [cosine(p["article"], p["handoff"]), cosine(q["article"], q["handoff"])]
    diff = [cosine(p["article"], q["article"]), cosine(p["handoff"], q["handoff"]),
            cosine(p["article"], q["handoff"]), cosine(p["handoff"], q["article"])]
    return {"name": name, "danger": danger, "safe": safe, "luck": luck,
            "merged": ev["pairs"]["correct"], "wrong": ev["pairs"]["wrong"],
            "should": ev["pairs"]["should_merge"],
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
    print(f"{'step':6} {'danger':>7} {'safe':>5} {'merged':>8} {'wrong':>5} {'luck':>5} {'2x2 gap':>8}  3x at safe")
    cache: dict = {}
    for name in names:
        s = score(name, RENDERINGS[name], rows, ledger, cache)
        print(f"{name:6} {s['danger']:7.3f} {s['safe']:5.2f} {s['merged']:>3}/{s['should']:<4} "
              f"{s['wrong']:>5} {s['luck'] or 0:5.2f} {s['gap']:+8.3f}  {', '.join(s['big']) or '-'}")
        print(f"       2x2 same procedure {' '.join(f'{x:.3f}' for x in s['same'])} | "
              f"different {' '.join(f'{x:.3f}' for x in s['diff'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
