#!/usr/bin/env python3
"""Check drafted skills against criteria fixed before any draft ran.

    python3 tests/benchmarks/draft_check.py prepare draft.code.feature
    python3 tests/benchmarks/draft_check.py check

Drafting is a frontier-model call, so it is not part of the unit suite: the
developer runs it. Everything around it is scripted, so a change to the draft
prompt (`skill_plus_plus/commands/skill-plus-plus-draft.md`) can be judged the same way every
time.

`prepare` folds one recorded session, alone, into its own ledger and prints the
`skill-plus-plus draft` command to run. `check` reads what the draft wrote and prints
every criterion in `tests/fixtures/sessions/draft_cases.json` as pass or fail,
with the line that decided it.

One session per draft, by the one-run rule: the draft is written from the
candidate's first run, so a single recorded run is exactly its input.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SESSIONS = REPO / "tests" / "fixtures" / "sessions"
CASES = SESSIONS / "draft_cases.json"
DEFAULT_OUT = Path.home() / "skill-plus-plus-draft-check"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SESSIONS))

from skill_plus_plus.lifecycle import parse_frontmatter  # noqa: E402

MARKER = "<!-- skill-plus-plus:write-the-procedure -->"
STEP = re.compile(r"^\s{0,3}\d+\.\s")
SUBSTEP = re.compile(r"^###\s+(step\s+)?\d+[.):]?\s", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
NEGATED = re.compile(r"\b(not|no|never|without|don'?t|doesn'?t|avoid)\b", re.IGNORECASE)
HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
LEAKS = ("${HOME}", "/Users/", "/private/", "skillpp-recordings")


def load_cases() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


# -- reading a SKILL.md ----------------------------------------------------

def body_of(text: str) -> str:
    """Everything after the frontmatter."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for each `##` section, in order."""
    out, title, lines = [], "", []
    for line in body_of(text).splitlines():
        m = HEADING.match(line)
        if m and len(m.group(1)) == 2:
            out.append((title, "\n".join(lines)))
            title, lines = m.group(2).strip(), []
        else:
            lines.append(line)
    out.append((title, "\n".join(lines)))
    return out


def procedure_steps(text: str) -> list[str]:
    """The numbered steps of the procedure: the first section after
    `## When to use` that has numbered lines. A step runs until the next
    numbered line or heading, so its explanation counts as part of it."""
    secs = sections(text)
    names = [t.lower() for t, _ in secs]
    start = names.index("when to use") + 1 if "when to use" in names else 0
    for title, body in secs[start:]:
        if title.lower() in ("open questions", "requirements"):
            continue
        # Steps written as `### 1. Read the script` subheadings: each runs to
        # the next one, numbered lists inside it included.
        if any(SUBSTEP.match(l) for l in body.splitlines()):
            steps, current = [], None
            for line in body.splitlines():
                if SUBSTEP.match(line):
                    if current is not None:
                        steps.append(current)
                    current = line.lstrip("# ")
                elif current is not None:
                    current += "\n" + line
            if current is not None:
                steps.append(current)
            return steps
        steps, current = [], None
        for line in body.splitlines():
            if HEADING.match(line):
                break
            if STEP.match(line):
                if current is not None:
                    steps.append(current)
                current = line
            elif current is not None:
                current += "\n" + line
        if current is not None:
            steps.append(current)
        if steps:
            return steps
    return []


def without_questions(text: str) -> str:
    """The draft minus `## Open questions`: a setting the draft *asks* about
    is the behaviour wanted, not a one-off turned into a rule."""
    return "\n".join(b for t, b in sections(text) if t.lower() != "open questions")


def question_items(text: str) -> list[str]:
    """Each item under `## Open questions`, its wrapped lines joined."""
    items: list[str] = []
    for title, body in sections(text):
        if title.lower() != "open questions":
            continue
        for line in body.splitlines():
            if re.match(r"\s{0,3}(-|\*|\d+\.)\s", line):
                items.append(line.strip())
            elif line.strip() and items and not line.startswith(("#", "---", "_")):
                items[-1] += " " + line.strip()
    return items


