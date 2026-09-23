"""Secret and PII scrubbing, applied on write.

What a hook captures — prompts, tool inputs and results, the agent's words —
goes through :func:`scrub` before it reaches disk. Identifiers are kept as they
are: the working directory, the transcript's path and the session id, because
the project and the conversation are found from them. docs/privacy.md lists
what is and is not recognised, and changes with ``_PATTERNS``.

A redaction is a type label that never depends on the value —
``[REDACTED:github-token]`` — so two runs of one workflow with different
secrets still embed alike (`skillpp.matching`) and keep the same step shapes
(`skillpp.signals`).
"""

from __future__ import annotations

import math
import re
from urllib.parse import unquote

# (label, pattern, group-to-redact). Group 0 means "the whole match".
#
# `\b` is Unicode-aware, so it finds no boundary between CJK or accented text
# and ASCII: a secret written straight after `は` would slip past it. The token
# rules use re.ASCII; the rules that use `\s` spell the boundary out instead,
# because re.ASCII would make `\s` ASCII-only too.
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    # The body stops at the next BEGIN: a bare `.*?` rescans to the end of the
    # text from every header that has no END, quadratic in a list of headers.
    ("private-key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----BEGIN).)*?"
        r"-----END[A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
        re.DOTALL), 0),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", re.ASCII), 0),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b", re.ASCII), 0),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.ASCII), 0),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b", re.ASCII), 0),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b", re.ASCII), 0),
    ("google-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b", re.ASCII), 0),
    ("prefixed-token", re.compile(
        r"\b(?:(?:whsec_|hf_|[rs]k_(?:live|test)_)[A-Za-z0-9]{16,}"
        r"|glpat-[A-Za-z0-9_-]{20,})", re.ASCII), 0),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", re.ASCII), 0),
    # `bearer` alone; `Basic` and `Token` only after an Authorization header.
    ("bearer-token", re.compile(
        r"(?i)(?<![A-Za-z0-9_])((?:bearer|authorization['\"]?:\s*['\"]?(?:basic|token))\s+)"
        r"([A-Za-z0-9._~+/=-]{16,})"), 2),
    ("connection-string", re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
        r"://[^\s'\"]+"), 0),
    # Any scheme. Its {0,31} bound keeps this linear; unbounded it is quadratic.
    ("basic-auth-url", re.compile(
        r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)([^/\s:@]*:[^/\s:@]+)@"), 2),
    # A cookie is a credential whatever it is called: a `Cookie:` header or a
    # curl `-b`/`--cookie` value, as far as its `name=value; ...` pairs run.
    # Keyed on the context, not the name, because `session =` is code.
    ("cookie", re.compile(
        r"(?i)((?<![A-Za-z0-9_])cookie['\"]?:\s*['\"]?|(?<!\S)(?:-b|--cookie)\s+['\"]?)"
        r"([^\s'\";=:]+=[^\s'\";]*(?:;\s*[^\s'\";=:]+=[^\s'\";]*)*)"), 2),
    # key = value / key: "value" in commands, env files and config blobs. The
    # key may carry a prefix (`DB_PASSWORD`), quotes or brackets (`"password":`,
    # `user[password]=`); a quoted value may hold spaces but not start with one,
    # so `grep "token=" src/` is not read as a value. A value that is already a
    # placeholder keeps the label an earlier rule gave it.
    ("credential", re.compile(
        r"(?i)(?:(?<![A-Za-z0-9])|(?<=%5b))"
        r"((?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token"
        r"|csrf[_-]?token|token|password|passwd|pwd|credential|private[_-]?key)"
        r"(?:['\"\]]|%5d)?\s*[=:]\s*['\"]?)(?!\[REDACTED:)"
        r"((?<=['\"])[^\s'\"][^'\"\n]{5,}(?=['\"])|[^\s'\"&;|]{6,})"), 2),
    # Not an SCP remote: `git@github.com:acme/api.git` names nobody. A remote is
    # the host, `:` and a path with a `/`; `jane@example.com:token` is still an
    # address. The lookahead skips the rest of the host, or the regex backtracks
    # to a shorter one and redacts half of `git@gitlab.example.com:team/r.git`.
    # A match starts only where a run of address characters starts: `\b` would
    # retry from every `%xx` of a URL-encoded blob, quadratic in its length.
    ("email", re.compile(
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        r"(?![A-Za-z0-9_])(?![A-Za-z0-9.-]*:[\w.~-]*/)"), 0),
    ("internal-host", re.compile(
        r"(?i)(?<![A-Za-z0-9_])https?://(?:[^/\s@]+@)?[A-Za-z0-9.-]+"
        r"\.(?:internal|corp|intranet|local|lan)(?![A-Za-z0-9_])[^\s'\"]*"), 0),
]

