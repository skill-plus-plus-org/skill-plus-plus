"""The skills a project has installed, and Skill++'s own records of them.

`web` shows and changes them, and `skill-plus-plus edit-skill` has the agent edit
one. Neither imports the other for it, so what both need lives here: where a
draft sits, which of its files are the skill, which skills a project has, and
the agent's edits of them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .lifecycle import parse_frontmatter

# A folder name a skill may be installed under, or addressed by from the page.
SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Bookkeeping beside a draft, never part of the skill. `agent.log` is the drafting
# agent's transcript: it names local paths and it is not a file anyone who
# installs the skill should receive. `downloaded.json` and `installed.json` are
# no longer written, but drafts from 0.1 still hold them.
NOT_SKILL_FILES = ("status.json", "downloaded.json", "installed.json", "agent.log")

# How much of a SKILL.md the page shows. A skill is a few pages of text;
# anything bigger is not for reading in a browser.
READ_CAP = 256 * 1024
# A YAML block scalar, `description: >`: the frontmatter reader only sees its marker.
_BLOCK_MARKERS = (">", "|", ">-", "|-", ">+", "|+")


def draft_dir(config: Config, entry_id: str) -> Path:
    return config.root / "drafts" / entry_id


def drafted_skill(config: Config, entry_id: str) -> Path | None:
    """The draft's SKILL.md, wherever its row stands (installed included)."""
    found = sorted(p for p in draft_dir(config, entry_id).rglob("SKILL.md")
                   if ".revisions" not in p.parts)
    return found[0] if found else None


def draft_files(config: Config, entry_id: str) -> list[Path]:
    root = draft_dir(config, entry_id)
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.name not in NOT_SKILL_FILES
                  and not p.name.endswith(".tmp")
                  # `.revisions/` holds the versions before each revision.
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_skill(entry) -> Path | None:
    """The SKILL.md a candidate's skill was installed as, while it is there.

    The ledger's `skill_path` is all that is kept of an install: from then on
    the folder is the skill, changed on its own card. Deleting the folder is
    the whole uninstall, and its draft is back to review.
    """
    if not entry.skill_path:
        return None
    path = Path(entry.skill_path).expanduser()
    return path if path.exists() else None


# -- the skills a project has ------------------------------------------------

def project_key(project: str | Path) -> str:
    """A folder name for one project's edits: readable, safe as a path, and
    apart from another repo that happens to have the same name. It hashes the
    path as the ledger records it, so it stays put across runs."""
    text = os.path.normpath(os.path.expanduser(str(project)))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(text).name).lstrip(".-")[:40]
    return f"{slug or 'project'}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


def skill_folders(place: "Place") -> list[Path]:
    """The skills Claude Code loads from a place: each folder directly in its
    skills folder that holds a SKILL.md. Dot folders are left out."""
    try:
        children = list(place.skills.iterdir())
    except OSError:
        return []
    return sorted((p for p in children
                   if not p.name.startswith(".") and (p / "SKILL.md").is_file()),
                  key=lambda p: p.name.lower())


def skill_files(folder: Path) -> list[Path]:
    """Every file of a skill, SKILL.md first. Dot files and folders are left
    out. A link is listed, never followed, except the skill folder itself when
    it is one: then the link is the skill."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        base = Path(dirpath)
        links = {d for d in dirnames if (base / d).is_symlink()}
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in links)
        found += [base / name for name in (*filenames, *links) if not name.startswith(".")]

    def order(path: Path) -> tuple[bool, str]:
        rel = path.relative_to(folder).as_posix()
        return rel != "SKILL.md", rel.lower()
    return sorted(found, key=order)


def _block_scalar(text: str, key: str) -> str:
    """The lines of a YAML block scalar under *key*, joined into one line."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            kept = []
            for follow in lines[i + 1:]:
                if follow.strip() == "---" or (follow and not follow[:1].isspace()):
                    break
                kept.append(follow.strip())
            return " ".join(part for part in kept if part)
    return ""