def sentences(text: str) -> list[str]:
    # Not after "e.g." or "i.e.": splitting there moved the hedge that makes a
    # setting an example into the sentence before it.
    return [s for s in re.split(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s+|\n", text) if s.strip()]


# -- the checks ------------------------------------------------------------

def result(cid: str, label: str, ok: bool, evidence: str = "") -> dict:
    return {"id": cid, "label": label, "ok": bool(ok), "evidence": evidence.strip()[:160]}


def general_checks(text: str, log: str, entry_id: str, fixture: dict | None) -> list[dict]:
    out = []
    header = log.splitlines()[0] if log else ""
    declined = next((l for l in log.splitlines() if l.strip().startswith("SKILL-PLUS-PLUS-DECLINE:")), "")
    out.append(result("G1", "the run finished", re.search(r"\bexit 0\b", header) and not declined,
                      declined or header or "no agent.log"))

    front = parse_frontmatter(text)
    provenance = str((front.get("metadata") or {}).get("provenance", ""))
    out.append(result("G2", "valid frontmatter", front and provenance == f"ledger:{entry_id}",
                      f"provenance: {provenance or 'missing'}"))
    name = str(front.get("name") or "")
    out.append(result("G3", "a usable name",
                      re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64, name))
    desc = str(front.get("description") or "")
    out.append(result("G4", "the description is a trigger",
                      20 <= len(desc) <= 200 and re.search(r"\bwhen(ever)?\b", desc, re.I),
                      f"{len(desc)} chars: {desc}"))
    out.append(result("G5", "the template was filled in", MARKER not in text,
                      MARKER if MARKER in text else ""))

    when = next((b for t, b in sections(text) if t.lower() == "when to use"), None)
    out.append(result("G6", "a trigger section", when is not None and when.strip(),
                      "missing" if when is None else when.strip().splitlines()[0] if when.strip() else "empty"))
    steps = procedure_steps(text)
    out.append(result("G7", "real steps", 3 <= len(steps) <= 12, f"{len(steps)} numbered steps"))

    leaks = [s for s in LEAKS if s in text]
    if fixture:
        if fixture.get("tag") and fixture["tag"] in text:
            leaks.append(fixture["tag"])
        seen = set(HEX.findall(json.dumps(fixture.get("steps", []))))
        leaks += sorted(h for h in set(HEX.findall(text)) if h in seen)
    out.append(result("G8", "nothing from the recording leaked", not leaks, ", ".join(leaks)))

    blocks = re.findall(r"^```.*?\n(.*?)^```", text, re.M | re.S)
    long = [b for b in blocks if len(b.strip().splitlines()) > 3]
    out.append(result("G9", "no shell pasted back", not long,
                      long[0].splitlines()[0] if long else ""))

    bad = [i for i in question_items(text) if "?" not in i]
    out.append(result("G10", "questions are questions", not bad, bad[0] if bad else ""))
    return out


def find_concept(steps: list[str], spec: dict, found: dict[str, int | None]) -> int | None:
    after = found.get(spec["after"]) if spec.get("after") else 0
    if spec.get("after") and after is None:
        return None
    for n, step in enumerate(steps, 1):
        if n <= (after or 0):
            continue
        if spec.get("max_step") and n > spec["max_step"]:
            return None
        # "Do not build yet" names the build without asking for it: where the
        # concept is an instruction to do something, negated sentences do not count.
        text = step
        if spec.get("affirmative"):
            # Joined first: a wrapped line put "Do not" and "generate any file"
            # in different sentences.
            flat = " ".join(step.split())
            text = " ".join(s for s in sentences(flat) if not NEGATED.search(s))
        if all(re.search(p, text, re.IGNORECASE) for p in spec["all"]):
            return n
    return None


def _norm(s: str) -> str:
    """For finding a quoted line: no markdown emphasis or code ticks, one space."""
    return " ".join(re.sub(r"[*`_]", "", s).lower().split())


def judged_rows(text: str, case: dict, answers: list[dict]) -> list[dict]:
    """The judge's answers as rows. A yes counts only with a quote that is
    really in the draft: an answer the judge cannot point at is not evidence."""
    by_id = {a.get("id"): a for a in answers if isinstance(a, dict)}
    draft = _norm(text)
    out = []
    for q in case.get("judge", []):
        a = by_id.get(q["id"]) or {}
        quote = str(a.get("quote") or "").strip()
        yes = str(a.get("answer", "")).lower() == "yes"
        if not a:
            out.append(result(q["id"], q["question"][:38], False, "not answered"))
        elif yes and not (quote and _norm(quote) in draft):
            out.append(result(q["id"], q["question"][:38], False, f"quote not in draft: {quote}"))
        else:
            out.append(result(q["id"], q["question"][:38], yes, quote or "no"))
    return out


def case_checks(text: str, case: dict, hedge: str, judged: list[dict] | None = None) -> list[dict]:
    out = []
    steps = procedure_steps(text)
    body = without_questions(text)
    found: dict[str, int | None] = {}
    # What the method is and in what order is meaning, which the judge reads;
    # the word lists below stand in only where it has not run.
    if judged is not None:
        out += judged_rows(text, case, judged)
    for cid, spec in ({} if judged is not None else case.get("concepts", {})).items():
        found[cid] = n = find_concept(steps, spec, found)
        out.append(result(cid, spec["label"], n is not None,
                          f"step {n}: {steps[n - 1].splitlines()[0]}" if n else "not found"))

    for a, op, b in ([] if judged is not None else case.get("order", [])):
        na, nb = found.get(a), found.get(b)
        ok = na is not None and nb is not None and (na < nb if op == "<" else na <= nb)
        out.append(result(f"order {a}{op}{b}", "step order", ok, f"{a} at {na}, {b} at {nb}"))

    for spec in ([] if judged is not None else case.get("absent", [])):
        hits = [s for s in sentences(body) if re.search(spec["pattern"], s, re.I)
                and not NEGATED.search(s)]
        out.append(result(spec["id"], spec["label"], not hits, hits[0] if hits else ""))

    unhedged = [s for pat in case.get("settings", []) for s in sentences(body)
                if re.search(pat, s, re.I) and not re.search(hedge, s, re.I)]
    if case.get("settings"):
        out.append(result("settings", "that day's settings are inputs", not unhedged,
                          unhedged[0] if unhedged else ""))

    front = parse_frontmatter(text)
    meta = f"{front.get('name', '')} {front.get('description', '')}".lower()
    named = [t for t in case.get("meta_forbid", []) if t.lower() in meta]
    out.append(result("meta", "about the procedure, not this run", not named, ", ".join(named)))

    if case.get("body_limit") and judged is None:
        lim = case["body_limit"]
        hits = [s for s in sentences(body) if re.search(lim["pattern"], s, re.I)]
        loose = [s for s in hits if not re.search(hedge, s, re.I)]
        out.append(result(lim["id"], lim["label"], len(hits) <= lim["max"] and not loose,
                          f"{len(hits)} mention(s)" + (f"; unhedged: {loose[0]}" if loose else "")))
    return out


def evaluate(text: str, log: str, entry_id: str, fixture: dict | None,
             case: dict, hedge: str, judged: list[dict] | None = None) -> list[dict]:
    return general_checks(text, log, entry_id, fixture) + case_checks(text, case, hedge, judged)


# -- the judge -------------------------------------------------------------

def judge_prompt(text: str, case: dict, data: dict) -> str:
    questions = "\n".join(f"{q['id']}: {q['question']}" for q in case["judge"])
    return f"{data['judge_prompt']}\n\nQuestions:\n{questions}\n\nSKILL.md:\n<<<\n{body_of(text)}\n>>>\n"


def parse_judge(output: str) -> list[dict]:
    """The JSON array in the judge's reply, or [] if there is none."""
    start, end = output.find("["), output.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        answers = json.loads(output[start:end + 1])
    except ValueError:
        return []
    return answers if isinstance(answers, list) else []


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def run_judge(name: str, skill: Path, case: dict, data: dict, timeout: int = 300) -> dict:
    """One Claude call, from an empty folder so no project instructions leak
    in, and marked internal so skill-plus-plus's own hooks do not capture it."""
    import subprocess
    import tempfile
    text = skill.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            ["claude", "-p", judge_prompt(text, case, data), "--no-session-persistence"],
            cwd=tmp, capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, SKILL_PLUS_PLUS_INTERNAL="1"))
    return {"case": name, "skill_sha256": _digest(text), "exit": proc.returncode,
            "answers": parse_judge(proc.stdout), "raw": proc.stdout[-4000:]}


