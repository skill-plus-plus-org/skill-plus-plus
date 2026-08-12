"""Claude Code wiring: hooks in settings.json, plus the review command.

Nothing here writes to a real settings file unless explicitly asked with
``--apply``. The default is a dry run that prints the exact diff, because
editing a developer's Claude Code configuration is not a side effect anybody
should get by surprise.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_EVENTS = ("UserPromptSubmit", "PostToolUse", "SessionEnd")
MARKER = "skillpp hook"


def hook_command(python: str | None = None, package_root: Path | None = None) -> str:
    """The shell command Claude Code will run for each hook event.

    ``PYTHONPATH`` makes ``-m skillpp`` importable from a checkout without
    installing the package. The ledger root is deliberately *not* passed, so it
    defaults to ``~/.claude/skillpp`` rather than landing inside the repo.
    """
    python = python or sys.executable
    root = package_root or Path(__file__).resolve().parent.parent
    return f'PYTHONPATH="{root}" "{python}" -m skillpp hook'


def desired_hooks(python: str | None = None, package_root: Path | None = None) -> dict:
    cmd = hook_command(python, package_root)
    return {
        event: [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]
        for event in HOOK_EVENTS
    }


def _has_marker(entries: list) -> bool:
    return MARKER in json.dumps(entries)


def plan_settings(settings_path: Path, python: str | None = None,
                  package_root: Path | None = None) -> tuple[dict, list[str]]:
    """Return (merged settings, human-readable change list) without writing."""
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"cannot parse {settings_path}: {exc}") from exc

    merged = json.loads(json.dumps(existing))  # deep copy
    hooks = merged.setdefault("hooks", {})
    changes: list[str] = []
    for event, config in desired_hooks(python, package_root).items():
        current = hooks.get(event)
        if current is None:
            hooks[event] = config
            changes.append(f"add {event} hook")
        elif isinstance(current, list) and not _has_marker(current):
            current.extend(config)
            changes.append(f"append to existing {event} hooks ({len(current) - 1} already there)")
        else:
            changes.append(f"{event} already wired — no change")
    return merged, changes


def apply_settings(settings_path: Path, merged: dict) -> Path | None:
    """Write settings, backing up any existing file first."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if settings_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = settings_path.with_suffix(f".json.skillpp-backup-{stamp}")
        shutil.copy2(settings_path, backup)
    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return backup


def build_plugin_bundle(skills: list[Path], out_dir: Path, name: str,
                        description: str, version: str = "1.0.0",
                        commands: list[Path] | None = None) -> dict:
    """Package skills as a Claude Code plugin.

    Layout, matching what Claude Desktop already provisions for its own
    managed skills:

        <root>/.claude-plugin/plugin.json
        <root>/skills/<name>/SKILL.md
        <root>/commands/<name>.md          (optional)

    This is the portable unit: the same bundle is what a team pull request
    ships (README §13) and what a plugin-install flow consumes. It is *not* a
    way to sideload into Claude Desktop's session cache — that directory is
    provisioned per session and anything written there is transient.
    """
    out_dir = Path(out_dir).expanduser()
    manifest_dir = out_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": version,
                    "description": description}, indent=2) + "\n",
        encoding="utf-8")

    skills_root = out_dir / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for skill_md in skills:
        skill_md = Path(skill_md)
        if not skill_md.exists():
            continue
        dest = skills_root / skill_md.parent.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_md.parent, dest)
        copied.append(skill_md.parent.name)

    copied_commands: list[str] = []
    if commands:
        commands_root = out_dir / "commands"
        commands_root.mkdir(parents=True, exist_ok=True)
        for command in commands:
            command = Path(command)
            if command.exists():
                shutil.copy2(command, commands_root / command.name)
                copied_commands.append(command.name)

    return {"root": str(out_dir), "skills": copied, "commands": copied_commands}


# Limits published for custom-skill upload (Customize → Skills).
UPLOAD_NAME_MAX = 64
UPLOAD_DESCRIPTION_MAX = 200


def validate_for_upload(skill_md: Path) -> list[str]:
    """Problems that would make a skill fail the upload flow.

    Checked at build time rather than discovered at upload time — the
    description limit in particular is easy to sail past while writing a
    trigger line that reads well.
    """
    from .lifecycle import parse_frontmatter

    skill_md = Path(skill_md)
    if not skill_md.exists():
        return [f"{skill_md} does not exist"]
    fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    problems: list[str] = []

    name = str(fm.get("name") or "")
    description = str(fm.get("description") or "")
    if not name:
        problems.append("frontmatter is missing `name`")
    elif len(name) > UPLOAD_NAME_MAX:
        problems.append(f"name is {len(name)} chars (max {UPLOAD_NAME_MAX})")
    if not description:
        problems.append("frontmatter is missing `description`")
    elif len(description) > UPLOAD_DESCRIPTION_MAX:
        problems.append(
            f"description is {len(description)} chars "
            f"(max {UPLOAD_DESCRIPTION_MAX}) — trim by "
            f"{len(description) - UPLOAD_DESCRIPTION_MAX}")
    return problems


def build_upload_bundle(skill_md: Path, out_dir: Path) -> str:
    """Zip one skill in the shape the upload flow expects.

        my-skill.zip
        └── my-skill/
            └── SKILL.md

    The skill folder is the archive root, not the files themselves. Any
    sibling `scripts/` or `references/` directories travel with it.
    """
    skill_md = Path(skill_md)
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    skill_dir = skill_md.parent
    return shutil.make_archive(
        str(out_dir / skill_dir.name), "zip",
        root_dir=str(skill_dir.parent), base_dir=skill_dir.name)


def install_command_file(target_dir: Path, source: Path | None = None) -> Path:
    """Copy the /skillpp-review slash command into .claude/commands/."""
    source = source or (Path(__file__).resolve().parent.parent / "commands" /
                        "skillpp-review.md")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "skillpp-review.md"
    shutil.copy2(source, dest)
    return dest
