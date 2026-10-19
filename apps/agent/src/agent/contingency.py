"""Ordered conditions in a combat turn (EDG-182, contingencies).

A turn may end with one "if" clause: the action first, then a branch the engine runs only if the
condition holds once that action has resolved. The engine evaluates two conditions, both on state it
records:

- `target_down`: the foe the turn's attack names is no longer standing ("if it drops, ...").
- `hp_below`: the hero's hit points are below N ("if I fall below 5 HP, ..."); `hp_at_or_below` for
  "at or below N".

The only branch the engine runs is a move, since a move takes no action (PHB p.192). A second attack
or any other action needs its own action, and the turn spends one (PHB p.189), so it is refused with
that ruling. When the condition does not hold, the branch is not taken, and the round says so. A clause
the engine cannot read is asked about, never dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_DOWN = "target_down"
HP_BELOW = "hp_below"
HP_AT_OR_BELOW = "hp_at_or_below"

_IF = re.compile(r"\bif\b", re.IGNORECASE)
_TARGET_DOWN = re.compile(
    r"\bif\s+(?:it|he|she|they|them|the\s+[\w\s]{1,30}?)\s+"
    r"(?:drops?|dies|falls?|goes\s+down|is\s+(?:down|dead|defeated|slain|knocked\s+out))\b",
    re.IGNORECASE,
)
_HP_BELOW = re.compile(
    r"\bif\s+[^.;,]{0,40}?\b(?:i|my\s+(?:hp|hit\s+points))\b[^.;,]{0,30}?\b"
    r"(?P<or_equal>at\s+or\s+)?(?:below|under)\s+(?P<n>\d+)\s*(?:hp|hit\s+points)?\b",
    re.IGNORECASE,
)
_BRANCH_LEAD = re.compile(r"^\s*(?:,|then\b|and\s+then\b|:)?\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Contingency:
    condition: str  # TARGET_DOWN, HP_BELOW or HP_AT_OR_BELOW
    threshold: int | None  # the hit points, for the HP conditions
    clause: str  # the condition as the player wrote it, for the summary and the round's log
    branch: str  # what the player says to do if it holds

    def describe(self) -> str:
        return f"if {self.clause}"


@dataclass(frozen=True)
class SplitTurn:
    main: str  # the turn without its conditional clause
    contingency: Contingency | None
    unreadable_if: str | None  # an "if" the engine cannot read, as written (asked, never dropped)


def split_contingency(text: str | None) -> SplitTurn:
    """Split one "if" clause off the turn, if there is one, and read its condition.

    Any "if" the engine cannot read as a condition is returned as `unreadable_if`, so the turn is asked
    about it rather than run without it.
    """
    body = str(text or "")
    match = _IF.search(body)
    if match is None:
        return SplitTurn(main=body, contingency=None, unreadable_if=None)
    head = re.sub(r"(?:[\s,;.]*\b(?:and|then))*[\s,;.]*$", "", body[: match.start()], flags=re.I)
    rest = body[match.start() :]
    found = None
    for pattern, kind in ((_TARGET_DOWN, TARGET_DOWN), (_HP_BELOW, None)):
        hit = pattern.match(rest)
        if hit:
            found = (kind, hit)
            break
    if found is None:
        return SplitTurn(main=body, contingency=None, unreadable_if=rest.strip())
    kind, hit = found
    if kind is None:
        n = int(hit.group("n"))
        condition = HP_AT_OR_BELOW if hit.group("or_equal") else HP_BELOW
        threshold = n
    else:
        condition, threshold = kind, None
    clause = rest[len("if") : hit.end()].strip()
    branch = _BRANCH_LEAD.sub("", rest[hit.end() :]).strip().rstrip(".")
    if not branch or not head:
        return SplitTurn(main=body, contingency=None, unreadable_if=rest.strip())
    return SplitTurn(
        main=head,
        contingency=Contingency(condition, threshold, clause, branch),
        unreadable_if=None,
    )


def holds(contingency: Contingency, *, target_standing: bool | None, hero_hp: int) -> bool:
    """Whether the condition holds once the turn's action has resolved."""
    if contingency.condition == TARGET_DOWN:
        return target_standing is False
    if contingency.condition == HP_AT_OR_BELOW:
        return hero_hp <= int(contingency.threshold or 0)
    return hero_hp < int(contingency.threshold or 0)


def not_held_text(contingency: Contingency) -> str:
    return f"{contingency.describe()} did not hold, so nothing else is done this turn."
