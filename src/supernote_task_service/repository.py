"""SQL data-access for Supernote tasks and lists.

All queries are parameterized. Writes always set ``last_modified`` to the
current time so the Supernote device treats them as the latest version, and
deletes are soft (``is_deleted='Y'``) to stay compatible with device sync.

Incremental sync uses a half-open millisecond window: a call with ``since``
returns rows where ``since < last_modified <= server_now`` and returns
``server_now`` as the next cursor. This avoids both gaps and duplicates at
timestamp boundaries.
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
    t.task_id, t.task_list_id, t.title, t.detail, t.status,
    t.due_time, t.completed_time, t.last_modified, t.links, t.is_deleted,
    COALESCE(g.title, 'Inbox') AS category
"""


class Repository:
    """Data-access object backed by a :class:`Database` connection pool."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ----- mapping helpers ------------------------------------------------

    @staticmethod
    def _row_to_task(row: dict[str, Any]) -> Task:
        link_data = decode_document_link(row.get("links"))
        document_link = DocumentLink.model_validate(link_data) if link_data else None
        due = int(row["due_time"] or 0)
        completed = int(row["completed_time"] or 0)
        return Task(
            id=row["task_id"],
            list_id=row["task_list_id"],
            category=decode_emoji(row["category"]),
            title=decode_emoji(row["title"]),
            detail=decode_emoji(row["detail"] or ""),
            status=TaskStatus(row["status"]),
            due=_ms_to_dt(due),
            completed=_ms_to_dt(completed),
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
    ) -> tuple[list[Task], int]:
        server_now = now_ms()
        clauses: list[str] = []
        params: list[Any] = []

        if since is not None:
            # Delta mode: include completed and soft-deleted rows so clients can
            # propagate every change, including deletions.
            clauses.append("t.last_modified > %s AND t.last_modified <= %s")
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
        # Only the constant column list and pre-built clause strings (which use
        # %s placeholders) are interpolated; all values are parameterized.
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM t_schedule_task t "  # noqa: S608
            "LEFT JOIN t_schedule_task_group g ON t.task_list_id = g.task_list_id "
            f"WHERE {where} ORDER BY t.last_modified ASC"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_task(r) for r in rows], server_now

    def get_task(self, task_id: str) -> Task | None:
        # Only the constant column list is interpolated; task_id is bound.
        sql = (
            f"SELECT {_TASK_COLUMNS} FROM t_schedule_task t "  # noqa: S608
            "LEFT JOIN t_schedule_task_group g ON t.task_list_id = g.task_list_id "
            "WHERE t.task_id = %s AND t.is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            cur.execute(sql, (task_id,))
            row = cur.fetchone()
        return self._row_to_task(row) if row else None

    # ----- task writes ----------------------------------------------------

    def create_task(self, data: TaskCreate) -> str:
        task_id = new_id()
        ts = now_ms()
        user_id = self._db.get_user_id()
        due_time = _dt_to_ms(data.due)
        completed_time = ts if data.status == TaskStatus.completed else 0
        links = (
            encode_document_link(_link_payload(data.document_link)) if data.document_link else None
        )
        sql = """
        INSERT INTO t_schedule_task (
            task_id, task_list_id, user_id, title, detail,
            last_modified, is_reminder_on, status, importance,
            due_time, completed_time, links, is_deleted,
            sort, sort_completed, planer_sort, all_sort,
            all_sort_completed, sort_time, planer_sort_time, all_sort_time
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, 'N', %s, NULL,
            %s, %s, %s, 'N',
            NULL, NULL, NULL, NULL, NULL, %s, %s, %s
        )
        """
        with self._db.cursor() as cur:
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
                    due_time,
                    completed_time,
                    links,
                    ts,
                    ts,
                    ts,
                ),
            )
        return task_id

    def update_task(self, task_id: str, data: TaskUpdate) -> bool:
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
        if "due" in fields:
            sets.append("due_time = %s")
            params.append(_dt_to_ms(data.due))
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
            return self.task_exists(task_id)

        sets.append("last_modified = %s")
        params.append(now_ms())
        params.append(task_id)

        # ``sets`` contains only constant "column = %s" fragments; values are bound.
        sql = (
            "UPDATE t_schedule_task SET "  # noqa: S608
            + ", ".join(sets)
            + " WHERE task_id = %s AND is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, params))
        return affected > 0

    def set_status(self, task_id: str, status: TaskStatus) -> bool:
        return self.update_task(task_id, TaskUpdate(status=status))

    def delete_task(self, task_id: str) -> bool:
        sql = (
            "UPDATE t_schedule_task SET is_deleted = 'Y', last_modified = %s "
            "WHERE task_id = %s AND is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, (now_ms(), task_id)))
        return affected > 0

    def task_exists(self, task_id: str) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_schedule_task WHERE task_id = %s AND is_deleted = 'N'",
                (task_id,),
            )
            return cur.fetchone() is not None

    # ----- list reads -----------------------------------------------------

    def list_lists(self, *, since: int | None = None) -> tuple[list[TaskList], int]:
        server_now = now_ms()
        if since is not None:
            sql = (
                "SELECT task_list_id, title, last_modified, is_deleted "
                "FROM t_schedule_task_group "
                "WHERE last_modified > %s AND last_modified <= %s "
                "ORDER BY last_modified ASC"
            )
            params: tuple[Any, ...] = (since, server_now)
            include_inbox = False
        else:
            sql = (
                "SELECT task_list_id, title, last_modified, is_deleted "
                "FROM t_schedule_task_group WHERE is_deleted = 'N' ORDER BY title"
            )
            params = ()
            include_inbox = True

        with self._db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        lists = [self._row_to_list(r) for r in rows]
        if include_inbox:
            lists.insert(0, TaskList(id=None, title="Inbox", last_modified=0, is_deleted=False))
        return lists, server_now

    def list_exists(self, list_id: str) -> bool:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM t_schedule_task_group WHERE task_list_id = %s AND is_deleted = 'N'",
                (list_id,),
            )
            return cur.fetchone() is not None

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

    def update_list(self, list_id: str, title: str) -> bool:
        sql = (
            "UPDATE t_schedule_task_group SET title = %s, last_modified = %s "
            "WHERE task_list_id = %s AND is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, (encode_emoji(title), now_ms(), list_id)))
        return affected > 0

    def delete_list(self, list_id: str) -> bool:
        sql = (
            "UPDATE t_schedule_task_group SET is_deleted = 'Y', last_modified = %s "
            "WHERE task_list_id = %s AND is_deleted = 'N'"
        )
        with self._db.cursor() as cur:
            affected = int(cur.execute(sql, (now_ms(), list_id)))
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
