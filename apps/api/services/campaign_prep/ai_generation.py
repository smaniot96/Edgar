"""Sequential AI adventure generation: premise -> cast -> outline -> one chapter at a time.

Every author node makes one structured call, and its paired check node validates the
result at once. A failed check records its problems and routes back to the author, which
revises with that exact feedback, a bounded number of times. Identifiers that later
steps must reference are offered as enums or assigned locally, so a plan cannot point at
a chapter that does not exist. Provenance is assigned locally as authored; the model
never sees PDF fields. Accepted output is composed into the shared Spine and Bundles, so
continuity, assembly, review and publication stay unchanged.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import Field

from agent.resolution import normalise_check
from edgar_core.campaign_semantics import Bundles, Record, Spine
from edgar_core.identifiers import Identifier

from .compile import validate_semantics
from .profiles import canonical_profiles
from .store import NeedsAttention, digest
from .structural_repair import reference_defects, structural_defects

# Marker stored in the frozen request. Requests without it keep the single-spine path,
# so retained runs replay exactly as they were dispatched.
AI_WORKFLOW = "sequential-ai-1"
# Author attempts per step and per retry round; Retry grants another round.
ATTEMPTS = 3
RESERVED = {"campaign_spine", "campaign_registry"}
ARCHETYPES = (
    "hook",
    "hub",
    "investigation",
    "journey",
    "site",
    "stronghold",
    "climax",
    "aftermath",
)
FINAL_ARCHETYPES = {"climax", "stronghold", "aftermath"}


@dataclass(frozen=True)
class SizeProfile:
    """Structural targets derived from published modules and the DMG's adventure shape."""

    chapters: tuple[int, int]
    events_per_chapter: tuple[int, int]
    goals_per_chapter: tuple[int, int]
    campaign_secrets: tuple[int, int]
    shared_cast: tuple[int, int]
    combat_encounters: tuple[int, int]
    arc: str


SIZE_PROFILES = {
    "small": SizeProfile(
        (1, 2),
        (2, 4),
        (1, 3),
        (1, 2),
        (5, 9),
        (0, 2),
        "One compact situation, or a hook chapter followed by its resolution.",
    ),
    "medium": SizeProfile(
        (3, 4),
        (4, 6),
        (2, 4),
        (2, 3),
        (10, 14),
        (3, 6),
        "Hook with early action; a hub or investigation offering at least one optional lead; "
        "the antagonist's site or stronghold; a climax whose outcome depends on choices.",
    ),
    "large": SizeProfile(
        (5, 7),
        (5, 8),
        (2, 5),
        (3, 4),
        (16, 24),
        (5, 10),
        "Hook; a hub with several optional regional sites; the antagonist reacts to progress "
        "between chapters; a stronghold; a climax and aftermath.",
    ),
    "gigantic": SizeProfile(
        (8, 12),
        (5, 9),
        (3, 6),
        (4, 6),
        (24, 36),
        (8, 16),
        "Several acts, each with its own hub or site, a mid-campaign turn that changes the "
        "heroes' goal, and secrets revealed across acts toward a decisive climax.",
    ),
}


def size_profile(request):
    size = ((request or {}).get("generation") or {}).get("size") or "small"
    if size not in SIZE_PROFILES:
        raise NeedsAttention("Unsupported adventure size: " + str(size))
    return size, SIZE_PROFILES[size]


# --- Model-facing schemas (no provenance fields) -------------------------------------


class AiAntagonist(Record):
    name: str
    nature: str
    motive: str
    methods: str
    weakness: str = Field(description="A flaw, need or leverage the heroes can discover and use.")


class AiSchemeStep(Record):
    when: str = Field(description="Relative timing, e.g. 'before the adventure' or 'chapter 2'.")
    action: str
    consequence: str = Field(description="What changes in the world if nobody interferes.")


class AiPremise(Record):
    title: str
    premise: str
    setting: str
    tone: str
    hook: str = Field(description="Player-safe opening situation.")
    central_question: str
    antagonist: AiAntagonist
    scheme: list[AiSchemeStep] = Field(min_length=2)
    stakes: str
    ending: str = Field(description="Outcomes that depend on player choices; never one script.")
    level_recommendation: str
    dm_guidance: str


class AiEntity(Record):
    id: Identifier
    kind: Literal["npc", "place", "faction", "clue"]
    name: str
    aliases: list[str]
    description: str = Field(description="Player-safe description on first sight or meeting.")
    motive: str
    knowledge: str = Field(description="What this entity privately knows.")
    role: str = Field(
        description="Narrative function: antagonist, patron, rival, informant, hub, lair, key evidence…"
    )


class AiSecret(Record):
    id: Identifier
    truth: str
    known_by: list[Identifier]
    reveal_condition: str = Field(
        description="How the heroes can earn it; allow credible approaches."
    )


