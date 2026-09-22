"""Judging one episode: is this a method, or was it one particular job?

Segmentation answers *where* a task ended. It does not answer whether the task
was worth keeping, and those are different questions — a partition cannot
discard anything, so cutting a session more accurately still leaves every step
in some episode. Something has to be able to say no.

That is this module. It asks one question per episode and it is the only step
in the pipeline that can throw work away, which is why every uncertain answer
keeps the episode instead of dropping it: an episode wrongly kept costs one
line in a review list, an episode wrongly dropped is never seen again.

The question is deliberately not "did this finish". Finishing is easy to detect
and tells you nothing — a one-off fix ends in a commit exactly like a release
procedure does. See ``prompts/reusable.md``.
"""

from __future__ import annotations

from .local import (DEFAULT_HOST, DEFAULT_MODEL, LocalModelUnavailable,
                    PROMPTS, ask, yes_no)
from .normalize import strip_scaffolding

# No per-step character cap. There was one, at 200, and it guillotined exactly
# the steps that carry the most meaning: a `python3 - <<PY` heredoc renders as
# its first line and an ellipsis, which tells a reader nothing. Measured over
# 2988 real episodes, uncapped rendering peaks at ~8.2k tokens against the
# 16384 ceiling `local.ask` sizes to — no episode overflows. `_MAX_STEPS` is
# what bounds the prompt, and it stays.
_MAX_STEPS = 40


def render_step(step: dict) -> str:
    """One readable step — raw, not fingerprinted.

    Deliberately not ``normalize_command``: that reduces
    ``python3 -m unittest discover`` to ``python3``, and a model reading it then
    says, correctly, that nothing here ran any tests.

    A Bash step renders as two lines when the agent supplied a ``description``,
    with the description first. That ordering is the point rather than a
    cosmetic choice: ``sift`` asks whether a sequence is a *method* or one
    particular job, and a shell command states mechanism while the description
    states purpose. 87% of captured Bash calls carry one — written by the agent
    at call time, already scrubbed on the way in, and until now read by nothing.
    """
    tool = str(step.get("tool") or "?")
    payload = step.get("input") or {}
    mark = "!" if step.get("failed") else "$"
    if tool == "Bash":
        # Scaffolding out. Measured: realistic commands cost sift 16 points of
        # recall, and showing the description alone put all of it back — so the
        # noise is what costs, not the command. Stripping keeps something to
        # audit where description-only would leave a claim and nothing else.
        body = " ".join(strip_scaffolding(str(payload.get("command", ""))).split())
        note = " ".join(str(payload.get("description") or "").split())
        if note:
            return f"{mark} {note}\n        {body}"
        return f"{mark} {body}"
    if tool in ("Edit", "Write", "NotebookEdit"):
        body = f"{tool} {payload.get('file_path', '')}"
    elif tool == "UserPrompt":
        # `handle_prompt` writes "text"; this read "prompt" and so rendered
        # empty. Dead today — UserPrompt is in `_NOISE_TOOLS` and never reaches
        # `entry.steps` — but it would have failed silently the moment it was
        # not, so both keys are accepted.
        text = payload.get("text") or payload.get("prompt") or ""
        return "> " + " ".join(str(text).split())
    else:
        args = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2])
        body = f"{tool}({args})"
    return f"{mark} {' '.join(body.split())}"


def render(entry) -> tuple[str, str]:
    """An entry's stated intent and its steps, as the prompt wants them."""
    # Every stated intent, not the first three. The cap predated an episode
    # holding a whole task: on a real seven-turn session it handed the model
    # "did you call MCP for this?" — a question about the agent's own behaviour
    # — while dropping the closing check that one constant was still the single
    # source of truth, the verification step the procedure exists to perform.
    # `_intents_for` already bounds this by characters; capping again here only
    # loses the end of the task, which is where verification lives.
    ask_text = "\n    ".join(entry.intents) or entry.title or "(not recorded)"
    lines = [render_step(s) for s in entry.steps[:_MAX_STEPS]]
    if len(entry.steps) > _MAX_STEPS:
        lines.append(f"… and {len(entry.steps) - _MAX_STEPS} more steps")
    return ask_text, "\n    ".join(lines)


def is_reusable(entry, *, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST
                ) -> tuple[bool | None, str]:
    """Would someone follow these steps again for a different case?

    Returns ``(verdict, why)``. ``None`` means no opinion — the model was
    unreachable or would not commit — and every caller must treat that as keep.
    """
    ask_text, steps = render(entry)
    if not entry.steps:
        return None, "nothing recorded to judge"
    prompt = (PROMPTS / "reusable.md").read_text(encoding="utf-8")
    prompt = prompt.replace("{ASK}", ask_text).replace("{STEPS}", steps)
    try:
        reply = ask(model, prompt, host=host)
    except LocalModelUnavailable as exc:
        return None, f"no local model ({exc}); keeping"
    verdict = yes_no(reply)
    if verdict is None:
        return None, "no clear answer; keeping"
    return verdict, ("a method worth repeating" if verdict
                     else "one particular job, not a method")


# Twice is not an opinion. An episode the developer has actually performed more
# than once is behavioural evidence that it is a method, and it outranks
# anything a model reads off the steps — measured: `match(b)` banked the
# bug-filing procedure at x2 and the model discarded it anyway.
RECURRENCE_FLOOR = 2


def rank(entry, *, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST
         ) -> tuple[str, str]:
    """Order an entry for review: ``"method"``, ``"one-off"`` or ``""``.

    Cheap rules first, and they are not tie-breakers — they are the parts that
    hold. The model is asked only where no rule applies, and its answer is a
    hint for sorting rather than a verdict, because it is a good junk detector
    and a poor procedure detector: 11 of 11 junk entries dropped correctly, and
    4 of 6 real procedures dropped with them.
    """
    if entry.occurrences >= RECURRENCE_FLOOR:
        return "method", (f"done {entry.occurrences}x — recurrence, "
                          f"not an opinion")
    verdict, why = is_reusable(entry, model=model, host=host)
    if verdict is None:
        return "", why
    return ("method" if verdict else "one-off"), why


# Review order: what looks repeatable first, what a model doubted last, and
# anything unjudged in between rather than buried.
_ORDER = {"method": 0, "": 1, "one-off": 2}


def rank_key(entry) -> tuple[int, int]:
    return (_ORDER.get(entry.hint, 1), -entry.occurrences)
