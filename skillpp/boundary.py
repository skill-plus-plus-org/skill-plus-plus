"""Did the task end at this step? Asked of every tool call, as it happens.

`segment.is_marker` answers the same question from a fixed vocabulary, and that
vocabulary is entirely code: `git commit`, `gh pr create`, `glab mr create`.
Work done through tools has no entry in it, so a productivity session never ends
anything — which is why `_absorb_before_commit` can never fire on one, and why
9 of 24 benchmark cases hold no ending signal of any kind.

This asks a small local model instead, once per tool call, and records the
verdict on the step. Recording rather than re-deriving is what makes the rest
testable: the judgement is made once with the live context, and `segment`,
the fixtures and the tests all read a plain boolean afterwards.

**Every failure is a `None`.** No local model, a timeout, an answer that is
neither yes nor no — all return `None`, and `segment` treats `None` as "not an
ending". A boundary detector that stops a developer's session to think is worse
than one that misses a boundary.

Measured on `gemma3n:e4b`, warm, on this prompt:

* `think=False` matters: 11.5s unset against 4.2s with it off. Ollama turns
  thinking on by default where a model supports it.
* the one-word instruction matters more: 4.42s against **0.49s**, because
  generation length dominates, not prompt processing. The instruction in
  `prompts/task_end.md` is load-bearing for latency, not tidiness.
* ~0.73s per call with the full context prompt below.

Framings tried and rejected, on a 13-case probe:

* the step alone, no goal and no span — 4/13, and every one of its correct
  answers was a "no". It answered "no" to everything, which is exactly what
  `docs/benchmarks.md` recorded the first time this was attempted.
* the same, plus the goal and the steps behind it — also 4/13, same shape. The
  context alone changes nothing.
* adding what an *ending is* — 11/13. This is the whole difference.
* adding a deterministic prior ("that step only looked things up") for the model
  to confirm or override — 10/13, and it broke a case that had been passing.
  Not kept; noted here so it is not tried again.
"""

from __future__ import annotations

from .local import PROMPTS, LocalModelUnavailable, ask, yes_no

# How much of the span behind the step to show. Measured on the live sessions,
# lean rendering, everything else held: 6 steps scored 2/5, 10 scored 3/5, 20
# scored 4/5 — and 20 is where the one multi-task session first came out right,
# the shape that had collapsed to a single episode in every earlier run. The
# work that finished had simply scrolled out of a 6-step window.
#
# It buys nothing on the synthetic corpus, and that is not a contradiction:
# every task there runs 3-4 steps, so the whole of one already fits in a 6-step
# window and a wider one shows the model nothing new.
#
# Cost is bounded. The widest prompt across the live sessions is 8.5 KB, about
# 2.1k tokens, still under `local._CTX_FLOOR` — so `num_ctx` does not grow and
# nothing is truncated.
CONTEXT_STEPS = 20

# Short on purpose. This runs inside `PostToolUse`, so the developer waits for
# it. A model that has not answered in this long is not going to answer usefully.
DEFAULT_TIMEOUT = 5.0


# Verbs for the tools whose whole payload is a path.
_FILE_VERBS = {"Write": "wrote", "Edit": "edited", "NotebookEdit": "edited",
               "Read": "read"}

# A leftover value is context, not content. Long ones are usually a pasted body
# and push the prompt — and so the latency — up for nothing.
_VALUE_CHARS = 80

# Whether the developer's own `description` of a Bash step reaches the model.
#
# Off, and that is a measurement rather than an opinion. On the live sessions it
# scored 2/5 against 4/5 with it withheld, and it never fixed a session that was
# not already right; on the synthetic corpus it changed nothing at all — 16/24
# either way, the same cases failing. Both placements were tried, before the
# command and after it, and they produced byte-identical results, so it is not a
# phrasing effect: the extra detail itself pushes the model toward "delivered".
#
# Capture still stores it, and the ledger still shows it. This governs one thing
# — what the judge is shown. The benchmarks flip it to re-measure.
SEND_DESCRIPTION = False


