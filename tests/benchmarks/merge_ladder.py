#!/usr/bin/env python3
"""Score what matching embeds, one rendering at a time.

    python3 tests/benchmarks/merge_ladder.py R0          # today's conversation text
    python3 tests/benchmarks/merge_ladder.py R0 R1 R2    # a ladder, one row each

Every live session is banked with merging off, so each episode is its own entry.
Each rendering then turns an entry into the text that would be embedded, and the
fold is replayed in the order the sessions happened, at every floor from 0.50 to
0.99 — the same comparison `matching.find_same` makes, against each entry's
first run.

One row per rendering:

* **danger** — the highest score between two *different* procedures.
* **safe** — the first floor above the danger line (a hundredth above it). The
  floor a change would ship at: every wrong pair scores below it.
* **merged** — correct merges in the replay at the safe floor, of every
  same-procedure pair, and **wrong** merges there (0 by construction, printed
  as a check).
* **luck** — the lowest floor with no wrong merge in the replay. Below *safe*,
  it held only because of the order the sessions were folded in; shown, never
  chosen. (An earlier "margin" column subtracted the danger line from it, and
  read worse exactly when a change pulled same-procedure pairs up.)
* **2x2 gap** — runs 4, 5, 6 and 8: two procedures on two files. Worst
  same-procedure score minus best different-procedure score; positive means the
  text follows the procedure rather than the material.
* **3x** — families with an entry holding three or more runs at the safe floor.

A step on the ladder changes one thing. Nothing here decides; it prints.
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "fixtures" / "sessions"))

import recurrence                                   # noqa: E402
from skill_plus_plus.config import Config                   # noqa: E402
from skill_plus_plus.ledger import Entry, Ledger            # noqa: E402
from skill_plus_plus.local import cosine, embed             # noqa: E402
from skill_plus_plus.matching import (conversation_text,    # noqa: E402
                              turns_text)

# Two procedures on two source files — the pairs that separate procedure from material.
PRESENTATION = {"article": "3e035b46", "handoff": "bfb9ecdc"}
POST = {"article": "45a6f1a8", "handoff": "cb233c48"}

FILE = re.compile(r"\b[\w./-]+\.(md|py|json|pptx|ts|tsx|js|yaml|yml|txt|pdf|docx|xlsx)\b")


def r0(entry: Entry) -> str:
    """The baseline this ladder started from: prompts and whole replies, nothing
    masked. Frozen here, because `matching.turns_text` has since become R3."""
    return "\n\n".join(f"User: {t.get('prompt', '')}\nAgent: {t.get('reply', '')}"
                       for t in entry.turns)


def r1(entry: Entry) -> str:
    """R0 with every file name, in prompts and replies, replaced by `<file>`."""
    return FILE.sub("<file>", r0(entry))


LIST_ITEM = re.compile(r"^\s*([-*•+]|\d+[.)])\s+")
BOLD_LINE = re.compile(r"^\s*\*\*[^*].*\*\*:?\s*$")


def strip_deliverable(reply: str) -> str:
    """The reply without what it delivered: the agent's own sentences stay.

    Dropped: text between `---` separators (a drafted post), fenced code,
    table rows, headings, whole-line bold labels (slide titles), and list items
    with any lines indented under them.
    """
    out: list[str] = []
    in_fence = in_rule = in_item = False
    for line in reply.splitlines():
        text = line.strip()
        if text.startswith("```"):
            in_fence = not in_fence
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", text):
            in_rule = not in_rule
            continue
        if in_fence or in_rule:
            continue
        if not text:
            in_item = False
            continue
        if (text.startswith("|") or text.startswith("#") or BOLD_LINE.match(line)
                or LIST_ITEM.match(line) or (in_item and line[:1].isspace())):
            in_item = bool(LIST_ITEM.match(line)) or in_item
            continue
        in_item = False
        out.append(text)
    return "\n".join(out)


def r2(entry: Entry) -> str:
    """R1, with each reply's deliverable blocks removed.

    Measured, not kept: the two-procedures-two-files gap rose to +0.098, but the
    danger line moved to a coding pair (add-eval-case ~ compare-adk-docs, 0.854)
    whose replies carry their topic in plain sentences this cannot see, so the
    safe floor rose to 0.86 and merges fell from 12 to 10.
    """
    turns = [{**t, "reply": strip_deliverable(t.get("reply", ""))} for t in entry.turns]
    return FILE.sub("<file>", turns_text(turns))


REPLY_HEAD = 300


def r3(entry: Entry) -> str:
    """R1, with each reply cut to its first 300 characters. Frozen here: the
    shipped `matching.turns_text` has since grown the `Used:` line of A1."""
    turns = [{**t, "reply": " ".join(str(t.get("reply", "")).split())[:REPLY_HEAD], "used": []}
             for t in entry.turns]
    return FILE.sub("<file>", turns_text(turns))


def r4(entry: Entry) -> str:
    """R3 without the replies: `User: prompt` per turn, file names masked.

    Measured, not kept: merged 12 -> 19 and the 2x2 gap +0.161, but every added
    merge was a create-presentation pair, whose prompts were scripted and pasted
    verbatim. On the unscripted coding sessions it changed nothing (add-eval-case
    4/21 both). A rendering that reads only prompt wording is flattered by a
    corpus whose non-coding runs share that wording.
    """
    return FILE.sub("<file>", "\n\n".join(f"User: {t.get('prompt', '')}" for t in entry.turns))


def a1(entry: Entry) -> str:
    """R3 plus, per turn, the skills and MCP tools that turn used.

    The one fact a conversation does not carry: which skill did the work. It is
    already on the turn (`capture._turns`), costs one short line, and names a
    capability rather than a subject.

    Kept: merged 12 -> 13 of 61, danger 0.849 -> 0.848, the two-procedures-two-
    files gap +0.057 -> +0.071, and the two scripted presentation runs crossed
    the floor (0.844 -> 0.851) because both used `anthropic-skills:pptx`. Not
    measurable on this corpus: two *different* procedures sharing one skill —
    only one family here uses a skill at all. The unscripted runs built their
    decks with the `Artifact` tool, which is not a skill and adds no line.
    """
    return turns_text(entry.turns)          # what `matching` embeds per turn


DOC_EXT = re.compile(r"\.(pptx|pdf|docx|xlsx|html|md|csv)\b")


def deliverable(entry: Entry) -> str:
    """One line naming what the run produced, by kind, not by name."""
    exts = {Path(str((s.get("input") or {}).get("file_path") or "")).suffix
            for s in entry.steps if s.get("tool") in ("Write", "Edit", "NotebookEdit")}
    for step in entry.steps:
        exts |= {"." + m for m in DOC_EXT.findall(
            str((step.get("input") or {}).get("command") or ""))}
    exts = sorted(e for e in exts if e)
    sent = any(s.get("tool") == "SendUserFile" for s in entry.steps)
    return (f"Produced: {', '.join(exts) or 'nothing'}"
            + ("; handed the file over" if sent else ""))


def a2(entry: Entry) -> str:  # noqa: D401 - shipped; see matching.conversation_text
    """A1 plus one closing line: the kinds of file the run produced.

    Kept, marginally: merged 13 -> 14 of 61, danger and the two-procedures-two-
    files gap unchanged (0.849, +0.072). Both changes are inside the scripted
    presentation family — the two pptx runs score 0.856 — while the unscripted
    runs, which produced `.html, .json` through the Artifact tool, gained
    nothing. `.md` is produced by LinkedIn posts, coverage write-ups and eval
    cases alike, so this line says little outside file-producing work, and the
    lowest floor with no wrong merge fell to 0.81.
    """
    return conversation_text(entry.turns, entry.steps)


def tool_sequence(entry: Entry) -> str:
    """The tools in order, runs of one tool collapsed: `Read, Bash x3, Write`."""
    out: list[str] = []
    for step in entry.steps:
        tool = str(step.get("tool", "?"))
        if out and out[-1][0] == tool:
            out[-1][1] += 1
        else:
            out.append([tool, 1])
    return "Tools: " + ", ".join(t if n == 1 else f"{t} x{n}" for t, n in out)


def a3(entry: Entry) -> str:
    """A2 plus one line: the tools the run used, in order.

    Measured, not kept: merged 14/61 either way, danger unchanged, the gap
    +0.072 -> +0.075. Two ways of building a deck share no tokens — `Skill,
    Bash x4, Write, Bash x11, SendUserFile` against `Artifact x2, Bash, Write
    x7, Artifact x2` — so it only strengthens runs that already executed alike,
    which A1 and A2 covered; `Bash` and `Write` are in nearly every session.
    """
    return a2(entry) + "\n" + tool_sequence(entry)


def step_notes(entry: Entry) -> str:
    """The agent's own one-line description of each step, where it wrote one."""
    lines = []
    for step in entry.steps:
        payload = step.get("input") or {}
        note = " ".join(str(payload.get("description") or "").split())
        if not note:
            target = payload.get("file_path") or payload.get("path")
            note = f"{step.get('tool', '?')}" + (f" {Path(str(target)).name}" if target else "")
        lines.append(note)
    return "Steps: " + "; ".join(lines)


