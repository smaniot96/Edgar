"""Deterministic rules engine: the LLM proposes, this module (plus the dice tool) decides.

Everything that changes game numbers lives here so the adjudicator, the combat subgraph and
the API write path share one implementation:

  * checks — modifier from the character sheet (ability mod + proficiency when proficient),
    DC from a difficulty tier, d20 rolled by `tools.dice`, success = total >= DC;
  * attacks — d20 + attack bonus vs target AC, nat 20 crits (double dice), nat 1 misses;
  * side-effect enforcement — damage only after a failed check (engine-rolled, capped),
    healing only from a consumable actually in inventory or a known rest, 5e conditions only,
    capped/sanitised inventory additions, snake_case flags, validated scene ids;
  * campaign completion — an LLM proposal alone never ends a campaign (see `decide_completion`).

Character dict shape (from `character_play_state`):
  {id, name, class, level, hp_current, hp_max, stats: {"STR": 16, ..., "conditions": [...]},
   inventory: {"weapons": [...], "armor": "...", "items": [...], <tool>: True, ...}}
"""

from __future__ import annotations

import difflib
import logging
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from tools.dice import DiceOutcome, parse_dice, roll, try_roll

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------

ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
_ABILITY_ALIASES = {
    "str": "STR", "strength": "STR",
    "dex": "DEX", "dexterity": "DEX",
    "con": "CON", "constitution": "CON",
    "int": "INT", "intelligence": "INT",
    "wis": "WIS", "wisdom": "WIS",
    "cha": "CHA", "charisma": "CHA",
}  # fmt: skip

SKILL_ABILITY = {
    "athletics": "STR",
    "acrobatics": "DEX", "sleight_of_hand": "DEX", "stealth": "DEX",
    "arcana": "INT", "history": "INT", "investigation": "INT", "nature": "INT", "religion": "INT",
    "animal_handling": "WIS", "insight": "WIS", "medicine": "WIS", "perception": "WIS",
    "survival": "WIS",
    "deception": "CHA", "intimidation": "CHA", "performance": "CHA", "persuasion": "CHA",
}  # fmt: skip
_SKILL_ALIASES = {
    "sleight": "sleight_of_hand", "pickpocket": "sleight_of_hand",
    "animal": "animal_handling", "search": "investigation", "investigate": "investigation",
    "perceive": "perception", "notice": "perception", "spot": "perception", "listen": "perception",
    "track": "survival", "tracking": "survival", "climb": "athletics", "climbing": "athletics",
    "swim": "athletics", "jump": "athletics", "sneak": "stealth", "hide": "stealth",
    "persuade": "persuasion", "lie": "deception", "bluff": "deception", "intimidate": "intimidation",
    "heal": "medicine", "first_aid": "medicine",
}  # fmt: skip
_TOOL_CHECKS = {
    "thieves_tools": "DEX", "lockpicking": "DEX", "lock_picking": "DEX", "pick_lock": "DEX",
    "disarm_trap": "DEX",
}  # fmt: skip

DIFFICULTY_DC = {"easy": 10, "medium": 15, "hard": 20, "very_hard": 25}
DEFAULT_DIFFICULTY = "medium"

# Saving-throw proficiencies per class (PHB).
CLASS_SAVES = {
    "barbarian": ("STR", "CON"), "bard": ("DEX", "CHA"), "cleric": ("WIS", "CHA"),
    "druid": ("INT", "WIS"), "fighter": ("STR", "CON"), "monk": ("STR", "DEX"),
    "paladin": ("WIS", "CHA"), "ranger": ("STR", "DEX"), "rogue": ("DEX", "INT"),
    "sorcerer": ("CON", "CHA"), "warlock": ("WIS", "CHA"), "wizard": ("INT", "WIS"),
    "artificer": ("CON", "INT"),
}  # fmt: skip

# Default skill picks for sheets that do not list proficiencies (the presets don't). Chosen from
# each class's PHB skill list so a level-1 character is proficient where the class shines.
CLASS_SKILLS = {
    "barbarian": ("athletics", "survival"),
    "bard": ("performance", "persuasion", "deception"),
    "cleric": ("insight", "religion"),
    "druid": ("nature", "medicine"),
    "fighter": ("athletics", "perception"),
    "monk": ("acrobatics", "stealth"),
    "paladin": ("athletics", "persuasion"),
    "ranger": ("survival", "perception", "stealth"),
    "rogue": ("stealth", "sleight_of_hand", "acrobatics", "perception"),
    "sorcerer": ("arcana", "persuasion"),
    "warlock": ("arcana", "deception"),
    "wizard": ("arcana", "investigation"),
    "artificer": ("arcana", "investigation"),
}

CASTING_ABILITY = {
    "wizard": "INT", "artificer": "INT", "cleric": "WIS", "druid": "WIS", "ranger": "WIS",
    "bard": "CHA", "sorcerer": "CHA", "warlock": "CHA", "paladin": "CHA",
}  # fmt: skip
# Representative level-1 attack cantrip damage per caster class.
_SPELL_DAMAGE = {"wizard": "1d10", "sorcerer": "1d10", "warlock": "1d10", "artificer": "1d10"}
_DEFAULT_SPELL_DAMAGE = "1d8"