def frontmatter(folder: Path) -> tuple[dict, str]:
    """A skill's frontmatter and its SKILL.md text. A description written as a
    YAML block (`description: >`) is joined into one line."""
    text = (folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    front = parse_frontmatter(text)
    if str(front.get("description", "")).strip() in _BLOCK_MARKERS:
        front["description"] = _block_scalar(text, "description")
    return front, text


# -- where a gallery's skills are ----------------------------------------------

@dataclass(frozen=True)
class Place:
    """One project's skills: the folder they are in, and its folder under
    `edits/`."""
    key: str                                # the project's path, as the ledger has it
    name: str
    skills: Path
    store: str


def project_place(project: str) -> Place:
    root = Path(project).expanduser()
    return Place(key=project, name=root.name or project, skills=root / ".claude" / "skills",
                 store=project_key(project))


def write_json(path: Path, data) -> None:
    """Write a record whole: a reader never finds half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- the gallery ---------------------------------------------------------------

def _card(folder: Path) -> dict:
    """A skill as its card shows it: what is in its folder, whoever put it there."""
    front, _ = frontmatter(folder)
    return {"name": folder.name,
            "description": str(front.get("description") or ""),
            "edit_block": edit_refusal(folder)}


def list_place(place: Place) -> dict:
    """One project's skills, as cards. Reads files only and starts no
    process, so the page can ask on every poll."""
    return {"project": place.key,
            "name": place.name,
            "exists": Path(place.key).expanduser().is_dir(),
            "skills": [_card(folder) for folder in skill_folders(place)]}


def _read_head(path: Path, limit: int) -> bytes:
    """Up to *limit* bytes of a file that is not a link, checked at the open."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit)


def read_skill_md(folder: Path) -> dict:
    """A skill's SKILL.md for the page: its text, up to READ_CAP, and never
    read through a link. The caller resolved *folder*; nothing here takes a
    path from a request."""
    try:
        data = _read_head(folder / "SKILL.md", READ_CAP + 1)
    except OSError as exc:
        return {"text": None, "truncated": False, "error": exc.strerror or str(exc)}
    return {"text": data[:READ_CAP].decode("utf-8", errors="replace"),
            "truncated": len(data) > READ_CAP, "error": ""}


# -- edits ---------------------------------------------------------------------
# An agent edits a copy of the skill, never the skill: the copy comes back as a
# proposal under `edits/<project key>/<folder>/current/`, shown as a diff, and
# only Apply writes it into the project.

# What an agent edit may work on. A skill past these is edited by hand.
EDIT_FILES_CAP = 100
EDIT_BYTES_CAP = 1024 * 1024
# How much of a proposal the page shows: a file past DIFF_FILE_CAP gets no diff.
DIFF_FILE_CAP = 256 * 1024
DIFF_OUT_CAP = 100 * 1024
DIFF_TOTAL_CAP = 400 * 1024
# The frontmatter keys claude.ai uploads and the Skills API accept. Claude Code
# reads more, but a skill carrying any other key can no longer be uploaded.
UPLOAD_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")


def edit_root(config: Config, place: Place, folder_name: str) -> Path:
    return config.root / "edits" / place.store / folder_name


def edit_dir(config: Config, place: Place, folder_name: str) -> Path:
    """The one edit of this skill in progress or waiting: `status.json`,
    `base.json`, `base/`, `proposal/` and `agent.log`."""
    return edit_root(config, place, folder_name) / "current"


def undo_dir(config: Config, place: Place, folder_name: str) -> Path:
    """The last applied edit, while it can be undone: the same folder,
    moved here by Apply. `base/` is the skill as it was."""
    return edit_root(config, place, folder_name) / "undo"


def edit_refusal(folder: Path) -> str:
    """Why the agent may not edit this skill, or "" when it may."""
    if folder.is_symlink():
        return "the skill folder is a link; edit it where it lives"
    files = skill_files(folder)
    if any(path.is_symlink() for path in files):
        return "the skill contains links"
    if len(files) > EDIT_FILES_CAP:
        return f"it has more than {EDIT_FILES_CAP} files"
    if sum(path.stat().st_size for path in files) > EDIT_BYTES_CAP:
        return "it is larger than 1 MB"
    try:
        (folder / "SKILL.md").read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "its SKILL.md is not UTF-8 text"
    return ""


def snapshot(folder: Path, dest: Path | None = None) -> dict[str, str]:
    """`{relative path: sha256}` of a skill's regular files, dot files aside.
    With *dest*, the files are also copied there."""
    found = {}
    for path in skill_files(folder):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(folder).as_posix()
        found[rel] = digest(path)
        if dest is not None:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / rel)
    return found