class AiCast(Record):
    entities: list[AiEntity] = Field(min_length=2)
    secrets: list[AiSecret] = Field(min_length=1)


class AiCombatPlan(Record):
    opponents: str = Field(description="Who or what fights, as the chapter author will cast it.")
    stakes: str = Field(description="Why this fight matters to the story.")
    alternatives: str = Field(
        description="How the heroes might avoid, end or win it without slaughter."
    )


def outline_schema(cast):
    """Chapter plans may only name cast identities that already exist."""
    ids = tuple(e["id"] for e in cast["entities"])
    places = tuple(e["id"] for e in cast["entities"] if e["kind"] == "place")
    secrets = tuple(s["id"] for s in cast["secrets"])
    EntityRef = Literal[ids]  # noqa: N806 - local type alias
    PlaceRef = Literal[places]  # noqa: N806
    SecretRef = Literal[secrets]  # noqa: N806

    class AiChapterPlan(Record):
        slug: Identifier = Field(description="Short readable name; the chapter ID is derived.")
        title: str
        archetype: Literal[ARCHETYPES]
        brief: str
        dramatic_purpose: str
        primary_location_id: PlaceRef
        entity_ids: list[EntityRef]
        sets_up_secret_ids: list[SecretRef]
        reveals_secret_ids: list[SecretRef]
        antagonist_pressure: str = Field(
            description="What the antagonist does during this chapter."
        )
        leads_onward: list[str] = Field(
            description="Independent leads toward the next chapter; empty only for the final one."
        )
        combat_encounters: list[AiCombatPlan]
        chronology: str

    class AiOutline(Record):
        chapters: list[AiChapterPlan] = Field(min_length=1)
        dm_guidance: str

    return AiOutline


class AiReveal(Record):
    entity_id: Identifier
    public_text: str
    condition: str = Field(description="Private discovery guidance; allow any credible approach.")


class AiEncounter(Record):
    actor_id: Identifier = Field(description="NPC entity fighting here; also a participant.")
    creature: str = Field(description="Exact name from verified_creatures.")
    quantity: int = Field(ge=1)
    tactics: str


class AiEvent(Record):
    id: Identifier
    title: str
    location_id: Identifier
    participants: list[Identifier]
    goal_ids: list[Identifier]
    secret_ids: list[Identifier]
    clue_ids: list[Identifier]
    visible_entity_ids: list[Identifier]
    reveals: list[AiReveal]
    trigger: str
    timing: str
    public_description: str = Field(description="Player-safe sensory situation; no hidden truths.")
    dm_notes: str
    next_event_ids: list[Identifier]
    encounters: list[AiEncounter]


class AiGoal(Record):
    id: Identifier
    title: str
    objective: str
    completion: str = Field(description="Outcome recognizable through any credible solution.")
    required: bool
    event_ids: list[Identifier] = Field(min_length=1)
    depends_on: list[Identifier]


class AiCheck(Record):
    id: Identifier
    action: str
    event_ids: list[Identifier] = Field(min_length=1)
    check: str = Field(description="Skill, ability or tool identifier, e.g. insight or athletics.")
    dc: int = Field(ge=5, le=30)
    success_text: str
    failure_text: str


class AiItem(Record):
    entity_id: Identifier = Field(description="A clue entity representing the object.")
    procedure: str
    event_ids: list[Identifier] = Field(min_length=1)
    portable: bool
    consumable: bool


class AiChapterDraft(Record):
    new_entities: list[AiEntity]
    new_secrets: list[AiSecret]
    goals: list[AiGoal] = Field(min_length=1)
    events: list[AiEvent] = Field(min_length=1)
    checks: list[AiCheck]
    items: list[AiItem]
    dm_guidance: str
    running_notes: str
    continuity_summary: str = Field(
        description="DM-facing: what this chapter establishes, possible outcomes and open threads."
    )


# --- Local conversion ---------------------------------------------------------------


def authored(note=""):
    return {"kind": "authored", "pages": [], "regions": [], "note": note, "uncertain": False}


def to_entity(row):
    knowledge = row["knowledge"]
    if row.get("role"):
        knowledge = (knowledge + "\n\n" if knowledge else "") + "Narrative role: " + row["role"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "aliases": row["aliases"],
        "description": row["description"],
        "motive": row["motive"],
        "knowledge": knowledge,
        "origin": authored(),
    }


def to_secret(row, setup, reveal):
    return {
        "id": row["id"],
        "truth": row["truth"],
        "known_by": row["known_by"],
        "setup_chapters": setup,
        "reveal_chapters": reveal,
        "reveal_condition": row["reveal_condition"],
        "origin": authored(),
    }


def chapter_ids(outline):
    """Chapter IDs are local: position plus the model's slug, always unique and valid."""
    result = []
    for index, plan in enumerate(outline["chapters"], 1):
        prefix = "ch" + str(index) + "_"
        result.append(prefix + plan["slug"][: 64 - len(prefix)])
    return result


