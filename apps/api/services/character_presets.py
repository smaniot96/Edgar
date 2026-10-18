"""Predefined level-1 character class templates for the new-adventure wizard.

The presets are the only supported starting kits. A character's class, ability scores and base
inventory must be exactly one preset's (see `kit_error`), so a class cannot be saved with another
class's kit. `engine_support` states what the engine executes for the class; it is a label, not a
gate, so a preset with no class features is still playable.
"""

CHARACTER_PRESETS: list[dict] = [
    {
        "key": "fighter_l1",
        "label": "Fighter (level 1)",
        "character_class": "Fighter",
        "level": 1,
        "hp_max": 12,
        "hp_current": 12,
        "stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
        "inventory": {"weapons": ["longsword", "shield"], "armor": "chain mail"},
        "engine_support": "Engine: Second Wind; Action Surge from level 2",
    },
    {
        "key": "wizard_l1",
        "label": "Wizard (level 1)",
        "character_class": "Wizard",
        "level": 1,
        "hp_max": 6,
        "hp_current": 6,
        "stats": {"STR": 8, "DEX": 14, "CON": 12, "INT": 17, "WIS": 13, "CHA": 10},
        "inventory": {"weapons": ["quarterstaff", "dagger"], "armor": "none", "spellbook": True},
        "engine_support": "Engine: catalog spells only",
    },
    {
        "key": "rogue_l1",
        "label": "Rogue (level 1)",
        "character_class": "Rogue",
        "level": 1,
        "hp_max": 8,
        "hp_current": 8,
        "stats": {"STR": 10, "DEX": 17, "CON": 12, "INT": 12, "WIS": 11, "CHA": 14},
        "inventory": {"weapons": ["shortsword", "shortbow"], "armor": "leather", "thieves_tools": True},
        "engine_support": "Engine: Sneak Attack",
    },
    {
        "key": "cleric_l1",
        "label": "Cleric (level 1)",
        "character_class": "Cleric",
        "level": 1,
        "hp_max": 8,
        "hp_current": 8,
        "stats": {"STR": 13, "DEX": 10, "CON": 14, "INT": 10, "WIS": 16, "CHA": 12},
        "inventory": {"weapons": ["mace"], "armor": "scale mail", "holy_symbol": True},
        "engine_support": "Engine: no class features yet; kit and HP only",
    },
    {
        "key": "ranger_l1",
        "label": "Ranger (level 1)",
        "character_class": "Ranger",
        "level": 1,
        "hp_max": 10,
        "hp_current": 10,
        "stats": {"STR": 12, "DEX": 16, "CON": 13, "INT": 10, "WIS": 14, "CHA": 10},
        "inventory": {"weapons": ["longbow", "shortsword"], "armor": "leather", "arrows": 20},
        "engine_support": "Engine: no class features yet; kit and HP only",
    },
]


def preset_for_class(character_class: str) -> dict | None:
    """The supported preset for a class name (case and surrounding spaces ignored), or None."""
    wanted = character_class.strip().casefold()
    return next((p for p in CHARACTER_PRESETS if p["character_class"].casefold() == wanted), None)


def kit_error(character_class: str, base_stats: dict, base_inventory: dict) -> str | None:
    """Why a class and its kit are not a supported preset's, or None when they are coherent.

    The messages name the supported classes and never echo the submitted values.
    """
    preset = preset_for_class(character_class)
    if preset is None:
        supported = ", ".join(p["character_class"] for p in CHARACTER_PRESETS)
        return f"Class is not supported. Choose one of: {supported}."
    if base_stats != preset["stats"] or base_inventory != preset["inventory"]:
        return f"Ability scores and inventory must match the {preset['character_class']} starting preset."
    return None
