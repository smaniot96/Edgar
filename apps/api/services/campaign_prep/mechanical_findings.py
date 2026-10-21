"""Local material findings for semantic mechanical joins, before engine assembly."""

from __future__ import annotations

from edgar_core.manifests import ResolutionPolicy

from .compile import derived
from .mechanics import source_check, source_profile
from .store import digest


def mechanical_findings(bundles, mechanics):
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
                        "instruction": "Preserve this source check's action, DC, success/failure guidance and provenance. Use an existing engine ability, saving throw, skill or tool check that represents the original procedure; do not replace its mechanics with an unrelated skill. Correct only this check in its chapter content.",
                    }
                )
        for event in bundle["events"]:
            for encounter in event["encounters"]:
                actor = encounter["actor_id"]
                profile = encounter.get("source_stat_block", "")
                binding = encounter["creature"]
                if profile.strip():
                    normalized = " ".join(profile.split()).casefold()
                    binding = "source:" + digest(normalized)
                    try:
                        validated = source_profile(
                            profile,
                            name=encounter["creature"],
                            source_evidence={"pages": encounter.get("source_pages", [])},
                        )
                        binding = "source:" + digest(" ".join(validated["text"].split()).casefold())
                    except ValueError as exc:
                        findings.append(
                            {
                                "id": "mechanics_" + digest([event["id"], encounter])[:16],
                                "severity": "material",
                                "record_ids": [event["id"], actor],
                                "evidence": str(exc),
                                "instruction": "Restore the complete source stat block from its page evidence. Correct layout/transcription errors; never replace a source-specific profile with a stronger or weaker catalogue creature merely to satisfy parsing.",
                            }
                        )
                elif encounter["creature"].casefold() not in catalogue:
                    findings.append(
                        {
                            "id": "mechanics_" + digest([event["id"], encounter])[:16],
                            "severity": "material",
                            "record_ids": [event["id"], actor],
                            "evidence": "Unverified encounter template: " + encounter["creature"],
                            "instruction": "If this creature has a source-specific profile, retain that complete stat block with its source pages. Otherwise choose its exact verified catalogue template; preserve source quantities and narrative roles. Do not silently substitute a different creature.",
                        }
                    )
                uses.setdefault(actor, []).append((event["id"], binding))
    for actor, rows in uses.items():
        if len({name.casefold() for _, name in rows}) > 1:
            findings.append(
                {
                    "id": "mechanics_" + digest(actor)[:16],
                    "severity": "material",
                    "record_ids": sorted({actor, *(event for event, _ in rows)}),
                    "evidence": "One actor identity is bound to incompatible creature profiles: "
                    + repr(rows),
                    "instruction": "First distinguish an inconsistent profile reference from genuinely different creatures or mechanics. When these appearances are the same individual with unchanged statistics, retain its stable actor ID and use one authoritative exact profile consistently in every encounter, preserving source provenance and conditional movement. A disguise with unchanged statistics is not another creature or profile. Only genuinely distinct creatures or mechanically different forms need separate engine actor identities, preserving their narrative relationship and transformation conditions. Preserve source encounter quantities. New NPC entities may be added only when referenced by repaired events. One engine actor has one exact stat profile.",
                }
            )
    return findings
