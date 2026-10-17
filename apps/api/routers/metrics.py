"""GET /metrics: Prometheus text exposition of the operational counters (EDG-139).

Not public by default. The endpoint answers 404 unless `EDGAR_METRICS_ENABLED` is set, the same
shape as the developer-only diagnostics routes. In local mode, when `EDGAR_METRICS_TOKEN` is also set,
the scraper must send `Authorization: Bearer <token>`; a wrong or missing token gets 403. In token
(hosting) mode the scraper sends an admin API token instead (EDG-065): no token gets 401, a non-admin
gets 403, and `EDGAR_METRICS_TOKEN` is not consulted. Compose publishes the API only on 127.0.0.1, so
a scraper should reach it on the internal network (see the operations doc).
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..auth import auth_mode, require_admin
from ..services.operational_metrics import (
    CONTENT_TYPE,
    metrics_enabled,
    metrics_token,
    render_exposition,
)

router = APIRouter(tags=["metrics"])


def _authorized(request: Request, expected: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), expected)


@router.get("/metrics", include_in_schema=False, dependencies=[Depends(require_admin)])
def metrics(request: Request) -> PlainTextResponse:
    if not metrics_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    expected = metrics_token()
    if expected is not None and auth_mode() == "local" and not _authorized(request, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    return PlainTextResponse(render_exposition(), media_type=CONTENT_TYPE)
