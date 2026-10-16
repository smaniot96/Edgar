"""Combatant disposition: the single source of truth for who is living, who can act (EDG-022/023).

Stored on every combatant:
  * `disposition`: one of DISPOSITIONS. `active` (standing in the fight), `unconscious` (living,
    knocked out by a declared nonlethal blow), `captured` (living captive after a victory),
    `dead` (lethal blow), `defeated` (reduced to 0 HP with no declared intent; the cause is
    unknown, so it is never narrated as death or capture).
  * `alive`: kept for existing readers with the living-vs-not meaning. True only for living
    dispositions (active, unconscious, captured). False for dead and defeated.

Derived, never stored: `can_act` (active, above 0 HP and neither paralyzed nor stunned). Only a
combatant that can act attacks. `is_standing` is the same without the paralysis: a paralyzed foe is
still in the fight (it can be targeted, it still has a turn, and the fight is not over while it stands).

`effects` (EDG-021): a list of typed combat effects, each `{kind, source, applied_round}`. Only
the kinds in `EFFECT_KINDS` are read. Restraint (`restrained`) and `prone` come from a supported
contested maneuver, persist in this same JSONB list, and end explicitly: a declared release,
the foe's defeat, or the end of the encounter. `attack_roll_mode` turns them into the 5e
advantage/disadvantage state of an attack. Missing or malformed `effects` reads as none.
`dodging` (EDG-044) is the Dodge action: attacks against that combatant have disadvantage and its
Dexterity saves advantage (EDG-043, `save_roll_mode`). It ends when the combatant's next turn starts
(see `agent.graph_combat`), not at the end of a round.

Spell conditions (EDG-043, see `agent.conditions`) add `blinded`, `marked` and `paralyzed` to the same
list. Monster riders and save actions add `poisoned`, `frightened` and `stunned` to a player. They
may also carry `expires_after_round` (the last round in force) and `save` (`{ability, dc}`: the
combatant repeats that save at the end of each of its turns, and the effect ends on success), or
`ends_with_turn_of` (the creature at whose next turn's end the effect ends). Rows written without
those keys read exactly as before.
A caster's concentration (`concentration` on the player combatant, see `agent.conditions`) is a
single dict, or absent. Recharge attacks used and not yet recharged, and once-per-fight actions used,
are `recharge_spent` names. A legendary creature's spent legendary actions are `legendary_spent`
(EDG-043): absent reads as none spent, so a row written before legendary actions has its full budget.

Legacy rows (written before `disposition` existed) carry only `alive`/`hp_current`. They are
read honestly: `alive` False means "defeated" (cause unknown). It is not treated as proof of
death or capture, and no nonlethal intent is invented. No stored data is rewritten.

After the fight (EDG-146), captured and dead foes are session NPC rows (`captive`, `dead`), and
every captured, dead or defeated outcome is an `encounter` fact in the story ledger. A player may
then release a captive (`released`) or kill it on an explicit lethal declaration (`dead`); see
`agent.captives`. The ended combat record itself is never rewritten.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agent.monster_stats import narrator_note
from agent.positioning import band_of, band_text

Lethality = Literal["lethal", "nonlethal"]

DISPOSITIONS: tuple[str, ...] = ("active", "unconscious", "captured", "dead", "defeated")
LIVING_DISPOSITIONS = frozenset({"active", "unconscious", "captured"})
# Display vocabulary for the narrator. Never claims death or capture for `defeated`.
_STATUS_TEXT = {
    "active": "active, can act",
    "unconscious": "unconscious, cannot act",
    "captured": "captured, cannot act",
    "dead": "dead, cannot act",
    "defeated": "defeated, cannot act",
}
EFFECT_KINDS: tuple[str, ...] = (
    "restrained",
    "prone",
    "dodging",
    "blinded",
    "marked",
    "paralyzed",
    "poisoned",
    "frightened",
    "stunned",
)
# Kinds that give the bearer's own attack rolls disadvantage, and kinds that give attacks against
# the bearer advantage (a prone bearer is the exception: its 5-foot split is in attack_roll_mode).
# A frightened bearer has it while the source of its fear is in sight; the engine counts the source as
# in sight while it stands (see `agent.conditions.end_fear_of_defeated`).
_OWN_ATTACK_DISADVANTAGE = frozenset({"restrained", "prone", "blinded", "poisoned", "frightened"})
_AGAINST_ADVANTAGE = frozenset({"restrained", "blinded", "marked", "paralyzed", "stunned"})
# Kinds that give the bearer disadvantage on ability checks (PHB Appendix A, PDF pp.291, 293).
_CHECK_DISADVANTAGE = frozenset({"frightened", "poisoned"})
_EFFECT_TEXT = {
    "restrained": (
        "restrained with {source}: its attacks have disadvantage and attacks against it have "
        "advantage"
    ),
    "prone": (
        "knocked prone: its attacks have disadvantage, attacks against it from within 5 feet have "
        "advantage and from farther away have disadvantage"
    ),
    "blinded": (
        "blinded by {source}: its attacks have disadvantage and attacks against it have advantage"
    ),
    "marked": "marked by {source}: the next attack roll against it has advantage",
    "paralyzed": (
        "paralyzed by {source}: it cannot act; attacks against it have advantage, and a melee hit "
        "from within 5 feet is a critical hit"
    ),
    "poisoned": "poisoned by {source}: its attacks have disadvantage",
    "frightened": (
        "frightened of {source}: its attacks and ability checks have disadvantage while {source} "
        "stands, and it cannot willingly move closer to {source}"
    ),
    "stunned": (
        "stunned by {source}: it cannot act, attacks against it have advantage, and it fails "
        "Strength and Dexterity saves"
    ),
    "dodging": (
        "dodging (Dodge action): attacks against it have disadvantage and its Dexterity saves "
        "advantage until its next turn"
    ),
}

# The player's own declared intent, read from their words. Nonlethal wins when both appear
# ("I try to knock him out, I don't want to kill him"). Italian and Spanish intent words are read
# too ("ucciderlo", "matarlo", "KO", "sin matarlo"); other languages are not declared (see README).
_NONLETHAL_TEXT = re.compile(
    r"\b(?:knock\w*\s+(?:\w+\s+){0,3}out|subdu\w*|non-?lethal\w*|take\s+(?:\w+\s+){0,3}(?:alive|prisoner)"
    r"|capture\w*|prisoner|prisionero\w*|prigionier\w*|cattur\w*|captur\w*"
    r"|(?:don'?t|do\s+not|won'?t|will\s+not|without)\b[^.!?\n]{0,40}?\bkill\w*"
    r"|\bnot\s+(?:to\s+|trying\s+to\s+)?kill\w*|senza\s+uccid\w*|non\s+voglio\s+uccid\w*"
    r"|sin\s+matar\w*|\bKO\b|inconscient\w*|unconscious|senseless|insensible"
    r"|spare\s+(?:\w+\s+)?life"
    r"|stun\w*)\b",
    re.IGNORECASE,
)
# "kill time" and "kill the lights" are not attacks on a creature.
_LETHAL_TEXT = re.compile(
    r"\b(?:(?:kill|kills|killing)\b(?!\s+(?:time|the\s+(?:lights?|lamp|candles?|music|engine|power"
    r"|mood|conversation|silence)))|slay|slays|execute|executes|finish(?:es|ed|ing)?\s+off"
    r"|finish\s+(?:him|her|them|it)\s+off|cut\s+(?:him|her|them|it)\s+down|butcher"
    r"|\bmatar\w*|\bammazz\w*|\buccid\w*)\b",
    re.IGNORECASE,
)
# Post-combat NPC dispositions written for combatants (session NPC rows, not manual records).
NPC_DISPOSITION = {"captured": "captive", "dead": "dead"}
# EDG-146: a player-released captive is no longer a captive but is still never re-rostered as a
# foe. It is neutral (not hostile), so it is not an enemy either.
RELEASED_NPC_DISPOSITION = "released"
INERT_NPC_DISPOSITIONS = frozenset({*NPC_DISPOSITION.values(), RELEASED_NPC_DISPOSITION})
_ATTACK_VERBS = (
    r"hits?|strikes?|swings?|attacks?|lunges?|stabs?|shoots?|slashes?|bites?|slams?|cuts?|"
    r"thrusts?|fires|lashes?|smashes?|clubs?|jabs?|hurls?|charges?"
)
_OTHER_ATTACKER = re.compile(
    # Only an attack by a second X counts ("the other goblin remains poised" is not an attacker). "the
    # remaining Raider" names the surviving foe and is not listed.
    r"\b(?:other|second|another|additional|third)\s+([a-z][a-z-]*)\b"
    r"[^.!?\n]{0,40}?\s(?:" + _ATTACK_VERBS + r")\b",
    re.IGNORECASE,
)
# Supported contested maneuvers, read from the player's words (EDG-021). Shove-away and other
# maneuvers are not matched here, so they stay unsupported and receive a ruling.
_RESTRAIN_TEXT = re.compile(
    r"\b(?:grapple\w*|pin|pins|pinned|pinning|restrain\w*|tie\s+(?:him|her|it|them)\s+up"
    r"|hold\s+(?:him|her|it|them)\s+(?:down|in\s+place|still|fast))\b",
    re.IGNORECASE,
)
_PRONE_TEXT = re.compile(
    r"\b(?:knock\w*\s+(?:\w+\s+){0,2}(?:prone|down)|trip\w*"
    r"|shov(?:e|es|ed|ing)\b[^.!?\n]{0,40}?\b(?:prone|down|(?:to|on)\s+the\s+(?:ground|floor)"
    r"|off\s+(?:his|her|its|their)\s+feet))\b",
    re.IGNORECASE,
)
_RELEASE_TEXT = re.compile(
    r"\b(?:release\w*|let\s+(?:go\s+of|him\s+go|her\s+go|it\s+go|them\s+go)|unpin\w*|untie\w*"
    r"|unbind\w*)\b",
    re.IGNORECASE,
)
_ROPE_TEXT = re.compile(r"\b(?:rope|cord|chains?|manacles?|bonds?|bindings?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CombatManeuver:
    kind: str  # "restrained" | "prone" | "release"
    source: str | None = None  # "rope" | "hands" for a restraint; "shove" for prone


def classify_combat_maneuver(text: str | None) -> CombatManeuver | None:
    """The supported maneuver the player declared, from their words. None for anything else."""
    words = text or ""
    if _RELEASE_TEXT.search(words):
        return CombatManeuver("release")
    if _RESTRAIN_TEXT.search(words):
        return CombatManeuver("restrained", "rope" if _ROPE_TEXT.search(words) else "hands")
    if _PRONE_TEXT.search(words):
        return CombatManeuver("prone", "shove")
    return None


def display_name(c: Mapping[str, Any]) -> str:
    return str(c.get("display_name") or c.get("name") or "?")


def stable_id(c: Mapping[str, Any]) -> str:
    """Encounter-unique key (roster names are deduplicated when the roster is built)."""
    return str(c.get("name") or "?")


def _hp(c: Mapping[str, Any]) -> int:
    value = c.get("hp_current")
    return int(value) if value is not None else 1


def disposition_of(c: Mapping[str, Any]) -> str:
    stored = c.get("disposition")
    if stored in DISPOSITIONS:
        return str(stored)
    # Legacy row: no disposition was stored, so it is read from `alive` and HP only.
    if c.get("alive", True) is False or _hp(c) <= 0:
        return "defeated"
    return "active"


def is_living(c: Mapping[str, Any]) -> bool:
    return disposition_of(c) in LIVING_DISPOSITIONS


def is_standing(c: Mapping[str, Any]) -> bool:
    """Active and above 0 HP, paralysis aside: still in the fight (targets, victory, its own turn)."""
    return disposition_of(c) == "active" and _hp(c) > 0


def can_act(c: Mapping[str, Any]) -> bool:
    return is_standing(c) and not (has_effect(c, "paralyzed") or has_effect(c, "stunned"))


def declared_lethality(parsed: Any, player_input: str | None) -> Lethality | None:
    """The player's declared intent for an attack: the parser's field, else their own words.

    EDG-147 measurement: the real parser named a lethal kill for "stab through the heart" with no
    declaration in the words. A kill is never inferred, so a parser-only "lethal" is ignored. A
    parser-declared "nonlethal" stands alone (the foe is left alive), and a field that the words
    contradict is kept as before (EDG-022). Without a field, the words decide.
    """
    text = player_input or ""
    words: Lethality | None = None
    if _NONLETHAL_TEXT.search(text):
        words = "nonlethal"
    elif _LETHAL_TEXT.search(text):
        words = "lethal"
    declared = getattr(parsed, "lethality", None)
    if declared == "lethal" and words is None:
        return None
    if declared in ("lethal", "nonlethal"):
        return declared
    return words


def apply_defeat(c: dict, lethality: Lethality | None) -> None:
    """Record a combatant reduced to 0 HP. Unknown intent is `defeated`, never `dead`."""
    c["hp_current"] = 0
    if lethality == "lethal":
        disposition = "dead"
    elif lethality == "nonlethal":
        disposition = "unconscious"
    else:
        disposition = "defeated"
    c["disposition"] = disposition
    c["alive"] = disposition in LIVING_DISPOSITIONS
    # A combatant that cannot act keeps no restraint or prone state: the effect ends with it.
    clear_effects([c])


def effects_of(c: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Typed effects on a combatant. Legacy rows without `effects` read as none."""
    raw = c.get("effects")
    if not isinstance(raw, list):
        return []
    return [dict(e) for e in raw if isinstance(e, Mapping) and e.get("kind") in EFFECT_KINDS]