def render_step(step: dict) -> str:
    """One clause describing what the developer just did.

    Reads as the continuation of "they ...", so the prompt stays a sentence
    rather than a table. `ledger.describe_step` renders for a human reading a
    ledger entry; this renders for a model judging completion, which wants the
    verb and the intent.

    **Nothing captured is dropped here.** An earlier version rendered from a
    per-tool branch and silently discarded everything it did not name — in one
    54-step live session that was every `Bash` step's `description` (35 of them,
    the developer's own statement of what the command was for) and every
    `Read`'s path, which rendered as the bare words "used Read". Capture already
    decided what was worth keeping, and scrubbed it; a renderer that then throws
    two thirds of it away is answering a question the model was never shown.

    So the tool-specific branch consumes the keys it knows how to phrase, and
    whatever is left is appended rather than lost.
    """
    tool = str(step.get("tool") or "")
    payload = {k: v for k, v in (step.get("input") or {}).items()
               if v not in (None, "")}
    intent = str(payload.pop("description", "")).strip()

    if tool == "Bash":
        command = str(payload.pop("command", "")).strip().replace("\n", " ")
        core = f"ran `{command}`"
    elif tool in _FILE_VERBS and "file_path" in payload:
        core = f"{_FILE_VERBS[tool]} the file `{payload.pop('file_path')}`"
    elif tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 2 else "an external"
        core = f"used the {server} tool to {parts[-1].replace('_', ' ')}"
    elif tool == "SendUserFile":
        core = "handed a file over to the developer"
    elif "file_path" in payload:
        core = f"used {tool} on `{payload.pop('file_path')}`"
    else:
        core = f"used {tool or 'a tool'}"

    rest = ", ".join(f"{key} {str(value)[:_VALUE_CHARS]}"
                     for key, value in payload.items())
    if rest:
        core = f"{core}, {rest}"
    if intent and SEND_DESCRIPTION:
        core = f"{intent} — {core}"
    if step.get("failed"):
        core = f"{core} — and it failed"
    return core


def build_prompt(goal: str, prior: list[str], step: dict) -> str:
    template = (PROMPTS / "task_end.md").read_text(encoding="utf-8")
    lines = "\n".join(f"    {p}" for p in prior) or "    (nothing yet)"
    return (template
            .replace("{GOAL}", goal.strip() or "(not stated)")
            .replace("{PRIOR}", lines)
            .replace("{STEP}", render_step(step)))


def judge(step: dict, *, goal: str = "", prior: list[str] | None = None,
          model: str, host: str, timeout: float = DEFAULT_TIMEOUT) -> bool | None:
    """Did *step* end the task? ``None`` means no opinion — treat as "no"."""
    prompt = build_prompt(goal, list(prior or []), step)
    try:
        reply = ask(model, prompt, host=host, timeout=timeout, think=False)
    except LocalModelUnavailable:
        return None
    return yes_no(reply)


def window(steps: list[dict]) -> tuple[str, list[str]]:
    """The task in progress: what was asked for, and what has been done since.

    The span runs from the last step judged an ending to now. That is causal —
    it reads verdicts already recorded, never a later one — so it means the same
    thing live in the hook as it does replaying a stored session.

    **The goal is every prompt in that span, in order.** Three shapes measured,
    on the live sessions at 20 steps of context:

    * the *latest* prompt alone — 0/5. A task is often stated across several
      prompts, so the last is a sub-step, and a sub-step is satisfied by one
      edit. It read 54 steps as 27 endings.
    * the *first* prompt of the span — 0/5 live, though 4/5 on the synthetic
      multi-task class against 0/5 for this one. Real sessions run 20+ steps
      under a handful of prompts, so once that first ask is satisfied every
      later span re-supplies it and the model keeps answering yes: 54 steps,
      17 episodes.
    * *every* prompt in the span — 4/5. It carries a known cost, a ratchet:
      miss one ending and the next prompt joins the same goal, so the question
      becomes "is every part done" over two tasks and is harder to answer yes
      than the first was. That is why its failures are all `got 1`. It is still
      the best measured, and the synthetic corpus cannot see its advantage
      because every task there is 3-4 steps long.
    """
    from .segment import is_prompt

    start = 0
    for index, step in enumerate(steps):
        if step.get("end") is True:
            start = index + 1
    def text(step: dict) -> str:
        return str((step.get("input") or {}).get("text", "")).strip()

    span = steps[start:]
    asked = [text(s) for s in span if is_prompt(s) and text(s)]
    if not asked:
        # An ending fired part-way through a task, so the span opens with no
        # prompt — the developer has not said anything new and the previous ask
        # still governs. Without this the goal renders "(not stated)" and the
        # model is asked whether a request it cannot see is finished; it says
        # yes, which fires another ending, which opens another promptless span.
        earlier = [text(s) for s in steps[:start] if is_prompt(s) and text(s)]
        asked = earlier[-1:]
    done = [render_step(s) for s in span if not is_prompt(s)]
    return "\n".join(f"- {a}" for a in asked), done[-CONTEXT_STEPS:]


def judge_in_session(config, session: dict, step: dict) -> bool | None:
    """`judge`, with the task read out of the live session buffer."""
    goal, done = window(session.get("steps", []))
    return judge(step, goal=goal, prior=done,
                 model=config.local_model, host=config.ollama_url)
