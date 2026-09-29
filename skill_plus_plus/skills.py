"""The skills a project has installed, and Skill++'s own records of them.

`web` shows and changes them, and `skill-plus-plus edit-skill` has the agent edit
one. Neither imports the other for it, so what both need lives here: where a
draft sits, which of its files are the skill, how an install is recorded, and
whether the installed copy changed since.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .install import read_settings, write_settings
from .lifecycle import load_usage, parse_frontmatter

# A folder name a skill may be installed under, or addressed by from the page.
SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# Bookkeeping beside a draft, never part of the skill. `agent.log` is the drafting
# agent's transcript: it names local paths and it is not a file anyone who
# installs the skill should receive.
NOT_SKILL_FILES = ("status.json", "downloaded.json", "installed.json", "agent.log")

# What the viewer shows of a skill, at most. Past a cap a file is named, not
# shown: a skill is a few pages of text, and anything bigger is not for reading
# in a browser pane.
READ_FILE_CAP = 256 * 1024
READ_TOTAL_CAP = 2 * 1024 * 1024
READ_FILES_CAP = 200
# The values `skillOverrides` takes (settings reference). An absent entry is "on".
OVERRIDE_STATES = ("on", "name-only", "user-invocable-only", "off")
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


# -- the skills a project has ------------------------------------------------

def skills_dir(project: str | Path) -> Path:
    """Where Claude Code loads a project's skills from."""
    return Path(project).expanduser() / ".claude" / "skills"


def project_key(project: str | Path) -> str:
    """A folder name for one project's archive and edits: readable, safe as a
    path, and apart from another repo that happens to have the same name. It
    hashes the path as the ledger records it, so it stays put across runs."""
    text = os.path.normpath(os.path.expanduser(str(project)))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(text).name).lstrip(".-")[:40]
    return f"{slug or 'project'}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


def skill_folders(project: str | Path) -> list[Path]:
    """The skills Claude Code loads from a project: each folder directly in
    `.claude/skills/` that holds a SKILL.md. Dot folders are left out."""
    try:
        children = list(skills_dir(project).iterdir())
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


def override_key(folder: Path, front: dict) -> str:
    """The name Claude Code knows the skill by, which `skillOverrides` matches:
    its frontmatter `name`, or its folder's when it has none."""
    name = front.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else folder.name