def has_effect(c: Mapping[str, Any], kind: str) -> bool:
    return any(e["kind"] == kind for e in effects_of(c))


def add_effect(
    c: dict,
    kind: str,
    *,
    source: str | None,
    round_applied: int,
    expires_after_round: int | None = None,
    save: Mapping[str, Any] | None = None,
    ends_with_turn_of: str | None = None,
    source_id: str | None = None,
) -> bool:
    """Apply a typed effect. False when the combatant already has that effect (no stacking)."""
    if kind not in EFFECT_KINDS:
        raise ValueError(f"unknown combat effect: {kind!r}")
    if has_effect(c, kind):
        return False
    entry: dict[str, Any] = {"kind": kind, "source": source, "applied_round": int(round_applied)}
    if expires_after_round is not None:
        entry["expires_after_round"] = int(expires_after_round)
    if save is not None:
        entry["save"] = {"ability": str(save["ability"]), "dc": int(save["dc"])}
    if ends_with_turn_of is not None:
        entry["ends_with_turn_of"] = ends_with_turn_of
    if source_id is not None:
        entry["source_id"] = source_id
    c["effects"] = [*effects_of(c), entry]
    return True


def release_effect(c: dict, kind: str) -> bool:
    """Explicitly end one effect (a declared release). False when it was not in force."""
    before = effects_of(c)
    after = [e for e in before if e["kind"] != kind]
    if len(after) == len(before):
        return False
    c["effects"] = after
    return True