def profile_id(creature):
    slug = re.sub(r"[^a-z0-9]+", "_", creature.casefold()).strip("_") or "creature"
    value = "profile_" + slug
    return value if len(value) <= 64 else "profile_" + digest(creature)[:48]


def creature_index(mechanics):
    rows = []
    for row in mechanics or []:
        match = re.search(r"Challenge\s+(\d+(?:/\d+)?)", row.get("text", ""))
        rows.append({"name": row["name"], "challenge": match.group(1) if match else None})
    return rows


# --- Checks ---------------------------------------------------------------------------


def check_premise(value):
    problems = []
    for field in ("title", "premise", "setting", "hook", "central_question", "ending"):
        if not value[field].strip():
            problems.append("Field is empty: " + field)
    if not value["antagonist"]["name"].strip():
        problems.append("The antagonist needs a name.")
    return value, problems


def check_cast(value, premise, profile):
    problems = []
    entities, secrets = value["entities"], value["secrets"]
    ids = [e["id"] for e in entities] + [s["id"] for s in secrets]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append("IDs must be unique across entities and secrets: " + ", ".join(duplicates))
    if RESERVED & set(ids):
        problems.append("Reserved IDs may not be used: " + ", ".join(sorted(RESERVED & set(ids))))
    low, high = profile.shared_cast
    if not low <= len(entities) <= high:
        problems.append(f"Create {low}-{high} shared entities; {len(entities)} were supplied.")
    low, high = profile.campaign_secrets
    if not low <= len(secrets) <= high:
        problems.append(f"Create {low}-{high} campaign secrets; {len(secrets)} were supplied.")
    seen = {}
    for row in entities:
        for name in {normalized_name(n) for n in [row["name"], *row["aliases"]]}:
            if name in seen and seen[name] != row["id"]:
                problems.append(
                    f"Entities {seen[name]} and {row['id']} share the name or alias '{name}'."
                )
            seen.setdefault(name, row["id"])
    kinds = {e["kind"] for e in entities}
    for kind in ("npc", "place"):
        if kind not in kinds:
            problems.append("The cast needs at least one " + kind + " entity.")
    holders = {e["id"] for e in entities if e["kind"] in ("npc", "faction")}
    for secret in secrets:
        unknown = sorted(set(secret["known_by"]) - holders)
        if unknown:
            problems.append(
                f"Secret {secret['id']} known_by must name NPC or faction entities: "
                + ", ".join(unknown)
            )
    villain = premise["antagonist"]["name"].casefold()
    named = [
        n.casefold()
        for e in entities
        if e["kind"] in ("npc", "faction")
        for n in [e["name"], *e["aliases"]]
    ]
    if villain and not any(villain in n or n in villain for n in named if n):
        problems.append(
            "The antagonist '" + premise["antagonist"]["name"] + "' must be an NPC or faction."
        )
    return value, problems


def check_outline(value, cast, profile):
    problems = []
    plans = value["chapters"]
    low, high = profile.chapters
    if not low <= len(plans) <= high:
        problems.append(f"Plan {low}-{high} chapters; {len(plans)} were supplied.")
    slugs = [p["slug"] for p in plans]
    if len(set(slugs)) != len(slugs):
        problems.append("Chapter slugs must be unique.")
    for index, plan in enumerate(plans):
        final = index == len(plans) - 1
        if not final and len([lead for lead in plan["leads_onward"] if lead.strip()]) < 2:
            problems.append(
                f"Chapter {index + 1} ({plan['slug']}) needs at least two independent leads onward."
            )
        if final and len(plans) >= 3 and plan["archetype"] not in FINAL_ARCHETYPES:
            problems.append(
                "The final chapter must be a climax, stronghold or aftermath; got "
                + plan["archetype"]
                + "."
            )
    planned = sum(len(p["combat_encounters"]) for p in plans)
    low, high = profile.combat_encounters
    if not low <= planned <= high:
        problems.append(f"Plan {low}-{high} combat encounters in total; {planned} were planned.")
    for secret in cast["secrets"]:
        setups = [i for i, p in enumerate(plans) if secret["id"] in p["sets_up_secret_ids"]]
        reveals = [i for i, p in enumerate(plans) if secret["id"] in p["reveals_secret_ids"]]
        if not reveals:
            problems.append(f"Secret {secret['id']} is never revealed in any chapter.")
        if not setups:
            problems.append(f"Secret {secret['id']} is never set up before its reveal.")
        elif reveals and min(setups) > min(reveals):
            problems.append(f"Secret {secret['id']} is revealed before it is set up.")
    used = {i for p in plans for i in p["entity_ids"] + [p["primary_location_id"]]}
    unused = sorted(e["id"] for e in cast["entities"] if e["id"] not in used)
    if unused:
        problems.append("Every shared entity must appear in a chapter: " + ", ".join(unused))
    return value, problems


