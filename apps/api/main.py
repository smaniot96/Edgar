"""FastAPI application entrypoint.

Wires the routers, serves the chat UI at /ui, attaches a request-id middleware so structured
logs and 500 responses share a correlation id, and configures structlog at startup.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from edgar_core.config import API_TITLE, API_VERSION

from .logging_config import configure_logging
from .routers.campaign import router as campaign_router
from .routers.character import router as character_router
from .routers.health import router as health_router
from .routers.seed import router as seed_router
from .routers.session import router as session_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler so 500s carry a request_id and never leak internals.

    We re-raise HTTPException and RequestValidationError so FastAPI's built-in handlers can
    return their proper status codes (404, 422, etc.).
    """
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, RequestValidationError):
        raise exc

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
app.include_router(campaign_router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(character_router, prefix="/api")
app.include_router(seed_router, prefix="/api")

# Do not use `edgar_core.config.EDGAR_ROOT` here: when `edgar_core` is imported from
# `.venv`/site-packages, that path points at the wrong tree. Walk upward from this file until we
# find `static/index.html` (repo layout) or `apps/api/static/index.html` (monorepo root).
def _chat_static_dir() -> Path:
    start = Path(__file__).resolve()
    for anchor in (start.parent, *start.parents):
        for rel in ("static", Path("apps") / "api" / "static"):
            candidate = anchor / rel
            if (candidate / "index.html").is_file():
                return candidate
    raise RuntimeError(
        "Chat UI missing: could not find static/index.html near "
        f"{start} (checked parent directories)"
    )


# Explicit routes (not only StaticFiles): some deployments resolve `main.py` under `.venv`, and a
# mount-based `/ui` can yield FastAPI's JSON 404 for `GET /ui` in browsers.
_chat_static = _chat_static_dir()


@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
async def chat_ui() -> FileResponse:
    return FileResponse(_chat_static / "index.html")


@app.get("/")
def read_root():
    return {"message": "Hello from api!"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
