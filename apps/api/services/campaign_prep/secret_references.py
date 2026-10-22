"""Source-grounded, retained corrections of secret-to-chapter joins only."""

from copy import deepcopy
from typing import Literal

from pydantic import Field

from edgar_core.campaign_semantics import Record
from edgar_core.identifiers import Identifier

from .source import evidence_pages
from .store import NeedsAttention, digest

SECRET_REFERENCE_POLICY = "secret-chapter-membership-1"


class SecretCorrectionRejected(NeedsAttention):
    """A retained correction was locally rejected, rather than awaiting a provider."""


class SecretChapterJoin(Record):
    owner: str = Field(description="spine, or the exact owning chapter_id")
    index: int = Field(ge=0)
    expected_id: Identifier
    key: Literal["setup_chapters", "reveal_chapters"]
    chapter_ids: list[Identifier]
    rationale: str = Field(min_length=1)


class SecretChapterJoins(Record):
    joins: list[SecretChapterJoin]


def chapter_reference_defects(spine, bundles=None):
    chapters = {row["id"] for row in spine["chapters"]}
    owners = [("spine", spine["secrets"])] + [
        (bundle["chapter_id"], bundle["new_secrets"])
        for bundle in (bundles or {}).get("chapters", [])
    ]
    return [
        {
            "owner": owner,
            "index": index,
            "expected_id": row["id"],
            "key": key,
            "invalid_chapter_ids": sorted(set(row[key]) - chapters),
            "record": row,
        }
        for owner, secrets in owners
        for index, row in enumerate(secrets)
        for key in ("setup_chapters", "reveal_chapters")
        if set(row[key]) - chapters
    ]


async def reconcile_secret_chapters(workflow, spine, bundles=None):
    """Retain paid authoring and correct only diagnosed invalid chapter lists.

    Called immediately after new spine authoring, and inside retained structural
    recovery before atomic final validation. Never edits a sealed publication.
    """
    defects = chapter_reference_defects(spine, bundles)
    if not defects:
        return spine, bundles
    signature = digest(
        {"policy": SECRET_REFERENCE_POLICY, "spine": spine, "bundles": bundles, "defects": defects}
    )
    key = "secret-chapter-repair-" + signature
    receipt = workflow.store.get(key) or {}
    if receipt.get("state") == "rejected":
        raise SecretCorrectionRejected(
            "Retained secret chapter correction needs attention: " + receipt["error"]
        )
    source = workflow.store.get("source")
    pages = sorted({p for d in defects for p in d["record"]["origin"]["pages"]})
    if not pages:
        # A sparse planner may omit secret provenance. Retain actual original
        # chapter evidence rather than guessing the intended chapter from an ID.
        referenced = {
            ref
            for d in defects
            for ref in (d["record"]["setup_chapters"] + d["record"]["reveal_chapters"])
        }
        pages = sorted(
            {
                p
                for chapter in spine["chapters"]
                if chapter["id"] in referenced
                for p in chapter["pages"]
            }
        )
    evidence = {
        "policy": SECRET_REFERENCE_POLICY,
        "defects": defects,
        "chapters": spine["chapters"],
        "event_index": [
            {
                "chapter_id": b["chapter_id"],
                "events": [
                    {k: event[k] for k in ("id", "title", "trigger")} for event in b["events"]
                ],
            }
            for b in (bundles or {}).get("chapters", [])
        ],
        "source_evidence": evidence_pages(source, pages),
    }
    raw = receipt.get("candidate")
    if raw is None:
        result = await workflow.semantic.call(
            "secret-chapter-joins-" + signature,
            "secret-chapter-joins",
            SecretChapterJoins,
            evidence,
            workflow.source_images(pages),
        )
        raw = result.model_dump()
        receipt = {
            "policy": SECRET_REFERENCE_POLICY,
            "candidate": raw,
            "original_spine_sha256": digest(spine),
            "original_bundles_sha256": digest(bundles),
            "defects": defects,
        }
        workflow.store.put(key, receipt)
    fixed_spine, fixed_bundles = deepcopy(spine), deepcopy(bundles)
    owners = {
        "spine": fixed_spine["secrets"],
        **{b["chapter_id"]: b["new_secrets"] for b in (fixed_bundles or {}).get("chapters", [])},
    }
    allowed = {(d["owner"], d["index"], d["expected_id"], d["key"]) for d in defects}
    chapters = {c["id"] for c in spine["chapters"]}
    try:
        seen = set()
        for join in SecretChapterJoins.model_validate(raw).joins:
            identity = join.owner, join.index, join.expected_id, join.key
            if identity not in allowed or identity in seen:
                raise NeedsAttention("Secret correction targeted an unaffected or repeated join.")
            seen.add(identity)
            row = owners[join.owner][join.index]
            valid_original = set(row[join.key]) & chapters
            if (
                not join.chapter_ids
                or not set(join.chapter_ids) <= chapters
                or not valid_original <= set(join.chapter_ids)
            ):
                raise NeedsAttention(
                    "Secret correction must retain valid chapters and name existing chapters."
                )
            row[join.key] = list(dict.fromkeys(join.chapter_ids))
        if seen != allowed or chapter_reference_defects(fixed_spine, fixed_bundles):
            raise NeedsAttention("Secret correction left diagnosed chapter references unresolved.")
    except (ValueError, KeyError, IndexError) as exc:
        workflow.store.put(key, {**receipt, "state": "rejected", "error": str(exc)})
        raise SecretCorrectionRejected(
            "Secret chapter correction was rejected: " + str(exc)
        ) from exc
    workflow.store.put(
        key,
        {
            **receipt,
            "state": "accepted",
            "result_spine_sha256": digest(fixed_spine),
            "result_bundles_sha256": digest(fixed_bundles),
        },
    )
    return fixed_spine, fixed_bundles