def a4(entry: Entry) -> str:
    """A2 plus the agent's step descriptions (A3, the tool sequence, was dropped).

    Measured, not kept: same-procedure pairs rose (2x2 0.856 -> 0.864) but so did
    one different-procedure pair, 0.849 -> 0.850, which pushes the safe floor to
    0.86 and costs two correct merges (14 -> 12 of 61). The descriptions are
    written in tool vocabulary — "Install pptxgenjs in scratchpad workspace"
    against "Write cover.html" — plus verbs every procedure's steps contain.
    """
    return a2(entry) + "\n" + FILE.sub("<file>", step_notes(entry))


# One sentence per turn from the local model, with the subject taken out. The
# question is deliberately about the *kind* of work: every deterministic
# ingredient so far (skills, files, tools, step notes) describes how a run was
# carried out, so two runs of one procedure through different tools stay apart.
ACTION_PROMPT = """A person asked an assistant for something, and the assistant answered.

ASKED:
{ASK}

ANSWERED:
{REPLY}

In one sentence of at most 20 words, say what was asked for and what the
assistant did. Describe the *kind* of work only. Do not name any file, product,
person, tool, topic or number. Start with a verb."""

ACTIONS = Path(tempfile.gettempdir()) / "skill-plus-plus-turn-actions.json"


