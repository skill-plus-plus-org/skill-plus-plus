"""Asking a small local model one question at a time.

Why local and why small: the question this module asks is narrow — one episode
in, one word out — and a narrow question is what a 7B answers reliably. Keeping
it local means no key, no per-call cost, and nothing leaving the machine, which
is what lets it run over a whole ledger without anyone budgeting for it.

Two rules learned the hard way, both worth keeping:

* **One question per call.** Asking for five fields in one JSON schema is what
  made a small model return ``task_count=40`` for a session with two tasks. The
  same model answers a single yes/no correctly.
* **Crisp, not lossy.** The input is cleaned of banners and plumbing, but never
  fingerprinted. Reducing ``python3 -m unittest discover`` to ``python3`` hides
  that tests ran, and the model then correctly says they did not.

Requires Ollama on the default port. Absence is not an error here: every caller
treats an unreachable model as "no opinion" and keeps whatever it was judging.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

PROMPTS = Path(__file__).resolve().parent / "prompts"

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3n:e4b"

# Ollama defaults num_ctx to 4096 and silently drops what will not fit, so a
# long episode would be judged on a fragment of itself without saying so. Size
# it per prompt instead, with a ceiling a small model can actually hold.
_CTX_FLOOR = 4096
_CTX_CEILING = 16384
_CHARS_PER_TOKEN = 4


class LocalModelUnavailable(RuntimeError):
    """Ollama is not reachable, or the model is not installed."""


def _num_ctx(prompt: str, reserve: int = 512) -> int:
    want = (len(prompt) // _CHARS_PER_TOKEN) + reserve
    return max(_CTX_FLOOR, min(_CTX_CEILING, want))


def ask(model: str, prompt: str, *, host: str = DEFAULT_HOST,
        timeout: float = 120.0, think: bool | None = None,
        reserve: int = 512, meta: dict | None = None,
        num_ctx: int | None = None) -> str:
    """Put *prompt* to *model* and return its reply.

    Temperature is zero: this is a classifier, and a classifier that answers
    differently on a rerun cannot be scored.

    *think* asks a reasoning-capable model to reason before answering. It is
    sent only when set, because Ollama rejects it outright on models without
    the capability — and most of the ones installed here lack it, so a default
    of ``True`` would break every existing caller — and Ollama turns thinking
    *on* by default for a model that supports it, so leaving this unset is not
    the same as leaving it off. Measured on `qwen3.5:9b`: 113.8s unset against
    0.5s with it off, for the same one-word question.
    """
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0,
                    "num_ctx": num_ctx or _num_ctx(prompt, reserve)},
    }
    if think is not None:
        payload["think"] = think
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LocalModelUnavailable(
            f"could not reach a local model at {host}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LocalModelUnavailable(f"unreadable reply from {host}") from exc
    if "error" in payload:
        raise LocalModelUnavailable(str(payload["error"]))
    # For the benchmarks: what a verdict cost and what was reasoned first.
    # Nothing here changes the request, so a caller that passes no `meta`
    # sends exactly what it always did.
    if meta is not None:
        meta.update({
            "num_ctx": num_ctx or _num_ctx(prompt, reserve),
            "prompt_tokens": payload.get("prompt_eval_count"),
            "answer_tokens": payload.get("eval_count"),
            "seconds": round((payload.get("total_duration") or 0) / 1e9, 2),
            "load_seconds": round((payload.get("load_duration") or 0) / 1e9, 2),
            "thinking_chars": len(payload.get("thinking") or ""),
        })
    return str(payload.get("response", "")).strip()


def yes_no(reply: str) -> bool | None:
    """First yes or no in *reply*, or ``None`` if it said neither.

    A small model told to answer in one word often answers in three. Reading
    the first token that is a verdict is what makes the instruction advisory
    rather than load-bearing.
    """
    for word in reply.replace("*", " ").replace("`", " ").lower().split():
        cleaned = word.strip(".,:;!?'\"()[]")
        if cleaned in ("yes", "y", "true"):
            return True
        if cleaned in ("no", "n", "false"):
            return False
    return None


DEFAULT_EMBED_MODEL = "nomic-embed-text"


def embed(text: str, *, model: str = DEFAULT_EMBED_MODEL,
          host: str = DEFAULT_HOST, timeout: float = 60.0,
          on_truncate: Callable[[int], None] | None = None) -> list[float]:
    """Embed *text* with a local embedding model.

    Separate from :func:`ask` because it is a different endpoint and a different
    kind of question. Embeddings answer "are these the same shape", which is the
    one thing lexical comparison cannot do — `npm test` and `pytest -q` play the
    same role in a release and share not one token.

    `/api/embed` with `truncate`, not the legacy `/api/embeddings`. The legacy
    endpoint refuses input past the model's context with an HTTP 500, which read
    here as "could not reach" — so two long ledger entries (174 and 47 steps)
    made every later fold in their project crash as if Ollama were down. A
    character cap cannot prevent that: measured chars per token on real runs
    went from 2.4 down to 2.11, and hash-like paths go lower. Truncating here
    cuts at the model's own token limit, and keeps the head, which is what the
    character cap in `matching` already kept. Same vectors either way — cosine
    1.000000 between the two endpoints on the same text.

    *on_truncate* is called with the token count when the model's limit was
    reached, so the caller can say so instead of matching on a fragment
    silently.
    """
    body = json.dumps({"model": model, "input": text,
                       "truncate": True}).encode("utf-8")
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/embed", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LocalModelUnavailable(
            f"could not reach an embedding model at {host}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LocalModelUnavailable(f"unreadable reply from {host}") from exc
    vectors = payload.get("embeddings")
    vector = vectors[0] if isinstance(vectors, list) and vectors else None
    if not isinstance(vector, list) or not vector:
        raise LocalModelUnavailable(f"no embedding in the reply from {host}")
    if on_truncate is not None:
        used = payload.get("prompt_eval_count")
        limit = _context_length(model, host)
        if isinstance(used, int) and limit and used >= limit:
            on_truncate(used)
    return [float(x) for x in vector]


_CONTEXT_LENGTHS: dict[tuple[str, str], int | None] = {}


def _context_length(model: str, host: str) -> int | None:
    """The model's context in tokens, from `/api/show`; None if unknown.

    Asked once per model and host. Only used to notice truncation, so any
    failure means "don't know" and never costs the embedding itself.
    """
    key = (host, model)
    if key not in _CONTEXT_LENGTHS:
        length = None
        try:
            request = urllib.request.Request(
                f"{host.rstrip('/')}/api/show",
                data=json.dumps({"model": model}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10.0) as response:
                info = json.loads(response.read().decode("utf-8")).get("model_info") or {}
            length = next((int(v) for k, v in info.items()
                           if k.endswith(".context_length")), None)
        except (urllib.error.URLError, OSError, TimeoutError,
                json.JSONDecodeError, AttributeError, TypeError, ValueError):
            length = None
        _CONTEXT_LENGTHS[key] = length
    return _CONTEXT_LENGTHS[key]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, or 0.0 if either side is degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)
