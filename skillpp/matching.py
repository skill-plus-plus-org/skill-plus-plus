"""Is this episode the same procedure as an existing candidate? Asked of an embedding.

This replaces a lexical signature — the steps reduced to `read | edit:.json |
bash:python3 | bash:git add` and compared with `SequenceMatcher`. Measured on the
real ledger it could not see a procedure through its incidental steps: six
entries a person tagged good, all "add an eval case, regenerate, commit", scored
0.12-0.66 against each other because one run also ran `ls`, another `source` and
`cd`. It was also asymmetric: the two "cover course reimbursement fact" entries,
0.98 alike to an embedding, scored 0.365 one way and 0.410 the other against a
0.40 floor, and the embedding never saw them.

The reason given for lexical matching was that it ran inside a hook, where no
model could be waited on. That stopped being true when `SessionEnd` began waiting
on the local model for the boundary judge; a session with no model is already
held offline rather than guessed at.

What is embedded is the run's conversation when it has one — each prompt and the
agent's reply, `User: …` / `Agent: …` — and otherwise its steps, one numbered
line each: `3. Bash git status --short`.

Measured on the fourteen live sessions banked as 18 separate episodes, the real
fold order replayed for each input ("danger" is the highest score between two
*different* procedures):

| embedded | danger | no wrong merge at | correct merges of 32 |
|---|---|---|---|
| steps (commands) | 0.921 | 0.93 | 4 |
| step descriptions | 0.846 | 0.85 | 7 |
| prompts only | 0.864 | 0.88 | 3 |
| **prompts and replies** | **0.854** | **0.88** | **6** |

Commands carry what a run happened to type — scratchpad paths, `sed` against
`Read`, whether it also fixed the docs — so three runs of one presentation
procedure scored 0.66-0.83 on them. Prompts alone had pulled matching toward
wording once: card-case sessions opening with the same doc-lookup sentence held
together at 0.97. The replies are what the run produced, and with them 0.88
clears the danger line by 0.026, against 0.009 for commands at 0.93.

Only like is compared with like. A run with a conversation is compared with
entries that have one, at `match_floor_turns`; a run without replies (older
captures, dictation) with entries that have none, on steps at `match_floor`.
A conversation's score and a command list's score are not on the same scale.

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

# Each step's command or path is cut here, and the whole text at TEXT_CHARS on a
# step boundary. This bounds what is embedded; it does not keep the text inside
# the model's context. It used to be meant to — nomic-embed-text reads 2,048
# tokens — but real runs cost 2.11 to 2.4 characters a token, so 5,000
# characters can be 2,370 tokens, and two ledger entries at that length made
# every fold after them fail. `local.embed` now truncates at the model's own
# limit and `_log_truncation` says when it did.
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


# What a run is *about* rather than what was *done* in it: the file names it
# names, and the body of what the agent produced. Both were measured to pull
# matching toward the material — `tests/benchmarks/merge_ladder.py` scores one
# such change at a time over every live session.
_FILE = re.compile(r"\b[\w./-]+\.(md|py|json|pptx|ts|tsx|js|yaml|yml|txt|pdf|docx|xlsx)\b")
# How much of each reply is read. The opening is the agent saying what it is
# doing — "Read article first. Here proposed deck, 10 slides…", "Claim audit —
# draft vs <file>:" — and the rest is the deliverable.
REPLY_HEAD = 300


def turns_text(turns: list[dict]) -> str:
    """The run as it was said: `User: <prompt>` and `Agent: <reply>` per turn.

    File names are masked and each reply is cut to its opening, because two
    procedures run over one document scored *higher* against each other (0.852,
    a talk deck and a LinkedIn post from the same article) than two runs of one
    procedure over different documents (0.831). Measured over 19 live sessions,
    23 episodes, one change at a time:

    | rendering | danger | safe floor | merged | 2-procedures-2-files gap |
    |---|---|---|---|---|
    | prompts and whole replies | 0.854 | 0.86 | 10/46 | -0.021 |
    | + file names masked | 0.848 | 0.85 | 12/46 | +0.022 |
    | **+ replies cut to 300** | 0.849 | **0.85** | **12/46** | **+0.057** |
    | prompts only | 0.837 | 0.84 | 19/46 | +0.161 |

    Prompts alone score best and are not used: every merge they added was a
    presentation pair whose prompts were scripted and pasted word for word,
    while the unscripted coding sessions gained nothing. Removing the
    deliverable by its markdown shape was also measured and dropped — coding
    replies carry their topic in plain sentences, so the danger line moved onto
    a coding pair instead.
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


# File kinds, for the line below. A name says what a run was about; a kind says
# what came out of it.
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

    The two added lines were measured one at a time (`merge_ladder`): the skills
    a turn used took merges from 12 to 13 of 61, naming the produced file kinds
    to 14, both with the danger line unmoved. Tool sequences, the agent's step
    descriptions and model-written summaries were each measured after them and
    each cost more than they gave.
    """
    return turns_text(turns) + "\n\n" + deliverable_text(steps)


def has_conversation(turns: list[dict] | None) -> bool:
    """Does the run carry a reply? Prompts alone were measured worse than steps."""
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


def cached_vector(entry: Entry, config, cache: dict) -> list[float] | None:
    """The entry's vector if the cache still has a current one; never embeds."""
    hit = cache.get(entry.id)
    if (hit and hit.get("model") == config.embed_model
            and hit.get("hash") == _digest(entry_text(entry))):
        return hit["vector"]
    return None


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
