import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from edgar_core.config import API_TITLE, API_VERSION

from .logging_config import configure_logging
from .routers.campaign import router as campaign_router
from .routers.character import router as character_router
from .routers.health import router as health_router
from .routers.session import router as session_router
from .routers.seed import router as seed_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    yield
    from .dependencies import close_redis

    await close_redis()


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
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

_static = Path(__file__).parent / "static"
app.mount("/ui", StaticFiles(directory=str(_static), html=True), name="ui")


@app.get("/")
def read_root():
    return {"message": "Hello from api!"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
