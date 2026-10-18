"""Dictionary-free OCR-signature scores for native text layers.

A native layer produced by an earlier OCR run can contain systematic misreadings of rules
vocabulary ("levei" for "level", "Equlpment" for "Equipment", "ld12" for "1d12"). These
helpers measure only that signature inside a tiny built-in vocabulary. They never correct
or rewrite text; the caller decides which source to keep.
"""

import re

# Rules vocabulary that native layers most often mangle, plus frequent English function
# words used only to estimate how much of a page is recognisable running text.
RULES_TERMS = frozenset(
    """
    level levels hit points point equipment armor armour class classes challenge speed
    saving throw throws spell spells slot slots creature creatures action actions bonus
    reaction reactions legendary lair mythic damage resistance resistances immunity
    immunities vulnerability vulnerabilities senses languages proficiency proficient skills
    skill strength dexterity constitution intelligence wisdom charisma ability abilities
    score scores modifier modifiers feature features trait traits rest short long hours
    minute minutes round rounds turn turns attack attacks weapon weapons shield magic item
    items character characters player players dungeon master rules rule check checks
    condition conditions chapter table tool tools background backgrounds race races
    spellcasting cantrip cantrips ritual component components range duration concentration
    dice die experience gold
    """.split()
)
COMMON_WORDS = frozenset(
    """
    the and for with that this from its are was were you your can may each one two any not
    has have been would must use which their they there then than only also into more such
    other will while does make made take after before during where when
    """.split()
)
VOCABULARY = RULES_TERMS | COMMON_WORDS

# Single-character confusions observed in OCR output: l/I/1 and e/c.
_SWAPS = {"i": "l1", "l": "i1", "1": "il", "e": "c", "c": "e"}
# A dice count read as a letter ("ld12", "Id4") rather than "1d12"; the correct form never matches.
_DICE_SIGNATURE = re.compile(r"[il]d\d+", re.IGNORECASE)
DEGRADED_SIGNATURE_RATE = 0.005
MIN_SIGNATURE_HITS = 2


def _signature_hit(token: str) -> bool:
    if _DICE_SIGNATURE.fullmatch(token):
        return True
    if token in VOCABULARY:
        return False
    for index, char in enumerate(token):
        for replacement in _SWAPS.get(char, ""):
            if token[:index] + replacement + token[index + 1 :] in VOCABULARY:
                return True
    return False


def score_text(text: str) -> dict:
    """Signature hits, rate and known-word rate for one page's selected text.

    `ocr_signature_score` is 1 minus the signature rate (higher is better). `known_word_rate`
    is the share of alphabetic words found in the built-in vocabulary. Both are recorded for
    native and OCR candidates so a reviewer can see why a source was kept.
    """
    words = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", text)
        if len(token) >= 3 and re.search(r"[A-Za-z]", token)
    ]
    hits = sum(1 for word in words if _signature_hit(word))
    alphabetic = [word for word in words if word.isalpha()]
    known = sum(1 for word in alphabetic if word in VOCABULARY)
    rate = hits / max(1, len(words))
    return {
        "signature_hits": hits,
        "signature_rate": round(rate, 6),
        "known_word_rate": round(known / max(1, len(alphabetic)), 4),
        "ocr_signature_score": round(1 - rate, 6),
        "words": len(words),
    }


def is_degraded(scores: dict) -> bool:
    """A few misreadings are weak evidence; a repeated, dense signature triggers OCR."""
    return (
        scores["signature_hits"] >= MIN_SIGNATURE_HITS
        and scores["signature_rate"] >= DEGRADED_SIGNATURE_RATE
    )


def prefer_ocr(native: dict, ocr: dict) -> bool:
    """OCR wins on a strictly lower signature rate, or an equal rate and more known words."""
    if ocr["signature_rate"] != native["signature_rate"]:
        return ocr["signature_rate"] < native["signature_rate"]
    return ocr["known_word_rate"] > native["known_word_rate"]
