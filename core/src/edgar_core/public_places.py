"""Location labels derived only from information already public to the player."""

from __future__ import annotations

import re

_PLACE_WORDS = frozenset(
    "alley bay bridge camp castle cave chamber city clearing compound courtyard crossroads "
    "dungeon ferry forest garden gate hall hamlet harbor harbour hideout hill home house inn island library "
    "keep lake landing lodge manor market meadow mill mountain outpost path platform pool quay road room "
    "ruins sanctuary settlement shop shrine square stable street tavern temple tower town "
    "towpath track trail village warehouse woods".split()
)
_NAME_CONNECTORS = frozenset({"a", "an", "the", "of", "and", "at", "on", "in", "by"})


def public_place_label(text: str | None, name: str = "", *, limit: int = 64) -> str:
    """Use an earned short place name or a physical description from public text.

    Arrival prose describes an event; its first words are not a geographic identity.
    A supplied prepared name is eligible only when the exact phrase is public and its
    wording or public context identifies a place. Otherwise a short geographic noun
    phrase can name an unnamed location without disclosing a prepared event title.
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
        return _descriptive_place_label(text, limit)
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
    return _descriptive_place_label(text, limit)


def _descriptive_place_label(text: str, limit: int) -> str:
    """A physical noun phrase quoted from earned prose, never a private event title.

    Descriptive names remain useful when the author called an event 'An unexpected
    guest', but the arrival actually describes a market town or a woodland path.
    Keep the phrase short and grounded; do not promote a whole arrival sentence.
    """
    words = "|".join(sorted(_PLACE_WORDS, key=len, reverse=True))
    match = re.search(
        rf"\b(?:the|a|an)\s+((?:[\w’'-]+\s+){{0,2}}?(?:{words}))\b",
        text,
        re.IGNORECASE,
    )
    if match:
        phrase = match.group(1)
        # Do not turn an actor's action ('a sheep near the road') into a place name.
        if not re.search(r"\b(?:near|at|in|on|to|from|of|and|with|is|are)\b", phrase, re.I):
            return phrase[:1].upper() + phrase[1:limit]
    return "Unidentified place"
