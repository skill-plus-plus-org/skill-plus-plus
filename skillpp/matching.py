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
from .similar import as_text


def entry_text(entry: Entry) -> str:
    """What is embedded for an entry: its first prompt, then its steps."""
    return as_text(entry)


def episode_text(steps: list[dict], intents: list[str]) -> str:
    """The same text for an episode that is not an entry yet."""
    return as_text(Entry(id="", signature="", title="", intents=list(intents),
                         steps=list(steps)))


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


def vector_for(entry: Entry, config, cache: dict) -> list[float]:
    """The entry's vector, from the cache when it still describes the entry.

    Raises `LocalModelUnavailable` when a vector has to be computed and cannot.
    """
    text = entry_text(entry)
    key = _digest(text)
    hit = cache.get(entry.id)
    if hit and hit.get("model") == config.embed_model and hit.get("hash") == key:
        return hit["vector"]
    vector = embed(text, model=config.embed_model, host=config.ollama_url)
    cache[entry.id] = {"model": config.embed_model, "hash": key, "vector": vector}
    return vector


def find_same(steps: list[dict], intents: list[str], entries: list[Entry],
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
        query = embed(episode_text(steps, intents), model=config.embed_model,
                      host=config.ollama_url)
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


def remember(entry: Entry, config) -> None:
    """Cache a newly banked entry's vector now, while the model is known up."""
    cache = load_cache(config)
    vector_for(entry, config, cache)
    save_cache(config, cache)
