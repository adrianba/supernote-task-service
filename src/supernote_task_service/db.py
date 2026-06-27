"""MariaDB connection management for the Supernote database.

Provides a small thread-safe connection pool over ``pymysql`` and helpers to
resolve the single exposed ``user_id`` (by configured email, or auto-detected
for single-user installs). Connections are validated (and reconnected) before
being handed out.
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


class UserResolutionError(RuntimeError):
    """Raised when the configured/exposed Supernote user cannot be resolved.

    Distinct from :class:`pymysql.Error` so startup can tell a misconfiguration
    (fatal — abort boot) apart from a transient database outage (retry lazily).
    """


class Database:
    """A minimal thread-safe pool of pymysql connections."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: queue.LifoQueue[Connection] = queue.LifoQueue(maxsize=settings.db_pool_size)
        self._lock = threading.Lock()
        self._user_lock = threading.Lock()
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

    def _try_reserve_slot(self) -> bool:
        """Atomically reserve a pool slot if capacity remains.

        The check and the increment happen under the same lock so two threads
        can never both observe spare capacity and overshoot ``db_pool_size``.
        """
        with self._lock:
            if self._created < self._settings.db_pool_size:
                self._created += 1
                return True
            return False

    def _unreserve_slot(self) -> None:
        with self._lock:
            self._created -= 1

    def _create_reserved(self) -> Connection:
        """Open a connection for an already-reserved slot, freeing it on failure."""
        try:
            return self._connect()
        except Exception:
            # Release the reserved slot so a transient failure can't permanently
            # shrink the pool's capacity.
            self._unreserve_slot()
            raise

    def _acquire(self) -> Connection:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            if self._try_reserve_slot():
                return self._create_reserved()
            try:
                # Bounded wait avoids deadlocking a worker forever if every
                # connection is in use and none is returned.
                conn = self._pool.get(timeout=self._settings.db_connect_timeout)
            except queue.Empty as exc:
                raise pymysql.OperationalError(
                    2003, "No database connection available from the pool."
                ) from exc
        try:
            conn.ping(reconnect=True)
        except pymysql.Error:
            with suppress(pymysql.Error):
                conn.close()
            # The dead connection still owns its reserved slot; reconnect under
            # the same reservation instead of releasing and re-reserving it.
            try:
                conn = self._connect()
            except Exception:
                self._unreserve_slot()
                raise
        return conn

    def _release(self, conn: Connection) -> None:
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            # Sound accounting keeps the queue from ever filling, but free the
            # slot defensively if it somehow does so the count can't inflate.
            conn.close()
            self._unreserve_slot()

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
        """Return the resolved exposed ``user_id`` (resolved once and cached)."""
        if self._user_id is not None:
            return self._user_id
        # A dedicated lock (not the pool lock) so resolution can acquire a pooled
        # connection without risking a deadlock against ``_acquire``.
        with self._user_lock:
            if self._user_id is not None:
                return self._user_id
            self._user_id = self._resolve_user_id()
            return self._user_id

    def _resolve_user_id(self) -> int:
        """Resolve the exposed user.

        - If ``SUPERNOTE_USER_EMAIL`` is set, look it up in ``u_user`` (the
          ``email`` column is UNIQUE); raise if there is no such user.
        - Otherwise auto-detect: use the sole ``u_user`` row, or raise if the
          database contains more than one user (the caller must then configure
          ``SUPERNOTE_USER_EMAIL``). If ``u_user`` is empty, fall back to the
          single distinct ``user_id`` referenced by the task tables.
        """
        email = self._settings.user_email
        with self.cursor() as cur:
            if email:
                cur.execute("SELECT user_id FROM u_user WHERE email = %s", (email,))
                row: dict[str, Any] | None = cur.fetchone()
                if row and row.get("user_id") is not None:
                    return int(row["user_id"])
                raise UserResolutionError(
                    f"No Supernote user found with email {email!r} (check SUPERNOTE_USER_EMAIL)."
                )

            cur.execute("SELECT user_id FROM u_user ORDER BY user_id")
            users = [r["user_id"] for r in cur.fetchall() if r.get("user_id") is not None]
            if len(users) == 1:
                return int(users[0])
            if len(users) > 1:
                raise UserResolutionError(
                    f"The database contains {len(users)} users; set "
                    "SUPERNOTE_USER_EMAIL to select which one to expose."
                )
            # u_user is empty (or has no usable ids): fall back to the task tables.
            return self._detect_user_id_from_tasks(cur)

    @staticmethod
    def _detect_user_id_from_tasks(cur: DictCursor) -> int:
        """Resolve a single distinct ``user_id`` from the task tables (legacy)."""
        cur.execute("SELECT DISTINCT user_id FROM t_schedule_task WHERE user_id IS NOT NULL")
        ids = {r["user_id"] for r in cur.fetchall() if r.get("user_id") is not None}
        cur.execute("SELECT DISTINCT user_id FROM t_schedule_task_group WHERE user_id IS NOT NULL")
        ids |= {r["user_id"] for r in cur.fetchall() if r.get("user_id") is not None}
        if len(ids) == 1:
            return int(next(iter(ids)))
        if len(ids) > 1:
            raise UserResolutionError(
                f"The task tables reference {len(ids)} users; set "
                "SUPERNOTE_USER_EMAIL to select which one to expose."
            )
        raise UserResolutionError("Unable to resolve a Supernote user_id from the database.")

    def close(self) -> None:
        """Close all pooled connections."""
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            with suppress(pymysql.Error):
                conn.close()
