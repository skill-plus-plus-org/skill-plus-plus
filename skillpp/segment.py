"""Cutting a session into task-sized episodes.

``fold_session`` used to fingerprint everything between session start and
``SessionEnd`` as one workflow. That holds only when a session contains exactly
one task. Surround the same workflow with unrelated work and its signature
stops resembling the next occurrence's, so a workflow performed three times is
filed as three unrelated one-offs and never reaches the recurrence threshold
(``tests/fixtures/messy_session.py`` measures it: 0.358–0.475 against a 0.85
threshold, for a deploy that sits intact and contiguous inside all three).

Two signals cut, both free and both local:

* a **completion marker** — a command whose success means the developer's goal
  is done, not merely that a step worked
* a **prompt** — the developer stating a new goal, recognised by position in
  the step stream rather than by any clock

No timers. A gap threshold would need tuning against real sessions and
misfires the moment somebody reads documentation mid-task.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import re

from .normalize import normalize_command, normalize_links

# What ends a task.
#
# The bar is deliberately high: a marker must mean *the developer's goal is
# done*, never *a step succeeded*. You do not commit halfway through a thought,
# so version-control verbs qualify.
#
# Excluded on purpose: ``terraform apply``, ``kubectl apply``, ``npm publish``,
# ``docker push``. They read like the most obvious "work landed" signals and
# they are the trap. In the fixture, ``terraform apply -lock=false`` succeeds
# and is followed by ``./scripts/deploy.sh <target>``; treating it as a marker
# closes the deploy one step early, leaves ``deploy.sh`` as a fragment the
# minimum-size rule discards, and — because the truncated prefix is identical
# across all three sessions — still merges to three occurrences. The result is
# a clean-looking candidate that builds and runs terraform but never deploys,
# with nothing to flag it as incomplete. Over-cutting mid-workflow is worse
# than under-cutting: an under-cut candidate is visibly wrong and dies at
# review, an over-cut one is silently missing its payload.
#
# Also excluded: tests going red→green. Green tests mean the goal was met only
# when testing *was* the goal; usually they are mid-task verification.
# ``signals.py`` already mines failure-then-retry for question generation,
# which is the right use of that pattern.
COMPLETION_MARKERS = frozenset({
    "git commit", "git push", "git merge", "git tag", "git revert",
    "gh pr create", "gh pr merge", "gh release create", "glab mr create",
})

# Commands that only inspect. Anchored on purpose: `grep` is a read,
# `vim $(grep ...)` is not. Ported from the capture branch, which is the one
# place that design got something this one lacked.
_READ_ONLY_RE = re.compile(
    r"(?i)\A(?:sudo\s+)?(?:grep|rg|ag|ack|cat|bat|head|tail|less|more|ls|ll|tree|"
    r"find|fd|wc|file|stat|du|df|ps|top|which|whereis|pwd|env|printenv|date|"
    r"man|type|echo|jq|column|sort|uniq|diff|cmp|"
    r"git\s+(?:log|show|status|diff|blame|branch|remote|config)|"
    r"kubectl\s+(?:get|describe|logs|top|explain|version)|"
    r"docker\s+(?:ps|images|logs|inspect)|"
    r"terraform\s+(?:plan|show)|npm\s+(?:ls|view)|pip\s+(?:show|list))\b")


# Tools whose invocation is itself the delivery of a finished thing.
ARTIFACT_TOOLS = frozenset({"SendUserFile"})

# The sentinel ``capture.handle_prompt`` writes into the step stream so the
# interleaving of prompts and steps survives to here.
PROMPT_TOOL = "UserPrompt"

@dataclass
class Episode:
    """One task's worth of steps, cut out of a session."""

    steps: list[dict] = field(default_factory=list)
    ended_by: str = "session-end"   # "marker" | "prompt" | "session-end"
    flagged: bool = False           # no marker, and the session did segment
    trimmed: int = 0                # leading read-only steps dropped

    @property
    def has_marker(self) -> bool:
        return any(is_marker(step) for step in self.steps)