def clear_effects(combatants: Iterable[dict]) -> None:
    """End every typed effect (defeat or end of the encounter). Rows without effects are untouched."""
    for c in combatants:
        if c.get("effects"):
            c["effects"] = []


def attack_roll_mode(
    attacker: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    ranged: bool = False,
    within_five_feet: bool = False,
) -> str:
    """The 5e attack-roll mode from combatant effects: "advantage", "disadvantage" or "normal".

    A restrained, prone or blinded attacker rolls at disadvantage. A restrained, blinded or marked
    target is attacked with advantage; a prone target with advantage from within 5 feet and
    disadvantage from farther away (PHB Appendix A, PDF p.293); a dodging target with disadvantage.
    `within_five_feet` (EDG-044, the melee band) is the typed source of both the PHB rule that a
    ranged attack made within 5 feet of a hostile creature has disadvantage (it applies only to a
    ranged attack) and the prone rule above. Opposing sources cancel to normal, as the rules do.
    Pure: it never rolls and never sets a number.
    """
    advantage = disadvantage = False
    if any(
        e["kind"] in _OWN_ATTACK_DISADVANTAGE
        and not (e["kind"] == "frightened" and e.get("source_defeated"))
        for e in effects_of(attacker)
    ):
        disadvantage = True
    if ranged and within_five_feet:
        disadvantage = True
    for e in effects_of(target):
        if e["kind"] in _AGAINST_ADVANTAGE:
            advantage = True
        elif e["kind"] == "dodging":
            disadvantage = True
        elif e["kind"] == "prone":
            if ranged and not within_five_feet:
                disadvantage = True
            else:
                advantage = True
    if advantage == disadvantage:
        return "normal"
    return "advantage" if advantage else "disadvantage"


