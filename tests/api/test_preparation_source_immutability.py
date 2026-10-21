"""Source recovery cannot mutate held archives or frozen graph campaign inputs."""

import io

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from apps.api.routers import adventures
from apps.api.services import adventures_meta, campaign_sources


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [None, "campaign-graph-2.0"])
@pytest.mark.parametrize("private", [False, True])
async def test_source_reupload_preserves_retained_data(tmp_path, monkeypatch, workflow, private):
    directory = tmp_path / "adventures"
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", directory)
    monkeypatch.setattr(adventures, "_ADVENTURES_DIR", directory)
    metadata = {"status": "failed", "private_preparation": private}
    if workflow:
        metadata["preparation_workflow"] = workflow
    adventures_meta.save_meta("retained", metadata)
    path = adventures_meta.meta_path("retained")
    before = path.read_bytes()
    before_files = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))
    monkeypatch.setattr(
        campaign_sources, "source_read", lambda _: pytest.fail("Source recovery was entered")
    )
    uploaded = UploadFile(filename="replacement.pdf", file=io.BytesIO(b"%PDF-1.4 replacement"))
    with pytest.raises(HTTPException) as error:
        await adventures.recover_source_upload("retained", uploaded, BackgroundTasks())
    assert error.value.status_code == 409
    assert "immutable" in error.value.detail
    assert path.read_bytes() == before
    assert sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*")) == before_files
    assert uploaded.file.tell() == 0
