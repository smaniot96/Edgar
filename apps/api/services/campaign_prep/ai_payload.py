"""Keep requested AI content present through repair and publication.

The semantic contract is classified against the actual brief and reviewed again
independently. These checks enforce concrete payload shape, not keyword guesses
about whether a social adventure ought to contain combat.
"""

from .compile import derived
from .store import NeedsAttention, digest

AI_PAYLOAD_POLICY = "requested-ai-payload-1"


def ai_request_evidence(store):
    request = store.state().get("request", {})
    if request.get("source_kind") != "ai":
        return None
    return {
        "policy": AI_PAYLOAD_POLICY,
        "request": request.get("generation"),
        "preferences": request.get("preferences"),
        "premise": store.get("ai-premise"),
        "content_contract": store.get("ai-content-contract"),
    }


def ai_payload_findings(store, spine, bundles):
    evidence = ai_request_evidence(store)
    contract = (evidence or {}).get("content_contract")
    # Existing publications have no new contract. Reading or reopening one must
    # never rewrite it or retroactively reinterpret its reviewed artifacts.
    if contract is None:
        return []
    findings = []

    def finding(records, problem, instruction):
        findings.append(
            {
                "id": "ai_payload_" + digest([records, problem])[:16],
                "severity": "material",
                "record_ids": records,
                "evidence": problem,
                "instruction": instruction,
            }
        )

    encounters = [
        (event, encounter)
        for chapter in bundles["chapters"]
        for event in chapter["events"]
        for encounter in event["encounters"]
    ]
    if contract["combat"] == "requested" and not encounters:
        finding(
            [
                "campaign_spine",
                *[event["id"] for chapter in bundles["chapters"] for event in chapter["events"]],
            ],
            "The accepted request/premise requires meaningful combat choices, but the actual "
            "prepared payload contains zero executable encounter bindings. Contract evidence: "
            + str(contract["evidence"]),
            "Provide at least one reachable, optional combat encounter with an existing or "
            "explicitly added NPC actor, a verified mechanical profile, tactics, morale and a "
            "credible nonviolent outcome. Preserve the request and meaningful choices; prose "
            "about danger alone does not provide a playable combat route.",
        )
    elif contract["combat"] == "excluded" and encounters:
        finding(
            [event["id"] for event, _ in encounters],
            "The explicit user prohibition of combat conflicts with executable encounters.",
            "Honor the original noncombat request with consequential supported alternatives.",
        )
    plans = (store.get("ai-outline") or {}).get("chapters", [])
    for chapter, plan in zip(bundles["chapters"], plans, strict=False):
        planned = len(plan["combat_encounters"])
        actual = sum(bool(event["encounters"]) for event in chapter["events"])
        if actual < planned:
            finding(
                [
                    derived(chapter["chapter_id"], "content"),
                    *[event["id"] for event in chapter["events"]],
                ],
                f"Chapter {chapter['chapter_id']} promised {planned} encounters but retains {actual}.",
                "Restore each planned combat situation as a reachable event with a verified "
                "profile, concrete tactics/morale and a consequential nonviolent alternative. "
                "Keep stable actors and conditional movement; do not duplicate populations.",
            )
    for event, encounter in encounters:
        if not encounter.get("adaptation", "").strip():
            finding(
                [event["id"]],
                "Executable encounter has no authored operating guidance.",
                "Supply concrete tactics, flee/surrender/last-stand morale and a meaningful "
                "nonviolent outcome in this encounter's adaptation guidance.",
            )

    # The chapter author emits these as explicit fields, then conversion retains
    # them as running material. Recheck that repairs have not discarded the finale.
    final_id = spine["chapters"][-1]["id"]
    final = next(b for b in bundles["chapters"] if b["chapter_id"] == final_id)
    section = final.get("source_notes", "").partition("Conditional resolutions:\n")[2]
    outcomes = [
        line.split(" → ", 1)[1].split(". Failure/cost: ", 1)[0].strip().casefold()
        for line in section.splitlines()
        if line.startswith("If ") and " → " in line and ". Failure/cost: " in line
    ]
    if len(set(outcomes)) < 2:
        finding(
            [derived(final_id, "content")],
            "The final chapter lacks two retained explicit conditional resolution options.",
            "Retain at least two distinct supported endings in source_notes under "
            "'Conditional resolutions:', each on its own line as 'If <condition>: <supported "
            "approach> → <outcome>. Failure/cost: <consequence>'. Keep each route achievable "
            "through actual engine operations and independent of one mandatory die result. "
            "Never substitute item custody for unsupported essential magical restoration.",
        )
    return findings


def require_ai_payload(store):
    findings = ai_payload_findings(store, store.get("spine"), store.get("bundles"))
    if findings:
        raise NeedsAttention(
            "Requested AI payload is incomplete: " + "; ".join(f["evidence"] for f in findings)
        )
