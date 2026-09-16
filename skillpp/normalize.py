"""Parameterisation and step shapes.

Two jobs, both run *after* :mod:`skillpp.sanitize`:

* :func:`parameterize` turns machine-specific values into template variables,
  so a captured trace can run on somebody else's laptop (README 3.5).
* :func:`step_shape` reduces one step to a token describing what it *did*,
  throwing away arguments, so ``pytest -k auth`` and ``pytest -k billing`` are
  the same kind of step. `signals.py` compares these across a candidate's runs.

A whole-workflow `signature` built from these used to decide whether two runs
were the same procedure. That is now an embedding (`skillpp.matching`).
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
    return _normalize_segment(head)


# Options a program takes *before* its subcommand rather than after it, with
# how many argument tokens each consumes. The "stop at the first flag" rule
# below is what keeps `pytest -k auth` and `pytest -k billing` fingerprinting
# alike, and it is right for flags that follow a subcommand — but `-C <path>`
# precedes one, so reading it as the end of the command called
# `git -C /repo commit` a bare `git`.
#
# Measured on 2,639 real git calls: 90 use this form. It hid two completion
# markers — a session that committed looked like it never had — and 76
# read-only `status`/`log`/`diff` calls that then counted as work.
#
# Deliberately a small per-program table rather than a general rule: knowing
# which flags take a value is program-specific, and guessing wrong here would
# move fingerprints everywhere.
_PRE_SUBCOMMAND_FLAGS = {
    "git": {"-C": 1, "-c": 1, "--git-dir": 1, "--work-tree": 1,
            "--namespace": 1, "--no-pager": 0, "--no-replace-objects": 0,
            "--bare": 0, "--literal-pathspecs": 0},
}


def _skip_pre_subcommand_flags(program: str, rest: list[str]) -> int:
    """How many tokens of *rest* are global options preceding the subcommand."""
    table = _PRE_SUBCOMMAND_FLAGS.get(program)
    if not table:
        return 0
    skipped = 0
    while skipped < len(rest):
        token = rest[skipped]
        name, _, inline = token.partition("=")
        takes = table.get(name)
        if takes is None:
            break
        # `--git-dir=/x` carries its value; `--git-dir /x` needs the next token.
        skipped += 1 if inline or not takes else 1 + takes
    return skipped


def _normalize_segment(head: str) -> str:
    """``normalize_command`` for one already-isolated link of a chain."""
    tokens = head.split()  # noqa: D401 — see _skip_pre_subcommand_flags below

    index = 0
    while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
        index += 1  # inline env assignment: FOO=bar cmd
    if index >= len(tokens):
        return head[:40]

    raw_program = tokens[index]
    program = raw_program.rsplit("/", 1)[-1]
    if "/" in raw_program or program.endswith(_SCRIPT_SUFFIXES):
        return program

    index += _skip_pre_subcommand_flags(program, tokens[index + 1:])

    for token in tokens[index + 1:]:
        if _FLAG_RE.match(token):
            break  # flags have started; anything after is an argument
        if "/" in token or "." in token or token.startswith("$") or "=" in token:
            break  # a path or value, not a subcommand
        return f"{program} {token}"
    return program


# Chains are split on the shell's sequencing characters *and* on newlines, so a
# multi-line script body is reached too. Deliberately crude: a heredoc body can
# contain an `&` and produce a link that is not a command at all. That costs
# nothing, because the only consumer tests membership in a nine-verb set —
# a nonsense link simply matches nothing.
_LINK_SPLIT_RE = re.compile(r"[|;&\n]{1,2}")


def normalize_links(command: str) -> list[str]:
    """Every link of a chain, each normalised as ``normalize_command`` would.

    ``normalize_command`` keeps only the head, which is right for fingerprinting
    — a signature must not change because someone prefixed a ``cd``. It is wrong
    for asking "did this command do X", because the interesting verb is usually
    last: ``cd repo && git add -A && git commit`` is a commit, and reading the
    head calls it a ``cd``. Measured on one real session, that mistake hid 17
    commits out of 17.

    Separate function rather than a flag on ``normalize_command`` so the
    fingerprinting path cannot be altered by accident.
    """
    if not command:
        return []
    out: list[str] = []
    for chunk in _LINK_SPLIT_RE.split(command.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        shape = _normalize_segment(chunk)
        if shape:
            out.append(shape)
    return out


# Scaffolding a developer's agent wraps around the command that matters.
# Calibrated against 4,853 real Bash calls, not against the benchmark corpus —
# the first draft of this list was written from fixtures I had authored myself,
# which measures nothing. Shares in the real corpus, in order below: 33%, 17%,
# 48%, 32%/43%, 17%, 16%, 8%.
_SCAF_VAR = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S+\s*;\s*")
_SCAF_CD = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
# `cd` alone on its own line — 17% of real commands, and invisible to the
# form above because the split has already consumed the newline.
_SCAF_CD_ONLY = re.compile(r"^\s*cd\s+\S+\s*$")
_SCAF_ECHO = re.compile(r"""echo\s+(?:"[^"]*"|'[^']*')\s*""")
_SCAF_REDIR = re.compile(r"\s*(?:2>&1|>\s*/dev/null(?:\s+2>&1)?)\s*")
_SCAF_PAGER = re.compile(r"\s*\|\s*(?:head|tail)(?:\s+-n)?(?:\s+-?\d+)?\s*")
_SCAF_ORTRUE = re.compile(r"\s*\|\|\s*(?:true|echo\s+(?:\S+|\"[^\"]*\"))\s*")


def strip_scaffolding(command: str) -> str:
    """The command without the plumbing a developer's agent wraps it in.

    **Rendering only.** Nothing that feeds a fingerprint may call this: a
    signature has to be stable, and this is a lossy convenience for a reader.
    ``step_shape`` below deliberately keeps using the raw text.

    The motivation is measured. Giving the benchmark corpus the shape of real
    commands dropped sift recall from 95% to 79%; hiding the command entirely
    and showing only the agent's own description put it back to 95%. So the
    noise is what costs, not the command — and the answer is to remove the
    noise rather than the command, which would leave nothing to audit.

    Conservative on purpose: it removes seven known forms and leaves anything
    unrecognised alone. An over-eager stripper that ate a real argument would
    be worse than the scaffolding it removed, because the result still looks
    like a plausible command.
    """
    if not command:
        return command
    parts = []
    for line in command.split("\n"):
        if _SCAF_CD_ONLY.match(line):
            continue
        line = _SCAF_VAR.sub("", line)
        line = _SCAF_CD.sub("", line)
        line = _SCAF_ORTRUE.sub(" ", line)
        line = _SCAF_PAGER.sub(" ", line)
        line = _SCAF_REDIR.sub(" ", line)
        line = _SCAF_ECHO.sub("", line)
        line = re.sub(r"^\s*(?:&&|;|\|)\s*|\s*(?:&&|;|\|)\s*$", "", line)
        line = line.strip(" ;&|")
        if line:
            parts.append(line)
    return " ".join(" ".join(parts).split()) or command.strip()


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
