"""Tests for the in-memory rate limiter and API-key security helpers."""

from __future__ import annotations

from supernote_task_service.config import _hash_key
from supernote_task_service.ratelimit import RateLimiter
from supernote_task_service.security import _extract_presented_key, _key_matches


def test_rate_limiter_blocks_after_limit() -> None:
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.check("k").allowed is True
    assert limiter.check("k").allowed is True
    blocked = limiter.check("k")
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after >= 1


def test_rate_limiter_isolates_keys() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.check("a").allowed is True
    assert limiter.check("b").allowed is True
    assert limiter.check("a").allowed is False


def test_extract_presented_key_bearer_and_header() -> None:
    assert _extract_presented_key("Bearer abc", None) == "abc"
    assert _extract_presented_key("bearer abc", None) == "abc"
    assert _extract_presented_key(None, "xyz") == "xyz"
    assert _extract_presented_key("Basic abc", None) is None
    assert _extract_presented_key(None, None) is None


def test_key_matches_constant_time() -> None:
    valid = frozenset({_hash_key("secret")})
    assert _key_matches("secret", valid) is True
    assert _key_matches("wrong", valid) is False
