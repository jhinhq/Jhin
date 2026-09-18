"""Authorization for plaintext settings and write-only sensitive variables.

No public method reveals sensitive material. ``resolve_bound`` is the trusted
connector boundary and must never be registered as a tool or HTTP endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import (
    Agent,
    AuditEvent,
    Connection,
    Secret,
    Team,
    Workspace,
)
from jhin_db.models.variables import ScopedVariable, SecureInputCapture, VariableConnectionBinding
from jhin_domain import new_uuid7
from jhin_secrets.crypto import SecretCrypto
from jhin_secrets.store import SecretStore


class VariableError(ValueError):
    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_value(value: str, *, allow_empty: bool = False) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise VariableError("Variable value exceeds its format or size limit", 422) from None
    if size > 8192 or "\x00" in value or (not size and not allow_empty):
        raise VariableError("Variable value exceeds its format or size limit", 422)


@dataclass(frozen=True)
class VariableActor:
    workspace_id: UUID
    actor_type: str
    actor_id: UUID
    is_admin: bool = False
    # Exact scopes covered by the tool gateway's current allow grants.
    write_scopes: frozenset[tuple[str, UUID]] = frozenset()
    conversation_id: UUID | None = None


def origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise VariableError("Invalid approved origin", 422) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise VariableError("Invalid approved origin", 422)
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")


def canonical_admin_url(value: str) -> str:
    """Normalize the exact Ghost install URL without outbound I/O or guessing."""
    if not isinstance(value, str) or any(
        ord(char) <= 32 or ord(char) == 127 or char in "\\%?#" for char in value
    ):
        raise VariableError("Invalid approved Admin URL", 422)
    base = origin(value)
    path = urlsplit(value).path.rstrip("/")
    for suffix in ("/ghost/api/admin", "/ghost"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if "//" in path or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in path.split("/") if part
    ):
        raise VariableError("Invalid approved Admin URL", 422)
    return base + path


class VariableStore:
    """Flush-only operations so callers can commit settings and receipts together."""

    def __init__(self, session: AsyncSession, crypto: SecretCrypto | None = None) -> None:
        self.session = session
        self.crypto = crypto

    async def _reveal_current(
        self, workspace_id: UUID, secret_id: UUID, *, record_use: bool = True
    ) -> str:
        if self.crypto is None:
            raise VariableError("Secret encryption is unavailable", 503)
        current = await self.session.scalar(
            select(Secret)
            .where(Secret.id == secret_id, Secret.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
        if current is None:
            raise VariableError("Stored connection credential is unavailable", 503)
        store = SecretStore(self.session, self.crypto)
        if record_use:
            return await store.reveal(workspace_id, secret_id)
        return await store.reveal(workspace_id, secret_id, record_use=False)

    async def _teams(self, actor: VariableActor) -> set[UUID]:
        # Shared DB helper is intentionally independent of memory extraction.
        from jhin_db.memberships import active_team_ids

        return set(await active_team_ids(self.session, actor.workspace_id, actor.actor_id))

    async def _authorize(
        self, actor: VariableActor, scope: str, scope_id: UUID, *, write: bool = False
    ) -> None:
        if scope not in {"agent", "team", "company"}:
            raise VariableError("Invalid variable scope", 422)
        if scope == "agent":
            query = select(Agent.id).where(
                Agent.id == scope_id, Agent.workspace_id == actor.workspace_id
            )
        elif scope == "team":
            query = select(Team.id).where(
                Team.id == scope_id, Team.workspace_id == actor.workspace_id
            )
        else:
            query = select(Workspace.id).where(
                Workspace.id == scope_id, Workspace.id == actor.workspace_id
            )
        if await self.session.scalar(query) is None:
            raise VariableError("Variable scope not found", 404)
        if actor.actor_type == "user":
            if not actor.is_admin:
                raise VariableError("Variable management requires admin authority")
            return
        if (
            actor.actor_type != "agent"
            or await self.session.scalar(
                select(Agent.id).where(
                    Agent.id == actor.actor_id,
                    Agent.workspace_id == actor.workspace_id,
                    Agent.status == "active",
                )
            )
            is None
        ):
            raise VariableError("Variable not found", 404)
        accessible = (
            (scope == "agent" and scope_id == actor.actor_id)
            or scope == "company"
            or (scope == "team" and scope_id in await self._teams(actor))
        )
        if not accessible:
            raise VariableError("Variable not found", 404)
        if write and scope != "agent" and (scope, scope_id) not in actor.write_scopes:
            raise VariableError("Changing shared variables requires scoped authority")

    async def _lock(self, workspace_id: UUID) -> None:
        # NO KEY UPDATE preserves foreign-key progress in conversation journals.
        await self.session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update(key_share=True)
        )

    async def get(self, actor: VariableActor, variable_id: UUID | str) -> ScopedVariable:
        try:
            target = UUID(str(variable_id))
        except ValueError:
            raise VariableError("Variable not found", 404) from None
        row = await self.session.scalar(
            select(ScopedVariable)
            .where(ScopedVariable.id == target, ScopedVariable.workspace_id == actor.workspace_id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise VariableError("Variable not found", 404)
        await self._authorize(actor, row.scope, row.scope_id)
        return row

    async def list(
        self,
        actor: VariableActor,
        *,
        scope: str | None = None,
        scope_id: UUID | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[ScopedVariable]:
        query = await self._visible_query(actor, scope, scope_id)
        rows = await self.session.scalars(
            query.order_by(ScopedVariable.created_at, ScopedVariable.id)
            .offset(max(offset, 0))
            .limit(max(0, min(limit, 1000)))
        )
        return list(rows)

    async def count(
        self, actor: VariableActor, *, scope: str | None = None, scope_id: UUID | None = None
    ) -> int:
        query = await self._visible_query(actor, scope, scope_id)
        return int(
            await self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        )

    async def _visible_query(
        self, actor: VariableActor, scope: str | None, scope_id: UUID | None
    ) -> Select[tuple[ScopedVariable]]:
        await self._authorize(actor, "company", actor.workspace_id)
        query = select(ScopedVariable).where(ScopedVariable.workspace_id == actor.workspace_id)
        if scope is not None:
            query = query.where(ScopedVariable.scope == scope)
        if scope_id is not None:
            query = query.where(ScopedVariable.scope_id == scope_id)
        if actor.actor_type == "agent":
            query = query.where(
                or_(
                    ScopedVariable.scope == "company",
                    and_(
                        ScopedVariable.scope == "agent", ScopedVariable.scope_id == actor.actor_id
                    ),
                    and_(
                        ScopedVariable.scope == "team",
                        ScopedVariable.scope_id.in_(await self._teams(actor)),
                    ),
                )
            )
        return query

    @staticmethod
    def public(row: ScopedVariable) -> dict[str, Any]:
        result = {
            key: getattr(row, key)
            for key in (
                "id",
                "workspace_id",
                "name",
                "scope",
                "scope_id",
                "sensitive",
                "version",
                "source_variable_id",
                "source_version",
                "description",
                "created_by_type",
                "created_by_id",
                "updated_by_type",
                "updated_by_id",
                "created_at",
                "updated_at",
            )
        }
        result["configured"] = (
            row.secret_id is not None if row.sensitive else row.plaintext is not None
        )
        if not row.sensitive:
            result["value"] = row.plaintext
        return result

    def _audit(self, actor: VariableActor, action: str, row: ScopedVariable) -> None:
        self.session.add(
            AuditEvent(
                workspace_id=actor.workspace_id,
                actor_type=actor.actor_type,
                actor_id=actor.actor_id,
                action=action,
                target_type="scoped_variable",
                target_id=row.id,
                metadata_json={
                    "scope": row.scope,
                    "scope_id": str(row.scope_id),
                    "version": row.version,
                    "sensitive": row.sensitive,
                },
            )
        )

    async def _capture(self, actor: VariableActor, secret_ref: str | UUID) -> SecureInputCapture:
        try:
            target = UUID(str(secret_ref).removeprefix("secure_input:"))
        except ValueError:
            raise VariableError("Secure input not found", 404) from None
        row = await self.session.scalar(
            select(SecureInputCapture)
            .where(
                SecureInputCapture.id == target,
                SecureInputCapture.workspace_id == actor.workspace_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or (actor.actor_type == "agent" and row.agent_id != actor.actor_id)
            or (actor.actor_type == "agent" and row.conversation_id != actor.conversation_id)
            or (actor.actor_type == "user" and row.user_id != actor.actor_id)
        ):
            raise VariableError("Secure input not found", 404)
        if row.variable_id is None and row.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
            raise VariableError("Secure input expired; provide it again", 409)
        return row

    async def set(
        self,
        actor: VariableActor,
        *,
        variable_id: UUID | None = None,
        expected_version: int | None = None,
        name: str | None = None,
        scope: str | None = None,
        scope_id: UUID | None = None,
        description: str | None = None,
        value: str | None = None,
        sensitive: bool | None = None,
        secret_ref: str | UUID | None = None,
    ) -> ScopedVariable:
        from jhin_secrets.intake import secret_spans

        await self._lock(actor.workspace_id)
        row = await self.get(actor, variable_id) if variable_id else None
        if row is not None:
            await self._authorize(actor, row.scope, row.scope_id, write=True)
            if expected_version != row.version:
                raise VariableError("Variable version changed; reload before saving", 409)
            if sensitive is not None and sensitive != row.sensitive:
                raise VariableError("Variable sensitivity cannot be changed", 422)
            if (scope is not None and scope != row.scope) or (
                scope_id is not None and scope_id != row.scope_id
            ):
                raise VariableError("Variable scope cannot be changed", 422)
            scope, scope_id, sensitive = row.scope, row.scope_id, row.sensitive
        else:
            if expected_version not in (None, 0) or not name or not scope or scope_id is None:
                raise VariableError("New variables require name and scope", 422)
            await self._authorize(actor, scope, scope_id, write=True)
            sensitive = bool(sensitive)
        if name is not None and (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,119}", name) or secret_spans(name)
        ):
            raise VariableError(
                "Use a short variable name with letters, numbers, dots, dashes or underscores", 422
            )
        if description is not None and (
            len(description) > 2000
            or "\x00" in description
            or any(0xD800 <= ord(char) <= 0xDFFF for char in description)
            or secret_spans(description)
        ):
            raise VariableError("Description cannot contain credential material", 422)
        if value is not None:
            validate_value(value, allow_empty=not sensitive)
        if not sensitive and value is not None and secret_spans(value):
            raise VariableError("Credential material must be stored as sensitive", 422)
        if sensitive and actor.actor_type == "agent" and value is not None:
            raise VariableError("Agents must use an opaque secure input reference")
        if secret_ref is not None and (not sensitive or value is not None):
            raise VariableError(
                "Secure inputs require a sensitive variable without a plaintext value", 422
            )
        if sensitive and self.crypto is None:
            raise VariableError("Secret encryption is unavailable", 503)
        capture = await self._capture(actor, secret_ref) if secret_ref is not None else None
        if capture is not None and capture.variable_id is not None:
            existing = await self.get(actor, capture.variable_id)
            if (existing.scope, existing.scope_id, existing.name) != (
                scope,
                scope_id,
                name or (row.name if row else None),
            ):
                raise VariableError("Secure input was already consumed by another variable", 409)
            return existing
        target_name = name if name is not None else row.name if row else ""
        duplicate = await self.session.scalar(
            select(ScopedVariable.id).where(
                ScopedVariable.workspace_id == actor.workspace_id,
                ScopedVariable.scope == scope,
                ScopedVariable.scope_id == scope_id,
                ScopedVariable.name == target_name,
            )
        )
        if duplicate is not None and (row is None or duplicate != row.id):
            raise VariableError("A variable with this name already exists in the scope", 409)
        if row is None:
            if value is None and capture is None:
                raise VariableError("A value or secure input is required", 422)
            row = ScopedVariable(
                id=new_uuid7(),
                workspace_id=actor.workspace_id,
                scope=scope,
                scope_id=scope_id,
                name=target_name,
                description=description or "",
                sensitive=sensitive,
                version=1,
                created_by_type=actor.actor_type,
                created_by_id=actor.actor_id,
                updated_by_type=actor.actor_type,
                updated_by_id=actor.actor_id,
            )
        else:
            row.version += 1
            row.name = target_name
            if description is not None:
                row.description = description
        if sensitive:
            if self.crypto is None:
                raise VariableError("Secret encryption is unavailable", 503)
            secrets = SecretStore(self.session, self.crypto)
            if capture is not None:
                # A capture can be consumed once; its encrypted row becomes the
                # variable's backing store without ever returning plaintext.
                row.secret_id = capture.secret_id
            elif value is not None:
                if row.secret_id:
                    secret = await secrets.rotate(actor.workspace_id, row.secret_id, value)
                else:
                    secret = await secrets.create(
                        workspace_id=actor.workspace_id,
                        name=f"variable/{row.id}",
                        plaintext=value,
                        created_by_user_id=actor.actor_id if actor.actor_type == "user" else None,
                    )
                    row.secret_id = secret.id
                secret.masked_hint = ""
            row.plaintext = None
        elif value is not None:
            row.plaintext = value
        row.updated_by_type, row.updated_by_id = actor.actor_type, actor.actor_id
        self.session.add(row)
        await self.session.flush()
        if capture is not None:
            capture.variable_id = row.id
            capture.consumed_at = datetime.now(UTC)
            await self.session.flush()
        self._audit(actor, "variable.updated" if row.version > 1 else "variable.created", row)
        await self.session.refresh(row)
        return row

    async def copy(
        self,
        actor: VariableActor,
        source_variable_id: UUID,
        *,
        expected_version: int,
        scope: str,
        scope_id: UUID,
        name: str | None = None,
    ) -> ScopedVariable:
        """Copy to an authorized namespace without exposing sensitive plaintext."""
        from jhin_secrets.intake import secret_spans

        await self._lock(actor.workspace_id)
        original = await self.get(actor, source_variable_id)
        await self._authorize(actor, scope, scope_id, write=True)
        if original.version != expected_version:
            raise VariableError("Variable version changed; reload before copying", 409)
        target_name = name or original.name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,119}", target_name) or secret_spans(
            target_name
        ):
            raise VariableError("Use a short variable name without credential material", 422)
        existing = await self.session.scalar(
            select(ScopedVariable)
            .where(
                ScopedVariable.workspace_id == actor.workspace_id,
                ScopedVariable.scope == scope,
                ScopedVariable.scope_id == scope_id,
                ScopedVariable.name == target_name,
            )
            .execution_options(populate_existing=True)
        )
        if existing:
            if (
                existing.source_variable_id == original.id
                and existing.source_version == original.version
                and existing.version == 1
            ):
                return existing
            raise VariableError("A variable with this name already exists in the scope", 409)
        identifier = new_uuid7()
        secret_id = None
        if original.sensitive:
            if self.crypto is None or original.secret_id is None:
                raise VariableError("Secret encryption is unavailable", 503)
            secrets = SecretStore(self.session, self.crypto)
            plaintext = await self._reveal_current(actor.workspace_id, original.secret_id)
            secret = await secrets.create(
                workspace_id=actor.workspace_id,
                name=f"variable/{identifier}",
                plaintext=plaintext,
                created_by_user_id=actor.actor_id if actor.actor_type == "user" else None,
            )
            secret.masked_hint = ""
            secret_id = secret.id
            del plaintext
        row = ScopedVariable(
            id=identifier,
            workspace_id=actor.workspace_id,
            scope=scope,
            scope_id=scope_id,
            name=target_name,
            description=original.description,
            sensitive=original.sensitive,
            secret_id=secret_id,
            plaintext=None if original.sensitive else original.plaintext,
            version=1,
            source_variable_id=original.id,
            source_version=original.version,
            created_by_type=actor.actor_type,
            created_by_id=actor.actor_id,
            updated_by_type=actor.actor_type,
            updated_by_id=actor.actor_id,
        )
        self.session.add(row)
        await self.session.flush()
        self._audit(actor, "variable.copied", row)
        return row

    async def delete(
        self, actor: VariableActor, variable_id: UUID, *, expected_version: int
    ) -> None:
        await self._lock(actor.workspace_id)
        row = await self.get(actor, variable_id)
        await self._authorize(actor, row.scope, row.scope_id, write=True)
        if expected_version != row.version:
            raise VariableError("Variable version changed; reload before deleting", 409)
        bindings = (
            await self.session.scalars(
                select(VariableConnectionBinding)
                .where(
                    VariableConnectionBinding.workspace_id == actor.workspace_id,
                    VariableConnectionBinding.variable_id == row.id,
                )
                .order_by(VariableConnectionBinding.connection_id)
            )
        ).all()
        for binding in bindings:
            connection = await self.session.scalar(
                select(Connection)
                .where(
                    Connection.id == binding.connection_id,
                    Connection.workspace_id == actor.workspace_id,
                )
                .with_for_update()
            )
            if connection is not None:
                # Revocation cannot leave an apparently enabled app with a
                # missing credential. Reconnection requires explicit setup.
                connection.status = "disabled"
            await self.session.delete(binding)
        await self.session.flush()
        self._audit(actor, "variable.deleted", row)
        captures = (
            await self.session.scalars(
                select(SecureInputCapture).where(SecureInputCapture.variable_id == row.id)
            )
        ).all()
        secret_ids = {capture.secret_id for capture in captures}
        if row.secret_id:
            secret_ids.add(row.secret_id)
        for capture in captures:
            await self.session.delete(capture)
        await self.session.delete(row)
        await self.session.flush()
        for secret_id in secret_ids:
            secret = await self.session.get(Secret, secret_id)
            if secret:
                await self.session.delete(secret)
        await self.session.flush()

    async def bind(
        self,
        actor: VariableActor,
        variable_id: UUID,
        connection_id: UUID,
        *,
        credential_field: str,
        approved_origin: str,
    ) -> None:
        await self._lock(actor.workspace_id)
        row = await self.get(actor, variable_id)
        if actor.actor_type != "agent" or not row.sensitive:
            raise VariableError(
                "A connector binding requires an authorized agent and sensitive variable"
            )
        connection = await self.session.scalar(
            select(Connection)
            .where(Connection.id == connection_id, Connection.workspace_id == actor.workspace_id)
            .execution_options(populate_existing=True)
        )
        if connection is None or origin(str(connection.config_json.get("admin_url", ""))) != origin(
            approved_origin
        ):
            raise VariableError("Connection origin does not match the approved origin", 409)
        approved_url = canonical_admin_url(str(connection.config_json.get("admin_url", "")))
        existing = await self.session.scalar(
            select(VariableConnectionBinding).where(
                VariableConnectionBinding.workspace_id == actor.workspace_id,
                VariableConnectionBinding.connection_id == connection_id,
                VariableConnectionBinding.credential_field == credential_field,
            )
        )
        if existing:
            if (
                existing.variable_id != row.id
                or existing.approved_origin != origin(approved_origin)
                or existing.approved_admin_url != approved_url
            ):
                raise VariableError("Connection credential is already bound", 409)
            return
        self.session.add(
            VariableConnectionBinding(
                workspace_id=actor.workspace_id,
                variable_id=row.id,
                connection_id=connection_id,
                credential_field=credential_field,
                approved_origin=origin(approved_origin),
                approved_admin_url=approved_url,
                created_by_agent_id=actor.actor_id,
            )
        )
        await self.session.flush()

    async def resolve_bound(
        self,
        actor: VariableActor,
        variable_id: UUID,
        connection_id: UUID,
        *,
        credential_field: str,
        approved_origin: str,
        allow_disabled: bool = False,
        record_use: bool = True,
    ) -> str:
        row = await self.get(actor, variable_id)
        binding = await self.session.scalar(
            select(VariableConnectionBinding).where(
                VariableConnectionBinding.workspace_id == actor.workspace_id,
                VariableConnectionBinding.variable_id == row.id,
                VariableConnectionBinding.connection_id == connection_id,
                VariableConnectionBinding.credential_field == credential_field,
            )
        )
        connection = await self.session.scalar(
            select(Connection)
            .where(Connection.id == connection_id, Connection.workspace_id == actor.workspace_id)
            .execution_options(populate_existing=True)
        )
        if (
            binding is None
            or connection is None
            or (connection.status != "active" and not allow_disabled)
        ):
            raise VariableError("Variable is not bound to this enabled connection")
        if (
            origin(approved_origin) != binding.approved_origin
            or origin(str(connection.config_json.get("admin_url", ""))) != binding.approved_origin
            or not binding.approved_admin_url
            or canonical_admin_url(str(connection.config_json.get("admin_url", "")))
            != binding.approved_admin_url
        ):
            raise VariableError("Connection origin changed; approve a new binding", 409)
        if not row.sensitive or row.secret_id is None or self.crypto is None:
            raise VariableError("Stored connection credential is unavailable", 503)
        return await self._reveal_current(actor.workspace_id, row.secret_id, record_use=record_use)


async def resolve_internal(
    ctx: Any,
    variable_id: UUID,
    *,
    connection_id: UUID,
    credential_field: str,
    approved_origin: str,
    record_use: bool = True,
) -> str:
    """Worker-only after the connector tool gateway has authorized connection_id."""
    return await VariableStore(ctx.session, ctx.crypto).resolve_bound(
        VariableActor(ctx.workspace_id, "agent", ctx.agent_id),
        variable_id,
        connection_id,
        credential_field=credential_field,
        approved_origin=approved_origin,
        record_use=record_use,
    )


async def bind_internal(
    ctx: Any, variable_id: UUID, *, connection_id: UUID, credential_field: str, approved_origin: str
) -> None:
    """Called only by the explicitly authorized connector setup executor."""
    await VariableStore(ctx.session, ctx.crypto).bind(
        VariableActor(ctx.workspace_id, "agent", ctx.agent_id),
        variable_id,
        connection_id,
        credential_field=credential_field,
        approved_origin=approved_origin,
    )
