#!/usr/bin/env python3
"""The README as PyPI will show it: every relative link and image made absolute.

    python3 scripts/pypi_readme.py              # print the rewritten README
    python3 scripts/pypi_readme.py --in-place   # rewrite README.md (the release build does)

PyPI shows README.md as the project page, and a relative path such as
`docs/images/banner-light.svg` resolves against pypi.org there, where nothing
is. The README in the repo keeps its relative paths, because GitHub resolves
those against the branch being viewed, and does so while the repo is private;
an absolute link into a private repo loads nothing, even on GitHub. So only the
copy a release is built from is rewritten: images point at the raw file, links
at the file or folder on GitHub, both on `main`. Anchors and absolute URLs are
left as they are.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = "himanshu096/skill-plus-plus"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/"
BLOB = f"https://github.com/{REPO}/blob/main/"
TREE = f"https://github.com/{REPO}/tree/main/"
README = Path(__file__).resolve().parent.parent / "README.md"

# A target is relative when it has no scheme and is not an anchor or a
# site-absolute path.
_RELATIVE = r"(?![a-z][a-z0-9+.-]*:|#|/)([^\"')\s]+)"
_IMAGE = re.compile(r'\b(src|srcset)="' + _RELATIVE + '"')
_HREF = re.compile(r'\bhref="' + _RELATIVE + '"')
_MARKDOWN = re.compile(r"(!?)(\[[^\]\n]*\])\(" + _RELATIVE + r"\)")


def _link(path: str) -> str:
    return (TREE if path.endswith("/") else BLOB) + path.removeprefix("./")


def absolute(text: str) -> str:
    """*text* with every relative image and link target made absolute."""
    text = _IMAGE.sub(lambda m: f'{m.group(1)}="{RAW}{m.group(2).removeprefix("./")}"', text)
    text = _HREF.sub(lambda m: f'href="{_link(m.group(1))}"', text)
    return _MARKDOWN.sub(lambda m: f"{m.group(1)}{m.group(2)}("
                         + (RAW + m.group(3).removeprefix("./") if m.group(1) else _link(m.group(3)))
                         + ")", text)


def main(argv: list[str]) -> int:
    text = absolute(README.read_text(encoding="utf-8"))
    if "--in-place" in argv:
        README.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
