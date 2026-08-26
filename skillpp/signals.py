"""Gap detection — where the trace is ambiguous, and what to ask about it.

README 4: the questions are generated *by the ambiguity in the trace*, never
from a fixed questionnaire. A clean candidate asks nothing; a messy one asks
precisely about the part that is messy.

Every question carries a pre-filled guess so the developer answers by
confirming rather than composing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from .normalize import normalize_command, step_shape

# Commands that change the world. Used both for the effect summary and for
# spotting a workflow that ends without a verification step.
DESTRUCTIVE = re.compile(
    r"(?:^|\s|/)(rm|rmdir|drop|truncate|delete|destroy|prune|reset\s+--hard|"
    r"push\s+--force|push\s+-f|kill|shutdown)\b", re.IGNORECASE)
MUTATING = re.compile(
    r"(?:^|\s|/)(deploy|apply|publish|release|push|upload|migrate|terraform|"
    r"kubectl|helm|ansible|npm\s+publish|docker\s+push)\b", re.IGNORECASE)
VERIFYING = re.compile(
    r"(?:^|\s|/)(test|check|status|curl|get|describe|logs|health|verify|"
    r"lint|assert|ps|diff)\b", re.IGNORECASE)
NETWORK = re.compile(
    r"(?:^|\s|/)(curl|wget|ssh|scp|rsync|http|git\s+(?:push|pull|fetch|clone)|"
    r"docker\s+(?:pull|push)|npm\s+(?:install|publish))\b", re.IGNORECASE)


@dataclass
class Question:
    kind: str
    text: str
    prefill: str
    evidence: str

    def to_dict(self) -> dict:
        return asdict(self)


def _command(step: dict) -> str:
    return str((step.get("input") or {}).get("command", ""))


def detect(entry) -> list[Question]:
    """Generate clarifying questions for a ledger entry, most valuable first."""
    if getattr(entry, "source", "capture") == "dictated":
        return dictation_gaps(entry)

    steps = entry.steps or []
    questions: list[Question] = _failure_retry(steps)

    # A retry already explains why that command varies. Asking about it again as
    # a divergence would burn one of the three question slots on a duplicate.
    explained = {q.text.split("`")[1] for q in questions if "`" in q.text}
    questions += [q for q in _divergence(entry)
                  if not any(shape in q.text for shape in explained)]

    questions += _off_trace_ending(steps)
    questions += _conditional_steps(entry)
    questions += _unparameterized_literals(steps)
    return questions


def _failure_retry(steps: list[dict]) -> list[Question]:
    """A command failed and a variant succeeded: the trace has the fix, not the
    diagnosis. Highest-value question there is (README 4)."""
    out: list[Question] = []
    for i, step in enumerate(steps):
        if not step.get("failed"):
            continue
        failed_cmd = _command(step)
        shape = normalize_command(failed_cmd)
        for later in steps[i + 1:]:
            if later.get("failed"):
                continue
            later_cmd = _command(later)
            if not later_cmd or normalize_command(later_cmd) != shape:
                continue
            if later_cmd.strip() == failed_cmd.strip():
                continue
            added = _added_tokens(failed_cmd, later_cmd)
            hint = f"`{added}`" if added else "the second form"
            out.append(Question(
                kind="failure_retry",
                text=(f"`{shape}` failed, then succeeded with {hint}. "
                      f"What tells you to reach for that — how do you recognise "
                      f"this failure?"),
                prefill="",
                evidence=f"failed: {failed_cmd}\nsucceeded: {later_cmd}",
            ))
            break
    return out


def _added_tokens(before: str, after: str) -> str:
    b, a = set(before.split()), after.split()
    added = [t for t in a if t not in b]
    return " ".join(added[:3])


def _divergence(entry) -> list[Question]:
    """The same step ran with different arguments across occurrences. That is
    exactly where the template variable belongs — ask for the rule."""
    variants = getattr(entry, "variants", None) or []
    if len(variants) < 2:
        return []
    out: list[Question] = []
    by_shape: dict[str, list[str]] = {}
    for variant in variants:
        for step in variant:
            if step.get("tool") != "Bash":
                continue
            cmd = _command(step).strip()
            by_shape.setdefault(normalize_command(cmd), []).append(cmd)
    for shape, commands in by_shape.items():
        distinct = list(dict.fromkeys(commands))
        if len(distinct) < 2:
            continue
        out.append(Question(
            kind="divergence",
            text=(f"`{shape}` ran differently across occurrences. Is that a "
                  f"parameter, or does one form supersede the other?"),
            prefill=distinct[-1],
            evidence="\n".join(f"- {c}" for c in distinct[:4]),
        ))
        if len(out) >= 2:
            break
    return out


def _off_trace_ending(steps: list[dict]) -> list[Question]:
    """The capture stops at a mutating step. The verification almost certainly
    happened somewhere the hook cannot see."""
    if not steps:
        return []
    tail = steps[-3:]
    mutating = [s for s in tail if s.get("tool") == "Bash" and MUTATING.search(_command(s))]
    if not mutating:
        return []
    if any(s.get("tool") == "Bash" and VERIFYING.search(_command(s)) for s in tail[-2:]):
        return []
    last = _command(mutating[-1]).strip()
    return [Question(
        kind="off_trace_ending",
        text=(f"The capture ends at `{normalize_command(last)}` with no check "
              f"after it. How do you know it worked?"),
        prefill="",
        evidence=f"last mutating step: {last}",
    )]


def recurring_steps(entry) -> list[dict]:
    """The steps that happened *every* time, in order.

    An episode is one occurrence of a procedure wrapped in that day's
    particulars. Observed on a real entry seen 11 times: the method was three
    steps — harvest the review markers, re-assemble, check word counts against
    budget — and the recorded episode held fourteen, the other eleven being one
    session's renames and edits.

    Recurrence is what separates them, and it is already paid for: `variants`
    holds up to four occurrences precisely so they can be compared, and
    `_conditional_steps` below already computes the intersection in order to
    ask about what falls outside it. This returns the inside.

    Fewer than two variants means nothing to compare, so everything is kept —
    a single occurrence has no evidence about which of its steps are incidental.
    """
    variants = getattr(entry, "variants", None) or []
    if len(variants) < 2:
        return list(entry.steps)
    common = set.intersection(*({step_shape(s) for s in v} for v in variants))
    kept = [s for s in entry.steps if step_shape(s) in common]
    # Never reduce to nothing, and never to something too thin to be a
    # procedure: an empty intersection means the occurrences disagree more than
    # they agree, and the honest answer there is to show the whole episode.
    return kept if len(kept) >= 2 else list(entry.steps)


def _conditional_steps(entry) -> list[Question]:
    """A step present in some runs but not others: conditional, or incidental?"""
    variants = getattr(entry, "variants", None) or []
    if len(variants) < 2:
        return []
    shape_sets = [{step_shape(s) for s in v} for v in variants]
    common = set.intersection(*shape_sets) if shape_sets else set()
    union = set.union(*shape_sets) if shape_sets else set()
    optional = union - common
    out: list[Question] = []
    for shape in sorted(optional):
        if shape.startswith(("read", "glob", "grep")):
            continue  # exploration noise, not workflow structure
        out.append(Question(
            kind="conditional_step",
            text=(f"`{shape}` appears in some runs but not others. Is it "
                  f"conditional, or was that a one-off?"),
            prefill="one-off — leave it out",
            evidence=f"present in {sum(1 for s in shape_sets if shape in s)}"
                     f"/{len(shape_sets)} occurrences",
        ))
        if len(out) >= 2:
            break
    return out


def _unparameterized_literals(steps: list[dict]) -> list[Question]:
    """A literal we replaced with ${ID} might be constant, or might vary."""
    out: list[Question] = []
    seen: set[str] = set()
    for step in steps:
        cmd = _command(step)
        for match in re.finditer(r"\b(\d{6,})\b", cmd):
            literal = match.group(1)
            if literal in seen:
                continue
            seen.add(literal)
            out.append(Question(
                kind="literal",
                text=(f"`{literal}` is hard-coded here. Is it constant across "
                      f"environments, or should it become a parameter?"),
                prefill="parameter",
                evidence=cmd.strip(),
            ))
            if len(out) >= 1:
                return out
    return out


# -- dictated candidates ---------------------------------------------------
#
# A dictated workflow has no trace, so there is nothing to mine for retries or
# divergence. What *is* mechanically checkable is completeness: whether the
# description covers trigger, procedure, output shape and failure handling.
# "give it to me in this format" names a format without supplying one, and that
# is detectable without understanding a word of the domain.

_TRIGGER_RE = re.compile(
    r"(?i)\b(when(?:ever)?|if|after|before|every\s+time|each\s+time|anytime|"
    r"on\s+\w+ing)\b")
_DANGLING_FORMAT_RE = re.compile(
    r"(?i)\b(?:in|into|using|follow(?:ing)?|per|with|to)\s+"
    r"(?:this|that|the\s+(?:same|usual|standard|normal|following))\s+"
    r"(?:format|structure|template|layout|style|shape|form|way)\b")
_FORMAT_GIVEN_RE = re.compile(r"(?m)(^\s*(?:[-*+]|\d+[.)])\s+\S)|```|^\s*\|")
_RESEARCH_RE = re.compile(
    r"(?i)\b(search|look\s?up|research|verify|fact.?check|google|cross.?check|"
    r"find\s+out|check\s+online)\b")
_SOURCE_CONSTRAINT_RE = re.compile(
    r"(?i)\b(from\s+\w|using\s+\w|via\s+\w|sources?|official|primary|"
    r"peer.?reviewed|at\s+least\s+\w+|\bcite|citation|reference)\b")
_FAILURE_RE = re.compile(
    r"(?i)\b(if\s+(?:not|no|none|nothing|there|you\s+can)|otherwise|fall\s?back|"
    r"fails?|failure|missing|conflict|contradict|unavailable|unsure|uncertain|"
    r"can(?:no|')t\s+find|don'?t\s+know)\b")


def dictation_gaps(entry) -> list[Question]:
    """Completeness check over a dictated workflow, most valuable gap first."""
    # The verbatim dictation, not a concatenation of it with its own parsed
    # steps — otherwise every evidence snippet shows the text twice.
    text = entry.intents[0] if entry.intents else " ".join(
        describe_text(s) for s in entry.steps)
    out: list[Question] = []

    if _DANGLING_FORMAT_RE.search(text) and not _FORMAT_GIVEN_RE.search(text):
        out.append(Question(
            kind="dangling_format",
            text=("You refer to a format but never give one. What should the "
                  "output actually look like — fields, order, an example?"),
            prefill="",
            evidence=_first_match(_DANGLING_FORMAT_RE, text),
        ))

    if not _TRIGGER_RE.search(text):
        out.append(Question(
            kind="missing_trigger",
            text=("When should this fire? The description line is the only "
                  "thing an agent sees before deciding to load the skill."),
            # No prefill: a trigger condition mangled out of the title reads
            # worse than nothing. The agent proposes one; the CLI only finds
            # that it is missing.
            prefill="",
            evidence="no trigger condition stated",
        ))

    if _RESEARCH_RE.search(text) and not _SOURCE_CONSTRAINT_RE.search(text):
        out.append(Question(
            kind="vague_sources",
            text=("What counts as a good enough source, and how many need to "
                  "agree before a fact is settled?"),
            prefill="two independent primary sources; cite both",
            evidence=_first_match(_RESEARCH_RE, text),
        ))

    if not _FAILURE_RE.search(text):
        out.append(Question(
            kind="missing_failure",
            # Phrased for any workflow, not just a command pipeline — "when a
            # step fails" reads oddly for something like a weekly email, where
            # the real edge case is having nothing to report.
            text=("What should happen when there is nothing to report, or a "
                  "step cannot be completed?"),
            prefill="say plainly what is missing; never invent filler",
            evidence="no failure handling stated",
        ))

    if len([s for s in entry.steps if describe_text(s)]) < 2:
        out.append(Question(
            kind="thin_procedure",
            text=("This is one step. What are the actual stages, in order?"),
            prefill="",
            evidence=f"{len(entry.steps)} step(s) parsed from the description",
        ))

    return out


def describe_text(step: dict) -> str:
    return str((step.get("input") or {}).get("text", "")).strip()


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    start = max(0, m.start() - 30)
    return "…" + text[start:m.end() + 30].strip() + "…"


def effects(steps: list[dict]) -> dict:
    """What the skill will *do* — the review surface (README 3.4).

    Effects, not purpose: a purpose summary can be accurate while the steps
    underneath are wrong. That is why the command itself is what this returns
    and what the proposal prints. ``describes`` sits beside it rather than
    replacing it — the agent's own note makes a wall of shell legible, but it
    is annotation, and a reader approving a destructive step must still be
    approving the command.
    """
    commands: list[str] = []
    writes: list[str] = []
    destructive: list[str] = []
    network: list[str] = []
    mcp: list[str] = []
    describes: dict[str, str] = {}

    for step in steps:
        tool = step.get("tool", "")
        payload = step.get("input") or {}
        if tool == "Bash":
            cmd = str(payload.get("command", "")).strip()
            if cmd:
                commands.append(cmd)
                note = " ".join(str(payload.get("description") or "").split())
                # First one wins, so this agrees with the deduped command list
                # below: the same command run twice can be described two ways,
                # and the review surface should not appear to contradict itself.
                if note:
                    describes.setdefault(cmd, note)
                if DESTRUCTIVE.search(cmd):
                    destructive.append(cmd)
                if NETWORK.search(cmd) or MUTATING.search(cmd):
                    network.append(cmd)
                for redirect in re.finditer(r">>?\s*([^\s;|&]+)", cmd):
                    writes.append(redirect.group(1))
        elif tool in ("Write", "Edit", "NotebookEdit"):
            target = str(payload.get("file_path", ""))
            if target:
                writes.append(target)
        elif tool.startswith("mcp__"):
            mcp.append(tool)

    dedup = lambda xs: list(dict.fromkeys(xs))  # noqa: E731 - order-preserving
    return {
        "commands": dedup(commands),
        "writes": dedup(writes),
        "destructive": dedup(destructive),
        "network": dedup(network),
        "mcp": dedup(mcp),
        # Added alongside rather than folded into `commands`, which stays a
        # plain list of strings. Every other key keeps its type, so the
        # destructive-operations block and `scaffold_skill` are untouched.
        "describes": describes,
    }
