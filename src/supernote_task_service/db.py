"""MariaDB connection management for the Supernote database.

Provides a small thread-safe connection pool over ``pymysql`` and helpers to
auto-detect the single-user ``user_id``. Connections are validated (and
reconnected) before being handed out.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from .config import Settings

logger = logging.getLogger(__name__)


class Database:
    """A minimal thread-safe pool of pymysql connections."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: queue.LifoQueue[Connection] = queue.LifoQueue(maxsize=settings.db_pool_size)
        self._lock = threading.Lock()
        self._created = 0
        self._user_id: int | None = None

    def _connect(self) -> Connection:
        return pymysql.connect(
            host=self._settings.db_host,
            port=self._settings.db_port,
            user=self._settings.db_user,
            password=self._settings.db_password,
            database=self._settings.db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=self._settings.db_connect_timeout,
            read_timeout=self._settings.db_connect_timeout,
            write_timeout=self._settings.db_connect_timeout,
        )

    def _acquire(self) -> Connection:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._settings.db_pool_size:
                    self._created += 1
                    return self._connect()
            conn = self._pool.get()  # block until one is returned
        try:
            conn.ping(reconnect=True)
        except pymysql.Error:
            with suppress(pymysql.Error):
                conn.close()
            conn = self._connect()
        return conn

    def _release(self, conn: Connection) -> None:
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()

    @contextmanager
    def cursor(self) -> Iterator[DictCursor]:
        """Yield a dict cursor from a pooled connection."""
        conn = self._acquire()
        try:
            with conn.cursor(DictCursor) as cur:
                yield cur
        finally:
            self._release(conn)

    def ping(self) -> None:
        """Raise if the database is unreachable (used by readiness checks)."""
        with self.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    def get_user_id(self) -> int:
        """Return the single-user ``user_id``, auto-detected and cached."""
        if self._user_id is not None:
            return self._user_id
        with self._lock:
            if self._user_id is not None:
                return self._user_id
            self._user_id = self._detect_user_id()
            return self._user_id

    def _detect_user_id(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT user_id FROM t_schedule_task WHERE user_id IS NOT NULL LIMIT 1")
            row: dict[str, Any] | None = cur.fetchone()
            if row and row.get("user_id") is not None:
                return int(row["user_id"])
            cur.execute("SELECT user_id FROM t_schedule_task_group LIMIT 1")
            row = cur.fetchone()
            if row and row.get("user_id") is not None:
                return int(row["user_id"])
            cur.execute("SELECT id FROM u_user LIMIT 1")
            row = cur.fetchone()
            if row and row.get("id") is not None:
                return int(row["id"])
        raise RuntimeError("Unable to detect Supernote user_id from the database.")

    def close(self) -> None:
        """Close all pooled connections."""
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            with suppress(pymysql.Error):
                conn.close()
