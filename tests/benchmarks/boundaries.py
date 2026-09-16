#!/usr/bin/env python3
"""Ground truth for *where a task ended*, taken from real transcripts.

    python3 tests/benchmarks/boundaries.py dump 058e7e50 --start 100 --count 60
    python3 tests/benchmarks/boundaries.py score

Why this exists. Every boundary measurement so far used "a step whose command
contains `git commit`" as truth, because it is the only label available for
free. That yardstick is wrong in both directions: it misses endings that never
touched version control — a file handed over, an eval finished, a document sent
— and it counts a mid-task checkpoint commit as an ending. Measured against it,
a model scored precision 0.29, and reading the twenty-two "false positives"
showed several were arguable endings. A bad model and a bad yardstick look
identical from the outside, so the yardstick goes first.

``dump`` writes a reviewable file: every step rendered as the pipeline renders
it, interleaved with what the agent said around it. That surrounding text is the
evidence — a transcript states completion in plain language ("Committed as
`0728ac0`, tree clean") where the command stream only implies it.

A label is an index: the step *after which* a task ended. Recording the reason
alongside is not decoration — a label with no stated evidence cannot be argued
with later, and this file exists precisely because the last yardstick could not
be argued with.

``score`` compares any segmenter's cuts against the labels, with a tolerance:
a boundary one step early is a different error from one twenty steps away, and
scoring exact-match only would hide that.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skillpp.episode import render_step  # noqa: E402
from skillpp.segment import PROMPT_TOOL, segment  # noqa: E402

LABELS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "boundaries"
EPISODES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "episodes"
HOLDOUT_LOG = EPISODES_DIR / "holdout-runs.jsonl"

# Steps that are not the work. Mirrors `capture._NOISE_TOOLS` rather than
# importing it, because this file measures that module and should not silently
# track a change to it.
NOISE = {"Read", "Glob", "Grep", "TodoWrite", "Task", "WebFetch", "WebSearch",
         PROMPT_TOOL}

# A session leans on commands or on documents and tools. Bash share separates
# the two cleanly; MCP share does not — the Stitch design sessions sit at 8%
# MCP and would be filed as command-heavy by any MCP threshold, which is how a
# first attempt at this quietly rebuilt the same dev-heavy sample it was meant
# to avoid.
COMMAND_LEANING_AT = 0.55

# What `capture._KEEP_INPUT` keeps, so a replay sees exactly what a hook would.
KEEP = {"Bash": ("command", "description"), "Write": ("file_path",),
        "Edit": ("file_path",), "NotebookEdit": ("file_path",),
        # Matches `capture._KEEP_INPUT`. It did not, once: this table added the
        # path so a narration would not render `Read()` with nothing in it, and
        # asserted capture should not keep it. That divergence quietly made
        # every fixture built here unfaithful to what capture stores, and the
        # one rule that reads the field — `_substantive`'s read-feeds-a-write —
        # fired in fixtures and never in production. Keep the two tables equal.
        "Read": ("file_path",)}
ENVELOPE = ("<task-notification", "<system-reminder", "<local-command",
            "<command-name", "<command-message", "<!-- attach")


def load_session(tag: str) -> list[dict]:
    """Replay a stored transcript into the step stream the buffer would hold.

    Assistant text and thinking ride along on the step they precede, under keys
    the pipeline ignores. They are the evidence a labeller reads and must never
    reach a segmenter — hence the leading underscore, and hence `strip`.
    """
    matches = glob.glob(f"{Path.home()}/.claude/projects/*/{tag}*.jsonl")
    if not matches:
        raise SystemExit(f"no transcript matching {tag}")
    rows = [json.loads(line) for line in open(matches[0], errors="ignore")
            if line.strip()]

    failures: dict[str, bool] = {}
    for row in rows:
        message = row.get("message")
        if (row.get("type") == "user" and isinstance(message, dict)
                and isinstance(message.get("content"), list)):
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    failures[block.get("tool_use_id")] = bool(block.get("is_error"))

    steps: list[dict] = []
    pending: list[str] = []
    for row in rows:
        message = row.get("message")
        kind = row.get("type")
        if kind == "user" and isinstance(message, dict) and isinstance(
                message.get("content"), str):
            text = message["content"].strip()
            if text and not text.startswith(ENVELOPE):
                steps.append({"tool": PROMPT_TOOL, "input": {"text": text},
                              "failed": False, "_said": pending[:]})
                pending = []
            continue
        if kind != "assistant" or not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            btype = block.get("type")
            if btype in ("text", "thinking") and (block.get(btype) or "").strip():
                pending.append(block[btype])
            elif btype == "tool_use":
                name = block["name"]
                raw = block.get("input") or {}
                keep = KEEP.get(name)
                kept = ({k: raw[k] for k in keep if raw.get(k) is not None}
                        if keep else {})
                # What the edit actually did. `_KEEP_INPUT` drops it, so a
                # rendered `Edit path/to/file.md` says nothing about the change
                # — which makes an episode unreadable to a person asked to
                # judge it. Underscored, so `strip` removes it before anything
                # in the pipeline can see it.
                change = ""
                if name in ("Edit", "NotebookEdit"):
                    before = " ".join(str(raw.get("old_string", "")).split())[:90]
                    after = " ".join(str(raw.get("new_string", "")).split())[:90]
                    if before or after:
                        change = f"{before or '(new)'}  ->  {after}"
                elif name == "Write":
                    body = " ".join(str(raw.get("content", "")).split())
                    change = f"wrote {len(body)} chars: {body[:90]}"
                steps.append({"tool": name, "input": kept,
                              "failed": failures.get(block.get("id"), False),
                              "_said": pending[:], "_change": change})
                pending = []
        if pending and steps:
            steps[-1].setdefault("_after", []).extend(pending)
            pending = []
    return steps


def strip(steps: list[dict]) -> list[dict]:
    """The same steps with the evidence removed — what a segmenter may see."""
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in steps]


def cmd_dump(args: argparse.Namespace) -> int:
    steps = load_session(args.tag)
    end = min(len(steps), args.start + args.count)
    out = [f"# Boundary labelling — {args.tag}, steps {args.start}..{end - 1}",
           "",
           "A label is the index of the step **after which** a task ended.",
           "Read the agent's own words: they usually say so outright.",
           ""]
    for index in range(args.start, end):
        step = steps[index]
        rendered = render_step(step).replace("\n", "\n      ")
        out.append(f"[{index:>4}] {rendered[:400]}")
        # What the agent said *after* running it. Text and tool calls usually
        # arrive in separate messages, so this is also the reasoning that
        # precedes the next step — and it is where completion gets stated
        # outright, which is the whole point of reading a transcript instead of
        # a command log.
        for said in step.get("_said", []) + step.get("_after", []):
            body = " ".join(said.split())
            if body:
                out.append(f"         | {body[:500]}")
        out.append("")
    path = Path(args.out) if args.out else Path(f"boundaries-{args.tag}.md")
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"{end - args.start} steps -> {path}")
    return 0


# -- sampling real episodes -------------------------------------------------

def eligible_transcripts(older_than_days: int = 14, min_kb: int = 50,
                         min_tools: int = 20) -> list[dict]:
    """Transcripts old enough to measure against, profiled by what they contain.

    The age filter is the load-bearing part. Of the fifteen transcripts every
    threshold in this pipeline was calibrated against, fourteen are less than
    two weeks old — so requiring a gap excludes almost exactly the material the
    tuning saw, which is a better guard than anyone's memory of what they read.
    """
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=older_than_days)).timestamp()
    out = []
    for path in glob.glob(f"{Path.home()}/.claude/projects/*/*.jsonl"):
        if os.path.getsize(path) // 1024 < min_kb:
            continue
        if os.path.getmtime(path) >= cutoff:
            continue
        tools = collections.Counter()
        for line in open(path, errors="ignore"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = row.get("message")
            if row.get("type") != "assistant" or not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if block.get("type") == "tool_use":
                    tools[block["name"]] += 1
        total = sum(tools.values())
        if total < min_tools:
            continue
        out.append({"path": path, "tag": os.path.basename(path)[:8],
                    "tools": total, "bash_share": tools["Bash"] / total,
                    "kind": ("command" if tools["Bash"] / total >= COMMAND_LEANING_AT
                             else "document")})
    return sorted(out, key=lambda t: t["tag"])


def _episodes_of(entry: dict) -> list[dict]:
    """Every bankable episode in one transcript, with its evidence attached."""
    raw = load_session(entry["tag"])
    # Segment the *un-stripped* stream. `segment` reads only `tool`, `input`
    # and `failed`, so the `_after` evidence rides along untouched — and each
    # episode then carries the words spoken inside its own span. Segmenting the
    # stripped copy instead gave every episode in a transcript the same
    # evidence, which is no evidence at all.
    out = []
    for index, episode in enumerate(segment(raw, 2)):
        work = [s for s in episode.steps if s["tool"] not in NOISE]
        if len(work) < 2:
            continue
        said = [" ".join(t.split())
                for step in episode.steps for t in (step.get("_after") or [])]
        asked = next((" ".join(str((s.get("input") or {}).get("text", "")).split())
                      for s in episode.steps if s["tool"] == PROMPT_TOOL), "")
        # What the agent said *before* the first step: the plan, which is what
        # makes the steps legible. Reading only the closing summary asks someone
        # to judge a process from its receipt.
        plan = next((" ".join(t.split())
                     for s in episode.steps for t in (s.get("_said") or [])), "")
        out.append({"transcript": entry["tag"], "kind": entry["kind"],
                    "index": index, "steps": [dict(w) for w in work],
                    "narrate": [s for s in episode.steps
                                if s["tool"] != PROMPT_TOOL],
                    "asked": asked[:400], "plan": plan[:400],
                    "evidence": said[-1][:400] if said else ""})
    return out


def _narrate(steps: list[dict]) -> list[str]:
    """One readable line per step, with the agent's own words under it."""
    out = []
    for step in steps:
        line = render_step(step).replace("\n", " / ")
        if step.get("_change"):
            line += f"   [{step['_change']}]"
        out.append(line)
        for text in (step.get("_after") or []):
            body = " ".join(text.split())
            if body:
                out.append(f"      -> {body[:300]}")
    return out