def save_roll_mode(combatant: Mapping[str, Any], ability: str) -> str:
    """The 5e saving-throw mode from the bearer's effects: a dodging combatant has advantage on its
    Dexterity saves (PHB, PDF p.193, Dodge). Pure: it never rolls."""
    return "advantage" if ability == "DEX" and has_effect(combatant, "dodging") else "normal"


def impaired_checks(c: Mapping[str, Any]) -> bool:
    """True when the combatant's conditions give its ability checks disadvantage: frightened or
    poisoned (PHB Appendix A). Pure: it never rolls."""
    return any(
        e["kind"] in _CHECK_DISADVANTAGE
        and not (e["kind"] == "frightened" and e.get("source_defeated"))
        for e in effects_of(c)
    )


def save_auto_fails(combatant: Mapping[str, Any], ability: str) -> bool:
    """A stunned or paralyzed combatant automatically fails Strength and Dexterity saves (PHB, PDF
    pp.291, 293)."""
    return ability in ("STR", "DEX") and (
        has_effect(combatant, "stunned") or has_effect(combatant, "paralyzed")
    )


def apply_player_disposition(player: dict, *, subdued: bool) -> None:
    """The player stands while above 0 HP; a subdued defeat leaves them unconscious (alive)."""
    disposition = "unconscious" if subdued or _hp(player) <= 0 else "active"
    player["disposition"] = disposition
    player["alive"] = disposition in LIVING_DISPOSITIONS