def normalized_name(name):
    return " ".join(name.casefold().split())


def entity_names(entities):
    """Normalized name/alias -> entity ID, as continuity joins identities."""
    result = {}
    for row in entities:
        for name in [row["name"], *row["aliases"]]:
            result.setdefault(normalized_name(name), row["id"])
    return result


def prior_identities(committed):
    """Identities introduced by already accepted chapters, keyed by ID."""
    rows = {}
    for bundle in committed:
        for field, kind in (
            ("new_entities", None),
            ("new_secrets", "secret"),
            ("goals", "goal"),
            ("events", "event"),
            ("checks", "check"),
        ):
            for row in bundle[field]:
                rows[row["id"]] = row["kind"] if kind is None else kind
    return rows


def normalize_chapter(draft, known_kinds, creatures):
    """Deterministic, intent-preserving joins; anything ambiguous is left for the check."""
    draft = {k: v for k, v in draft.items()}
    kinds = {**known_kinds, **{e["id"]: e["kind"] for e in draft["new_entities"]}}
    by_name = {c["name"].casefold(): c["name"] for c in creatures}
    goals = {g["id"]: g for g in draft["goals"]}
    events = []
    for raw in draft["events"]:
        event = {
            **raw,
            "participants": list(raw["participants"]),
            "clue_ids": list(raw["clue_ids"]),
        }

        def include(identity, event=event):
            kind = kinds.get(identity)
            target = "clue_ids" if kind == "clue" else "participants" if kind else None
            if target and identity not in event["participants"] + event["clue_ids"]:
                event[target].append(identity)

        for identity in raw["visible_entity_ids"]:
            if kinds.get(identity) != "place":
                include(identity)
        for reveal in raw["reveals"]:
            include(reveal["entity_id"])
        encounters = []
        for encounter in raw["encounters"]:
            if kinds.get(encounter["actor_id"]) == "npc":
                include(encounter["actor_id"])
            name = by_name.get(encounter["creature"].strip().casefold(), encounter["creature"])
            encounters.append({**encounter, "creature": name})
        event["encounters"] = encounters
        event["goal_ids"] = sorted(
            set(raw["goal_ids"])
            | {gid for gid, goal in goals.items() if raw["id"] in goal["event_ids"]}
        )
        events.append(event)
    draft["events"] = events
    event_goals = {e["id"]: set(e["goal_ids"]) for e in events}
    draft["goals"] = [
        {
            **goal,
            "event_ids": sorted(
                set(goal["event_ids"])
                | {eid for eid, gids in event_goals.items() if goal["id"] in gids}
            ),
        }
        for goal in draft["goals"]
    ]
    draft["checks"] = [
        {**check, "check": (normalise_check(check["check"]) or (check["check"],))[0]}
        for check in draft["checks"]
    ]
    return draft


def chapter_bundle(chapter_id, draft):
    """Convert a normalized draft into the shared Bundle contract."""
    profiles, declared = [], set()
    events = []
    for event in draft["events"]:
        encounters = []
        for encounter in event["encounters"]:
            pid = profile_id(encounter["creature"])
            if pid not in declared:
                declared.add(pid)
                profiles.append(
                    {
                        "id": pid,
                        "creature": encounter["creature"],
                        "source_stat_block": "",
                        "source_pages": [],
                    }
                )
            encounters.append(
                {
                    "actor_id": encounter["actor_id"],
                    "profile_id": pid,
                    "quantity": encounter["quantity"],
                    "adaptation": encounter["tactics"],
                }
            )
        events.append(
            {
                **{k: v for k, v in event.items() if k != "encounters"},
                "encounters": encounters,
                "origin": authored(),
            }
        )
    return {
        "chapter_id": chapter_id,
        "mechanical_profiles": profiles,
        "goals": [{**goal, "origin": authored()} for goal in draft["goals"]],
        "events": events,
        "new_entities": [to_entity(e) for e in draft["new_entities"]],
        "new_secrets": [to_secret(s, [chapter_id], [chapter_id]) for s in draft["new_secrets"]],
        "dm_guidance": draft["dm_guidance"],
        "source_pages": [],
        "source_notes": draft["running_notes"],
        "items": [{**item, "origin": authored()} for item in draft["items"]],
        "checks": [
            {
                "id": c["id"],
                "action": c["action"],
                "event_ids": c["event_ids"],
                "check": c["check"],
                "dc": c["dc"],
                "success_text": c["success_text"],
                "failure_text": c["failure_text"],
                "origin": authored(),
            }
            for c in draft["checks"]
        ],
    }


