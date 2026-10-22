"""Resolve public actor names and exact descriptors within the verified current scene.

This selects a reviewed actor, never a similar monster template. The record resolver
still enforces the actor's exact statistics, edition, source and scene restrictions.
"""

from __future__ import annotations

import re

from db.vector.references import normalize_reference
from edgar_core.manifests import require_published, select_scene_context


def _stat_descriptors(actor: dict, manifest, selection: dict, state: dict, collection: str) -> list[str]:
    """An actor's own reviewed stat header can bind a visible exact subtype.

    This is not a race-to-monster lookup: only the actor's explicit module stat reference
    is eligible, and callers additionally require the descriptor in public evidence about
    that actor. A shared subtype remains ambiguous across current eligible actors.
    """
    from agent.nodes.story_effects import _reviewed_chunk

    reference = actor.get("stat_reference") or {}
    if reference.get("kind") != "module":
        return []
    passage_id = reference.get("passage_id")
    passage = next((p for p in manifest.passages if p.passage_id == passage_id), None)
    if not passage or passage_id not in selection["planner_passage_ids"] or actor["entity_id"] not in passage.entity_ids:
        return []
    for chunk in state.get("planner_context", []) or []:
        if chunk.get("collection") != collection or chunk.get("passage_id") != passage_id or not _reviewed_chunk(chunk, state):
            continue
        lines = [line.strip() for line in chunk.get("text", "").splitlines() if line.strip()]
        if len(lines) < 2 or normalize_reference(lines[0].lstrip("# ")) != normalize_reference(actor["title"]):
            continue
        # The subtype belongs to the stat header, never traits, tactics or other prose.
        header = re.fullmatch(r"(?:Tiny|Small|Medium|Large|Huge|Gargantuan)\s+[^()\n,]+\(([^()\n]+)\),[^\n]+", lines[1], re.I)
        if header:
            return [part.strip().casefold() for part in header[1].split(",")
                    if re.fullmatch(r"[\w -]{3,60}", part.strip())]
    return []


def scene_actor_aliases(state: dict) -> dict[str, list[str]]:
    """Names and exact stat descriptors need verified public evidence about that actor.

    Private titles alone do not create aliases. In particular, a private 'Captain Ash'
    can bind the public 'Ash', but cannot make 'Captain' or an unseen actor targetable.
    Ambiguity is resolved by callers across all eligible actors, before selecting one.
    """
    from agent.nodes.story_effects import _reviewed_chunk

    current = state.get("current_scene_id")
    if state.get("manifest_status") != "reviewed" or not current:
        return {}
    result = {}
    for collection, value in (state.get("adventure_manifests") or {}).items():
        manifest = require_published(value)
        selection = select_scene_context(manifest, current, state.get("world_flags"))
        passages = {p.passage_id: p for p in manifest.passages
                    if p.passage_id in selection["player_passage_ids"]
                    and current in p.scene_ids}
        chunks = [chunk for chunk in state.get("adventure_context", []) or []
                  if chunk.get("collection") == collection
                  and chunk.get("passage_id") in passages
                  and _reviewed_chunk(chunk, state)]
        for actor in selection["entities"]:
            if actor["kind"] != "actor":
                continue
            name = actor["title"]
            words = normalize_reference(name).split()
            public = " ".join(
                normalize_reference(chunk["text"]) for chunk in chunks
                if actor["entity_id"] in passages[chunk["passage_id"]].entity_ids
            )
            aliases = [
                " ".join(words[start:]) for start in range(1, len(words))
                if len(words[-1]) >= 3
                and f" {' '.join(words[start:])} " in f" {public} "
            ]
            if f" {normalize_reference(name)} " in f" {public} ":
                aliases.insert(0, normalize_reference(name))
            for descriptor in _stat_descriptors(actor, manifest, selection, state, collection):
                if f" {normalize_reference(descriptor)} " in f" {public} ":
                    aliases.append(descriptor)
                    if f" the {normalize_reference(descriptor)} " in f" {public} ":
                        aliases.append("the " + descriptor)
            if aliases:
                result[name] = list(dict.fromkeys(aliases))
    return result


def canonical_actor_name(name: str, aliases: dict[str, list[str]]) -> str:
    """Only a unique current actor can claim an exact earned short name."""
    key = normalize_reference(name)
    exact = [title for title in aliases if normalize_reference(title) == key]
    if len(exact) == 1:
        return exact[0]
    matches = [
        title for title, values in aliases.items()
        if key in {normalize_reference(title), *(normalize_reference(value) for value in values)}
    ]
    return matches[0] if len(matches) == 1 else name


def public_actor_name(name: str, aliases: dict[str, list[str]]) -> str | None:
    """Keep an earned suffix public while retaining the full source binding privately.

    None means no earned suffix uniquely identifies this actor. Choosing a hidden full
    title to disambiguate would disclose information the player has not learned.
    """
    if name not in aliases or normalize_reference(name) in {normalize_reference(alias) for alias in aliases[name]}:
        return name
    tokens = list(re.finditer(r"\w+", name))
    for token in tokens[1:]:
        suffix = name[token.start():]
        if normalize_reference(suffix) in {normalize_reference(alias) for alias in aliases[name]} and canonical_actor_name(suffix, aliases) == name:
            return suffix
    for alias in aliases[name]:
        if canonical_actor_name(alias, aliases) == name:
            return alias
    return None


def roster_name_matches(enemy: dict, name: str) -> bool:
    key = normalize_reference(name)
    return bool(key) and key in {
        normalize_reference(str(value))
        for value in [enemy.get("name", ""), enemy.get("canonical_name", ""), *(enemy.get("target_aliases") or [])]
    }


def matching_roster_targets(enemies: list[dict], name: str) -> list[dict]:
    """An exact full roster name wins over another actor's short alias."""
    key = normalize_reference(name)
    exact = [enemy for enemy in enemies if key and normalize_reference(str(enemy.get("canonical_name") or enemy.get("name", ""))) == key]
    return exact or [enemy for enemy in enemies if roster_name_matches(enemy, name)]


def unique_roster_target(enemies: list[dict], name: str) -> dict | None:
    matches = matching_roster_targets(enemies, name)
    return matches[0] if len(matches) == 1 else None