HIT_DIE = {
    "barbarian": 12, "fighter": 10, "paladin": 10, "ranger": 10, "bard": 8, "cleric": 8,
    "druid": 8, "monk": 8, "rogue": 8, "warlock": 8, "artificer": 8, "sorcerer": 6, "wizard": 6,
}  # fmt: skip


@dataclass(frozen=True)
class Weapon:
    name: str
    dice: str
    finesse: bool = False
    ranged: bool = False


_WEAPONS = [
    Weapon("dagger", "1d4", finesse=True), Weapon("club", "1d4"), Weapon("quarterstaff", "1d6"),
    Weapon("mace", "1d6"), Weapon("spear", "1d6"), Weapon("handaxe", "1d6"),
    Weapon("javelin", "1d6"), Weapon("light hammer", "1d4"), Weapon("sickle", "1d4"),
    Weapon("shortsword", "1d6", finesse=True), Weapon("scimitar", "1d6", finesse=True),
    Weapon("rapier", "1d8", finesse=True), Weapon("whip", "1d4", finesse=True),
    Weapon("longsword", "1d8"), Weapon("battleaxe", "1d8"), Weapon("warhammer", "1d8"),
    Weapon("morningstar", "1d8"), Weapon("flail", "1d8"), Weapon("trident", "1d6"),
    Weapon("war pick", "1d8"), Weapon("greatsword", "2d6"), Weapon("greataxe", "1d12"),
    Weapon("maul", "2d6"), Weapon("glaive", "1d10"), Weapon("halberd", "1d10"),
    Weapon("pike", "1d10"), Weapon("lance", "1d12"),
    Weapon("shortbow", "1d6", ranged=True), Weapon("longbow", "1d8", ranged=True),
    Weapon("hand crossbow", "1d6", ranged=True), Weapon("heavy crossbow", "1d10", ranged=True),
    Weapon("light crossbow", "1d8", ranged=True), Weapon("crossbow", "1d8", ranged=True),
    Weapon("sling", "1d4", ranged=True), Weapon("dart", "1d4", finesse=True, ranged=True),
]  # fmt: skip
# Longest names first so "light crossbow" wins over "crossbow".
_WEAPONS_BY_LEN = sorted(_WEAPONS, key=lambda w: len(w.name), reverse=True)
_DEFAULT_WEAPON = Weapon("weapon", "1d8")

CONDITIONS_5E = frozenset(
    {
        "blinded", "charmed", "deafened", "exhaustion", "frightened", "grappled",
        "incapacitated", "invisible", "paralyzed", "petrified", "poisoned", "prone",
        "restrained", "stunned", "unconscious",
    }
)  # fmt: skip
_CONDITION_ALIASES = {
    "blind": "blinded", "charm": "charmed", "deaf": "deafened", "exhausted": "exhaustion",
    "fear": "frightened", "afraid": "frightened", "scared": "frightened", "grapple": "grappled",
    "paralysed": "paralyzed", "paralysis": "paralyzed", "poison": "poisoned", "knocked_prone": "prone",
    "restrain": "restrained", "stun": "stunned", "knocked_out": "unconscious",
}  # fmt: skip

# (keyword tokens that must all be present, healing dice) — most specific first.
_HEALING_ITEMS: list[tuple[frozenset[str], str]] = [
    (frozenset({"supreme", "healing"}), "10d4+20"),
    (frozenset({"superior", "healing"}), "8d4+8"),
    (frozenset({"greater", "healing"}), "4d4+4"),
    (frozenset({"potion", "healing"}), "2d4+2"),
    (frozenset({"healing", "draught"}), "2d4+2"),
    (frozenset({"healing", "elixir"}), "2d4+2"),
    (frozenset({"goodberry"}), "1d1"),
]

COMPLETION_FLAG = "campaign_complete"
PENDING_COMPLETION_FLAG = "campaign_completion_proposed"
RESERVED_FLAGS = frozenset({COMPLETION_FLAG, PENDING_COMPLETION_FLAG})
_TRUTHY = {"true", "1", "yes", "complete", "completed", "victory"}

MAX_FLAGS_PER_TURN = 6
MAX_FLAG_VALUE_LEN = 200
MAX_ITEMS_ADDED_PER_TURN = 3
MAX_ITEM_NAME_LEN = 60
MAX_SCENE_ID_LEN = 64

_FLAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCENE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SNAKE_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# --------------------------------------------------------------------------------------------
# Character sheet helpers
# --------------------------------------------------------------------------------------------


def ability_mod(score: Any) -> int:
    try:
        return (int(score) - 10) // 2
    except (TypeError, ValueError):
        return 0


def proficiency_bonus(level: Any) -> int:
    try:
        lvl = max(1, int(level))
    except (TypeError, ValueError):
        lvl = 1
    return 2 + (lvl - 1) // 4


def character_class(character: dict | None) -> str:
    return str((character or {}).get("class") or "").strip().lower()


def ability_scores(character: dict | None) -> dict[str, int]:
    """Normalise stats to {"STR": 16, ...}; accepts "STR", "str" or "strength" keys."""
    stats = (character or {}).get("stats") or {}
    out: dict[str, int] = {}
    if isinstance(stats, dict):
        for key, value in stats.items():
            ab = _ABILITY_ALIASES.get(str(key).strip().lower())
            if ab and ab not in out:
                try:
                    out[ab] = int(value)
                except (TypeError, ValueError):
                    continue
    for ab in ABILITIES:
        out.setdefault(ab, 10)
    return out