def check_proposal(copy: Path, base: dict) -> str:
    """Why the agent's copy cannot be proposed, or "" when it can. *base* is
    the edit's `base.json`: the files it started from and the skill's name."""
    if not (copy / "SKILL.md").is_file():
        return "the agent removed SKILL.md"
    if any(path.is_symlink() for path in copy.rglob("*")):
        return "the agent added a link"
    files = snapshot(copy)
    if any(rel in NOT_SKILL_FILES for rel in files):
        return "the agent added a file named like one of Skill++'s own records"
    if (len(files) > EDIT_FILES_CAP
            or sum((copy / rel).stat().st_size for rel in files) > EDIT_BYTES_CAP):
        return "the edited skill is too large to apply from the page"
    try:
        text = (copy / "SKILL.md").read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "SKILL.md is no longer UTF-8 text"
    front = parse_frontmatter(text)
    if base.get("had_front") and not front:
        return "SKILL.md lost its frontmatter"
    name = front.get("name") if isinstance(front.get("name"), str) else ""
    if base.get("had_name") and name != base.get("name"):
        return (f"the agent renamed the skill to {name or 'nothing'}; it is found and turned "
                f"off by its name, so ask again without renaming it")
    if not base.get("had_name") and name:
        return "the agent gave the skill a name; it is named after its folder"
    if files == base.get("files"):
        return "the agent made no change"
    return ""


def collect_proposal(copy: Path, dest: Path) -> None:
    """Keep the agent's version, its regular non-dot files, as the proposal."""
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    snapshot(copy, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)


def _text_of(path: Path) -> str | None:
    """A file's text for a diff, or None when it is binary or too large."""
    if path.stat().st_size > DIFF_FILE_CAP:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _warnings(old_md: Path, new_md: Path) -> list[str]:
    """What an edit does to the frontmatter that the developer should know."""
    old = parse_frontmatter(old_md.read_text(encoding="utf-8", errors="replace"))
    new = parse_frontmatter(new_md.read_text(encoding="utf-8", errors="replace"))
    said = []
    description = str(new.get("description") or "")
    if description != str(old.get("description") or "") and len(description) > 200:
        said.append(f"The description is {len(description)} characters; claude.ai "
                    f"uploads take at most 200.")
    for key in sorted(set(new) - set(old) - set(UPLOAD_KEYS)):
        said.append(f"New frontmatter key `{key}`: Claude Code reads it, but claude.ai "
                    f"uploads refuse any key outside {', '.join(UPLOAD_KEYS)}.")
    return said


def _order(rel: str) -> tuple[bool, str]:
    return rel != "SKILL.md", rel.lower()


def diff_proposal(current: Path, folder: Path) -> dict:
    """What a proposed edit changes, file by file, as unified diffs; plus what
    it does to the frontmatter, and whether the skill changed since the copy
    was taken (`stale`: then it cannot be applied)."""
    base = json.loads((current / "base.json").read_text(encoding="utf-8"))
    before, after = base["files"], snapshot(current / "proposal")
    files, total = [], 0
    for rel in sorted(set(before) | set(after), key=_order):
        if before.get(rel) == after.get(rel):
            continue
        item = {"path": rel, "added": 0, "removed": 0, "diff": "", "binary": False,
                "truncated": False,
                "change": "added" if rel not in before else
                          "removed" if rel not in after else "changed"}
        files.append(item)
        old = _text_of(current / "base" / rel) if rel in before else ""
        new = _text_of(current / "proposal" / rel) if rel in after else ""
        if old is None or new is None:
            item["binary"] = True
            continue
        lines = [line if line.endswith("\n") else line + "\n" for line in difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}")]
        item["added"] = sum(1 for line in lines[2:] if line.startswith("+"))
        item["removed"] = sum(1 for line in lines[2:] if line.startswith("-"))
        text, room = "".join(lines), max(0, min(DIFF_OUT_CAP, DIFF_TOTAL_CAP - total))
        if len(text) > room:
            text, item["truncated"] = text[:room], True
        total += len(text)
        item["diff"] = text
    stale = not folder.is_dir() or folder.is_symlink() or snapshot(folder) != before
    return {"files": files, "stale": stale, "instruction": base.get("instruction", ""),
            "warnings": _warnings(current / "base" / "SKILL.md", current / "proposal" / "SKILL.md")}


