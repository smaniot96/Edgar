"""Batch independent reviews by measured request size, retaining exact citations."""

from __future__ import annotations

import json
from copy import deepcopy

import tiktoken


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


def review_batches(manuscript, common, *, target_tokens=90000):
    """Review each complete chapter once, with actual cross-chapter evidence.

    The target partitions requests; it never blocks preparation or limits spending.
    Other chapters supply exact transition text and their full authored DM notes.
    All original global JSON pointers remain valid against the retained manuscript.
    """
    encoding = tiktoken.get_encoding("o200k_base")
    chapters = list(manuscript.chapters)

    def evidence(indices, *, interval=None, compact=False):
        assigned = set(indices)
        relevant = {scene for index in indices for scene in chapters[index].scene_ids}
        complete = []
        related = []
        for index, chapter in enumerate(chapters):
            pointer = f"/manuscript/chapters/{index}/text"
            if index in assigned:
                start, end = interval if interval is not None else (0, len(chapter.text))
                complete.append({**chapter.model_dump(mode="json"),
                                 "text": chapter.text[start:end], "text_json_pointer": pointer,
                                 "text_start": start, "text_end": end,
                                 "complete_chapter": start == 0 and end == len(chapter.text)})
            else:
                text = chapter.text
                excerpts = [text[:1800], text[-1800:]]
                marker = text.find("[DM preparation notes]")
                if marker >= 0 and not compact:
                    excerpts.append(text[marker:])
                related.append({"chapter_id": chapter.chapter_id, "act_id": chapter.act_id,
                                "text_json_pointer": pointer, "exact_excerpts": excerpts})
        scoped = deepcopy(common)
        canonical = scoped["canonical"]
        # Keep actual cross-act definitions/effects/routes. Scope verbose private
        # packets, provenance and branches to reviewed scenes and retain global
        # pointers, so compacted array positions never become false citations.
        def rows(values, path, predicate=lambda row: True):
            return [{"json_pointer": f"{path}/{index}", "record": row}
                    for index, row in enumerate(values) if predicate(row)]

        for name in ("passages", "scenes", "resolution_policies"):
            values = canonical[name]
            canonical[name] = rows(values, f"/manifest/{name}", lambda row:
                row.get("scene_id") in relevant or bool(set(row.get("scene_ids", [])) & relevant))
        prepared = canonical["prepared"]
        for name in ("scene_packets", "branches", "extensions"):
            values = prepared[name]
            prepared[name] = rows(values, f"/manifest/prepared/{name}", lambda row:
                row.get("scene_id") in relevant or bool(set(row.get("scene_ids", [])) & relevant))
        branch_ids = {item["record"]["branch_id"] for item in prepared["branches"]}
        prepared["outcomes"] = rows(prepared["outcomes"], "/manifest/prepared/outcomes",
                                    lambda row: row["branch_id"] in branch_ids)
        scoped["cross_act_execution"] = {
            "policies": [{"json_pointer": f"/manifest/resolution_policies/{i}",
                          "record": {key: value for key, value in row.items()
                                     if key not in ("provenance",)}}
                         for i, row in enumerate(common["canonical"]["resolution_policies"])],
            "branches": [{key: row[key] for key in ("branch_id", "scene_id", "approach", "requires", "policy_ids", "outcome_ids")}
                         for row in common["canonical"]["prepared"]["branches"]],
            "outcomes": [{key: row[key] for key in ("outcome_id", "branch_id", "result", "policy_selections", "requires", "compatible_with")}
                         for row in common["canonical"]["prepared"]["outcomes"]],
        }
        if compact:
            adjacent = set(relevant)
            for route in common["canonical"]["prepared"]["routes"]:
                if route["from_scene_id"] in relevant or route["to_scene_id"] in relevant:
                    adjacent.update((route["from_scene_id"], route["to_scene_id"]))
            execution = scoped["cross_act_execution"]
            execution["policies"] = [item for item in execution["policies"]
                                     if set(item["record"].get("scene_ids", [])) & adjacent]
            execution["branches"] = [row for row in execution["branches"] if row["scene_id"] in adjacent]
            adjacent_branches = {row["branch_id"] for row in execution["branches"]}
            execution["outcomes"] = [row for row in execution["outcomes"] if row["branch_id"] in adjacent_branches]
            act_ids = {chapters[index].act_id for index in indices}
            grounding = scoped["grounding"]
            grounding["acts"] = [row for row in grounding["acts"] if row["plan"]["act_id"] in act_ids]
            creature_ids = {member["creature"]["creature_id"] for row in grounding["acts"]
                            for receipt in row["encounters"] for member in receipt["creatures"]}
            grounding["verified_creatures"] = {key: row for key, row in grounding["verified_creatures"].items()
                                               if key in creature_ids}
        return {**scoped, "reviewed_chapters": complete, "related_chapter_text": related}

    def count(value):
        return len(encoding.encode(json.dumps(value, ensure_ascii=False, sort_keys=True),
                                   disallowed_special=())) + 6000

    batches, current = [], []
    for index in range(len(chapters)):
        proposed = current + [index]
        if current and count(evidence(proposed)) > target_tokens:
            batches.append((current, evidence(current)))
            current = [index]
        else:
            current = proposed
    if current:
        batches.append((current, evidence(current)))
    bounded = []
    for indices, value in batches:
        if count(value) <= target_tokens or len(indices) != 1:
            bounded.append((indices, value))
            continue
        index = indices[0]
        text = chapters[index].text
        start = 0
        # Partitioning is advisory, never a request refusal or a one-character
        # dispatch loop when shared evidence itself exceeds the preferred size.
        effective_target = max(target_tokens, count(evidence(indices, interval=(0, 0), compact=True)) + 8000)
        # A truly long chapter is covered by exact contiguous slices, rather than
        # refusing the run. No character is skipped and no prose is summarized.
        while start < len(text):
            end = len(text)
            candidate = evidence(indices, interval=(start, end), compact=True)
            while end - start > 1 and count(candidate) > effective_target:
                end = start + (end - start) // 2
                candidate = evidence(indices, interval=(start, end), compact=True)
            bounded.append((indices, candidate))
            start = end
    return bounded
