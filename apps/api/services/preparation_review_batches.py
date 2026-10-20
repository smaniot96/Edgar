"""Batch independent reviews by measured request size, retaining exact citations."""

from __future__ import annotations

import json

import tiktoken

from edgar_core.preparation import canonical_sha256


def review_grounding(payload):
    """Keep exact creatures once instead of repeating full stat blocks per encounter."""
    creatures = {}
    acts = []
    for raw in payload["acts"]:
        act = {**raw, "encounters": []}
        for receipt in raw["encounters"]:
            members = []
            for member in receipt["creatures"]:
                creature = member["creature"]
                creatures[creature["creature_id"]] = creature
                members.append({**member, "creature": {"creature_id": creature["creature_id"]}})
            act["encounters"].append({**receipt, "creatures": members})
        acts.append(act)
    return {**payload, "acts": acts, "verified_creatures": creatures}


def _fields(row, names):
    return {name: row[name] for name in names if name in row and row[name] not in (None, [], {})}


def _lean_guidance(guidance):
    seen, entries = set(), []
    for index, row in enumerate(guidance.get("evidence", [])):
        # Exact monster statistics are supplied once by the scoped grounding
        # report. The broad book search also returns unrelated monster actions.
        if row.get("role") == "mm":
            continue
        # The critic reviews the authored campaign. Engine arithmetic, equipment
        # tables and basic combat execution have already been checked by code.
        # Retain exact guidance for story structure, adjudication, party resources
        # and encounter risk, rather than repeating every retrieved combat rule.
        section = row.get("section", "").casefold()
        if row.get("role") == "phb":
            engine_topics = {"armor and shields", "heavy armor", "weapon proficiency", "weapon properties",
                "weapons", "adventuring gear", "bonus actions", "initiative", "surprise", "the order of combat",
                "your turn", "movement and position", "reactions", "actions in combat", "making an attack",
                "melee attacks", "opportunity attacks", "cover", "damage and healing", "death saving throws",
                "dropping to o hit points", "stabilizing a creature", "knocking a creature out"}
            if section.rsplit(">", 1)[-1].strip() in engine_topics:
                continue
        elif row.get("role") == "dmg":
            if any(name in section for name in ("evaluating encounter difficulty", "xp thresholds", "encounter multipliers")):
                continue
            relevant = ("adventure", "clues", "encounter", "small groups", "resolution", "resolving",
                        "rest", "trap", "damage severity", "healing", "morale", "monster motivation",
                        "involving the characters")
            if not any(name in section for name in relevant):
                continue
        identity = (row.get("role"), row.get("text"))
        if identity in seen:
            continue
        seen.add(identity)
        entries.append({**_fields(row, ("role", "source", "page", "page_end", "section", "text", "rule_policy")),
                        "text_json_pointer": f"/study/evidence/{index}/text"})
    return {"rules_edition": guidance.get("rules_edition"), "evidence": entries,
            "monster_rules": "See exact verified creature stat blocks and parsed mechanics in grounding."}


def _lean_grounding(grounding, relevant):
    acts, creature_ids, origins = [], set(), {}
    for index, raw in enumerate(grounding.get("acts", [])):
        encounters = []
        for position, receipt in enumerate(raw.get("encounters", [])):
            if receipt["scene_id"] not in relevant:
                continue
            members = []
            for member_index, member in enumerate(receipt["creatures"]):
                creature_id = member["creature"]["creature_id"]
                creature_ids.add(creature_id)
                origins.setdefault(creature_id,
                    f"/acts/{index}/encounters/{position}/creatures/{member_index}/creature")
                members.append(_fields(member, ("creature", "actor_id", "requested_quantity", "effective_quantity", "role")))
            encounters.append({**_fields(receipt, ("encounter_id", "scene_id", "actor_ids", "raw_xp", "adjusted_xp",
                "difficulty", "thresholds", "solo_budget", "adaptation_notes", "supported_mechanics",
                "unsupported_mechanics", "findings")), "creatures": members})
        if encounters:
            acts.append({"act_id": raw["plan"]["act_id"], "encounters": encounters})
    creatures = []
    for creature_id in sorted(creature_ids):
        raw = grounding["verified_creatures"][creature_id]
        creatures.append({**_fields(raw, ("creature_id", "creature_name", "source", "page", "page_end",
            "challenge_rating", "xp", "stat_block_text", "monster_stats", "evidence_origin")),
            "json_pointer": origins[creature_id],
            "text_json_pointer": origins[creature_id] + "/stat_block_text"})
    return {"acts": acts, "verified_creatures": creatures}


