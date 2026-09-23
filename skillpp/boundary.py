"""Did the developer start a new job here? Asked once per prompt, at session end.

A local model is asked at each gap between two tool calls that a prompt landed
in, and the question is a comparison rather than an assessment: what they asked
for, what they just said, what they did next — is that a new job? The verdict is
recorded on the step, so `segment`, the fixtures and the tests all read a plain
boolean afterwards. `segment.is_marker`'s fixed vocabulary no longer decides
boundaries; it survives as the trailing flag's test and the suite's stand-in
judge.

**Every failure is a `None`.** No local model, a timeout, an answer that is
neither yes nor no — all return `None`, and `segment` treats `None` as "not an
ending". A boundary detector that stops a developer's session to think is worse
than one that misses a boundary.

Two things keep each call fast, and neither may be undone:

* the one-word answer that `prompts/new_job.md` ends by asking for, because the
  length of what is generated, not of what is read, sets the time a call takes;
* thinking off (`JUDGE_THINKS`): the default model, `gemma4:e4b`, can think,
  and on a model that thinks the same one-word question takes minutes instead
  of a second.

Why the question is asked here and in this shape — the per-tool-call judge it
replaced, the polarity test that settled it, the framings rejected since and
what each cost — is in docs/research/benchmarks.md, "The question moved".
"""

from __future__ import annotations

import re

from .local import PROMPTS, LocalModelUnavailable, ask, yes_no

# How many steps the judge sees after and before a gap. Both are windows, not
# knobs: change one only by measuring the recorded sessions again
# (docs/research/benchmarks.md, "The question moved").
#
# `NEXT_STEPS`: one step is too few to tell two jobs apart, and five reach far
# enough into the next task to echo the old one.
# `PRIOR_STEPS`: a long history makes a late gap read as a continuation,
# whatever was said in it.
NEXT_STEPS = 3
PRIOR_STEPS = 3

# How much of the developer's instruction to show. It is the load-bearing slot:
# without it the judge cuts at follow-up instructions as well as at real
# switches of task.
_PROMPT_CHARS = 400

# The judge runs at session end or in `fold-pending`, never while someone waits
# on a reply. It was 5 s when it ran per tool call, and at session end that made
# a cold or busy model's silence decide the boundaries: replayed warm, three
# presentation runs that had stayed whole were each cut, because the live runs
# had timed out rather than answered.
DEFAULT_TIMEOUT = 30.0


# Verbs for the tools whose whole payload is a path.
_FILE_VERBS = {"Write": "wrote", "Edit": "edited", "NotebookEdit": "edited",
               "Read": "read"}

# A leftover value is context, not content. Long ones are usually a pasted body
# and push the prompt — and so the latency — up for nothing.
_VALUE_CHARS = 80

# Whether the developer's own `description` of a Bash step reaches the model.
#
# Off. It was measured only against the old per-step question, where it pushed
# the model toward "done" (docs/research/benchmarks.md, "The boundary judge:
# nine configurations, measured"). Against the current gap question it has not
# been measured; `tests/benchmarks/judge_replay.py --describe` does that.
#
# Capture still stores it, and the ledger still shows it. This governs only what
# the judge is shown.
SEND_DESCRIPTION = False

# Three things the judge has never been shown, each off by default so the
# question renders exactly as it was measured. `FULL` shows all of it. They
# exist to be measured one at a time — see `tests/benchmarks/judge_replay.py`.
#
# * the tail of what the assistant said last before the developer spoke —
#   the hand-off the next instruction either answers or ignores;
# * the head of what it said in reply to that instruction — how it read it;
# * what the step before the gap returned.
FULL = -1
REPLY_BEFORE_CHARS = 0
REPLY_AFTER_CHARS = 0
STEP_OUTPUT_CHARS = 0