def ability_modifier(character: dict | None, ability: str) -> int:
    return ability_mod(ability_scores(character).get(ability, 10))


def _norm_token(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")


def _listed_proficiencies(character: dict | None) -> set[str]:
    stats = (character or {}).get("stats") or {}
    out: set[str] = set()
    if isinstance(stats, dict):
        for key in ("proficiencies", "skills", "skill_proficiencies", "tool_proficiencies"):
            val = stats.get(key)
            if isinstance(val, list):
                out |= {_norm_token(v) for v in val}
            elif isinstance(val, dict):
                out |= {_norm_token(k) for k, v in val.items() if v}
    return out


def skill_proficiencies(character: dict | None) -> set[str]:
    listed = {s for s in _listed_proficiencies(character) if s in SKILL_ABILITY}
    if listed:
        return listed
    return set(CLASS_SKILLS.get(character_class(character), ()))


def save_proficiencies(character: dict | None) -> set[str]:
    stats = (character or {}).get("stats") or {}
    listed = stats.get("saving_throws") if isinstance(stats, dict) else None
    if isinstance(listed, list) and listed:
        return {a for a in (_ABILITY_ALIASES.get(_norm_token(v)) for v in listed) if a}
    return set(CLASS_SAVES.get(character_class(character), ()))


def inventory_items(character_or_inventory: dict | None) -> list[str]:
    """Flatten an inventory dict into display strings (lists, string values, True flags)."""
    inv = character_or_inventory or {}
    if "inventory" in inv and isinstance(inv.get("inventory"), (dict, type(None))):
        inv = inv.get("inventory") or {}
    out: list[str] = []
    if not isinstance(inv, dict):
        return out
    for key, value in inv.items():
        if isinstance(value, list):
            for v in value:
                if isinstance(v, dict):
                    name = v.get("name")
                    if name:
                        out.append(str(name))
                elif v is not None and str(v).strip():
                    out.append(str(v))
        elif value is True:
            out.append(str(key))
    return out


_STOPWORDS = {"of", "the", "a", "an", "my", "some", "this", "that", "your", "his", "her"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in _STOPWORDS}


def match_inventory_item(items: Iterable[str], name: str | None) -> str | None:
    """Return the inventory entry `name` refers to (exact, then token-subset match)."""
    if not name:
        return None
    items = list(items)
    lowered = name.strip().lower()
    for it in items:
        if it.strip().lower() == lowered:
            return it
    want = _tokens(name)
    if not want:
        return None
    for it in items:
        have = _tokens(it)
        if have and (want <= have or have <= want):
            return it
    # Singular/plural tolerance ("potions" vs "potion").
    want_s = {t.rstrip("s") for t in want}
    for it in items:
        have_s = {t.rstrip("s") for t in _tokens(it)}
        if have_s and (want_s <= have_s or have_s <= want_s):
            return it
    return None


def has_tool(character: dict | None, tool: str) -> bool:
    return match_inventory_item(inventory_items(character), tool.replace("_", " ")) is not None


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------


@dataclass
class CheckSpec:
    check: str  # normalised name, e.g. "athletics", "dex_save", "attack"
    kind: str  # "skill" | "ability" | "save" | "tool" | "attack" | "spell_attack" | "flat"
    ability: str | None
    modifier: int
    proficient: bool
    weapon: Weapon | None = None


@dataclass
class CheckResult:
    check: str
    kind: str
    ability: str | None
    modifier: int
    dc: int
    against: str  # "DC" | "AC"
    roll_expression: str
    natural: int
    total: int
    success: bool
    critical: bool = False
    fumble: bool = False
    dice_log: list[dict] = field(default_factory=list)
    weapon: Weapon | None = None

    def describe(self) -> str:
        verdict = "success" if self.success else "failure"
        if self.critical:
            verdict = "critical hit" if self.kind in ("attack", "spell_attack") else verdict
        elif self.fumble and self.kind in ("attack", "spell_attack"):
            verdict = "automatic miss (natural 1)"
        label = self.check.replace("_", " ")
        return (
            f"{label}: {self.roll_expression} = {self.total} (d20 rolled {self.natural}) "
            f"vs {self.against} {self.dc} -> {verdict}"
        )

    def apply_to(self, adj: AdjudicationResult) -> None:
        """Stamp the engine outcome over whatever the LLM returned."""
        adj.success = self.success
        adj.dice_result = self.total
        adj.check = self.check
        adj.dc = self.dc
        adj.against = self.against
        adj.modifier = self.modifier
        adj.roll_expression = self.roll_expression
        adj.natural_roll = self.natural
        adj.dice_log = [*adj.dice_log, *self.dice_log]


def normalise_check(raw: str | None) -> tuple[str, str, str | None] | None:
    """Map an LLM-proposed check name to (name, kind, ability). None when no check."""
    if not raw:
        return None
    token = _norm_token(raw)
    for suffix in ("_check", "_roll", "_test"):
        if token.endswith(suffix) and token != suffix.strip("_"):
            token = token[: -len(suffix)]
    if not token or token in {"none", "null", "no", "no_check"}:
        return None

    if token in {"spell_attack", "spell", "cantrip", "spell_attack_roll", "ranged_spell_attack",
                 "melee_spell_attack"}:  # fmt: skip
        return "spell_attack", "spell_attack", None
    if token in {"attack", "melee_attack", "ranged_attack", "weapon_attack", "melee", "ranged",
                 "attack_roll", "strike"}:  # fmt: skip
        return "attack", "attack", None

    if "save" in token or "saving" in token:
        for part in token.split("_"):
            ab = _ABILITY_ALIASES.get(part)
            if ab:
                return f"{ab.lower()}_save", "save", ab
        return "save", "flat", None

    if token in SKILL_ABILITY:
        return token, "skill", SKILL_ABILITY[token]
    if token in _SKILL_ALIASES:
        skill = _SKILL_ALIASES[token]
        return skill, "skill", SKILL_ABILITY[skill]
    if token in _TOOL_CHECKS:
        return "thieves_tools", "tool", _TOOL_CHECKS[token]
    if token in _ABILITY_ALIASES:
        ab = _ABILITY_ALIASES[token]
        return ab.lower(), "ability", ab
    if token == "initiative":
        return "initiative", "ability", "DEX"
    # Compound names like "strength_athletics" / "wisdom_perception".
    for part in token.split("_"):
        if part in SKILL_ABILITY:
            return part, "skill", SKILL_ABILITY[part]
    for part in token.split("_"):
        if part in _ABILITY_ALIASES:
            ab = _ABILITY_ALIASES[part]
            return ab.lower(), "ability", ab
    log.info("unknown_check_name", extra={"check": raw})
    return token[:40], "flat", None


def choose_weapon(character: dict | None, hint: str = "", *, prefer_ranged: bool = False) -> Weapon:
    inv = (character or {}).get("inventory") or {}
    weapons = inv.get("weapons") if isinstance(inv, dict) else None
    names = [str(w) for w in weapons] if isinstance(weapons, list) else ([str(weapons)] if weapons else [])
    known: list[Weapon] = []
    for n in names:
        nl = n.lower()
        for w in _WEAPONS_BY_LEN:
            if w.name in nl:
                known.append(w)
                break
    hint_l = hint.lower()
    for w in known:
        if w.name in hint_l:
            return w
    if prefer_ranged:
        for w in known:
            if w.ranged:
                return w
    for w in known:
        if not w.ranged:
            return w
    return known[0] if known else _DEFAULT_WEAPON


def _attack_ability(character: dict | None, weapon: Weapon) -> str:
    scores = ability_scores(character)
    if weapon.ranged and not weapon.finesse:
        return "DEX"
    if weapon.finesse:
        return "DEX" if scores["DEX"] >= scores["STR"] else "STR"
    return "STR"


def check_spec(character: dict | None, check: str | None, *, hint: str = "") -> CheckSpec | None:
    norm = normalise_check(check)
    if norm is None:
        return None
    name, kind, ability = norm
    prof = proficiency_bonus((character or {}).get("level", 1))
    cls = character_class(character)

    if kind == "attack":
        weapon = choose_weapon(character, hint, prefer_ranged="ranged" in _norm_token(check or ""))
        ability = _attack_ability(character, weapon)
        return CheckSpec(
            name, kind, ability, ability_modifier(character, ability) + prof, True, weapon=weapon
        )
    if kind == "spell_attack":
        ability = CASTING_ABILITY.get(cls)
        if ability is None:  # non-caster: treat as a weapon attack
            return check_spec(character, "attack", hint=hint)
        return CheckSpec(name, kind, ability, ability_modifier(character, ability) + prof, True)
    if kind == "flat" or ability is None:
        return CheckSpec(name, "flat", None, 0, False)

    mod = ability_modifier(character, ability)
    proficient = False
    if kind == "skill":
        proficient = name in skill_proficiencies(character)
    elif kind == "save":
        proficient = ability in save_proficiencies(character)
    elif kind == "tool":
        proficient = cls == "rogue" or "thieves_tools" in _listed_proficiencies(character) or has_tool(
            character, "thieves tools"
        )
    return CheckSpec(name, kind, ability, mod + (prof if proficient else 0), proficient)


def d20_expression(modifier: int) -> str:
    return f"1d20{modifier:+d}"


def dc_for(difficulty: str | None) -> int:
    return DIFFICULTY_DC.get(str(difficulty or DEFAULT_DIFFICULTY), DIFFICULTY_DC[DEFAULT_DIFFICULTY])


def resolve_check(
    character: dict | None,
    check: str | None,
    difficulty: str | None = None,
    *,
    dc: int | None = None,
    against: str = "DC",
    hint: str = "",
    rng: random.Random | None = None,
) -> CheckResult | None:
    """Roll `check` for `character` against a DC (or AC). None when no check is needed."""
    spec = check_spec(character, check, hint=hint)
    if spec is None:
        return None
    target = dc if dc is not None else dc_for(difficulty)
    expr = d20_expression(spec.modifier)
    outcome = roll(expr, rng)
    natural = outcome.rolls[0]
    is_attack = spec.kind in ("attack", "spell_attack")
    critical = is_attack and natural == 20
    fumble = is_attack and natural == 1
    if critical:
        success = True
    elif fumble:
        success = False
    else:
        success = outcome.total >= target
    return CheckResult(
        check=spec.check,
        kind=spec.kind,
        ability=spec.ability,
        modifier=spec.modifier,
        dc=target,
        against=against,
        roll_expression=expr,
        natural=natural,
        total=outcome.total,
        success=success,
        critical=critical,
        fumble=fumble,
        dice_log=[outcome.to_log(f"{spec.check} check")],
        weapon=spec.weapon,
    )


def resolve_attack(
    character: dict | None,
    target_ac: int,
    *,
    spell: bool = False,
    hint: str = "",
    rng: random.Random | None = None,
) -> CheckResult:
    ranged = bool(re.search(r"\b(shoot|shoots|fire|fires|loose|looses)\b", hint, re.IGNORECASE))
    result = resolve_check(
        character,
        "spell_attack" if spell else ("ranged_attack" if ranged else "attack"),
        dc=int(target_ac),
        against="AC",
        hint=hint,
        rng=rng,
    )
    assert result is not None
    return result


def _double_dice(expr: str) -> str:
    p = parse_dice(expr)
    mod = f"{p.modifier:+d}" if p.modifier else ""
    return f"{min(p.count * 2, 100)}d{p.sides}{mod}"


def roll_attack_damage(
    character: dict | None,
    attack: CheckResult,
    *,
    hint: str = "",
    rng: random.Random | None = None,
) -> tuple[int, DiceOutcome]:
    """Weapon (or cantrip) damage for a hit; dice doubled on a critical."""
    cls = character_class(character)
    if attack.kind == "spell_attack":
        expr = _SPELL_DAMAGE.get(cls, _DEFAULT_SPELL_DAMAGE)
    else:
        weapon = attack.weapon or choose_weapon(character, hint)
        mod = ability_modifier(character, _attack_ability(character, weapon))
        expr = f"{weapon.dice}{mod:+d}" if mod else weapon.dice
    if attack.critical:
        expr = _double_dice(expr)
    outcome = roll(expr, rng)
    return max(1, outcome.total), outcome


def initiative_bonus(character: dict | None) -> int:
    return ability_modifier(character, "DEX")


# --------------------------------------------------------------------------------------------
# Parsed input -> check (with legacy dice_expression tolerance)
# --------------------------------------------------------------------------------------------


def resolve_from_parsed(
    character: dict | None,
    parsed: Any,
    *,
    player_input: str = "",
    rng: random.Random | None = None,
) -> tuple[CheckResult | None, list[dict]]:
    """Resolve the check the parser proposed. Returns (check_result, extra_dice_log).

    Legacy `dice_expression` handling: when no `check` is given but the old prompt returned a
    d20 expression, we still roll a check — using the entity "skill" if present, otherwise a flat
    d20 — but never the LLM's modifier. Non-d20 legacy expressions are rolled for the log only;
    invalid ones are dropped.
    """
    if parsed is None:
        return None, []
    check = getattr(parsed, "check", None)
    difficulty = getattr(parsed, "difficulty", None)
    hint = player_input
    if check:
        return resolve_check(character, check, difficulty, hint=hint, rng=rng), []

    legacy = getattr(parsed, "dice_expression", None)
    if not legacy:
        return None, []
    outcome = try_roll(legacy, rng)
    if outcome is None:
        log.info("dropped_invalid_dice_expression", extra={"expression": legacy})
        return None, []
    if outcome.sides == 20 and outcome.count == 1:
        entities = getattr(parsed, "entities", None) or {}
        skill = entities.get("skill") if isinstance(entities, dict) else None
        return resolve_check(character, skill or "flat", difficulty, hint=hint, rng=rng), []
    return None, [outcome.to_log("legacy dice_expression")]


# --------------------------------------------------------------------------------------------
# Healing
# --------------------------------------------------------------------------------------------

_USE_VERBS = re.compile(
    r"\b(drink|drinks|drank|quaff|quaffs|gulp|gulps|swig|sip|down|downs|use|uses|consume|"
    r"consumes|imbibe|eat|eats|apply|applies|pour)\b",
    re.IGNORECASE,
)
_ITEM_MENTION = re.compile(r"\b((?:\w+\s+){0,3}(?:potion|elixir|draught|goodberry)(?:\s+of(?:\s+\w+){1,2})?)", re.IGNORECASE)


def healing_dice_for(item: str | None) -> str | None:
    if not item:
        return None
    toks = _tokens(item)
    for needed, dice in _HEALING_ITEMS:
        if needed <= toks:
            return dice
    return None


@dataclass
class HealResult:
    amount: int
    source: str
    item: str | None = None
    dice: DiceOutcome | None = None


def find_healing_consumable(
    character: dict | None,
    *,
    parsed: Any = None,
    player_input: str = "",
    proposed_removals: Iterable[str] = (),
) -> str | None:
    """Return the inventory entry of a healing consumable the player is using this turn."""
    items = inventory_items(character)
    if not items:
        return None
    candidates: list[str] = []
    uses_item = getattr(parsed, "uses_item", None)
    if uses_item:
        candidates.append(uses_item)
    if _USE_VERBS.search(player_input or ""):
        candidates += [m.group(1) for m in _ITEM_MENTION.finditer(player_input or "")]
    # An LLM-proposed removal of a healing item only counts if the player actually used it.
    if uses_item or _USE_VERBS.search(player_input or ""):
        candidates += list(proposed_removals)
    for cand in candidates:
        found = match_inventory_item(items, cand)
        if found and healing_dice_for(found):
            return found
        # "I drink a potion" -> the only healing potion in the pack.
        if not found and "potion" in _tokens(cand):
            healing = [it for it in items if healing_dice_for(it)]
            if healing:
                return healing[0]
    return None


def mentions_item_use(parsed: Any, player_input: str) -> bool:
    """The player declares using/consuming an item (whether or not they actually carry it)."""
    if getattr(parsed, "uses_item", None):
        return True
    return bool(_USE_VERBS.search(player_input or "") and _ITEM_MENTION.search(player_input or ""))


def heal_from_consumable(
    character: dict | None, item: str, rng: random.Random | None = None
) -> HealResult | None:
    dice = healing_dice_for(item)
    if not dice:
        return None
    outcome = roll(dice, rng) if dice != "1d1" else None
    amount = outcome.total if outcome else 1
    return HealResult(amount=max(0, amount), source="consumable", item=item, dice=outcome)


_LONG_REST = re.compile(
    r"\b(long rest|sleep (?:for )?(?:the |a )?night|sleep for (?:8|eight) hours|camp for the night|"
    r"rest (?:for )?(?:the )?night|rest until (?:dawn|morning))\b",
    re.IGNORECASE,
)
_SHORT_REST = re.compile(r"\b(short rest|rest for (?:an|1|one) hour|catch (?:my|our) breath for an hour)\b", re.IGNORECASE)


def detect_rest(text: str | None) -> str | None:
    if not text:
        return None
    if _LONG_REST.search(text):
        return "long"
    if _SHORT_REST.search(text):
        return "short"
    return None


def heal_from_rest(
    character: dict | None, kind: str, rng: random.Random | None = None
) -> HealResult | None:
    if not character:
        return None
    hp_cur = int(character.get("hp_current") or 0)
    hp_max = int(character.get("hp_max") or hp_cur)
    if kind == "long":
        return HealResult(amount=max(0, hp_max - hp_cur), source="long_rest")
    if kind == "short":
        die = HIT_DIE.get(character_class(character), 8)
        con = ability_modifier(character, "CON")
        expr = f"1d{die}{con:+d}" if con else f"1d{die}"
        outcome = roll(expr, rng)
        return HealResult(amount=max(1, outcome.total), source="short_rest", dice=outcome)
    return None


# --------------------------------------------------------------------------------------------
# Validation helpers (shared with apps.api.services.world_writes)
# --------------------------------------------------------------------------------------------


def normalise_condition(name: Any) -> str | None:
    token = _norm_token(name)
    if token.startswith("exhaustion"):
        return "exhaustion"
    token = _CONDITION_ALIASES.get(token, token)
    return token if token in CONDITIONS_5E else None


def normalise_conditions(names: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for n in names or []:
        c = normalise_condition(n)
        if c is None:
            log.info("dropped_unknown_condition", extra={"condition": str(n)[:60]})
            continue
        if c not in out:
            out.append(c)
    return out


# Text that never belongs in an item name: markup, code, prompt-injection phrasing, or absurd
# power claims ("+5", "of wishes", "infinite").
_ITEM_JUNK = re.compile(
    r"[<>{}\[\]`|\\]|https?://|\b(ignore|instruction|system|prompt|assistant|developer|admin|"
    r"infinite|unlimited|godlike|invincib\w*|immortal\w*|of wishes|wish|9999|999)\b|\+\s*[4-9]\b|"
    r"\+\s*\d{2,}",
    re.IGNORECASE,
)
_RARE_ITEM = re.compile(
    r"\b(legendary|artifact|vorpal|holy avenger|deck of many things)\b|\+\s*[1-3]\b", re.IGNORECASE
)


def sanitize_item_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    text = re.sub(r"\s+", " ", name).strip().strip(".,;:!\"'")
    if not text or len(text) > MAX_ITEM_NAME_LEN:
        return None
    if any(ord(ch) < 32 for ch in name.replace("\n", "")) or "\n" in name:
        return None
    if _ITEM_JUNK.search(text):
        return None
    return text


def sanitize_inventory_add(
    items: Iterable[Any],
    *,
    context_text: str | None = None,
    limit: int = MAX_ITEMS_ADDED_PER_TURN,
) -> list[str]:
    """Cap and clean LLM-proposed item grants.

    Rare/magic-sounding items (legendary, artifact, +1..+3) are only accepted when the adventure
    context this turn actually mentions them; obvious injections are always dropped.
    """
    ctx = (context_text or "").lower()
    out: list[str] = []
    for raw in items or []:
        clean = sanitize_item_name(raw)
        if clean is None:
            log.info("dropped_inventory_item", extra={"item": str(raw)[:80]})
            continue
        if _RARE_ITEM.search(clean) and (context_text is None or clean.lower() not in ctx):
            log.info("dropped_rare_inventory_item", extra={"item": clean})
            continue
        if clean.lower() in (o.lower() for o in out):
            continue
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def sanitize_flag_key(key: Any) -> str | None:
    token = _norm_token(key)
    return token if _FLAG_KEY_RE.fullmatch(token or "") else None


def sanitize_flags(
    flags: Iterable[FlagUpdate], *, allow_reserved: bool = False, limit: int = MAX_FLAGS_PER_TURN
) -> list[FlagUpdate]:
    out: list[FlagUpdate] = []
    seen: set[str] = set()
    for fu in flags or []:
        key = sanitize_flag_key(getattr(fu, "key", None))
        if key is None or key in seen:
            log.info("dropped_flag", extra={"key": str(getattr(fu, "key", ""))[:80]})
            continue
        if key in RESERVED_FLAGS and not allow_reserved:
            continue
        value = str(getattr(fu, "value", "") or "").strip()[:MAX_FLAG_VALUE_LEN]
        out.append(FlagUpdate(key=key, value=value))
        seen.add(key)
        if len(out) >= limit:
            break
    return out


def validate_flags_cleared(
    keys: Iterable[Any], existing: Iterable[str], *, allow_reserved: bool = False
) -> list[str]:
    existing_set = set(existing or [])
    out: list[str] = []
    for k in keys or []:
        key = str(k).strip()
        if key not in existing_set:
            log.info("dropped_flag_clear_unknown_key", extra={"key": key[:80]})
            continue
        if key in RESERVED_FLAGS and not allow_reserved:
            continue
        if key not in out:
            out.append(key)
    return out


def known_scene_ids(current_scene_id: str | None, adventure_context: Iterable[dict] | None) -> set[str]:
    known: set[str] = set()
    if current_scene_id:
        known.add(current_scene_id)
    for chunk in adventure_context or []:
        for key in ("scene_id", "scene"):
            v = chunk.get(key) if isinstance(chunk, dict) else None
            if isinstance(v, str) and _SCENE_ID_RE.fullmatch(v):
                known.add(v)
        text = chunk.get("text", "") if isinstance(chunk, dict) else ""
        known |= {t for t in _SNAKE_TOKEN_RE.findall(str(text)) if len(t) <= MAX_SCENE_ID_LEN}
    return known


def validate_scene_id(proposed: Any, known: Iterable[str] | None = None) -> str | None:
    """Accept only snake_case ids (<= 64 chars); snap near-misses onto a known id.

    Junk (sentences, markup, over-long strings) is logged and dropped (None).
    """
    if proposed is None:
        return None
    raw = str(proposed).strip()
    if not raw:
        return None
    if len(raw) > MAX_SCENE_ID_LEN or re.search(r"[^A-Za-z0-9_\- ]", raw) or len(raw.split()) > 6:
        log.info("dropped_invalid_scene_id", extra={"scene_id": raw[:80]})
        return None
    candidate = re.sub(r"[\s\-]+", "_", raw.lower()).strip("_")
    if not _SCENE_ID_RE.fullmatch(candidate):
        log.info("dropped_invalid_scene_id", extra={"scene_id": raw[:80]})
        return None
    known_list = sorted(set(known or []))
    if candidate in known_list:
        return candidate
    close = difflib.get_close_matches(candidate, known_list, n=1, cutoff=0.85)
    if close:
        return close[0]
    if known_list:
        log.info("unknown_scene_id_accepted", extra={"scene_id": candidate})
    return candidate


# --------------------------------------------------------------------------------------------
# Side-effect enforcement
# --------------------------------------------------------------------------------------------


def _damage_cap_expression(expr: str | None, level: int) -> str | None:
    """Clamp an LLM damage proposal to a sane hazard size for the character's level."""
    if not expr:
        return None
    try:
        p = parse_dice(expr)
    except ValueError:
        return None
    max_count = max(1, min(4, 1 + level // 2))
    count = min(p.count, max_count)
    sides = min(p.sides, 12)
    mod = max(-5, min(p.modifier, 5))
    return f"{count}d{sides}{mod:+d}" if mod else f"{count}d{sides}"


def enforce_side_effects(
    adj: AdjudicationResult,
    *,
    character: dict | None,
    check_result: CheckResult | None,
    world_flags: dict | None = None,
    current_scene_id: str | None = None,
    adventure_context: list[dict] | None = None,
    player_input: str = "",
    parsed: Any = None,
    in_combat: bool = False,
    rng: random.Random | None = None,
) -> AdjudicationResult:
    """Keep only the LLM-proposed side effects the engine can justify. Mutates and returns adj."""
    adj.conditions = normalise_conditions(adj.conditions)
    context_text = " ".join(str(c.get("text", "")) for c in (adventure_context or []))
    items = inventory_items(character)

    cu = adj.character_update
    if cu is not None:
        cu.add_conditions = normalise_conditions(cu.add_conditions)
        cu.remove_conditions = normalise_conditions(cu.remove_conditions)
        cu.inventory_add = sanitize_inventory_add(cu.inventory_add, context_text=context_text)
        removals: list[str] = []
        for name in cu.inventory_remove:
            found = match_inventory_item(items, name)
            if found and found not in removals:
                removals.append(found)
        proposed_removals = list(cu.inventory_remove)
        cu.inventory_remove = removals
    else:
        proposed_removals = []

    if in_combat:
        # Combat owns HP entirely (engine-rolled damage, Second Wind, consumables).
        if cu is not None:
            cu.hp_delta = None
            cu.damage_dice = None
    else:
        hp_delta = 0
        damage_taken = 0
        proposed_damage = cu is not None and (cu.damage_dice or (cu.hp_delta or 0) < 0)
        if proposed_damage and check_result is not None and not check_result.success:
            level = int((character or {}).get("level") or 1)
            expr = _damage_cap_expression(cu.damage_dice, level) or "1d6"
            outcome = roll(expr, rng)
            damage_taken = max(0, outcome.total)
            if not cu.damage_dice and cu.hp_delta is not None:
                # A bare number from the LLM is only an upper bound on the engine roll.
                damage_taken = min(damage_taken, abs(cu.hp_delta))
            adj.dice_log = [*adj.dice_log, outcome.to_log("damage taken")]
            cu.damage_dice = expr
        elif proposed_damage:
            log.info("dropped_unbacked_damage", extra={"hp_delta": cu.hp_delta, "dice": cu.damage_dice})
            cu.damage_dice = None
        hp_delta -= damage_taken

        heal: HealResult | None = None
        item = find_healing_consumable(
            character, parsed=parsed, player_input=player_input, proposed_removals=proposed_removals
        )
        if item:
            heal = heal_from_consumable(character, item, rng)
        else:
            rest = detect_rest(player_input)
            if rest:
                heal = heal_from_rest(character, rest, rng)
        if heal is not None and heal.amount > 0:
            hp_delta += heal.amount
            if heal.dice is not None:
                adj.dice_log = [*adj.dice_log, heal.dice.to_log(f"healing ({heal.source})")]
        if heal is not None and heal.item:
            if cu is None:
                cu = CharacterUpdate()
                adj.character_update = cu
            if heal.item not in cu.inventory_remove:
                cu.inventory_remove.append(heal.item)
        if cu is None and hp_delta:
            cu = CharacterUpdate()
            adj.character_update = cu
        if cu is not None:
            if cu.hp_delta and cu.hp_delta > 0 and heal is None:
                log.info("dropped_unbacked_healing", extra={"hp_delta": cu.hp_delta})
            cu.hp_delta = hp_delta or None
        adj.damage = damage_taken or None

    adj.flags_set = sanitize_flags(adj.flags_set, allow_reserved=True)
    adj.flags_cleared = validate_flags_cleared(
        adj.flags_cleared, (world_flags or {}).keys(), allow_reserved=True
    )
    adj.scene_id = validate_scene_id(
        adj.scene_id, known_scene_ids(current_scene_id, adventure_context)
    )
    if adj.scene_id == current_scene_id:
        adj.scene_id = None
    return adj


# --------------------------------------------------------------------------------------------
# Campaign completion
# --------------------------------------------------------------------------------------------

_CONFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|aye|i confirm|confirm(ed)?|i do|let it end|end it)\b"
    r"|\b(end|conclude|finish|close)\s+(the|this|my|our)\s+(adventure|campaign|story|tale)\b",
    re.IGNORECASE,
)


def completion_proposed(adj: AdjudicationResult | None) -> bool:
    if adj is None:
        return False
    return any(
        fu.key == COMPLETION_FLAG and str(fu.value).strip().lower() in _TRUTHY for fu in adj.flags_set
    )


def player_confirms_ending(parsed: Any, player_input: str) -> bool:
    if getattr(parsed, "confirms_ending", False):
        return True
    return bool(_CONFIRM_RE.search(player_input or ""))


_PENDING_NOTE = (
    "[The adventure appears to have reached its conclusion. Ask the player, in character, "
    "whether they want to end the campaign here; it only ends if they confirm.]"
)


def decide_completion(
    adj: AdjudicationResult,
    *,
    world_flags: dict | None,
    parsed: Any = None,
    player_input: str = "",
    combat_active: bool = False,
    boss_victory: bool = False,
) -> str:
    """Engine decision on an LLM completion proposal. Mutates adj flags; returns the status.

    Rules (one LLM sentence can never end a campaign):
      * combat still running            -> any proposal is rejected (no pending either);
      * boss defeated this turn + proposal -> accepted;
      * boss defeated, no proposal      -> pending (player is asked to confirm);
      * pending from an EARLIER turn + explicit player confirmation this turn -> accepted;
      * new proposal otherwise          -> pending (`campaign_completion_proposed` flag);
      * pending, no confirmation, no repeat proposal -> pending cleared (story moved on).
    """
    proposed = completion_proposed(adj)
    adj.flags_set = [fu for fu in adj.flags_set if fu.key not in RESERVED_FLAGS]
    adj.flags_cleared = [k for k in adj.flags_cleared if k not in RESERVED_FLAGS]
    flags = world_flags or {}
    pending = str(flags.get(PENDING_COMPLETION_FLAG, "")).strip().lower() in _TRUTHY

    def _set_pending() -> None:
        if not pending:
            adj.flags_set.append(FlagUpdate(key=PENDING_COMPLETION_FLAG, value="true"))
        if _PENDING_NOTE not in adj.mechanical_summary:
            adj.mechanical_summary = f"{adj.mechanical_summary} {_PENDING_NOTE}".strip()

    def _accept() -> None:
        adj.flags_set.append(FlagUpdate(key=COMPLETION_FLAG, value="true"))
        if PENDING_COMPLETION_FLAG in flags:
            adj.flags_cleared.append(PENDING_COMPLETION_FLAG)

    if combat_active:
        status = "rejected" if proposed else "none"
    elif boss_victory and proposed:
        _accept()
        status = "accepted"
    elif pending and player_confirms_ending(parsed, player_input):
        _accept()
        status = "accepted"
    elif proposed or boss_victory:
        _set_pending()
        status = "pending"
    elif pending:
        adj.flags_cleared.append(PENDING_COMPLETION_FLAG)
        status = "none"
    else:
        status = "none"
    adj.completion_status = status
    return status