def review_batches(manuscript, common, blueprint_artifact, extraction, *, target_tokens=90000,
                   reviewed_ranges=None):
    """Partition exact chapter coverage around a lean semantic review context.

    The provider receives source, authored decisions and exact grounded statistics,
    rather than the compiler's repeated expanded joins and integrity receipts.
    Global IDs, goals and route semantics remain visible in every batch. Every
    citation pointer still addresses the original immutable artifact.
    """
    encoding = tiktoken.get_encoding("o200k_base")
    chapters = list(manuscript.chapters)
    canonical = common["canonical"]
    prepared = canonical["prepared"]
    blueprint = blueprint_artifact.payload["blueprint"]
    design = common["whole_design"]
    pages = dict(extraction.text_pages())
    page_indices = {page.page: index for index, page in enumerate(extraction.pages)}
    inventory = {row["kind"]: row for row in common.get("evidence_inventory", [])}
    citation_kinds = {"canonical_manifest", "prepared_campaign", "retained_manuscript", "canonical_extraction",
                      "compact_blueprint", "grounding_report", "asset_coverage", "campaign_design", "book_dossier"}
    global_canonical = {"source": canonical["source"], "opening_scene_id": canonical["opening_scene_id"]}
    for name, fields in {
        "scenes": ("scene_id", "title", "links"),
        "entities": ("entity_id", "kind", "title", "discovery_flag", "reveal_on", "object_id", "stat_reference", "lair_reference", "surprise"),
        "objectives": ("canonical_id", "title", "scene_ids", "completion_condition", "engine_requirements", "engine_forbidden", "requires"),
        "milestones": ("canonical_id", "title", "scene_ids", "completion_condition", "engine_requirements", "engine_forbidden", "requires"),
        "objects": ("canonical_id", "title", "scene_ids", "operations", "initial_owner", "portable", "consumable", "requires", "reveal_on", "statuses"),
        "promises": ("canonical_id", "title", "scene_ids", "requires", "reveal_on", "statuses"),
        "clocks": ("canonical_id", "title", "scene_ids", "clock_max", "requires", "reveal_on", "clock_reveal"),
    }.items():
        global_canonical[name] = [{"json_pointer": f"/manifest/{name}/{index}", "record": _fields(row, fields)}
                                  for index, row in enumerate(canonical.get(name, []))]
    for row in global_canonical["entities"]:
        for name in ("stat_reference", "lair_reference"):
            if name in row["record"]:
                row["record"][name] = _fields(row["record"][name],
                    ("kind", "creature_name", "collection", "rules_edition", "passage_id"))
    global_canonical["prepared"] = {
        "narrative_mode": prepared.get("narrative_mode", "agent"),
        "quests": [_fields(row, ("quest_id", "objective_id", "milestone_ids", "required_for_completion",
                                    "required_milestone_ids", "scene_ids", "prerequisites", "required_approaches"))
                   for row in prepared.get("quests", [])],
    }
    assets = common.get("assets", {})
    receipt = assets.get("receipt", {})
    shared = {name: common[name] for name in ("narrative_contract", "actual_party", "critic_finding_schema",
              "manuscript_artifact_id", "manuscript_artifact_sha256") if name in common}
    shared.update(
        binding=_fields(common.get("binding", {}), ("run_id", "run_binding_sha256", "source_binding_sha256",
                        "hero_party_sha256", "manifest_artifact_id", "prepared_artifact_id", "manuscript_artifact_id")),
        whole_design={**_fields(design, ("title", "premise", "opening_scene_id", "continuity")),
                      "acts": [_fields(row, ("act_id", "title", "scene_ids", "summary", "role", "advance_ids", "climax_ids", "source_pages"))
                               for row in design["acts"]]},
        review_bindings_sha256=canonical_sha256(common.get("binding", {})),
        canonical=global_canonical, source_guidance=_lean_guidance(common.get("source_guidance", {})),
        evidence_inventory=[row for row in common.get("evidence_inventory", []) if row["kind"] in citation_kinds],
        blueprint_artifact_id=blueprint_artifact.artifact_id,
        assets={"coverage_count": len(receipt.get("coverage", [])),
                "coverage_status_counts": {status: sum(row.get("status") == status for row in receipt.get("coverage", []))
                                           for status in sorted({row.get("status", "unknown") for row in receipt.get("coverage", [])})},
                "coverage_exceptions": [{"json_pointer": f"/receipt/coverage/{index}",
                    **_fields(row, ("asset_id", "kind", "required", "status", "player_review_status", "explanation"))}
                    for index, row in enumerate(receipt.get("coverage", []))
                    if row.get("status") != "ready" or row.get("player_review_status") not in {"not_required", "approved"}],
                "findings": receipt.get("findings", []),
                "visual_reviews": assets.get("visual_reviews", [])},
        composition=("Read every assigned text character. The complete retained source and authored DM notes are in "
                     "reviewed_chapters. The scoped blueprint supplies exact source-span references, disclosure gates, "
                     "encounters and optional tools. Compiler-expanded joins, offsets and digests are verified by code. "
                     "All json_pointer/text_json_pointer fields address original artifacts; never cite compact array indices."),
    )

    def evidence(indices, *, intervals=None):
        relevant = {scene for index in indices for scene in chapters[index].scene_ids}
        complete = []
        for index in indices:
            chapter = chapters[index]
            start, end = (intervals or {}).get(index, (0, len(chapter.text)))
            complete.append({**_fields(chapter.model_dump(mode="json"), ("chapter_id", "act_id", "order", "role", "scene_ids")),
                             "text": chapter.text[start:end], "text_json_pointer": f"/manuscript/chapters/{index}/text",
                             "text_start": start, "text_end": end,
                             "complete_chapter": start == 0 and end == len(chapter.text)})
        related = []
        neighbours = {index for current in indices for index in (current - 1, current + 1)
                      if 0 <= index < len(chapters) and index not in indices}
        for index in sorted(neighbours):
            chapter = chapters[index]
            related.append({"chapter_id": chapter.chapter_id, "text_json_pointer": f"/manuscript/chapters/{index}/text",
                            "exact_excerpts": [chapter.text[:900], chapter.text[-900:]]})
        scoped = {name: blueprint[name] for name in ("resource_plan", "progression_plan") if name in blueprint}
        for name in ("scenes", "facts", "decisions", "transitions"):
            scoped[name] = []
            for index, row in enumerate(blueprint.get(name, [])):
                if not (row.get("id") in relevant or row.get("scene") in relevant
                        or set(row.get("scenes", [])) & relevant):
                    continue
                # Scene notes/additions are already retained verbatim in the chapter.
                record = {key: value for key, value in row.items() if name != "scenes" or key not in {"notes", "additions"}}
                scoped[name].append({"json_pointer": f"/blueprint/{name}/{index}", "record": record})
        public = []
        public_scopes = {}
        for index, passage in enumerate(canonical["passages"]):
            if passage["audience"] != "player" or not set(passage["scene_ids"]) & relevant:
                continue
            scope = _fields(passage, ("scene_ids", "entity_ids", "known_scene_ids", "requires"))
            identity = json.dumps(scope, sort_keys=True)
            if identity not in public_scopes:
                item = {"disclosure_scope": scope, "passages": []}
                public_scopes[identity] = item
                public.append(item)
            row = [f"/manifest/passages/{index}", passage["passage_id"],
                   f"/pages/{page_indices[passage['page']]}/text", passage["start"], passage["end"]]
            exact = pages[passage["page"]][passage["start"]:passage["end"]]
            content = {"exact_text": exact}
            for chapter in complete:
                offset = chapter["text"].find(exact)
                if offset >= 0:
                    reference = {"text_json_pointer": chapter["text_json_pointer"],
                                 "text_start": chapter["text_start"] + offset,
                                 "text_end": chapter["text_start"] + offset + len(exact)}
                    if len(json.dumps(reference)) < len(json.dumps(content)):
                        content = reference
                    break
            row.append(content)
            public_scopes[identity]["passages"].append(row)
        active = {**global_canonical}
        packets = [{"json_pointer": f"/manifest/prepared/scene_packets/{index}",
                    "record": _fields(row, ("scene_id", "entry_requires", "entity_ids", "objective_ids", "milestone_ids"))}
                   for index, row in enumerate(prepared.get("scene_packets", [])) if row["scene_id"] in relevant]
        entity_ids = {entity for row in packets for entity in row["record"].get("entity_ids", [])}
        entity_ids.update(entity for row in canonical["passages"] if set(row["scene_ids"]) & relevant
                          for entity in row.get("entity_ids", []))
        active["entities"] = [{**row, "record": row["record"] if row["record"]["entity_id"] in entity_ids
                               else _fields(row["record"], ("entity_id", "kind", "title"))}
                              for row in global_canonical["entities"]]
        active["objects"] = [{**row, "record": row["record"] if set(row["record"].get("scene_ids", [])) & relevant
                              else _fields(row["record"], ("canonical_id", "title", "scene_ids"))}
                             for row in global_canonical["objects"]]
        active["prepared"] = {**global_canonical["prepared"], "routes": [
            {"json_pointer": f"/manifest/prepared/routes/{index}",
             "record": _fields(row, ("from_scene_id", "to_scene_id", "visibility", "traversal_requires", "reveal_requires"))}
            for index, row in enumerate(prepared.get("routes", []))
            if row["from_scene_id"] in relevant or row["to_scene_id"] in relevant], "scene_packets": packets}
        return {**shared, "canonical": active, "reviewed_chapters": complete, "related_chapter_text": related,
                "authored_blueprint": scoped,
                "public_source_excerpts": {"audience": "player", "source_artifact_id": inventory.get("canonical_extraction", {}).get("artifact_id"),
                    "passage_columns": ["original_json_pointer", "passage_id", "source_text_json_pointer", "start", "end", "exact_text_or_retained_reference"],
                    "disclosures": public},
                "grounding": _lean_grounding(common.get("grounding", {}), relevant)}

    def count(value):
        return len(encoding.encode(json.dumps(value, ensure_ascii=False, sort_keys=True), disallowed_special=())) + 6000

    groups, current = [], []
    for index, chapter in enumerate(chapters):
        cursor = 0
        remaining = []
        for start, end in sorted((reviewed_ranges or {}).get(chapter.chapter_id, ())):
            if not 0 <= start < end <= len(chapter.text):
                raise ValueError("Saved independent review range is outside the retained chapter.")
            if start > cursor:
                remaining.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < len(chapter.text):
            remaining.append((cursor, len(chapter.text)))
        for start, end in remaining:
            item = (index, start, end)
            candidate = current + [item]
            indices = [i for i, _, _ in candidate]
            intervals = {i: (s, e) for i, s, e in candidate}
            if current and (index in [i for i, _, _ in current]
                            or count(evidence(indices, intervals=intervals)) > target_tokens):
                groups.append(current)
                current = [item]
            else:
                current = candidate
    if current:
        groups.append(current)
    batches = []
    for items in groups:
        indices = [i for i, _, _ in items]
        intervals = {i: (s, e) for i, s, e in items}
        value = evidence(indices, intervals=intervals)
        # A small overshoot is cheaper than resending the entire semantic context
        # for the last few paragraphs of an otherwise complete chapter.
        if count(value) <= target_tokens * 1.15 or len(indices) != 1:
            batches.append((indices, value))
            continue
        index, start, stop = items[0]
        text = chapters[index].text
        # A preferred request size never refuses work. Keep enough text per request
        # even if unusually large semantic context exceeds the preferred size itself.
        effective_target = max(target_tokens, count(evidence(indices, intervals={index: (0, 0)})) + 8000)
        while start < stop:
            low, high, end = start + 1, stop, start + 1
            while low <= high:
                middle = (low + high) // 2
                if count(evidence(indices, intervals={index: (start, middle)})) <= effective_target:
                    end, low = middle, middle + 1
                else:
                    high = middle - 1
            if end < stop:
                boundary = text.rfind("\n", start + (end - start) // 2, end)
                if boundary > start:
                    end = boundary + 1
            batches.append((indices, evidence(indices, intervals={index: (start, end)})))
            start = end
    return batches
