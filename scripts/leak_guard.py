#!/usr/bin/env python3
"""Fail if anything private is about to be published.

    python3 scripts/leak_guard.py                      # tracked files
    python3 scripts/leak_guard.py --history            # every commit, and its authors
    python3 scripts/leak_guard.py --denylist PATH      # plus your own terms

Two layers. The patterns below are generic and safe to publish: home paths,
email addresses, full UUIDs and credential-shaped strings, each with the
synthetic values the tests use allowed by name. The terms that are actually
sensitive to you (an employer, an internal project, a colleague, a hostname)
cannot live here, because a public list of them publishes them. Keep those in a
file outside the repo, one per line, and pass it with `--denylist` or
`SKILL_PLUS_PLUS_DENYLIST`; they are matched case-insensitively.

Exits 1 and prints every hit as `where: pattern: text`.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Home directories other than the synthetic `dev` the tests use.
HOME_PATH = re.compile(r"/(?:Users|home)/(?!dev\b)[A-Za-z][A-Za-z0-9._-]*")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Domains that are placeholders, SCP-style git remotes, or not an address at
# all (`python@3.14` is a Homebrew formula).
EMAIL_ALLOWED = re.compile(
    r"@(?:(?:[\w-]+\.)*example\.(?:com|org|net)|acme\.co|corp\.com|b\.com|"
    r"github\.com|gitlab\.internal|bitbucket\.org|anthropic\.com|"
    r"users\.noreply\.github\.com|3\.14)$", re.IGNORECASE)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                  re.IGNORECASE)
# A file uploaded to GitHub, such as the README's video, is linked by a UUID
# that names the file, not a session.
UUID_ALLOWED_AFTER = "github.com/user-attachments/assets/"
SECRET = re.compile(r"ghp_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}"
                    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}")
# The fake credentials the scrubber's own tests and the demo are built around.
SECRET_ALLOWED = {"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8", "AKIAIOSFODNN7EXAMPLE",
                  "xoxb-123456789012-abcdefghijkl"}
# This file names the patterns it looks for.
SKIP = {"scripts/leak_guard.py"}


def denylist(path: str | None) -> list[re.Pattern]:
    """Each term as a whole word, case-insensitive. A plain substring found
    `cat` inside `concatenate`. Underscores count as a boundary, so
    `cat_project` still matches."""
    source = path or os.environ.get("SKILL_PLUS_PLUS_DENYLIST")
    if not source:
        return []
    terms = []
    for line in Path(source).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(line)}(?![A-Za-z0-9])",
                                    re.IGNORECASE))
    return terms


def scan_text(where: str, text: str, terms: list[re.Pattern],
              generic: bool = True) -> list[str]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        found = []
        if generic:
            found += [("home path", m.group()) for m in HOME_PATH.finditer(line)]
            found += [("email", m.group()) for m in EMAIL.finditer(line)
                      if not EMAIL_ALLOWED.search(m.group())]
            found += [("uuid", m.group()) for m in UUID.finditer(line)
                      if not line[:m.start()].endswith(UUID_ALLOWED_AFTER)]
            found += [("secret", m.group()) for m in SECRET.finditer(line)
                      if m.group() not in SECRET_ALLOWED]
        found += [("denylist", m.group()) for term in terms for m in term.finditer(line)]
        hits += [f"{where}:{number}: {kind}: {value}" for kind, value in found]
    return hits


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                         check=True).stdout.decode("utf-8")
    return [REPO / name for name in out.split("\0") if name and name not in SKIP]


def scan_tree(terms: list[re.Pattern]) -> list[str]:
    hits = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += scan_text(str(path.relative_to(REPO)), text, terms)
    return hits


def scan_history(terms: list[re.Pattern]) -> list[str]:
    """Every commit's diff and message, and every author and committer."""
    log = subprocess.run(["git", "log", "--all", "-p", "--format=commit %H%n%B"],
                         cwd=REPO, capture_output=True, check=True
                         ).stdout.decode("utf-8", errors="replace")
    hits, commit, buffer = [], "?", []

    def flush():
        if buffer:
            hits.extend(scan_text(f"commit {commit[:10]}", "\n".join(buffer), terms))
            buffer.clear()

    for line in log.splitlines():
        if line.startswith("commit ") and len(line) == 47:
            flush()
            commit = line[7:]
            continue
        # This file's own diffs name the patterns it looks for.
        if line.startswith(("+++ b/scripts/leak_guard.py", "--- a/scripts/leak_guard.py")):
            continue
        buffer.append(line)
    flush()
    people = subprocess.run(["git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>"],
                            cwd=REPO, capture_output=True, check=True
                            ).stdout.decode("utf-8", errors="replace")
    # An author's address is public by design, and the mailmap decides it; what
    # matters here is whether one still names something on the denylist.
    hits += scan_text("authors", "\n".join(sorted(set(people.splitlines()))), terms,
                      generic=False)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--history", action="store_true",
                        help="scan every commit on every branch instead of the tree")
    parser.add_argument("--denylist", help="file of private terms, one per line")
    args = parser.parse_args(argv)
    terms = denylist(args.denylist)
    hits = scan_history(terms) if args.history else scan_tree(terms)
    for hit in hits:
        print(hit)
    scope = "history" if args.history else "tracked files"
    extra = f", plus {len(terms)} private term(s)" if terms else ""
    print(f"{len(hits)} hit(s) in {scope}{extra}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
