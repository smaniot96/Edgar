"""Reversible library visibility, separate from retained campaign and preparation data."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import adventures_meta


def _path() -> Path:
    path = adventures_meta.ADVENTURES_DIR.parent / "campaign-library.json"
    if path.is_symlink():
        raise ValueError("Campaign library preferences cannot follow a symlink.")
    return path


def _preferences() -> dict:
    path = _path()
    if not path.exists():
        return {"version": 1, "archived_slugs": [], "titles": {}}
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("archived_slugs"), list):
        raise ValueError("Invalid campaign library preferences.")
    slugs = data["archived_slugs"]
    if any(not adventures_meta.valid_adventure_slug(slug) for slug in slugs):
        raise ValueError("Invalid archived campaign binding.")
    titles = data.setdefault("titles", {})
    if not isinstance(titles, dict) or any(
        not adventures_meta.valid_adventure_slug(slug)
        or not isinstance(title, str) or not title.strip() or len(title) > 255
        for slug, title in titles.items()
    ):
        raise ValueError("Invalid adventure display title.")
    return data


def archived_slugs() -> set[str]:
    return set(_preferences()["archived_slugs"])


def display_title(slug: str, fallback: str) -> str:
    """A library label can change without rewriting an accepted preparation or save."""
    return _preferences()["titles"].get(slug, fallback)


def set_archived(slug: str, archived: bool) -> None:
    set_preferences(slug, archived=archived)


def set_preferences(slug: str, *, archived: bool | None = None, title: str | None = None) -> None:
    """Change only the shared library index; source bytes and saved play stay untouched."""
    if not adventures_meta.valid_adventure_slug(slug):
        raise ValueError("Invalid adventure source binding.")
    if title is not None and (not title.strip() or len(title) > 255):
        raise ValueError("Invalid adventure display title.")
    with adventures_meta.lifecycle_lock("campaign-library"):
        data = _preferences()
        slugs = set(data["archived_slugs"])
        if archived is True:
            slugs.add(slug)
        elif archived is False:
            slugs.discard(slug)
        data["archived_slugs"] = sorted(slugs)
        if title is not None:
            data["titles"][slug] = title.strip()
        path = _path()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as file:
                temporary = Path(file.name)
                json.dump(data, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
