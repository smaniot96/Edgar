"""Public scene descriptions for visible actors whose canonical identity is still hidden."""

from __future__ import annotations

import hashlib
import re

# Extract a short noun phrase from the public wording. An actor's private title only binds a
# matching word to a current actor; it is never used as the public label or as creature statistics.
_PHRASE = re.compile(
    r"\b(?:[Aa]n?|[Tt]he|[Oo]ne|[Tt]wo|[Tt]hree|[Ff]our|[Ff]ive|[Ss]ix|[Ss]everal|\d{1,2})"
    r"\s+(?:[a-z][a-z-]*\s+){0,4}[a-z][a-z-]*\b"
)
_GENERIC_HEADS = {"figure", "figures", "person", "people", "creature", "creatures", "beast", "beasts"}
_IGNORED_WORDS = {"the", "a", "an", "of", "and", "s"}
_UNCERTAIN = re.compile(r"\b(?:if|unless|might|could|rumou?r\w*|told|says?|said|tales?|once|not|no|never)\b", re.I)
_ABSENT_AFTER = re.compile(
    r"^\s*(?:"
    r"(?:is|are|was|were|do|does|did|has|have|had|will|would|can|could)\s+"
    r"(?:(?:currently|actually|now|still|certainly)\s+)*(?:not|never|no\s+longer)\b"
    r"|(?:is|are|was|were)\s+(?:absent|missing|gone|nowhere)\b"
    r"|(?:isn|aren|wasn|weren|don|doesn|didn|hasn|haven|hadn|won|wouldn|can|couldn)['’]t\b"
    r"|no\s+longer\b)", re.I,
)


def descriptive_actor_mentions(text: str, actors: list[dict]) -> list[str]:
    """Only source-verbatim descriptions, with a word binding to an eligible current actor.

    A private 'Cloaked Bear Guard' may bind a public 'huge cloaked figure'; the label exposes no
    bear identity. Quoted accounts, conditional sightings and unrelated objects do not qualify.
    """
    body = re.sub(r'"[^"\n]*"|“[^”\n]*”', lambda m: " " * len(m[0]), text)
    result = []
    for actor in actors:
        if actor.get("kind") != "actor":
            continue
        title_words = set(re.findall(r"[a-z][a-z-]*", str(actor.get("title") or "").lower())) - _IGNORED_WORDS
        for match in _PHRASE.finditer(body):
            sentence_prefix = re.split(r"[.!?\n]", body[:match.start()])[-1]
            if _UNCERTAIN.search(sentence_prefix):
                continue
            words = match[0].split()
            word_ends = [token.end() for token in re.finditer(r"\S+", match[0])]
            # The greedy match may include the actor's following verb. Stop at the first known
            # actor noun or neutral person noun so an action or hidden identity never enters a label.
            title_head = re.findall(r"[a-z][a-z-]*", str(actor.get("title") or "").lower())[-1:]
            for end in range(1, len(words)):
                head = words[end].lower()
                phrase_words = {word.lower() for word in words[1:end + 1]}
                if head not in set(title_head) | _GENERIC_HEADS:
                    continue
                if not phrase_words.intersection(title_words):
                    continue
                if _ABSENT_AFTER.search(body[match.start() + word_ends[end]:]):
                    continue
                # An adjective shared with a title is insufficient unless a neutral person head
                # follows it; e.g. 'a cloaked doorway' is not an actor description.
                if head in title_words and end + 1 < len(words) and words[end + 1].lower() in _GENERIC_HEADS:
                    continue
                if head in title_words and head not in _GENERIC_HEADS:
                    later = words[end + 1:] if end + 1 < len(words) else []
                    if later and not re.match(r"^(?:wear\w*|stand\w*|push\w*|arriv\w*|approach\w*|follow\w*|with|and|behind|is|are|has|have)$", later[0], re.I):
                        continue
                label = " ".join(words[:end + 1])
                label = re.sub(r"^(?:[Aa]n?|[Tt]he)\s+", "", label)
                if label not in result:
                    result.append(label)
                break
    return result


def visible_descriptive_participants(state: dict) -> list[dict]:
    """Derive descriptive nonparticipants from verified current public passages only."""
    from agent.nodes.story_effects import _reviewed_chunk

    current = state.get("current_scene_id")
    if state.get("manifest_status") != "reviewed" or not current:
        return []
    actors = [e for e in state.get("discovery_entities", []) or [] if isinstance(e, dict)]
    result = []
    for chunk in state.get("adventure_context", []) or []:
        if chunk.get("audience") != "player" or current not in chunk.get("scene_ids", []):
            continue
        if not _reviewed_chunk(chunk, state):
            continue
        for label in descriptive_actor_mentions(str(chunk.get("text") or ""), actors):
            identity = f"{chunk.get('collection')}:{current}:{label.casefold()}"
            result.append({
                "id": "visible_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                "name": label,
                "participation": "visible_nonparticipant",
                "reason": "Visible in this scene; outside the active combat roster and takes no turn in this encounter.",
            })
    return list({row["id"]: row for row in result}.values())


def refresh_visible_participants(state: dict, combat_state: dict, player: dict) -> None:
    """Keep an existing encounter truthful after reload without adding combat turns or stats."""
    existing = list(player.get("encounter_participants") or [])
    labels = {str(row.get("name") or "").casefold() for row in existing}
    for row in visible_descriptive_participants(state):
        if row["name"].casefold() not in labels:
            existing.append(row)
            labels.add(row["name"].casefold())
    if existing:
        player["encounter_participants"] = existing
        combat_state["participants"] = existing
