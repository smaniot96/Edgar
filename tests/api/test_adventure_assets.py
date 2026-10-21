"""Retained-asset access and session-safe projection checks; no generation paths."""

import hashlib
import io

import pytest

from apps.api.services import adventure_assets as assets
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


@pytest.mark.asyncio
async def test_map_svg_rejects_a_projection_changed_since_the_json_read(monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from fastapi import HTTPException, Response

    from apps.api.routers import session_assets

    async def context(*_):
        return {
            "session": SimpleNamespace(id=12, current_scene_id=None),
            "campaign": SimpleNamespace(id=9),
            "manifests": {},
            "facts": [],
        }

    monkeypatch.setattr(session_assets, "_session_map_context", context)
    version = hashlib.sha256(b"edgar-map-v1:12:9").hexdigest()
    response = await session_assets.get_session_map_svg(12, None, Response(), db=None, v=version)
    assert response.headers["Cache-Control"] == "private, no-store"
    assert b"No known places" in response.body
    with pytest.raises(HTTPException) as error:
        await session_assets.get_session_map_svg(12, None, Response(), db=None, v="a" * 64)
    assert error.value.status_code == 409


def test_player_asset_alias_is_session_scoped():
    assert assets._public_asset_id(1, "schematic-map") != assets._public_asset_id(
        2, "schematic-map"
    )


def test_handout_assets_require_player_passage_knowledge_from_the_same_source_version(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    manifest = AdventureManifest.model_validate(
        {
            "source": "synthetic",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "harbor",
            "scenes": [{"scene_id": "harbor", "title": "The harbor"}],
            "passages": [
                {
                    "passage_id": "same_passage_id",
                    "page": 1,
                    "start": 0,
                    "end": 21,
                    "scene_ids": ["harbor"],
                    "audience": "player",
                }
            ],
            "reviewed_by": "test",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    content = b"An exact player handout."
    record = assets.AssetArtifactRecord(
        artifact_id="handout_one",
        plan_asset_id="handout_one",
        job_key="deterministic",
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        extraction_sha256=manifest.extraction_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        prepared_sha256="c" * 64,
        kind="handout",
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain; charset=utf-8",
        passage_ids=["same_passage_id"],
        relative_file="handout_one.txt",
    )
    private_content = b'<svg xmlns="http://www.w3.org/2000/svg"><text>DM secret</text></svg>'
    private_map = record.model_copy(
        update={
            "artifact_id": "private_map",
            "kind": "schematic_map",
            "passage_ids": [],
            "scene_ids": ["harbor"],
            "relative_file": "private_map.svg",
            "sha256": hashlib.sha256(private_content).hexdigest(),
        }
    )
    unsigned_visual = record.model_copy(
        update={
            "artifact_id": "unsigned_visual",
            "kind": "source_map",
            "scene_ids": ["harbor"],
            "passage_ids": [],
            "relative_file": "unsigned_visual.png",
            "media_type": "image/png",
        }
    )
    empty_gate = unsigned_visual.model_copy(
        update={
            "artifact_id": "empty_gate",
            "scene_ids": [],
            "passage_ids": [],
            "entity_id": None,
            "relative_file": "empty_gate.png",
        }
    )
    version_dir = assets._version_dir("synthetic", manifest.source_sha256, manifest.reviewed_sha256)
    version_dir.mkdir(parents=True)
    (version_dir / record.relative_file).write_bytes(content)
    (version_dir / private_map.relative_file).write_bytes(private_content)
    assets._catalog_path("synthetic", manifest.source_sha256, manifest.reviewed_sha256).write_bytes(
        assets.AssetCatalog(
            entries={
                record.artifact_id: record,
                private_map.artifact_id: private_map,
                unsigned_visual.artifact_id: unsigned_visual,
                empty_gate.artifact_id: empty_gate,
            }
        )
        .model_dump_json()
        .encode(),
    )

    target = StoryRef(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        kind="scene",
        canonical_id="harbor",
    )

    def fact_from(source_collection: str) -> StoryFact:
        source_ref = {
            "reference_id": "d" * 64,
            "audience": "player",
            "collection": source_collection,
            "source_sha256": manifest.source_sha256,
            "manifest_sha256": manifest.reviewed_sha256,
            "passage_id": "same_passage_id",
        }
        effect = accept_story_effect(
            StoryEffect(kind="learn_place", target=target, status="known"),
            turn_id="turn-one",
            source_refs=[source_ref],
            display_name="The harbor",
        )
        return StoryFact(**effect.model_dump(), fact_id=story_target_id(target), event_id=1)

    wrong_binding = fact_from("other-collection")
    assert (
        assets.authorized_player_assets(
            session_id=7, collection="synthetic", manifest=manifest, facts=[wrong_binding]
        )
        == []
    )
    correct_binding = fact_from("synthetic")
    assert (
        len(
            assets.authorized_player_assets(
                session_id=7, collection="synthetic", manifest=manifest, facts=[correct_binding]
            )
        )
        == 1
    )


def test_raster_sanitization_removes_exif_and_rejects_invalid_bytes():
    from PIL import Image

    source = Image.new("RGB", (3, 2), color=(40, 50, 60))
    exif = Image.Exif()
    exif[0x010E] = "private scanner annotation"
    raw = io.BytesIO()
    source.save(raw, format="JPEG", exif=exif)
    sanitized = assets._sanitize_raster(raw.getvalue(), "image/jpeg")
    with Image.open(io.BytesIO(sanitized)) as retained:
        assert retained.format == "PNG"
        assert not retained.getexif()
        assert "exif" not in retained.info
    with pytest.raises(assets.AssetServiceError, match="validation"):
        assets._sanitize_raster(b"not an image", "image/png")
