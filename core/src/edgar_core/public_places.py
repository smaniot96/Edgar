"""Location labels derived only from information already public to the player."""

from __future__ import annotations

import re


def public_place_label(text: str | None, name: str = "", *, limit: int = 64) -> str:
    """Use an earned name, otherwise a short verbatim part of the visible description.

    Private scene titles are never a fallback. Keeping the fallback a literal substring
    also lets the normal earned-name boundary verify it without a new disclosure rule.
    """
    text = " ".join((text or "").split())
    if name and name.casefold() not in {"scene", "place", "entity", "known place"}:
        if name.casefold() in text.casefold():
            return name[:200]
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].rstrip(".!?")
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip(",;:—-")
    return first
