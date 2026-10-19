"""Actual cross-origin request modes against the real app (EDG-052).

The app is rebuilt through `importlib.reload(apps.api.main)` after monkeypatching the config
values, so the import-time wiring (the same path production takes) is what gets exercised. Requests
go through httpx's ASGI transport; no database, Redis server, network or model provider is used.

Findings are recorded in `docs/cross-origin-hosting.md`. The state-changing request-mode table is
test (e) below, and it asserts observed behaviour rather than a claim that CSRF is fixed.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from apps.api.origin_policy import OriginConfigurationError
from edgar_core import config

ALLOWED = "https://ui.example.com"
FOREIGN = "https://evil.example.net"
STREAM = "/api/sessions/1/turn/stream"


@pytest.fixture
def load_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Rebuild the API under the given origin settings, then restore the default afterwards."""

    def load(origins: str = "", credentials: str = "", hosts: str = "") -> Any:
        monkeypatch.setattr(config, "EDGAR_ALLOWED_ORIGINS", origins)
        monkeypatch.setattr(config, "EDGAR_ALLOW_CREDENTIALS", credentials)
        monkeypatch.setattr(config, "EDGAR_TRUSTED_HOSTS", hosts)
        main = importlib.import_module("apps.api.main")
        return importlib.reload(main).app

    yield load
    monkeypatch.undo()
    importlib.reload(importlib.import_module("apps.api.main"))


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


class _FakeDb:
    async def commit(self) -> None:
        return None


class _Replay:
    """Stands in for an already-committed turn so the stream opens without a database or model."""

    def model_dump(self) -> dict[str, Any]:
        return {"turn_id": "replayed-1", "narration": "The door creaks open."}


def _stub_stream_dependencies(app: Any, redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch):
    from apps.api.dependencies import get_redis, get_session
    from apps.api.routers import session as session_router

    async def no_db():
        yield _FakeDb()

    async def fake_redis():
        yield redis

    async def replay(*_a: Any, **_kw: Any):
        return _Replay()

    monkeypatch.setattr(session_router, "_replayed_turn", replay)
    app.dependency_overrides[get_session] = no_db
    app.dependency_overrides[get_redis] = fake_redis