def _prune_empty(folder: Path) -> None:
    """Remove folders an edit left empty, below the skill folder only."""
    for path in sorted((p for p in folder.rglob("*") if p.is_dir() and not p.is_symlink()),
                       key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _write_into(folder: Path, rel: str, source: Path) -> None:
    """Write one file of an edit into the skill whole, keeping the mode the
    installed file had (an agent's Write can drop `+x`)."""
    target = folder / rel
    for parent in target.relative_to(folder).parents:
        if (folder / parent).is_symlink():
            raise OSError(f"{folder / parent} is a link")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (target if target.exists() else source).stat().st_mode & 0o777
    tmp = target.with_name(f".{target.name}.skill-plus-plus.tmp")
    shutil.copyfile(source, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, target)


def apply_edit(config: Config, place: Place, folder: Path) -> dict:
    """Write a proposed edit into the skill.

    Refused when the skill changed after the agent's copy was taken: the
    proposal was made against files that are no longer there. On a failed
    write, what was written is put back from the copy. The edit then moves to
    `undo_dir`, for `undo_edit`, until `forget_undo`.
    """
    current = edit_dir(config, place, folder.name)
    base = json.loads((current / "base.json").read_text(encoding="utf-8"))
    before = base["files"]
    if not folder.is_dir() or folder.is_symlink() or snapshot(folder) != before:
        raise RuntimeError("the skill changed after the agent started; discard this edit "
                           "and ask again")
    why = check_proposal(current / "proposal", base)
    if why:
        raise RuntimeError(why)
    changed = _replace_files(folder, current / "proposal", before,
                             snapshot(current / "proposal"), current / "base")
    forget_undo(config, place, folder.name)
    os.replace(current, undo_dir(config, place, folder.name))
    return {"changed": changed}


def undo_edit(config: Config, place: Place, folder: Path) -> list[str]:
    """Put the skill back as it was before its last Apply, from the copy the
    edit was made on. Refused once anything changed the skill since: undoing
    it then would throw that change away."""
    kept = undo_dir(config, place, folder.name)
    if not (kept / "base").is_dir():
        raise RuntimeError("there is nothing to undo")
    now, then = snapshot(kept / "proposal"), snapshot(kept / "base")
    if not folder.is_dir() or folder.is_symlink() or snapshot(folder) != now:
        raise RuntimeError("the skill changed after the edit was applied, so it can't be undone")
    changed = _replace_files(folder, kept / "base", now, then, kept / "proposal")
    forget_undo(config, place, folder.name)
    return changed


def forget_undo(config: Config, place: Place, folder_name: str) -> None:
    """Drop what Undo would put back."""
    shutil.rmtree(undo_dir(config, place, folder_name), ignore_errors=True)
    try:
        edit_root(config, place, folder_name).rmdir()
    except OSError:
        pass                              # an edit of it is waiting


def forget_every_undo(config: Config) -> None:
    """Drop every copy Undo kept: nothing can ask for one once the page that
    applied the edit is gone, which a new server means."""
    for kept in (config.root / "edits").glob("*/*/undo"):
        shutil.rmtree(kept, ignore_errors=True)
        try:
            kept.parent.rmdir()
        except OSError:
            pass


def _replace_files(folder: Path, source: Path, before: dict, after: dict, backup: Path) -> list[str]:
    """Make the skill hold *source*'s files: the changed ones written whole,
    the ones *source* has not removed. *before* and *after* are the two sides'
    `snapshot`s. On a failed write, what was written is put back from
    *backup*, a copy of the skill as it was."""
    changed = [rel for rel in sorted(set(before) | set(after), key=_order)
               if before.get(rel) != after.get(rel)]
    done: list[str] = []
    try:
        for rel in changed:
            if rel in after:
                _write_into(folder, rel, source / rel)
            else:
                (folder / rel).unlink()
            done.append(rel)
    except OSError:
        for rel in done:
            if rel in before:
                _write_into(folder, rel, backup / rel)
            else:
                (folder / rel).unlink(missing_ok=True)
        _prune_empty(folder)
        raise
    _prune_empty(folder)
    return changed


def discard_edit(config: Config, place: Place, folder_name: str) -> None:
    """Drop a proposed edit. Only Skill++'s own copy goes; the skill is untouched."""
    shutil.rmtree(edit_dir(config, place, folder_name), ignore_errors=True)
    try:                                  # kept while the last Apply can be undone
        edit_root(config, place, folder_name).rmdir()
    except OSError:
        pass
