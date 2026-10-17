"""FastAPI application entrypoint.

Wires the routers, serves the React UI at /ui when ``frontend/dist`` is present, attaches a
request-id middleware so structured logs and 500 responses share a correlation id, and configures
structlog at startup.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from edgar_core.config import API_TITLE, API_VERSION
from edgar_core.model_profiles import ModelProfileError, validate_provider_configuration

from .limits import (
    LimitRejected,
    UploadSizeLimitMiddleware,
    limit_rejection_response,
    validate_limit_configuration,
)
from .logging_config import RequestContextMiddleware, configure_logging, request_id_from_header
from .origin_policy import (
    apply_origin_policy,
    origin_policy_from_config,
    origin_policy_summary,
)
from .routers._common import HTTPErrorWithContext
from .routers.adventure_manifests import router as adventure_manifests_router
from .routers.adventures import router as adventures_router
from .routers.campaign import router as campaign_router
from .routers.campaign_characters import router as campaign_characters_router
from .routers.character import router as character_router
from .routers.character_presets import router as character_presets_router
from .routers.health import router as health_router
from .routers.journal import router as journal_router
from .routers.limits import router as limits_router
from .routers.metrics import router as metrics_router
from .routers.npc import router as npc_router
from .routers.rules import router as rules_router
from .routers.seed import router as seed_router
from .routers.session import router as session_router
from .routers.session_assets import router as session_assets_router
from .routers.user import router as user_router
from .routers.world_flag import router as world_flag_router
from .services.safe_errors import classify_error, classify_http_failure, log_failure

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Fail before any background work when local provider settings are unusable.
    try:
        provider_settings = validate_provider_configuration()
    except ModelProfileError as exc:
        # Startup aborts; this event is the structured alert signal. The message names the faulty
        # setting but never its value, and the error class is the only field logged.
        log.error("provider_configuration_invalid", error_class=type(exc).__name__)
        raise
    log.info("provider_configuration_validated", **provider_settings)
    log.info("limits_configured", **validate_limit_configuration())
    log.info("origin_policy_configured", **origin_policy_summary(_ORIGIN_POLICY))
    # Background ingest/generation tasks die with the process; flag the ones a previous run
    # left in "processing" as failed so the UI can offer a retry instead of spinning forever.
    from .services.adventures_meta import fail_stale_processing

    try:
        stale = await run_in_threadpool(fail_stale_processing)
        if stale:
            log.warning("adventures_marked_failed_after_restart", slugs=stale)
        from .services.preparation_runtime import recover_private_preparations

        await recover_private_preparations()
    except OSError:
        log.exception("adventures_stale_scan_failed")
    try:
        yield
    finally:
        # Lazy imports keep unrelated tests independent of Redis/engine initialization.
        from db.postgres import session as db_session_module

        from .dependencies import close_redis

        try:
            await close_redis()
        finally:
            await db_session_module.engine.dispose()


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(UploadSizeLimitMiddleware)
# EDG-052: fail closed at import on a malformed origin policy. Added last so CORS and the
# trusted-host check are the outermost layers. Same-origin default installs nothing.
_ORIGIN_POLICY = origin_policy_from_config()
apply_origin_policy(app, _ORIGIN_POLICY)


@app.exception_handler(HTTPErrorWithContext)
async def http_error_with_context_handler(request: Request, exc: HTTPErrorWithContext):
    """Keep assignment context without copying arbitrary exception details."""
    failure = classify_http_failure(
        exc.status_code,
        code="character_assignment_conflict" if "active_campaign_id" in exc.context else None,
        outcome="not_committed",
    )
    context = {
        key: value
        for key, value in exc.context.items()
        if key in {"active_campaign_id", "character_id"} and type(value) is int and value > 0
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={**failure.to_payload(), **context},
        headers=exc.headers,
    )


@app.exception_handler(LimitRejected)
async def limit_rejected_handler(request: Request, exc: LimitRejected):
    """Refused before any paid or heavy work: the shared safe payload with a Retry-After hint."""
    return limit_rejection_response(exc)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    """Pydantic's input/context fields can contain private player text or credentials."""
    failure = classify_error("validation_failed", outcome="not_committed")
    return JSONResponse(status_code=422, content=failure.to_payload())


