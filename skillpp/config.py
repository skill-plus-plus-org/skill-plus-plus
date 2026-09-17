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


def _bool_env(name: str, default: bool) -> bool:
    """Unset means *default*; anything falsey-looking means off.

    Generous about what counts as off on purpose — someone reaching for this is
    turning something off in a hurry, and `SKILLPP_JUDGE=false` failing open
    because only "0" was handled is the wrong way to learn the spelling.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


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
        # Unapproved candidates self-delete after this long (README 5).
        self.candidate_ttl_days = _int_env("SKILLPP_TTL_DAYS", 14)
        # Hard caps so a runaway session cannot bloat the ledger.
        self.max_steps_per_session = _int_env("SKILLPP_MAX_STEPS", 500)
        self.max_field_chars = _int_env("SKILLPP_MAX_FIELD", 2000)
        # Never ask the developer more than this many questions (README 4).
        self.max_questions = _int_env("SKILLPP_MAX_QUESTIONS", 3)
        # Ask a local model, on every tool call, whether the task ended there
        # (`skillpp.boundary`). Off restores the marker-and-prompt rules, which
        # is what every session captured before it, and every fixture, records —
        # so turn it off when recording a fixture that has to stay comparable
        # with those, or when the ~1.6s per tool call is not worth paying.
        self.judge_boundaries = _bool_env("SKILLPP_JUDGE", True)
        # Ask the same model, on every tool call, to write one sentence saying
        # what the step did and the part it plays (`skillpp.boundary.describe`).
        # Same trade as the judge: it costs the developer a second or two per
        # step and it is what lets a later stage read intent instead of parsing
        # a command. `SKILLPP_DESCRIBE=0` turns it off.
        self.describe_steps = _bool_env("SKILLPP_DESCRIBE", True)
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
        # Which embedding model decides "same procedure" (`skillpp.matching`).
        self.embed_model = _str_env("SKILLPP_EMBED_MODEL", "nomic-embed-text")
        # Cosine at or above which an episode joins an existing entry. Set where
        # wrong merges stop, not where merges are most numerous: a wrong merge
        # silently mixes two procedures into one skill, a missed one only leaves
        # a duplicate a person can still see. Measured on the eleven live
        # sessions (`tests/fixtures/sessions/recurrence.py`), embedding the
        # steps alone: 0.93 is the lowest floor with no wrong merge; 0.86-0.92
        # merge more and put an unrelated run in with the eval cases.
        self.match_floor = _float_env("SKILLPP_MATCH_FLOOR", 0.93)
        # Conversation text scores on its own scale; see `matching`.
        self.match_floor_turns = _float_env("SKILLPP_MATCH_FLOOR_TURNS", 0.85)
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
