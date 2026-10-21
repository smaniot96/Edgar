"""Local material findings for semantic mechanical joins, before engine assembly."""

from __future__ import annotations

from edgar_core.manifests import ResolutionPolicy

from .compile import derived
from .mechanics import source_check, source_profile
from .profiles import canonical_profiles, resolved_encounter
from .store import digest


def mechanical_findings(bundles, mechanics):
    declared = {row["id"] for row in bundles.get("mechanical_profiles", [])}
    bundles = canonical_profiles(bundles, mechanics)
    profiles = {row["id"]: row for row in bundles.get("mechanical_profiles", [])}
    checked_profiles = {}
    catalogue = {row["name"].casefold() for row in mechanics}
    uses = {}
    findings = []
    for bundle in bundles["chapters"]:
        for check in bundle.get("checks", []):
            try:
                ResolutionPolicy(
                    policy_id=check["id"],
                    action=check["action"],
                    scene_ids=check["event_ids"],
                    passage_ids=["source_check_evidence"],
                    mode="check",
                    check=source_check(check["check"]),
                    dc=check["dc"],
                )
            except ValueError as exc:
                findings.append(
                    {
                        "id": "mechanics_" + digest([bundle["chapter_id"], check])[:16],
                        "severity": "material",
                        "record_ids": [derived(bundle["chapter_id"], "content")],
                        "evidence": "Source check cannot become an engine policy: " + str(exc),
                        "instruction": "Make this prepared check executable using a supported engine ability, saving throw, skill or tool. Retain coherent action and success/failure guidance; a reasonable DC or procedure adaptation is acceptable when consistent with the campaign. Briefly note a material change. Correct only this check in its chapter content; never claim unsupported mechanics are implemented.",
                    }
                )
        for event in bundle["events"]:
            for appearance in event["encounters"]:
                encounter = resolved_encounter(appearance, profiles)
                actor = encounter["actor_id"]
                profile = encounter.get("source_stat_block", "")
                binding = encounter["creature"]
                if profile.strip():
                    normalized = " ".join(profile.split()).casefold()
                    binding = "source:" + digest(normalized)
                    try:
                        validated = checked_profiles.get(encounter["profile_id"])
                        if validated is None:
                            try:
                                validated = source_profile(
                                    profile,
                                    name=encounter["creature"],
                                    source_evidence={"pages": encounter.get("source_pages", [])},
                                )
                            except ValueError as exc:
                                checked_profiles[encounter["profile_id"]] = exc
                                raise
                            checked_profiles[encounter["profile_id"]] = validated
                        if isinstance(validated, ValueError):
                            raise validated
                        binding = "source:" + digest(" ".join(validated["text"].split()).casefold())
                    except ValueError as exc:
                        findings.append(
                            {
                                "id": "mechanics_" + digest([event["id"], encounter])[:16],
                                "severity": "material",
                                "record_ids": [event["id"], actor]
                                + (
                                    [encounter["profile_id"]]
                                    if encounter["profile_id"] in declared
                                    else []
                                ),
                                "evidence": str(exc),
                                "instruction": "Correct the shared profile's layout/transcription from its page evidence or choose a verified playable profile that fits this prepared campaign. A coherent mechanical adaptation is acceptable; briefly note a material substitution and use it consistently for every appearance of this actor. Never fabricate statistics or verification.",
                            }
                        )
                elif encounter["creature"].casefold() not in catalogue:
                    findings.append(
                        {
                            "id": "mechanics_" + digest([event["id"], encounter])[:16],
                            "severity": "material",
                            "record_ids": [event["id"], actor]
                            + (
                                [encounter["profile_id"]]
                                if encounter["profile_id"] in declared
                                else []
                            ),
                            "evidence": "Unverified encounter template: " + encounter["creature"],
                            "instruction": "Use one complete source-grounded profile or a verified catalogue template that fits this creature's prepared role. A coherent adaptation of creature type or quantity is acceptable when consistent across the campaign; briefly note a material substitution. Never invent catalogue verification or missing statistics.",
                        }
                    )
                uses.setdefault(actor, []).append((event["id"], binding, encounter["profile_id"]))
    for actor, rows in uses.items():
        if len({name.casefold() for _, name, _ in rows}) > 1:
            findings.append(
                {
                    "id": "mechanics_" + digest(actor)[:16],
                    "severity": "material",
                    "record_ids": sorted(
                        {
                            actor,
                            *(event for event, _, _ in rows),
                            *(profile for _, _, profile in rows if profile in declared),
                        }
                    ),
                    "evidence": "One actor identity is bound to incompatible creature profiles: "
                    + repr(rows),
                    "instruction": "First distinguish an inconsistent profile reference from genuinely different creatures or mechanics. When these appearances are the same individual with unchanged statistics, retain its stable actor ID and use one authoritative profile consistently in every encounter, preserving coherent conditional movement. A disguise with unchanged statistics is not another creature or profile. Only genuinely distinct creatures or mechanically different forms need separate engine actor identities, preserving their narrative relationship and transformation conditions. Coherent source adaptations are acceptable; briefly note a material substitution. New NPC entities may be added only when referenced by repaired events. One engine actor has one exact stat profile.",
                }
            )
    return findings
