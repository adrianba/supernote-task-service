"""Task list (category) endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from ..deps import CallerDep, RepositoryDep
from ..models import CreatedId, TaskList, TaskListCreate, TaskListsPage, TaskListUpdate

router = APIRouter(prefix="/v1/lists", tags=["lists"])

ListId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


@router.get("", response_model=TaskListsPage, summary="List categories")
def list_lists(
    repo: RepositoryDep,
    _caller: CallerDep,
    since: Annotated[int | None, Query(ge=0, description="Unix ms cursor for delta sync.")] = None,
    limit: Annotated[int | None, Query(ge=1, le=1000, description="Max rows to return.")] = None,
) -> TaskListsPage:
    lists, cursor = repo.list_lists(since=since, limit=limit)
    return TaskListsPage(lists=lists, cursor=cursor)


@router.post(
    "",
    response_model=CreatedId,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
)
def create_list(repo: RepositoryDep, _caller: CallerDep, body: TaskListCreate) -> CreatedId:
    list_id = repo.create_list(body.title)
    return CreatedId(id=list_id)


@router.patch("/{list_id}", response_model=TaskList, summary="Rename a category")
def update_list(
    repo: RepositoryDep,
    _caller: CallerDep,
    body: TaskListUpdate,
    list_id: ListId,
) -> TaskList:
    if not repo.update_list(list_id, body.title):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
    lists, _ = repo.list_lists()
    for item in lists:
        if item.id == list_id:
            return item
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")


@router.delete(
    "/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a category",
)
def delete_list(repo: RepositoryDep, _caller: CallerDep, list_id: ListId) -> Response:
    if not repo.delete_list(list_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
