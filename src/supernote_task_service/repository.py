"""SQL data-access for Supernote tasks and lists.

All queries are parameterized. Writes always set ``last_modified`` to the
current time so the Supernote device treats them as the latest version, and
deletes are soft (``is_deleted='Y'``) to stay compatible with device sync.

Incremental sync uses an inclusive millisecond lower bound: a call with
``since`` returns rows where ``since <= last_modified <= server_now`` and
returns the next cursor. The inclusive bound means a change written at exactly
the previous cursor millisecond is never lost, at the cost of re-delivering the
boundary row, so clients must treat sync as idempotent. When a full page is
entirely a single millisecond the remaining rows at that millisecond are
drained and the cursor advances past it, so pagination can never stall.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .db import Database
from .encoding import (
    datetime_to_ms,
    decode_document_link,
    decode_emoji,
    encode_detail,
    encode_document_link,
    encode_emoji,
    ms_to_datetime,
    new_id,
    now_ms,
)
from .models import (
    DocumentLink,
    Task,
    TaskCreate,
    TaskList,
    TaskStatus,
    TaskUpdate,
)

_TASK_COLUMNS = """
    t.task_id, t.task_list_id, t.title, t.detail, t.status, t.importance,
    t.due_time, t.completed_time, t.last_modified, t.sort, t.links, t.is_deleted,
    COALESCE(g.title, 'Inbox') AS category
