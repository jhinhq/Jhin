"""Response contracts for audit queries (plan 17.12, 23)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class AuditEventOut(BaseModel):
    id: UUID
    workspace_id: UUID | None
    actor_type: str
    actor_id: UUID | None
    action: str
    target_type: str
    target_id: UUID | None
    metadata_json: dict[str, Any]
    request_id: UUID | None
    created_at: datetime


class AuditEventPage(BaseModel):
    events: list[AuditEventOut]
    total: int