def _client(app: Any, **kwargs: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", **kwargs)


def _preflight(origin: str, method: str = "POST", headers: str = "content-type") -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


# (a) Default configuration: same-origin only, no CORS headers at all.


async def test_default_config_sends_no_cors_headers_to_a_foreign_origin(load_app) -> None:
    app = load_app()
    async with _client(app) as client:
        simple = await client.get("/health", headers={"Origin": FOREIGN})
        preflight = await client.options(STREAM, headers=_preflight(FOREIGN))
        allowed_looking = await client.options(STREAM, headers=_preflight(ALLOWED))
    for response in (simple, preflight, allowed_looking):
        assert "access-control-allow-origin" not in response.headers
        assert not [name for name in response.headers if name.startswith("access-control-")]
    assert simple.status_code == 200
    # Without CORS middleware the preflight is an ordinary OPTIONS, which the route refuses.
    assert preflight.status_code == 405


# (b) Explicit allow-list: exact-origin echo only, never `*`, and nothing for other origins.


async def test_allowed_origin_gets_exact_echo_never_wildcard(load_app) -> None:
    app = load_app(origins=ALLOWED)
    async with _client(app) as client:
        response = await client.get("/health", headers={"Origin": ALLOWED})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-allow-origin"] != "*"
    assert "access-control-allow-credentials" not in response.headers
    assert "Origin" in response.headers["vary"]


@pytest.mark.parametrize(
    "origin",
    [
        FOREIGN,
        "https://ui.example.com:8443",
        "http://ui.example.com",
        "https://sub.ui.example.com",
        "null",
    ],
)
async def test_disallowed_origin_gets_no_allow_origin_header(load_app, origin: str) -> None:
    app = load_app(origins=ALLOWED)
    async with _client(app) as client:
        simple = await client.get("/health", headers={"Origin": origin})
        preflight = await client.options(STREAM, headers=_preflight(origin))
    assert simple.status_code == 200
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers
    assert preflight.status_code == 400
    assert "Origin" in simple.headers["vary"]


async def test_preflight_from_allowed_origin_lists_only_the_policy_methods_and_headers(
    load_app,
) -> None:
    app = load_app(origins=ALLOWED)
    async with _client(app) as client:
        delete = await client.options(
            "/api/campaigns/1", headers=_preflight(ALLOWED, "DELETE", "authorization")
        )
        put = await client.options(STREAM, headers=_preflight(ALLOWED, "PUT"))
        bad_header = await client.options(
            STREAM, headers=_preflight(ALLOWED, "POST", "content-type, x-evil-header")
        )
    assert delete.status_code == 200
    assert delete.headers["access-control-allow-origin"] == ALLOWED
    assert "DELETE" in delete.headers["access-control-allow-methods"]
    assert delete.headers["access-control-max-age"] == "600"
    allowed_headers = {
        h.strip().lower() for h in delete.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type", "accept", "x-request-id"} <= allowed_headers
    assert "x-evil-header" not in allowed_headers

    # A 400 refusal grants nothing: the method and header are never listed as allowed.
    assert put.status_code == 400
    assert "PUT" not in put.headers.get("access-control-allow-methods", "")
    assert bad_header.status_code == 400
    assert "x-evil-header" not in bad_header.headers.get("access-control-allow-headers", "")


async def test_request_id_is_exposed_and_retry_after_is_not(load_app) -> None:
    app = load_app(origins=ALLOWED)
    async with _client(app) as client:
        response = await client.get("/health", headers={"Origin": ALLOWED, "X-Request-ID": "req-1"})
    assert response.headers["x-request-id"] == "req-1"
    exposed = {
        h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert exposed == {"x-request-id"}


async def test_credentials_are_off_by_default_and_opt_in(load_app) -> None:
    off = load_app(origins=ALLOWED)
    async with _client(off) as client:
        response = await client.get("/health", headers={"Origin": ALLOWED})
    assert "access-control-allow-credentials" not in response.headers

    on = load_app(origins=ALLOWED, credentials="1")
    async with _client(on) as client:
        response = await client.get("/health", headers={"Origin": ALLOWED})
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-allow-origin"] == ALLOWED


# (c) The streaming turn route: preflight passes, and the real streamed POST carries the echo.


async def test_stream_preflight_from_allowed_origin_passes(load_app) -> None:
    app = load_app(origins=ALLOWED)
    async with _client(app) as client:
        response = await client.options(
            STREAM, headers=_preflight(ALLOWED, "POST", "content-type, accept, authorization")
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]


async def test_streamed_turn_from_allowed_origin_keeps_sse_and_echo(
    load_app, redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = load_app(origins=ALLOWED)
    _stub_stream_dependencies(app, redis, monkeypatch)
    async with _client(app) as client:
        response = await client.post(
            STREAM,
            json={"message": "I open the door"},
            headers={"Origin": ALLOWED, "Accept": "text/event-stream"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"


async def test_streamed_turn_from_foreign_origin_still_runs_but_is_unreadable(
    load_app, redis: FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORS limits what a browser may READ. It does not stop a request from reaching the server.

    This is the reason CORS is not a CSRF defence on its own (see test (e) and the docs).
    """
    app = load_app(origins=ALLOWED)
    _stub_stream_dependencies(app, redis, monkeypatch)
    async with _client(app) as client:
        response = await client.post(
            STREAM, json={"message": "I open the door"}, headers={"Origin": FOREIGN}
        )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


# (d) Startup validation: a bad value stops the import before the app is served.


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("EDGAR_ALLOWED_ORIGINS", "*"),
        ("EDGAR_ALLOWED_ORIGINS", "https://ui.example.com,https://*.example.com"),
        ("EDGAR_ALLOWED_ORIGINS", "ui.example.com"),
        ("EDGAR_ALLOWED_ORIGINS", "ftp://ui.example.com"),
        ("EDGAR_ALLOWED_ORIGINS", "https://ui.example.com/app"),
        ("EDGAR_ALLOWED_ORIGINS", "null"),
        ("EDGAR_ALLOW_CREDENTIALS", "true"),
        ("EDGAR_TRUSTED_HOSTS", "*"),
        ("EDGAR_TRUSTED_HOSTS", "https://edgar.example.com"),
    ],
)
def test_bad_startup_value_fails_import_naming_only_the_setting(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    values = {
        "EDGAR_ALLOWED_ORIGINS": config.EDGAR_ALLOWED_ORIGINS,
        "EDGAR_ALLOW_CREDENTIALS": config.EDGAR_ALLOW_CREDENTIALS,
        "EDGAR_TRUSTED_HOSTS": config.EDGAR_TRUSTED_HOSTS,
    }
    values[setting] = value
    monkeypatch.setattr(config, "EDGAR_ALLOWED_ORIGINS", values["EDGAR_ALLOWED_ORIGINS"])
    monkeypatch.setattr(config, "EDGAR_ALLOW_CREDENTIALS", values["EDGAR_ALLOW_CREDENTIALS"])
    monkeypatch.setattr(config, "EDGAR_TRUSTED_HOSTS", values["EDGAR_TRUSTED_HOSTS"])
    main = importlib.import_module("apps.api.main")
    try:
        with pytest.raises(OriginConfigurationError) as info:
            importlib.reload(main)
        assert setting in str(info.value)
        assert value not in str(info.value)
    finally:
        monkeypatch.undo()
        importlib.reload(main)


def test_credentials_without_origins_fails_startup(load_app) -> None:
    with pytest.raises(OriginConfigurationError, match="EDGAR_ALLOW_CREDENTIALS=1 requires"):
        load_app(credentials="1")


# Trusted hosts (optional). Default unset behaviour is covered by test (a).


async def test_trusted_hosts_reject_unknown_host_header(load_app) -> None:
    app = load_app(origins=ALLOWED, hosts="edgar.example.com")
    async with _client(app) as client:
        known = await client.get(
            "/health", headers={"Host": "edgar.example.com", "Origin": ALLOWED}
        )
        unknown = await client.get("/health", headers={"Host": "attacker.example.net"})
    assert known.status_code == 200
    assert known.headers["access-control-allow-origin"] == ALLOWED
    assert unknown.status_code == 400


# (e) State-changing request modes without authentication (local mode).
#
# A browser can send a cross-site request without a CORS preflight only when it is a "simple"
# request: POST with a CORS-safelisted Content-Type (or none), or a form submit. CORS only decides
# whether the page may READ the response. These cases pin what the server does with each simple
# body shape on the streaming turn route, which is the same route a JSON client uses.


@pytest.mark.parametrize(
    ("label", "headers", "content", "expected_status"),
    [
        (
            "text/plain (what fetch/sendBeacon send for a string body)",
            {"content-type": "text/plain;charset=UTF-8"},
            b'{"message": "I open the door"}',
            422,
        ),
        (
            "application/x-www-form-urlencoded (HTML form submit)",
            {"content-type": "application/x-www-form-urlencoded"},
            b"message=I+open+the+door",
            422,
        ),
        (
            "multipart/form-data (HTML form submit)",
            {"content-type": "multipart/form-data; boundary=edgar"},
            b'--edgar\r\nContent-Disposition: form-data; name="message"\r\n\r\nI open\r\n--edgar--\r\n',
            422,
        ),
        (
            "application/json (not simple: browser needs a preflight, so cross-site it is blocked)",
            {"content-type": "application/json"},
            b'{"message": "I open the door"}',
            404,
        ),
        (
            "no Content-Type header (e.g. fetch with a Blob of empty type): simple, no preflight",
            {},
            b'{"message": "I open the door"}',
            422,
        ),
    ],
)
async def test_simple_request_body_shapes_on_the_turn_route(
    load_app,
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    headers: dict[str, str],
    content: bytes,
    expected_status: int,
) -> None:
    """Observed behaviour, not a claim of protection.

    422: FastAPI refuses the body before the handler runs, so the simple request cannot reach the
    JSON handler. 404: the body parsed as JSON and the handler ran; the stub raises before any
    write, so the test shows the route was reached (only a `application/json` request, which a
    browser cannot send cross-site without a preflight that the default policy refuses).
    """
    from apps.api.routers import session as session_router

    app = load_app()  # default same-origin configuration: no CORS at all
    _stub_stream_dependencies(app, redis, monkeypatch)

    async def no_replay(*_a: Any, **_kw: Any):
        return None

    async def missing_session(*_a: Any, **_kw: Any):
        raise HTTPException(status_code=404, detail={"code": "not_found"})

    monkeypatch.setattr(session_router, "_replayed_turn", no_replay)
    monkeypatch.setattr(session_router, "_load_turn_initial_state", missing_session)

    async with _client(app) as client:
        response = await client.post(STREAM, content=content, headers=headers)
    assert response.status_code == expected_status, label
    assert "access-control-allow-origin" not in response.headers


# The request-mode table for every mutating route, pinned. A new route must be placed in one of these
# sets on purpose. Simple-request surface = routes a cross-site browser can call with no preflight
# (form/plain/multipart or no body). DELETE/PUT/PATCH need a preflight, which the default policy refuses.
JSON_BODY_ROUTES = {
    ("PATCH", "/api/adventures/{slug}"),
    ("PATCH", "/api/campaigns/{campaign_id}"),
    ("PATCH", "/api/campaigns/{campaign_id}/characters/{character_id}"),
    ("PATCH", "/api/campaigns/{campaign_id}/members/{user_id}"),
    ("PATCH", "/api/campaigns/{campaign_id}/world-flags/{key}"),
    ("PATCH", "/api/characters/{character_id}"),
    ("PATCH", "/api/npcs/{npc_id}"),
    ("PATCH", "/api/sessions/{session_id}"),
    ("PATCH", "/api/users/me"),
    ("POST", "/api/adventures/generate"),
    ("POST", "/api/adventures/{slug}/manifest/publish"),
    ("POST", "/api/adventures/{slug}/manifest/solo-review/spoiler-opened"),
    ("POST", "/api/adventures/{slug}/preparation/setup"),
    ("POST", "/api/adventures/{slug}/retry"),
    ("POST", "/api/campaigns"),
    ("POST", "/api/campaigns/{campaign_id}/characters"),
    ("POST", "/api/campaigns/{campaign_id}/invites"),
    ("POST", "/api/campaigns/{campaign_id}/characters/{character_id}/level-up"),
    ("POST", "/api/campaigns/{campaign_id}/npcs"),
    ("POST", "/api/campaigns/{campaign_id}/world-flags"),
    ("POST", "/api/characters"),
    ("POST", "/api/sessions"),
    ("POST", "/api/sessions/{session_id}/presence"),
    ("POST", "/api/sessions/{session_id}/turn"),
    ("POST", "/api/invites/accept"),
    ("POST", "/api/sessions/{session_id}/turn/stream"),
    ("POST", "/api/users"),
    ("PUT", "/api/adventures/{slug}/manifest"),
}
BODYLESS_POST_ROUTES = {
    ("POST", "/api/adventures/{slug}/campaign"),
    ("POST", "/api/adventures/{slug}/cancel"),
    ("POST", "/api/adventures/{slug}/manifest/compile"),
    ("POST", "/api/campaigns/{campaign_id}/end"),
    ("POST", "/api/campaigns/{campaign_id}/leave"),
    ("POST", "/api/campaigns/{campaign_id}/reopen"),
    ("POST", "/api/seed"),
    ("POST", "/api/sessions/{session_id}/intro"),
}
MULTIPART_ROUTES = {
    ("POST", "/api/adventures/upload"),
    ("POST", "/api/campaigns/{campaign_id}/sources/{slug}/upload"),
}
DELETE_ROUTES = {
    ("DELETE", "/api/adventures/{slug}"),
    ("DELETE", "/api/campaigns/{campaign_id}"),
    ("DELETE", "/api/campaigns/{campaign_id}/characters/{character_id}"),
    ("DELETE", "/api/campaigns/{campaign_id}/invites/{invite_id}"),
    ("DELETE", "/api/campaigns/{campaign_id}/members/{user_id}"),
    ("DELETE", "/api/campaigns/{campaign_id}/world-flags/{key}"),
    ("DELETE", "/api/characters/{character_id}"),
    ("DELETE", "/api/npcs/{npc_id}"),
    ("DELETE", "/api/sessions/{session_id}"),
}


def _mutating_routes(app: Any) -> set[tuple[str, str]]:
    from fastapi.routing import APIRoute

    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_every_mutating_route_is_classified_in_the_request_mode_table(load_app) -> None:
    """A new state-changing route fails here until it is placed in a request-mode set."""
    app = load_app()
    classified = JSON_BODY_ROUTES | BODYLESS_POST_ROUTES | MULTIPART_ROUTES | DELETE_ROUTES
    assert _mutating_routes(app) == classified
    assert len(classified) == 46
    simple_surface = BODYLESS_POST_ROUTES | MULTIPART_ROUTES
    assert len(simple_surface) == 10


async def test_simple_form_post_reaches_a_bodyless_route_without_a_content_type_check() -> None:
    """Framework behaviour: a bodyless POST route does not inspect Content-Type at all.

    The real bodyless routes are not executed here (their handlers write data). A minimal app with
    the same shape shows what a cross-site HTML form can do in local mode: it runs the handler.
    """
    from fastapi import FastAPI

    hits: list[str] = []
    mini = FastAPI()

    @mini.post("/bodyless")
    def bodyless() -> dict[str, str]:
        hits.append("bodyless")
        return {"status": "ok"}

    async with _client(mini) as client:
        response = await client.post(
            "/bodyless",
            content=b"a=1",
            headers={"content-type": "application/x-www-form-urlencoded", "Origin": FOREIGN},
        )
    assert response.status_code == 200
    assert hits == ["bodyless"]
    assert "access-control-allow-origin" not in response.headers