def load_judged(here: Path, skill: Path) -> list[dict] | None:
    """The judge's answers for exactly this SKILL.md, if it has run on it."""
    path = here / "judge.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    if record.get("skill_sha256") != _digest(skill.read_text(encoding="utf-8")):
        return None
    return record.get("answers") or []


# -- the two commands ------------------------------------------------------

def _fixture_for(session: str) -> dict:
    import score
    for doc in score.load():
        if doc.get("session") == session:
            return doc
    raise SystemExit(f"no fixture for session {session}; build it with "
                     f"from_transcript.py --session {session}")


def prepare(check: str, out: Path, force: bool) -> int:
    cases = load_cases()["cases"]
    if check not in cases:
        raise SystemExit(f"unknown check {check!r}; known: {', '.join(cases)}")
    fixture = _fixture_for(cases[check]["session"])
    here = out / check
    root = here / "skill-plus-plus"
    if root.exists() and not force:
        raise SystemExit(f"{root} exists; pass --force to prepare it again")

    # One session in an empty ledger has nothing to be matched against, so the
    # embedding cannot change the outcome: a fixed vector stands in, and no
    # local model is started. Naming is off for the same reason — the draft
    # names the skill itself.
    os.environ["SKILL_PLUS_PLUS_NAME"] = "0"
    from skill_plus_plus import matching
    matching.embed = lambda text, **kw: [1.0, 0.0, 0.0]
    matching.model_reachable = lambda config: True
    from skill_plus_plus.capture import fold_session
    from skill_plus_plus.config import Config
    from skill_plus_plus.ledger import Ledger

    if root.exists():
        import shutil
        shutil.rmtree(root)
    config = Config(root)
    config.ensure_dirs()
    fold_session(config, {"session_id": fixture["tag"], "cwd": "", "prompts": [],
                          "steps": copy.deepcopy(fixture["steps"])}, force=True)
    entries = sorted(Ledger(config).all(), key=lambda e: -len(e.steps))
    if not entries:
        raise SystemExit(f"{fixture['tag']} banked nothing; see its fixture")
    entry = entries[0]
    (here / "case.json").write_text(json.dumps(
        {"check": check, "entry": entry.id, "session": cases[check]["session"],
         "fixture": str(fixture["_path"])}, indent=1) + "\n")
    review = here / "review.md"
    if not review.exists():
        review.write_text(
            f"# Review: {check}\n\nAnswer yes or no, with a line on why.\n\n"
            "1. Could a colleague follow it?\n2. Does it generalize beyond this one run?\n"
            "3. Did any one-off setting become a fixed rule?\n"
            "4. Are the open questions sensible?\n5. Is anything invented?\n")
    print(f"prepared {check}: candidate {entry.id} from {fixture['tag']} "
          f"({cases[check]['session']})")
    print(f"\nrun:\n  python3 {REPO / 'bin' / 'skill-plus-plus'} --root {root} draft {entry.id} --apply")
    print(f"\nthen review it in {review}")
    return 0


