"""One shared mechanical definition per profile; encounters contain references.

Legacy inline data is a read-only compatibility input. Canonicalization creates a
derived projection and never edits retained provider responses or the caller's data.
"""

from copy import deepcopy

from edgar_core.campaign_semantics import MechanicalProfile

from .store import NeedsAttention, digest


def profile_key(profile):
    text = profile.get("source_stat_block", "")
    return (
        ("source", " ".join(text.split()).casefold())
        if text.strip()
        else ("catalogue", profile["creature"].strip().casefold())
    )


def canonical_profiles(bundles, mechanics=()):
    """Fold transport declarations and unambiguous legacy profiles into one registry."""
    result = deepcopy(bundles)
    profiles, by_key, aliases = {}, {}, {}

    def declare(raw):
        profile = MechanicalProfile.model_validate(raw).model_dump()
        identity, key = profile["id"], profile_key(profile)
        if identity in aliases and profile_key(profiles[aliases[identity]]) != key:
            raise NeedsAttention("Mechanical profile ID has conflicting definitions: " + identity)
        canonical = by_key.setdefault(key, identity)
        aliases[identity] = canonical
        if canonical in profiles:
            profiles[canonical]["source_pages"] = sorted(
                set(profiles[canonical]["source_pages"] + profile["source_pages"])
            )
        else:
            profiles[canonical] = {**profile, "id": canonical}
        return canonical

    for profile in result.get("mechanical_profiles", []):
        declare(profile)
    for bundle in result["chapters"]:
        for profile in bundle.get("mechanical_profiles", []):
            declare(profile)
    for bundle in result["chapters"]:
        for event in bundle["events"]:
            for encounter in event["encounters"]:
                if encounter.get("profile_id"):
                    continue
                raw = {
                    field: encounter.get(field, default)
                    for field, default in (
                        ("creature", ""),
                        ("source_stat_block", ""),
                        ("source_pages", []),
                    )
                }
                raw["id"] = "profile_" + digest(profile_key(raw))[:48]
                encounter["profile_id"] = declare(raw)
    # A source/catalogue representation is equivalent only when the complete
    # verified catalogue text matches, including traits the parser cannot execute.
    catalogue = {row["name"].casefold(): row for row in mechanics}
    source_by_text = {
        profile_key(profile)[1]: identity
        for identity, profile in profiles.items()
        if profile.get("source_stat_block", "").strip()
    }
    for identity, profile in list(profiles.items()):
        if profile.get("source_stat_block", "").strip():
            continue
        verified = catalogue.get(profile["creature"].casefold(), {})
        match = source_by_text.get(" ".join(verified.get("text", "").split()).casefold())
        if match:
            aliases[identity] = match
            del profiles[identity]

    def resolve(identity):
        seen = set()
        while identity in aliases and aliases[identity] != identity:
            if identity in seen:
                raise NeedsAttention("Cyclic mechanical profile aliases.")
            seen.add(identity)
            identity = aliases[identity]
        if identity not in profiles:
            raise NeedsAttention("Unknown shared mechanical profile: " + str(identity))
        return identity

    for bundle in result["chapters"]:
        if bundle.get("mechanical_profiles"):
            bundle["mechanical_profiles"] = []
        for event in bundle["events"]:
            for encounter in event["encounters"]:
                identity = resolve(encounter["profile_id"])
                if encounter.get("source_stat_block") and profile_key(encounter) != profile_key(
                    profiles[identity]
                ):
                    raise NeedsAttention("Inline legacy profile contradicts its shared reference.")
                encounter["profile_id"] = identity
                for field in ("creature", "source_stat_block", "source_pages"):
                    encounter.pop(field, None)
    if profiles or "mechanical_profiles" in result:
        result["mechanical_profiles"] = list(profiles.values())
    return result


def resolved_encounter(encounter, profiles):
    """Compiler/legacy projection only; provider authoring never emits these fields."""
    profile = profiles.get(encounter.get("profile_id"))
    if not profile:
        raise NeedsAttention(
            "Unknown shared mechanical profile: " + str(encounter.get("profile_id"))
        )
    return {**profile, **encounter}
