"""Shared FastAPI dependencies: settings, repository, and rate limiting."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response, status

from .config import Settings
from .ratelimit import RateLimiter
from .repository import Repository
from .security import require_api_key


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repository(request: Request) -> Repository:
    return request.app.state.repository  # type: ignore[no-any-return]


def _client_ip(request: Request, trust_proxy: bool) -> str:
    """Return the client IP used for rate-limit bucketing.

    When behind a trusted single-hop proxy (Traefik), the *rightmost*
    ``X-Forwarded-For`` entry is the address the proxy actually observed, so it
    cannot be spoofed by a client prepending its own values to the header.
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _set_rate_limit_headers(response: Response, limit: int, remaining: int, reset_at: int) -> None:
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_at)


async def enforce_rate_limit(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Rate-limit per IP (pre-auth), authenticate, then rate-limit per caller+IP.

    The pre-auth per-IP check runs *before* API-key validation so that invalid
    keys are still throttled, preventing unbounded online key guessing.
    """
    settings: Settings = request.app.state.settings
    ip = _client_ip(request, settings.trust_proxy_headers)

    pre_auth: RateLimiter = request.app.state.pre_auth_rate_limiter
    pre = pre_auth.check(f"ip:{ip}")
    if not pre.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": str(pre.retry_after)},
        )

    caller_id = await require_api_key(request, authorization, x_api_key)

    limiter: RateLimiter = request.app.state.rate_limiter
    result = limiter.check(f"{caller_id}:{ip}")
    _set_rate_limit_headers(response, result.limit, result.remaining, result.reset_at)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": str(result.retry_after)},
        )
    return caller_id


RepositoryDep = Annotated[Repository, Depends(get_repository)]
CallerDep = Annotated[str, Depends(enforce_rate_limit)]
