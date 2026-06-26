"""API-key authentication.

Keys are compared by SHA-256 hash using constant-time comparison. Raw keys are
never stored or logged.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings


def _extract_presented_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract a presented API key from the Authorization or X-API-Key header."""
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential:
            return credential.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def _key_matches(presented: str, valid_hashes: frozenset[str]) -> bool:
    """Constant-time membership test for a presented key against valid hashes."""
    presented_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    matched = False
    # Compare against every hash to avoid early-exit timing leaks.
    for valid in valid_hashes:
        if hmac.compare_digest(presented_hash, valid):
            matched = True
    return matched


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """FastAPI dependency that enforces a valid API key.

    Returns a stable, non-secret identifier for the authenticated caller
    (the key hash prefix) for use as a rate-limit bucket key.
    """
    settings: Settings = request.app.state.settings
    valid_hashes = settings.api_key_hashes

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not valid_hashes:
        # Fail closed: refuse all requests if no keys are configured.
        raise unauthorized

    presented = _extract_presented_key(authorization, x_api_key)
    if not presented or not _key_matches(presented, valid_hashes):
        raise unauthorized

    return hashlib.sha256(presented.encode("utf-8")).hexdigest()[:16]


CallerId = Depends(require_api_key)
