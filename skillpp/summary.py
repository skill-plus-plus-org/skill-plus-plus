"""The review surface and the skill scaffold.

Effect summaries, not purpose summaries (README 3.4): the proposal leads with
what the skill will *do*, because a purpose summary can be perfectly accurate
while the steps underneath are wrong.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import Config
from .ledger import Entry, describe_step
from .signals import Question, detect, effects, recurring_steps

PROMPTS = Path(__file__).resolve().parent / "prompts"


# -- the local model's name and sentence -----------------------------------
#
# One call, two surfaces: the name titles the candidate, the sentence is what
# the review page shows when a row is opened. Capture can otherwise only reuse
# a string it observed, which is how the real ledger ended up with 31 of 42
# entries titled after stray prompts — `.pptx` three times, `go`, `Commit`,
# `Looks good. What's next?`. The evidence handed to the model is the same
# either way, so asking for both at once costs nothing over asking for one.


def summaries_path(config: Config) -> Path:
    return config.root / "review_summaries.json"


def load_summaries(config: Config) -> dict:
    try:
        data = json.loads(summaries_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_summary(cache: dict, entry: Entry) -> str:
    """The cached sentence, if it still describes the entry as it stands."""
    hit = cache.get(entry.id) or {}
    return hit.get("text", "") if hit.get("steps") == len(entry.steps) else ""


def store_summary(config: Config, entry: Entry, text: str) -> None:
    """Cache the sentence beside the ledger, keyed on step count.

    Never in `entry.description`, which decides whether a promoted skill loads.
    """
    cache = load_summaries(config)
    cache[entry.id] = {"steps": len(entry.steps), "text": text}
    path = summaries_path(config)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# The model opens with "The developer" despite being told to start with a verb,
# and sometimes appends a labelled block of its own after the two lines. The
# bullet is defence rather than observation: asking for a name that starts with
# an "-ing verb" got a literal `-` or `-ing` in front of 17 of 42 names on the
# real ledger, and the instruction no longer spells it that way — but a model
# that writes a list anyway must not put the hyphen on the page.
_LEAD_IN_RE = re.compile(r"\A(the\s+)?developer\s+", re.IGNORECASE)
_LABELLED_RE = re.compile(r"\A\*\*[^*\n]+:\*\*")
_BULLET_RE = re.compile(r"\A(?:[-*+\u2022]+|\d+[.)])\s*")


def _clean(line: str) -> str:
    text = _BULLET_RE.sub("", line.strip().strip("*").strip())
    text = _LEAD_IN_RE.sub("", text.strip()).strip()
    return (text[0].upper() + text[1:]) if text else ""


# A name is listed on every row, so it is cut at a word rather than mid-word:
# `Crafting and refining LinkedIn announcements for a new article and l` is
# what a hard 70-character clip produced.
def _clip(text: str, limit: int = 70) -> str:
    if len(text) <= limit:
        return text.rstrip(" .,-")
    head = text[:limit]
    if " " in head:
        head = head[:head.rindex(" ")]
    return head.rstrip(" .,-") + "…"


def name_and_sentence(config: Config, entry: Entry) -> tuple[str, str]:
    """Ask the local model to name this procedure and say what the run did.

    Returns `(name, sentence)`; the name is empty when the model answered with
    one line only. Raises `LocalModelUnavailable` — the caller decides whether
    that is fatal (a fold keeps the title it derived itself, the review page
    reports it).
    """
    from .boundary import render_step as step_line
    from .local import ask

    asks = "\n".join(f"- {i.strip()[:200]}" for i in entry.intents[:6] if i.strip())
    reports = [st["closing_note"][:200] for st in entry.steps if st.get("closing_note")]
    steps = "\n".join(f"- {step_line(st)[:140]}" for st in entry.steps[:12])
    prompt = ((PROMPTS / "candidate_summary.md").read_text(encoding="utf-8")
              .replace("{ASKS}", asks or "- (none recorded)")
              .replace("{REPORTS}", "\n".join(f"- {r}" for r in reports[:4]) or "- (none)")
              .replace("{STEPS}", steps or "- (none)"))
    reply = ask(config.local_model, prompt, host=config.ollama_url,
                timeout=90.0, think=False)

    lines = [l.strip() for l in reply.strip().splitlines()
             if l.strip() and not _LABELLED_RE.match(l.strip())]
    name = _clean(lines[0]) if lines else ""
    sentence = _clean(lines[1]) if len(lines) > 1 else ""
    # A model that ignores the two-line shape answers with the sentence alone.
    # Take it as the sentence rather than the name: a wrong title is on every
    # row of the page, a missing one only costs the fallback.
    if not sentence:
        name, sentence = "", name
    # A model that writes `Name: the sentence` on one line has answered both
    # questions in the wrong shape; the name is the half before the colon.
    if not sentence and ": " in name:
        name, sentence = name.split(": ", 1)
    return _clip(name), sentence


def render_proposal(entry: Entry, config: Config) -> str:
    """The text a developer reads before approving. Effects first, evidence
    second, questions last."""
    if getattr(entry, "source", "capture") == "dictated":
        return _render_dictated(entry, config)

    eff = effects(entry.steps)
    lines: list[str] = []
    lines.append(f"Candidate {entry.id} — seen {entry.occurrences}×")
    lines.append(f"  {entry.title}")
    lines.append("")

    lines.append("WHAT IT WILL DO")
    if eff["commands"]:
        for cmd in eff["commands"][:12]:
            # The command stays the thing being approved; the agent's own note,
            # where it left one, goes underneath so a screenful of shell can be
            # read at a glance.
            lines.append(f"  run      {cmd}")
            note = eff.get("describes", {}).get(cmd)
            if note:
                lines.append(f"           ↳ {note}")
        if len(eff["commands"]) > 12:
            lines.append(f"           … and {len(eff['commands']) - 12} more")
    for target in eff["writes"][:8]:
        lines.append(f"  writes   {target}")
    for tool in eff["mcp"]:
        lines.append(f"  mcp      {tool}")
    if eff["network"]:
        lines.append(f"  network  {len(eff['network'])} step(s) reach outside this machine")
    if eff["destructive"]:
        lines.append("")
        lines.append("  ⚠ DESTRUCTIVE")
        for cmd in eff["destructive"][:5]:
            lines.append(f"    {cmd}")
    lines.append("")

    if entry.deps_cli or entry.deps_mcp:
        lines.append("REQUIRES")
        if entry.deps_cli:
            lines.append(f"  cli  {', '.join(entry.deps_cli)}")
        if entry.deps_mcp:
            lines.append(f"  mcp  {', '.join(entry.deps_mcp)}")
        lines.append("")

    lines.append("DERIVED FROM")
    for intent in entry.intents[:3]:
        lines.append(f"  “{intent.splitlines()[0][:100]}”")
    lines.append(f"  {len(entry.steps)} steps across {entry.occurrences} occurrence(s)")
    lines.append("")

    questions = detect(entry)[: config.max_questions]
    if questions:
        lines.append(f"OPEN QUESTIONS ({len(questions)})")
        for n, q in enumerate(questions, 1):
            lines.append(f"  {n}. {q.text}")
            if q.prefill:
                lines.append(f"     suggested: {q.prefill}")
            for ev in q.evidence.splitlines()[:3]:
                lines.append(f"     ↳ {ev}")
        lines.append("")
    return "\n".join(lines)


def _render_dictated(entry: Entry, config: Config) -> str:
    """A dictated candidate has no trace, so there are no effects to summarise.
    Show what was said, and what it does not yet say."""
    lines = [f"Candidate {entry.id} — dictated", f"  {entry.title}", ""]
    lines.append("WHAT YOU DESCRIBED")
    for n, step in enumerate(entry.steps, 1):
        lines.append(f"  {n}. {describe_step(step)}")
    lines.append("")

    questions = detect(entry)[: config.max_questions]
    remaining = len(detect(entry)) - len(questions)
    if questions:
        lines.append(f"NOT YET SPECIFIED ({len(questions)})")
        for n, q in enumerate(questions, 1):
            lines.append(f"  {n}. {q.text}")
            if q.prefill:
                lines.append(f"     suggested: {q.prefill}")
            if q.evidence:
                lines.append(f"     ↳ {q.evidence}")
        if remaining > 0:
            lines.append(f"  (+{remaining} more — these become ## Open questions)")
        lines.append("")
    else:
        lines.append("NOT YET SPECIFIED\n  nothing — the description is complete.\n")
    return "\n".join(lines)


def questions_for(entry: Entry, config: Config) -> list[Question]:
    """At most ``max_questions``. If synthesis has more than that, the candidate
    is not ready — the count is a quality signal, not a budget (README 4)."""
    return detect(entry)[: config.max_questions]


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


# Readable answer keys, so callers need not memorise internal question kinds.
# Answering under either name closes the gap.
_ANSWER_ALIASES = {
    "when_to_use": "missing_trigger",
    "trigger": "missing_trigger",
    "output_format": "dangling_format",
    "format": "dangling_format",
    "sources": "vague_sources",
    "failure": "missing_failure",
    "failure_handling": "missing_failure",
    "steps": "thin_procedure",
    "diagnosis": "failure_retry",
    "parameter": "divergence",
    "verification": "off_trace_ending",
}


def _answered_kinds(answers: dict[str, str]) -> set[str]:
    """Question kinds closed by the supplied answers."""
    closed: set[str] = set()
    for key, value in answers.items():
        if not value:
            continue
        closed.add(key)
        if key in _ANSWER_ALIASES:
            closed.add(_ANSWER_ALIASES[key])
    return closed


def _cli_of(entry, steps: list[dict]) -> list[str]:
    """Dependencies of the steps actually written into the skill.

    `entry.deps_cli` accumulates across every occurrence, so after
    `recurring_steps` trims an episode it can still name programs from steps
    that are no longer in the skill. Narrow it to what the kept steps use — but
    only when something was trimmed, and only when the narrowing finds
    anything: `deps_cli` also holds programs a path-invoked script needs, which
    cannot be re-derived from the command text.
    """
    from .capture import _cli_dependencies
    from .lifecycle import COREUTILS, PROGRAM_RE, SHELL_BUILTINS

    # Filtered here, not only where it is parsed: entries banked before the
    # parser learned that a heredoc body is not shell keep their junk on disk
    # forever, and one real candidate reached a skill declaring `')` and
    # `console.log('ok')"` as requirements.
    clean = [dep for dep in entry.deps_cli
             if PROGRAM_RE.match(dep) and dep not in SHELL_BUILTINS
             and dep not in COREUTILS]
    if len(steps) == len(entry.steps):
        return clean
    narrowed = sorted(set(clean) & _cli_dependencies(steps))
    return narrowed or clean


# The marker a facts-only scaffold leaves where the procedure belongs. Fixed
# text on purpose: skillpp's own `<!-- TODO: replace with the real trigger
# condition -->` shipped verbatim into two real skills because nothing could
# tell a finished draft from an untouched one.
WRITE_HERE = "<!-- skillpp:write-the-procedure -->"


def scaffold_skill(
    entry: Entry,
    name: str,
    description: str = "",
    answers: dict[str, str] | None = None,
    tier: str = "provisional",
    *,
    body: str = "auto",
    limit: int | None = None,
) -> str:
    """Deterministic starting point for a SKILL.md.

    The engine produces the facts — frontmatter, declared dependencies, the
    destructive-operations warning. Whether it also produces a *procedure*
    depends on what the candidate holds:

    * **facts** — the run was steered through conversation, so `entry.turns`
      has the method and the tool calls were only how it was carried out.
      Writing the steps out here hands the agent a finished-looking document
      to leave alone; both of the first real drafts came back as pure scaffold,
      TODO comment and all. The agent writes the procedure from the turns.
    * **full** — no turns, so the steps are all the evidence there is.

    `auto` chooses on `bool(entry.turns)`. Resolved here rather than in
    `cmd_scaffold` so every caller gets it and a new one cannot get it wrong.
    Requirements and destructive operations stay in both modes: they are
    derived from tool calls, which `show --json --draft` withholds, so dropping
    them would lose the "if a requirement is missing, stop" contract with no
    way for the agent to recover it.
    """
    full = body == "full" or (body == "auto" and not getattr(entry, "turns", None))
    answers = answers or {}
    # The steps that happened every time, not the ones that happened once.
    # Where an entry has only been seen once there is nothing to compare and
    # this is the whole episode.
    steps = recurring_steps(entry)
    eff = effects(steps)
    # One list for the frontmatter and the Requirements section. They used to
    # disagree: the bullets iterated `entry.deps_cli`, which is unioned across
    # every occurrence while `entry.steps` keeps one run's, so a real skill
    # listed 27 requirements where its own steps justified 15.
    cli = _cli_of(entry, steps)
    dictated = getattr(entry, "source", "capture") == "dictated"
    desc = description or f"{entry.title}. Use when repeating this workflow."
    # A dictated entry's title is the raw description, which makes a poor
    # heading. The skill name reads better.
    heading = name.replace("-", " ").replace("_", " ").title() if dictated else entry.title

    lines = [
        "---",
        f"name: {name}",
        f"description: {json.dumps(desc)}",
        "metadata:",
        '  source: "skill-plus-plus"',
        f'  provenance: "ledger:{entry.id}"',
        f'  tier: "{tier}"',
        f"  occurrences: {entry.occurrences}",
        f"  requires_cli: {_yaml_list(cli)}",
        f"  requires_mcp: {_yaml_list(entry.deps_mcp)}",
        "---",
        "",
        f"# {heading}",
        "",
    ]
    if full:
        lines += ["## When to use", "",
                  answers.get("when_to_use",
                              "<!-- TODO: replace with the real trigger "
                              "condition -->"),
                  ""]

    if cli or entry.deps_mcp:
        lines += ["## Requirements", ""]
        for dep in cli:
            lines.append(f"- `{dep}` on PATH")
        for dep in entry.deps_mcp:
            lines.append(f"- MCP tool `{dep}`")
        lines += [
            "",
            "If a requirement is missing, state what is missing and stop. "
            "Do not improvise a workaround.",
            "",
        ]

    if full:
        lines += ["## Steps", ""]
        for n, step in enumerate(steps, 1):
            lines.append(f"{n}. {describe_step(step)}")
        lines.append("")
    else:
        lines += [WRITE_HERE, "",
                  "Write the procedure here, from the turns in "
                  "`skillpp show <id> --json --draft`: what was asked, what "
                  "came back, which skills did the work. Leave the frontmatter "
                  "and the sections above as they are.", ""]

    if eff["destructive"]:
        lines += ["## Destructive operations", "",
                  "These steps change or remove state. Confirm before running:", ""]
        lines += [f"- `{c}`" for c in eff["destructive"]]
        lines.append("")

    answered = {k: v for k, v in answers.items() if k not in ("when_to_use",) and v}
    if full and answered:
        lines += ["## Judgement", ""]
        for key, value in answered.items():
            lines.append(f"- **{key.replace('_', ' ').capitalize()}:** {value}")
        lines.append("")

    closed = _answered_kinds(answers)
    # Capped like `questions_for` does: these now block a draft's download, and
    # an unbounded list is a wall rather than a review.
    open_questions = [q for q in detect(entry) if q.kind not in closed]
    if limit is not None:
        open_questions = open_questions[:limit]
    if full and open_questions:
        lines += ["## Open questions", "",
                  "Unresolved at approval time. Close these the first time the "
                  "skill is run and the branch is hit.", ""]
        lines += [f"- {q.text}" for q in open_questions]
        lines.append("")

    lines += [
        "---",
        "",
        (f"_Described by the developer and scaffolded by Skill Plus Plus. "
         f"Tier: {tier}._" if dictated else
         f"_Captured by Skill Plus Plus from {entry.occurrences} observed "
         f"occurrence(s). Tier: {tier}._"),
        "",
    ]
    return "\n".join(lines)


def check_dependencies(deps_cli: list[str], deps_mcp: list[str],
                       cwd: Path | None = None) -> dict:
    """Dependency check at pull time, not at run time (README 5).

    Failing at install is cheap; failing halfway through a deploy is not.
    """
    cwd = Path(cwd or Path.cwd())
    missing_cli = [d for d in deps_cli if shutil.which(d) is None]

    configured: set[str] = set()
    for candidate in (cwd / ".mcp.json", Path.home() / ".claude.json",
                      cwd / ".claude" / "settings.json"):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers") or {}
        if isinstance(servers, dict):
            configured.update(servers.keys())

    missing_mcp = []
    for tool in deps_mcp:
        parts = tool.split("__")
        server = parts[1] if len(parts) > 2 else tool
        if server not in configured:
            missing_mcp.append(tool)

    return {
        "ok": not missing_cli and not missing_mcp,
        "missing_cli": missing_cli,
        "missing_mcp": missing_mcp,
        "known_servers": sorted(configured),
    }