def draft_labels(episode: dict) -> dict:
    """A first opinion, with the evidence it rests on. Never the final word.

    Drafted by the same author as the pipeline being measured, which is the
    conflict `decisions.py` warns about — so a draft that cannot quote the
    transcript is marked low confidence rather than asserted. Adjudication
    turns `verdict` from `null` into an answer.
    """
    work = episode["steps"]
    tools = {s["tool"] for s in work}
    writes = sum(1 for s in work
                 if s["tool"] in ("Edit", "Write", "NotebookEdit")
                 or (s["tool"].startswith("mcp__")
                     and not s["tool"].split("__")[-1].lower().startswith(
                         ("search", "list", "get", "read", "fetch"))))
    landed = bool(re.search(r"(?i)committed|pushed|merged|done\b|shipped",
                            episode.get("evidence", "")))
    method = writes >= 1 and len(work) >= 3
    return {"one_task": {"draft": True, "verdict": None},
            "method": {"draft": method, "verdict": None},
            "confidence": "high" if (landed and writes) else "low",
            "why": (f"{len(work)} steps, {writes} of them change something; "
                    f"tools {sorted(tools)[:4]}")}


def cmd_sample(args: argparse.Namespace) -> int:
    pool = eligible_transcripts(args.older_than)
    by_kind = collections.defaultdict(list)
    for t in pool:
        by_kind[t["kind"]].append(t)
    print(f"eligible: {len(pool)} transcripts "
          f"({len(by_kind['document'])} document, {len(by_kind['command'])} command)")

    # Weighted toward document sessions: the tuning was done on command-heavy
    # ones, so that is where an unseen failure is most likely to be hiding.
    want = {"document": round(args.count * 0.64), "command": args.count
            - round(args.count * 0.64)}
    rng = random.Random(args.seed)
    picked = []
    for kind, target in want.items():
        candidates = []
        for t in by_kind[kind]:
            candidates.extend(_episodes_of(t))
        rng.shuffle(candidates)
        picked.extend(candidates[:target])
        print(f"  {kind:<9} {len(candidates):>4} episodes available -> took {min(target, len(candidates))}")

    rng.shuffle(picked)
    holdout = round(len(picked) * 0.4)
    batch = []
    for i, ep in enumerate(picked):
        batch.append({
            "id": f"{ep['transcript']}-{ep['index']}",
            "transcript": ep["transcript"], "kind": ep["kind"],
            "split": "holdout" if i < holdout else "tuning",
            "1_asked": ep["asked"] or "(no prompt in this episode's span)",
            # Steps interleaved with what the agent said after each one. Text
            # and tool calls arrive in separate messages, so a step's `_after`
            # is also the reasoning that leads into the next — which is what
            # makes a sequence of commands legible as a process. Reading the
            # closing summary alone asks someone to judge a procedure from its
            # receipt.
            "2_what_happened": _narrate(ep["narrate"]),
            "3_afterwards": ep["evidence"] or "(nothing said afterwards)",
            "labels": draft_labels(ep),
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"created": datetime.date.today().isoformat(),
         "older_than_days": args.older_than, "seed": args.seed,
         "note": ("Set each `verdict` to true or false. A batch with any "
                  "verdict still null is refused by `score`, so drafts are "
                  "never scored as if they were answers."),
         "episodes": batch}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"\n{len(batch)} episodes -> {out}")
    print(f"  {sum(1 for b in batch if b['split'] == 'tuning')} tuning, "
          f"{holdout} holdout")
    print(f"  {sum(1 for b in batch if b['labels']['confidence'] == 'low')} "
          f"low-confidence — review those first")
    return 0


