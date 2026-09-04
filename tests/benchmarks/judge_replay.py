#!/usr/bin/env python3
"""Score the model boundary judge against the live sessions.

    python3 tests/benchmarks/judge_replay.py            # every live session
    python3 tests/benchmarks/judge_replay.py fb505861   # one

The live sessions in `tests/fixtures/sessions/` were captured before the judge
existed, so their steps carry no verdict and `segment` reads them with the
vocabulary rules. This replays each step through `boundary.judge` — with the
goal and the span behind it, exactly as `judge_in_session` builds them live —
writes the verdicts in, and scores the result against the same hand-written
ground truth `score.py` uses.

It talks to a real local model, so it is slow and it is not part of the unit
suite. That is the point: the unit suite must pass without Ollama running.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fixtures" / "sessions"))

import score as live_score                                    # noqa: E402
from skillpp.boundary import (describe, judge, render_step,   # noqa: E402
                              window)
from skillpp.config import Config                             # noqa: E402
from skillpp.segment import is_prompt                         # noqa: E402


def judged_copy(doc: dict, config: Config, *, verbose: bool = False,
                summarise: bool = False) -> dict:
    """A copy of *doc* whose steps carry the model's verdicts.

    Goal and span come from `boundary.window`, the same one `judge_in_session`
    assembles from the live buffer — every prompt in the current task, and the
    last `CONTEXT_STEPS` steps taken on it.
    """
    # deepcopy, not a json round trip: `score.load` stashes a `Path` on the doc.
    out = copy.deepcopy(doc)
    # Judge against the stream built so far, verdicts included — `window` reads
    # its own earlier answers to find where the current task started, so replay
    # has to grow the list the way the hook does rather than pass the whole
    # session in. Feeding it the finished stream would let a step be judged
    # against boundaries found after it.
    seen: list[dict] = []
    for step in out["steps"]:
        if is_prompt(step):
            seen.append(step)
            continue
        if summarise:
            asks = [str((s.get("input") or {}).get("text", "")).strip()
                    for s in seen if is_prompt(s)]
            note = describe(step, asks=[a for a in asks if a],
                            index=sum(1 for s in seen if not is_prompt(s)) + 1,
                            reply=step.get("tool_returned", ""),
                            model=config.local_model, host=config.ollama_url)
            if note:
                step["summary"] = note
        goal, done = window(seen)
        verdict = judge(step, goal=goal,
                        prior=done, model=config.local_model,
                        host=config.ollama_url, timeout=30.0)
        if verdict is not None:
            step["end"] = verdict
        if verbose and verdict:
            print(f"       end: {render_step(step)[:70]}")
        seen.append(step)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", help="score one session")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every step the model called an ending")
    ap.add_argument("--context", type=int, default=None,
                    help="how many prior steps to show (default boundary.CONTEXT_STEPS)")
    ap.add_argument("--describe", action="store_true",
                    help="also send the developer's description (default: withheld)")
    ap.add_argument("--value-chars", type=int, default=None,
                    help="how much of each leftover input field render_step shows "
                         "(default boundary._VALUE_CHARS). 80 is what clipped an "
                         "Edit's old_string in the session that flipped.")
    ap.add_argument("--skip", action="append", default=[],
                    help="tag to leave out, repeatable. `263d65ce` is a third of "
                         "the corpus by step count and the slowest by far.")
    ap.add_argument("--summarise", action="store_true",
                    help="write a `summary` on each step first, so the judge reads "
                         "sentences instead of raw commands. Slow: adds a model call "
                         "per step. Fixtures were recorded before summaries existed, "
                         "so without this the judge is measured on the old input.")
    args = ap.parse_args(argv)

    import skillpp.boundary as boundary
    if args.context is not None:
        boundary.CONTEXT_STEPS = args.context
    boundary.SEND_DESCRIPTION = args.describe
    if args.value_chars is not None:
        boundary._VALUE_CHARS = args.value_chars
    if args.summarise:
        # Keep the summary whole and let the judge read it — the configuration
        # being measured. Defaults stay where the last measurement left them.
        boundary.SUMMARY_CHARS = 1200
        boundary.JUDGE_READS_SUMMARY = True

    config = Config()
    print(f"value_chars {boundary._VALUE_CHARS}, "
          f"context {boundary.CONTEXT_STEPS} steps, "
          f"description {'sent' if args.describe else 'withheld'}, "
          f"summaries {'generated' if args.summarise else 'absent'}")
    docs = [d for d in live_score.load(args.tag)
            if not any(d["tag"].startswith(t) for t in args.skip)]
    if not docs:
        print("no sessions matched", file=sys.stderr)
        return 1

    print(f"model {config.local_model} at {config.ollama_url}\n")
    worse = better = 0
    for doc in docs:
        before = live_score.check(doc)
        started = time.time()
        after = live_score.check(judged_copy(doc, config, verbose=args.verbose,
                                             summarise=args.summarise))
        steps = sum(1 for s in doc["steps"] if not is_prompt(s))
        took = time.time() - started

        def line(row):
            return (f"episodes {row['episodes']['got']}/{row['episodes']['want']}"
                    f"  kept {'all' if row['kept']['ok'] else 'MISSING ' + str(row['kept']['missing'])}")

        ok_before = before["episodes"]["ok"] and before["kept"]["ok"]
        ok_after = after["episodes"]["ok"] and after["kept"]["ok"]
        verdict = ("FIXED" if ok_after and not ok_before else
                   "BROKE" if ok_before and not ok_after else
                   "ok" if ok_after else "still wrong")
        better += ok_after and not ok_before
        worse += ok_before and not ok_after
        print(f"{verdict:<12} {doc['tag']}  {doc['name']}")
        print(f"       vocabulary  {line(before)}")
        print(f"       judged      {line(after)}")
        print(f"       {steps} steps judged in {took:.1f}s "
              f"({took / max(steps, 1):.2f}s per step)\n")

    print(f"{better} fixed, {worse} broken")
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
