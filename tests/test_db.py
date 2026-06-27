"""Unit tests for :meth:`Database._resolve_user_id` user-resolution logic.

These stub the cursor so no real MariaDB connection is needed; they verify the
precedence rules: explicit ``SUPERNOTE_USER_EMAIL`` lookup, single ``u_user``
row auto-detection, the fail-fast error when multiple users exist, and the
legacy fallback to the task tables when ``u_user`` is empty.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from supernote_task_service.config import Settings
from supernote_task_service.db import Database, UserResolutionError


class FakeCursor:
    """Minimal cursor stub answering the resolution queries from canned rows."""

    def __init__(
        self,
        *,
        u_user_rows: list[dict[str, Any]],
        task_ids: list[int] | None = None,
        group_ids: list[int] | None = None,
    ) -> None:
        self._u_user_rows = u_user_rows
        self._task_ids = task_ids or []
        self._group_ids = group_ids or []
        self._last_sql = ""
        self._last_params: tuple[Any, ...] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._last_sql = sql
        self._last_params = params

    def fetchone(self) -> dict[str, Any] | None:
        # Only the email lookup uses fetchone.
        assert self._last_params is not None
        email = self._last_params[0]
        for row in self._u_user_rows:
            if row.get("email") == email:
                return {"user_id": row["user_id"]}
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        sql = self._last_sql
        if "FROM u_user" in sql:
            return [{"user_id": row["user_id"]} for row in self._u_user_rows]
        if "t_schedule_task_group" in sql:
            return [{"user_id": uid} for uid in self._group_ids]
        if "t_schedule_task" in sql:
            return [{"user_id": uid} for uid in self._task_ids]
        return []


def _make_db(cursor: FakeCursor, *, user_email: str = "") -> Database:
    settings = Settings(SUPERNOTE_DB_PASSWORD="unused", SUPERNOTE_USER_EMAIL=user_email)
    db = Database(settings)

    @contextmanager
    def _fake_cursor() -> Iterator[FakeCursor]:
        yield cursor

    db.cursor = _fake_cursor  # type: ignore[method-assign]
    return db


def test_resolve_by_email_found() -> None:
    cursor = FakeCursor(
        u_user_rows=[
            {"user_id": 11, "email": "a@example.com"},
            {"user_id": 22, "email": "b@example.com"},
        ]
    )
    db = _make_db(cursor, user_email="b@example.com")
    assert db.get_user_id() == 22


def test_resolve_by_email_not_found_raises() -> None:
    cursor = FakeCursor(u_user_rows=[{"user_id": 11, "email": "a@example.com"}])
    db = _make_db(cursor, user_email="missing@example.com")
    with pytest.raises(UserResolutionError):
        db.get_user_id()


def test_resolve_single_user_autodetect() -> None:
    cursor = FakeCursor(u_user_rows=[{"user_id": 7, "email": "solo@example.com"}])
    db = _make_db(cursor)
    assert db.get_user_id() == 7


def test_resolve_multiple_users_without_email_raises() -> None:
    cursor = FakeCursor(
        u_user_rows=[
            {"user_id": 1, "email": "a@example.com"},
            {"user_id": 2, "email": "b@example.com"},
        ]
    )
    db = _make_db(cursor)
    with pytest.raises(UserResolutionError):
        db.get_user_id()


def test_resolve_empty_u_user_falls_back_to_tasks() -> None:
    cursor = FakeCursor(u_user_rows=[], task_ids=[42, 42], group_ids=[42])
    db = _make_db(cursor)
    assert db.get_user_id() == 42


def test_resolve_empty_u_user_multiple_task_users_raises() -> None:
    cursor = FakeCursor(u_user_rows=[], task_ids=[1], group_ids=[2])
    db = _make_db(cursor)
    with pytest.raises(UserResolutionError):
        db.get_user_id()


def test_resolve_no_users_anywhere_raises() -> None:
    cursor = FakeCursor(u_user_rows=[])
    db = _make_db(cursor)
    with pytest.raises(UserResolutionError):
        db.get_user_id()


def test_resolved_user_id_is_cached() -> None:
    cursor = FakeCursor(u_user_rows=[{"user_id": 9, "email": "solo@example.com"}])
    db = _make_db(cursor)
    assert db.get_user_id() == 9
    # Mutating the backing rows must not change the cached result.
    cursor._u_user_rows = [{"user_id": 99, "email": "other@example.com"}]
    assert db.get_user_id() == 9
