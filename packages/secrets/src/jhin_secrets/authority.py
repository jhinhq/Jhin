"""Server-attested human requests retain their original permission ceiling.

Attestations are written only by authenticated ingress, never taken from a
client's message JSON or an agent's tool arguments. They convey no capability;
privileged consumers must also check current membership and credential state.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import ApiKey, Message, User, WorkspaceMembership
from jhin_domain import WorkspaceRole, effective_scopes

AUTHORITY_KEY = "_human_authority"


def attest_human_content(
    content: dict[str, Any],
    *,
    workspace_id: UUID,
    user_id: UUID,
    role: str,
    api_key_id: UUID | None = None,
    scopes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Internal ingress helper; callers must supply authenticated server state."""
    return {
        **content,
        AUTHORITY_KEY: {
            "version": 1,
            "workspace_id": str(workspace_id),
            "user_id": str(user_id),
            "role": role,
            "source": "api_key" if api_key_id else "browser",
            "api_key_id": str(api_key_id) if api_key_id else None,
            "scopes": sorted(scopes) if api_key_id else [],
        },
    }


async def human_content_authorized(
    session: AsyncSession,
    workspace_id: UUID,
    user_id: UUID | None,
    content: dict[str, Any],
    *,
    required_scope: str,
) -> bool:
    attestation = content.get(AUTHORITY_KEY)
    if (
        not user_id
        or not isinstance(attestation, dict)
        or not (
            attestation.get("version") == 1
            and attestation.get("workspace_id") == str(workspace_id)
            and attestation.get("user_id") == str(user_id)
            and attestation.get("role") in {"owner", "admin"}
        )
    ):
        return False
    current = await session.scalar(
        select(WorkspaceMembership.role)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
            User.status == "active",
        )
    )
    if current not in {"owner", "admin"}:
        return False
    if attestation.get("source") == "browser":
        return attestation.get("api_key_id") is None
    if attestation.get("source") != "api_key":
        return False
    try:
        key_id = UUID(str(attestation.get("api_key_id")))
    except ValueError:
        return False
    initial_scopes = attestation.get("scopes")
    if not isinstance(initial_scopes, list) or required_scope not in initial_scopes:
        return False
    key = await session.scalar(
        select(ApiKey)
        .where(
            ApiKey.id == key_id,
            ApiKey.workspace_id == workspace_id,
            ApiKey.created_by_user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    if key is None or key.revoked_at is not None or key.role_ceiling not in {"owner", "admin"}:
        return False
    expiry = key.expires_at
    if expiry and (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)) <= datetime.now(UTC):
        return False
    return required_scope in effective_scopes(key.scopes_json, WorkspaceRole(key.role_ceiling))


async def human_message_authorized(
    session: AsyncSession,
    message: Message,
    *,
    required_scope: str,
) -> bool:
    return message.sender_type == "user" and await human_content_authorized(
        session,
        message.workspace_id,
        message.sender_id,
        message.content_json,
        required_scope=required_scope,
    )