def is_marker(step: dict) -> bool:
    """Does *step* complete a task?

    A failed marker does not: a commit rejected by a pre-commit hook means the
    task is still in progress, not finished.
    """
    if step.get("failed"):
        return False
    tool = step.get("tool", "")
    if tool in ARTIFACT_TOOLS:
        return True
    if tool != "Bash":
        return False
    command = str((step.get("input") or {}).get("command", ""))
    # Every link of the chain, not just the head. `normalize_command` keeps the
    # head because a signature must not move when someone prefixes a `cd` — but
    # the finishing verb is usually last, so reading the head called
    # `cd repo && git add -A && git commit` a `cd`. Measured on one real
    # session: 17 commits, 0 markers, and every boundary fell through to "the
    # developer typed something new".
    return any(link in COMPLETION_MARKERS for link in normalize_links(command))


def is_prompt(step: dict) -> bool:
    return step.get("tool") == PROMPT_TOOL


# MCP verbs that only retrieve. A tool call cannot be judged from its name in
# general, which is why this is a prefix list and not a rule: `search_messages`
# and `get_event` look, `send_message` and `append_rows` do not. Anything not
# recognised counts as work, so the failure direction is keeping an episode
# rather than discarding one.
_MCP_READ_VERBS = ("search", "list", "get", "read", "fetch", "find", "query",
                   "describe", "lookup")


def is_read_only(step: dict) -> bool:
    """Does *step* only look at things?

    Bash commands are matched against an anchored vocabulary. MCP calls are
    matched on the verb in their name, which is a heuristic and is allowed to
    be: an unrecognised call is treated as work, so a wrong guess keeps an
    episode rather than throwing one away.
    """
    tool = str(step.get("tool") or "")
    if tool.startswith("mcp__"):
        leaf = tool.split("__")[-1].lower()
        return leaf.startswith(_MCP_READ_VERBS)
    # The tools whose whole purpose is looking. Their absence here meant the
    # flagging rule below — "every substantive step only looked at things" —
    # could never fire on an episode containing a `Read`, which is most of them.
    # `reading-around` passed only because the trimmer happened to cut one step
    # and push it under the too-thin gate, not because anything recognised it as
    # pure exploration.
    if tool in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
        return True
    if tool != "Bash":
        return False
    command = str((step.get("input") or {}).get("command", ""))
    return bool(_READ_ONLY_RE.match(command.strip()))


def trim_leading_exploration(steps: list[dict],
                             min_steps: int = 2) -> tuple[list[dict], int]:
    """Drop the searching that *found* the task, keep the work that did it.

    A recipe's first real action changes something; the reads before it are how
    the developer located the problem, not how they solved it. Eight greps then
    a one-line fix is a two-step recipe, and the greps are noise in it.

    This matters twice over here. It shortens the episode, and it changes what a
    reader concludes from it: a nine-step episode whose method is three curls
    under six greps reads as one particular job, and reads as a method once the
    greps are gone. Measured — the same model flips its verdict.

    Only a *leading* run goes. Reads interleaved with real work stay, because
    those are usually genuine steps: "check the logs, then restart".

    Prompt sentinels are never cut. They carry the stated intent an episode is
    titled from, and dropping them would leave the episode named after a
    command.

    **An MCP retrieval is never cut either.** The argument above is a
    programming argument: greps that *located* a bug are not the fix. It does
    not transfer to work done through tools, where fetching the material is
    step one of the method rather than the search that found it — "pull the
    docs, build the agenda, share it" is a procedure whose first two steps are
    reads. Trimming them left `meeting-prep` as two steps out of five, and the
    model called it one particular job, correctly, on what it was shown.

    Narrow on purpose: this changes what the *trimmer* considers droppable and
    leaves `is_read_only` alone. The flagging rules downstream use it to spot an
    episode that only ever looked at things, and an all-MCP-reads episode is
    exactly that — teaching `is_read_only` to ignore MCP made `reading-around`
    bank a candidate it should have discarded.
    """

    def trimmable(step: dict) -> bool:
        if str(step.get("tool") or "").startswith("mcp__"):
            return False
        return is_read_only(step)

    prefix: list[dict] = []
    index = cut = 0
    while index < len(steps):
        step = steps[index]
        if is_prompt(step):
            prefix.append(step)
            index += 1
            continue
        if trimmable(step):
            cut += 1
            index += 1
            continue
        break
    remaining = prefix + steps[index:]
    if sum(1 for s in remaining if not is_prompt(s)) < min_steps:
        # Trimming to nothing is how an exploration-only episode would become a
        # two-step "recipe" of whatever happened to follow it. Leave it whole
        # and let the flagging rules deal with it.
        return steps, 0
    return remaining, cut


