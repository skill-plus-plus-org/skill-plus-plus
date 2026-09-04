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
    # `git -C <path> status` is as read-only as `git status`. Anchoring
    # straight on the subcommand missed 76 such calls in real transcripts and
    # counted every one as work. Same cause as the marker bug fixed in
    # `normalize._skip_pre_subcommand_flags`.
    r"git\s+(?:(?:-C|-c|--git-dir|--work-tree|--namespace)\s+\S+\s+|"
    r"(?:--no-pager|--bare|--literal-pathspecs)\s+)*"
    r"(?:log|show|status|diff|blame|branch|remote|config)|"
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
    # "judged" | "marker" | "prompt" | "session-end"
    ended_by: str = "session-end"
    flagged: bool = False           # no ending, and the session did segment
    trimmed: int = 0                # leading read-only steps dropped

    @property
    def has_marker(self) -> bool:
        return any(is_end(step) for step in self.steps)


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


def was_judged(steps: list[dict]) -> bool:
    """Did something judge these steps as they were captured?

    A step carries ``end`` only if `boundary.judge_in_session` answered for it.
    Sessions recorded before that existed, and every hand-written fixture, carry
    no verdict at all — so the vocabulary rules below stay live for them rather
    than silently reading every old stream as one unbroken episode.
    """
    return any("end" in step for step in steps if not is_prompt(step))


def is_end(step: dict, judged: bool | None = None) -> bool:
    """Did *step* finish a task?

    Where a verdict was recorded, that is the answer. Where none was — an older
    session, a fixture, a model that was unreachable at the time — fall back to
    `is_marker`'s vocabulary. Passing *judged* decides it for a whole stream at
    once; leaving it `None` decides per step, which is what `Episode.has_marker`
    needs since it holds steps and not the stream they came from.
    """
    if judged is None:
        judged = "end" in step
    if judged:
        return step.get("end") is True
    return is_marker(step)


def is_prompt(step: dict) -> bool:
    return step.get("tool") == PROMPT_TOOL


# How far ahead to look for the write a `Read` fed. Read-then-edit is usually
# adjacent; a couple of steps of slack covers a read, a check, then the edit.
READ_FEEDS_WINDOW = 3

_WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")


def feeds_a_write(steps: list[dict], index: int,
                  window: int = READ_FEEDS_WINDOW) -> bool:
    """Does the `Read` at *index* name the file a nearby write then changes?

    Lives here, and not in `capture`, because two rules need the same answer and
    for a while they disagreed. `capture._substantive` kept such a read; then
    `trim_leading_exploration` cut it again whenever it happened to open an
    episode, because it only asked whether the step was read-only. The read that
    names the file a procedure operates on is not exploration in one position
    and procedure in another.
    """
    step = steps[index]
    if step.get("tool") != "Read":
        return False
    path = (step.get("input") or {}).get("file_path")
    if not path:
        return False
    ahead = steps[index + 1:index + 1 + window]
    return any(s.get("tool") in _WRITE_TOOLS
               and (s.get("input") or {}).get("file_path") == path
               for s in ahead)


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
    # `ToolSearch` loads a tool's schema and `AskUserQuestion` asks the
    # developer something. Both are lookups. Their absence made the episode
    # that fetched the framework docs register as work that had produced
    # something, which kept it from folding into the commit it belonged to.
    if tool in ("Read", "Glob", "Grep", "WebFetch", "WebSearch",
                "ToolSearch", "AskUserQuestion"):
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

    def trimmable(position: int) -> bool:
        step = steps[position]
        if str(step.get("tool") or "").startswith("mcp__"):
            return False
        # The read that named the file about to be written is step one of the
        # procedure, wherever it falls. `capture._substantive` already keeps it;
        # cutting it here because it happens to come first undid that, and did
        # it silently — the episode simply started at the edit.
        if feeds_a_write(steps, position):
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
        if trimmable(index):
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


def _absorb_before_commit(episodes: list[Episode]) -> list[Episode]:
    """Fold markerless episodes into the commit that closed their work.

    A prompt arriving mid-task closes an episode on the strength of a step
    count, and a step count measures that work *happened*, not that a goal
    *ended*. Any instruction taking more than one tool call satisfies it.

    Measured on a real six-turn session — fetch the framework docs, add a test
    case, regenerate the generated file, verify a decision record, commit — the
    prompt rule cut five times and banked five fragments plus the commit, none
    of them the procedure. One fragment was the documentation lookup on its
    own, severed from the work it exists to inform.

    The evidence those cuts lacked is the commit, and it does not exist until
    afterwards, so this is a pass over the finished list rather than a rule
    inside the loop.

    Deliberately not "the episode produced nothing" — the doc lookup and the
    decision-record check produce no file and are the two steps a person would
    follow this procedure again *for*. The test is whether the episode ever
    concluded on its own, which is what a marker means.

    KNOWN COST, accepted deliberately: a task that completes without committing
    — a deploy ending in `./scripts/deploy.sh` — is absorbed into whatever
    commits next. `tests/fixtures/messy_session.py` s2 is exactly that shape and
    its deploy count drops from 3 to 2. That fixture is hand-authored and its
    real-world frequency is unmeasured; the session this rule was built from is
    real. Revisit when a real session shows the deploy shape.
    """
    out: list[Episode] = []
    pending: list[Episode] = []
    for episode in episodes:
        if episode.has_marker:
            episode.steps = [s for p in pending for s in p.steps] + episode.steps
            out.append(episode)
            pending = []
        else:
            pending.append(episode)
    # Work after the last commit keeps its own boundary. An investigation that
    # trailed off is not part of the commit that preceded it.
    out.extend(pending)
    return out


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

    Length is deliberately not a flag. A `max_markerless_steps` setting used to
    flag any episode past 25 steps that carried no marker. Measured against the
    live sessions it never fired on the vocabulary path — every session long
    enough to trip it ends in a marker — and on the judged path it emptied three
    ledgers outright: the judge found no ending, the whole session became one
    markerless episode, and the rule threw it away. Removing it took the judge
    from 3 fixed / 3 broken to 3 fixed / 0 broken. See `docs/benchmarks.md`.
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

    judged = was_judged(steps)

    for step in steps:
        if is_prompt(step):
            # A new stated goal ends the preceding work, but only if there was
            # enough of it. Mid-task prompts ("continue", "fix that") are common
            # and must not shred an episode.
            #
            # Only where nothing judged the steps. A prompt is a proxy for "the
            # last thing must have finished", and it is a poor one — it cuts on a
            # step count, which measures that work happened, not that a goal
            # ended. Where a verdict exists it answers the question directly, and
            # the sentinel goes back to being context and a source of titles.
            if not judged and substantive_count(current) >= min_steps:
                close("prompt")
            # The sentinel belongs to the episode it opens, so the title can
            # come from a prompt inside the episode's own span.
            current.steps.append(step)
            continue

        current.steps.append(step)
        if is_end(step, judged) and substantive_count(current) >= min_steps:
            # Nothing after this point belongs to the finished task.
            close("marker" if not judged else "judged")

    if substantive_count(current) > 0:
        episodes.append(current)

    # Before trimming, not after: `trim_leading_exploration` only cuts a
    # *leading* run, so running it on the fragments first treats every mid-task
    # prompt as the start of a task. On the session this was written for that
    # discarded eleven steps of real work — the greps that worked out the file
    # format, the `git diff` before committing — while keeping sixteen steps of
    # opening exploration, because trimming those whole would have left nothing.
    episodes = _absorb_before_commit(episodes)

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
