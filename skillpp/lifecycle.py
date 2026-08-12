"""Tiering and decay for the skill library.

Approved skills are never auto-deleted (README 6). Disuse is a poor proxy for
value — the incident runbook is rare *by nature* — so unused skills are
demoted out of the always-loaded index, not destroyed:

    hot (indexed) → cold (searchable) → archived (explicit lookup only)

Because Claude Code indexes everything under the skills directory, demotion is
implemented as a file move rather than a flag (README 8).

Staleness is a separate signal from disuse: a skill rots when the script it
calls is renamed or the flag it passes is removed. That is detected by
checking whether its references still resolve.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PROGRAM_RE = re.compile(r"\A[a-zA-Z][\w.-]*\Z")

# Sections that discuss the workflow rather than performing it. Scanning them
# would report a command named in a question as a broken reference.
_META_SECTIONS = {"known gaps", "judgement", "judgment", "notes", "when to use"}

# Shell builtins resolve to nothing on PATH but are never missing.
_SHELL_BUILTINS = {
    "export", "cd", "echo", "source", "alias", "set", "unset", "eval", "exec",
    "read", "shift", "test", "true", "false", "trap", "wait", "local", "return",
    "if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac",
}


@dataclass
class SkillInfo:
    name: str
    path: Path
    tier: str
    requires_cli: list[str]
    requires_mcp: list[str]
    provenance: str
    uses: int
    last_used: str
    stale_refs: list[str]

    @property
    def is_stale(self) -> bool:
        return bool(self.stale_refs)


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML reader for the subset skills actually use.

    Scalars, inline JSON-ish lists, and one level of nesting under ``metadata``.
    Deliberately not a general YAML parser — this only has to read what
    :func:`skillpp.summary.scaffold_skill` writes, plus hand-authored skills
    that stick to the common shape.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict = {}
    current_parent: str | None = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indented = raw_line[:1].isspace()
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        parsed: object
        if not value:
            parsed = {}
        elif value.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = [v.strip().strip("\"'") for v in value.strip("[]").split(",") if v.strip()]
        elif value.startswith(('"', "'")):
            parsed = value[1:-1]
        else:
            parsed = value
        if indented and current_parent:
            parent = out.setdefault(current_parent, {})
            if isinstance(parent, dict):
                parent[key] = parsed
        else:
            out[key] = parsed
            current_parent = key if parsed == {} else None
    return out


def usage_path(config: Config) -> Path:
    return config.root / "usage.json"


def load_usage(config: Config) -> dict:
    path = usage_path(config)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def record_use(config: Config, skill_name: str) -> None:
    """Called from the PostToolUse hook when a Skill invocation is observed."""
    if not skill_name:
        return
    usage = load_usage(config)
    entry = usage.setdefault(skill_name, {"uses": 0, "last_used": ""})
    entry["uses"] = int(entry.get("uses", 0)) + 1
    entry["last_used"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        usage_path(config).write_text(json.dumps(usage, indent=2), encoding="utf-8")
    except OSError:
        pass


def _referenced(text: str, body_only: bool = True) -> tuple[list[str], list[str]]:
    """Extract referenced file paths and programs from a skill body.

    Reads the first token of every backticked span, so ``./scripts/deploy.sh
    prod`` yields the script rather than being skipped for containing a space.
    Meta sections are ignored — a command named inside an open question is not
    a reference to check.
    """
    body = _FRONTMATTER_RE.sub("", text) if body_only else text
    paths: list[str] = []
    programs: list[str] = []
    skipping = False

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("# ").strip().lower()
            skipping = heading in _META_SECTIONS
            continue
        if skipping:
            continue
        for match in _BACKTICK_RE.finditer(line):
            tokens = match.group(1).strip().split()
            if not tokens:
                continue
            ref = tokens[0].rstrip(".,;:")
            if not ref or ref.startswith(("http://", "https://", "${", "-", "$")):
                continue
            if "/" in ref or ref.startswith("."):
                paths.append(ref)
            elif _PROGRAM_RE.match(ref) and ref not in _SHELL_BUILTINS:
                programs.append(ref)

    return list(dict.fromkeys(paths)), list(dict.fromkeys(programs))


def check_staleness(skill_path: Path, project_root: Path | None = None) -> list[str]:
    """References that no longer resolve — the real decay signal."""
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return []
    root = project_root or Path.cwd()
    paths, programs = _referenced(text)
    stale: list[str] = []
    for ref in paths:
        if "${" in ref or ref.startswith("~"):
            continue
        # Only judge things that look like in-repo files, not prose with dots.
        if "/" not in ref and not ref.startswith("."):
            continue
        candidate = (root / ref) if not ref.startswith("/") else Path(ref)
        sibling = skill_path.parent / ref
        if not candidate.exists() and not sibling.exists():
            stale.append(f"path: {ref}")
    for program in programs:
        if shutil.which(program) is None and not (root / program).exists():
            stale.append(f"command: {program}")
    return stale


def scan(skills_dir: Path, config: Config,
         project_root: Path | None = None) -> list[SkillInfo]:
    """Inventory every skill across all tiers."""
    usage = load_usage(config)
    found: list[SkillInfo] = []
    tiers = [("hot", skills_dir), ("cold", config.cold_dir), ("archived", config.archive_dir)]
    for tier, directory in tiers:
        if not directory.exists():
            continue
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            text = ""
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            name = str(fm.get("name") or skill_file.parent.name)
            stats = usage.get(name, {})
            found.append(SkillInfo(
                name=name,
                path=skill_file,
                tier=tier,
                requires_cli=list(meta.get("requires_cli") or []),
                requires_mcp=list(meta.get("requires_mcp") or []),
                provenance=str(meta.get("provenance") or ""),
                uses=int(stats.get("uses", 0) or 0),
                last_used=str(stats.get("last_used", "") or ""),
                stale_refs=check_staleness(skill_file, project_root),
            ))
    return found


def move_tier(skill: SkillInfo, target_tier: str, skills_dir: Path,
              config: Config) -> Path:
    """Move a skill between hot / cold / archived. Never deletes."""
    destinations = {
        "hot": skills_dir,
        "cold": config.cold_dir,
        "archived": config.archive_dir,
    }
    if target_tier not in destinations:
        raise ValueError(f"unknown tier: {target_tier}")
    dest_root = destinations[target_tier]
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / skill.path.parent.name
    if dest.resolve() == skill.path.parent.resolve():
        return dest
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
    shutil.move(str(skill.path.parent), str(dest))
    return dest
