#!/usr/bin/env python3
"""Score the pipeline against real captured sessions.

    python3 tests/fixtures/sessions/score.py            # all
    python3 tests/fixtures/sessions/score.py 1c3c9422   # one

Unlike `tests/benchmarks/run.py`, nothing here was authored alongside the
detector. The sessions are real work, and the ground truth was written by
reading them rather than by running the pipeline and recording what it did.

Three things are checked, in increasing order of what they actually mean:

* **episodes** — did segmentation bank the right number of candidates
* **title** — is the entry called what the work was called
* **kept** — did the steps the procedure exists *for* survive into an episode

The last is the one that matters. A run can bank one episode with the right
name and still have thrown away the documentation lookup that makes the
procedure worth repeating, and only `must_contain` notices that.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from skillpp.capture import fold_session  # noqa: E402
from skillpp.config import Config  # noqa: E402
from skillpp.ledger import Ledger  # noqa: E402
from skillpp.segment import is_marker, is_prompt  # noqa: E402


def load(tag: str | None = None) -> list[dict]:
    out = []
    for path in sorted(HERE.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if tag is None or doc["tag"].startswith(tag):
            doc["_path"] = path
            out.append(doc)
    return out


def bank(doc: dict) -> list:
    """The candidates this session actually banks, by running the real fold.

    Not `segment()`. Segmentation is the first of three stages — it cuts, then
    `fold_session` discards the flagged episodes, then `_fold_steps` discards
    anything under two substantive steps. `truth.episodes` is defined by the
    README as what a correct run *banks*, which is the third stage, so scoring
    the first counted leftovers the pipeline throws away: a trailing
    `git status` after a commit read as a second candidate in three fixtures.

    Calling `fold_session` rather than reproducing its filters is the point. A
    private copy of pipeline logic in a test helper is what put the count wrong
    here, and what earlier let `boundaries.KEEP` drift from
    `capture._KEEP_INPUT` until a rule fired only in fixtures. Anything the fold
    stage learns to discard next is picked up here without an edit.

    Nothing is re-executed: the steps are records, the commands are strings
    being read. The temporary root exists only because banking writes entry
    files.
    """
    with tempfile.TemporaryDirectory() as tmp:
        config = Config(Path(tmp) / "skillpp")
        config.ensure_dirs()
        fold_session(config, {"session_id": doc["tag"], "cwd": "",
                              "prompts": [],
                              "steps": copy.deepcopy(doc["steps"])})
        return sorted(Ledger(config).all(), key=lambda e: -len(e.steps))


def check(doc: dict) -> dict:
    """Run one session through the pipeline and compare against its truth."""
    steps = doc["steps"]
    truth = doc["truth"]
    entries = bank(doc)
    titles = [entry.title for entry in entries]

    # `must_contain` is checked against the largest banked candidate: the
    # procedure is what the bulk of the work was, and a stray one-step trailer
    # after the commit is not where the doc lookup would be. Against the
    # *banked* one, because a needle surviving into an episode that is then
    # discarded has not survived anywhere that matters.
    blob = json.dumps(entries[0].steps) if entries else "[]"
    missing = [needle for needle in truth.get("must_contain", [])
               if needle not in blob]

    # A session that never commits has no commit subject to be titled from, so
    # `title: null` means "not applicable" rather than "no opinion". Scoring it
    # against the prompt-derived title would pin behaviour we want to change.
    want_title = truth.get("title")
    title_ok = True if want_title is None else want_title in titles

    # `markers` is pinned only where it is the point of the fixture — a
    # commitless session's shape is the thing being recorded, not an accident.
    want_markers = truth.get("markers")
    got_markers = sum(1 for s in steps if is_marker(s))
    markers_ok = True if want_markers is None else got_markers == want_markers

    # WHERE the cut fell, not just how many there were. The count alone cannot
    # see a boundary in the wrong place: a run that split `241955c7` in the
    # middle of its second task still banked 2 episodes and scored a pass. Only
    # pinned where a session has more than one task, since that is the only
    # place a cut can be wrong rather than absent.
    want_at = truth.get("boundary_after")
    got_at = [i for i, s in enumerate(steps)
              if not is_prompt(s) and s.get("end") is True]
    # 1-based over substantive steps, which is how the fixtures read.
    work_index = {}
    n = 0
    for i, s in enumerate(steps):
        if not is_prompt(s):
            n += 1
            work_index[i] = n
    got_at = [work_index[i] for i in got_at]
    at_ok = True if want_at is None else got_at == want_at

    return {
        "tag": doc["tag"],
        "name": doc["name"],
        "episodes": {"want": truth["episodes"], "got": len(entries),
                     "ok": len(entries) == truth["episodes"]},
        "title": {"want": want_title, "got": titles[0] if titles else "",
                  "ok": title_ok, "n/a": want_title is None},
        "kept": {"want": truth.get("must_contain", []), "missing": missing,
                 "ok": not missing},
        "markers": {"want": want_markers, "got": got_markers,
                    "ok": markers_ok, "n/a": want_markers is None},
        "boundary": {"want": want_at, "got": got_at, "ok": at_ok,
                     "n/a": want_at is None},
    }


def main(argv: list[str]) -> int:
    docs = load(argv[0] if argv else None)
    if not docs:
        print("no sessions matched", file=sys.stderr)
        return 1

    failed = 0
    for doc in docs:
        row = check(doc)
        ok = all(row[k]["ok"]
                 for k in ("episodes", "title", "kept", "markers",
                           "boundary"))
        # A fixture can record a gap the pipeline is known not to close. Those
        # are not failures of the run; they are the reason the fixture exists.
        known = doc.get("expected_fail")
        if not ok and known:
            label = "GAP "
        else:
            label = "ok  " if ok else "MISS"
            failed += not ok
        print(f"\n{label} {row['tag']}  {row['name']}")
        e = row["episodes"]
        print(f"       episodes  {e['got']}/{e['want']}"
              f"{'' if e['ok'] else '   <-- wrong'}")
        t = row["title"]
        print(f"       title     {t['got'][:58]!r}"
              f"{' (not scored — no commit)' if t['n/a'] else ''}"
              f"{'' if t['ok'] else chr(10) + '                 want ' + repr(t['want'])}")
        k = row["kept"]
        print(f"       kept      {'all present' if k['ok'] else 'MISSING ' + str(k['missing'])}"
              f"   ({', '.join(k['want'])})")
        m = row["markers"]
        if not m["n/a"]:
            print(f"       markers   {m['got']}/{m['want']}"
                  f"{'' if m['ok'] else '   <-- wrong'}")
        b = row["boundary"]
        if not b["n/a"]:
            print(f"       cut after step {b['got']} / want {b['want']}"
                  f"{'' if b['ok'] else '   <-- wrong place'}")

    gaps = sum(1 for d in docs if d.get("expected_fail"))
    print(f"\n{len(docs) - failed - gaps}/{len(docs) - gaps} sessions pass"
          + (f", {gaps} known gap(s)" if gaps else ""))
    for d in docs:
        if d.get("expected_fail"):
            print(f"   GAP {d['tag']}: {d['expected_fail'][:120]}…")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
