"""The ledger: candidate workflows, stored as readable markdown.

One file per candidate. Human-readable body for browsing and grepping, with a
lossless JSON payload in a trailing HTML comment so the engine has a single
authoritative source of truth. Frontmatter is *regenerated* from the payload on
every write, so the two can never drift.

Entries hold summaries, never raw traces (docs/design.md §3, step 3).
"""

from __future__ import annotations

import json
import secrets
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Config

_DATA_RE = re.compile(r"<!--\s*skillpp:data\s*\n(.*?)\n-->", re.DOTALL)

STATUS_CANDIDATE = "candidate"
STATUS_PROMOTED = "promoted"
STATUS_DISMISSED = "dismissed"
# Judged one particular job rather than a method, by `skillpp sift`. Kept
# rather than deleted: the verdict came from a model and has to be auditable
# and reversible, so it parks the entry instead of removing it.
STATUS_ONE_OFF = "one-off"
# Superseded by two entries a frontier reader split it into. Retained rather
# than deleted so the split is auditable and the original steps survive: markers
# and prompt boundaries are what code can see, and where it saw neither it banks
# one candidate covering two procedures. Splitting is the expensive stage
# correcting the cheap one.
STATUS_SPLIT = "split"
# Work an already-promoted skill covers. Not a proposal — the skill exists and
# was reinforced — but kept rather than deleted, because it is the evidence
# that the skill is still being used and the only record of the session that
# used it. Out of the review queue; `ready()` gates on status.
STATUS_COVERED = "covered"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Entry:
    """One candidate workflow in the ledger."""

    id: str
    # A lexical fingerprint of the steps, kept only on entries saved before
    # matching moved to embeddings (`skillpp.matching`). Nothing reads it to
    # decide anything, and new entries leave it empty.
    signature: str = ""
    title: str = ""
    # Where `title` came from: "commit" (the subject of the commit that closed
    # the episode), "model" (the local model named the procedure), "prompt" or
    # "command" (a string capture observed and had to reuse), "" (dictated, or
    # saved before this was recorded). Only the first two are names of a
    # procedure; `skillpp retitle` walks the rest.
    title_source: str = ""
    status: str = STATUS_CANDIDATE
    occurrences: int = 1
    created: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    projects: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    # The run as a conversation — `[{"prompt", "reply", "used"}]` — from the
    # run that created the entry. What `skillpp draft` writes from; see
    # `capture._turns`. Empty on entries saved before it existed.
    turns: list[dict] = field(default_factory=list)
    # One `{"session", "at"}` per recognition, in the order they happened —
    # the provenance the review page lists under "Seen in". `sessions` cannot
    # answer it: it deduplicates, and carries no time, so a candidate seen
    # three times over two weeks looked like a single moment. Empty on entries
    # saved before this existed; the page falls back to `sessions` then.
    seen: list[dict] = field(default_factory=list)
    variants: list[list[dict]] = field(default_factory=list)
    deps_mcp: list[str] = field(default_factory=list)
    deps_cli: list[str] = field(default_factory=list)
    skill_path: str = ""
    notes: str = ""
    source: str = "capture"  # "capture" | "dictated"
    # What `sift` thought, if it has run: "method" | "one-off" | "".
    # An annotation used to order the review queue, never a gate — a local
    # model dropped 4 of 6 real procedures when it was allowed to decide.
    hint: str = ""
    # A task-shaped name and one line saying when this applies, written by the
    # agent in `skillpp draft`. Capture can only reuse a string it observed, so
    # an unnamed candidate is titled with whatever the developer happened to
    # type — measured against a frontier reader that produced
    # `draining-app-replicas-to-clear-a-migration-lock` where this branch had
    # `the staging migration is stuck, get it green`. A description is the only
    # thing read when deciding whether to load a skill, so the difference is
    # between a candidate that can fire and one that cannot.
    description: str = ""
    # What `occurrences` stood at when a person parked this. Recurrences past
    # that point are the only evidence that the parking was wrong, and without
    # the mark there is nothing to measure from.
    parked_at_occurrences: int = 0
    # Banked without being compared to anything, because no embedding model
    # answered — only dictation does that, since a
    # captured session with no model is held instead. `skillpp merge` checks
    # these first.
    unmatched: bool = False

    # -- derived ---------------------------------------------------------
    @property
    def age_days(self) -> float:
        return (datetime.now(timezone.utc) - _parse_ts(self.created)).total_seconds() / 86400

    def recurrences_since_parked(self) -> int:
        """How often this work happened again after someone said no."""
        if not self.parked_at_occurrences:
            return 0
        return max(0, self.occurrences - self.parked_at_occurrences)

    def parking_looks_wrong(self, threshold: int) -> bool:
        """Said no, then did it this many times anyway.

        Never re-proposes anything — a decision is not overturned by a counter.
        It only says the evidence has changed since, which is a different claim
        and the developer's to act on.
        """
        return self.recurrences_since_parked() >= threshold

    def ready(self, threshold: int) -> bool:
        if self.status != STATUS_CANDIDATE:
            return False
        # The recurrence threshold exists to filter noise. An explicit request
        # is not noise, so dictated candidates are ready immediately.
        if self.source == "dictated":
            return True
        return self.occurrences >= threshold

    # -- serialisation ---------------------------------------------------
    def to_markdown(self) -> str:
        payload = json.dumps(asdict(self), indent=2, ensure_ascii=False)
        lines = [
            "---",
            f"id: {self.id}",
            f"title: {self.title}",
            f"status: {self.status}",
            *([f"description: {self.description}"]
              if self.description else []),
            f"occurrences: {self.occurrences}",
            f"created: {self.created}",
            f"last_seen: {self.last_seen}",
            f"projects: {json.dumps(self.projects)}",
            f"deps_mcp: {json.dumps(self.deps_mcp)}",
            f"deps_cli: {json.dumps(self.deps_cli)}",
            "---",
            "",
            f"# {self.title or self.id}",
            "",
            "## Intent",
            "",
        ]
        lines += [f"- {i}" for i in self.intents] or ["- (no stated intent captured)"]
        lines += ["", "## Steps", ""]
        for n, step in enumerate(self.steps, 1):
            lines.append(f"{n}. {describe_step(step)}")
        if not self.steps:
            lines.append("_(none)_")
        if self.notes:
            lines += ["", "## Notes", "", self.notes]
        lines += ["", "<!-- skillpp:data", payload, "-->", ""]
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text: str) -> "Entry":
        m = _DATA_RE.search(text)
        if not m:
            raise ValueError("ledger entry is missing its skillpp:data payload")
        raw = json.loads(m.group(1))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