def check_chapter(
    draft,
    *,
    plan,
    chapter_id,
    profile,
    known_kinds,
    creatures,
    known_names=None,
    actor_creatures=None,
):
    """Normalize, then list every problem so one revision can address all of them."""
    draft = normalize_chapter(draft, known_kinds, creatures)
    problems = []
    local = {
        **{e["id"]: e["kind"] for e in draft["new_entities"]},
        **{s["id"]: "secret" for s in draft["new_secrets"]},
        **{g["id"]: "goal" for g in draft["goals"]},
        **{e["id"]: "event" for e in draft["events"]},
        **{c["id"]: "check" for c in draft["checks"]},
    }
    rows = (
        [e["id"] for e in draft["new_entities"]]
        + [s["id"] for s in draft["new_secrets"]]
        + [g["id"] for g in draft["goals"]]
        + [e["id"] for e in draft["events"]]
        + [c["id"] for c in draft["checks"]]
    )
    duplicates = sorted({i for i in rows if rows.count(i) > 1})
    if duplicates:
        problems.append("IDs are declared twice in this chapter: " + ", ".join(duplicates))
    clashes = sorted(set(local) & (set(known_kinds) | RESERVED | {chapter_id}))
    if clashes:
        problems.append(
            "New IDs already exist; reference the existing record or choose new IDs: "
            + ", ".join(clashes)
        )
    names = dict(known_names or {})
    for row in draft["new_entities"]:
        for name in {normalized_name(n) for n in [row["name"], *row["aliases"]]}:
            if name in names and names[name] != row["id"]:
                problems.append(
                    f"New entity {row['id']} reuses the name or alias '{name}' of "
                    f"{names[name]}; reference {names[name]} or choose a distinct name."
                )
            names.setdefault(name, row["id"])
    kinds = {**known_kinds, **local}
    entity_kinds = {"npc", "place", "faction", "clue"}
    event_ids = {e["id"] for e in draft["events"]}
    goal_ids = {g["id"] for g in draft["goals"]}
    secret_ids = {k for k, v in kinds.items() if v == "secret"}
    low, high = profile.events_per_chapter
    if not low <= len(draft["events"]) <= high + 2:
        problems.append(f"Write {low}-{high} events; {len(draft['events'])} were supplied.")
    low, high = profile.goals_per_chapter
    if not low <= len(draft["goals"]) <= high + 2:
        problems.append(f"Write {low}-{high} goals; {len(draft['goals'])} were supplied.")
    creature_names = {c["name"] for c in creatures}
    bindings = dict(actor_creatures or {})
    for event in draft["events"]:
        eid = event["id"]
        if kinds.get(event["location_id"]) != "place":
            problems.append(
                f"Event {eid}: location_id {event['location_id']} is not a place entity."
            )
        for field in ("participants", "visible_entity_ids"):
            bad = sorted(i for i in event[field] if kinds.get(i) not in entity_kinds)
            if bad:
                problems.append(f"Event {eid}: unknown {field}: " + ", ".join(bad))
        bad = sorted(i for i in event["clue_ids"] if kinds.get(i) != "clue")
        if bad:
            problems.append(f"Event {eid}: clue_ids must be clue entities: " + ", ".join(bad))
        bad = sorted(set(event["secret_ids"]) - secret_ids)
        if bad:
            problems.append(f"Event {eid}: unknown secret_ids: " + ", ".join(bad))
        bad = sorted(set(event["goal_ids"]) - goal_ids)
        if bad:
            problems.append(f"Event {eid}: goal_ids outside this chapter: " + ", ".join(bad))
        bad = sorted(set(event["next_event_ids"]) - event_ids)
        if bad:
            problems.append(f"Event {eid}: next_event_ids outside this chapter: " + ", ".join(bad))
        if not event["goal_ids"]:
            problems.append(f"Event {eid} serves no goal.")
        for reveal in event["reveals"]:
            if kinds.get(reveal["entity_id"]) not in entity_kinds:
                problems.append(f"Event {eid}: reveal entity_id {reveal['entity_id']} is unknown.")
        for encounter in event["encounters"]:
            if kinds.get(encounter["actor_id"]) != "npc":
                problems.append(
                    f"Event {eid}: encounter actor {encounter['actor_id']} must be an NPC entity."
                )
            if encounter["creature"] not in creature_names:
                problems.append(
                    f"Event {eid}: creature '{encounter['creature']}' is not in verified_creatures."
                )
            # The engine binds one actor to one creature profile everywhere.
            bound = bindings.setdefault(encounter["actor_id"], encounter["creature"])
            if bound != encounter["creature"]:
                problems.append(
                    f"Event {eid}: actor {encounter['actor_id']} is already a {bound}; reuse that "
                    f"creature or give the {encounter['creature']} its own NPC actor."
                )
    for goal in draft["goals"]:
        bad = sorted(set(goal["event_ids"]) - event_ids)
        if bad:
            problems.append(f"Goal {goal['id']}: event_ids outside this chapter: " + ", ".join(bad))
        bad = sorted(set(goal["depends_on"]) - goal_ids)
        if bad:
            problems.append(
                f"Goal {goal['id']}: depends_on outside this chapter: " + ", ".join(bad)
            )
    for check in draft["checks"]:
        normalized = normalise_check(check["check"])
        if normalized is None or normalized[1] not in ("skill", "ability", "tool"):
            problems.append(
                f"Check {check['id']}: '{check['check']}' is not a skill, ability or tool."
            )
        bad = sorted(set(check["event_ids"]) - event_ids)
        if bad:
            problems.append(
                f"Check {check['id']}: event_ids outside this chapter: " + ", ".join(bad)
            )
    for item in draft["items"]:
        if kinds.get(item["entity_id"]) != "clue":
            problems.append(f"Item {item['entity_id']} must be a clue entity.")
        bad = sorted(set(item["event_ids"]) - event_ids)
        if bad:
            problems.append(
                f"Item {item['entity_id']}: event_ids outside this chapter: " + ", ".join(bad)
            )
    for secret in draft["new_secrets"]:
        bad = sorted(i for i in secret["known_by"] if kinds.get(i) not in ("npc", "faction"))
        if bad:
            problems.append(
                f"Secret {secret['id']} known_by must name NPCs or factions: " + ", ".join(bad)
            )
    planned = len(plan.get("combat_encounters", []))
    fights = sum(1 for e in draft["events"] if e["encounters"])
    if fights < planned:
        problems.append(
            f"The plan calls for {planned} combat encounter(s) here but {fights} event(s) have "
            "encounters; give each planned fight an event encounter with an NPC actor and a "
            "verified creature."
        )
    present = {i for e in draft["events"] for i in e["secret_ids"]}
    for field, verb in (("sets_up_secret_ids", "set up"), ("reveals_secret_ids", "reveal")):
        missing = sorted(set(plan[field]) - present)
        if missing:
            problems.append(
                f"The plan says this chapter must {verb} these secrets; reference each in an "
                "event's secret_ids: " + ", ".join(missing)
            )
    if plan["primary_location_id"] not in {e["location_id"] for e in draft["events"]}:
        problems.append(
            "At least one event must take place at the planned primary location "
            + plan["primary_location_id"]
            + "."
        )
    return draft, problems


