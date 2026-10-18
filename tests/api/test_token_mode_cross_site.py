"""Token mode refuses cross-site simple requests to the nine EDG-052 routes (EDG-160).

EDG-052 found that, in local mode, nine mutating routes accept a cross-site "simple" request (a
bodyless POST, or a form/plain/multipart body) with no CORS preflight. Browsers attach no bearer
token to such a request, so token mode (EDG-065) must refuse it before any handler runs.

These tests need no database: `require_authentication` refuses a missing or non-bearer credential
before it reads a token, and the route handlers never run. Token mode is switched on per test through
the environment, which the auth module reads per request.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.params import File, Form
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

FOREIGN = "https://evil.example.net"

# The nine routes that accept a cross-site simple request (docs/cross-origin-hosting.md, EDG-052).
# A pinned list, cross-checked against the live router below, so a new simple-request route fails
# here until it is added on purpose.
SIMPLE_REQUEST_ROUTES = {
    ("POST", "/api/adventures/{slug}/campaign"),
    ("POST", "/api/adventures/{slug}/cancel"),
    ("POST", "/api/adventures/{slug}/manifest/compile"),
    ("POST", "/api/campaigns/{campaign_id}/end"),
    ("POST", "/api/campaigns/{campaign_id}/leave"),
    ("POST", "/api/campaigns/{campaign_id}/reopen"),
    ("POST", "/api/seed"),
    ("POST", "/api/sessions/{session_id}/intro"),
    ("POST", "/api/adventures/upload"),
    ("POST", "/api/campaigns/{campaign_id}/sources/{slug}/upload"),
}

_PATH_VALUES = {"slug": "sample-module", "campaign_id": "1", "session_id": "1"}

_MULTIPART_BODY = (
    b'--edgar\r\nContent-Disposition: form-data; name="file"; filename="module.pdf"\r\n'
    b"Content-Type: application/pdf\r\n\r\nnot a real pdf\r\n--edgar--\r\n"
)
_MULTIPART_TYPE = "multipart/form-data; boundary=edgar"

# Cross-site simple request bodies a page can send without a preflight.
BODYLESS_SHAPES = [
    ("html form submit", "application/x-www-form-urlencoded", b"confirm=yes"),
    ("text/plain body", "text/plain;charset=UTF-8", b"{}"),
    ("no content type", None, b""),
]
MULTIPART_SHAPES = [("multipart form submit", _MULTIPART_TYPE, _MULTIPART_BODY)]

# Credential headers that must all be refused: absent, a different scheme, and an empty bearer.
CREDENTIAL_VARIANTS = [
    ("no Authorization header", None),
    ("Basic scheme", "Basic dXNlcjpwYXNz"),
    ("empty Bearer value", "Bearer "),
]


def _concrete(path: str) -> str:
    return path.format(**_PATH_VALUES) if "{" in path else path


def _simple_request_routes(app: Any) -> set[tuple[str, str]]:
    """POST routes whose body, if any, is only form or file fields: the cross-site simple surface.

    Derived from the router rather than the pinned list, so the two are compared, not trusted.
    """
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or "POST" not in route.methods:
            continue
        if all(isinstance(param.field_info, (File, Form)) for param in route.dependant.body_params):
            found.add(("POST", route.path))
    return found


@pytest.fixture
def token_mode_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", "1")
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    monkeypatch.delenv("EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE", raising=False)
    return importlib.import_module("apps.api.main").app


@pytest.fixture
async def client(token_mode_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=token_mode_app), base_url="http://test") as c:
        yield c


def test_pinned_simple_request_routes_match_the_live_router(token_mode_app: Any) -> None:
    """The nine routes are exactly the POST routes a browser can send cross-site without a preflight."""
    assert len(SIMPLE_REQUEST_ROUTES) == 10
    derived = _simple_request_routes(token_mode_app)
    assert derived == SIMPLE_REQUEST_ROUTES, (
        f"missing from pin: {sorted(derived - SIMPLE_REQUEST_ROUTES)}; "
        f"no longer simple: {sorted(SIMPLE_REQUEST_ROUTES - derived)}"
    )


def _request_cases() -> list[Any]:
    cases = []
    for method, path in sorted(SIMPLE_REQUEST_ROUTES):
        shapes = MULTIPART_SHAPES if path.endswith("/upload") else BODYLESS_SHAPES
        for shape_label, content_type, body in shapes:
            for cred_label, authorization in CREDENTIAL_VARIANTS:
                cases.append(
                    pytest.param(
                        method,
                        path,
                        content_type,
                        body,
                        authorization,
                        id=f"{path}|{shape_label}|{cred_label}",
                    )
                )
    return cases


@pytest.mark.parametrize(("method", "path", "content_type", "body", "authorization"), _request_cases())
async def test_cross_site_simple_post_is_refused_in_token_mode(
    client: AsyncClient,
    method: str,
    path: str,
    content_type: str | None,
    body: bytes,
    authorization: str | None,
) -> None:
    """A cross-site simple request without a valid bearer token gets 401 and reaches no handler.

    The 401 is raised by the router-level guard, which runs before any route handler, so a 401 here
    is the proof that nothing was written. The response must not expose CORS headers either.
    """
    headers = {"Origin": FOREIGN}
    if content_type is not None:
        headers["content-type"] = content_type
    if authorization is not None:
        headers["Authorization"] = authorization

    response = await client.request(method, _concrete(path), content=body, headers=headers)

    assert response.status_code == 401, response.text
    assert response.headers.get("www-authenticate") == "Bearer"
    assert "access-control-allow-origin" not in response.headers
    # Routes under /api/sessions and /api/adventures use the safe-error payload; the rest keep
    # FastAPI's detail. Both say the same thing: the caller must authenticate.
    body = response.json()
    assert body.get("code") == "authentication_required" or body.get("detail") == "Authentication required."
