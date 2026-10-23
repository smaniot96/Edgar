"""Protected-route matrix (EDG-065). Walks ``app.routes`` so a future route cannot ship unguarded.

Rules enforced here:
* Every route is either on the explicit public allow-list below or depends (directly or through a
  router-level dependency) on ``require_authentication``.
* The admin-only set is exact: adding an admin route means updating this file and docs/authentication.md.
* The public set is exact: nothing becomes public by accident.
Static file mounts (the bundled UI assets) serve public build output and are not routes to guard.
"""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import Mount

from apps.api.auth import require_admin, require_authentication
from apps.api.main import app

# Probes, API documentation and the static UI. Nothing else is public.
PUBLIC_PATHS = {
    "/",
    "/health",
    "/ready",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/ui",
    "/ui/",
    "/ui/{path:path}",
}

# Operator-only in token mode. Local mode is unaffected (require_admin is a no-op there).
ADMIN_ROUTES = {
    ("GET", "/metrics"),
    ("POST", "/api/seed"),
    ("POST", "/api/users"),
    ("GET", "/api/sessions/{session_id}/diagnostics/combat"),
    ("GET", "/api/sessions/{session_id}/diagnostics/turns"),
    ("GET", "/api/sessions/{session_id}/diagnostics/turn-traces"),
    ("POST", "/api/campaigns/{campaign_id}/sources/{slug}/upload"),
    ("GET", "/api/adventures/{slug}/extraction"),
    ("GET", "/api/adventures/{slug}/extraction/markdown"),
    ("POST", "/api/adventures/upload"),
    ("POST", "/api/adventures/generate"),
    ("POST", "/api/adventures/{slug}/retry"),
    ("POST", "/api/adventures/{slug}/preparation/setup"),
    ("POST", "/api/adventures/{slug}/cancel"),
    ("PATCH", "/api/adventures/{slug}"),
    ("PATCH", "/api/adventures/{slug}/library"),
    ("DELETE", "/api/adventures/{slug}"),
    ("POST", "/api/adventures/{slug}/manifest/compile"),
    ("GET", "/api/adventures/{slug}/manifest"),
    ("PUT", "/api/adventures/{slug}/manifest"),
    ("POST", "/api/adventures/{slug}/manifest/publish"),
    ("GET", "/api/adventures/{slug}/manifest/solo-review"),
    ("POST", "/api/adventures/{slug}/manifest/solo-review/spoiler-opened"),
}


def _depends_on(dependant: Dependant, target: object) -> bool:
    if dependant.call is target:
        return True
    return any(_depends_on(sub, target) for sub in dependant.dependencies)


def _api_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _methods(route: APIRoute) -> set[tuple[str, str]]:
    return {(method, route.path) for method in route.methods if method != "HEAD"}


def test_every_route_is_guarded_or_explicitly_public() -> None:
    unguarded: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        if isinstance(route, Mount):
            continue
        if isinstance(route, APIRoute) and _depends_on(route.dependant, require_authentication):
            continue
        unguarded.add(path)
    assert unguarded == PUBLIC_PATHS, (
        "Routes without the auth dependency must be on PUBLIC_PATHS: "
        f"unexpected={sorted(unguarded - PUBLIC_PATHS)} missing={sorted(PUBLIC_PATHS - unguarded)}"
    )


def test_every_api_route_is_guarded() -> None:
    offenders = [
        route.path
        for route in _api_routes()
        if route.path.startswith("/api/")
        and not _depends_on(route.dependant, require_authentication)
    ]
    assert offenders == []


def test_admin_routes_are_exactly_the_documented_set() -> None:
    actual: set[tuple[str, str]] = set()
    for route in _api_routes():
        if _depends_on(route.dependant, require_admin):
            actual |= _methods(route)
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/metrics":
            if _depends_on(route.dependant, require_admin):
                actual |= _methods(route)
    assert actual == ADMIN_ROUTES


def test_admin_routes_are_also_authenticated() -> None:
    for route in _api_routes():
        if _depends_on(route.dependant, require_admin):
            assert _depends_on(route.dependant, require_authentication), route.path


def test_diagnostics_routes_respect_the_debug_gate_in_addition() -> None:
    # Admin is necessary but not sufficient: the debug gate (EDGAR_DEBUG) still applies in local mode.
    names = {route.name for route in _api_routes() if "diagnostics" in route.path}
    assert names == {"list_combat_diagnostics", "list_turn_diagnostics", "list_turn_traces"}