def load_labels(tag: str) -> dict:
    path = LABELS_DIR / f"{tag}.json"
    if not path.exists():
        raise SystemExit(f"no labels at {path} — run `dump` and label first")
    return json.loads(path.read_text(encoding="utf-8"))


def score(predicted: list[int], truth: list[int], tolerance: int = 1) -> dict:
    """Match predicted cuts to labelled ones, nearest first, within *tolerance*.

    Greedy nearest-match rather than exact equality: a cut one step off is a
    near miss and a cut twenty steps off is a different claim about the work,
    and a score that cannot tell them apart cannot guide anything.
    """
    unmatched = sorted(truth)
    hits: list[tuple[int, int]] = []
    for cut in sorted(predicted):
        if not unmatched:
            break
        nearest = min(unmatched, key=lambda t: abs(t - cut))
        if abs(nearest - cut) <= tolerance:
            hits.append((cut, nearest))
            unmatched.remove(nearest)
    tp = len(hits)
    fp = len(predicted) - tp
    fn = len(truth) - tp
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 0.0 if not (precision + recall) else 2 * precision * recall / (precision + recall)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3),
            "recall": round(recall, 3), "f1": round(f1, 3),
            "matched": hits, "missed": unmatched}


# -- scoring against adjudicated labels -------------------------------------

