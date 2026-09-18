"""Only authenticated human admins can create prospective capture authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from jhin_api.deps import WorkspaceContext
from jhin_domain import WorkspaceRole, new_uuid7


async def test_capture_create_is_prospective_and_company_maps_to_workspace(session, admin_ctx):
    from jhin_api.memory_capture import (
        CapturePolicyCreate,
        create_capture_policy,
        revoke_capture_policy,
    )
    from jhin_db.models import Agent

    agent = Agent(workspace_id=admin_ctx.workspace_id, name="Writer", slug="writer")
    session.add(agent)
    await session.flush()
    before = datetime.now(UTC)
    result = await create_capture_policy(
        CapturePolicyCreate(
            scope="company",
            scope_id=admin_ctx.workspace_id,
            actor_ids=[agent.id],
            allowed_classes=["company_fact"],
        ),
        admin_ctx,
        session,
    )
    assert result.scope == "workspace"
    assert result.effective_from >= before
    assert result.source_user_id == admin_ctx.user.id
    revoked = await revoke_capture_policy(result.id, admin_ctx, session)
    assert revoked.revoked_at is not None


async def test_capture_cannot_be_backdated_or_created_by_member(session, admin_ctx):
    from pydantic import ValidationError

    from jhin_api.memory_capture import CapturePolicyCreate, create_capture_policy

    values = {
        "scope": "team",
        "scope_id": new_uuid7(),
        "actor_ids": [new_uuid7()],
        "allowed_classes": ["editorial_style"],
    }
    with pytest.raises(ValidationError):
        CapturePolicyCreate(**values, effective_from=datetime.now(UTC) - timedelta(days=1))
    member: WorkspaceContext = replace(admin_ctx, role=WorkspaceRole.MEMBER)
    with pytest.raises(HTTPException) as rejected:
        await create_capture_policy(CapturePolicyCreate(**values), member, session)
    assert rejected.value.status_code == 403
