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
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fixtures" / "sessions"))

import score as live_score                                    # noqa: E402
import skillpp.boundary as boundary                           # noqa: E402
from skillpp.boundary import describe, gaps, said_text        # noqa: E402
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
        ask(config.local_model, "say ok", host=config.ollama_url, timeout=180.0,
            think=False)
    except LocalModelUnavailable as exc:
        raise SystemExit(
            f"no model at {config.ollama_url}: {exc}\n"
            f"start it with `ollama serve` and make sure "
            f"`{config.local_model}` is pulled. Refusing to run: a replay "
            f"without verdicts scores the vocabulary and calls it the judge.")


def _yn(value) -> str:
    return {True: "yes", False: "no ", None: " — "}[value]


def judged_copy(doc: dict, config: Config, *, verbose: bool = False,
                summarise: bool = False, gap_log: list | None = None) -> dict:
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

    # Truth is pinned per session as the work steps after which a new task
    # starts (`boundary_after`, 1-based over non-prompt steps); every other gap
    # is labelled "not a boundary".
    truth_at = set(doc["truth"].get("boundary_after") or [])
    work = {i: n for n, i in enumerate(
        (i for i, s in enumerate(steps) if not is_prompt(s)), 1)}
    asked = answered = 0
    for index, said, follow in gaps(steps):
        asked += 1
        meta: dict = {}
        verdict = boundary.judge_gap(steps, index, said, follow,
                                     model=config.local_model,
                                     host=config.ollama_url, meta=meta)
        record = {"tag": doc["tag"], "work": work[index],
                  "label": work[index] in truth_at,
                  "stored": doc["steps"][index].get("end"),
                  "verdict": verdict,
                  "said": said_text(said)[:140], **meta}
        if gap_log is not None:
            gap_log.append(record)
        if verbose:
            mark = ("ok " if verdict == record["label"] else
                    "-- " if verdict is None else "XX ")
            kind = "BOUNDARY" if record["label"] else "        "
            print(f"   {mark} {kind} step {work[index]:>3}  stored "
                  f"{_yn(record['stored'])}  now {_yn(verdict)}"
                  f"  {record['said'][:60]!r}")
        if verdict is None:
            steps[index].pop("end", None)
            continue
        answered += 1
        steps[index]["end"] = verdict
    # Not raised here: `main` refuses the whole run if *any* gap went
    # unanswered. A timeout reads as "no", which is exactly the answer being
    # measured, so one silent gap is enough to make a run worthless.
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


def _chars(value: str) -> int:
    """A character limit: a number, or `full` for all of it."""
    return boundary.FULL if value == "full" else int(value)


def _steps(value: str) -> int:
    """A step count: a number, or `all` for the whole span."""
    return 10 ** 9 if value == "all" else int(value)


def _resident(config: Config) -> list[dict]:
    try:
        with urllib.request.urlopen(f"{config.ollama_url}/api/ps", timeout=10) as r:
            return [{"name": m["name"], "gb": round(m["size"] / 1e9, 1)}
                    for m in json.loads(r.read()).get("models", [])]
    except OSError:
        return []