def compose(premise, cast, outline, chapters):
    """Join accepted steps into the shared Spine and Bundles contracts."""
    ids = chapter_ids(outline)
    plans = outline["chapters"]
    villain = premise["antagonist"]
    scheme = "\n".join(
        f"- {s['when']}: {s['action']} (if unopposed: {s['consequence']})"
        for s in premise["scheme"]
    )
    spine = {
        "title": premise["title"],
        "premise": premise["premise"],
        "setting": premise["setting"],
        "hook": premise["hook"],
        "ending": premise["ending"],
        "level_recommendation": premise["level_recommendation"],
        "dm_guidance": "\n\n".join(
            part
            for part in (
                premise["dm_guidance"],
                outline["dm_guidance"],
                "Central question: " + premise["central_question"],
                "Tone: " + premise["tone"] + ". Stakes: " + premise["stakes"],
                f"Antagonist: {villain['name']}, {villain['nature']}. Motive: {villain['motive']} "
                f"Methods: {villain['methods']} Weakness: {villain['weakness']}",
                "Antagonist's scheme if nobody interferes:\n" + scheme,
            )
            if part.strip()
        ),
        "chapters": [
            {
                "id": cid,
                "title": plan["title"],
                "brief": "\n\n".join(
                    (
                        plan["brief"],
                        "Purpose (" + plan["archetype"] + "): " + plan["dramatic_purpose"],
                        "Antagonist pressure: " + plan["antagonist_pressure"],
                        *(
                            ["Leads onward:\n" + "\n".join("- " + x for x in plan["leads_onward"])]
                            if plan["leads_onward"]
                            else []
                        ),
                    )
                ),
                "source_order": index,
                "play_order": index,
                "chronology": plan["chronology"],
                "pages": [],
                "entity_ids": list(
                    dict.fromkeys([plan["primary_location_id"], *plan["entity_ids"]])
                ),
                "secret_ids": list(
                    dict.fromkeys(plan["sets_up_secret_ids"] + plan["reveals_secret_ids"])
                ),
                "depends_on": [ids[index - 2]] if index > 1 else [],
            }
            for index, (cid, plan) in enumerate(zip(ids, plans, strict=True), 1)
        ],
        "entities": [to_entity(e) for e in cast["entities"]],
        "secrets": [
            to_secret(
                s,
                [
                    cid
                    for cid, p in zip(ids, plans, strict=True)
                    if s["id"] in p["sets_up_secret_ids"]
                ],
                [
                    cid
                    for cid, p in zip(ids, plans, strict=True)
                    if s["id"] in p["reveals_secret_ids"]
                ],
            )
            for s in cast["secrets"]
        ],
        "important_visual_pages": [],
    }
    bundles = {"chapters": chapters}
    spine = Spine.model_validate(spine).model_dump()
    bundles = Bundles.model_validate(bundles).model_dump()
    return spine, bundles


