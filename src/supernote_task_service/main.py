"""FastAPI application factory and ASGI middleware wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .db import Database
from .errors import register_exception_handlers
from .ratelimit import RateLimiter
from .repository import Repository
from .routers import health, lists, tasks

logger = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    database = Database(settings)
    app.state.database = database
    app.state.repository = Repository(database)
    app.state.rate_limiter = RateLimiter(
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    app.state.pre_auth_rate_limiter = RateLimiter(
        limit=settings.unauth_rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    try:
        yield
    finally:
        database.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    _configure_logging(settings.log_level)

    if not settings.has_api_keys():
        logger.warning(
            "No API_KEYS configured; the service will reject all authenticated requests."
        )

    docs_url = "/docs" if settings.enable_docs else None
    app = FastAPI(
        title="Supernote Task Service",
        version=__version__,
        summary="Authenticated sync API for Supernote to-do tasks.",
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    register_exception_handlers(app)

    @app.middleware("http")
    async def _limit_body_and_set_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        has_body_method = request.method in {"POST", "PUT", "PATCH"}
        if content_length is not None:
            try:
                if int(content_length) > settings.max_request_body_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": "Request body too large.", "code": "payload_too_large"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header.", "code": "bad_request"},
                )
        elif has_body_method and request.headers.get("transfer-encoding"):
            # Require a declared length so the body-size limit cannot be bypassed
            # with a chunked/streaming request.
            return JSONResponse(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                content={"detail": "Content-Length is required.", "code": "length_required"},
            )
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    app.include_router(health.router)
    app.include_router(lists.router)
    app.include_router(tasks.router)
    return app
