"""Task endpoints, including incremental (``since``) sync."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from ..deps import CallerDep, RepositoryDep
from ..models import (
    CreatedId,
    Task,
    TaskCreate,
    TaskListPage,
    TaskStatus,
    TaskUpdate,
)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

TaskId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
ListIdQuery = Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")]


def _ensure_list_exists(repo: RepositoryDep, list_id: str | None) -> None:
    if list_id is not None and not repo.list_exists(list_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")


@router.get("", response_model=TaskListPage, summary="List or sync tasks")
def list_tasks(
    repo: RepositoryDep,
    _caller: CallerDep,
    since: Annotated[int | None, Query(ge=0, description="Unix ms cursor for delta sync.")] = None,
    list_id: ListIdQuery = None,
    inbox: Annotated[bool, Query(description="Only Inbox tasks (task_list_id IS NULL).")] = False,
    include_completed: bool = True,
    limit: Annotated[int | None, Query(ge=1, le=1000, description="Max rows to return.")] = None,
) -> TaskListPage:
    tasks, cursor = repo.list_tasks(
        since=since,
        list_id=list_id,
        include_completed=include_completed,
        inbox_only=inbox,
        limit=limit,
    )
    return TaskListPage(tasks=tasks, cursor=cursor)


@router.post(
    "",
    response_model=CreatedId,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
)
def create_task(repo: RepositoryDep, _caller: CallerDep, body: TaskCreate) -> CreatedId:
    _ensure_list_exists(repo, body.list_id)
    task_id = repo.create_task(body)
    return CreatedId(id=task_id)


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
) -> Task:
    if "list_id" in body.model_fields_set:
        _ensure_list_exists(repo, body.list_id)
    if not repo.update_task(task_id, body):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return _require_task(repo, task_id)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a task",
)
def delete_task(repo: RepositoryDep, _caller: CallerDep, task_id: TaskId) -> Response:
    if not repo.delete_task(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/complete", response_model=Task, summary="Mark task completed")
def complete_task(repo: RepositoryDep, _caller: CallerDep, task_id: TaskId) -> Task:
    if not repo.set_status(task_id, TaskStatus.completed):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return _require_task(repo, task_id)


@router.post("/{task_id}/uncomplete", response_model=Task, summary="Mark task active")
def uncomplete_task(repo: RepositoryDep, _caller: CallerDep, task_id: TaskId) -> Task:
    if not repo.set_status(task_id, TaskStatus.needs_action):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return _require_task(repo, task_id)


def _require_task(repo: RepositoryDep, task_id: str) -> Task:
    task = repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task
