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


def _num_ctx(prompt: str) -> int:
    want = (len(prompt) // _CHARS_PER_TOKEN) + 512
    return max(_CTX_FLOOR, min(_CTX_CEILING, want))


def ask(model: str, prompt: str, *, host: str = DEFAULT_HOST,
        timeout: float = 120.0) -> str:
    """Put *prompt* to *model* and return its reply.

    Temperature is zero: this is a classifier, and a classifier that answers
    differently on a rerun cannot be scored.
    """
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": _num_ctx(prompt)},
    }).encode("utf-8")
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
