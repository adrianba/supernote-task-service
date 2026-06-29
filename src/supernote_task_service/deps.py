"""Shared FastAPI dependencies: settings, repository, and rate limiting."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings
from .encoding import now_ms
from .errors import ApiError
from .ratelimit import RateLimiter
from .repository import Repository
from .security import validate_api_key


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repository(request: Request) -> Repository:
    return request.app.state.repository  # type: ignore[no-any-return]


def ensure_cursor_fresh(since: int | None, settings: Settings) -> None:
    """Reject a delta cursor older than the configured retention with 410.

    Disabled by default (``cursor_max_age_ms == 0``) so behavior is unchanged;
    when enabled, a too-old ``since`` signals the client to do a full resync.
    """
    if since is None or settings.cursor_max_age_ms <= 0:
        return
    if since < now_ms() - settings.cursor_max_age_ms:
        raise ApiError(
            status.HTTP_410_GONE,
            "Cursor has expired; perform a full resync without 'since'.",
            code="cursor_expired",
        )


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


async def enforce_rate_limit(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    """Authenticate the caller; rate-limit only unauthenticated requests.

    Authenticated callers (valid API key) are **not** rate limited. Missing or
    invalid keys are aggressively throttled per client IP to blunt online key
    guessing and unauthenticated floods: when over the limit they get ``429``,
    otherwise ``401``.
    """
    caller_id = validate_api_key(request, authorization, x_api_key)
    if caller_id is not None:
        return caller_id

    settings: Settings = request.app.state.settings
    ip = _client_ip(request, settings.trust_proxy_headers)
    unauth: RateLimiter = request.app.state.unauth_rate_limiter
    result = unauth.check(f"ip:{ip}")
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": str(result.retry_after)},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )


RepositoryDep = Annotated[Repository, Depends(get_repository)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CallerDep = Annotated[str, Depends(enforce_rate_limit)]