def partial_problems(premise, cast, outline, chapters, source, mechanics):
    """Run the shared deterministic validators on the chapters accepted so far."""
    ids = chapter_ids(outline)[: len(chapters)]
    trimmed = {**outline, "chapters": outline["chapters"][: len(chapters)]}
    spine, bundles = compose(premise, cast, trimmed, chapters)
    included = set(ids)
    for secret in spine["secrets"]:
        secret["setup_chapters"] = [c for c in secret["setup_chapters"] if c in included]
        secret["reveal_chapters"] = [c for c in secret["reveal_chapters"] if c in included]
    problems = [
        "Structural defect: " + str(issue)
        for issue in structural_defects(spine, bundles)[0] + reference_defects(spine, bundles)[0]
    ]
    try:
        validate_semantics(spine, canonical_profiles(bundles, mechanics), source)
    except NeedsAttention as exc:
        problems.append("Validation: " + str(exc))
    return problems


# --- Workflow steps -------------------------------------------------------------------


class AiGeneration:
    """Author/check node pairs. The Store remains the only state authority."""

    def __init__(self, workflow):
        self.workflow = workflow
        self.store = workflow.store

    # Step bookkeeping ------------------------------------------------------------------

    def accepted(self, step):
        return self.store.get("ai-" + step)

    def attempts(self, step):
        return self.store.get("ai-attempts-" + step) or []

    def limit(self):
        return ATTEMPTS * (1 + int(self.store.state().get("retry_round", 0)))

    async def author(self, step, prompt, schema, evidence):
        if self.accepted(step) is not None:
            return
        attempts = self.attempts(step)
        if attempts and attempts[-1]["problems"] is None:
            return  # Awaiting its check.
        if len(attempts) >= self.limit():
            raise NeedsAttention(
                f"AI generation step '{step}' still has problems after {len(attempts)} attempts; "
                "Retry grants more attempts. Last problems: " + "; ".join(attempts[-1]["problems"])
            )
        if attempts:
            evidence = {
                **evidence,
                "revision": {
                    "attempt": len(attempts) + 1,
                    "previous_output": attempts[-1]["value"],
                    "problems": attempts[-1]["problems"],
                    "instruction": "Return a complete corrected output that fixes every listed "
                    "problem and keeps everything else that was sound.",
                },
            }
        value = await self.workflow.semantic.call(
            "ai-" + step + "-" + str(len(attempts) + 1), prompt, schema, evidence
        )
        self.store.put(
            "ai-attempts-" + step,
            attempts + [{"value": value.model_dump(mode="json"), "problems": None}],
        )

    def check(self, step, validate):
        """Validate the newest attempt; True when accepted."""
        self.store.check()
        if self.accepted(step) is not None:
            return True
        attempts = self.attempts(step)
        value, problems = validate(attempts[-1]["value"])
        if problems:
            attempts[-1]["problems"] = problems
            self.store.put("ai-attempts-" + step, attempts)
            return False
        self.store.put("ai-" + step, value)
        return True

    # Shared evidence --------------------------------------------------------------------

    def basis(self):
        request = self.workflow.request()
        size, profile = size_profile(request)
        return {
            "request": request["generation"],
            "size": size,
            "size_profile": asdict(profile),
            "preferences": request["preferences"],
            "rules_edition": request["rules_edition"],
        }

    def outline_value(self):
        return self.accepted("outline")

    def plans(self):
        outline = self.outline_value()
        return list(zip(chapter_ids(outline), outline["chapters"], strict=True))

    def committed(self):
        result = []
        for cid, _plan in self.plans():
            bundle = self.store.get("ai-bundle-" + cid)
            if bundle is None:
                break
            result.append(bundle)
        return result

    def current_chapter(self):
        done = len(self.committed())
        plans = self.plans()
        return plans[done] if done < len(plans) else (None, None)

    def known_kinds(self):
        cast = self.accepted("cast")
        kinds = {e["id"]: e["kind"] for e in cast["entities"]}
        kinds.update({s["id"]: "secret" for s in cast["secrets"]})
        kinds.update(prior_identities(self.committed()))
        kinds.update({cid: "chapter" for cid, _ in self.plans()})
        return kinds

    def known_names(self):
        return entity_names(
            self.accepted("cast")["entities"]
            + [e for bundle in self.committed() for e in bundle["new_entities"]]
        )

    def actor_creatures(self):
        """Actor -> creature already fixed by accepted chapters."""
        result = {}
        for bundle in self.committed():
            profiles = {p["id"]: p["creature"] for p in bundle["mechanical_profiles"]}
            for event in bundle["events"]:
                for encounter in event["encounters"]:
                    result.setdefault(encounter["actor_id"], profiles[encounter["profile_id"]])
        return result

    def creatures(self):
        return creature_index(self.store.get("mechanics"))

    # Nodes ------------------------------------------------------------------------------

    async def premise(self):
        self.store.enter("ai_premise")
        await self.author("premise", "ai-premise", AiPremise, self.basis())

    def premise_check(self):
        return self.check("premise", check_premise)

    async def cast(self):
        self.store.enter("ai_cast")
        evidence = {**self.basis(), "premise": self.accepted("premise")}
        await self.author("cast", "ai-cast", AiCast, evidence)

    def cast_check(self):
        _, profile = size_profile(self.workflow.request())
        premise = self.accepted("premise")
        return self.check("cast", lambda value: check_cast(value, premise, profile))

    async def outline(self):
        self.store.enter("ai_outline")
        cast = self.accepted("cast")
        evidence = {
            **self.basis(),
            "premise": self.accepted("premise"),
            "cast": cast,
            "archetypes": list(ARCHETYPES),
        }
        await self.author("outline", "ai-outline", outline_schema(cast), evidence)

    def outline_check(self):
        _, profile = size_profile(self.workflow.request())
        cast = self.accepted("cast")
        return self.check("outline", lambda value: check_outline(value, cast, profile))

    async def chapter(self):
        self.store.enter("ai_chapter")
        cid, plan = self.current_chapter()
        if cid is None:
            return
        committed = self.committed()
        index = len(committed)
        outline = self.outline_value()
        evidence = {
            **self.basis(),
            "premise": self.accepted("premise"),
            "cast": self.accepted("cast"),
            "outline": [
                {"chapter_id": c, **p}
                for c, p in zip(chapter_ids(outline), outline["chapters"], strict=True)
            ],
            "requested_chapter": {
                "chapter_id": cid,
                "number": index + 1,
                "of": len(outline["chapters"]),
                **plan,
            },
            "story_so_far": [
                {
                    "chapter_id": bundle["chapter_id"],
                    "continuity_summary": self.store.get("ai-summary-" + bundle["chapter_id"]),
                    "goals": [
                        {k: g[k] for k in ("id", "title", "objective", "completion")}
                        for g in bundle["goals"]
                    ],
                    "events": [
                        {k: e[k] for k in ("id", "title", "location_id", "participants")}
                        for e in bundle["events"]
                    ],
                    "new_entities": [
                        {k: e[k] for k in ("id", "kind", "name", "description")}
                        for e in bundle["new_entities"]
                    ],
                    "new_secrets": [
                        {k: s[k] for k in ("id", "truth")} for s in bundle["new_secrets"]
                    ],
                }
                for bundle in committed
            ],
            "existing_ids": sorted(self.known_kinds()),
            "existing_combatants": self.actor_creatures(),
            "verified_creatures": self.creatures(),
        }
        await self.author("chapter-" + cid, "ai-chapter", AiChapterDraft, evidence)

    def chapter_check(self):
        cid, plan = self.current_chapter()
        if cid is None:
            return True
        _, profile = size_profile(self.workflow.request())
        known, names, creatures = self.known_kinds(), self.known_names(), self.creatures()
        combatants = self.actor_creatures()
        premise, cast, outline = (
            self.accepted("premise"),
            self.accepted("cast"),
            self.outline_value(),
        )
        committed = self.committed()

        def validate(value):
            draft, problems = check_chapter(
                value,
                plan=plan,
                chapter_id=cid,
                profile=profile,
                known_kinds=known,
                creatures=creatures,
                known_names=names,
                actor_creatures=combatants,
            )
            if problems:
                return draft, problems
            bundle = chapter_bundle(cid, draft)
            problems = partial_problems(
                premise,
                cast,
                outline,
                committed + [bundle],
                self.store.get("source"),
                self.store.get("mechanics") or [],
            )
            return draft, problems

        if not self.check("chapter-" + cid, validate):
            return False
        draft = self.accepted("chapter-" + cid)
        self.store.put("ai-summary-" + cid, draft["continuity_summary"])
        self.store.put("ai-bundle-" + cid, chapter_bundle(cid, draft))
        return True

    def chapters_complete(self):
        return self.current_chapter()[0] is None

    def compose(self):
        self.store.enter("ai_compose")
        premise, cast, outline = (
            self.accepted("premise"),
            self.accepted("cast"),
            self.outline_value(),
        )
        spine, bundles = compose(premise, cast, outline, self.committed())
        bundles = canonical_profiles(bundles, self.store.get("mechanics") or [])
        validate_semantics(spine, bundles, self.store.get("source"))
        self.store.put("spine", spine, digest([premise, cast, outline]))
        self.store.put("bundles", bundles, digest(spine))