def turn_action(turn: dict, config: Config, cache: dict) -> str:
    from skill_plus_plus.local import LocalModelUnavailable, ask
    ask_text = " ".join(str(turn.get("prompt", "")).split())[:600]
    reply = " ".join(str(turn.get("reply", "")).split())[:600]
    key = f"{ask_text}||{reply}"
    if key not in cache:
        prompt = ACTION_PROMPT.replace("{ASK}", ask_text or "(nothing)").replace("{REPLY}", reply or "(nothing)")
        try:
            cache[key] = " ".join(ask(config.local_model, prompt, host=config.ollama_url,
                                      timeout=60.0, think=False).split())
        except LocalModelUnavailable:
            cache[key] = ""
    return cache[key]


def a5(entry: Entry) -> str:
    """A2 plus one topic-free sentence per turn, written by the local model.

    Measured, not kept: same-procedure pairs rose (2x2 0.856 -> 0.876) and
    different-procedure pairs rose further (0.784 -> 0.813), so the danger line
    went 0.849 -> 0.855, the safe floor to 0.86, merges stayed at 14/61 and the
    gap fell to +0.063. Asked to describe the kind of work without subjects, a
    small model writes in one house style — "Generated a presentation outline…",
    "Drafted a short social media update…", "The user inquired…; the assistant…"
    — and the shared frame is itself similarity that no procedure earns. Also
    the slowest rendering by far: one model call per turn.
    """
    config = Config()
    cache = json.loads(ACTIONS.read_text()) if ACTIONS.exists() else {}
    before = len(cache)
    lines = [turn_action(t, config, cache) for t in entry.turns]
    if len(cache) != before:
        ACTIONS.write_text(json.dumps(cache))
    return a2(entry) + "\n\n" + "\n".join(f"Turn: {line}" for line in lines if line)


