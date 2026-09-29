"""The skills a project has installed, and Skill++'s own records of them.

`web` shows and changes them, and `skill-plus-plus edit-skill` has the agent edit
one. Neither imports the other for it, so what both need lives here: where a
draft sits, which of its files are the skill, how an install is recorded, and
whether the installed copy changed since.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .config import Config

# A folder name a skill may be installed under, or addressed by from the page.
SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Bookkeeping beside a draft, never part of the skill. `agent.log` is the drafting
# agent's transcript: it names local paths and it is not a file anyone who
# installs the skill should receive.
NOT_SKILL_FILES = ("status.json", "downloaded.json", "installed.json", "agent.log")


def draft_dir(config: Config, entry_id: str) -> Path:
    return config.root / "drafts" / entry_id


def draft_files(config: Config, entry_id: str) -> list[Path]:
    root = draft_dir(config, entry_id)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.name not in NOT_SKILL_FILES
                  and not p.name.endswith(".tmp")
                  # `.revisions/` holds the versions before each revision.
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_record(config: Config, entry_id: str) -> dict:
    """What installing this draft wrote, and where: `{path, target, files, at}`,
    or `{}` when it is not installed from the page."""
    try:
        return json.loads((draft_dir(config, entry_id) / "installed.json").read_text())
    except (OSError, ValueError):
        return {}


def install_stale(record: dict, skill_md: Path) -> bool:
    """Whether the draft changed since it was installed: a revision that
    Update would pass on."""
    return bool(record) and record.get("files", {}).get("SKILL.md") not in (None, digest(skill_md))


def changed_since_install(record: dict) -> list[str]:
    """The installed files edited after the install, by hand or otherwise."""
    dest = Path(record["path"])
    return [rel for rel, want in record.get("files", {}).items()
            if (dest / rel).exists() and digest(dest / rel) != want]
