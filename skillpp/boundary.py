"""Did the developer start a new job here? Asked once per prompt, at session end.

`segment.is_marker` answered the same question from a fixed vocabulary, and that
vocabulary is entirely code: `git commit`, `gh pr create`, `glab mr create`.
Work done through tools has no entry in it, so a productivity session never ends
anything, and 9 of 24 benchmark cases hold no ending signal at all. That
vocabulary no longer decides boundaries anywhere — it survives as the trailing
flag's test and as the suite's stand-in judge.

This asks a small local model instead, and records the verdict on the step.
Recording rather than re-deriving is what makes the rest testable: `segment`,
the fixtures and the tests all read a plain boolean afterwards.

**It used to ask, once per tool call, whether the whole request was finished.**
Measured across the eleven live sessions that answered 3 endings in 357 steps,
declined a completed `git commit` it accepted in a neighbouring session, and
missed `241955c7` entirely. `docs/benchmarks.md` lists what was ruled out —
four rewordings of the definition, the tool output, the completion report, two
other phrasings, both polarities, three context sizes. The polarity test settled
it: 0/24 endings asking "is the request done" against 17/24 asking "is more work
needed", same steps, same context, temperature 0. It was answering the shape of
the question, not reading the step.

So the question moved to where a boundary can actually be — a gap between two
tool calls that a prompt landed in — and became a comparison rather than an
assessment: here is what they asked for, here is what they just said, here is
what they did next; is that a new job? 10/11 on the corpus at **33 calls
instead of 357**, and it finds `241955c7` and `263d65ce` in the right places
where the old one found only `263d65ce`.

**Every failure is a `None`.** No local model, a timeout, an answer that is
neither yes nor no — all return `None`, and `segment` treats `None` as "not an
ending". A boundary detector that stops a developer's session to think is worse
than one that misses a boundary.

Measured on `gemma3n:e4b`, warm, on this prompt:

* the one-word instruction is what buys the latency: 4.42s against **0.49s**,
  because generation length dominates, not prompt processing. The instruction in
  `prompts/task_end.md` is load-bearing, not tidiness.
* ~0.73s per call with the full context prompt below.
* `think=False` costs nothing and buys nothing *here*. An earlier note in this
  docstring claimed 11.5s unset against 4.2s with it off; re-measured warm, three
  runs each, it is 1.10s against 1.09s — the original 11.5s was the model
  loading. `gemma3n:e4b` has no thinking capability at all and `think=True`
  returns HTTP 400. The flag stays because it is free and because it matters
  enormously on a model that *can* think: `local.ask` records 113.8s against
  0.5s on `qwen3.5:9b` for the same one-word question.

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
# How many steps of context each slot carries. Both are windows, not knobs, and
# both were measured across the eleven live sessions.
#
# `NEXT_STEPS` is the sharp one: 1 step scores 7/11, 3 scores 10/11, 5 scores
# 8/11. One step is too little to tell two jobs apart — `find cases.json` could
# belong to either. Five reaches far enough into the next task to echo the old
# one, and sessions start failing again.
#
# `PRIOR_STEPS` at 3 beat the old 20: a long history made a late gap look like a
# continuation whatever it said. Proved by swapping the text between an early
# and a late gap, holding everything else — the verdict followed the position,
# not the words.
NEXT_STEPS = 3
PRIOR_STEPS = 3

# How much of the developer's instruction to show. It is the load-bearing slot:
# removing it drops the corpus from 10/11 to 8/11, and `241955c7` starts cutting
# at "Regenerate the evalset" as well as at "Separate job:".
_PROMPT_CHARS = 400

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
    if JUDGE_READS_SUMMARY:
        did = str(step.get("summary") or "").strip()
        if did:
            return f"{did}{' — and it failed' if step.get('failed') else ''}"

    # `step["summary"]` is deliberately NOT used here, and that is a
    # measurement. Rendering the judge's context as summaries instead of raw
    # commands took it from 3 fixed / 5 broken to 1 fixed / 6 broken on the live
    # sessions, over-cutting every one: desk-booking 1 -> 4 episodes,
    # failed-retry 1 -> 4, long-session 1 -> 8, and three of them lost
    # `must_contain` steps as the content scattered.
    #
    # The cause is in the summaries themselves. Each ends by tying the step to
    # the request — "fulfilling the developer's request", "informing the
    # developer's next task" — and the judge is then asked whether the request
    # is done while reading twenty such sentences. The phrasing that makes a
    # summary readable is the phrasing that reads as completion.
    #
    # See `docs/benchmarks.md`. The summary stays on the step for readers and
    # later stages; it just does not feed this prompt.
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


def build_prompt(goal: str, prior: list[str], step: dict,
                 said: str, follow: list[str]) -> str:
    """The question, filled in. Every slot in it was measured.

    `{PRIOR}` falls back to "(nothing yet)" rather than rendering an empty
    section, and that is load-bearing rather than tidiness: on `95b6bde7` a
    blank block under the header flips the verdict from "no" to "yes" on its
    own, because a session with nothing behind it reads as one that has not
    started. Removing the section entirely fails the same way.
    """
    template = (PROMPTS / "new_job.md").read_text(encoding="utf-8")
    lines = "\n".join(f"    {p}" for p in prior) or "    (nothing yet)"
    nxt = "\n".join(f"    {p}" for p in follow) or "    (nothing yet)"
    return (template
            .replace("{GOAL}", goal.strip() or "(not stated)")
            .replace("{PRIOR}", lines)
            .replace("{STEP}", render_step(step))
            .replace("{PROMPT}", said.strip()[:_PROMPT_CHARS] or "(nothing)")
            .replace("{NEXT}", nxt))


def judge(step: dict, *, goal: str = "", prior: list[str] | None = None,
          said: str = "", follow: list[str] | None = None,
          model: str, host: str, timeout: float = DEFAULT_TIMEOUT) -> bool | None:
    """Did the developer start a new job after *step*?

    ``None`` means no opinion — no model, a timeout, an answer that is neither
    yes nor no — and `segment` treats it as "not a boundary".
    """
    prompt = build_prompt(goal, list(prior or []), step, said, list(follow or []))
    try:
        reply = ask(model, prompt, host=host, timeout=timeout, think=False)
    except LocalModelUnavailable:
        return None
    return yes_no(reply)


def gaps(steps: list[dict]) -> list[tuple[int, list[dict], list[dict]]]:
    """Every place a boundary could be: `(index, what was said, what follows)`.

    A gap is a prompt sitting between two tool calls. That is the only shape
    this asks about, which is why it costs one call per prompt rather than one
    per step — 33 against 357 across the live sessions.

    KNOWN LIMIT: a task that ends where the developer says nothing is invisible
    here. No live session shows that shape, and the per-step judge it replaced
    could see it in principle, so this is a trade rather than a free win.
    """
    from .segment import is_prompt

    out = []
    for index, step in enumerate(steps):
        if is_prompt(step):
            continue
        after = steps[index + 1:]
        nxt = next((i for i, s in enumerate(after) if not is_prompt(s)), None)
        if nxt is None:
            continue
        # *Every* prompt in the gap, not the first. A developer often sends a
        # challenge and then the instruction behind it, and the first alone can
        # be unreadable: `1c3c9422` has "did you call MCP for this?" followed
        # immediately by "Use the adk-docs MCP tool to look up how ADK eval
        # cases and evalsets are structured" — a restatement of the opening
        # request. Shown only the first, the model called it a new job and the
        # session banked two episodes against a truth of one; shown both, it
        # reads it as the redo it is. That was the corpus's last gap.
        said = [s for s in after[:nxt] if is_prompt(s)]
        if said:
            out.append((index, said,
                        [s for s in after[nxt:] if not is_prompt(s)][:NEXT_STEPS]))
    return out


def said_text(prompts: list[dict]) -> str:
    """What the developer said in one gap, in order, as the prompt renders it."""
    out = [str((p.get("input") or {}).get("text", "")).strip() for p in prompts]
    return "\n\n    ".join(t for t in out if t)


def judge_session(config, session: dict) -> int:
    """Mark the boundaries in a finished session. Returns how many it found.

    Every substantive step gets a verdict — `False` where nothing was asked —
    so `segment.was_judged` is satisfied and the session is not read as offline.
    A step whose question failed keeps no key, and defaults to "not a boundary".
    """
    from .segment import is_prompt

    steps = session.get("steps", [])
    for step in steps:
        if not is_prompt(step):
            step["end"] = False

    found = 0
    for index, said, follow in gaps(steps):
        goal, prior = window(steps[:index])
        verdict = judge(steps[index], goal=goal, prior=prior[-PRIOR_STEPS:],
                        said=said_text(said),
                        follow=[render_step(s) for s in follow],
                        model=config.local_model, host=config.ollama_url)
        if verdict is None:
            steps[index].pop("end", None)
            continue
        steps[index]["end"] = verdict
        found += verdict
    return found


# How much of a field to show the describer. Long enough that a `Write` is
# recognisable from its opening, short enough that the model describes the step
# instead of continuing it — handed 900 characters of a markdown file with no
# other context, it started rewriting the document.
_DESCRIBE_CHARS = 700

# How much of the tool's reply the describer sees. This was 220 and the number
# was a guess. Measured on a real `grep` whose reply ran 4,623 characters, with
# nothing else changed: at 220 the description was "identified relevant Python
# files using grep" and named nothing; at 1200 it was "located relevant code in
# `cards.py` and `skillset.py`", which is the answer.
#
# It does not have the failure mode raw file *content* has, where more input made
# the model continue the document instead of describing the step. Command output
# is signal — paths, matches, counts — not prose to be continued.
_REPLY_CHARS = 1200

# How much of a generated summary to keep. A length instruction does not control
# this: across three wordings, including "never exceed 200 characters", the
# median moved 150 -> 90 and the maximum stayed ~900 in all three. Enforced
# here, not asked for.
SUMMARY_CHARS = 300

# Whether the judge's context renders steps as their summaries instead of raw
# commands. Off: measured 1 fixed / 6 broken against 3 fixed / 5 broken, every
# session gaining episodes. See `docs/benchmarks.md`. The benchmarks flip it.
JUDGE_READS_SUMMARY = False


def describe(step: dict, *, asks: list[str], index: int,
             model: str, host: str, reply: str = "",
             timeout: float = 30.0) -> str:
    """One sentence: what this step did, and the part it plays in the task.

    Everything downstream currently infers that from a command string, each
    with its own partial vocabulary — git verbs in `is_marker`, a read list in
    `is_read_only`, four tool shapes in `render_step`. This records it once,
    where the answer is still knowable.

    Three things the probes settled, all load-bearing:

    * **Every request so far, in order.** Given only the latest one, the model
      describes the goal rather than the step, identically each time.
    * **No previously generated descriptions.** Given them, it copies them — by
      the sixth step it repeated one sentence verbatim for every step after,
      including the one that produced the deliverable.
    * **The part it plays goes in the sentence, not in a field.** Asked for a
      label from a closed vocabulary it answered "Checking" for almost
      everything and called the delivering step "Narrowing". Asked for prose it
      writes "enabling the identification of…", "attempted to locate…",
      "fulfilling the developer's request" — which is the same information,
      correct.

    Costs, measured warm through `handle_tool` on a real payload, median of
    four: capture alone 0.00s, judge alone 0.89s, describing alone 1.93s, both
    2.49s. Measure warm — a first call is ~11s and it is the model loading, the
    same trap that once put a false `think=False` claim in this docstring.

    *reply* is the tool's answer, passed in rather than read off the step. The
    step's own `tool_returned` is already cut to storage size, so reading it
    back would cap the describer below `_REPLY_CHARS` and make widening that
    budget a no-op. The caller has the untruncated reply in hand.

    Returns "" on any failure. A missing model costs the sentence, never the
    step.
    """
    payload = dict(step.get("input") or {})
    body = ""
    for key in ("command", "content", "new_string", "pattern", "url", "query",
                "file_path", "skill"):
        if payload.get(key):
            body = str(payload[key])[:_DESCRIBE_CHARS]
            break
    template = (PROMPTS / "step_description.md").read_text(encoding="utf-8")
    prompt = (template
              .replace("{TOOL}", str(step.get("tool") or "unknown"))
              .replace("{INPUT}", body or "(no arguments recorded)")
              .replace("{REPLY}", (reply or step.get("tool_returned")
                                  or "-")[:_REPLY_CHARS]))
    try:
        reply = ask(model, prompt, host=host, timeout=timeout, think=False)
    except LocalModelUnavailable:
        return ""
    return " ".join(reply.split())[:SUMMARY_CHARS]


def describe_in_session(config, session: dict, step: dict,
                        reply: str = "") -> str:
    """`describe`, with the asks and the step's position read off the buffer."""
    from .segment import is_prompt

    steps = session.get("steps", [])
    asks = [str((s.get("input") or {}).get("text", "")).strip()
            for s in steps if is_prompt(s)]
    index = sum(1 for s in steps if not is_prompt(s)) + 1
    return describe(step, asks=[a for a in asks if a], index=index,
                    reply=reply,
                    model=config.local_model, host=config.ollama_url)


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
    # Sliced by the caller: `judge_session` shows PRIOR_STEPS of it.
    return "\n".join(f"- {a}" for a in asked), done
