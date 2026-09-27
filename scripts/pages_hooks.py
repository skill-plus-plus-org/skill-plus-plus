"""MkDocs hooks for the GitHub Pages site (`mkdocs.yml`, `.github/workflows/pages.yml`).

    pip install "mkdocs-material>=9.5,<10" && mkdocs serve

The site is `docs/` with the README as its home page. The README stays at the
repo root, where GitHub shows it, so the home page is generated from it on each
build rather than kept as a second copy: a path into `docs/` loses that prefix,
a path to anything else in the repo (LICENSE, CONTRIBUTING.md, tests/) becomes
a link to the file on GitHub, and the pages that link back to `../README.md`
link to the home page instead.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pypi_readme  # noqa: E402  the repo's link targets on GitHub live there

# A target is relative when it has no scheme and is not an anchor or a
# site-absolute path; the same rule pypi_readme.py applies.
_RELATIVE = r"(?![a-z][a-z0-9+.-]*:|#|/)([^\"')\s]+)"
_ATTR = re.compile(r'\b(src|srcset|href)="' + _RELATIVE + '"')
_MARKDOWN = re.compile(r"(!?\[[^\]\n]*\])\(" + _RELATIVE + r"\)")
# GitHub embeds an uploaded video from its bare link on a line of its own.
_VIDEO = re.compile(r"^(https://github\.com/user-attachments/assets/[0-9a-f-]+)$", re.M)


def _target(path: str) -> str:
    path = path.removeprefix("./")
    if path.startswith("docs/"):
        return path.removeprefix("docs/")
    return pypi_readme._link(path)


def _href(path: str) -> str:
    """MkDocs rewrites Markdown links to its page URLs but leaves raw HTML
    alone, so an `href` names the page's URL itself: `usage.md` is `usage/`."""
    target = _target(path)
    if "://" in target:
        return target
    target = re.sub(r"(?:^|(?<=/))(?:README|index)\.md$", "", target)
    return re.sub(r"\.md$", "/", target)


def home(readme: str) -> str:
    """*readme* as the site's home page."""
    text = _ATTR.sub(lambda m: f'{m.group(1)}="'
                     + (_href if m.group(1) == "href" else _target)(m.group(2)) + '"', readme)
    text = _MARKDOWN.sub(lambda m: f"{m.group(1)}({_target(m.group(2))})", text)
    # GitHub renders Markdown inside these; Python-Markdown only when asked.
    text = text.replace('<div align="center">', '<div align="center" markdown>')
    return _VIDEO.sub(r'<video src="\1" controls muted playsinline style="width:100%"></video>', text)


def on_files(files, config):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    files.append(File.generated(config, "index.md", content=home(readme)))
    return files


def on_page_markdown(markdown, page, config, files):
    """A link to a folder names the folder's README, which MkDocs resolves; a
    link back to the repo's README names the home page."""
    markdown = re.sub(r"\]\(((?:\.\./)*research/)\)", r"](\1README.md)", markdown)
    if page.file.src_uri == "index.md":
        return markdown
    up = "../" * page.file.src_uri.count("/")
    return re.sub(r"\]\((?:\.\./)+README\.md", f"]({up}index.md", markdown)
