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

# Long enough to show what a step did, short enough that thirty of them still
# fit a small context. Heredoc bodies and minified payloads are what make a
# single step run to kilobytes.
_MAX_STEP_CHARS = 200
_MAX_STEPS = 40


def render_step(step: dict) -> str:
    """One readable line for *step* — raw, not fingerprinted.

    Deliberately not ``normalize_command``: that reduces
    ``python3 -m unittest discover`` to ``python3``, and a model reading it then
    says, correctly, that nothing here ran any tests.
    """
    tool = str(step.get("tool") or "?")
    payload = step.get("input") or {}
    mark = "!" if step.get("failed") else "$"
    if tool == "Bash":
        body = str(payload.get("command", "")).replace("\n", " ⏎ ")
    elif tool in ("Edit", "Write", "NotebookEdit"):
        body = f"{tool} {payload.get('file_path', '')}"
    elif tool == "UserPrompt":
        return f"> {str(payload.get('prompt', ''))[:_MAX_STEP_CHARS]}"
    else:
        args = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2])
        body = f"{tool}({args})"
    body = " ".join(body.split())
    if len(body) > _MAX_STEP_CHARS:
        body = body[:_MAX_STEP_CHARS - 1] + "…"
    return f"{mark} {body}"


def render(entry) -> tuple[str, str]:
    """An entry's stated intent and its steps, as the prompt wants them."""
    ask_text = "\n    ".join(entry.intents[:3]) or entry.title or "(not recorded)"
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
