"""The review surface and the skill scaffold.

Effect summaries, not purpose summaries (README 3.4): the proposal leads with
what the skill will *do*, because a purpose summary can be perfectly accurate
while the steps underneath are wrong.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Config
from .ledger import Entry, describe_step
from .signals import Question, detect, effects


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
            lines.append(f"  run      {cmd}")
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
            lines.append(f"  (+{remaining} more — these become ## Known gaps)")
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


def scaffold_skill(
    entry: Entry,
    name: str,
    description: str = "",
    answers: dict[str, str] | None = None,
    tier: str = "provisional",
) -> str:
    """Deterministic starting point for a SKILL.md.

    The engine produces structure, dependencies and the verbatim steps. The
    judgement — prose, naming, when *not* to use it — is the agent's job at
    review time, editing this scaffold.
    """
    answers = answers or {}
    eff = effects(entry.steps)
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
        f"  requires_cli: {_yaml_list(entry.deps_cli)}",
        f"  requires_mcp: {_yaml_list(entry.deps_mcp)}",
        "---",
        "",
        f"# {heading}",
        "",
        "## When to use",
        "",
        answers.get("when_to_use", "<!-- TODO: replace with the real trigger condition -->"),
        "",
    ]

    if entry.deps_cli or entry.deps_mcp:
        lines += ["## Requirements", ""]
        for dep in entry.deps_cli:
            lines.append(f"- `{dep}` on PATH")
        for dep in entry.deps_mcp:
            lines.append(f"- MCP tool `{dep}`")
        lines += [
            "",
            "If a requirement is missing, state what is missing and stop. "
            "Do not improvise a workaround.",
            "",
        ]

    lines += ["## Steps", ""]
    for n, step in enumerate(entry.steps, 1):
        lines.append(f"{n}. {describe_step(step)}")
    lines.append("")

    if eff["destructive"]:
        lines += ["## Destructive operations", "",
                  "These steps change or remove state. Confirm before running:", ""]
        lines += [f"- `{c}`" for c in eff["destructive"]]
        lines.append("")

    answered = {k: v for k, v in answers.items() if k not in ("when_to_use",) and v}
    if answered:
        lines += ["## Judgement", ""]
        for key, value in answered.items():
            lines.append(f"- **{key.replace('_', ' ').capitalize()}:** {value}")
        lines.append("")

    closed = _answered_kinds(answers)
    open_questions = [q for q in detect(entry) if q.kind not in closed]
    if open_questions:
        lines += ["## Known gaps", "",
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
