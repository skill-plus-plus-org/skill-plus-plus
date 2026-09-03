#!/usr/bin/env python3
"""Turn a recorded Claude Code transcript into a live-session fixture.

    python3 tests/fixtures/sessions/from_transcript.py <tag> --name two-tasks \
        --episodes 2 --must-contain foo.py --must-contain bar.json

Writes `<tag>-<name>.json` beside this file, in the shape `score.py` reads, and
prints what it found so the ground truth can be written by hand afterwards —
which is the order `README.md` insists on:

> Write the ground truth before running the pipeline over it. Deciding what
> "correct" means after seeing the output is how the synthetic corpus ended up
> measuring itself.

**It keeps exactly what `capture` keeps**, via `_KEEP_INPUT`. That matters: the
loader in `tests/benchmarks/boundaries.py` deliberately adds a `Read`'s path for
readable narration, and fixtures built there were for a while unfaithful to what
the pipeline actually stores — which hid a rule that could never fire.

`$HOME` is templated out and the result is scanned for secrets before it lands,
because an earlier attempt at fixtures shipped a token-shaped string into a repo
file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from skillpp.capture import _KEEP_INPUT                     # noqa: E402
from skillpp.sanitize import scrub, scrub_obj               # noqa: E402
from skillpp.segment import PROMPT_TOOL, is_prompt, segment  # noqa: E402

HOME = os.path.expanduser("~")
ENVELOPE = ("<task-notification", "<system-reminder", "<local-command",
            "<command-name", "<command-message", "<!-- attach")
SECRETS = re.compile(r"(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
                     r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)")


def find(tag: str) -> Path:
    matches = sorted(Path.home().glob(f".claude/projects/*/{tag}*.jsonl"))
    if not matches:
        raise SystemExit(f"no transcript matching {tag}")
    return matches[0]


def extract(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.open(errors="ignore")
            if line.strip()]
    failures: dict[str, bool] = {}
    for row in rows:
        message = row.get("message") or {}
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    failures[block.get("tool_use_id")] = bool(block.get("is_error"))

    steps: list[dict] = []
    for row in rows:
        message = row.get("message") or {}
        if row.get("type") == "user" and isinstance(message.get("content"), str):
            text = message["content"].strip()
            if text and not text.startswith(ENVELOPE):
                steps.append({"tool": PROMPT_TOOL,
                              "input": {"text": scrub(text)[:2000]},
                              "failed": False})
            continue
        if row.get("type") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            tool = block.get("name")
            raw = block.get("input") or {}
            keep = _KEEP_INPUT.get(tool)
            if keep:
                kept = {k: raw.get(k) for k in keep if raw.get(k) is not None}
            elif str(tool).startswith("mcp__"):
                kept = {k: v for k, v in list(raw.items())[:5]
                        if isinstance(v, (str, int, float, bool))}
            else:
                kept = {}
            steps.append({"tool": tool, "input": scrub_obj(kept, 2000),
                          "failed": failures.get(block.get("id"), False)})
    return json.loads(json.dumps(steps).replace(HOME, "${HOME}"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--name", required=True, help="slug for the filename")
    ap.add_argument("--episodes", type=int, required=True, help="ground truth")
    ap.add_argument("--markers", type=int, default=None)
    ap.add_argument("--title", default=None, help="omit for a commitless session")
    ap.add_argument("--must-contain", action="append", default=[])
    args = ap.parse_args(argv)

    steps = extract(find(args.tag))
    blob = json.dumps(steps)
    hits = SECRETS.findall(blob)
    if hits:
        raise SystemExit(f"refusing to write: {len(hits)} secret-shaped string(s)")
    if HOME in blob:
        raise SystemExit("refusing to write: $HOME survived templating")

    work = [s for s in steps if not is_prompt(s)]
    doc = {
        "tag": args.tag[:8],
        "name": args.name,
        "captured": __import__("datetime").date.today().isoformat(),
        "procedure": "",
        "truth": {"episodes": args.episodes, "markers": args.markers,
                  "title": args.title, "must_contain": args.must_contain},
        "why": "", "history": "",
        "shape": {"steps": len(work),
                  "prompts": len(steps) - len(work),
                  "failed": sum(1 for s in work if s.get("failed"))},
        "steps": steps,
    }
    out = HERE / f"{args.tag[:8]}-{args.name}.json"
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    got = segment([dict(s) for s in steps], 2, 25)
    print(f"wrote {out.name}")
    print(f"  {len(work)} steps, {doc['shape']['prompts']} prompts, "
          f"{doc['shape']['failed']} failed")
    print(f"  segments into {len(got)} episode(s) against a truth of {args.episodes}"
          f"{'   <-- records a gap' if len(got) != args.episodes else ''}")
    print("  fill in `procedure`, `why` and `history` by hand before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
