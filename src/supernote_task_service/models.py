"""Pydantic request and response models for the public API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Field length limits mirror the underlying MariaDB columns.
TITLE_MAX = 600
DETAIL_MAX = 255
LIST_TITLE_MAX = 255
LINKS_MAX = 5000

IdStr = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class TaskStatus(StrEnum):
    needs_action = "needsAction"
    completed = "completed"


class DocumentLink(BaseModel):
    """A link from a task to a page in a Supernote notebook."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    app_name: str = Field(default="note", max_length=64, alias="appName")
    file_id: str = Field(max_length=255, alias="fileId")
    file_path: str = Field(max_length=1024, alias="filePath")
    page: int = Field(ge=0)
    page_id: str = Field(max_length=255, alias="pageId")


class TaskListCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=LIST_TITLE_MAX)


class TaskListUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=LIST_TITLE_MAX)


class TaskList(BaseModel):
    id: str | None = Field(description="List ID; null for the implicit Inbox.")
    title: str
    last_modified: int = Field(description="Unix epoch milliseconds.")
    is_deleted: bool = False


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_MAX)
    detail: str = Field(default="", max_length=DETAIL_MAX)
    list_id: IdStr | None = Field(default=None, description="Null for the Inbox.")
    status: TaskStatus = TaskStatus.needs_action
    due: datetime | None = None
    document_link: DocumentLink | None = None


class TaskUpdate(BaseModel):
    """Partial update. Omitted fields are left unchanged.

    ``due`` and ``document_link`` accept null to *clear* the value, so the
    presence of each field is tracked explicitly via ``model_fields_set``.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    detail: str | None = Field(default=None, max_length=DETAIL_MAX)
    list_id: IdStr | None = None
    status: TaskStatus | None = None
    due: datetime | None = None
    document_link: DocumentLink | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> TaskUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


class Task(BaseModel):
    id: str
    list_id: str | None
    category: str
    title: str
    detail: str
    status: TaskStatus
    due: datetime | None
    completed: datetime | None
    last_modified: int = Field(description="Unix epoch milliseconds.")
    document_link: DocumentLink | None = None
    is_deleted: bool = False


class TaskListPage(BaseModel):
    """A page of tasks plus the cursor for the next incremental sync call."""

    tasks: list[Task]
    cursor: int = Field(description="Pass as ?since= on the next call (Unix ms).")


class TaskListsPage(BaseModel):
    lists: list[TaskList]
    cursor: int = Field(description="Pass as ?since= on the next call (Unix ms).")


class CreatedId(BaseModel):
    id: str


class ErrorResponse(BaseModel):
    detail: str
