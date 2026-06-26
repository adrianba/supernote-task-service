"""Centralized error handling that avoids leaking internal details.

Every error response carries both a human-readable ``detail`` and a stable,
machine-readable ``code`` so clients can branch on the latter without parsing
prose. ``detail`` text is preserved for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

import pymysql
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Default mapping from HTTP status to a stable error code. Endpoints that need a
# more specific code (e.g. an expired cursor) raise :class:`ApiError` instead.
_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_410_GONE: "gone",
    status.HTTP_411_LENGTH_REQUIRED: "length_required",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_503_SERVICE_UNAVAILABLE: "db_unavailable",
}


def code_for_status(status_code: int) -> str:
    """Return the stable error code for an HTTP status (fallback ``error``)."""
    return _STATUS_CODES.get(status_code, "error")


class ApiError(StarletteHTTPException):
    """An HTTP error carrying an explicit machine-readable ``code``."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code or code_for_status(status_code)


def _error_response(status_code: int, detail: Any, code: str, headers: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.detail, exc.code, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", None) or code_for_status(exc.status_code)
        return _error_response(exc.status_code, exc.detail, code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            jsonable_encoder(exc.errors()),
            "validation_error",
        )

    @app.exception_handler(pymysql.Error)
    async def _handle_db_error(request: Request, exc: pymysql.Error) -> JSONResponse:
        logger.error("Database error on %s %s: %s", request.method, request.url.path, exc)
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The task database is currently unavailable.",
            "db_unavailable",
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Internal server error.",
            "internal_error",
        )
