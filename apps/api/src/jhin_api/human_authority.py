"""Attach authenticated request provenance before a human input is persisted."""

from typing import Any

from jhin_api.deps import WorkspaceContext
from jhin_secrets.authority import attest_human_content


def human_content(ctx: WorkspaceContext, content: dict[str, Any]) -> dict[str, Any]:
    key = ctx.api_key
    return attest_human_content(
        content,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        role=ctx.role.value,
        api_key_id=key.id if key else None,
        scopes=key.scopes if key else frozenset(),
    )
