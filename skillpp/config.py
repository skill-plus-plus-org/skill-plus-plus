"""Filesystem layout and tunable thresholds.

Everything is overridable by environment variable so the test-suite (and a
curious user) can point the whole engine at a scratch directory without
touching a real ledger.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "skillpp"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class Config:
    """Resolved paths and thresholds for one invocation."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is not None:
            self.root = Path(root).expanduser()
        elif os.environ.get("SKILLPP_ROOT"):
            self.root = Path(os.environ["SKILLPP_ROOT"]).expanduser()
        else:
            self.root = DEFAULT_ROOT

        # A workflow must recur this many times before it is proposed (README 3.3).
        self.recurrence_threshold = _int_env("SKILLPP_RECURRENCE", 3)
        # Lexical similarity above which two traces are considered the same workflow.
        self.similarity_threshold = _float_env("SKILLPP_SIMILARITY", 0.85)
        # Unapproved candidates self-delete after this long (README 5).
        self.candidate_ttl_days = _int_env("SKILLPP_TTL_DAYS", 14)
        # Hard caps so a runaway session cannot bloat the ledger.
        self.max_steps_per_session = _int_env("SKILLPP_MAX_STEPS", 500)
        self.max_field_chars = _int_env("SKILLPP_MAX_FIELD", 2000)
        # Never ask the developer more than this many questions (README 4).
        self.max_questions = _int_env("SKILLPP_MAX_QUESTIONS", 3)
        # A boundary that would close an episode smaller than this is ignored:
        # one step is not a workflow.
        # Where a local model is served, and which one to ask. The episode
        # filter is the only thing that uses these, and it runs on demand
        # rather than in a hook: a hook that waits on a model is a hook
        # that stalls a session.
        # How to invoke the developer's own agent to write a draft. A command
        # template rather than an API call, so this needs no key and no
        # vendor: whatever agent the developer already uses writes the body,
        # authenticated as they already are. {PROMPT} is the only
        # substitution.
        self.agent_command = _str_env(
            "SKILLPP_AGENT",
            "claude -p {PROMPT} --no-session-persistence "
            '--allowed-tools "Bash(python3 bin/skillpp *),Read,Write,Edit"')
        self.ollama_url = _str_env("SKILLPP_OLLAMA", "http://127.0.0.1:11434")
        self.local_model = _str_env("SKILLPP_LOCAL_MODEL", "gemma3n:e4b")
        # Near-miss recurrence. Lexical similarity is robust to arguments,
        # ordering, extra steps and leading noise — measured at 1.000,
        # 0.880, 1.000 and (after trimming) 1.000. It fails on one shape:
        # the same procedure with a step served by a different tool.
        # `npm test` against `pytest -q` in an otherwise identical
        # release scores 0.786, just under the 0.85 threshold, which is
        # the worst place for it to land. An embedding separates that
        # pair at 0.912 against 0.451 for an unrelated procedure.
        self.embed_model = _str_env("SKILLPP_EMBED_MODEL", "nomic-embed-text")
        self.near_miss_floor = _float_env("SKILLPP_NEAR_MISS_FLOOR", 0.70)
        self.embed_floor = _float_env("SKILLPP_EMBED_FLOOR", 0.80)
        # Above this, an episode with no completion marker is a slog
        # rather than a procedure. Not a cap on procedures: a finished
        # 50-step migration that ends in a marker is one recipe and is
        # kept however long it ran. This only catches the other shape —
        # a long stretch that ended because the next request arrived,
        # which is what a 433 KB session produces nine of. Sized from
        # the windowing measurements on real sessions: prompt-to-prompt
        # segments run a median of 290 tokens and p90 of 1,458, so a
        # procedure is a small number of steps, not sixty.
        self.max_markerless_steps = _int_env(
            "SKILLPP_MAX_MARKERLESS_STEPS", 25)
        self.min_episode_steps = _int_env("SKILLPP_MIN_EPISODE_STEPS", 2)

    @property
    def decisions_file(self) -> Path:
        """Append-only log of what a person decided about a candidate.

        Statuses are overwritten in place, so without this every decision is
        lost the moment it is superseded — and with it the only ground truth
        that is not hand-written. Ported in spirit from the pattern-detection
        branch, whose `truth.py` reads labels out of reviews the pipeline
        already wrote, on the reasoning that hand-made fixtures are exactly
        what hid three defects until real data arrived.
        """
        return self.root / "decisions.jsonl"

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def cold_dir(self) -> Path:
        return self.root / "cold"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def log_file(self) -> Path:
        return self.root / "skillpp.log"

    def ensure_dirs(self) -> None:
        for d in (self.ledger_dir, self.sessions_dir, self.cold_dir, self.archive_dir):
            d.mkdir(parents=True, exist_ok=True)


def default_skills_dir(cwd: str | Path | None = None) -> Path:
    """Project-local skills directory, falling back to the personal one."""
    base = Path(cwd) if cwd else Path.cwd()
    project = base / ".claude" / "skills"
    if project.exists():
        return project
    return Path.home() / ".claude" / "skills"