def load_batch(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [e["id"] for e in data["episodes"]
               if any(e["labels"][k]["verdict"] is None
                      for k in ("one_task", "method"))]
    if missing:
        raise SystemExit(
            f"{len(missing)} episode(s) still hold a draft rather than a verdict, "
            f"first is {missing[0]}.\nAdjudicate them, or drop them from the "
            f"batch. Scoring a draft would be scoring my own guess and calling "
            f"it ground truth.")
    return data


def cmd_score(args: argparse.Namespace) -> int:
    from skillpp.config import Config
    from skillpp.episode import is_reusable
    from skillpp.ledger import Entry

    data = load_batch(args.batch)
    config = Config(args.root)
    piles = ("tuning",) if not args.holdout else ("tuning", "holdout")
    if args.holdout:
        print("Scoring the HOLDOUT. Do this deliberately, not while iterating.\n")

    for pile in piles:
        rows = [e for e in data["episodes"] if e["split"] == pile]
        if not rows:
            continue
        tp = fp = fn = tn = unknown = 0
        for episode in rows:
            truth = episode["labels"]["method"]["verdict"]
            steps = [{"tool": "Bash", "input": {"command": line.lstrip("$! ")},
                      "failed": line.startswith("!")}
                     for line in episode["steps"]]
            entry = Entry(id=episode["id"],
                          title=episode["id"], steps=steps,
                          intents=[episode.get("evidence", "")[:200]])
            got, _ = is_reusable(entry, model=config.local_model,
                                 host=config.ollama_url)
            if got is None:
                unknown += 1
            elif got and truth:
                tp += 1
            elif got and not truth:
                fp += 1
            elif not got and truth:
                fn += 1
            else:
                tn += 1
        judged = tp + fp + fn + tn
        recall = tp / max(1, tp + fn)
        precision = tp / max(1, tp + fp)
        print(f"{pile.upper():<8} {len(rows)} episodes, {judged} judged, "
              f"{unknown} no opinion")
        print(f"         sift recall {tp}/{tp + fn} = {recall:.0%}   "
              f"precision {tp}/{tp + fp} = {precision:.0%}")
        if pile == "holdout":
            _log_holdout(len(rows), recall, precision)
    if not args.holdout:
        print("\nHoldout not scored. Add --holdout when you actually want to know.")
    return 0


def _log_holdout(n: int, recall: float, precision: float) -> None:
    """Append every holdout run, so scoring it repeatedly is visible.

    A held-out set stops being held out the moment someone quietly checks it
    after each change. The log does not prevent that; it makes it obvious.
    """
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent.parent.parent
                             ).stdout.strip()
    except OSError:
        rev = "unknown"
    HOLDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HOLDOUT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="seconds"),
                             "rev": rev, "episodes": n,
                             "recall": round(recall, 3),
                             "precision": round(precision, 3)}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dump", help="write a reviewable slice for labelling")
    p.add_argument("tag", help="transcript id prefix, e.g. 058e7e50")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--count", type=int, default=60)
    p.add_argument("--out")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("sample", help="stratified batch of real episodes to label")
    p.add_argument("--count", type=int, default=50)
    p.add_argument("--older-than", type=int, default=14,
                   metavar="DAYS", help="skip transcripts newer than this")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", default=str(EPISODES_DIR / "batch-01.json"))
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("score", help="score sift against adjudicated labels")
    p.add_argument("batch")
    p.add_argument("--holdout", action="store_true",
                   help="also score the held-out pile; logged every time")
    p.add_argument("--root")
    p.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