# Whether the judge may reason before answering. Off: it is asked for one word.
# `gemma4:e4b` can think, and with thinking on it caught both real boundaries
# on the private set but cut three single tasks, at about ten times the cost
# per gap (docs/research/benchmarks.md). Affordable to measure now that the
# judge runs detached at session end rather than inside a hook.
#
# Thinking needs room of its own. `local._num_ctx` reserves 512 tokens past the
# prompt, and a model that writes more than that before answering has its
# context shifted silently — it loses the start of its own question. The 30s
# timeout would also cut it off, and an unanswered gap is no verdict at all.
JUDGE_THINKS = False
_THINK_TIMEOUT = 180.0
_THINK_RESERVE = 4096
# One context size for every thinking call: Ollama reloads the model whenever
# the size changes, so sizing it per prompt reloaded on nearly every call. 8,192
# tokens holds the largest judge prompt and the longest reasoning seen, with room
# to spare.
_THINK_CTX = 8192

# The section showing what the assistant did after the developer spoke. With
# thinking on, the model read the question's "that" as these steps and judged
# them: at the review prompt it agreed the instruction continued the task, then
# called `pytest` and `npm test` "a shift from content review to code testing"
# and answered "new job". Both settings exist to measure whether the section
# helps, hurts, or only needs saying differently.
SHOW_NEXT = True
NEXT_LABEL = "What they do next:"


def render_step(step: dict) -> str:
    """One clause describing what the developer just did.

    Reads as the continuation of "they ...", so the prompt stays a sentence
    rather than a table. `ledger.describe_step` renders for a human reading a
    ledger entry; this renders for a model judging completion, which wants the
    verb and the intent.

    **Nothing captured is dropped here, with one switch.** The tool-specific
    branch phrases the keys it knows, and every other key is appended rather
    than lost: capture already decided what was worth keeping, and scrubbed it.
    The exception is a `Bash` step's `description`, which reaches the model only
    when `SEND_DESCRIPTION` is on.
    """
    if JUDGE_READS_SUMMARY:
        did = str(step.get("summary") or "").strip()
        if did:
            return f"{did}{' — and it failed' if step.get('failed') else ''}"

    # `step["summary"]` feeds this only when `JUDGE_READS_SUMMARY` is on; its
    # comment says why it is off. The summary stays on the step for readers and
    # later stages.
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


_SLOT_RE = re.compile(r"\{(GOAL|PRIOR|STEP_OUTPUT|STEP|REPLY_BEFORE|PROMPT|"
                      r"REPLY_AFTER|NEXT_BLOCK)\}")


def _block(label: str, text: str) -> str:
    """A labelled, indented slot — or nothing, so an off slot adds no bytes."""
    return f"\n\n{label}\n\n    {text}" if text else ""


def _head(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit == FULL or len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut[:cut.rfind(" ")] if " " in cut else cut) + " …"


