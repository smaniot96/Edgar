"""Location labels derived only from information already public to the player."""

from __future__ import annotations

import re

_PLACE_WORDS = frozenset(
    "alley bay bridge camp castle cave chamber city clearing compound courtyard crossroads "
    "dungeon ferry forest gate hall hamlet harbor harbour hideout hill home house inn island "
    "keep lake landing lodge manor market meadow mill mountain outpost path quay road room "
    "ruins sanctuary settlement shop shrine square stable street tavern temple tower town "
    "towpath trail village warehouse".split()
)
_NAME_CONNECTORS = frozenset({"a", "an", "the", "of", "and", "at", "on", "in", "by"})


def public_place_label(text: str | None, name: str = "", *, limit: int = 64) -> str:
    """Use an explicitly public short place name, otherwise a stable unnamed label.

    Arrival prose describes an event; its first words are not a geographic identity.
    A supplied prepared name is eligible only when the exact phrase is public and its
    wording or public context identifies a place. No place is inferred from the prose.
    """
    text = " ".join((text or "").split())
    name = " ".join((name or "").split())
    words = name.split()
    if (
        not words
        or len(words) > 8
        or len(name) > limit
        or re.search(r"[.,;:!?()\[\]]", name)
        or name.casefold()
        in {"scene", "place", "entity", "known place", "current location", "unidentified place"}
        or not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
    ):
        return "Unidentified place"
    named_words = [word.strip("-'’").casefold() for word in words]
    name_like = all(
        word[:1].isupper() or normalized in _NAME_CONNECTORS | _PLACE_WORDS
        for word, normalized in zip(words, named_words, strict=True)
    )
    place_word = bool(set(named_words) & _PLACE_WORDS)
    place_context = re.search(
        rf"\b(?:at|in|inside|outside|near|toward|towards|through|from|to|called|named|enter|reach)\s+"
        rf"(?:the\s+)?{re.escape(name)}(?!\w)",
        text,
        re.IGNORECASE,
    )
    if name_like and (place_word or place_context):
        return name
    return "Unidentified place"
