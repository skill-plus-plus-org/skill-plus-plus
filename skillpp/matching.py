"""Is this episode the same procedure as an existing candidate? Asked of an embedding.

What is embedded is the run's conversation when it has one: each prompt and the
opening of the agent's reply, file names masked, then the skills it used and the
kinds of file it produced (`conversation_text`). A run without replies is
embedded as its steps, one numbered line each: `3. Bash git status --short`.

Only like is compared with like. A run with a conversation is compared with
entries that have one, at `match_floor_turns`; a run without, with entries that
have none, at `match_floor`. The two texts do not score on the same scale.

Both floors sit where wrong merges stop, not where merges are most numerous: a
wrong merge silently mixes two procedures into one skill, a missed one leaves a
duplicate a person can still fold. The measurements, and the lexical signature
this replaced, are in docs/research/benchmarks.md, "Same procedure, decided by
embedding".

Matching needs the local model; `capture` holds a session it cannot match
rather than guess.

Vectors are cached per entry in `<root>/embeddings.json`, keyed on the model and
a hash of the embedded text, so an entry is embedded once and again only when
either changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .ledger import Entry
from .local import cosine, embed

# Each step's command or path is cut at STEP_CHARS, and the whole text at
# TEXT_CHARS on a step boundary. This bounds what is embedded, not what the model
# reads: TEXT_CHARS can exceed the model's token limit, so `local.embed` cuts at
# that limit and `_log_truncation` records it.
STEP_CHARS = 120
TEXT_CHARS = 5000


def steps_text(steps: list[dict]) -> str:
    """The steps as numbered lines, `N. Tool command-or-path` — what is embedded.

    Raw commands, not fingerprints: reducing `python3 -m unittest discover` to
    `python3` removes the very token that makes it recognisable as a test run.
    """
    lines: list[str] = []
    size = 0
    for n, step in enumerate(steps, 1):
        payload = step.get("input") or {}
        body = (payload.get("command") or payload.get("file_path")
                or ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2]))
        line = f"{n}. {step.get('tool', '?')} {str(body)[:STEP_CHARS]}"
        if lines and size + len(line) + 1 > TEXT_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


# File names say what a run was about, not what it did, so `turns_text` masks
# them: two runs of one procedure over different files must still match.
_FILE = re.compile(r"\b[\w./-]+\.(md|py|json|pptx|ts|tsx|js|yaml|yml|txt|pdf|docx|xlsx)\b")
# How much of each reply is read. The opening says what the agent is doing; the
# rest is the deliverable, which carries the subject.
REPLY_HEAD = 300


def turns_text(turns: list[dict]) -> str:
    """The run as it was said: `User: <prompt>` and `Agent: <reply>` per turn.

    File names are masked and each reply is cut to its opening, because whole
    replies follow the material: two procedures over one document scored higher
    against each other than one procedure over two documents.

    Also measured and rejected: prompts alone, whose extra merges all came from
    prompts pasted word for word, and removing the deliverable by its markdown
    shape, which misses a topic stated in plain sentences
    (docs/research/benchmarks.md, "One change at a time").
    """
    lines = []
    for turn in turns:
        reply = " ".join(str(turn.get("reply", "")).split())[:REPLY_HEAD]
        block = f"User: {turn.get('prompt', '')}\nAgent: {reply}"
        if turn.get("used"):
            # Which skill or MCP tool did the work: a capability, not a subject.
            block += "\nUsed: " + "; ".join(turn["used"])
        lines.append(block)
    return _FILE.sub("<file>", "\n\n".join(lines))


# The document kinds a command can produce. Only documents: a command also names
# the scripts it runs (`python3 build.py`), which it did not produce.
_DOC_EXT = re.compile(r"\.(pptx|pdf|docx|xlsx|html|md|csv)\b")


def deliverable_text(steps: list[dict]) -> str:
    """One line: the kinds of file the run produced, and whether it handed one over."""
    kinds = {Path(str((s.get("input") or {}).get("file_path") or "")).suffix
             for s in steps if s.get("tool") in ("Write", "Edit", "NotebookEdit")}
    for step in steps:
        kinds |= {"." + m for m in _DOC_EXT.findall(
            str((step.get("input") or {}).get("command") or ""))}
    kinds = sorted(k for k in kinds if k)
    handed = any(s.get("tool") == "SendUserFile" for s in steps)
    return (f"Produced: {', '.join(kinds) or 'nothing'}"
            + ("; handed the file over" if handed else ""))


def conversation_text(turns: list[dict], steps: list[dict]) -> str:
    """What a run with a conversation embeds: its turns, then what it produced.

    Beside the words it carries how the run worked, not its subject: the skills
    each turn used (`turns_text`) and the kinds of file produced. Tool
    sequences, the agent's step descriptions and model-written summaries were
    measured too and left out (docs/research/benchmarks.md, "One change at a
    time").
    """
    return turns_text(turns) + "\n\n" + deliverable_text(steps)


def has_conversation(turns: list[dict] | None) -> bool:
    """Does the run carry a reply? Without one it is matched on its steps."""
    return any(t.get("reply") for t in (turns or []))


def entry_text(entry: Entry) -> str:
    """What is embedded for an entry: its first run's conversation, else its steps."""
    if has_conversation(entry.turns):
        return conversation_text(entry.turns, entry.steps)
    return steps_text(entry.steps)


def floor_for(entry_or_turns, config) -> float:
    """The floor for one kind of text: conversation or steps."""
    turns = getattr(entry_or_turns, "turns", entry_or_turns)
    return config.match_floor_turns if has_conversation(turns) else config.match_floor


def _cache_path(config) -> Path:
    return config.root / "embeddings.json"


def load_cache(config) -> dict:
    try:
        data = json.loads(_cache_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(config, cache: dict) -> None:
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(path)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _log_truncation(config, what: str):
    """A callback for `embed` that records a match made on a cut text."""
    def note(tokens: int) -> None:
        from .capture import log_error
        log_error(config, f"embedding truncated at {tokens} tokens: {what}")
    return note


def vector_for(entry: Entry, config, cache: dict) -> list[float]:
    """The entry's vector, from the cache when it still describes the entry.

    Raises `LocalModelUnavailable` when a vector has to be computed and cannot.
    """
    text = entry_text(entry)
    key = _digest(text)
    hit = cache.get(entry.id)
    if hit and hit.get("model") == config.embed_model and hit.get("hash") == key:
        return hit["vector"]
    vector = embed(text, model=config.embed_model, host=config.ollama_url,
                   on_truncate=_log_truncation(config, f"entry {entry.id}"))
    cache[entry.id] = {"model": config.embed_model, "hash": key, "vector": vector}
    return vector


def find_same(steps: list[dict], entries: list[Entry],
              config, turns: list[dict] | None = None) -> tuple[Entry, float] | None:
    """The existing entry this episode is a run of, or None.

    Every status is compared, not only candidates. A parked entry that stopped
    matching would be rebuilt as a fresh candidate by its next occurrence, which
    undoes the decision that parked it; a promoted skill that stopped matching
    would never see its count move again.

    Raises `LocalModelUnavailable` if the model cannot be reached — the caller
    decides what an unanswerable question means, not this function.
    """
    talk = has_conversation(turns)
    entries = [e for e in entries if has_conversation(e.turns) == talk]
    if not entries:
        return None
    cache = load_cache(config)
    try:
        query = embed(conversation_text(turns, steps) if talk else steps_text(steps),
                      model=config.embed_model, host=config.ollama_url,
                      on_truncate=_log_truncation(config, "new episode"))
        best, best_score = None, -1.0
        for entry in entries:
            score = cosine(query, vector_for(entry, config, cache))
            if score > best_score:
                best, best_score = entry, score
    finally:
        save_cache(config, cache)
    if best is not None and best_score >= floor_for(turns, config):
        return best, best_score
    return None


def model_reachable(config) -> bool:
    """One cheap embedding, to learn whether matching can happen at all."""
    from .local import LocalModelUnavailable
    try:
        embed("ping", model=config.embed_model, host=config.ollama_url)
    except LocalModelUnavailable:
        return False
    return True


def remember(entry: Entry, config) -> None:
    """Cache a newly banked entry's vector now, while the model is known up."""
    cache = load_cache(config)
    vector_for(entry, config, cache)
    save_cache(config, cache)
