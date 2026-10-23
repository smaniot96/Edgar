"""Apply exact prose corrections without reviving superseded guidance or dropping its context."""

from __future__ import annotations

from collections import defaultdict

from pydantic import Field

from edgar_core.campaign_semantics import FieldEdit, Record

from .store import NeedsAttention, digest


class InvalidFieldEdits(NeedsAttention):
    def __init__(self, record_id, field, message):
        super().__init__(message)
        self.record_id, self.field = record_id, field


class OverlappingFieldEdits(InvalidFieldEdits):
    def __init__(self, record_id, field):
        super().__init__(record_id, field, "Exact text repair contains overlapping replacements.")


class FieldEditBatch(Record):
    field_edits: list[FieldEdit] = Field(min_length=1)


async def recover_field_edits(registry, bundles, edits, allowed, semantic, store):
    """Correct an invalid field once, preserving every unrelated proposed edit.

    The raw paid patch remains immutable. Every attempted consolidation has a durable
    semantic receipt, is checked against the original field, and still needs review.
    """
    pending, corrected = list(edits), set()
    while True:
        try:
            return apply_field_edits(registry, bundles, pending, allowed)
        except InvalidFieldEdits as exc:
            group = (exc.record_id, exc.field)
            if group in corrected:
                raise NeedsAttention(
                    "Corrected exact text repair remains invalid; retained candidate requires attention."
                ) from exc
            selected = [row for row in pending if (row.record_id, row.field) == group]
            evidence = {
                "record_id": exc.record_id,
                "field": exc.field,
                "original_text": registry[exc.record_id]["value"].get(exc.field, ""),
                "proposed_edits": [row.model_dump() for row in selected],
            }
            identity = digest(evidence)
            prompt = "field-edit-overlap" if isinstance(exc, OverlappingFieldEdits) else "field-edit-rebase"
            result = await semantic.call(
                prompt + "-" + identity[:16], prompt,
                FieldEditBatch, evidence,
            )
            if not result.field_edits or any(
                (row.record_id, row.field) != group for row in result.field_edits
            ):
                raise NeedsAttention("Consolidation tried to change another record or field.") from exc
            store.put("field-edit-consolidation-" + identity, {
                **evidence, "consolidated_edits": [row.model_dump() for row in result.field_edits],
            })
            corrected.add(group)
            pending = [row for row in pending if (row.record_id, row.field) != group]
            pending.extend(result.field_edits)


def apply_field_edits(registry, bundles, edits, allowed):
    """Validate every edit before applying any; preserve original text in the repair receipt."""
    grouped = defaultdict(list)
    targets = {}
    for edit in edits:
        key, field = edit.record_id, edit.field
        if key not in allowed or key not in registry:
            raise NeedsAttention("Exact text repair tried to change a record outside its closure.")
        record = registry[key]
        required_kind = "chapter" if field == "source_notes" else "event"
        if record["kind"] != required_kind:
            raise NeedsAttention("Exact text repair field does not belong to that record kind.")
        if required_kind == "chapter":
            target = next(
                bundle for bundle in bundles["chapters"]
                if bundle["chapter_id"] == record["value"]["chapter_id"]
            )
        else:
            target = next(
                event for bundle in bundles["chapters"] for event in bundle["events"]
                if event["id"] == key
            )
        original = record["value"].get(field, "")
        if target.get(field, "") != original:
            raise NeedsAttention("Do not mix an exact text edit with a replacement of that field.")
        start = original.find(edit.old_text)
        if start < 0:
            raise InvalidFieldEdits(
                key, field, "Exact text repair substring was not found in the accepted field."
            )
        if original.find(edit.old_text, start + 1) >= 0:
            raise InvalidFieldEdits(
                key, field, "Exact text repair substring is ambiguous in the accepted field."
            )
        end = start + len(edit.old_text)
        group = (key, field)
        if any(start < prior_end and prior_start < end for prior_start, prior_end, _ in grouped[group]):
            raise OverlappingFieldEdits(key, field)
        grouped[group].append((start, end, edit))
        targets[group] = target

    prepared, receipts = [], []
    for (key, field), spans in grouped.items():
        original = registry[key]["value"].get(field, "")
        updated = original
        for start, end, edit in sorted(spans, key=lambda span: span[0], reverse=True):
            updated = updated[:start] + edit.new_text + updated[end:]
        prepared.append((targets[(key, field)], field, updated))
        receipts.append({
            "record_id": key,
            "field": field,
            "original_text": original,
            "original_sha256": digest(original),
            "updated_sha256": digest(updated),
            "edits": [edit.model_dump() for _, _, edit in sorted(spans, key=lambda span: span[0])],
        })
    for target, field, updated in prepared:
        target[field] = updated
    return receipts
