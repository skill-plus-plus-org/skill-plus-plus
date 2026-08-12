"""Secret and PII scrubbing, applied on write.

The engine never persists a raw trace. Every string captured from a hook goes
through :func:`scrub` before it reaches disk, so the ledger is never a
liability sitting in a buffer waiting to be cleaned up later (README 3.2).

Redactions keep a stable type label — ``[REDACTED:github-token]`` rather than
``***`` — so that scrubbing does not disturb recurrence matching: the same
workflow run twice still produces the same signature.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

# (label, pattern, group-to-redact). Group 0 means "the whole match".
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private-key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.DOTALL), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), 0),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), 0),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), 0),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), 0),
    ("google-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"), 0),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), 0),
    ("bearer-token", re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{20,})"), 2),
    ("connection-string", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"]+"), 0),
    ("basic-auth-url", re.compile(r"(https?://)([^/\s:@]+:[^/\s:@]+)(@)"), 2),
    # key = value / key: "value" in commands, env files and config blobs.
    ("credential", re.compile(
        r"(?i)\b((?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token"
        r"|token|password|passwd|pwd|credential|private[_-]?key)"
        r"\s*[=:]\s*)['\"]?([^\s'\"&;|]{6,})"), 2),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0),
    ("internal-host", re.compile(
        r"\bhttps?://[A-Za-z0-9.-]+\.(?:internal|corp|intranet|local|lan)\b[^\s'\"]*"), 0),
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]+\Z")


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    """Conservative high-entropy fallback for credentials we have no rule for."""
    if _HEX_RE.match(token):
        return False  # git SHAs, checksums — normalised elsewhere, not secrets
    classes = sum((
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
        any(c in "+/=_-" for c in token),
    ))
    return classes >= 3 and _shannon(token) > 3.5


def scrub(text: str) -> str:
    """Return *text* with credentials and PII replaced by typed placeholders."""
    if not text:
        return text
    out = text
    for label, pattern, group in _PATTERNS:
        def _repl(m: re.Match[str], _label: str = label, _group: int = group) -> str:
            placeholder = f"[REDACTED:{_label}]"
            if _group == 0:
                return placeholder
            # Preserve everything around the sensitive group.
            spans = []
            for i in range(1, (m.lastindex or 0) + 1):
                spans.append(placeholder if i == _group else (m.group(i) or ""))
            return "".join(spans)

        out = pattern.sub(_repl, out)

    def _entropy_repl(m: re.Match[str]) -> str:
        token = m.group(0)
        return "[REDACTED:high-entropy]" if _looks_like_secret(token) else token

    return _TOKEN_RE.sub(_entropy_repl, out)


def scrub_all(values: Iterable[str]) -> list[str]:
    return [scrub(v) for v in values]


def scrub_obj(obj, max_chars: int = 2000):
    """Recursively scrub a JSON-ish structure, truncating long strings."""
    if isinstance(obj, str):
        cleaned = scrub(obj)
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars] + f"… [truncated {len(cleaned) - max_chars} chars]"
        return cleaned
    if isinstance(obj, dict):
        return {k: scrub_obj(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, max_chars) for v in obj]
    return obj
