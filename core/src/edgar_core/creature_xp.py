"""Independent verified CR/XP parser used by the combat engine."""

import re

_CR_XP = {
    "0": 10,
    "1/8": 25,
    "1/4": 50,
    "1/2": 100,
    "1": 200,
    "2": 450,
    "3": 700,
    "4": 1100,
    "5": 1800,
    "6": 2300,
    "7": 2900,
    "8": 3900,
    "9": 5000,
    "10": 5900,
    "11": 7200,
    "12": 8400,
    "13": 10000,
    "14": 11500,
    "15": 13000,
    "16": 15000,
    "17": 18000,
    "18": 20000,
    "19": 22000,
    "20": 25000,
    "21": 33000,
    "22": 41000,
    "23": 50000,
    "24": 62000,
    "25": 75000,
    "26": 90000,
    "27": 105000,
    "28": 120000,
    "29": 135000,
    "30": 155000,
}
_CHALLENGE_LINE = re.compile(
    r"^Challenge\s+(\d+(?:/\d+)?)(?:\s+\((\d{1,3}(?:\s*,\s*\d{3})+|\d+)\s+XP\))?\s*$", re.IGNORECASE
)


def challenge_xp_from_stat_block(text: str) -> tuple[str, int]:
    """Read and cross-check the printed CR/XP line; never accept model-supplied numbers."""
    matches = [
        _CHALLENGE_LINE.fullmatch(line.strip().strip("*_`"))
        for line in text.splitlines()
        if line.strip()
    ]
    rows = [match for match in matches if match is not None]
    if len(rows) != 1:
        raise ValueError("Exact creature record must contain one complete Challenge/XP line.")
    rating = rows[0].group(1)
    printed_xp = rows[0].group(2)
    expected_xp = _CR_XP.get(rating)
    if expected_xp is None or printed_xp is None:
        raise ValueError("Creature CR/XP is missing or outside the engine's 2014 table.")
    if int(re.sub(r"[\s,]", "", printed_xp)) != expected_xp:
        raise ValueError("Printed XP conflicts with the engine-owned 2014 CR/XP table.")
    return rating, expected_xp