def finish_victory(combatants: Iterable[dict]) -> None:
    """Knocked-out foes are living captives once the fight is over."""
    for c in combatants:
        if not c.get("is_player") and disposition_of(c) == "unconscious":
            c["disposition"] = "captured"
            c["alive"] = True


def _public_effect(e: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": e["kind"],
        "source": e.get("source"),
        "applied_round": e.get("applied_round"),
    }
    if e.get("expires_after_round") is not None:  # additive: only timed effects carry it
        out["expires_after_round"] = e["expires_after_round"]
    return out


def view_fields(c: Mapping[str, Any]) -> dict[str, Any]:
    """Public, shape-stable disposition fields for the roster, HUD and narrator.

    `band` and `reaction_used` (EDG-044) are additive: the hero has no band, and a foe's band reads
    as melee when its row predates positioning (see `agent.positioning`).
    """
    disposition = disposition_of(c)
    return {
        "id": stable_id(c),
        "disposition": disposition,
        "can_act": can_act(c),
        "alive": disposition in LIVING_DISPOSITIONS,
        "effects": [_public_effect(e) for e in effects_of(c)],
        "band": None if c.get("is_player") else band_of(c),
        "reaction_used": bool(c.get("reaction_used", False)),
    }


def _effects_text(c: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for effect in effects_of(c):
        source = effect.get("source") or "hands"
        if effect["kind"] == "frightened" and effect.get("source_defeated"):
            parts.append(
                f"; remains frightened of {source}, but its source is down so attack, check and "
                "movement restrictions are inactive while its repeat save remains"
            )
        else:
            parts.append("; " + _EFFECT_TEXT[effect["kind"]].format(source=source))
    return "".join(parts)


def concentration_of(c: Mapping[str, Any]) -> dict[str, Any] | None:
    """The caster's concentration (`{spell, round_applied, expires_after_round}`), or None."""
    raw = c.get("concentration")
    if not isinstance(raw, Mapping) or not raw.get("spell"):
        return None  # a legacy row, or no concentration: none
    return dict(raw)


def _status(c: Mapping[str, Any]) -> str:
    if disposition_of(c) == "active" and _hp(c) > 0 and has_effect(c, "paralyzed"):
        return "active, cannot act (paralyzed)"
    if disposition_of(c) == "active" and _hp(c) > 0 and has_effect(c, "stunned"):
        return "active, incapacitated (stunned)"
    return _STATUS_TEXT[disposition_of(c)]
def _position_text(c: Mapping[str, Any]) -> str:
    """A foe's band, stated for rows that carry one (EDG-044); legacy rows keep their text."""
    return f"; position: {band_text(c)}" if "band" in c else ""


def narrator_roster(combatants: Iterable[Mapping[str, Any]], round_actors: Iterable[str]) -> str:
    """Authoritative roster text for the round narration. The engine decides; prose must agree."""
    combatants = list(combatants)
    enemies = [c for c in combatants if not c.get("is_player")]
    lines = [
        f'- id "{stable_id(c)}", name "{display_name(c)}": {_status(c)}'
        f"{_effects_text(c)}{_position_text(c)}{narrator_note(c)}"
        for c in enemies
    ]
    able = [display_name(c) for c in enemies if can_act(c)]
    actors = list(dict.fromkeys(round_actors))
    hero = next((c for c in combatants if c.get("is_player")), None)
    concentrating = concentration_of(hero) if hero is not None else None
    return (
        "Combat roster (authoritative; the engine decided every outcome):\n"
        + "\n".join(lines)
        + f"\nCombatants able to act now: {', '.join(able) if able else 'none'}."
        + (
            f"\n{display_name(hero)} is concentrating on {concentrating['spell']}."
            if concentrating
            else ""
        )
        + (
            f"\nEnemy attacks this round were made only by: {', '.join(actors)}."
            if actors
            else "\nNo enemy attacked this round."
        )
        + "\nName the attacker with its exact name above. Do not describe a dead, unconscious, "
        "captured or defeated combatant as attacking or acting, and do not invent another attacker "
        "(such as an unlisted 'other', 'second' or 'another' raider)."
    )


def roster_violations(text: str, combatants: Iterable[Mapping[str, Any]]) -> list[str]:
    """Deterministic post-check: prose that revives an inactive foe or invents an attacker."""
    enemies = [c for c in combatants if not c.get("is_player")]
    names = [display_name(c) for c in enemies]
    problems: list[str] = []
    for c in enemies:
        if can_act(c):
            continue
        name = display_name(c)
        # "Raider" must not match the start of "Raider Captain": exclude the longer names' tails.
        tails = [
            re.escape(n[len(name) + 1 :])
            for n in names
            if n != name and n.lower().startswith(name.lower() + " ")
        ]
        guard = f"(?!\\s+(?:{'|'.join(tails)})\\b)" if tails else ""
        pattern = re.compile(
            # The inactive foe must be the subject of the attack verb itself ("The Raider swings",
            # "the Raider rises and strikes"). Narrating the blow that felled it ("the Raider
            # staggers as your blade cuts through") or a second actor in the clause is fine.
            rf"(?<![\w]){re.escape(name)}{guard}(?![\w'’-])(?:\s+\w+)?(?:\s+and)?\s+(?:{_ATTACK_VERBS})\b",
            re.IGNORECASE,
        )
        if pattern.search(text):
            problems.append(name)
    active_nouns = Counter(_noun(display_name(c)) for c in enemies if can_act(c))
    all_nouns = {_noun(display_name(c)) for c in enemies}
    for match in _OTHER_ATTACKER.finditer(text):
        noun = match.group(1).lower()
        if noun in all_nouns and active_nouns[noun] <= 1:
            problems.append(" ".join(match.group(0).split()[:2]))
    return problems


def _noun(name: str) -> str:
    """Base noun of a roster name ('Raider Captain' -> 'captain', 'Raider 2' -> 'raider')."""
    words = [w for w in name.split() if not w.isdigit()]
    return words[-1].lower() if words else ""
