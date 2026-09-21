#!/usr/bin/env python3
"""Score the boundary judge against the synthetic corpus.

    python3 tests/benchmarks/judge_cases.py

`run.play` drives each case through the real hook handlers, so the judge fires
exactly as it would live. Three configurations over the same 24 cases:

* **vocabulary** — the judge stubbed out, so `segment` falls back to
  `is_marker` and the prompt rule. The number to beat.
* **judged, lean** — the model decides, steps rendered without the developer's
  `description`.
* **judged, full** — the model decides, with the description.

The corpus measures itself: `tests/fixtures/sessions/README.md` says it "was
written by whoever was also writing the detector, so it measures internal
consistency". Read it next to the live-session numbers, not instead of them.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import skillpp.boundary as boundary                      # noqa: E402
from cases import CASES                                  # noqa: E402
from run import score                                    # noqa: E402

_REAL_JUDGE = boundary.judge_in_session

# Only the segmenter is under test here; two cases are documented as failing for
# reasons downstream of it, and `run.score` reports them either way.
DOWNSTREAM = {"deploy-then-status-email"}


def run(label: str, *, judge: bool, describe: bool) -> dict:
    boundary.judge_in_session = (_REAL_JUDGE if judge
                                 else (lambda config, session, step: None))
    boundary.SEND_DESCRIPTION = describe
    started = time.time()
    result = score(CASES, use_model=False)
    took = time.time() - started
    seg = result["segmentation"]
    failing = sorted(r["name"] for r in result["rows"] if not r["segmentation"])
    print(f"\n{label}")
    print(f"  segmentation {seg['passed']}/{seg['of']}   {took:.0f}s")
    for row in result["rows"]:
        if not row["segmentation"]:
            print(f"    {row['name']:<34} want {row['expected_episodes']}"
                  f"  got {row['got_episodes']}   {','.join(row['tags'])}")
    return {"label": label, "passed": seg["passed"], "of": seg["of"],
            "failing": set(failing)}


def main() -> int:
    runs = [run("vocabulary (no model)", judge=False, describe=False),
            run("judged, lean steps", judge=True, describe=False),
            run("judged, full steps (with description)", judge=True,
                describe=True)]
    boundary.judge_in_session = _REAL_JUDGE
    boundary.SEND_DESCRIPTION = False

    base = runs[0]
    print("\n" + "=" * 62)
    for entry in runs:
        delta = ""
        if entry is not base:
            fixed = base["failing"] - entry["failing"]
            broke = entry["failing"] - base["failing"]
            delta = f"   fixed {sorted(fixed)}  broke {sorted(broke)}"
        print(f"{entry['label']:<40} {entry['passed']}/{entry['of']}{delta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
