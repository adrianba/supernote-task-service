"""Shared pytest fixtures: an in-memory repository and a configured TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from supernote_task_service.config import Settings
from supernote_task_service.deps import get_repository
from supernote_task_service.encoding import (
    datetime_to_ms,
    decode_emoji,
    encode_emoji,
    ms_to_datetime,
    new_id,
    now_ms,
)
from supernote_task_service.main import create_app
from supernote_task_service.models import (
    Task,
    TaskCreate,
    TaskList,
    TaskStatus,
    TaskUpdate,
)

API_KEY = "test-key"


class FakeRepository:
    """In-memory stand-in for :class:`Repository` used in tests."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lists: dict[str, dict] = {}

    # tasks
    def list_tasks(
        self,
        *,
        since=None,
        list_id=None,
        include_completed=True,
        inbox_only=False,
        limit=None,
    ) -> tuple[list[Task], int, bool]:
        cursor = now_ms()
        effective_limit = min(limit or 500, 1000)
        result = []
        for t in self._tasks.values():
            if since is not None:
                if not (since <= t["last_modified"] <= cursor):
                    continue
            else:
                if t["is_deleted"]:
                    continue
                if not include_completed and t["status"] == TaskStatus.completed:
                    continue
            if inbox_only and t["list_id"] is not None:
                continue
            if not inbox_only and list_id is not None and t["list_id"] != list_id:
                continue
            result.append(self._to_task(t))
        result.sort(key=lambda x: (x.last_modified, x.id))
        page = result[:effective_limit]
        if (
            since is not None
            and len(page) == effective_limit
            and page
            and page[0].last_modified == page[-1].last_modified
        ):
            boundary = page[-1].last_modified
            page = page + [t for t in result[effective_limit:] if t.last_modified == boundary]
            return page, boundary + 1, False
        has_more = len(page) == effective_limit and bool(page)
        if has_more:
            cursor = page[-1].last_modified
        return page, cursor, has_more

    def get_task(self, task_id: str) -> Task | None:
        t = self._tasks.get(task_id)
        if t is None or t["is_deleted"]:
            return None
        return self._to_task(t)

    def task_exists(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        return t is not None and not t["is_deleted"]

    def create_task(self, data: TaskCreate) -> str:
        tid = new_id()
        ts = now_ms()
        if data.sort is not None:
            sort = data.sort
        else:
            siblings = [
                t["sort"]
                for t in self._tasks.values()
                if not t["is_deleted"] and t["list_id"] == data.list_id and t["sort"] is not None
            ]
            sort = (max(siblings) + 1) if siblings else 0
        self._tasks[tid] = {
            "id": tid,
            "list_id": data.list_id,
            "title": encode_emoji(data.title),
            "detail": data.detail,
            "status": data.status,
            "importance": data.importance,
            "due": datetime_to_ms(data.due) if data.due else 0,
            "completed": ts if data.status == TaskStatus.completed else 0,
            "sort": sort,
            "last_modified": ts,
            "document_link": data.document_link,
            "is_deleted": False,
        }
        return tid

    def update_task(self, task_id: str, data: TaskUpdate, *, expected_last_modified=None) -> bool:
        t = self._tasks.get(task_id)
        if t is None or t["is_deleted"]:
            return False
        if expected_last_modified is not None and t["last_modified"] != expected_last_modified:
            return False
        fields = data.model_fields_set
        if "title" in fields and data.title is not None:
            t["title"] = encode_emoji(data.title)
        if "detail" in fields and data.detail is not None:
            t["detail"] = data.detail
        if "status" in fields and data.status is not None:
            t["status"] = data.status
            t["completed"] = now_ms() if data.status == TaskStatus.completed else 0
        if "importance" in fields:
            t["importance"] = data.importance
        if "due" in fields:
            t["due"] = datetime_to_ms(data.due) if data.due else 0
        if "sort" in fields and data.sort is not None:
            t["sort"] = data.sort
        if "list_id" in fields:
            t["list_id"] = data.list_id
        if "document_link" in fields:
            t["document_link"] = data.document_link
        t["last_modified"] = now_ms()
        return True

    def set_status(self, task_id: str, status: TaskStatus, *, expected_last_modified=None) -> bool:
        return self.update_task(
            task_id, TaskUpdate(status=status), expected_last_modified=expected_last_modified
        )

    def delete_task(self, task_id: str, *, expected_last_modified=None) -> bool:
        t = self._tasks.get(task_id)
        if t is None or t["is_deleted"]:
            return False
        if expected_last_modified is not None and t["last_modified"] != expected_last_modified:
            return False
        t["is_deleted"] = True
        t["last_modified"] = now_ms()
        return True

    # lists
    def list_lists(self, *, since=None, limit=None) -> tuple[list[TaskList], int, bool]:
        cursor = now_ms()
        effective_limit = min(limit or 500, 1000)
        out = []
        for ls in self._lists.values():
            if since is not None:
                if not (since <= ls["last_modified"] <= cursor):
                    continue
            elif ls["is_deleted"]:
                continue
            out.append(self._to_list(ls))
        out.sort(key=lambda x: (x.last_modified, x.id or ""))
        page = out[:effective_limit]
        if (
            since is not None
            and len(page) == effective_limit
            and page
            and page[0].last_modified == page[-1].last_modified
        ):
            boundary = page[-1].last_modified
            page = page + [ls for ls in out[effective_limit:] if ls.last_modified == boundary]
            return page, boundary + 1, False
        has_more = len(page) == effective_limit and bool(page)
        if has_more:
            cursor = page[-1].last_modified
        if since is None:
            page.insert(0, TaskList(id=None, title="Inbox", last_modified=0, is_deleted=False))
        return page, cursor, has_more

    def list_exists(self, list_id: str) -> bool:
        ls = self._lists.get(list_id)
        return ls is not None and not ls["is_deleted"]

    def get_list(self, list_id: str) -> TaskList | None:
        ls = self._lists.get(list_id)
        if ls is None or ls["is_deleted"]:
            return None
        return self._to_list(ls)

    def get_list_by_title(self, title: str) -> TaskList | None:
        encoded = encode_emoji(title)
        matches = [
            self._to_list(ls)
            for ls in self._lists.values()
            if not ls["is_deleted"] and ls["title"] == encoded
        ]
        matches.sort(key=lambda x: (x.last_modified, x.id or ""))
        return matches[0] if matches else None

    def create_list(self, title: str) -> str:
        lid = new_id()
        self._lists[lid] = {
            "id": lid,
            "title": encode_emoji(title),
            "last_modified": now_ms(),
            "is_deleted": False,
        }
        return lid

    def update_list(self, list_id: str, title: str, *, expected_last_modified=None) -> bool:
        ls = self._lists.get(list_id)
        if ls is None or ls["is_deleted"]:
            return False
        if expected_last_modified is not None and ls["last_modified"] != expected_last_modified:
            return False
        ls["title"] = encode_emoji(title)
        ls["last_modified"] = now_ms()
        return True

    def delete_list(self, list_id: str, *, expected_last_modified=None) -> bool:
        ls = self._lists.get(list_id)
        if ls is None or ls["is_deleted"]:
            return False
        if expected_last_modified is not None and ls["last_modified"] != expected_last_modified:
            return False
        ls["is_deleted"] = True
        ls["last_modified"] = now_ms()
        return True

    # mapping
    @staticmethod
    def _to_task(t: dict) -> Task:
        return Task(
            id=t["id"],
            list_id=t["list_id"],
            category="Inbox" if t["list_id"] is None else "List",
            title=decode_emoji(t["title"]),
            detail=t["detail"],
            status=t["status"],
            importance=t.get("importance"),
            due=ms_to_datetime(t["due"]),
            completed=ms_to_datetime(t["completed"]),
            sort=t.get("sort"),
            last_modified=t["last_modified"],
            document_link=t["document_link"],
            is_deleted=t["is_deleted"],
        )

    @staticmethod
    def _to_list(ls: dict) -> TaskList:
        return TaskList(
            id=ls["id"],
            title=decode_emoji(ls["title"]),
            last_modified=ls["last_modified"],
            is_deleted=ls["is_deleted"],
        )


@pytest.fixture
def _stub_user_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid hitting a real database when the app lifespan resolves the user.

    The API tests exercise the routes through ``FakeRepository``; the eager
    startup resolution in :func:`main._lifespan` would otherwise try to open a
    real MariaDB connection.
    """
    monkeypatch.setattr(
        "supernote_task_service.db.Database.get_user_id",
        lambda self: 1,
    )


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(repo: FakeRepository, _stub_user_resolution: None):
    settings = Settings(
        SUPERNOTE_DB_PASSWORD="unused",
        API_KEYS=API_KEY,
        RATE_LIMIT_REQUESTS=1000,
        RATE_LIMIT_WINDOW_SECONDS=60,
    )
    app = create_app(settings)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def low_rate_client(repo: FakeRepository, _stub_user_resolution: None):
    settings = Settings(
        SUPERNOTE_DB_PASSWORD="unused",
        API_KEYS=API_KEY,
        RATE_LIMIT_REQUESTS=5,
        RATE_LIMIT_WINDOW_SECONDS=60,
    )
    app = create_app(settings)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}