"""

# Safety bounds so a single request can never load an unbounded result set.
DEFAULT_PAGE_LIMIT = 500
MAX_PAGE_LIMIT = 1000


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    return max(1, min(limit, MAX_PAGE_LIMIT))


class Repository:
    """Data-access object backed by a :class:`Database` connection pool."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ----- mapping helpers ------------------------------------------------

    @staticmethod
    def _parse_importance(value: Any) -> int | None:
        """Coerce the varchar ``importance`` column to an int (None if empty)."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    @staticmethod
    def _row_to_task(row: dict[str, Any]) -> Task:
        link_data = decode_document_link(row.get("links"))
        document_link = DocumentLink.model_validate(link_data) if link_data else None
        due = int(row["due_time"] or 0)
        completed = int(row["completed_time"] or 0)
        sort = row.get("sort")
        return Task(
            id=row["task_id"],
            list_id=row["task_list_id"],
            category=decode_emoji(row["category"]),
            title=decode_emoji(row["title"]),
            detail=decode_emoji(row["detail"] or ""),
            status=TaskStatus(row["status"]),
            importance=Repository._parse_importance(row.get("importance")),
            due=_ms_to_dt(due),
            completed=_ms_to_dt(completed),
            sort=int(sort) if sort is not None else None,
            last_modified=int(row["last_modified"] or 0),
            document_link=document_link,
            is_deleted=(row["is_deleted"] == "Y"),
        )

    @staticmethod
    def _row_to_list(row: dict[str, Any]) -> TaskList:
        return TaskList(
            id=row["task_list_id"],
            title=decode_emoji(row["title"]),
            last_modified=int(row["last_modified"] or 0),
            is_deleted=(row["is_deleted"] == "Y"),
        )

    # ----- task reads -----------------------------------------------------

    def list_tasks(
        self,
        *,
        since: int | None = None,
        list_id: str | None = None,
        include_completed: bool = True,
        inbox_only: bool = False,
        limit: int | None = None,
    ) -> tuple[list[Task], int, bool]:
        server_now = now_ms()
        effective_limit = _clamp_limit(limit)
        uid = self._db.get_user_id()
        clauses: list[str] = ["t.user_id = %s"]
        params: list[Any] = [uid]

        if since is not None:
            # Delta mode: include completed and soft-deleted rows so clients can
            # propagate every change, including deletions. The lower bound is
            # inclusive so a change written at exactly the previous cursor
            # millisecond is never lost; clients must treat sync as idempotent.
            clauses.append("t.last_modified >= %s AND t.last_modified <= %s")
            params.extend([since, server_now])
        else:
            clauses.append("t.is_deleted = 'N'")
            if not include_completed:
                clauses.append("t.status = 'needsAction'")

        if inbox_only:
            clauses.append("t.task_list_id IS NULL")
        elif list_id is not None:
            clauses.append("t.task_list_id = %s")
            params.append(list_id)

        where = " AND ".join(clauses)
        params.append(effective_limit)
        # Only the constant column list and pre-built clause strings (which use
        # %s placeholders) are interpolated; all values are parameterized.
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM t_schedule_task t "  # noqa: S608
            "LEFT JOIN t_schedule_task_group g ON t.task_list_id = g.task_list_id "
            f"WHERE {where} ORDER BY t.last_modified ASC, t.task_id ASC LIMIT %s"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        tasks = [self._row_to_task(r) for r in rows]
        if since is not None and self._is_single_ms_full_page(tasks, effective_limit):
            # The whole page sits on one millisecond; more rows may share it.
            # Drain them and advance past the millisecond so we can't loop. The
            # boundary is fully drained, so there is definitively no more.
            boundary = tasks[-1].last_modified
            tasks.extend(
                self._drain_tasks_at(
                    boundary=boundary,
                    after_id=tasks[-1].id,
                    list_id=list_id,
                    inbox_only=inbox_only,
                )
            )
            return tasks, boundary + 1, False
        cursor = self._page_cursor(tasks, effective_limit, server_now)
        has_more = self._is_full_page(tasks, effective_limit)
        return tasks, cursor, has_more

    def _drain_tasks_at(
        self, *, boundary: int, after_id: str, list_id: str | None, inbox_only: bool
    ) -> list[Task]:
        """Return remaining tasks sharing ``boundary`` ms with id > ``after_id``."""
        clauses = ["t.user_id = %s", "t.last_modified = %s", "t.task_id > %s"]
        params: list[Any] = [self._db.get_user_id(), boundary, after_id]
        if inbox_only:
            clauses.append("t.task_list_id IS NULL")
        elif list_id is not None:
            clauses.append("t.task_list_id = %s")
            params.append(list_id)
        where = " AND ".join(clauses)
        # Only the constant column list and constant clause fragments are
        # interpolated; all values are parameterized.
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM t_schedule_task t "  # noqa: S608
            "LEFT JOIN t_schedule_task_group g ON t.task_list_id = g.task_list_id "
            f"WHERE {where} ORDER BY t.task_id ASC"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    @staticmethod
    def _is_single_ms_full_page(items: list[Any], effective_limit: int) -> bool:
        # A full page whose first and last rows share a millisecond may hide
        # further rows at that timestamp; advancing the cursor by milliseconds
        # alone would then stall, so callers must drain the remaining rows.
        return (
            len(items) == effective_limit
            and bool(items)
            and items[0].last_modified == items[-1].last_modified
        )

    @staticmethod
    def _page_cursor(items: list[Any], effective_limit: int, server_now: int) -> int:
        # If the page is full there may be more rows; advance the cursor only to
        # the last delivered row's timestamp so the next call resumes there.
        if len(items) == effective_limit and items:
            return int(items[-1].last_modified)
        return server_now

    @staticmethod
    def _is_full_page(items: list[Any], effective_limit: int) -> bool:
        # A page capped at the limit may have more rows behind it; the client
        # should keep paging while this is true.
        return len(items) == effective_limit and bool(items)

    def get_task(self, task_id: str) -> Task | None:
        # Only the constant column list is interpolated; task_id is bound.
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM t_schedule_task t "  # noqa: S608
            "LEFT JOIN t_schedule_task_group g ON t.task_list_id = g.task_list_id "
            "WHERE t.task_id = %s AND t.user_id = %s AND t.is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (task_id, self._db.get_user_id()))
            row = cur.fetchone()
        return self._row_to_task(row) if row else None

    # ----- task writes ----------------------------------------------------

    def create_task(self, data: TaskCreate) -> str:
        task_id = new_id()
        ts = now_ms()
        user_id = self._db.get_user_id()
        due_time = _dt_to_ms(data.due)
        completed_time = ts if data.status == TaskStatus.completed else 0
        importance = str(data.importance) if data.importance is not None else None
        links = (
            encode_document_link(_link_payload(data.document_link)) if data.document_link else None
        )
        # The Supernote device requires the integer sort columns to be non-NULL
        # or the task may not appear in the To-Do app. Default ``sort`` to the
        # next position in the list; the other sort columns follow the upstream
        # convention (see https://github.com/adrianba/supernote-todo).
        sql = """
        INSERT INTO t_schedule_task (
            task_id, task_list_id, user_id, title, detail,
            last_modified, is_reminder_on, status, importance,
            due_time, completed_time, links, is_deleted,
            sort, sort_completed, planer_sort, all_sort,
            all_sort_completed, sort_time, planer_sort_time, all_sort_time
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, 'N', %s, %s,
            %s, %s, %s, 'N',
            %s, 0, 0, NULL, NULL, %s, 0, NULL
        )
        """
        with self._db.cursor() as cur:
            sort = (
                data.sort if data.sort is not None else self._next_sort(cur, data.list_id, user_id)
            )
            cur.execute(
                sql,
                (
                    task_id,
                    data.list_id,
                    user_id,
                    encode_emoji(data.title),
                    encode_detail(data.detail),
                    ts,
                    data.status.value,
                    importance,
                    due_time,
                    completed_time,
                    links,
                    sort,
                    ts,
                ),
            )
        return task_id

    @staticmethod
    def _next_sort(cur: Any, list_id: str | None, user_id: int) -> int:
        """Return the next 0-based ``sort`` position within a list for a user.

        ``task_list_id <=> %s`` is null-safe so the implicit Inbox
        (``task_list_id IS NULL``) is scoped correctly.
        """
        cur.execute(
            "SELECT COALESCE(MAX(sort), -1) + 1 AS next_sort FROM t_schedule_task "
            "WHERE task_list_id <=> %s AND user_id = %s AND is_deleted = 'N'",
            (list_id, user_id),
        )
        row = cur.fetchone()
        return int(row["next_sort"]) if row and row.get("next_sort") is not None else 0

    def update_task(
        self, task_id: str, data: TaskUpdate, *, expected_last_modified: int | None = None
    ) -> bool:
        fields = data.model_fields_set
        sets: list[str] = []
        params: list[Any] = []

        if "title" in fields and data.title is not None:
            sets.append("title = %s")
            params.append(encode_emoji(data.title))
        if "detail" in fields and data.detail is not None:
            sets.append("detail = %s")
            params.append(encode_detail(data.detail))
        if "status" in fields and data.status is not None:
            sets.append("status = %s")
            params.append(data.status.value)
            sets.append("completed_time = %s")
            params.append(now_ms() if data.status == TaskStatus.completed else 0)
        if "importance" in fields:
            sets.append("importance = %s")
            params.append(str(data.importance) if data.importance is not None else None)
        if "due" in fields:
            sets.append("due_time = %s")
            params.append(_dt_to_ms(data.due))
        if "sort" in fields and data.sort is not None:
            sets.append("sort = %s")
            params.append(data.sort)
        if "list_id" in fields:
            sets.append("task_list_id = %s")
            params.append(data.list_id)
        if "document_link" in fields:
            sets.append("links = %s")
            params.append(
                encode_document_link(_link_payload(data.document_link))
                if data.document_link
                else None
            )

        if not sets:
            if expected_last_modified is not None:
                return self._row_matches_version(task_id, expected_last_modified)
            return self.task_exists(task_id)

        sets.append("last_modified = %s")
        params.append(now_ms())
        params.append(task_id)
        params.append(self._db.get_user_id())
        where = "task_id = %s AND user_id = %s AND is_deleted = 'N'"
        if expected_last_modified is not None:
            where += " AND last_modified = %s"
            params.append(expected_last_modified)

        # ``sets`` contains only constant "column = %s" fragments; values are bound.
        sql = "UPDATE t_schedule_task SET " + ", ".join(sets) + " WHERE " + where  # noqa: S608
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, params))
        return affected > 0

    def _row_matches_version(self, task_id: str, expected_last_modified: int) -> bool:
        """Return True if the (active) task exists at the expected version."""
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_schedule_task "
                "WHERE task_id = %s AND user_id = %s AND is_deleted = 'N' "
                "AND last_modified = %s",
                (task_id, self._db.get_user_id(), expected_last_modified),
            )
            return cur.fetchone() is not None

    def set_status(
        self, task_id: str, status: TaskStatus, *, expected_last_modified: int | None = None
    ) -> bool:
        return self.update_task(
            task_id, TaskUpdate(status=status), expected_last_modified=expected_last_modified
        )

    def delete_task(self, task_id: str, *, expected_last_modified: int | None = None) -> bool:
        params: list[Any] = [now_ms(), task_id, self._db.get_user_id()]
        where = "task_id = %s AND user_id = %s AND is_deleted = 'N'"
        if expected_last_modified is not None:
            where += " AND last_modified = %s"
            params.append(expected_last_modified)
        sql = "UPDATE t_schedule_task SET is_deleted = 'Y', last_modified = %s WHERE " + where  # noqa: S608
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, params))
        return affected > 0

    def task_exists(self, task_id: str) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_schedule_task "
                "WHERE task_id = %s AND user_id = %s AND is_deleted = 'N'",
                (task_id, self._db.get_user_id()),
            )
            return cur.fetchone() is not None

    # ----- list reads -----------------------------------------------------

    def list_lists(
        self, *, since: int | None = None, limit: int | None = None
    ) -> tuple[list[TaskList], int, bool]:
        server_now = now_ms()
        effective_limit = _clamp_limit(limit)
        uid = self._db.get_user_id()
        if since is not None:
            # Inclusive lower bound (see list_tasks) for idempotent delta sync.
            sql = (
                "SELECT task_list_id, title, last_modified, is_deleted "
                "FROM t_schedule_task_group "
                "WHERE user_id = %s AND last_modified >= %s AND last_modified <= %s "
                "ORDER BY last_modified ASC, task_list_id ASC LIMIT %s"
            )
            params: tuple[Any, ...] = (uid, since, server_now, effective_limit)
            include_inbox = False
        else:
            sql = (
                "SELECT task_list_id, title, last_modified, is_deleted "
                "FROM t_schedule_task_group WHERE user_id = %s AND is_deleted = 'N' "
                "ORDER BY title LIMIT %s"
            )
            params = (uid, effective_limit)
            include_inbox = True

        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        lists = [self._row_to_list(r) for r in rows]
        if since is not None and self._is_single_ms_full_page(lists, effective_limit):
            boundary = lists[-1].last_modified
            lists.extend(self._drain_lists_at(boundary=boundary, after_id=lists[-1].id))
            return lists, boundary + 1, False
        cursor = self._page_cursor(lists, effective_limit, server_now)
        has_more = self._is_full_page(lists, effective_limit)
        if include_inbox:
            lists.insert(0, TaskList(id=None, title="Inbox", last_modified=0, is_deleted=False))
        return lists, cursor, has_more

    def _drain_lists_at(self, *, boundary: int, after_id: str | None) -> list[TaskList]:
        """Return remaining lists sharing ``boundary`` ms with id > ``after_id``."""
        sql = (
            "SELECT task_list_id, title, last_modified, is_deleted "
            "FROM t_schedule_task_group "
            "WHERE user_id = %s AND last_modified = %s AND task_list_id > %s "
            "ORDER BY task_list_id ASC"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (self._db.get_user_id(), boundary, after_id))
            rows = cur.fetchall()
        return [self._row_to_list(r) for r in rows]

    def get_list(self, list_id: str) -> TaskList | None:
        sql = (
            "SELECT task_list_id, title, last_modified, is_deleted "
            "FROM t_schedule_task_group "
            "WHERE task_list_id = %s AND user_id = %s AND is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (list_id, self._db.get_user_id()))
            row = cur.fetchone()
        return self._row_to_list(row) if row else None

    def list_exists(self, list_id: str) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_schedule_task_group "
                "WHERE task_list_id = %s AND user_id = %s AND is_deleted = 'N'",
                (list_id, self._db.get_user_id()),
            )
            return cur.fetchone() is not None

    def get_list_by_title(self, title: str) -> TaskList | None:
        """Return the non-deleted list whose stored (encoded) title matches."""
        sql = (
            "SELECT task_list_id, title, last_modified, is_deleted "
            "FROM t_schedule_task_group "
            "WHERE title = %s AND user_id = %s AND is_deleted = 'N' "
            "ORDER BY last_modified ASC, task_list_id ASC LIMIT 1"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (encode_emoji(title), self._db.get_user_id()))
            row = cur.fetchone()
        return self._row_to_list(row) if row else None

    # ----- list writes ----------------------------------------------------

    def create_list(self, title: str) -> str:
        list_id = new_id()
        ts = now_ms()
        user_id = self._db.get_user_id()
        sql = (
            "INSERT INTO t_schedule_task_group "
            "(task_list_id, user_id, title, last_modified, is_deleted, create_time) "
            "VALUES (%s, %s, %s, %s, 'N', %s)"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (list_id, user_id, encode_emoji(title), ts, ts))
        return list_id

    def update_list(
        self, list_id: str, title: str, *, expected_last_modified: int | None = None
    ) -> bool:
        params: list[Any] = [encode_emoji(title), now_ms(), list_id, self._db.get_user_id()]
        where = "task_list_id = %s AND user_id = %s AND is_deleted = 'N'"
        if expected_last_modified is not None:
            where += " AND last_modified = %s"
            params.append(expected_last_modified)
        sql = "UPDATE t_schedule_task_group SET title = %s, last_modified = %s WHERE " + where  # noqa: S608
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, params))
        return affected > 0

    def delete_list(self, list_id: str, *, expected_last_modified: int | None = None) -> bool:
        params: list[Any] = [now_ms(), list_id, self._db.get_user_id()]
        where = "task_list_id = %s AND user_id = %s AND is_deleted = 'N'"
        if expected_last_modified is not None:
            where += " AND last_modified = %s"
            params.append(expected_last_modified)
        sql = "UPDATE t_schedule_task_group SET is_deleted = 'Y', last_modified = %s WHERE " + where  # noqa: S608
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, params))
        return affected > 0


def _link_payload(link: DocumentLink) -> dict[str, Any]:
    return {
        "appName": link.app_name,
        "fileId": link.file_id,
        "filePath": link.file_path,
        "page": link.page,
        "pageId": link.page_id,
    }


def _dt_to_ms(dt: datetime | None) -> int:
    return datetime_to_ms(dt) if dt is not None else 0


def _ms_to_dt(ms: int) -> datetime | None:
    return ms_to_datetime(ms)
