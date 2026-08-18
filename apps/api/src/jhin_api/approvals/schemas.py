"""Schemas for the approvals inbox and decisions (plan 6.16, 17.11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer

from jhin_api.public_payloads import public_tool_payload


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID | None
    run_id: UUID | None
    requested_by_agent_id: UUID | None
    action_type: str
    action_payload_sanitized: dict[str, Any]
    reason: str
    status: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by_user_id: UUID | None

    @field_serializer("action_payload_sanitized")
    def serialize_action_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        return public_tool_payload(self.action_type, value)


class ApprovalListItem(ApprovalOut):
    agent_name: str | None = None
    task_title: str | None = None


class ApprovalListOut(BaseModel):
    items: list[ApprovalListItem]
    total: int
    pending_count: int
