"""FastAPI application entrypoint.

Wires the routers, serves the React UI at /ui when ``frontend/dist`` is present, attaches a
request-id middleware so structured logs and 500 responses share a correlation id, and configures
structlog at startup.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from edgar_core.config import API_TITLE, API_VERSION

from .logging_config import configure_logging
from .routers._common import HTTPErrorWithContext
from .routers.adventures import router as adventures_router
from .routers.campaign import router as campaign_router
from .routers.campaign_characters import router as campaign_characters_router
from .routers.character import router as character_router
from .routers.character_presets import router as character_presets_router
from .routers.health import router as health_router
from .routers.npc import router as npc_router
from .routers.seed import router as seed_router
from .routers.session import router as session_router
from .routers.user import router as user_router
from .routers.world_flag import router as world_flag_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Background ingest/generation tasks die with the process; flag the ones a previous run
    # left in "processing" as failed so the UI can offer a retry instead of spinning forever.
    from .services.adventures_meta import fail_stale_processing

    try:
        stale = await run_in_threadpool(fail_stale_processing)
        if stale:
            log.warning("adventures_marked_failed_after_restart", slugs=stale)
    except OSError:
        log.exception("adventures_stale_scan_failed")
    yield
    # Lazy import: avoids importing Redis at module load (helps tests that don't need it).
    from .dependencies import close_redis

    await close_redis()


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Honour an inbound `X-Request-ID` (e.g. from a load balancer) or mint a new one.

    The id is bound into structlog's contextvars so every log line emitted while handling the
    request carries it, and echoed back as a response header so clients can correlate.
    """
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        structlog.contextvars.clear_contextvars()


@app.exception_handler(HTTPErrorWithContext)
async def http_error_with_context_handler(request: Request, exc: HTTPErrorWithContext):
    """`{"detail": "<message>", **context}` — detail stays a string on every error body."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, **exc.context},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler so 500s carry a request_id and never leak internals.

    Only reached for exceptions nobody else handled: HTTPException and RequestValidationError
    are converted by FastAPI's own handlers (ExceptionMiddleware) before they get here.
    """
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("x-request-id")
        or str(uuid.uuid4())
    )
    log.exception("unhandled", request_id=request_id, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "type": "internal",
            "request_id": request_id,
        },
    )


app.include_router(health_router)
app.include_router(adventures_router, prefix="/api")
app.include_router(campaign_router, prefix="/api")
app.include_router(campaign_characters_router, prefix="/api")
app.include_router(character_router, prefix="/api")
app.include_router(character_presets_router, prefix="/api")
app.include_router(npc_router, prefix="/api")
app.include_router(seed_router, prefix="/api")
app.include_router(session_router, prefix="/api")
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

if _frontend_dist is not None:
    app.mount(
        "/ui/assets",
        StaticFiles(directory=str(_frontend_dist / "assets")),
        name="ui-assets",
    )

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    async def ui_shell() -> FileResponse:
        return FileResponse(_frontend_dist / "index.html")

    _frontend_root = _frontend_dist.resolve()

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def ui_spa(path: str) -> FileResponse:
        # Resolve before checking so `..` segments (raw or %2f-encoded) cannot
        # escape the dist folder and serve arbitrary files from the host.
        requested = (_frontend_root / path).resolve()
        if requested.is_relative_to(_frontend_root) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(_frontend_root / "index.html")
else:
    log.warning(
        "frontend_dist_missing",
        hint="Run `cd frontend && npm ci && npm run build` to serve the UI at /ui",
    )

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/{path:path}", include_in_schema=False)
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


@app.get("/", include_in_schema=False)
def read_root() -> RedirectResponse:
    """The UI lives at /ui/ (API docs at /docs)."""
    return RedirectResponse(url="/ui/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
