"""Shared FastAPI dependencies: settings, repository, and rate limiting."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from .config import Settings
from .ratelimit import RateLimiter
from .repository import Repository
from .security import require_api_key


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repository(request: Request) -> Repository:
    return request.app.state.repository  # type: ignore[no-any-return]


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(
    request: Request,
    response: Response,
    caller_id: Annotated[str, Depends(require_api_key)],
) -> str:
    """Apply per-caller + per-IP rate limiting and set rate-limit headers."""
    settings: Settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    ip = _client_ip(request, settings.trust_proxy_headers)
    result = limiter.check(f"{caller_id}:{ip}")

    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(result.reset_at)

    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": str(result.retry_after)},
        )
    return caller_id


RepositoryDep = Annotated[Repository, Depends(get_repository)]
CallerDep = Annotated[str, Depends(enforce_rate_limit)]
