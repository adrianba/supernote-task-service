"""Task list (category) endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Response, status

from ..deps import CallerDep, RepositoryDep, SettingsDep, ensure_cursor_fresh
from ..errors import ApiError
from ..models import TaskList, TaskListCreate, TaskListsPage, TaskListUpdate

router = APIRouter(prefix="/v1/lists", tags=["lists"])

ListId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
IfUnmodifiedSince = Annotated[
    int | None,
    Header(alias="If-Unmodified-Since", ge=0, description="Expected last_modified (Unix ms)."),
]


def _resolve_write(repo: RepositoryDep, list_id: str, ok: bool, conditional: bool) -> None:
    """Translate a write outcome into 404 (missing) or 409 (stale precondition)."""
    if ok:
        return
    if conditional and repo.list_exists(list_id):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "List was modified since the supplied version.",
            code="conflict",
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")


@router.get("", response_model=TaskListsPage, summary="List categories")
def list_lists(
    repo: RepositoryDep,
    settings: SettingsDep,
    _caller: CallerDep,
    since: Annotated[int | None, Query(ge=0, description="Unix ms cursor for delta sync.")] = None,
    limit: Annotated[int | None, Query(ge=1, le=1000, description="Max rows to return.")] = None,
    title: Annotated[
        str | None, Query(description="Return only the list with this exact title.")
    ] = None,
) -> TaskListsPage:
    if title is not None:
        match = repo.get_list_by_title(title)
        if match is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
        return TaskListsPage(lists=[match], cursor=match.last_modified, has_more=False)
    ensure_cursor_fresh(since, settings)
    lists, cursor, has_more = repo.list_lists(since=since, limit=limit)
    return TaskListsPage(lists=lists, cursor=cursor, has_more=has_more)


@router.post(
    "",
    response_model=TaskList,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category (idempotent by title)",
)
def create_list(
    repo: RepositoryDep,
    _caller: CallerDep,
    body: TaskListCreate,
    response: Response,
) -> TaskList:
    existing = repo.get_list_by_title(body.title)
    if existing is not None:
        # Idempotent: a non-deleted list with this title already exists.
        response.status_code = status.HTTP_200_OK
        return existing
    list_id = repo.create_list(body.title)
    created = repo.get_list(list_id)
    if created is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
    return created


@router.patch("/{list_id}", response_model=TaskList, summary="Rename a category")
def update_list(
    repo: RepositoryDep,
    _caller: CallerDep,
    body: TaskListUpdate,
    list_id: ListId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> TaskList:
    ok = repo.update_list(list_id, body.title, expected_last_modified=if_unmodified_since)
    _resolve_write(repo, list_id, ok, if_unmodified_since is not None)
    updated = repo.get_list(list_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
    return updated


@router.delete(
    "/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a category",
)
def delete_list(
    repo: RepositoryDep,
    _caller: CallerDep,
    list_id: ListId,
    if_unmodified_since: IfUnmodifiedSince = None,
) -> Response:
    ok = repo.delete_list(list_id, expected_last_modified=if_unmodified_since)
    _resolve_write(repo, list_id, ok, if_unmodified_since is not None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