def _tail(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit == FULL or len(text) <= limit:
        return text
    cut = text[-limit:]
    return "… " + (cut[cut.find(" ") + 1:] if " " in cut else cut)


def gap_extras(steps: list[dict], index: int, said: list[dict]) -> dict:
    """The optional slots for one gap, cut to the current settings."""
    from .segment import is_prompt

    out = {}
    if STEP_OUTPUT_CHARS:
        out["step_output"] = _head(steps[index].get("tool_returned", ""),
                                   STEP_OUTPUT_CHARS)
    if REPLY_BEFORE_CHARS:
        # The reply to the last prompt before the gap: what the assistant said
        # last, including whatever it reported after its final tool call.
        before = next((s for s in reversed(steps[:index])
                       if is_prompt(s) and s.get("reply")), None)
        if before:
            out["reply_before"] = _tail(before["reply"], REPLY_BEFORE_CHARS)
    if REPLY_AFTER_CHARS and said and said[-1].get("reply"):
        out["reply_after"] = _head(said[-1]["reply"], REPLY_AFTER_CHARS)
    return out


def build_prompt(goal: str, prior: list[str], step: dict,
                 said: str, follow: list[str], extras: dict | None = None) -> str:
    """The question, filled in. Every slot in it was measured.

    `{PRIOR}` falls back to "(nothing yet)" rather than rendering an empty
    section, and that is load-bearing rather than tidiness: a blank block
    under the header flips the verdict from "no" to "yes" on its own, because
    a session with nothing behind it reads as one that has not started.
    Removing the section entirely fails the same way.
    """
    template = (PROMPTS / "new_job.md").read_text(encoding="utf-8")
    lines = "\n".join(f"    {p}" for p in prior) or "    (nothing yet)"
    nxt = "\n".join(f"    {p}" for p in follow) or "    (nothing yet)"
    extras = extras or {}
    values = {
        "GOAL": goal.strip() or "(not stated)",
        "PRIOR": lines,
        "STEP": render_step(step),
        "STEP_OUTPUT": _block("It returned:", extras.get("step_output", "")),
        "REPLY_BEFORE": _block("After that, the assistant told them:",
                               extras.get("reply_before", "")),
        "PROMPT": said.strip()[:_PROMPT_CHARS] or "(nothing)",
        "REPLY_AFTER": _block("The assistant answered:",
                              extras.get("reply_after", "")),
        "NEXT_BLOCK": f"{NEXT_LABEL}\n{nxt}\n\n" if SHOW_NEXT else "",
    }
    # One pass, not a chain of `replace`: a reply can contain braces and JSON,
    # and text already filled in must never be read as a placeholder again.
    return _SLOT_RE.sub(lambda m: values[m.group(1)], template)


def judge(step: dict, *, goal: str = "", prior: list[str] | None = None,
          said: str = "", follow: list[str] | None = None,
          model: str, host: str, timeout: float = DEFAULT_TIMEOUT,
          extras: dict | None = None, meta: dict | None = None) -> bool | None:
    """Did the developer start a new job after *step*?

    ``None`` means no opinion — no model, a timeout, an answer that is neither
    yes nor no — and `segment` treats it as "not a boundary". *meta*, when
    given, is filled with the prompt, the raw answer and the model's counts,
    for the benchmarks.
    """
    prompt = build_prompt(goal, list(prior or []), step, said,
                          list(follow or []), extras)
    if meta is not None:
        meta["prompt_chars"] = len(prompt)
    try:
        if JUDGE_THINKS:
            reply = ask(model, prompt, host=host, think=True, meta=meta,
                        timeout=max(timeout, _THINK_TIMEOUT),
                        num_ctx=_THINK_CTX)
        else:
            reply = ask(model, prompt, host=host, timeout=timeout,
                        think=False, meta=meta)
    except LocalModelUnavailable as exc:
        if meta is not None:
            meta["error"] = str(exc)
        return None
    if meta is not None:
        meta["answer"] = reply
    # With thinking on, the reasoning comes back in its own field. If it leaks
    # into the answer, `yes_no` — which reads every word — could pick up a
    # "no" from the middle of an argument. An answer longer than a few words
    # is no verdict rather than a guessed one.
    if JUDGE_THINKS and (not reply.strip() or len(reply.split()) > 5):
        if meta is not None:
            meta["unclean"] = True
        return None
    return yes_no(reply)


def judge_gap(steps: list[dict], index: int, said: list[dict],
              follow: list[dict], *, model: str, host: str,
              meta: dict | None = None) -> bool | None:
    """Ask about one gap, built exactly as `judge_session` builds it.

    The one place a gap's question is assembled, so the benchmark and the live
    path cannot drift apart — `judge_replay.py` used to keep its own copy.
    """
    goal, prior = window(steps[:index])
    history = prior[-PRIOR_STEPS:] if PRIOR_STEPS > 0 else []
    return judge(steps[index], goal=goal, prior=history,
                 said=said_text(said),
                 follow=[render_step(s) for s in follow],
                 model=model, host=host,
                 extras=gap_extras(steps, index, said), meta=meta)


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
        # be unreadable: a question about how the agent went about it, then a
        # restatement of the opening request. Shown only the first, the model
        # calls it a new job and banks two episodes where there is one; shown
        # both, it reads it as the redo it is.
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
        verdict = judge_gap(steps, index, said, follow,
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

# How much of the tool's reply the describer sees. A reply's paths, matches and
# counts are what let a description name what the step found: cut to a couple of
# hundred characters, a `grep` over a long result is described as having
# searched files, and names none of them.
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
# commands. Off. It was measured only against the old per-step question, where
# every session gained episodes: a summary ties its step to the request, and that
# phrasing reads as completion (docs/research/benchmarks.md, "The boundary judge:
# nine configurations, measured"). The benchmarks flip it to measure it against
# the current question.
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
