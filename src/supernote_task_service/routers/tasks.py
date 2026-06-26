"""Task endpoints, including incremental (``since``) sync."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Response, status

from ..deps import CallerDep, RepositoryDep, SettingsDep, ensure_cursor_fresh
from ..errors import ApiError
from ..models import (
    Task,
    TaskCreate,
    TaskListPage,
    TaskStatus,
    TaskUpdate,
)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

TaskId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
ListIdQuery = Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")]
# Optimistic-concurrency precondition: the client's last-known ``last_modified``
# as Unix milliseconds (numeric, not an HTTP-date). Absent => unconditional.
IfUnmodifiedSince = Annotated[
    int | None,
    Header(alias="If-Unmodified-Since", ge=0, description="Expected last_modified (Unix ms)."),
]


def _ensure_list_exists(repo: RepositoryDep, list_id: str | None) -> None:
    if list_id is not None and not repo.list_exists(list_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")


def _resolve_write(repo: RepositoryDep, task_id: str, ok: bool, conditional: bool) -> None:
    """Translate a write outcome into 404 (missing) or 409 (stale precondition)."""
    if ok:
        return
    if conditional and repo.task_exists(task_id):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "Task was modified since the supplied version.",
            code="conflict",
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")


@router.get("", response_model=TaskListPage, summary="List or sync tasks")
def list_tasks(
    repo: RepositoryDep,
    settings: SettingsDep,
    _caller: CallerDep,
    since: Annotated[int | None, Query(ge=0, description="Unix ms cursor for delta sync.")] = None,
    list_id: ListIdQuery = None,
    inbox: Annotated[bool, Query(description="Only Inbox tasks (task_list_id IS NULL).")] = False,
    include_completed: bool = True,
    limit: Annotated[int | None, Query(ge=1, le=1000, description="Max rows to return.")] = None,
) -> TaskListPage:
    ensure_cursor_fresh(since, settings)
    tasks, cursor, has_more = repo.list_tasks(
        since=since,
        list_id=list_id,
        include_completed=include_completed,
        inbox_only=inbox,
        limit=limit,
    )
    return TaskListPage(tasks=tasks, cursor=cursor, has_more=has_more)


@router.post(
    "",
    response_model=Task,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
)
def create_task(repo: RepositoryDep, _caller: CallerDep, body: TaskCreate) -> Task:
    _ensure_list_exists(repo, body.list_id)
    task_id = repo.create_task(body)
    return _require_task(repo, task_id)


@router.get("/{task_id}", response_model=Task, summary="Get a task")
def get_task(repo: RepositoryDep, _caller: CallerDep, task_id: TaskId) -> Task:
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task


@router.patch("/{task_id}", response_model=Task, summary="Update a task")
def update_task(
    repo: RepositoryDep,
    _caller: CallerDep,
    body: TaskUpdate,
    task_id: TaskId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> Task:
    if "list_id" in body.model_fields_set:
        _ensure_list_exists(repo, body.list_id)
    ok = repo.update_task(task_id, body, expected_last_modified=if_unmodified_since)
    _resolve_write(repo, task_id, ok, if_unmodified_since is not None)
    return _require_task(repo, task_id)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a task",
)
def delete_task(
    repo: RepositoryDep,
    _caller: CallerDep,
    task_id: TaskId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> Response:
    ok = repo.delete_task(task_id, expected_last_modified=if_unmodified_since)
    _resolve_write(repo, task_id, ok, if_unmodified_since is not None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/complete", response_model=Task, summary="Mark task completed")
def complete_task(
    repo: RepositoryDep,
    _caller: CallerDep,
    task_id: TaskId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> Task:
    ok = repo.set_status(task_id, TaskStatus.completed, expected_last_modified=if_unmodified_since)
    _resolve_write(repo, task_id, ok, if_unmodified_since is not None)
    return _require_task(repo, task_id)


@router.post("/{task_id}/uncomplete", response_model=Task, summary="Mark task active")
def uncomplete_task(
    repo: RepositoryDep,
    _caller: CallerDep,
    task_id: TaskId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> Task:
    ok = repo.set_status(
        task_id, TaskStatus.needs_action, expected_last_modified=if_unmodified_since
    )
    _resolve_write(repo, task_id, ok, if_unmodified_since is not None)
    return _require_task(repo, task_id)


def _require_task(repo: RepositoryDep, task_id: str) -> Task:
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task
