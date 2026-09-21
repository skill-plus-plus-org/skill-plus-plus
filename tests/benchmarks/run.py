#!/usr/bin/env python3
"""Score the pipeline against the benchmark corpus.

    python3 tests/benchmarks/run.py                  # segmentation + ranking
    python3 tests/benchmarks/run.py --no-model       # segmentation only, free
    python3 tests/benchmarks/run.py --kind productivity
    python3 tests/benchmarks/run.py --json > baseline.json

Two scores, kept apart on purpose because they fail for different reasons and
cost different amounts.

**Segmentation** is free, deterministic and runs in the unit suite: given a
session, does it bank the right number of candidates? A wrong count here cannot
be recovered downstream — merged episodes hide procedures inside each other and
split ones destroy the recurrence count.

**Ranking** needs a local model and is therefore a benchmark rather than a test:
of the candidates banked, are the reusable ones marked `method`? It is scored
only on cases that segmented correctly, since ranking episodes that should not
exist measures nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(Path(__file__).resolve().parent)]

from cases import CASES, by_kind  # noqa: E402


def load(repo: Path | None):
    """Import the capture pipeline from *repo*, defaulting to this checkout.

    A corpus is only worth having if it can be pointed at more than the design
    that produced it, so the import is late and parameterised rather than a
    module-level binding. Any branch exposing the three hook handlers and a
    Ledger can be scored — which is the same trick `replay.py` uses on the
    pattern-detection branch, for the same reason.
    """
    if repo is not None:
        sys.path.insert(0, str(Path(repo).resolve()))
        for name in [m for m in sys.modules if m.startswith("skillpp")]:
            del sys.modules[name]
    from skillpp.capture import handle_prompt, handle_session_end, handle_tool
    from skillpp.config import Config
    from skillpp.ledger import Ledger
    return handle_prompt, handle_tool, handle_session_end, Config, Ledger

_INPUT_KEY = {"Bash": "command", "Write": "file_path", "Edit": "file_path",
              "Read": "file_path"}


def play(case, root: Path, api, *, merge: bool = False) -> list:
    """Drive one case through the real hook handlers and return what was banked."""
    handle_prompt, handle_tool, handle_session_end, Config, Ledger = api
    config = Config(root)
    config.ensure_dirs()
    sid = case.name
    for tool, body, failed, *rest in case.script:
        if tool == "prompt":
            handle_prompt(config, {"session_id": sid, "cwd": "/w", "prompt": body})
            continue
        payload = {"session_id": sid, "cwd": "/w", "tool_name": tool}
        if tool.startswith("mcp__"):
            payload["tool_input"] = body
        else:
            payload["tool_input"] = {_INPUT_KEY.get(tool, "value"): body}
            if rest and rest[0]:
                payload["tool_input"]["description"] = rest[0]
        if failed:
            payload["tool_response"] = {"is_error": True, "error": "command failed"}
        handle_tool(config, payload)
    handle_session_end(config, {"session_id": sid})
    if case.follow:
        # A distinct session id, because occurrences count sessions and a
        # replay under the same id would measure nothing.
        for tool, body, failed, *rest in case.follow:
            if tool == "prompt":
                handle_prompt(config, {"session_id": sid + "-2", "cwd": "/w",
                                       "prompt": body})
                continue
            payload = {"session_id": sid + "-2", "cwd": "/w", "tool_name": tool}
            if tool.startswith("mcp__"):
                payload["tool_input"] = body
            else:
                payload["tool_input"] = {_INPUT_KEY.get(tool, "value"): body}
                if rest and rest[0]:
                    payload["tool_input"]["description"] = rest[0]
            if failed:
                payload["tool_response"] = {"is_error": True,
                                            "error": "command failed"}
            handle_tool(config, payload)
        handle_session_end(config, {"session_id": sid + "-2"})
    # `merge` is kept for callers but no longer does anything: runs are matched
    # by embedding when each session is folded, so there is no later pass to
    # apply.
    return list(Ledger(config).all())


def score(cases, use_model: bool, repo=None) -> dict:
    api = load(repo)
    Config, Ledger = api[3], api[4]
    rows = []
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            banked = play(case, Path(tmp) / "skillpp", api, merge=use_model)
            seg_ok = len(banked) == case.episodes
            top = max((e.occurrences for e in banked), default=0)
            row = {"name": case.name, "kind": case.kind, "tags": case.tags,
                   "expected_episodes": case.episodes, "got_episodes": len(banked),
                   "segmentation": seg_ok, "expected_methods": case.methods,
                   "got_methods": None, "ranking": None,
                   "expected_occurrences": case.occurrences,
                   "got_occurrences": top,
                   # Only scored where the case has a second session; a single
                   # session can never advance a count that counts sessions.
                   "recurrence": (top == case.occurrences
                                  if case.follow else None),
                   "titles": [e.title[:60] for e in banked]}
            # Ranking is only meaningful where segmentation produced the right
            # episodes; otherwise it is scoring the wrong objects.
            if use_model and seg_ok and banked:
                from skillpp.episode import rank
                config = Config(Path(tmp) / "skillpp")
                hints = [rank(e, host=config.ollama_url,
                              model=config.local_model)[0] for e in banked]
                got = sum(1 for h in hints if h == "method")
                row["got_methods"] = got
                row["ranking"] = got == case.methods
                row["hints"] = hints
            rows.append(row)
    seg = [r for r in rows if r["segmentation"]]
    ranked = [r for r in rows if r["ranking"] is not None]
    recur = [r for r in rows if r["recurrence"] is not None]
    return {
        "cases": len(rows),
        "segmentation": {"passed": len(seg), "of": len(rows)},
        "ranking": ({"passed": sum(1 for r in ranked if r["ranking"]),
                     "of": len(ranked)} if ranked else None),
        "recurrence": ({"passed": sum(1 for r in recur if r["recurrence"]),
                        "of": len(recur)} if recur else None),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=["programming", "productivity"])
    ap.add_argument("--no-model", action="store_true",
                    help="segmentation only — free and deterministic")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", type=Path,
                    help="score another checkout's pipeline instead of this one")
    args = ap.parse_args()

    result = score(by_kind(args.kind), use_model=not args.no_model,
                   repo=args.repo)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    for r in result["rows"]:
        mark = "ok  " if r["segmentation"] else "MISS"
        detail = f"{r['got_episodes']}/{r['expected_episodes']} episodes"
        if r["ranking"] is not None:
            detail += (f" · {r['got_methods']}/{r['expected_methods']} method"
                       f" {'ok' if r['ranking'] else 'MISS'}")
        if r["recurrence"] is not None:
            detail += (f" · x{r['got_occurrences']}/x{r['expected_occurrences']}"
                       f" {'ok' if r['recurrence'] else 'MISS'}")
        print(f"{mark} {r['kind'][:4]:4} {r['name']:30} {detail}")
        for title in r["titles"]:
            print(f"          · {title}")

    s = result["segmentation"]
    print(f"\nsegmentation  {s['passed']}/{s['of']}")
    if result["ranking"]:
        k = result["ranking"]
        print(f"ranking       {k['passed']}/{k['of']}   "
              f"(only cases that segmented correctly)")
    if result["recurrence"]:
        k = result["recurrence"]
        print(f"recurrence    {k['passed']}/{k['of']}   "
              f"(only cases with a second session)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