def segment(steps: list[dict], min_steps: int = 2,
            max_markerless: int = 0) -> list[Episode]:
    """Cut *steps* into episodes.

    *min_steps* counts steps that are not prompt sentinels — a boundary that
    would close an episode holding fewer than that is ignored instead. One step
    is not a workflow, and cutting anyway would strand it: a lone
    ``gh pr create`` would become an episode of its own and lose its declared
    dependency when the fragment was dropped.

    An episode is flagged — reported but not offered as a candidate — when it
    has no completion marker *and* ended only because the session did. That is
    work that trailed off: an investigation with nothing to show, the case the
    fixture's forty-minute ``kubectl`` stretch represents.

    An episode ending at a new prompt is not flagged even without a marker. The
    developer stating a fresh goal is itself evidence the previous one
    concluded — weaker than an artifact, but far stronger than a session dying
    mid-thought. This is what keeps a deploy (which ends in ``./scripts/
    deploy.sh``, not a commit) from being discarded.

    Flagging applies only when the session actually segmented. A session that
    did one thing start to finish needs no artifact to be believable.
    """
    episodes: list[Episode] = []
    current = Episode()

    def substantive_count(episode: Episode) -> int:
        return sum(1 for step in episode.steps if not is_prompt(step))

    def close(reason: str) -> None:
        nonlocal current
        current.ended_by = reason
        episodes.append(current)
        current = Episode()

    for step in steps:
        if is_prompt(step):
            # A new stated goal ends the preceding work, but only if there was
            # enough of it. Mid-task prompts ("continue", "fix that") are common
            # and must not shred an episode.
            if substantive_count(current) >= min_steps:
                close("prompt")
            # The sentinel belongs to the episode it opens, so the title can
            # come from a prompt inside the episode's own span.
            current.steps.append(step)
            continue

        current.steps.append(step)
        if is_marker(step) and substantive_count(current) >= min_steps:
            # Nothing after this point belongs to the finished task.
            close("marker")

    if substantive_count(current) > 0:
        episodes.append(current)

    for episode in episodes:
        episode.steps, episode.trimmed = trim_leading_exploration(
            episode.steps, min_steps)

    if len(episodes) > 1:
        for episode in episodes:
            # A trailing episode with no marker used to be flagged outright, on
            # the reasoning that it was an investigation that trailed off. That
            # holds only where markers exist: MCP work never produces a
            # `git commit`, so the rule silently discarded any session whose
            # last task was a productivity one — measured, the expenses half of
            # a session that also drafted an email. An episode that changed
            # something finished, whether or not a regex can see it, and pure
            # looking-around is caught below regardless of how it ended.
            episode.flagged = (
                episode.ended_by == "session-end"
                and not episode.has_marker
                and all(is_read_only(s) for s in episode.steps
                        if not is_prompt(s)))

    # A long stretch with nothing to show for itself. `max_markerless` is off
    # by default so callers opt in; `fold_session` passes the configured value.
    #
    # Deliberately conditioned on the absence of a marker. "Length is not the
    # failure; never finishing is" — a fifty-step migration ending in a commit
    # is one recipe. What this catches is the other shape: sixty steps that
    # ended only because the developer typed the next thing, which is what a
    # 433 KB session produced nine of, every one of them titled after whatever
    # was said at the top.
    if max_markerless:
        for episode in episodes:
            work = [s for s in episode.steps if not is_prompt(s)]
            if len(work) > max_markerless and not episode.has_marker:
                episode.flagged = True

    # An episode whose every substantive step only looked at things contains no
    # method, however it ended and however long it ran. Without this, a whole
    # session of reading around banks one candidate titled after the question
    # that started it — the single-episode case the flagging rule above
    # deliberately exempts, which is why it needs saying separately.
    for episode in episodes:
        work = [s for s in episode.steps if not is_prompt(s)]
        if work and all(is_read_only(s) for s in work):
            episode.flagged = True

    return episodes
