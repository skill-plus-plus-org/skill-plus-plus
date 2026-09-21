#!/usr/bin/env python3
"""Table a set of `judge_replay.py --dump` files, one row per run.

    python3 tests/benchmarks/judge_table.py DIR [BASELINE.json]

Read at the gap, not the session: only 2 of the 45 gaps in the live sessions
are real boundaries, and a judge that answers "no" everywhere scores 19/21. So
each row says how many real boundaries were caught, how many false cuts were
made and where, and which gaps changed against the baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verdicts(doc: dict) -> dict:
    return {(g["tag"], g["work"]): g["verdict"] for g in doc["gaps"]}


def main(argv: list[str]) -> int:
    folder = Path(argv[0])
    base_path = Path(argv[1]) if len(argv) > 1 else folder / "B0.json"
    base = verdicts(load(base_path)) if base_path.exists() else {}
    rows = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime):
        doc = load(path)
        s = doc["summary"]
        now = verdicts(doc)
        flipped = sorted(k for k in now if k in base and now[k] != base[k])
        false_cuts = sorted(f"{g['tag']}@{g['work']}" for g in doc["gaps"]
                            if not g["label"] and g["verdict"] is True)
        rows.append((path.stem, s, flipped, false_cuts))

    print(f"{'setting':<18} {'ok':>5} {'caught':>6} {'false':>5} {'flips':>5} "
          f"{'p50 s':>6} {'chars':>11} {'ctx':>11} {'think':>6}  false cuts")
    for name, s, flipped, false_cuts in rows:
        valid = "" if s["valid"] else "  INVALID"
        print(f"{name:<18} {s['sessions_ok']:>2}/{s['sessions']:<2} "
              f"{s['true_boundaries_caught']:>3}/{s['true_boundaries']:<2} "
              f"{s['false_cuts']:>5} {len(flipped):>5} {s['p50_seconds']:>6} "
              f"{s['prompt_chars_median']:>5}/{s['prompt_chars_max']:<5} "
              f"{','.join(str(c) for c in s['num_ctx']):>11} "
              f"{s['thinking_chars_median']:>6}  {' '.join(false_cuts)}{valid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
