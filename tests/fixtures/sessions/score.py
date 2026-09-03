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

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from skillpp.capture import _intents_for, _title_for  # noqa: E402
from skillpp.segment import is_marker, segment  # noqa: E402


def load(tag: str | None = None) -> list[dict]:
    out = []
    for path in sorted(HERE.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if tag is None or doc["tag"].startswith(tag):
            doc["_path"] = path
            out.append(doc)
    return out


def check(doc: dict) -> dict:
    """Run one session through segmentation and compare against its truth."""
    steps = doc["steps"]
    truth = doc["truth"]
    episodes = segment(steps, 2, 0)

    titles = []
    for episode in episodes:
        intents = _intents_for({"prompts": []}, episode.steps)
        titles.append(_title_for(intents, episode.steps))

    # `must_contain` is checked against the largest episode: the procedure is
    # what the bulk of the work was, and a stray one-step trailer after the
    # commit is not where the doc lookup would be.
    main = max(episodes, key=lambda e: len(e.steps), default=None)
    blob = json.dumps([s for s in (main.steps if main else [])])
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

    return {
        "tag": doc["tag"],
        "name": doc["name"],
        "episodes": {"want": truth["episodes"], "got": len(episodes),
                     "ok": len(episodes) == truth["episodes"]},
        "title": {"want": want_title, "got": titles[0] if titles else "",
                  "ok": title_ok, "n/a": want_title is None},
        "kept": {"want": truth.get("must_contain", []), "missing": missing,
                 "ok": not missing},
        "markers": {"want": want_markers, "got": got_markers,
                    "ok": markers_ok, "n/a": want_markers is None},
    }


def main(argv: list[str]) -> int:
    docs = load(argv[0] if argv else None)
    if not docs:
        print("no sessions matched", file=sys.stderr)
        return 1

    failed = 0
    for doc in docs:
        row = check(doc)
        ok = all(row[k]["ok"] for k in ("episodes", "title", "kept", "markers"))
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

    gaps = sum(1 for d in docs if d.get("expected_fail"))
    print(f"\n{len(docs) - failed - gaps}/{len(docs) - gaps} sessions pass"
          + (f", {gaps} known gap(s)" if gaps else ""))
    for d in docs:
        if d.get("expected_fail"):
            print(f"   GAP {d['tag']}: {d['expected_fail'][:120]}…")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
