"""Qdrant collection names: rules (core books) vs adventure modules.

Rules PDFs should be ingested with --collection rules_<name>, e.g. rules_player_handbook.
Legacy names (player_handbook, etc.) are kept for reference when migrating existing Qdrant data.
"""

# Canonical rules collections (ingest into these names).
RULES_PLAYER_HANDBOOK = "rules_player_handbook"
RULES_DM_GUIDE = "rules_dm_guide"
RULES_MONSTER_MANUAL = "rules_monster_manual"

RULES_COLLECTION_NAMES: tuple[str, ...] = (
    RULES_PLAYER_HANDBOOK,
    RULES_DM_GUIDE,
    RULES_MONSTER_MANUAL,
)

# If you have not re-ingested yet, search these as well (same PDFs, old collection names).
LEGACY_RULES_COLLECTION_ALIASES: tuple[str, ...] = (
    "player_handbook",
    "dm_guide",
    "monster_manual",
)

# Default adventure module when campaign.adventure_collections is empty (solo Chalice).
DEFAULT_ADVENTURE_COLLECTIONS: tuple[str, ...] = ("chalice_of_the_mountain_god",)


def campaign_lore_collection(campaign_id: int) -> str:
    """Qdrant collection name for campaign-specific DM notes (per-campaign RAG)."""
    return f"campaign_lore_{campaign_id}"


def effective_rules_collection_names(include_legacy: bool = True) -> list[str]:
    """Return Qdrant collection names to search for rules RAG."""
    names = list(RULES_COLLECTION_NAMES)
    if include_legacy:
        names.extend(LEGACY_RULES_COLLECTION_ALIASES)
    return names


def effective_adventure_collection_names(adventure_collections: list[str] | None) -> list[str]:
    """Return adventure module collection names; default to Chalice if none configured."""
    if adventure_collections:
        return list(adventure_collections)
    return list(DEFAULT_ADVENTURE_COLLECTIONS)
