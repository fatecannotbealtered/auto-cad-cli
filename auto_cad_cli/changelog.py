"""Runtime view of `CHANGELOG.md` - the single human-maintained change source.

REPO-SPEC forbids a second hand-maintained copy: the release notes and this
command are both derived from the same file, so an agent that self-updates can
ask what changed without anyone curating a parallel list (CLI-SPEC section 11).
"""

from __future__ import annotations

import re
from pathlib import Path

# Keep a Changelog categories, in the order the spec lists them.
CATEGORIES = ("added", "changed", "fixed", "deprecated", "removed", "security")

_VERSION_HEADING = re.compile(r"^##\s*\[([^\]]+)\]\s*(?:-\s*(\d{4}-\d{2}-\d{2}))?\s*$")
_CATEGORY_HEADING = re.compile(r"^###\s+(\w+)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")


def _locate() -> Path | None:
    """CHANGELOG.md sits beside the package in a checkout and inside a frozen build."""
    here = Path(__file__).resolve()
    for candidate in (here.parent / "CHANGELOG.md", here.parent.parent / "CHANGELOG.md"):
        if candidate.is_file():
            return candidate
    return None


def _strip_comments(text: str) -> str:
    """Drop HTML comments so the commented-out release template is not parsed."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def parse(text: str) -> list[dict]:
    """Turn the markdown into `entries[].{version, date, changes{...}}`."""
    entries: list[dict] = []
    current: dict | None = None
    category: str | None = None

    for line in _strip_comments(text).splitlines():
        heading = _VERSION_HEADING.match(line)
        if heading:
            current = {
                "version": heading.group(1),
                "date": heading.group(2),
                "changes": {name: [] for name in CATEGORIES},
            }
            entries.append(current)
            category = None
            continue
        if current is None:
            continue
        section = _CATEGORY_HEADING.match(line)
        if section:
            name = section.group(1).lower()
            category = name if name in CATEGORIES else None
            continue
        bullet = _BULLET.match(line.strip())
        if bullet and category:
            current["changes"][category].append(bullet.group(1).strip())

    return entries


def _sort_key(version: str) -> tuple:
    """Numeric semver ordering; `Unreleased` sorts above every release."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return (1,)
    return (0, *(int(part) for part in version.split(".")))


def newer_than(entries: list[dict], since: str) -> list[dict]:
    """Entries strictly newer than `since`, for an agent catching up after an update."""
    floor = _sort_key(since)
    return [e for e in entries if _sort_key(e["version"]) > floor]


def load() -> list[dict]:
    path = _locate()
    if path is None:
        return []
    return parse(path.read_text(encoding="utf-8"))
