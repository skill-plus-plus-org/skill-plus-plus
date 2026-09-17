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

What is embedded is the steps alone, one numbered line each: `3. Bash git
status --short`. No prompt. Prompts pulled matching toward wording rather than
work: three card-case sessions opened with the same doc-lookup sentence, which
held them together at 0.97 and pulled an unrelated run that opened the same way
to 0.914, six thousandths under the floor. With the steps alone, measured on the
eleven live sessions, 0.93 is the lowest floor with no wrong merge.

Vectors are cached per entry in `<root>/embeddings.json`, keyed on the model and
a hash of the embedded text, so an entry is embedded once and again only when
either changes.
"""

from __future__ import annotations

import hashlib
import json
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


def entry_text(entry: Entry) -> str:
    """What is embedded for an entry: the steps of its first run."""
    return steps_text(entry.steps)


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
              config) -> tuple[Entry, float] | None:
    """The existing entry this episode is a run of, or None.

    Every status is compared, not only candidates. A parked entry that stopped
    matching would be rebuilt as a fresh candidate by its next occurrence, which
    undoes the decision that parked it; a promoted skill that stopped matching
    would never see its count move again.

    Raises `LocalModelUnavailable` if the model cannot be reached — the caller
    decides what an unanswerable question means, not this function.
    """
    if not entries:
        return None
    cache = load_cache(config)
    try:
        query = embed(steps_text(steps), model=config.embed_model,
                      host=config.ollama_url,
                      on_truncate=_log_truncation(config, "new episode"))
        best, best_score = None, -1.0
        for entry in entries:
            score = cosine(query, vector_for(entry, config, cache))
            if score > best_score:
                best, best_score = entry, score
    finally:
        save_cache(config, cache)
    if best is not None and best_score >= config.match_floor:
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
