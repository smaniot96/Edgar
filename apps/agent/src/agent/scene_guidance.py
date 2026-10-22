"""Current source evidence reaches the parser and earned answers reach narration.

Reading scene guidance is local retrieval, not another semantic decision. The
existing consequence planner remains the DM who judges what an action earns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from edgar_core.manifests import require_published


def scene_signature(state):
    payload = {
        "scene": state.get("current_scene_id"),
        "flags": state.get("world_flags") or {},
        "edition": state.get("rules_edition", "2014"),
        "manifests": {
            key: value.get("reviewed_sha256")
            for key, value in (state.get("adventure_manifests") or {}).items()
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cached_scene_context(state):
    cache = state.get("parser_scene_context") or {}
    return cache.get("context") if cache.get("signature") == scene_signature(state) else None


async def preload_scene_context(state):
    """Read the current scene once, then reuse that exact verified selection."""
    from db.vector.retrieval import retrieve_active_scene

    if state.get("source_mode") != "module" or not state.get("adventure_manifests"):
        return None
    if cached_scene_context(state) is not None:
        return state["parser_scene_context"]
    context = await asyncio.to_thread(
        retrieve_active_scene,
        state["adventure_manifests"],
        state.get("current_scene_id"),
        state.get("world_flags"),
        client=state.get("qdrant_client"),
        rules_edition=state.get("rules_edition", "2014"),
    )
    transitions = []
    # Only expose the linked opportunity's trigger, never its private facts/source.
    # Exact retrieval still checks hashes; these references authorize no observations.
    for destination in context.get("allowed_scene_ids", []):
        if destination == state.get("current_scene_id"):
            continue
        linked = await asyncio.to_thread(
            retrieve_active_scene,
            state["adventure_manifests"],
            destination,
            state.get("world_flags"),
            client=state.get("qdrant_client"),
            rules_edition=state.get("rules_edition", "2014"),
        )
        for chunk in linked.get("planner_context", []):
            if chunk.get("scene_ids") != [destination]:
                continue
            paragraphs = chunk.get("text", "").split("\n\n")
            if len(paragraphs) < 3 or not paragraphs[1].startswith("Trigger: "):
                continue
            transitions.append(
                {
                    "scene_id": destination,
                    "title": paragraphs[0].removeprefix("# "),
                    "trigger": paragraphs[1].removeprefix("Trigger: "),
                    "timing": paragraphs[2].removeprefix("Timing: "),
                }
            )
            break
    return {
        "signature": scene_signature(state),
        "context": {**context, "manifest_status": "reviewed"},
        "event_transitions": transitions,
    }


def linked_transition_choices(state):
    """Bind one-hop opportunities to exact current evidence, without deciding their triggers.

    The live DM still judges whether play earns a move. Normal validation retains source,
    trigger, one-hop and stationary-position boundaries. Destination secrets stay private.
    """
    from agent.nodes.story_effects import story_candidates
    from edgar_core.sources import eligible_context, source_citation

    edition = state.get("rules_edition", "2014")
    evidence = eligible_context(
        state.get("adventure_context", []) + state.get("planner_context", []), edition
    )
    candidates = {
        candidate["target"]["canonical_id"]: candidate
        for candidate in story_candidates(state)
        if candidate["target"]["kind"] == "scene" and candidate.get("can_move")
    }
    choices = []
    for transition in (state.get("parser_scene_context") or {}).get("event_transitions", []):
        candidate = candidates.get(transition["scene_id"])
        if not candidate:
            continue
        target = candidate["target"]
        supporting = [
            chunk for chunk in evidence
            if chunk.get("passage_id") in candidate["passage_ids"]
            and all(
                chunk.get(key) == target[key]
                for key in ("collection", "source_sha256", "manifest_sha256")
            )
        ]
        supporting.sort(key=lambda chunk: (
            0 if chunk.get("scene_ids") == [state.get("current_scene_id")]
            and chunk.get("passage_id", "").endswith("_dm")
            else 1 if chunk.get("audience") == "player" else 2
        ))
        references = [
            source_citation(chunk, edition)["reference_id"]
            for chunk in supporting[:1]
        ]
        if references:
            choices.append({
                "trigger": transition["trigger"],
                "timing": transition.get("timing", ""),
                "proposal": {
                    "kind": "move", "target": target, "status": "visited",
                    "source_reference_ids": references,
                },
            })
    return choices


def parser_guidance(state):
    """Bound parser context to current event notes and chapter running guidance."""
    context = cached_scene_context(state)
    if context is None:
        return ""
    chunks = [
        c
        for c in context.get("planner_context", [])
        if c.get("passage_id", "").endswith(("_dm", "_content"))
        and state.get("current_scene_id") in c.get("scene_ids", [])
    ]
    chunks.sort(key=lambda c: not c.get("passage_id", "").endswith("_dm"))
    text = "\n\n".join(c.get("text", "") for c in chunks)
    if not text:
        return ""
    return (
        "Private reviewed current-scene DM guidance (classification evidence only; never disclose it):\n"
        + text[:8000]
        + "\nRespect source instructions that ordinary questions, visible details and accessible searches need no check. "
        "An unknown answer is not itself uncertainty requiring a roll. Never invent a check to delay an ordinary answer."
    )


def source_object_choices(state):
    """Known source objects, with exact identities; never an executable spell catalog.

    A public current-scene entity binding (or an already carried object) makes an object
    addressable. Merely existing in the private campaign registry does not.
    """
    from agent.nodes.story_effects import _reviewed_chunk
    from agent.resolution import inventory_items, match_inventory_item

    if state.get("source_mode") != "module":
        return []
    context = cached_scene_context(state) or state
    current = state.get("current_scene_id")
    public = [
        c
        for c in context.get("adventure_context", [])
        if c.get("audience") == "player"
        and current in c.get("scene_ids", [c.get("scene_id")])
        and _reviewed_chunk(c, state)
    ]
    items = inventory_items(state.get("character") or {})
    result = []
    for collection, value in (state.get("adventure_manifests") or {}).items():
        try:
            manifest = require_published(value)
        except (ValueError, TypeError):
            continue
        for obj in manifest.objects:
            if current not in obj.scene_ids:
                continue
            entity_ids = {e.entity_id for e in manifest.entities if e.object_id == obj.canonical_id}
            visible = any(
                c.get("collection") == collection
                and entity_ids.intersection(c.get("entity_ids", []))
                for c in public
            )
            if visible or match_inventory_item(items, obj.title):
                result.append({"object_id": obj.canonical_id, "name": obj.title})
    return result


def selected_source_object(state):
    """A grounded object selector never authorizes independent spellcasting or combat."""
    parsed = state.get("parsed_input")
    selected = getattr(parsed, "source_object_id", None)
    if not selected or (state.get("combat_state") and not state["combat_state"].get("ended")):
        return None
    text = state.get("player_input", "")
    # Explicit casting stays under the spell engine even beside an object interaction.
    if re.search(
        r"\b(?:cast(?:s|ing)?|invoke[sd]?|invoking|unleash(?:es|ing)?|conjure[sd]?|conjuring)\b",
        text,
        re.IGNORECASE,
    ):
        return None
    choices = [item for item in source_object_choices(state) if item["object_id"] == selected]
    if len(choices) != 1:
        return None
    choice = choices[0]
    spell = str((getattr(parsed, "entities", {}) or {}).get("spell") or "").strip()
    if (
        spell
        and spell.casefold() not in choice["name"].casefold()
        and spell.casefold() in text.casefold()
    ):
        # A second explicitly named spell beside the object remains under the spell engine.
        return None
    return choice


def active_observation_chunks(state):
    """Only active scene facts; shared campaign registries cannot authorize disclosure."""
    from agent.nodes.story_effects import _reviewed_chunk
    from edgar_core.manifests import select_scene_context
    from edgar_core.sources import eligible_context

    current = state.get("current_scene_id")
    edition = state.get("rules_edition", "2014")
    result = []
    for chunk in eligible_context(
        state.get("adventure_context", []) + state.get("planner_context", []), edition
    ):
        if chunk.get("passage_id") in ("campaign_registry", "campaign_spine"):
            continue
        value = (state.get("adventure_manifests") or {}).get(chunk.get("collection"))
        if (
            value is None
            or current not in chunk.get("scene_ids", [])
            or not _reviewed_chunk(chunk, state)
        ):
            continue
        manifest = require_published(value)
        selected = select_scene_context(manifest, current, state.get("world_flags"))
        if (
            chunk.get("passage_id")
            in selected["player_passage_ids"] + selected["planner_passage_ids"]
        ):
            result.append(chunk)
    return result


def observation_span_catalog(state, chunks=None):
    """Stable local sentence spans, pinned to scene, reviewed source and offsets."""
    from edgar_core.sources import source_citation

    result = {}
    for chunk in active_observation_chunks(state) if chunks is None else chunks:
        reference = source_citation(chunk, state.get("rules_edition", "2014"))["reference_id"]
        text = chunk["text"]
        start = 0
        ends = [m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n{2,}", text)] + [len(text)]
        for end in ends:
            if not text[start:end].strip():
                continue
            identity = json.dumps(
                [state.get("current_scene_id"), reference, start, end], separators=(",", ":")
            )
            span_id = "span_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            result[span_id] = {
                "source_span_id": span_id,
                "reference_id": reference,
                "start": start,
                "end": end,
                "text": text[start:end],
                "method": "selected_span",
            }
            start = end
    return result


def observation_excerpt(chunk, spans, edition="2014"):
    """Annotate existing evidence inline; never duplicate it or rehash altered text."""
    from edgar_core.sources import cited_excerpt, source_citation

    original = cited_excerpt(chunk, edition)
    reference = source_citation(chunk, edition)["reference_id"]
    selected = [span for span in spans.values() if span["reference_id"] == reference]
    if not selected:
        return original
    header = original.split("\n", 1)[0]
    return (
        header
        + "\n"
        + "".join(f"[source_span {span['source_span_id']}]\n{span['text']}" for span in selected)
    )


_QUOTE_PUNCTUATION = {
    **dict.fromkeys("\u2018\u2019\u201a\u201b", "'"),
    **dict.fromkeys("\u201c\u201d\u201e\u201f", '"'),
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-"),
    "\u2026": "...",
}


def _canonical_quote(value):
    """Typography/whitespace only, with each character mapped to its source span."""
    characters, spans = [], []
    for index, char in enumerate(value):
        replacement = " " if char.isspace() else _QUOTE_PUNCTUATION.get(char, char)
        if replacement == " " and characters and characters[-1] == " ":
            spans[-1] = (spans[-1][0], index + 1)
            continue
        for normalized in replacement:
            characters.append(normalized)
            spans.append((index, index + 1))
    start = 1 if characters and characters[0] == " " else 0
    end = len(characters) - (1 if characters and characters[-1] == " " else 0)
    return "".join(characters[start:end]), spans[start:end]


def match_source_quote(text, quote):
    """Return an original contiguous span; never fuzzy-match or paraphrase source words."""
    if not quote.strip():
        return None
    start = text.find(quote)
    if start >= 0:
        return {
            "text": text[start : start + len(quote)],
            "start": start,
            "end": start + len(quote),
            "method": "exact",
        }

    # Some model outputs double-escape formatting. Decode only whitespace and
    # typographic punctuation, never arbitrary character escapes or word content.
    def unescape(match):
        token = match.group(0)
        value = (
            chr(int(token[2:], 16))
            if token.startswith("\\u")
            else {r"\n": "\n", r"\r": "\r", r"\t": "\t", r"\"": '"', r"\'": "'"}[token]
        )
        return value if value.isspace() or value in _QUOTE_PUNCTUATION or value in "\"'" else token

    candidate = re.sub(r"\\(?:u[0-9a-fA-F]{4}|[nrt\"'])", unescape, quote)
    source, spans = _canonical_quote(text)
    target, _ = _canonical_quote(candidate)
    start = source.find(target) if target else -1
    if start < 0:
        return None
    original_start, original_end = spans[start][0], spans[start + len(target) - 1][1]
    return {
        "text": text[original_start:original_end],
        "start": original_start,
        "end": original_end,
        "method": "formatting",
    }


def _unearned_visibility_gated_answer(
    proposal, adjudication, state, evidence, match, selected_chunk
):
    """Apply explicit source visibility qualifiers across duplicate source spans.

    This covers only the authored phrase 'not automatically visible' and a simple
    named subject in the same sentence. It does not infer hidden actors or apply a
    general secrecy classifier to ordinary private-source answers.
    """
    action = state.get("player_input", "")
    checked_search = (
        adjudication.check in {"perception", "investigation"}
        and adjudication.dice_result is not None
        and adjudication.success
        and re.search(r"\b(?:search|investigate|inspect|examine|look\s+inside)\b", action, re.I)
    )
    if checked_search:
        return False
    answer = (proposal.text + " " + match["text"]).casefold()
    for chunk in evidence.values():
        if chunk.get("audience") != "planner" or any(
            chunk.get(key) != selected_chunk.get(key)
            for key in ("collection", "source_sha256", "manifest_sha256")
        ):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n{2,}", chunk.get("text", "")):
            if "not automatically visible" not in sentence.casefold():
                continue
            subject = re.match(
                r"\s*(?:(?:The|A|An)\s+)?([\w'-]+(?:\s+[\w'-]+){0,5}?)\s+(?:is|are)\b",
                sentence,
                re.I,
            )
            if not subject or not re.search(
                r"\b" + re.escape(subject[1].casefold()) + r"\b", answer
            ):
                continue
            # A source-supported sensory procedure can be routine; no invented
            # roll is required merely to listen or smell when the source permits it.
            if (
                re.search(r"\bsound\b", sentence, re.I)
                and re.search(r"\b(?:listen|listening)\b", action, re.I)
            ) or (
                re.search(r"\bsmell\b", sentence, re.I)
                and re.search(r"\b(?:smell|sniff)\b", action, re.I)
            ):
                continue
            return True
    return False


def _action_quote_matches(quote: str, action: str) -> bool:
    # A model can copy dialogue with typographic quotation marks. Preserve exact
    # wording/case/punctuation while treating only straight/curly quotes equally.
    quotes = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
    return bool(quote.strip()) and quote.translate(quotes) in action.translate(quotes)


_OBSERVATION_STATE_CHANGE = re.compile(
    r"\b(?:is|are)\s+(?:now\s+)?no\s+longer\s+(?:present|here|there|alive)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?gone\b"
    r"|\b(?:has|have)\s+(?:now\s+)?(?:disappeared|vanished)\b"
    r"|\b(?:is|are)\s+now\s+(?:gone|dead|defeated|captive|released)\b"
    r"|\b(?:is|are)\s+in\s+(?:your|his|her|their|[\w'-]+['’]s)\s+possession\b",
    re.IGNORECASE,
)


def validate_source_observations(adjudication, state):
    """Seal exact grounded answers chosen by the existing live DM judgment stage.

    Narrative appropriateness is the DM's semantic judgment, as for goal completion.
    Code enforces its boundaries: current source, exact quote/action, successful checked
    inference (or independent public observation), no future/global dossier or state writes.
    """
    from edgar_core.sources import source_citation

    adjudication.accepted_observations = []
    diagnostics = {
        "proposed": len(adjudication.source_observations),
        "accepted": 0,
        "eligible_references": 0,
        "rejected": [],
        "matches": [],
    }
    adjudication.observation_diagnostics = diagnostics
    if (
        state.get("manifest_status") != "reviewed"
        or not state.get("turn_id")
    ):
        diagnostics["blocked"] = "turn_not_eligible"
        return
    evidence = {
        source_citation(c, state.get("rules_edition", "2014"))["reference_id"]: c
        for c in active_observation_chunks(state)
    }
    diagnostics["eligible_references"] = len(evidence)
    spans = observation_span_catalog(state, list(evidence.values()))
    action = state.get("player_input", "")
    for index, proposal in enumerate(adjudication.source_observations):
        chunk = evidence.get(proposal.source_reference_id)
        reason = None
        match = None
        matched_reference = proposal.source_reference_id
        selected_span = spans.get(proposal.source_span_id) if proposal.source_span_id else None
        if proposal.source_span_id and selected_span is None:
            reason = "ineligible_source_span"
        elif not proposal.source_span_id and not chunk:
            reason = "ineligible_source_reference"
        elif not proposal.achievement_basis.strip():
            reason = "missing_achievement_basis"
        elif not _action_quote_matches(proposal.action_quote, action):
            reason = "action_quote_not_exact"
        elif not proposal.text.strip():
            reason = "source_quote_not_exact"
        elif selected_span:
            matched_reference = selected_span["reference_id"]
            match = selected_span
        else:
            quote = proposal.source_quote or proposal.text
            match = match_source_quote(chunk["text"], quote)
            if match is None:
                # The live model can copy a valid chapter quote while naming the
                # event's reference. Repair only that join: same eligible scene,
                # same reviewed identity, exactly one matching original span.
                relocated = [
                    (reference, candidate_match)
                    for reference, candidate in evidence.items()
                    if all(
                        candidate.get(key) == chunk.get(key)
                        for key in ("collection", "source_sha256", "manifest_sha256")
                    )
                    and (candidate_match := match_source_quote(candidate["text"], quote))
                    is not None
                ]
                if len(relocated) == 1:
                    matched_reference, match = relocated[0]
            if match is None:
                reason = "source_quote_not_exact"
        if not reason and not adjudication.success:
            # A failed inference does not erase a mixed request's plainly public facts.
            # Private evidence stays fail-closed even if the model marks it independent.
            if proposal.requires_check_success:
                reason = "failed_check"
            elif evidence[matched_reference].get("audience") != "player":
                reason = "failed_check_private_evidence"
        if not reason and _unearned_visibility_gated_answer(
            proposal, adjudication, state, evidence, match, evidence[matched_reference]
        ):
            reason = "source_visibility_condition_not_met"
        unsupported_state_clause = any(
            _OBSERVATION_STATE_CHANGE.search(clause)
            and match_source_quote(match["text"], clause.strip().rstrip(".;!?")) is None
            for clause in re.split(r"[;.!?]\s*", proposal.text)
        ) if not reason else False
        if unsupported_state_clause:
            # This channel has no authority to mutate the live world. A source
            # span listing rooms cannot remove a fallen actor or transfer an item.
            # The narrator receives actual inventory, accepted object effects and
            # engine actor outcomes separately. An exact authored clue or historical
            # statement may describe possession/absence; an unrelated geometry
            # citation cannot create a newly asserted live state change.
            reason = "state_change_requires_recorded_outcome"
        if reason:
            diagnostics["rejected"].append({"index": index, "reason": reason})
            continue
        if proposal.text not in adjudication.accepted_observations:
            adjudication.accepted_observations.append(proposal.text)
            diagnostics["matches"].append(
                {
                    "index": index,
                    "source_reference_id": matched_reference,
                    "proposed_source_reference_id": proposal.source_reference_id,
                    **({"source_span_id": proposal.source_span_id} if selected_span else {}),
                    **{key: match[key] for key in ("start", "end", "method")},
                }
            )
    diagnostics["accepted"] = len(adjudication.accepted_observations)
    if diagnostics["rejected"] and state.get("session_id"):
        # Operator-only replay receipt. Never put rejected prose in telemetry,
        # logs, metadata, narration or a public response. Recording cannot fail a turn.
        try:
            from apps.api.services.campaign_prep.runtime_evidence import (
                retain_observation_rejection,
            )

            retain_observation_rejection(state, adjudication, evidence)
        except Exception:
            diagnostics["private_capture"] = "unavailable"
