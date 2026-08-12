"""Parameterisation and workflow signatures.

Two jobs, both run *after* :mod:`skillpp.sanitize`:

* :func:`parameterize` turns machine-specific values into template variables,
  so a captured trace can run on somebody else's laptop (README 3.5).
* :func:`signature` reduces a sequence of steps to a stable fingerprint used
  for recurrence matching (README 3.3). It deliberately throws away arguments
  and keeps only the *shape* of the work, so ``pytest -k auth`` and
  ``pytest -k billing`` count as the same workflow.
"""

from __future__ import annotations

import re
from pathlib import Path

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_LONG_INT_RE = re.compile(r"\b\d{6,}\b")
_PORT_RE = re.compile(r"(?<=:)\d{4,5}\b")

# Flags and their values are noise for fingerprinting purposes.
_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9]")


def parameterize(text: str, project_path: str | None = None) -> str:
    """Replace environment-specific literals with ``${TEMPLATE}`` variables."""
    if not text:
        return text
    out = text
    if project_path:
        p = str(project_path).rstrip("/")
        if p:
            out = out.replace(p, "${PROJECT_PATH}")
    home = str(Path.home()).rstrip("/")
    out = out.replace(home, "${HOME}")
    out = _UUID_RE.sub("${UUID}", out)
    out = _DATE_RE.sub("${DATE}", out)
    out = _LONG_INT_RE.sub("${ID}", out)
    return out


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".rb", ".js", ".ts", ".pl")


def normalize_command(command: str) -> str:
    """Reduce a shell command to ``program subcommand`` for fingerprinting.

    ``git commit -m "wip"``      -> ``git commit``
    ``npm run test -- --watch``  -> ``npm run``
    ``pytest -k auth``           -> ``pytest``
    ``./scripts/deploy.sh prod`` -> ``deploy.sh``

    Two rules do the work. A subcommand only counts if it appears *before* any
    flag — otherwise ``-k auth`` reads as a subcommand and two runs of the same
    test suite fingerprint differently. And a script invocation never takes a
    subcommand: ``deploy.sh staging`` and ``deploy.sh prod`` are one workflow
    with a parameter, which is exactly what divergence detection needs to see.
    """
    if not command:
        return ""
    # Only fingerprint the first command of a pipeline/chain.
    head = re.split(r"[|;&]{1,2}", command.strip())[0].strip()
    tokens = head.split()

    index = 0
    while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
        index += 1  # inline env assignment: FOO=bar cmd
    if index >= len(tokens):
        return head[:40]

    raw_program = tokens[index]
    program = raw_program.rsplit("/", 1)[-1]
    if "/" in raw_program or program.endswith(_SCRIPT_SUFFIXES):
        return program

    for token in tokens[index + 1:]:
        if _FLAG_RE.match(token):
            break  # flags have started; anything after is an argument
        if "/" in token or "." in token or token.startswith("$") or "=" in token:
            break  # a path or value, not a subcommand
        return f"{program} {token}"
    return program


def step_shape(step: dict) -> str:
    """One token describing what a captured step *did*."""
    tool = step.get("tool", "")
    payload = step.get("input", {}) or {}
    if tool == "Bash":
        return f"bash:{normalize_command(str(payload.get('command', '')))}"
    if tool in ("Edit", "Write", "NotebookEdit"):
        target = str(payload.get("file_path", ""))
        ext = Path(target).suffix or "?"
        return f"{tool.lower()}:{ext}"
    if tool in ("Read", "Glob", "Grep"):
        return f"{tool.lower()}"
    if tool.startswith("mcp__"):
        return f"mcp:{tool}"
    return tool.lower() or "unknown"


def signature(steps: list[dict]) -> str:
    """Stable fingerprint for a whole workflow.

    Consecutive duplicates collapse — running the tests four times in a row is
    the same workflow as running them once.
    """
    shapes: list[str] = []
    for step in steps:
        shape = step_shape(step)
        if not shape:
            continue
        if shapes and shapes[-1] == shape:
            continue
        shapes.append(shape)
    return " | ".join(shapes)
