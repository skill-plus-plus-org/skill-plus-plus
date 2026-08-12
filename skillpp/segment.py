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

from .normalize import normalize_command

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
    # normalize_command already reduces "git commit -m 'whatever'" to
    # "git commit", so this is a set lookup rather than a pile of regexes.
    return normalize_command(command) in COMPLETION_MARKERS


def is_prompt(step: dict) -> bool:
    return step.get("tool") == PROMPT_TOOL


def segment(steps: list[dict], min_steps: int = 2) -> list[Episode]:
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
            # enough of it. Mid-task prompts ("continue", "fix that") are
            # common and must not shred an episode.
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

    if len(episodes) > 1:
        for episode in episodes:
            episode.flagged = (episode.ended_by == "session-end"
                               and not episode.has_marker)

    return episodes