def describe_step(step: dict) -> str:
    """One-line human description of a captured step."""
    tool = step.get("tool", "?")
    payload = step.get("input", {}) or {}
    if tool == "Stated":
        return str(payload.get("text", "")).strip()
    if tool == "Bash":
        cmd = str(payload.get("command", "")).strip().replace("\n", " ⏎ ")
        marker = "  ✗ failed" if step.get("failed") else ""
        return f"`{cmd}`{marker}"
    if tool in ("Edit", "Write", "NotebookEdit"):
        return f"{tool} `{payload.get('file_path', '?')}`"
    if tool.startswith("mcp__"):
        return f"MCP call `{tool}`"
    return f"{tool}"


def new_id(ledger_dir: Path) -> str:
    """A fresh entry id, never derived from content.

    Ids used to be a hash of the signature. That made a matcher miss on an
    identical signature silently overwrite the existing file — a parked
    decision undone, a promoted skill reverted to candidate. A random id turns
    the same miss into a visible duplicate, which a person can still fold.
    """
    while True:
        candidate = secrets.token_hex(6)
        if not (ledger_dir / f"{candidate}.md").exists():
            return candidate


class Ledger:
    """Directory of candidate entries."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.config.ensure_dirs()

    def path_for(self, entry_id: str) -> Path:
        return self.config.ledger_dir / f"{entry_id}.md"

    def save(self, entry: Entry) -> Path:
        path = self.path_for(entry.id)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(entry.to_markdown(), encoding="utf-8")
        tmp.replace(path)
        return path

    def get(self, entry_id: str) -> Entry | None:
        path = self.path_for(entry_id)
        if not path.exists():
            # Allow unambiguous id prefixes for convenience.
            matches = [p for p in self.config.ledger_dir.glob("*.md")
                       if p.stem.startswith(entry_id)]
            if len(matches) != 1:
                return None
            path = matches[0]
        try:
            return Entry.from_markdown(path.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def all(self) -> Iterator[Entry]:
        for path in sorted(self.config.ledger_dir.glob("*.md")):
            try:
                yield Entry.from_markdown(path.read_text(encoding="utf-8"))
            except (ValueError, json.JSONDecodeError, OSError):
                continue

    def candidates(self, ready_only: bool = False) -> list[Entry]:
        out = [e for e in self.all() if e.status == STATUS_CANDIDATE]
        if ready_only:
            out = [e for e in out if e.ready(self.config.recurrence_threshold)]
        out.sort(key=lambda e: (-e.occurrences, e.last_seen), reverse=False)
        out.sort(key=lambda e: e.occurrences, reverse=True)
        return out

    def delete(self, entry_id: str) -> bool:
        path = self.path_for(entry_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def search(self, query: str) -> list[tuple[float, Entry]]:
        """Plain substring + token scoring over intents, steps and titles."""
        terms = [t.lower() for t in re.split(r"\W+", query) if t]
        results: list[tuple[float, Entry]] = []
        for entry in self.all():
            haystack = " ".join([
                entry.title,
                " ".join(entry.intents),
                " ".join(describe_step(s) for s in entry.steps),
                entry.notes,
            ]).lower()
            if not terms:
                continue
            hits = sum(1 for t in terms if t in haystack)
            if hits:
                score = hits / len(terms)
                # Recency is a mild tiebreaker: this is a record of *your* work.
                age = (datetime.now(timezone.utc) - _parse_ts(entry.last_seen)).days
                score += max(0.0, 0.2 - age * 0.005)
                results.append((score, entry))
        results.sort(key=lambda pair: pair[0], reverse=True)
        return results

    def expire(self, now: datetime | None = None) -> list[str]:
        """Delete unapproved candidates past their TTL.

        Promoted entries are never touched — expiry applies to the ledger, not
        to the skill library (docs/design.md §6).
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.config.candidate_ttl_days)
        removed: list[str] = []
        for entry in self.all():
            if entry.status != STATUS_CANDIDATE:
                continue
            if entry.ready(self.config.recurrence_threshold):
                continue  # proposal is pending review; keep it
            if _parse_ts(entry.last_seen) < cutoff:
                if self.delete(entry.id):
                    removed.append(entry.id)
        return removed

    def stats(self) -> dict[str, Any]:
        entries = list(self.all())
        size = sum(p.stat().st_size for p in self.config.ledger_dir.glob("*.md"))
        return {
            "total": len(entries),
            "candidates": sum(1 for e in entries if e.status == STATUS_CANDIDATE),
            "ready": sum(1 for e in entries if e.ready(self.config.recurrence_threshold)),
            "promoted": sum(1 for e in entries if e.status == STATUS_PROMOTED),
            "dismissed": sum(1 for e in entries if e.status == STATUS_DISMISSED),
            "one_off": sum(1 for e in entries if e.status == STATUS_ONE_OFF),
            "bytes": size,
        }
