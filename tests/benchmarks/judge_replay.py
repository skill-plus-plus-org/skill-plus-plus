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
import skillpp.boundary as boundary                           # noqa: E402
from skillpp.boundary import (describe, gaps, judge,          # noqa: E402
                              render_step, said_text, window)
from skillpp.config import Config                             # noqa: E402
from skillpp.local import LocalModelUnavailable, ask          # noqa: E402
from skillpp.segment import is_prompt                         # noqa: E402


def require_model(config: Config) -> None:
    """Refuse to run against a model that is not there.

    Without this the harness scores a green `ok` with `0.00s per step` when
    Ollama is down: every `judge` call returns None, no verdict is written,
    `was_judged` stays false, and the "judged" row is the vocabulary row printed
    twice. It treats "the judge said nothing" and "the judge said no" as the
    same state, which is the difference between a measurement and a blank.
    """
    try:
        ask(config.local_model, "say ok", host=config.ollama_url, timeout=180.0)
    except LocalModelUnavailable as exc:
        raise SystemExit(
            f"no model at {config.ollama_url}: {exc}\n"
            f"start it with `ollama serve` and make sure "
            f"`{config.local_model}` is pulled. Refusing to run: a replay "
            f"without verdicts scores the vocabulary and calls it the judge.")


def judged_copy(doc: dict, config: Config, *, verbose: bool = False,
                summarise: bool = False) -> dict:
    """A copy of *doc* whose steps carry the model's verdicts.

    One question per prompt gap, exactly as `boundary.judge_session` asks it at
    `SessionEnd` — the goal, the last `PRIOR_STEPS` steps, the instruction that
    landed in the gap, and the `NEXT_STEPS` steps that followed it.

    The per-step judge this replaced was replayed forward, growing the stream
    the way the hook did. This question needs what came *after* a gap, so there
    is no forward-only version of it — which is why the live hook also runs at
    session end rather than per tool call.
    """
    # deepcopy, not a json round trip: `score.load` stashes a `Path` on the doc.
    out = copy.deepcopy(doc)
    steps = out["steps"]
    for step in steps:
        if not is_prompt(step):
            step["end"] = False

    if summarise:
        seen: list[dict] = []
        for step in steps:
            if is_prompt(step):
                seen.append(step)
                continue
            asks = [str((s.get("input") or {}).get("text", "")).strip()
                    for s in seen if is_prompt(s)]
            note = describe(step, asks=[a for a in asks if a],
                            index=sum(1 for s in seen if not is_prompt(s)) + 1,
                            reply=step.get("tool_returned", ""),
                            model=config.local_model, host=config.ollama_url)
            if note:
                step["summary"] = note
            seen.append(step)

    asked = answered = 0
    for index, said, follow in gaps(steps):
        goal, prior = window(steps[:index])
        asked += 1
        verdict = judge(steps[index], goal=goal,
                        prior=prior[-boundary.PRIOR_STEPS:],
                        said=said_text(said),
                        follow=[render_step(s) for s in follow],
                        model=config.local_model, host=config.ollama_url,
                        timeout=30.0)
        if verdict is None:
            steps[index].pop("end", None)
            continue
        answered += 1
        steps[index]["end"] = verdict
        if verbose and verdict:
            print(f"       new job after: {render_step(steps[index])[:58]}")
            print(f"          they said: "
                  f"{str((said.get('input') or {}).get('text', ''))[:58]!r}")
    if asked and not answered:
        raise SystemExit(
            f"{out['tag']}: the model answered none of {asked} questions. That "
            f"is a blank, not a result — nothing was measured.")
    out["_answered"] = answered
    out["_asked"] = asked
    return out


