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
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
# The recorded sessions. The ones in this directory are public work; a set
# recorded on work that cannot be published lives elsewhere and is scored by
# pointing this at it. `expected.json` beside them holds the numbers that
# belong to that set alone.
SESSIONS = Path(os.environ.get("SKILL_PLUS_PLUS_FIXTURES") or HERE).expanduser()
FIXTURE_NAME = re.compile(r"^[0-9a-f]{8}-.+\.json$")
sys.path.insert(0, str(REPO))
# Folding names each banked candidate with the local LLM. Nothing scored here
# reads the name, and it loaded a second model beside the embedder
# (recurrence.py imports this module too).
os.environ.setdefault("SKILL_PLUS_PLUS_NAME", "0")

from skill_plus_plus.capture import fold_session  # noqa: E402
from skill_plus_plus.config import Config  # noqa: E402
from skill_plus_plus.ledger import Ledger  # noqa: E402
from skill_plus_plus.segment import is_marker, is_prompt  # noqa: E402


def expected() -> dict:
    """The numbers that belong to this set of sessions, or {} if none."""
    path = SESSIONS / "expected.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load(tag: str | None = None) -> list[dict]:
    out = []
    for path in sorted(SESSIONS.glob("*.json")):
        # A fixture is named after its transcript: `<8-hex tag>-<name>.json`.
        # The set's other files (expected.json, catalogue.json, the draft
        # cases) sit beside them and are not sessions.
        if not FIXTURE_NAME.match(path.name):
            continue
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
        config = Config(Path(tmp) / "skill-plus-plus")
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


def cuts_by_role(doc: dict) -> dict[str, dict[str, int]]:
    """At each prompt after the first: was a cut made, and should it have been?

    Keyed by the prompt's role (`truth.roles`), so a judge that cuts at every
    "correct" or never at an unannounced "switch" shows up by name rather than
    in one overall count. Read from the stored verdicts; a fixture without
    `roles` or without verdicts contributes nothing.
    """
    roles = doc["truth"].get("roles")
    want = set(doc["truth"].get("boundary_after") or [])
    out: dict[str, dict[str, int]] = {}
    if not roles or not any("end" in s for s in doc["steps"] if not is_prompt(s)):
        return out
    # Prompts with no tool call between them share one gap, and the judge is
    # asked about it once. It belongs to the first of them: a switch whose
    # reply only proposed ("don't create it yet") must not hand its cut to the
    # "implement" that follows.
    work, last, prompt_no, counted = 0, None, 0, None
    for step in doc["steps"]:
        if is_prompt(step):
            if prompt_no and work and work != counted and prompt_no < len(roles):
                counted = work
                cell = out.setdefault(roles[prompt_no], {"asked": 0, "false": 0,
                                                         "cuts": 0, "missed": 0})
                cell["asked"] += 1
                cut = last is not None and last.get("end") is True
                if work in want:
                    cell["cuts"] += 1
                    cell["missed"] += not cut
                else:
                    cell["false"] += cut
            prompt_no += 1
        else:
            work += 1
            last = step
    return out


def check_table(docs: list[dict], rows: dict[str, dict]) -> list[str]:
    """One line per check id, passes per kind of work."""
    cells: dict[tuple[str, str], list[int]] = {}
    for doc in docs:
        row = rows[doc["tag"]]
        ok = all(row[k]["ok"] for k in ("episodes", "kept", "boundary"))
        for check in doc.get("checks") or []:
            if not check.startswith("detect."):
                continue            # merge checks are scored by recurrence.py
            cell = cells.setdefault((check, doc.get("kind") or "-"), [0, 0])
            cell[0] += ok
            cell[1] += 1
    if not cells:
        return []
    kinds = sorted({k for _, k in cells})
    lines = ["", f"{'check':<24}" + "".join(f"{k:>12}" for k in kinds)]
    for check in sorted({c for c, _ in cells}):
        lines.append(f"{check:<24}" + "".join(
            f"{'/'.join(map(str, cells[(check, k)])) if (check, k) in cells else '–':>12}"
            for k in kinds))
    return lines


def role_table(docs: list[dict]) -> list[str]:
    total: dict[str, dict[str, int]] = {}
    for doc in docs:
        for role, cell in cuts_by_role(doc).items():
            into = total.setdefault(role, {"asked": 0, "false": 0, "cuts": 0, "missed": 0})
            for k, v in cell.items():
                into[k] += v
    if not total:
        return []
    lines = ["", "judge verdicts by prompt role   asked   false cuts   real cuts missed"]
    for role in sorted(total):
        c = total[role]
        missed = f"{c['missed']}/{c['cuts']}" if c["cuts"] else "–"
        lines.append(f"  {role:<28} {c['asked']:>5}   {c['false']:>10}   {missed:>16}")
    return lines


def main(argv: list[str]) -> int:
    docs = load(argv[0] if argv else None)
    if not docs:
        print("no sessions matched", file=sys.stderr)
        return 1

    failed = 0
    rows = {}
    for doc in docs:
        row = rows[doc["tag"]] = check(doc)
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

    for line in check_table(docs, rows) + role_table(docs):
        print(line)

    gaps = sum(1 for d in docs if d.get("expected_fail"))
    print(f"\n{len(docs) - failed - gaps}/{len(docs) - gaps} sessions pass"
          + (f", {gaps} known gap(s)" if gaps else ""))
    for d in docs:
        if d.get("expected_fail"):
            print(f"   GAP {d['tag']}: {d['expected_fail'][:120]}…")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