def install_index(config: Config) -> dict[str, tuple[str, dict]]:
    """Where each draft installed from the page went: the installed folder's
    real path, to the entry's id and its install record."""
    index = {}
    for path in sorted((config.root / "drafts").glob("*/installed.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("path"):
            index[os.path.realpath(record["path"])] = (path.parent.name, record)
    return index


# -- turned off, or not ------------------------------------------------------

def _main_checkout(dot_git: Path) -> Path | None:
    """The main checkout of the linked worktree whose `.git` file this is."""
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = (dot_git.parent / line[len("gitdir:"):].strip()).resolve()
    try:
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return None                       # a submodule: its own checkout, no main one
    return common.parent if common.name == ".git" else None


def local_settings_path(project: str | Path) -> Path:
    """This user's own settings for the project, which Claude Code keeps at the
    repository root, and at the main checkout's root when the project is a
    linked worktree (settings docs, "Where Claude Code keeps the local file in
    a git repository")."""
    root = Path(project).expanduser()
    if (root / ".git").is_file():
        root = _main_checkout(root / ".git") or root
    return root / ".claude" / "settings.local.json"


def _overrides(path: Path) -> dict:
    value = read_settings(path).get("skillOverrides", {})
    if not isinstance(value, dict):
        raise RuntimeError(f"{path}: skillOverrides is not an object")
    return value


def read_overrides(project: str | Path, home: Path | None = None) -> dict:
    """The `skillOverrides` maps that apply in *project*: this user's local
    file, the project's committed one and the user's own settings.

    Only the local file is ever written from here, so only its errors are
    reported, and they keep Turn off and Turn on from writing over it. The other
    two are read leniently: they are not ours to fix, and Claude Code says so.
    """
    home = home or Path.home()
    found = {"local": {}, "team": {}, "user": {}, "local_error": ""}
    try:
        found["local"] = _overrides(local_settings_path(project))
    except RuntimeError as exc:
        found["local_error"] = str(exc)
    for source, path in (("team", Path(project).expanduser() / ".claude" / "settings.json"),
                         ("user", home / ".claude" / "settings.json")):
        try:
            found[source] = _overrides(path)
        except RuntimeError:
            pass
    return found


def visibility(key: str, found: dict) -> dict:
    """What Claude Code does with the skill named *key*, and whose setting
    says so: the local file over the project's, over the user's, entry by
    entry. No entry anywhere means "on"."""
    for source in ("local", "team", "user"):
        value = (found.get(source) or {}).get(key)
        if value in OVERRIDE_STATES:
            return {"state": value, "source": source}
    return {"state": "on", "source": ""}


# What in a clone's `.git/info/exclude` already keeps the local file out of commits.
_EXCLUDES_LOCAL = {"/.claude/settings.local.json", ".claude/settings.local.json",
                   "**/.claude/settings.local.json", "settings.local.json",
                   "/.claude/", ".claude/", "/.claude", ".claude"}


def git_exclude_path(root: Path) -> Path | None:
    """The clone's own ignore file, `.git/info/exclude`. It is never committed,
    so a line there keeps a file out of commits without a change anyone else
    has to review."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    try:
        line = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = (root / line[len("gitdir:"):].strip()).resolve()
    try:                                  # a worktree reads the shared excludes
        gitdir = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        pass
    return gitdir / "info" / "exclude"


def _exclude_local_settings(settings_path: Path) -> bool:
    """Keep a local settings file Skill++ has just created out of commits, as
    Claude Code does for the ones it creates. Whether a line was added."""
    exclude = git_exclude_path(settings_path.parent.parent)
    if exclude is None:
        return False
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if any(line.strip() in _EXCLUDES_LOCAL for line in text.splitlines()):
            return False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(("" if not text or text.endswith("\n") else "\n")
                         + "# Skill++: this user's own Claude Code settings\n"
                         + "/.claude/settings.local.json\n")
    except OSError:
        return False
    return True


def set_override(config: Config, project: str | Path, key: str, value: str | None) -> dict:
    """Turn the skill named *key* off for this user (*value* "off") or back
    on (None), in the project's local settings, the file Claude Code's own
    `/skills` menu writes.

    Every other setting stays as it was, in its order, and `skillOverrides`
    goes once nothing is left in it. Turning on removes the entry rather than
    writing "on", so a skill nobody turned off never creates a file. A file
    that cannot be parsed is never written over. Before a write, the file as
    it was is kept under `backups/`.
    """
    path = local_settings_path(project)
    existed = path.exists()
    settings = read_settings(path)
    overrides = settings.get("skillOverrides", {})
    if not isinstance(overrides, dict):
        raise RuntimeError(f"{path}: skillOverrides is not an object")
    if value is None:
        if key not in overrides:
            return {"changed": False, "created": False, "excluded": False, "path": str(path)}
        del overrides[key]
        if not overrides:
            del settings["skillOverrides"]
    else:
        settings["skillOverrides"] = {**overrides, key: value}
    if existed:
        backup = config.root / "backups" / project_key(project) / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    write_settings(path, settings)
    return {"changed": True, "created": not existed, "path": str(path),
            "excluded": not existed and _exclude_local_settings(path)}


# -- archived ------------------------------------------------------------------

def archive_root(config: Config, project: str | Path) -> Path:
    """Where one project's archived skills go. Two levels below `archive/`, so
    `lifecycle.scan`, which globs `archive/*/SKILL.md`, never lists them."""
    return config.root / "archive" / project_key(project)


def _archive_record(config: Config, project: str | Path) -> dict:
    """The project's archive record. Missing is empty; unreadable raises, so a
    write never replaces what it could not read."""
    path = archive_root(config, project) / "archived.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"project": str(project), "items": {}}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise RuntimeError(f"cannot read {path}: not an archive record")
    return data


def archived(config: Config, project: str | Path) -> list[dict]:
    """The skills archived from *project*, newest first."""
    try:
        items = _archive_record(config, project)["items"]
    except RuntimeError:
        return []
    return sorted(({"id": key, **item} for key, item in items.items() if isinstance(item, dict)),
                  key=lambda item: str(item.get("archived_at", "")), reverse=True)


def write_json(path: Path, data) -> None:
    """Write a record whole: a reader never finds half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _set_skill_path(config: Config, entry_id: str, skill_path: str) -> bool:
    from .ledger import Ledger
    ledger = Ledger(config)
    if not ledger.path_for(entry_id).exists():   # `get` would take a prefix
        return False
    entry = ledger.get(entry_id)
    entry.skill_path = skill_path
    ledger.save(entry)
    return True


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def archive(config: Config, project: str | Path, folder: Path) -> dict:
    """Move a skill out of the project into Skill++'s archive, where Claude
    Code no longer loads it. Nothing is deleted: a skill of the same name
    archived before stays, and this one is kept beside it as `name.2`.

    A skill installed from a draft takes its install record along, and the
    draft counts as not installed until the skill is restored.
    """
    record = _archive_record(config, project)
    root = archive_root(config, project)
    archive_id, n = folder.name, 2
    while archive_id in record["items"] or os.path.lexists(root / archive_id):
        archive_id, n = f"{folder.name}.{n}", n + 1
    front, _ = frontmatter(folder)
    entry_id, installed = install_index(config).get(os.path.realpath(folder), ("", {}))
    item = {"folder": folder.name, "name": override_key(folder, front),
            "description": str(front.get("description") or ""),
            "archived_at": _now(), "from": str(folder),
            "link": os.readlink(folder) if folder.is_symlink() else "",
            "entry": entry_id, "installed": installed}
    dest = root / archive_id
    root.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(folder), str(dest))
    except OSError:
        # A move across disks copies, then deletes. If the copy broke half
        # way, the skill is still where it was; take the partial copy away.
        if os.path.lexists(folder) and os.path.lexists(dest):
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        raise
    record["project"] = str(project)
    record["items"][archive_id] = item
    write_json(root / "archived.json", record)
    if entry_id:
        (draft_dir(config, entry_id) / "installed.json").unlink(missing_ok=True)
        _set_skill_path(config, entry_id, "")
    return {"archived": archive_id, "path": str(dest), "unlinked": entry_id}


def restore(config: Config, project: str | Path, archive_id: str) -> dict:
    """Put an archived skill back where it was. Refused, never forced, when
    something is already there."""
    record = _archive_record(config, project)
    item = record["items"].get(archive_id)
    folder = str(item.get("folder") or "") if isinstance(item, dict) else ""
    source = archive_root(config, project) / archive_id
    if not SAFE_NAME.match(folder) or not os.path.lexists(source):
        raise LookupError("not in the archive")
    if not Path(project).expanduser().is_dir():
        raise FileNotFoundError(f"{project} is gone; the skill stays archived")
    root = skills_dir(project)
    dest = root / folder
    if os.path.lexists(dest):
        raise FileExistsError(f"{dest} already exists; archive or rename it first")
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    del record["items"][archive_id]
    write_json(archive_root(config, project) / "archived.json", record)
    return {"path": str(dest), "relinked": _relink(config, item, dest),
            "reload": created}


def _relink(config: Config, item: dict, dest: Path) -> bool:
    """Record a restored skill as installed from its draft again, when it still
    is that install: the draft exists and was not installed anywhere since."""
    entry_id, installed = str(item.get("entry") or ""), item.get("installed") or {}
    if not entry_id or not installed or install_record(config, entry_id):
        return False
    if os.path.normpath(str(installed.get("path", ""))) != os.path.normpath(str(dest)):
        return False
    if not _set_skill_path(config, entry_id, str(dest / "SKILL.md")):
        return False
    write_json(draft_dir(config, entry_id) / "installed.json", installed)
    return True


# -- the gallery ---------------------------------------------------------------

def _card(config: Config, folder: Path, installs: dict, usage: dict, found: dict) -> dict:
    front, _ = frontmatter(folder)
    key = override_key(folder, front)
    files = skill_files(folder)
    meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    entry_id, record = installs.get(os.path.realpath(folder), ("", {}))
    drafted = drafted_skill(config, entry_id) if entry_id else None
    ours = meta.get("source") == "skill-plus-plus" or str(meta.get("provenance", "")).startswith("ledger:")
    used = usage.get(key) if isinstance(usage.get(key), dict) else {}
    return {
        "name": folder.name,
        "skill_name": key,
        "name_mismatch": key != folder.name,
        "description": str(front.get("description") or ""),
        "path": str(folder),
        "link": os.readlink(folder) if folder.is_symlink() else "",
        "files": [p.relative_to(folder).as_posix() for p in files[:20]],
        "file_count": len(files),
        "bytes": sum(p.lstat().st_size for p in files),
        "made_by": "draft" if entry_id else "skill-plus-plus" if ours else "",
        "entry": entry_id,
        "update": bool(drafted) and install_stale(record, drafted),
        "edit_block": edit_refusal(folder),
        "visibility": visibility(key, found),
        "uses": int(used.get("uses") or 0),
        "last_used": str(used.get("last_used") or ""),
    }


def list_project(config: Config, project: str, *, installs: dict | None = None,
                 usage: dict | None = None, home: Path | None = None) -> dict:
    """One project's gallery: its skills as cards, and the ones archived from
    it. Reads files only and starts no process, so the page can ask on every
    poll."""
    installs = install_index(config) if installs is None else installs
    usage = load_usage(config) if usage is None else usage
    root = skills_dir(project)
    found = read_overrides(project, home)
    return {"project": project,
            "name": Path(project).name,
            "exists": Path(project).expanduser().is_dir(),
            "linked": os.readlink(root) if root.is_symlink() else "",
            "settings_error": found["local_error"],
            "skills": [_card(config, folder, installs, usage, found)
                       for folder in skill_folders(project)],
            "archived": archived(config, project)}


def _read_head(path: Path, limit: int) -> bytes:
    """Up to *limit* bytes of a file that is not a link, checked at the open."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit)


def read_skill(folder: Path) -> dict:
    """Every file of a skill, for the viewer: text shown, binaries and links
    only named. *folder* was resolved by the caller; nothing here takes a path
    from a request."""
    files, shown, hidden = [], 0, 0
    for path in skill_files(folder):
        if len(files) >= READ_FILES_CAP:
            hidden += 1
            continue
        item: dict = {"path": path.relative_to(folder).as_posix(), "kind": "text",
                      "size": 0, "text": None, "truncated": False}
        files.append(item)
        if path.is_symlink():
            item.update(kind="link", target=os.readlink(path))
            continue
        try:
            item["size"] = path.lstat().st_size
            data = _read_head(path, READ_FILE_CAP + 1)
        except OSError as exc:
            item.update(kind="unreadable", error=exc.strerror or str(exc))
            continue
        cut = len(data) > READ_FILE_CAP
        try:
            text = data[:READ_FILE_CAP].decode("utf-8", errors="ignore" if cut else "strict")
        except UnicodeDecodeError:
            text = None
        if text is None or "\0" in text[:8192]:
            item["kind"] = "binary"
            continue
        if shown + len(text) > READ_TOTAL_CAP:
            item["truncated"] = True
            continue
        shown += len(text)
        item.update(text=text, truncated=cut)
    return {"files": files, "hidden": hidden}


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
# Applied edits kept per skill, each with the version it replaced.
HISTORY_KEPT = 10
# The frontmatter keys claude.ai uploads and the Skills API accept. Claude Code
# reads more, but a skill carrying any other key can no longer be uploaded.
UPLOAD_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")


def edit_root(config: Config, project: str | Path, folder_name: str) -> Path:
    return config.root / "edits" / project_key(project) / folder_name


def edit_dir(config: Config, project: str | Path, folder_name: str) -> Path:
    """The one edit of this skill in progress or waiting: `status.json`,
    `base.json`, `base/`, `proposal/` and `agent.log`."""
    return edit_root(config, project, folder_name) / "current"


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


def _sync_draft(config: Config, entry_id: str, proposal: Path, before: dict, after: dict) -> None:
    """Give the draft a skill was installed from the same edit, so the page's
    Update and Uninstall keep treating the installed copy as theirs. The
    draft's SKILL.md before it is kept in `.revisions/`, as a revision's is."""
    root = draft_dir(config, entry_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    history = root / ".revisions"
    history.mkdir(parents=True, exist_ok=True)
    if (root / "SKILL.md").is_file():
        shutil.copy2(root / "SKILL.md", history / f"SKILL.{stamp}.md")
    for rel, want in after.items():
        if before.get(rel) != want:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(proposal / rel, root / rel)
    for rel in before:
        if rel not in after and (root / rel).is_file():
            (history / stamp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root / rel), str(history / stamp / rel))


def apply_edit(config: Config, project: str | Path, folder: Path) -> dict:
    """Write a proposed edit into the skill.

    Refused when the skill changed after the agent's copy was taken: the
    proposal was made against files that are no longer there. On a failed
    write, what was written is put back from the copy. The replaced version
    goes to `history/`, with the last HISTORY_KEPT edits kept.
    """
    current = edit_dir(config, project, folder.name)
    base = json.loads((current / "base.json").read_text(encoding="utf-8"))
    before = base["files"]
    if not folder.is_dir() or folder.is_symlink() or snapshot(folder) != before:
        raise RuntimeError("the skill changed after the agent started; discard this edit "
                           "and ask again")
    why = check_proposal(current / "proposal", base)
    if why:
        raise RuntimeError(why)
    after = snapshot(current / "proposal")
    changed = [rel for rel in sorted(set(before) | set(after), key=_order)
               if before.get(rel) != after.get(rel)]
    done: list[str] = []
    try:
        for rel in changed:
            if rel in after:
                _write_into(folder, rel, current / "proposal" / rel)
            else:
                (folder / rel).unlink()
            done.append(rel)
    except OSError:
        for rel in done:
            if rel in before:
                _write_into(folder, rel, current / "base" / rel)
            else:
                (folder / rel).unlink(missing_ok=True)
        _prune_empty(folder)
        raise
    _prune_empty(folder)
    entry_id, record = install_index(config).get(os.path.realpath(folder), ("", {}))
    if entry_id:
        _sync_draft(config, entry_id, current / "proposal", before, after)
        write_json(draft_dir(config, entry_id) / "installed.json",
                   {**record, "files": after, "edited_at": _now()})
    history = edit_root(config, project, folder.name) / "history"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    history.mkdir(parents=True, exist_ok=True)
    os.replace(current, history / stamp)
    write_json(history / stamp / "applied.json", {"at": _now(), "changed": changed})
    for old in sorted(history.iterdir())[:-HISTORY_KEPT]:
        shutil.rmtree(old, ignore_errors=True)
    return {"changed": changed, "synced_draft": entry_id}


def discard_edit(config: Config, project: str | Path, folder_name: str) -> None:
    """Drop a proposed edit. Only Skill++'s own copy goes; the skill is untouched."""
    shutil.rmtree(edit_dir(config, project, folder_name), ignore_errors=True)