def check(out: Path, only: list[str]) -> int:
    data = load_cases()
    failed = 0
    names = only or list(data["cases"])
    for name in names:
        meta_path = out / name / "case.json"
        if not meta_path.exists():
            print(f"-- {name}: not prepared")
            continue
        meta = json.loads(meta_path.read_text())
        draft_dir = out / name / "skill-plus-plus" / "drafts" / meta["entry"]
        skill = draft_dir / "SKILL.md"
        if not skill.exists():
            print(f"-- {name}: no draft yet in {draft_dir}")
            continue
        log_path = draft_dir / "agent.log"
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        fixture = json.loads(Path(meta["fixture"]).read_text()) if Path(meta["fixture"]).exists() else None
        judged = load_judged(out / name, skill)
        rows = evaluate(skill.read_text(encoding="utf-8"), log, meta["entry"], fixture,
                        data["cases"][name], data["hedge"], judged)
        bad = [r for r in rows if not r["ok"]]
        failed += bool(bad)
        how = "judged" if judged is not None else "word lists"
        print(f"\n{'PASS' if not bad else 'FAIL'}  {name}  ({len(rows) - len(bad)}/{len(rows)}, method by {how})")
        for r in rows:
            print(f"   {'ok ' if r['ok'] else 'XX '} {r['id']:<14} {r['label']:<38} {r['evidence']}")
    return 1 if failed else 0


def judge(out: Path, only: list[str]) -> int:
    """Run the judge over every prepared draft, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    data = load_cases()
    jobs = []
    for name in only or list(data["cases"]):
        meta_path = out / name / "case.json"
        if not meta_path.exists():
            continue
        entry = json.loads(meta_path.read_text())["entry"]
        skill = out / name / "skill-plus-plus" / "drafts" / entry / "SKILL.md"
        if skill.exists():
            jobs.append((name, skill))
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda j: run_judge(j[0], j[1], data["cases"][j[0]], data), jobs))
    for record in records:
        (out / record["case"] / "judge.json").write_text(json.dumps(record, indent=1) + "\n")
        print(f"judged {record['case']}: exit {record['exit']}, {len(record['answers'])} answer(s)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="fold a case's session into its own ledger")
    p.add_argument("case")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--force", action="store_true")
    j = sub.add_parser("judge", help="have Claude answer the rubric for every prepared draft")
    j.add_argument("cases", nargs="*")
    j.add_argument("--out", type=Path, default=DEFAULT_OUT)
    c = sub.add_parser("check", help="check every prepared draft")
    c.add_argument("cases", nargs="*")
    c.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        return prepare(args.case, args.out.expanduser(), args.force)
    if args.cmd == "judge":
        return judge(args.out.expanduser(), args.cases)
    return check(args.out.expanduser(), args.cases)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
