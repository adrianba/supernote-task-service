"""Health and readiness endpoints (unauthenticated)."""

from __future__ import annotations

import pymysql
from fastapi import APIRouter, Request, Response, status

from .. import __version__

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/v1/version", summary="Service version")
async def version() -> dict[str, str]:
    return {"version": __version__}


@router.get("/readyz", summary="Readiness probe")
def readyz(request: Request, response: Response) -> dict[str, str]:
    # Sync handler so FastAPI runs the blocking DB ping in its threadpool and a
    # slow/unreachable database can't stall the event loop for other requests.
    db = request.app.state.database
    try:
        db.ping()
    except pymysql.Error:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ready"}
