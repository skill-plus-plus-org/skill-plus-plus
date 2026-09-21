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

`$HOME`, the account name and UUIDs are templated out of every row before
anything is cut, and the result goes through `scripts/leak_guard.py` before it
lands, because an earlier attempt at fixtures shipped a token-shaped string
into a repo file.
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

from skillpp.capture import (_ENVELOPE_PREFIXES, _KEEP_INPUT,  # noqa: E402
                             _NOT_A_PROMPT, _REPLY_CHARS,
                             _RESPONSE_CHARS, _reply_text)
from skillpp.sanitize import scrub, scrub_obj               # noqa: E402
from skillpp.segment import PROMPT_TOOL, is_prompt, segment  # noqa: E402

HOME = os.path.expanduser("~")
# What is not a prompt: the envelopes live capture ignores, and a tool's image
# result, which a transcript records as a `user` row with string content. Taking
# those as prompts gave run 1 of a real session 13 prompts where there were 4.
ENVELOPE = _ENVELOPE_PREFIXES + _NOT_A_PROMPT
SECRETS = re.compile(r"(ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
                     r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)")


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                   re.IGNORECASE)


def _template(value):
    """Environment literals out of every string in a transcript row, before
    anything is cut. Templating the finished fixture instead let a path cut in
    half at 2,000 characters, the home folder and one letter of the name,
    past both the substitution and the check: neither the home path nor the
    account name was whole any more.
    Account and session UUIDs go too; the rows are linked by tool-use ids."""
    if isinstance(value, dict):
        return {k: _template(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_template(v) for v in value]
    if not isinstance(value, str):
        return value
    user = Path(HOME).name
    text = value.replace(HOME, "${HOME}")
    text = text.replace("-Users-" + user.replace(".", "-") + "-", "-Users-${USER}-")
    text = re.sub(rf"(?<![\w.]){re.escape(user)}(?![\w])", "${USER}", text)
    return _UUID.sub("${UUID}", text)


def find(tag: str) -> Path:
    matches = sorted(Path.home().glob(f".claude/projects/*/{tag}*.jsonl"))
    if not matches:
        raise SystemExit(f"no transcript matching {tag}")
    return matches[0]


def extract(path: Path) -> list[dict]:
    rows = [_template(json.loads(line)) for line in path.open(errors="ignore")
            if line.strip()]
    failures: dict[str, bool] = {}
    replies: dict[str, object] = {}
    for row in rows:
        message = row.get("message") or {}
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    failures[block.get("tool_use_id")] = bool(block.get("is_error"))
                    replies[block.get("tool_use_id")] = block.get("content")

    steps: list[dict] = []
    replies_to: list[list[str]] = []   # per prompt, the agent's reply text
    asks = 0
    pending: list[str] = []          # what the assistant said before the next call
    closing: list[str] = []          # ... and what it said after the previous one
    for row in rows:
        message = row.get("message") or {}
        if row.get("type") == "user" and isinstance(message.get("content"), str):
            text = message["content"].strip()
            if text and not text.startswith(ENVELOPE):
                asks += 1
                # A prompt closes off whatever was said before it: that text
                # reports the step that just finished, not the one about to run.
                # Merging the two put every completion report on the next task —
                # see `capture._narration`.
                closing, pending = pending, []
                steps.append({"tool": PROMPT_TOOL,
                              "input": {"text": scrub(text)[:2000]},
                              "failed": False})
                replies_to.append([])
            continue
        if row.get("type") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            kind = block.get("type") if isinstance(block, dict) else None
            # The assistant's own words before the call. `thinking` as well as
            # `text` — real transcripts put the reasoning that chose a command
            # in a thinking block, and its content is often empty, so this is
            # sparse by nature (7-21% of calls across these sessions).
            if kind in ("text", "thinking") and (block.get(kind) or "").strip():
                pending.append(block[kind])
                # What the person read in reply — `text` only, as live capture
                # keeps it on the prompt marker (`capture._reply_before`).
                if kind == "text" and replies_to:
                    replies_to[-1].append(block["text"].strip())
                continue
            if kind != "tool_use":
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
            step = {"tool": tool, "input": scrub_obj(kept, 2000),
                    "failed": failures.get(block.get("id"), False),
                    "serves": asks or 1}
            # What the tool sent back, and what was said going in — both were
            # in the transcript all along and neither reached the old fixtures.
            returned = scrub(_reply_text(replies.get(block.get("id"))))
            if returned:
                step["tool_returned"] = returned
            note = scrub(" ".join(" ".join(pending).split())[:_RESPONSE_CHARS])
            if note:
                step["assistant_note"] = note
            if closing:
                back = scrub(" ".join(" ".join(closing).split())[:_RESPONSE_CHARS])
                earlier = [s for s in steps if not is_prompt(s)]
                if back and earlier:
                    earlier[-1].setdefault("closing_note", back)
                closing = []
            pending = []
            steps.append(step)
    # Anything left ran out with the session: the last task's own completion
    # report, with no prompt and no call after it. `capture` takes this at
    # SessionEnd.
    for marker, parts in zip((s for s in steps if is_prompt(s)), replies_to):
        reply = scrub("\n\n".join(parts))
        if len(reply) > _REPLY_CHARS:
            reply = reply[:_REPLY_CHARS].rsplit(" ", 1)[0] + " …"
        if reply:
            marker["reply"] = reply
    tail = scrub(" ".join(" ".join(pending).split())[:_RESPONSE_CHARS])
    work = [s for s in steps if not is_prompt(s)]
    if tail and work:
        work[-1].setdefault("closing_note", tail)
    blob = json.dumps(steps).replace(HOME, "${HOME}")
    # The account name survives `$HOME` in two shapes: as a file owner in `ls -l`
    # output, and inside the project slug of a scratchpad path
    # (`/private/tmp/claude-501/-Users-<name>-…`).
    user = Path(HOME).name
    blob = blob.replace("-Users-" + user.replace(".", "-") + "-", "-Users-${USER}-")
    blob = re.sub(rf"(?<![\w.]){re.escape(user)}(?![\w])", "${USER}", blob)
    return json.loads(blob)


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
    if HOME in blob or Path(HOME).name in blob:
        raise SystemExit("refusing to write: $HOME or the account name survived templating")
    # The same check the repo runs before anything is published, including the
    # private denylist when SKILLPP_DENYLIST names one.
    sys.path.insert(0, str(REPO / "scripts"))
    import leak_guard
    leaks = leak_guard.scan_text("fixture", json.dumps(steps, indent=1),
                                 leak_guard.denylist(None))
    if leaks:
        raise SystemExit("refusing to write, the leak guard found:\n  " + "\n  ".join(leaks[:20]))

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
    # Beside the others it will be scored with, public or private.
    out = Path(os.environ.get("SKILLPP_FIXTURES") or HERE).expanduser() / f"{args.tag[:8]}-{args.name}.json"
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    got = segment([dict(s) for s in steps], 2)
    print(f"wrote {out.name}")
    print(f"  {len(work)} steps, {doc['shape']['prompts']} prompts, "
          f"{doc['shape']['failed']} failed")
    print(f"  segments into {len(got)} episode(s) against a truth of {args.episodes}"
          f"{'   <-- records a gap' if len(got) != args.episodes else ''}")
    print("  fill in `procedure`, `why` and `history` by hand before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