# base64's own URL escapes (`%2B`, `%2F`, `%3D`) join a run; `%20` and the rest
# still split it.
_TOKEN_RE = re.compile(r"(?:[A-Za-z0-9+/=_-]|%(?:2[BbFf]|3[Dd])){40,}")
# One word: all lower, all digits, or Capitalised. Not `OObjTXEYQHXlFd4`.
_WORDLIKE = re.compile(r"\A(?:[a-z][a-z0-9]*|[0-9]+|[A-Z][a-z0-9]*)\Z")


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
    if any(sep in token for sep in "/-_"):
        # A name is words joined by separators; a credential is one long run of
        # entropy. Each condition stops a different secret passing as a name:
        # pieces of one or two characters do not count, since a random run split
        # at its `-`s is full of them; two pieces are too few; a base64 blob
        # split at its `/`s has pieces that are not words; and a key glued onto
        # a path is one piece of 40 or more, which the path's words would
        # otherwise outvote.
        pieces = [x for x in re.split(r"[/\-_]", token) if len(x) > 2]
        wordlike = sum(1 for x in pieces if _WORDLIKE.match(x))
        if (len(pieces) >= 3 and wordlike >= 0.6 * len(pieces)
                and max(len(x) for x in pieces) < 40):
            return False

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
            # Only the sensitive group is replaced; everything else the match
            # covered, quotes included, stays exactly as it was.
            start, end = m.span(_group)
            return m.string[m.start():start] + f"[REDACTED:{_label}]" + m.string[end:m.end()]

        out = pattern.sub(_repl, out)

    def _entropy_repl(m: re.Match[str]) -> str:
        token = m.group(0)
        # The decoded value, and each run between escapes on its own: joining
        # words onto a run must not let the name guard wave it through.
        runs = {unquote(token), *token.split("%")}
        return ("[REDACTED:high-entropy]" if any(
            _TOKEN_RE.fullmatch(r) and _looks_like_secret(r) for r in runs) else token)

    return _TOKEN_RE.sub(_entropy_repl, out)


def scrub_obj(obj, max_chars: int = 2000):
    """Recursively scrub a JSON-ish structure, truncating long strings.

    Scrubs before truncating: a cut can leave a secret too short for the
    patterns to recognise.
    """
    if isinstance(obj, str):
        cleaned = scrub(obj)
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars] + f"… [truncated {len(cleaned) - max_chars} chars]"
        return cleaned
    if isinstance(obj, dict):
        # A credential is often named only by its key, so a string value is
        # scrubbed as `key: value`, the form the credential rule reads. If the
        # joiner did not survive, the value is scrubbed on its own. Keys are
        # scrubbed too.
        out = {}
        for k, v in obj.items():
            if isinstance(v, str):
                _, joined, value = scrub(f"{k}: {v}").partition(": ")
                v = value if joined else v
            out[scrub_obj(k, max_chars)] = scrub_obj(v, max_chars)
        return out
    if isinstance(obj, list):
        return [scrub_obj(v, max_chars) for v in obj]
    return obj
