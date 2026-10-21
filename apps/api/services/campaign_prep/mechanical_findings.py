"""Local material findings for semantic mechanical joins, before engine assembly."""

from __future__ import annotations

from .store import digest


def mechanical_findings(bundles, mechanics):
    catalogue = {row["name"].casefold() for row in mechanics}
    uses = {}
    findings = []
    for bundle in bundles["chapters"]:
        for event in bundle["events"]:
            for encounter in event["encounters"]:
                actor = encounter["actor_id"]
                uses.setdefault(actor, []).append((event["id"], encounter["creature"]))
                if encounter["creature"].casefold() not in catalogue:
                    findings.append(
                        {
                            "id": "mechanics_" + digest([event["id"], encounter])[:16],
                            "severity": "material",
                            "record_ids": [event["id"], actor],
                            "evidence": "Unverified encounter template: " + encounter["creature"],
                            "instruction": "Choose a coherent verified template and explain the supported substitution; preserve source quantities and narrative roles.",
                        }
                    )
    for actor, rows in uses.items():
        if len({name.casefold() for _, name in rows}) > 1:
            findings.append(
                {
                    "id": "mechanics_" + digest(actor)[:16],
                    "severity": "material",
                    "record_ids": sorted({actor, *(event for event, _ in rows)}),
                    "evidence": "One actor identity is bound to incompatible creature templates: "
                    + repr(rows),
                    "instruction": "Give distinct creatures their own explicit NPC identities and encounter participants. Preserve the canonical NPC, source encounter quantities and actual forms; explain any supported adaptation. New NPC entities may be added only when referenced by repaired events. One engine actor has one verified stat template.",
                }
            )
    return findings