RENDERINGS = {
    "R0": r0,
    "R1": r1,
    "R2": r2,
    "R3": r3,
    "R4": r4,
    "A1": a1,
    "A2": a2,
    "A3": a3,
    "A4": a4,
    "A5": a5,
}


def bank_apart() -> tuple[list[dict], Ledger]:
    root = Path(tempfile.mkdtemp()) / "skill-plus-plus"
    config = Config(root)
    config.ensure_dirs()
    config.match_floor = config.match_floor_turns = 2.0   # nothing merges
    rows = recurrence.fold_all(config)
    return rows, Ledger(config)


def replay(rows: list[dict], vec: dict, floor: float) -> list[dict]:
    reps: list[str] = []
    out = []
    for row in rows:
        best = max(((cosine(vec[row["entry"]], vec[r]), r) for r in reps), default=(-1.0, None))
        if best[0] >= floor:
            out.append({**row, "entry": best[1]})
        else:
            reps.append(row["entry"])
            out.append(dict(row))
    return out


def score(name: str, render, rows: list[dict], ledger: Ledger, cache: dict) -> dict:
    vec = {}
    for row in rows:
        text = render(ledger.get(row["entry"])) or "(empty)"
        if text not in cache:
            cache[text] = embed(text)
        vec[row["entry"]] = cache[text]

    fam = {r["entry"]: r["family"] for r in rows}
    ids = [r["entry"] for r in rows]
    danger = max(cosine(vec[a], vec[b]) for i, a in enumerate(ids) for b in ids[i + 1:]
                 if fam[a] != fam[b])

    luck = None
    for step in range(50, 100):
        if recurrence.evaluate(replay(rows, vec, step / 100))["pairs"]["wrong"] == 0:
            luck = step / 100
            break
    safe = math.floor(danger * 100) / 100 + 0.01
    out = replay(rows, vec, safe)
    ev = recurrence.evaluate(out)
    sizes = Counter((o["family"], o["entry"]) for o in out)
    big = sorted({family for (family, _), n in sizes.items() if n >= 3})

    by_tag = {r["episode"].split("#")[0]: r["entry"] for r in rows}
    p = {k: vec[by_tag[t]] for k, t in PRESENTATION.items()}
    q = {k: vec[by_tag[t]] for k, t in POST.items()}
    same = [cosine(p["article"], p["handoff"]), cosine(q["article"], q["handoff"])]
    diff = [cosine(p["article"], q["article"]), cosine(p["handoff"], q["handoff"]),
            cosine(p["article"], q["handoff"]), cosine(p["handoff"], q["article"])]
    return {"name": name, "danger": danger, "safe": safe, "luck": luck,
            "merged": ev["pairs"]["correct"], "wrong": ev["pairs"]["wrong"],
            "should": ev["pairs"]["should_merge"],
            "gap": min(same) - max(diff), "same": same, "diff": diff, "big": big}


def main(argv: list[str]) -> int:
    names = argv or list(RENDERINGS)
    unknown = [n for n in names if n not in RENDERINGS]
    if unknown:
        print(f"unknown rendering(s): {unknown}; known: {list(RENDERINGS)}", file=sys.stderr)
        return 1
    rows, ledger = bank_apart()
    sessions = len({r["episode"].split("#")[0] for r in rows})
    print(f"{len(rows)} episodes from {sessions} live sessions\n")
    print(f"{'step':6} {'danger':>7} {'safe':>5} {'merged':>8} {'wrong':>5} {'luck':>5} {'2x2 gap':>8}  3x at safe")
    cache: dict = {}
    for name in names:
        s = score(name, RENDERINGS[name], rows, ledger, cache)
        print(f"{name:6} {s['danger']:7.3f} {s['safe']:5.2f} {s['merged']:>3}/{s['should']:<4} "
              f"{s['wrong']:>5} {s['luck'] or 0:5.2f} {s['gap']:+8.3f}  {', '.join(s['big']) or '-'}")
        print(f"       2x2 same procedure {' '.join(f'{x:.3f}' for x in s['same'])} | "
              f"different {' '.join(f'{x:.3f}' for x in s['diff'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