def save_verdicts(doc: dict, judged: dict, config: Config, boundary) -> None:
    """Write the model's verdicts into the fixture on disk.

    Only `end` (and `summary`, when one was generated). The `truth` block is
    never touched: rewriting a golden answer because the pipeline changed is how
    the synthetic corpus started measuring itself.

    A `judged` block records which model and configuration produced these, so a
    stale set is visible rather than silently scored. Re-run this whenever the
    judge prompt or its flags change.
    """
    path = doc["_path"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    for stored, replayed in zip(on_disk["steps"], judged["steps"]):
        if "end" in replayed:
            stored["end"] = replayed["end"]
        else:
            stored.pop("end", None)
        if replayed.get("summary"):
            stored["summary"] = replayed["summary"]
    on_disk["judged"] = {
        "model": config.local_model,
        "prior_steps": boundary.PRIOR_STEPS,
        "next_steps": boundary.NEXT_STEPS,
        "value_chars": boundary._VALUE_CHARS,
        "send_description": boundary.SEND_DESCRIPTION,
        "endings": sum(1 for s in judged["steps"] if s.get("end") is True),
        "answered": judged.get("_answered", 0),
        "replayed": __import__("datetime").date.today().isoformat(),
    }
    path.write_text(json.dumps(on_disk, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", help="score one session")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every step the model called an ending")
    ap.add_argument("--prior", type=int, default=None,
                    help="steps of history to show (default boundary.PRIOR_STEPS). "
                         "20 was the old default and scored worse: a long history "
                         "made every late gap read as a continuation.")
    ap.add_argument("--next", dest="next_steps", type=int, default=None,
                    help="steps after the gap to show (default boundary.NEXT_STEPS). "
                         "A window, not a knob: 1 scores 7/11, 3 scores 10/11, "
                         "5 scores 8/11.")
    ap.add_argument("--describe", action="store_true",
                    help="also send the developer's description (default: withheld)")
    ap.add_argument("--value-chars", type=int, default=None,
                    help="how much of each leftover input field render_step shows "
                         "(default boundary._VALUE_CHARS). 80 is what clipped an "
                         "Edit's old_string in the session that flipped.")
    ap.add_argument("--skip", action="append", default=[],
                    help="tag to leave out, repeatable. `263d65ce` is a third of "
                         "the corpus by step count and the slowest by far.")
    ap.add_argument("--write", action="store_true",
                    help="save the verdicts back into each fixture. The live "
                         "sessions are projections of Claude Code transcripts, "
                         "which carry no `end` field, so without this the "
                         "corpus is unjudged by construction and cannot "
                         "represent a pipeline that requires verdicts.")
    ap.add_argument("--summarise", action="store_true",
                    help="write a `summary` on each step first, so the judge reads "
                         "sentences instead of raw commands. Slow: adds a model call "
                         "per step. Fixtures were recorded before summaries existed, "
                         "so without this the judge is measured on the old input.")
    args = ap.parse_args(argv)

    if args.prior is not None:
        boundary.PRIOR_STEPS = args.prior
    if args.next_steps is not None:
        boundary.NEXT_STEPS = args.next_steps
    boundary.SEND_DESCRIPTION = args.describe
    if args.value_chars is not None:
        boundary._VALUE_CHARS = args.value_chars
    if args.summarise:
        # Keep the summary whole and let the judge read it — the configuration
        # being measured. Defaults stay where the last measurement left them.
        boundary.SUMMARY_CHARS = 1200
        boundary.JUDGE_READS_SUMMARY = True

    config = Config()
    require_model(config)
    print(f"value_chars {boundary._VALUE_CHARS}, "
          f"prior {boundary.PRIOR_STEPS} steps, "
          f"next {boundary.NEXT_STEPS} steps, "
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
        after_doc = judged_copy(doc, config, verbose=args.verbose,
                                summarise=args.summarise)
        after = live_score.check(after_doc)
        steps = sum(1 for s in doc["steps"] if not is_prompt(s))
        took = time.time() - started

        def line(row):
            return (f"episodes {row['episodes']['got']}/{row['episodes']['want']}"
                    f"  kept {'all' if row['kept']['ok'] else 'MISSING ' + str(row['kept']['missing'])}")

        if args.write:
            save_verdicts(doc, after_doc, config, boundary)

        ok_before = before["episodes"]["ok"] and before["kept"]["ok"]
        ok_after = after["episodes"]["ok"] and after["kept"]["ok"]
        verdict = ("FIXED" if ok_after and not ok_before else
                   "BROKE" if ok_before and not ok_after else
                   "ok" if ok_after else "still wrong")
        better += ok_after and not ok_before
        worse += ok_before and not ok_after
        print(f"{verdict:<12} {doc['tag']}  {doc['name']}")
        print(f"       as stored   {line(before)}")
        print(f"       replayed    {line(after)}")
        asked = after_doc.get("_asked", 0)
        print(f"       {asked} question(s) over {steps} steps in {took:.1f}s\n")

    print(f"{better} fixed, {worse} broken")
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
