#!/usr/bin/env python3
"""Render a benchmark case as a Claude Code transcript.

The corpus is written as hook events, which is what this branch observes. A
transcript-reading design needs the same sessions in its own input format, or a
comparison is measuring the adapter rather than the detector.

Written from the shape `tests/fixtures/seed_recurrence.py` produces on the
pattern-detection branch, so its `transcript.py` reads these without special
cases.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
_INPUT_KEY = {"Bash": "command", "Write": "file_path", "Edit": "file_path",
              "Read": "file_path"}


def render(case, out: Path) -> Path:
    n = [0]

    def stamp() -> str:
        n[0] += 1
        return (START + timedelta(minutes=n[0])).isoformat().replace("+00:00", "Z")

    rows = []

    def user(text):
        rows.append({"type": "user", "uuid": f"u{n[0]:03d}", "isSidechain": False,
                     "timestamp": stamp(),
                     "message": {"role": "user", "content": text}})

    def call(name, inp):
        rows.append({"type": "assistant", "uuid": f"a{n[0]:03d}",
                     "isSidechain": False, "timestamp": stamp(),
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": f"t{n[0]:03d}",
                          "name": name, "input": inp}]}})

    def result(text, error=False):
        block = {"type": "tool_result", "tool_use_id": f"t{n[0]:03d}",
                 "content": text}
        if error:
            block["is_error"] = True
        rows.append({"type": "user", "uuid": f"r{n[0]:03d}", "isSidechain": False,
                     "timestamp": stamp(),
                     "message": {"role": "user", "content": [block]}})

    for tool, body, failed in case.script:
        if tool == "prompt":
            user(body)
            continue
        inp = body if tool.startswith("mcp__") else {_INPUT_KEY.get(tool, "value"): body}
        call(tool, inp)
        result("command failed: exit 1" if failed else "ok", error=failed)

    path = out / f"{case.name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cases import CASES
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        print(render(case, out))
