"""Player error boundaries must not serialize or log private exception/input text."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from structlog.testing import capture_logs

from apps.api.main import (
    http_error_with_context_handler,
    request_validation_error_handler,
    unhandled_exception_handler,
)
from apps.api.routers._common import HTTPErrorWithContext

SECRET = "PRIVATE-PROMPT-and-future-branch-/private/manuscript-sk-sensitive"


async def test_internal_fault_response_and_log_drop_arbitrary_private_exception(fresh_safe_error_logger):
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/crash")
    async def crash():
        raise TypeError(SECRET)

    with capture_logs() as logs:
        async with AsyncClient(
            transport=ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.get("/crash")
    assert response.status_code == 500
    assert response.json()["category"] == "internal"
    assert response.json()["outcome"] == "unknown"
    assert response.json()["retryable"] is False
    assert response.json()["support_ref"] == logs[-1]["support_ref"]
    assert SECRET not in response.text + json.dumps(logs)
    assert "exception" not in logs[-1]


async def test_validation_response_drops_echoed_input_and_error_context():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)

    class Input(BaseModel):
        number: int

    @app.post("/validate")
    async def validate(body: Input):
        return {"number": body.number}

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/validate", json={"number": SECRET})
    assert response.status_code == 422
    assert response.json()["category"] == "validation"
    assert response.json()["outcome"] == "not_committed"
    assert SECRET not in response.text


async def test_assignment_error_keeps_safe_repair_context_only():
    app = FastAPI()
    app.add_exception_handler(HTTPErrorWithContext, http_error_with_context_handler)

    @app.post("/assign")
    async def assign():
        raise HTTPErrorWithContext(
            409, SECRET, active_campaign_id=42, secret=SECRET, character_id=7
        )

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/assign")
    body = response.json()
    assert response.status_code == 409
    assert body["category"] == "assignment"
    assert body["recovery_action"] == "assign_character"
    assert body["active_campaign_id"] == 42
    assert body["character_id"] == 7
    assert SECRET not in response.text


@pytest.mark.parametrize("provider", [False, True])
async def test_sync_and_stream_preserve_failure_attribution_and_uncommitted_outcome(
    monkeypatch, provider, fresh_safe_error_logger
):
    from openai import APIConnectionError

    from apps.api.routers import session as router
    from apps.api.services import turn_runner

    error = (
        APIConnectionError(message=SECRET, request=httpx.Request("POST", "https://example.invalid"))
        if provider
        else TypeError(SECRET)
    )

    class Lock:
        releases = 0

        async def release(self):
            self.releases += 1

    lock = Lock()

    async def acquire(*args):
        return lock

    async def no_receipt(*args):
        return None

    async def initial(*args):
        return {"campaign_id": 2}

    async def crash(*args):
        raise error

    async def stream_crash(*args, **kwargs):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(router, "acquire_turn_lock", acquire)
    monkeypatch.setattr(router, "_replayed_turn", no_receipt)
    monkeypatch.setattr(router, "_load_turn_initial_state", initial)
    monkeypatch.setattr(router, "run_turn", crash)
    monkeypatch.setattr(turn_runner, "prepare_app", SimpleNamespace(astream=stream_crash))
    from apps.api.schemas.turn import TurnRequest

    with capture_logs() as logs:
        response = await router.session_turn(
            1, TurnRequest(message="Look."), BackgroundTasks(), None, None
        )
        frames = [
            frame
            async for frame in turn_runner.stream_session_turn(
                session_id=1, body_message="Look.", initial_state={}
            )
        ]
    sync = json.loads(response.body)
    stream = json.loads(frames[-1].split("data: ", 1)[1])
    expected = "provider_unavailable" if provider else "internal"
    assert response.status_code == (503 if provider else 500)
    for payload in (sync, stream):
        assert payload["category"] == expected
        assert payload["outcome"] == "not_committed"
        assert payload["retryable"] is provider
        assert payload["support_ref"] in {event["support_ref"] for event in logs}
    assert {k: v for k, v in sync.items() if k != "support_ref"} == {
        k: v for k, v in stream.items() if k != "support_ref"
    }
    assert SECRET not in json.dumps(logs) + response.body.decode() + "".join(frames)
    assert lock.releases == 1


async def test_receipt_not_found_retains_definitive_outcome_and_reference():
    from fastapi import HTTPException

    from apps.api.main import player_http_error_handler

    app = FastAPI()
    app.add_exception_handler(HTTPException, player_http_error_handler)

    @app.get("/api/sessions/1/turns/missing")
    async def missing():
        raise HTTPException(
            404,
            detail={
                "code": "turn_not_found",
                "outcome": "not_committed",
                "support_ref": "err-0123456789abcdef",
            },
        )

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/sessions/1/turns/missing")
    assert response.json()["outcome"] == "not_committed"
    assert response.json()["retryable"] is True
    assert response.json()["support_ref"] == "err-0123456789abcdef"


@pytest.mark.parametrize("code", ["campaign_sources_unavailable", "adventure_manifest_review_required", "turn_identity_conflict"])
async def test_safe_http_boundary_preserves_actionable_codes_and_gate_envelopes(code):
    from fastapi import HTTPException

    from apps.api.main import player_http_error_handler

    app = FastAPI()
    app.add_exception_handler(HTTPException, player_http_error_handler)

    @app.post("/api/sessions/1/turn")
    async def blocked():
        raise HTTPException(409, detail={"code": code, "message": SECRET, "sources": [SECRET]})

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/sessions/1/turn")
    payload = response.json()
    assert response.status_code == 409
    assert payload["code"] == code
    assert payload["outcome"] == "not_committed"
    assert payload["retryable"] is False
    assert SECRET not in response.text
    if code != "turn_identity_conflict":
        assert payload["detail"]["code"] == code
        assert "sources" not in payload["detail"]
    if code == "campaign_sources_unavailable":
        assert payload["detail"]["retrieval_degraded"] is True
