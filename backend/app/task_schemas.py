from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.schemas import DocumentListItem


class TaskEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    actor: str
    action: str
    event_data: dict[str, Any]
    created_at: datetime


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_type: str
    status: str
    priority: str
    document_id: int
    invoice_id: int | None
    reason: str | None
    assigned_to: str | None
    resolution: str | None
    resolution_notes: str | None
    started_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    document: DocumentListItem
    events: list[TaskEventResponse] = Field(
        default_factory=list,
    )


class TaskResolveRequest(BaseModel):
    resolution: str = Field(
        min_length=2,
        max_length=100,
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
    )


class TaskActionResponse(BaseModel):
    success: bool
    message: str
    task: TaskResponse