def _swap() -> str:
    try:
        return subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _git() -> str:
    try:
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "skillpp"],
                               capture_output=True, text=True).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", nargs="?", help="score one session")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every gap: its true label, the stored "
                         "verdict and this run's")
    ap.add_argument("--dump", help="write every gap's verdict and cost as JSON")
    ap.add_argument("--prior", type=_steps, default=None,
                    help="steps of history to show, or `all` (default "
                         "boundary.PRIOR_STEPS). 20 was the old default and "
                         "scored worse on gemma3n: a long history made every "
                         "late gap read as a continuation.")
    ap.add_argument("--next", dest="next_steps", type=int, default=None,
                    help="steps after the gap to show (default boundary.NEXT_STEPS). "
                         "On gemma3n, 1 scored 7/11, 3 scored 10/11, 5 scored 8/11.")
    ap.add_argument("--prompt-chars", type=_chars, default=None,
                    help="how much of the developer's instruction to show, or "
                         "`full` (default boundary._PROMPT_CHARS)")
    ap.add_argument("--value-chars", type=_chars, default=None,
                    help="how much of each leftover input field render_step shows, "
                         "or `full` (default boundary._VALUE_CHARS)")
    ap.add_argument("--reply-before", type=_chars, default=None,
                    help="the tail of the assistant's last reply before the gap, "
                         "in characters or `full` (default: not shown)")
    ap.add_argument("--reply-after", type=_chars, default=None,
                    help="the head of the assistant's reply to the new "
                         "instruction, in characters or `full` (default: not shown)")
    ap.add_argument("--step-output", type=_chars, default=None,
                    help="what the step before the gap returned, in characters "
                         "or `full` (default: not shown)")
    ap.add_argument("--no-next", action="store_true",
                    help="leave out the steps after the gap entirely")
    ap.add_argument("--next-label", default=None,
                    help="the line that introduces the steps after the gap "
                         "(default boundary.NEXT_LABEL)")
    ap.add_argument("--think", action="store_true",
                    help="let the judge reason before answering (default: off)")
    ap.add_argument("--describe", action="store_true",
                    help="also send the developer's description (default: withheld)")
    ap.add_argument("--skip", action="append", default=[],
                    help="tag to leave out, repeatable.")
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
    if args.prompt_chars is not None:
        boundary._PROMPT_CHARS = (10 ** 9 if args.prompt_chars == boundary.FULL
                                  else args.prompt_chars)
    if args.value_chars is not None:
        boundary._VALUE_CHARS = (10 ** 9 if args.value_chars == boundary.FULL
                                 else args.value_chars)
    if args.reply_before is not None:
        boundary.REPLY_BEFORE_CHARS = args.reply_before
    if args.reply_after is not None:
        boundary.REPLY_AFTER_CHARS = args.reply_after
    if args.step_output is not None:
        boundary.STEP_OUTPUT_CHARS = args.step_output
    boundary.JUDGE_THINKS = args.think
    boundary.SHOW_NEXT = not args.no_next
    if args.next_label is not None:
        boundary.NEXT_LABEL = args.next_label
    boundary.SEND_DESCRIPTION = args.describe
    if args.summarise:
        # Keep the summary whole and let the judge read it — the configuration
        # being measured. Defaults stay where the last measurement left them.
        boundary.SUMMARY_CHARS = 1200
        boundary.JUDGE_READS_SUMMARY = True

    knobs = {"prior": boundary.PRIOR_STEPS, "next": boundary.NEXT_STEPS,
             "prompt_chars": boundary._PROMPT_CHARS,
             "value_chars": boundary._VALUE_CHARS,
             "reply_before": boundary.REPLY_BEFORE_CHARS,
             "reply_after": boundary.REPLY_AFTER_CHARS,
             "step_output": boundary.STEP_OUTPUT_CHARS,
             "think": boundary.JUDGE_THINKS,
             "show_next": boundary.SHOW_NEXT, "next_label": boundary.NEXT_LABEL,
             "describe": boundary.SEND_DESCRIPTION, "summarise": args.summarise}

    config = Config()
    require_model(config)
    print("  ".join(f"{k} {v}" for k, v in knobs.items()))
    docs = [d for d in live_score.load(args.tag)
            if not any(d["tag"].startswith(t) for t in args.skip)]
    if not docs:
        print("no sessions matched", file=sys.stderr)
        return 1

    print(f"model {config.local_model} at {config.ollama_url}\n")
    resident_before, swap_before, started_run = _resident(config), _swap(), time.time()
    gap_log: list[dict] = []
    sessions = []
    worse = better = 0
    for doc in docs:
        before = live_score.check(doc)
        started = time.time()
        after_doc = judged_copy(doc, config, verbose=args.verbose,
                                summarise=args.summarise, gap_log=gap_log)
        after = live_score.check(after_doc)
        steps = sum(1 for s in doc["steps"] if not is_prompt(s))
        took = time.time() - started

        def line(row):
            return (f"episodes {row['episodes']['got']}/{row['episodes']['want']}"
                    f"  kept {'all' if row['kept']['ok'] else 'MISSING ' + str(row['kept']['missing'])}"
                    + ("" if row["boundary"]["n/a"] else
                       f"  cut after {row['boundary']['got']} "
                       f"(want {row['boundary']['want']})"))

        if args.write:
            save_verdicts(doc, after_doc, config, boundary)

        # Placement as well as count: a two-task session cut in the wrong place
        # still banks two episodes, and used to score as fixed.
        def right(row):
            return row["episodes"]["ok"] and row["kept"]["ok"] and row["boundary"]["ok"]

        ok_before, ok_after = right(before), right(after)
        verdict = ("FIXED" if ok_after and not ok_before else
                   "BROKE" if ok_before and not ok_after else
                   "ok" if ok_after else "still wrong")
        better += ok_after and not ok_before
        worse += ok_before and not ok_after
        sessions.append({"tag": doc["tag"], "ok": ok_after, "was_ok": ok_before,
                         "verdict": verdict})
        print(f"{verdict:<12} {doc['tag']}  {doc['name']}")
        print(f"       as stored   {line(before)}")
        print(f"       replayed    {line(after)}")
        asked = after_doc.get("_asked", 0)
        print(f"       {asked} question(s) over {steps} steps in {took:.1f}s\n")

    unanswered = [g for g in gap_log if g["verdict"] is None]
    seconds = [g.get("seconds") or 0 for g in gap_log]
    chars = [g.get("prompt_chars") or 0 for g in gap_log]
    thinking = [g.get("thinking_chars") or 0 for g in gap_log]
    summary = {
        "valid": not unanswered,
        "sessions_ok": sum(s["ok"] for s in sessions), "sessions": len(sessions),
        "true_boundaries_caught": sum(1 for g in gap_log if g["label"] and g["verdict"] is True),
        "true_boundaries": sum(1 for g in gap_log if g["label"]),
        "false_cuts": sum(1 for g in gap_log if not g["label"] and g["verdict"] is True),
        "not_boundaries": sum(1 for g in gap_log if not g["label"]),
        "unanswered": len(unanswered),
        "unclean": sum(1 for g in gap_log if g.get("unclean")),
        "judge_seconds": round(sum(seconds), 1),
        "p50_seconds": round(statistics.median(seconds), 2) if seconds else 0,
        "p95_seconds": round(sorted(seconds)[max(0, int(len(seconds) * .95) - 1)], 2) if seconds else 0,
        "prompt_chars_median": int(statistics.median(chars)) if chars else 0,
        "prompt_chars_max": max(chars, default=0),
        "num_ctx": sorted({g.get("num_ctx") for g in gap_log if g.get("num_ctx")}),
        "thinking_chars_median": int(statistics.median(thinking)) if thinking else 0,
        "thinking_chars_max": max(thinking, default=0),
        "reloads": sum(1 for g in gap_log if (g.get("load_seconds") or 0) > 0.5),
        "fixed": better, "broken": worse,
    }
    print(f"{better} fixed, {worse} broken")
    print(f"sessions {summary['sessions_ok']}/{summary['sessions']}   "
          f"boundaries caught {summary['true_boundaries_caught']}/{summary['true_boundaries']}   "
          f"false cuts {summary['false_cuts']}/{summary['not_boundaries']}   "
          f"judge {summary['judge_seconds']}s (p50 {summary['p50_seconds']}s)")
    if unanswered:
        print(f"\nINVALID: {len(unanswered)} gap(s) got no verdict "
              f"({summary['unclean']} unclean). A missing verdict reads as "
              f"\"no\" — the answer being measured — so this run proves nothing.",
              file=sys.stderr)
    resident_after = _resident(config)
    foreign = [m["name"] for m in resident_after
               if m["name"] not in (config.local_model, config.embed_model)
               and not m["name"].startswith(config.embed_model + ":")]
    if foreign:
        print(f"WARNING: other models were resident: {foreign}", file=sys.stderr)
    if args.dump:
        Path(args.dump).write_text(json.dumps({
            "model": config.local_model, "knobs": knobs, "git": _git(),
            "template": (boundary.PROMPTS / "new_job.md").read_text(encoding="utf-8"),
            "wall_seconds": round(time.time() - started_run, 1),
            "resident_before": resident_before, "resident_after": resident_after,
            "swap_before": swap_before, "swap_after": _swap(),
            "summary": summary, "sessions": sessions, "gaps": gap_log,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.dump}")
    if unanswered:
        return 2
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