@app.exception_handler(HTTPException)
async def player_http_error_handler(request: Request, exc: HTTPException):
    """Use the same error boundary for preparation and session preflight failures."""
    if not request.url.path.startswith(("/api/sessions", "/api/adventures")):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    failure = classify_http_failure(
        exc.status_code,
        code=detail.get("code"),
        outcome=detail.get("outcome", "unknown"),
        support_ref=detail.get("support_ref"),
    )
    # A fixed gate can prove rejection; a generic server/transport failure cannot.
    if failure.category in {"source", "review", "validation", "assignment", "not_found"}:
        failure = classify_http_failure(
            exc.status_code,
            code=detail.get("code"),
            outcome="not_committed",
            support_ref=failure.support_ref,
        )
    from .services.safe_errors import http_failure_payload

    return JSONResponse(status_code=exc.status_code, content=http_failure_payload(failure, context=detail))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler so 500s carry a request_id and never leak internals.

    Only reached for exceptions nobody else handled. A programming fault stays internal;
    no exception message, traceback chain, or request path enters the covered log event.
    """
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(
        request.headers.get("x-request-id")
    )
    failure = classify_error("internal_fault")
    log_failure("unhandled", failure)
    return JSONResponse(
        status_code=500,
        content={
            **failure.to_payload(),
            "type": "internal",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(journal_router, prefix="/api")
app.include_router(limits_router, prefix="/api")
app.include_router(adventures_router, prefix="/api")
app.include_router(adventure_manifests_router, prefix="/api")
app.include_router(campaign_router, prefix="/api")
app.include_router(campaign_characters_router, prefix="/api")
app.include_router(character_router, prefix="/api")
app.include_router(character_presets_router, prefix="/api")
app.include_router(npc_router, prefix="/api")
app.include_router(rules_router, prefix="/api")
app.include_router(seed_router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(session_assets_router, prefix="/api")
app.include_router(user_router, prefix="/api")
app.include_router(world_flag_router, prefix="/api")


def _frontend_dist_dir() -> Path | None:
    """Locate ``frontend/dist/index.html`` by walking upward from this module (monorepo / installs)."""
    start = Path(__file__).resolve()
    for anchor in (start.parent, *start.parents):
        candidate = anchor / "frontend" / "dist"
        if (candidate / "index.html").is_file():
            return candidate
    return None


_frontend_dist = _frontend_dist_dir()

def mount_frontend_routes(api: FastAPI, frontend_dist: Path | None) -> None:
    """Mount the same-origin Vite bundle and nested SPA routes on an ASGI app."""
    if frontend_dist is None:
        @api.get("/ui", include_in_schema=False)
        @api.get("/ui/", include_in_schema=False)
        @api.get("/ui/{path:path}", include_in_schema=False)
        async def ui_unavailable() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Frontend bundle not found. Run `make frontend-build` from the repo root "
                        "or `npm run build` in `frontend/`."
                    )
                },
            )

        return

    api.mount(
        "/ui/assets",
        StaticFiles(directory=str(frontend_dist / "assets")),
        name="ui-assets",
    )

    @api.get("/ui", include_in_schema=False)
    @api.get("/ui/", include_in_schema=False)
    async def ui_shell() -> FileResponse:
        return FileResponse(frontend_dist / "index.html")

    frontend_root = frontend_dist.resolve()

    @api.get("/ui/{path:path}", include_in_schema=False)
    async def ui_spa(path: str) -> FileResponse:
        # Resolve before checking so `..` segments (raw or %2f-encoded) cannot
        # escape the dist folder and serve arbitrary files from the host.
        requested = (frontend_root / path).resolve()
        if requested.is_relative_to(frontend_root) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_root / "index.html")


mount_frontend_routes(app, _frontend_dist)

if _frontend_dist is None:
    log.warning(
        "frontend_dist_missing",
        hint="Run `cd frontend && npm ci && npm run build` to serve the UI at /ui",
    )


@app.get("/", include_in_schema=False)
def read_root() -> RedirectResponse:
    """The UI lives at /ui/ (API docs at /docs)."""
    return RedirectResponse(url="/ui/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
