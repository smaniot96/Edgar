"""Local material findings for semantic mechanical joins, before engine assembly."""

from __future__ import annotations

from .mechanics import source_profile
from .store import digest


def mechanical_findings(bundles, mechanics):
    catalogue = {row["name"].casefold() for row in mechanics}
    uses = {}
    findings = []
    for bundle in bundles["chapters"]:
        for event in bundle["events"]:
            for encounter in event["encounters"]:
                actor = encounter["actor_id"]
                profile = encounter.get("source_stat_block", "").strip()
                binding = "source:" + digest(profile) if profile else encounter["creature"]
                uses.setdefault(actor, []).append((event["id"], binding))
                if profile:
                    try:
                        source_profile(
                            profile,
                            name=encounter["creature"],
                            source_evidence={"pages": encounter.get("source_pages", [])},
                        )
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
    for actor, rows in uses.items():
        if len({name.casefold() for _, name in rows}) > 1:
            findings.append(
                {
                    "id": "mechanics_" + digest(actor)[:16],
                    "severity": "material",
                    "record_ids": sorted({actor, *(event for event, _ in rows)}),
                    "evidence": "One actor identity is bound to incompatible creature profiles: "
                    + repr(rows),
                    "instruction": "Give distinct creatures and mechanically different forms their own explicit engine actor identities while preserving their narrative relationship and transformation conditions. Preserve source encounter quantities. New NPC entities may be added only when referenced by repaired events. One engine actor has one exact stat profile.",
                }
            )
    return findings
