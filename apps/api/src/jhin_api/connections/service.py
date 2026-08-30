"""Connection lifecycle business logic (plan 6.9, 11).

Credential flow (plan 13.4/48.1): credential fields arrive once in the
create/rotate request, are serialized to JSON and stored through
``SecretStore`` (AES-256-GCM envelope), and the connection row only keeps the
secret id. Decryption happens transiently inside ``verify`` here and inside
tool executors at execution time — plaintext is never returned by any route.
"""

from __future__ import annotations

import contextlib
import json
import math
import secrets as stdlib_secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.audit import service as audit
from jhin_api.deps import WorkspaceContext
from jhin_connectors import (
    ConnectionHealth,
    Connector,
    VerifyContext,
    WebhookSecretMode,
    default_registry,
    normalize_config,
)
from jhin_connectors.mcp import (
    DISCOVERY_KEY,
    MCP_CONNECTOR_TYPE,
    OVERRIDES_KEY,
    capability_pattern_for,
    effective_risk,
    stored_overrides,
    stored_tools,
    tool_name_for,
)
from jhin_connectors.mcp.discovery import discovered_at as mcp_discovered_at
from jhin_connectors.mcp.oauth import OAUTH_CONFIG_KEYS as MCP_OAUTH_CONFIG_KEYS
from jhin_db.models import (
    Agent,
    AgentCapabilityGrant,
    Connection,
    ToolCall,
    Trigger,
    TriggerInvocation,
)
from jhin_db.models.connection import new_public_id
from jhin_domain import ConnectionStatus, SecretType
from jhin_oauth.lifecycle import ConnectionTokenService
from jhin_policy import RiskLevel, ToolDefinition, capability_matches, scope_matches
from jhin_secrets import (
    SecretCrypto,
    SecretMaterialError,
    SecretStore,
    decode_string_secret_map,
    get_redactor,
)

_REGISTRY = default_registry()
_MAX_PROVIDER_OUTPUT_STRING_CHARS = 2_000
_MAX_PROVIDER_OUTPUT_BYTES = 32_768
_MAX_PROVIDER_OUTPUT_ITEMS = 256
_MAX_PROVIDER_OUTPUT_DEPTH = 16
_MAX_CONNECTION_ACCESS_SUMMARY_ROWS = 256
_CONNECTION_ACCESS_SUMMARY_STREAM_BATCH_SIZE = 64
_ACCESS_SUMMARY_UNAVAILABLE_DETAIL = "Connection access summary is temporarily unavailable"
_STILL_DISABLED_NOTE = (
    "This app is still turned off, so agents cannot use its tools. Turn it back on when you "
    "want them to."
)
# The database index (migration 0030) that keeps one MCP short name per
# workspace; its name is how a lost race is told apart from a duplicate name.
MCP_SERVER_SLUG_INDEX = "uq_connection_mcp_server_slug"


class _ProviderOutputLimitError(ValueError):
    pass


@dataclass
class _ProviderOutputBudget:
    items: int = 0
    limit: int = _MAX_PROVIDER_OUTPUT_ITEMS

    def consume_item(self) -> None:
        self.items += 1
        if self.items > self.limit:
            raise _ProviderOutputLimitError("provider output exceeds the item limit")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _access_summary_unavailable() -> HTTPException:
    """Fail closed without exposing grant cardinality or row details."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_ACCESS_SUMMARY_UNAVAILABLE_DETAIL,
    )


def _decode_stored_credentials(plaintext: str) -> dict[str, str]:
    try:
        return decode_string_secret_map(plaintext)
    except SecretMaterialError:
        raise _bad_request("Stored connection credential is malformed") from None


def _safe_provider_text(value: str) -> str:
    """Redact the complete provider value before applying the persistence cap."""
    return get_redactor().redact_text(value)[:_MAX_PROVIDER_OUTPUT_STRING_CHARS]


def _safe_provider_key(value: object) -> str:
    try:
        rendered = str(value)
    except Exception:
        rendered = "[unsupported provider key]"
    return _safe_provider_text(rendered)


def _unique_provider_key(key: str, existing: dict[str, object]) -> str:
    """Preserve values when stringification/redaction makes keys collide."""
    if key not in existing:
        return key
    collision = 2
    while True:
        suffix = f"#{collision}"
        candidate = f"{key[: _MAX_PROVIDER_OUTPUT_STRING_CHARS - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
        collision += 1


def _sanitize_provider_value(
    value: object,
    *,
    budget: _ProviderOutputBudget,
    depth: int = 0,
) -> object:
    """Recursively scrub provider-controlled metadata without changing containers."""
    if depth > _MAX_PROVIDER_OUTPUT_DEPTH:
        raise _ProviderOutputLimitError("provider output exceeds the depth limit")
    if isinstance(value, str):
        return _safe_provider_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            budget.consume_item()
            safe_key = _unique_provider_key(_safe_provider_key(key), sanitized)
            sanitized[safe_key] = _sanitize_provider_value(
                item,
                budget=budget,
                depth=depth + 1,
            )
        return sanitized
    if isinstance(value, list):
        sanitized_list: list[object] = []
        for item in value:
            budget.consume_item()
            sanitized_list.append(_sanitize_provider_value(item, budget=budget, depth=depth + 1))
        return sanitized_list
    if isinstance(value, tuple):
        sanitized_items: list[object] = []
        for item in value:
            budget.consume_item()
            sanitized_items.append(_sanitize_provider_value(item, budget=budget, depth=depth + 1))
        return tuple(sanitized_items)
    if isinstance(value, (int, bool)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _safe_provider_key(value)


def _sanitize_provider_document(value: object) -> object:
    """Sanitize one provider document and enforce aggregate in-process bounds."""
    try:
        sanitized = _sanitize_provider_value(
            value,
            budget=_ProviderOutputBudget(),
        )
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except _ProviderOutputLimitError:
        raise
    except Exception:
        raise _ProviderOutputLimitError("provider output is not safely serializable") from None
    if len(encoded) > _MAX_PROVIDER_OUTPUT_BYTES:
        raise _ProviderOutputLimitError("provider output exceeds the byte limit")
    return sanitized


def _unsafe_provider_output() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Provider returned an unsafe or oversized response",
    )


def get_connector(connector_type: str) -> Connector:
    connector = _REGISTRY.get(connector_type)
    if connector is None:
        raise _bad_request(f"Unknown connector type '{connector_type}'")
    return connector


def webhook_secret_mode(connector: Connector) -> WebhookSecretMode:
    return connector.manifest.webhook_secret_mode


def public_connection_config(connection: Connection) -> dict[str, object]:
    """Return only manifest-declared settings that still pass current policy.

    Legacy rows predate strict settings validation and may contain unknown or
    now-unsafe values. Serialization therefore reuses the same generic and
    connector-specific validators as writes, and fails closed without ever
    falling back to the raw JSON column.
    """
    try:
        connector = get_connector(connection.connector_type)
    except Exception:
        return {}

    applicable_names = tuple(
        field.name
        for field in connector.manifest.config_fields
        if not field.auth_types or connection.auth_type in field.auth_types
    )
    submitted = {
        name: connection.config_json[name]
        for name in applicable_names
        if name in connection.config_json
    }

    def validate(candidate: dict[str, object]) -> dict[str, object] | None:
        try:
            normalized = normalize_config(
                connector.manifest,
                connection.auth_type,
                candidate,
            )
            provider_validated = connector.validate_settings(
                connection.auth_type,
                normalized,
            )
            return {
                name: provider_validated[name]
                for name in applicable_names
                if name in provider_validated
            }
        except Exception:
            return None

    fully_validated = validate(submitted)
    if fully_validated is not None:
        return fully_validated

    # Preserve independently valid public settings when one legacy field is
    # stale. Revalidate the accumulated candidate on every addition so an
    # unsafe cross-field combination can never be serialized.
    accepted: dict[str, object] = {}
    safe: dict[str, object] = {}
    pending = [(name, submitted[name]) for name in applicable_names if name in submitted]
    while pending:
        progress = False
        remaining: list[tuple[str, object]] = []
        for name, value in pending:
            candidate = {**accepted, name: value}
            validated = validate(candidate)
            if validated is None:
                remaining.append((name, value))
                continue
            accepted = candidate
            safe = validated
            progress = True
        if not progress:
            break
        pending = remaining
    return safe


def _validate_credentials(
    connector: Connector, auth_type: str, credentials: dict[str, str]
) -> None:
    """Check the submitted fields against the manifest's auth scheme: all
    required fields present and non-empty, no undeclared fields."""
    scheme = connector.manifest.auth_scheme(auth_type)
    if scheme is None:
        allowed = ", ".join(s.type for s in connector.manifest.auth_schemes)
        raise _bad_request(f"Auth type '{auth_type}' is not supported (expected one of: {allowed})")
    declared = {field.name for field in scheme.secret_fields}
    if set(credentials) - declared:
        raise _bad_request("Unknown credential fields")
    missing = [name for name in scheme.required_field_names() if not credentials.get(name)]
    if missing:
        raise _bad_request(f"Missing required credential fields: {', '.join(missing)}")


async def list_connections(db: AsyncSession, workspace_id: UUID) -> list[Connection]:
    rows = await db.scalars(
        select(Connection)
        .where(Connection.workspace_id == workspace_id)
        .order_by(Connection.created_at)
    )
    return list(rows)


async def get_connection(db: AsyncSession, workspace_id: UUID, connection_id: UUID) -> Connection:
    connection = await db.scalar(
        select(Connection).where(
            Connection.id == connection_id, Connection.workspace_id == workspace_id
        )
    )
    if connection is None:
        raise _not_found()
    return connection


def _connection_tools(connection: Connection) -> tuple[ToolDefinition, ...]:
    """The tools reachable through this connection, in a stable display order.

    Static connectors expose their registered tools; connectors with
    per-connection discovery (MCP) derive them from the stored discovery."""
    connector = get_connector(connection.connector_type)
    definitions = connector.connection_tool_definitions(connection.config_json)
    return tuple(sorted(definitions, key=lambda tool: tool.name))


def _capability_pattern_candidates(tools: tuple[ToolDefinition, ...]) -> tuple[str, ...]:
    """All persisted grant patterns that can match one selected connector tool."""
    candidates = {"*"}
    for tool in tools:
        parts = tool.required_capability.split(".")
        candidates.add(tool.required_capability)
        candidates.update(".".join(parts[:index]) + ".*" for index in range(1, len(parts)))
    return tuple(sorted(candidates))


def _string_scope(scope: object) -> dict[str, str] | None:
    """Return a scope safe for the string-only summary response schema."""
    if not isinstance(scope, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in scope.items()
    ):
        return None
    return dict(scope)


def _raw_scope_is_relevant_to_connection(scope: object, effect: str, connection_id: str) -> bool:
    """Check target relevance before rejecting unsupported summary scope values.

    Runtime scope matching supports list-valued ``any-of`` constraints. This
    endpoint intentionally cannot serialize or model those values precisely,
    but it must still recognize when they could influence this connection.
    """
    if not isinstance(scope, dict) or not all(isinstance(key, str) for key in scope):
        # A malformed candidate scope cannot be proven irrelevant without
        # changing runtime semantics, so fail closed rather than overreport.
        return True
    if effect == "allow" and "connection_id" not in scope:
        # Broad allows remain excluded from connection authorization.
        return False
    connection_scope: dict[str, Any] = (
        {"connection_id": scope["connection_id"]} if "connection_id" in scope else {}
    )
    return scope_matches(connection_scope, {"connection_id": connection_id})


def _is_scope_glob(value: str) -> bool:
    """Whether a grant value uses fnmatch's pattern language."""
    return any(character in value for character in "*?[")


def _glob_literal_prefix(pattern: str) -> str:
    """The initial literal segment of a glob, useful for proving disjointness."""
    for index, character in enumerate(pattern):
        if character in "*?[":
            return pattern[:index]
    return pattern


def _scope_values_overlap(allow_value: str, deny_value: str) -> bool:
    """Conservatively decide whether two string grant languages overlap."""
    allow_is_glob = _is_scope_glob(allow_value)
    deny_is_glob = _is_scope_glob(deny_value)
    if not allow_is_glob and not deny_is_glob:
        return allow_value == deny_value
    if not allow_is_glob:
        return fnmatchcase(allow_value, deny_value)
    if not deny_is_glob:
        return fnmatchcase(deny_value, allow_value)
    if allow_value == deny_value:
        return True
    allow_prefix = _glob_literal_prefix(allow_value)
    deny_prefix = _glob_literal_prefix(deny_value)
    are_provably_disjoint = bool(
        allow_prefix
        and deny_prefix
        and not allow_prefix.startswith(deny_prefix)
        and not deny_prefix.startswith(allow_prefix)
    )
    # The remaining glob intersection cases are deliberately conservative:
    # a false deny is safer than reporting authorization that could be denied.
    return not are_provably_disjoint


def _grant_applies_to_tool(
    tool: ToolDefinition,
    *,
    capability: str,
    scope: dict[str, str],
    effect: str,
    connection_id: str,
) -> bool:
    """Whether a grant structurally applies to this tool on this connection."""
    if not capability_matches(capability, tool.required_capability):
        return False
    if effect == "allow":
        # Connection allows are intentionally exact; broad allows never
        # authorize this summary. Other scope values are structural because
        # this endpoint has no concrete project/deployment invocation value.
        if scope.get("connection_id") != connection_id:
            return False
        if not set(tool.required_grant_scope_keys).issubset(scope):
            return False
    elif effect == "deny":
        # A deny may be broad, and its connection glob must be evaluated
        # against the actual selected connection rather than against itself.
        if not _deny_is_relevant_to_connection(scope, connection_id):
            return False
    else:
        return False
    return set(scope).issubset(tool.scope_keys)


def _eligibility_reason(
    tools: tuple[ToolDefinition, ...], capability: str, scope: dict[str, str], effect: str
) -> str | None:
    matching_tools = [
        tool for tool in tools if capability_matches(capability, tool.required_capability)
    ]
    if effect == "allow":
        missing = sorted(
            {
                key
                for tool in matching_tools
                for key in tool.required_grant_scope_keys
                if key not in scope
            }
        )
        if missing:
            return f"Missing required scope keys: {', '.join(missing)}"
    return "Grant scope does not match a selected connector tool"


def _grant_summary(
    grant: AgentCapabilityGrant,
    scope: dict[str, str],
    tools: tuple[ToolDefinition, ...],
    connection_id: str,
) -> dict[str, object]:
    eligible_tool_names = [
        tool.name
        for tool in tools
        if _grant_applies_to_tool(
            tool,
            capability=grant.capability,
            scope=scope,
            effect=grant.effect,
            connection_id=connection_id,
        )
    ]
    return {
        "grant_id": grant.id,
        "capability": grant.capability,
        "effect": grant.effect,
        "scope": scope,
        "eligible_tool_names": eligible_tool_names,
        "eligibility_reason": (
            None
            if eligible_tool_names
            else _eligibility_reason(tools, grant.capability, scope, grant.effect)
        ),
    }


def _tool_is_authorized(
    tool: ToolDefinition,
    grants: list[tuple[AgentCapabilityGrant, dict[str, str]]],
    connection_id: str,
) -> bool:
    """Apply scoped explicit-deny precedence without consulting approval policy."""
    for allow, allow_scope in grants:
        if allow.effect != "allow" or not _grant_applies_to_tool(
            tool,
            capability=allow.capability,
            scope=allow_scope,
            effect=allow.effect,
            connection_id=connection_id,
        ):
            continue
        denied = any(
            deny.effect == "deny"
            and capability_matches(deny.capability, tool.required_capability)
            and _deny_overlaps_allow_scope(tool, allow_scope, deny_scope, connection_id)
            for deny, deny_scope in grants
        )
        if not denied:
            return True
    return False


def _deny_overlaps_allow_scope(
    tool: ToolDefinition,
    allow_scope: dict[str, str],
    deny_scope: dict[str, str],
    connection_id: str,
) -> bool:
    """Whether this deny could overlap this allow for the selected connection.

    The summary has no concrete project/deployment input, so non-connection
    dimensions compare their scope languages. Any deny key absent from the
    allow is treated as potentially overlapping; this can underreport partial
    access but never reports authorization where a runtime call may be denied.
    """
    if not set(deny_scope).issubset(tool.scope_keys):
        return False
    for key, deny_value in deny_scope.items():
        if key == "connection_id":
            if not fnmatchcase(connection_id, deny_value):
                return False
            continue
        allow_value = allow_scope.get(key)
        if allow_value is not None and not _scope_values_overlap(allow_value, deny_value):
            return False
    return True


def _deny_is_relevant_to_connection(scope: dict[str, str], connection_id: str) -> bool:
    """Whether a scoped or broad deny can apply to this connection at runtime."""
    connection_scope = {"connection_id": scope["connection_id"]} if "connection_id" in scope else {}
    return scope_matches(connection_scope, {"connection_id": connection_id})


async def connection_access_summary(
    db: AsyncSession, workspace_id: UUID, connection_id: UUID
) -> dict[str, object]:
    """Return exact, connection-scoped grants and their effective tool access.

    The connection lookup occurs before querying grants, preserving the same
    workspace-local 404 behavior as every other connection endpoint.  The
    single joined query deliberately loads no connection credentials, config,
    or approval-policy JSON.
    """
    connection = await get_connection(db, workspace_id, connection_id)
    tools = _connection_tools(connection)
    target_connection_id = str(connection.id)
    capability_candidates = _capability_pattern_candidates(tools)
    result = await db.stream(
        select(Agent.id, Agent.name, AgentCapabilityGrant)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .where(
            Agent.workspace_id == workspace_id,
            AgentCapabilityGrant.workspace_id == workspace_id,
            AgentCapabilityGrant.capability.in_(capability_candidates),
            AgentCapabilityGrant.effect.in_(("allow", "deny")),
        )
        # This bounds driver fetch batches, not the total scan. Only rows that
        # survive exact Python relevance checks count against the public cap.
        .execution_options(yield_per=_CONNECTION_ACCESS_SUMMARY_STREAM_BATCH_SIZE)
    )
    relevant_by_agent: dict[
        UUID, tuple[str, list[tuple[AgentCapabilityGrant, dict[str, str]]]]
    ] = {}
    relevant_row_count = 0
    try:
        async for agent_id, agent_name, grant in result.tuples():
            raw_scope = grant.scope_json
            if not _raw_scope_is_relevant_to_connection(
                raw_scope, grant.effect, target_connection_id
            ):
                continue
            scope = _string_scope(raw_scope)
            if scope is None:
                raise _access_summary_unavailable()
            relevant_row_count += 1
            if relevant_row_count > _MAX_CONNECTION_ACCESS_SUMMARY_ROWS:
                raise _access_summary_unavailable()
            if agent_id not in relevant_by_agent:
                relevant_by_agent[agent_id] = (agent_name, [])
            relevant_by_agent[agent_id][1].append((grant, scope))
    finally:
        await result.close()

    agents: list[dict[str, object]] = []
    for agent_id, (agent_name, grants) in relevant_by_agent.items():
        authorized_tool_names = [
            tool.name for tool in tools if _tool_is_authorized(tool, grants, target_connection_id)
        ]
        ordered_grants = sorted(
            grants,
            key=lambda row: (
                row[0].capability,
                row[0].effect,
                json.dumps(row[1], sort_keys=True, separators=(",", ":")),
                str(row[0].id),
            ),
        )
        agents.append(
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "authorized": bool(authorized_tool_names),
                "authorized_tool_names": authorized_tool_names,
                "grants": [
                    _grant_summary(grant, scope, tools, target_connection_id)
                    for grant, scope in ordered_grants
                ],
            }
        )
    agents.sort(
        key=lambda item: (
            not bool(item["authorized"]),
            str(item["agent_name"]).casefold(),
            str(item["agent_id"]),
        )
    )
    return {
        "connection_id": connection.id,
        "agents": agents,
        "delete_impact": await delete_impact(db, workspace_id, connection.id),
    }


async def delete_impact(
    db: AsyncSession, workspace_id: UUID, connection_id: UUID
) -> dict[str, int]:
    """What deleting this connection takes with it (plan 17.9).

    Triggers hang off the connection with ``ON DELETE CASCADE``, and their
    invocation history hangs off them the same way, so removing a connection
    silently removes every automation built on it. The delete stays allowed —
    an admin may well mean it — but the confirmation has to say the cost."""
    trigger_ids = list(
        await db.scalars(
            select(Trigger.id).where(
                Trigger.workspace_id == workspace_id,
                Trigger.connection_id == connection_id,
            )
        )
    )
    invocation_count = 0
    if trigger_ids:
        invocation_count = int(
            await db.scalar(
                select(func.count())
                .select_from(TriggerInvocation)
                .where(
                    TriggerInvocation.workspace_id == workspace_id,
                    TriggerInvocation.trigger_id.in_(trigger_ids),
                )
            )
            or 0
        )
    return {"trigger_count": len(trigger_ids), "trigger_invocation_count": invocation_count}


def _duplicate_server_slug(server_slug: str, existing_name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"The short name '{server_slug}' is already used by the app '{existing_name}'. "
            "Give this server a different short name, or open the app you already connected "
            "and manage it there."
        ),
    )


async def _existing_server_slug_owner(
    db: AsyncSession,
    workspace_id: UUID,
    server_slug: str,
    *,
    exclude_id: UUID | None = None,
) -> Connection | None:
    """The MCP connection already claiming this short name, if there is one.

    Slugs live inside ``config_json``, whose JSON operators differ between
    Postgres and the SQLite used by unit tests, so the comparison happens in
    Python. A workspace holds a handful of connections, not a table scan."""
    query = select(Connection).where(
        Connection.workspace_id == workspace_id,
        Connection.connector_type == MCP_CONNECTOR_TYPE,
    )
    if exclude_id is not None:
        query = query.where(Connection.id != exclude_id)
    rows = await db.scalars(query.order_by(Connection.created_at, Connection.id))
    return next((row for row in rows if row.config_json.get("server_slug") == server_slug), None)


async def ensure_server_slug_is_free(
    db: AsyncSession,
    workspace_id: UUID,
    connector_type: str,
    config: dict[str, object],
    *,
    exclude_id: UUID | None = None,
) -> None:
    """Refuse a second MCP connection on one workspace's short name.

    Tool names, the capability family, and the per-tool risk overrides an
    admin approved are all keyed by ``mcp.<short name>``. Two connections
    sharing one short name means only one of them is ever resolved, so the
    other's reviewed risk levels silently stop being enforced — a destructive
    tool can end up running without the approval its own connection demands.
    The database index added in migration 0030 enforces the same rule; this
    check exists so the caller gets an explanation instead of a constraint."""
    if connector_type != MCP_CONNECTOR_TYPE:
        return
    server_slug = config.get("server_slug")
    if not isinstance(server_slug, str) or not server_slug:
        return
    existing = await _existing_server_slug_owner(
        db, workspace_id, server_slug, exclude_id=exclude_id
    )
    if existing is not None:
        raise _duplicate_server_slug(server_slug, existing.name)


async def create_connection(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    *,
    connector_type: str,
    name: str,
    auth_type: str,
    credentials: dict[str, str],
    config: dict[str, object],
    request_id: UUID,
    ip_hash: str,
) -> tuple[Connection, str | None]:
    """Create a connection; returns it plus the webhook signing secret
    plaintext (shown exactly once) when the connector supports webhooks."""
    connector = get_connector(connector_type)
    _validate_credentials(connector, auth_type, credentials)
    try:
        normalized_config = normalize_config(connector.manifest, auth_type, config)
        normalized_config = connector.validate_settings(auth_type, normalized_config)
    except ValueError as exc:
        raise _bad_request(str(exc)) from None
    # Before any secret is written, so a refused create leaves nothing behind.
    await ensure_server_slug_is_free(db, ctx.workspace_id, connector_type, normalized_config)

    public_id = new_public_id()
    store = SecretStore(db, crypto)
    credential_secret = await store.create(
        workspace_id=ctx.workspace_id,
        name=f"connection/{public_id}/credentials",
        plaintext=json.dumps(credentials),
        secret_type=SecretType.CONNECTION_CREDENTIALS,
        created_by_user_id=ctx.user.id,
    )

    webhook_plaintext: str | None = None
    webhook_secret_id: UUID | None = None
    if webhook_secret_mode(connector) == "generated":
        webhook_plaintext = stdlib_secrets.token_urlsafe(32)
        webhook_secret = await store.create(
            workspace_id=ctx.workspace_id,
            name=f"connection/{public_id}/webhook",
            plaintext=webhook_plaintext,
            secret_type=SecretType.WEBHOOK_SECRET,
            created_by_user_id=ctx.user.id,
        )
        webhook_secret_id = webhook_secret.id

    connection = Connection(
        workspace_id=ctx.workspace_id,
        connector_type=connector_type,
        name=name,
        auth_type=auth_type,
        public_id=public_id,
        encrypted_secret_id=credential_secret.id,
        webhook_secret_id=webhook_secret_id,
        config_json=normalized_config,
        created_by_user_id=ctx.user.id,
    )
    db.add(connection)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Two admins creating the same short name at once race past the check
        # above; the index is the authority and its name says which rule bit.
        # The winning row cannot be read back from a broken transaction, so
        # this message names the short name rather than the other app.
        if MCP_SERVER_SLUG_INDEX in str(exc.orig):
            server_slug = str(normalized_config.get("server_slug", ""))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"The short name '{server_slug}' was just taken by another app in this "
                    "workspace. Give this server a different short name and try again."
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A connection with this name already exists in the workspace",
        ) from exc
    audit.record(
        db,
        action="connection.created",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"connector_type": connector_type, "name": name, "auth_type": auth_type},
    )
    await db.commit()
    return connection, webhook_plaintext


OAUTH_AUTH_TYPE = "oauth"
# Internal, non-secret OAuth bookkeeping written onto ``config_json`` by the
# callback. They are deliberately not manifest fields: ``public_connection_config``
# serializes only manifest-declared settings, so these stay server-side while
# still travelling with the connection they describe.
#
# Built from the connector package's own list rather than restated, because
# these two halves must agree exactly: a key the connector writes but this
# tuple omits falls through to ``normalize_config``, which rejects any key the
# manifest does not declare, and turns a reconnect into a 400. The endpoint
# pair is added here because the API writes those two, not the connector.
OAUTH_CONFIG_KEYS = (
    *MCP_OAUTH_CONFIG_KEYS,
    "oauth_token_endpoint",
    "oauth_revocation_endpoint",
)


async def create_connection_from_oauth(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    connection_id: UUID | None,
    connector_type: str,
    name: str,
    config: dict[str, object],
    created_by_user_id: UUID,
) -> Connection:
    """Create (or re-authorize) a connection whose credential is an OAuth grant.

    Deliberately separate from :func:`create_connection`: there are no
    credential fields to validate here, because the credential does not exist
    until the token exchange succeeds and is written by
    ``ConnectionTokenService.store_tokens`` in the same transaction. The row
    is flushed, not committed — the caller owns the commit, so a connection
    and its tokens are never half-written.

    Re-authorization keeps the existing row: its id is what every grant, every
    trigger, and every recorded tool call points at, and replacing it to
    refresh a token would quietly revoke every agent's access to the app.
    """
    connector = get_connector(connector_type)
    internal = {key: config[key] for key in OAUTH_CONFIG_KEYS if key in config}
    public = {key: value for key, value in config.items() if key not in OAUTH_CONFIG_KEYS}
    try:
        normalized = normalize_config(connector.manifest, OAUTH_AUTH_TYPE, public)
        normalized = connector.validate_settings(OAUTH_AUTH_TYPE, normalized)
    except ValueError as exc:
        raise _bad_request(str(exc)) from None
    merged: dict[str, Any] = {**normalized, **internal}

    if connection_id is not None:
        connection = await get_connection(db, workspace_id, connection_id)
        await ensure_server_slug_is_free(
            db, workspace_id, connector_type, merged, exclude_id=connection.id
        )
        # Reconnecting replaces the grant, not the row's accumulated knowledge.
        # Anything already on the row that is neither a manifest setting nor
        # OAuth bookkeeping — the discovered tool list and when it was
        # discovered, the per-tool risk overrides an admin set — is carried
        # across, so a reconnect does not silently empty an app's tool list.
        carried = {
            key: value
            for key, value in connection.config_json.items()
            if key not in merged and key not in OAUTH_CONFIG_KEYS
        }
        connection.config_json = {**carried, **merged}
        connection.auth_type = OAUTH_AUTH_TYPE
        await db.flush()
        return connection

    await ensure_server_slug_is_free(db, workspace_id, connector_type, merged)
    connection = Connection(
        workspace_id=workspace_id,
        connector_type=connector_type,
        name=name,
        auth_type=OAUTH_AUTH_TYPE,
        public_id=new_public_id(),
        config_json=merged,
        created_by_user_id=created_by_user_id,
        oauth_authorized_by_user_id=created_by_user_id,
    )
    db.add(connection)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A connection with this name already exists in the workspace",
        ) from exc
    return connection


async def authorized_by(db: AsyncSession, connection: Connection) -> tuple[UUID, str] | None:
    """Whose provider account this connection acts as, if it is an OAuth one.

    Surfaced on every connection so an admin can see, without asking anybody,
    whose permissions the workspace's agents are borrowing when they use it.
    """
    if connection.oauth_authorized_by_user_id is None:
        return None
    from jhin_db.models import User

    user = await db.get(User, connection.oauth_authorized_by_user_id)
    if user is None:
        return None
    return user.id, user.display_name


async def set_webhook_secret(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    secret: str,
    request_id: UUID,
    ip_hash: str,
) -> None:
    """Create or rotate a provider-supplied webhook secret without readback."""
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.id == connection_id,
            Connection.workspace_id == ctx.workspace_id,
        )
        .with_for_update()
    )
    if connection is None:
        raise _not_found()
    connector = get_connector(connection.connector_type)
    if webhook_secret_mode(connector) != "provider_supplied":
        raise _bad_request("Connector does not accept provider-supplied webhook secrets")

    store = SecretStore(db, crypto)
    action: str
    if connection.webhook_secret_id is None:
        stored = await store.create(
            workspace_id=ctx.workspace_id,
            name=f"connection/{connection.public_id}/webhook",
            plaintext=secret,
            secret_type=SecretType.WEBHOOK_SECRET,
            created_by_user_id=ctx.user.id,
        )
        connection.webhook_secret_id = stored.id
        action = "connection.webhook_secret_configured"
    else:
        await store.rotate(ctx.workspace_id, connection.webhook_secret_id, secret)
        action = "connection.webhook_secret_rotated"

    audit.record(
        db,
        action=action,
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={},
    )
    await db.commit()


async def verify_connection(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
) -> tuple[Connection, ConnectionHealth]:
    """Run the connector's live health check and persist the outcome
    (status, last_verified_at, last_error — plan 6.9)."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential")

    store = SecretStore(db, crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    credentials = _decode_stored_credentials(plaintext)
    try:
        provider_health = await connector.verify_connection(
            VerifyContext(
                auth_type=connection.auth_type,
                credentials=credentials,
                config=dict(connection.config_json),
            )
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Provider connection verification failed",
        ) from None
    try:
        safe_health = _sanitize_provider_document(
            {
                "message": provider_health.message,
                "details": provider_health.details,
            }
        )
        if not isinstance(safe_health, dict):
            raise _ProviderOutputLimitError("provider health has an invalid shape")
        health = ConnectionHealth(
            ok=provider_health.ok,
            message=safe_health["message"],
            details=safe_health["details"],
        )
    except (_ProviderOutputLimitError, KeyError, TypeError, ValueError):
        raise _unsafe_provider_output() from None
    connection.last_verified_at = datetime.now(UTC)
    # A disabled connection was disabled on purpose, to cut its tools off.
    # Verification still runs, reports the truth, and records what it learned,
    # but putting the connection back in service stays the explicit job of the
    # enable control — otherwise "Test connection" quietly undoes an admin's
    # decision.
    was_disabled = connection.status == ConnectionStatus.DISABLED.value
    if health.ok:
        if not was_disabled:
            connection.status = ConnectionStatus.ACTIVE.value
        connection.last_error = None
        # Per-connection discovery (e.g. an MCP server's tool list) rides
        # along with a successful verification so the tools an admin sees are
        # the tools the gateway enforces. A failed refresh keeps the last
        # good discovery; it never fails the verification itself.
        try:
            discovery = await connector.refresh_discovery(
                VerifyContext(
                    auth_type=connection.auth_type,
                    credentials=credentials,
                    config=dict(connection.config_json),
                )
            )
        except Exception:
            discovery = None
        if discovery:
            connection.config_json = {**connection.config_json, **discovery}
    else:
        if not was_disabled:
            # A connector that recognised the failure as a dead sign-in says so
            # in ``details``. Honouring it is the difference between "something
            # is broken" and a row the UI can offer a Reconnect button on.
            connection.status = (
                ConnectionStatus.NEEDS_REAUTH.value
                if health.details.get("needs_reauth") == "true"
                else ConnectionStatus.ERROR.value
            )
        connection.last_error = health.message[:2000]
    if was_disabled:
        # The stored last_error keeps the provider's own words; only the
        # message handed back to the caller says why the app is still off.
        health = ConnectionHealth(
            ok=health.ok,
            message=f"{health.message.rstrip()} {_STILL_DISABLED_NOTE}".strip(),
            details=health.details,
        )
    audit.record(
        db,
        action="connection.verified",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"ok": health.ok, "status": connection.status},
    )
    await db.commit()
    return connection, health


async def fetch_metadata(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
) -> dict[str, object]:
    """Connector-provided, display-safe metadata for UI pickers (plan 17.10)."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential")
    store = SecretStore(db, crypto)
    plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
    credentials = _decode_stored_credentials(plaintext)
    try:
        provider_metadata = await connector.fetch_metadata(
            VerifyContext(
                auth_type=connection.auth_type,
                credentials=credentials,
                config=dict(connection.config_json),
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Provider metadata fetch failed: {type(exc).__name__}",
        ) from None
    try:
        safe_metadata = _sanitize_provider_document(provider_metadata)
        if not isinstance(safe_metadata, dict):
            raise _ProviderOutputLimitError("provider metadata has an invalid shape")
    except _ProviderOutputLimitError:
        raise _unsafe_provider_output() from None
    return cast(dict[str, object], safe_metadata)


async def rotate_credentials(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    credentials: dict[str, str],
    request_id: UUID,
    ip_hash: str,
) -> Connection:
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    _validate_credentials(connector, connection.auth_type, credentials)
    if connection.encrypted_secret_id is None:
        raise _bad_request("Connection has no stored credential to rotate")
    store = SecretStore(db, crypto)
    await store.rotate(ctx.workspace_id, connection.encrypted_secret_id, json.dumps(credentials))
    # The new credential is unproven: reset health so operators re-verify. A
    # disabled connection stays disabled — replacing a secret is maintenance,
    # not a decision to put the app back in service.
    if connection.status != ConnectionStatus.DISABLED.value:
        connection.status = ConnectionStatus.ACTIVE.value
    connection.last_error = None
    connection.last_verified_at = None
    audit.record(
        db,
        action="connection.credentials_rotated",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": connection.name},
    )
    await db.commit()
    return connection


async def set_status(
    db: AsyncSession,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    disabled: bool,
    request_id: UUID,
    ip_hash: str,
) -> Connection:
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connection.status = (
        ConnectionStatus.DISABLED.value if disabled else ConnectionStatus.ACTIVE.value
    )
    audit.record(
        db,
        action="connection.disabled" if disabled else "connection.enabled",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"name": connection.name},
    )
    await db.commit()
    return connection


async def delete_connection(
    db: AsyncSession,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    request_id: UUID,
    ip_hash: str,
    tokens: ConnectionTokenService | None = None,
) -> None:
    """Remove a connection, and hand any OAuth grant back to the provider first.

    Deleting our copy of a token is not the same as ending the grant: the
    provider keeps honouring an access token it was never told to forget, and
    a refresh token can outlive the row that referenced it by months. So when
    the caller supplies a ``ConnectionTokenService`` and this connection holds
    an OAuth grant, the grant is revoked upstream *before* the row goes. That
    revocation is best-effort — a provider being down must not strand an admin
    who is trying to disconnect an app — but the local erasure below is not.
    """
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    if tokens is not None and connection.auth_type == OAUTH_AUTH_TYPE:
        with contextlib.suppress(Exception):
            await tokens.revoke_and_clear(connection)
    store_ids = [connection.encrypted_secret_id, connection.webhook_secret_id]
    audit.record(
        db,
        action="connection.deleted",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"connector_type": connection.connector_type, "name": connection.name},
    )
    await db.delete(connection)
    # The connection's secrets have no other consumers; remove them too so no
    # orphaned ciphertext lingers.
    from jhin_db.models import Secret

    for secret_id in store_ids:
        if secret_id is not None:
            secret = await db.get(Secret, secret_id)
            if secret is not None:
                await db.delete(secret)
    await db.commit()


async def recent_tool_calls(
    db: AsyncSession, workspace_id: UUID, connection_id: UUID, *, limit: int = 20
) -> list[ToolCall]:
    """Latest tool calls that used this connection (plan 17.9 detail view)."""
    rows = await db.scalars(
        select(ToolCall)
        .where(ToolCall.workspace_id == workspace_id, ToolCall.connection_id == connection_id)
        .order_by(ToolCall.created_at.desc())
        .limit(min(limit, 100))
    )
    return list(rows)


# --- Per-connection tools (docs/architecture/mcp.md) ---

_MAX_TOOL_LISTING_ITEMS = 50_000
_VALID_RISKS = frozenset(risk.value for risk in RiskLevel)


def _safe_tool_document(value: object) -> object:
    """Redact + bound provider-controlled tool metadata (descriptions and
    JSON schemas). Larger budgets than verify/metadata because discovery is
    already capped at 200 tools x 16 KiB schemas."""
    try:
        return _sanitize_provider_value(
            value, budget=_ProviderOutputBudget(limit=_MAX_TOOL_LISTING_ITEMS)
        )
    except _ProviderOutputLimitError:
        raise _unsafe_provider_output() from None


def _is_dynamic(connection: Connection) -> bool:
    return connection.connector_type == MCP_CONNECTOR_TYPE


def _tools_listing(connection: Connection) -> dict[str, object]:
    config = dict(connection.config_json)
    if _is_dynamic(connection):
        server_slug = str(config.get("server_slug", ""))
        overrides = stored_overrides(config)
        tools: list[dict[str, object]] = []
        for tool in stored_tools(config):
            risk = effective_risk(tool, overrides)
            override = overrides.get(tool.slug)
            tools.append(
                {
                    "name": tool_name_for(server_slug, tool.slug),
                    "provider_name": tool.name,
                    "description": tool.description,
                    "risk": risk.value,
                    "derived_risk": tool.derived_risk.value,
                    "risk_override": override.value if override is not None else None,
                    "annotations": tool.annotations.model_dump(mode="json"),
                    "input_schema": tool.input_schema,
                    "schema_truncated": tool.schema_truncated,
                    "supports_approval": True,
                    "scope_keys": ["connection_id", "tool"],
                }
            )
        listing: dict[str, object] = {
            "connection_id": connection.id,
            "connector_type": connection.connector_type,
            "dynamic": True,
            "capability_pattern": capability_pattern_for(server_slug) if server_slug else None,
            "discovered_at": mcp_discovered_at(config),
            "tools": _safe_tool_document(tools),
        }
        return listing
    definitions = _connection_tools(connection)
    return {
        "connection_id": connection.id,
        "connector_type": connection.connector_type,
        "dynamic": False,
        "capability_pattern": f"{connection.connector_type}.*",
        "discovered_at": None,
        "tools": [
            {
                "name": definition.name,
                "provider_name": None,
                "description": definition.description,
                "risk": definition.risk.value,
                "derived_risk": None,
                "risk_override": None,
                "annotations": {},
                "input_schema": definition.input_json_schema(),
                "schema_truncated": False,
                "supports_approval": definition.supports_approval,
                "scope_keys": list(definition.scope_keys),
            }
            for definition in definitions
        ],
    }


async def list_connection_tools(
    db: AsyncSession,
    crypto: SecretCrypto,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    refresh: bool = False,
    request_id: UUID,
    ip_hash: str,
) -> dict[str, object]:
    """Tools reachable through one connection, with their risk levels.

    For discovery-based connectors the stored discovery is returned; when
    none exists yet (or ``refresh`` is requested) the provider is asked once
    and the result persisted, so a freshly created MCP connection lists its
    tools without a separate verify step."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    connector = get_connector(connection.connector_type)
    needs_discovery = _is_dynamic(connection) and (
        refresh or DISCOVERY_KEY not in connection.config_json
    )
    if needs_discovery:
        if connection.encrypted_secret_id is None:
            raise _bad_request("Connection has no stored credential")
        if connection.status == ConnectionStatus.DISABLED.value:
            raise _bad_request("Connection is disabled")
        store = SecretStore(db, crypto)
        plaintext = await store.reveal(ctx.workspace_id, connection.encrypted_secret_id)
        credentials = _decode_stored_credentials(plaintext)
        try:
            discovery = await connector.refresh_discovery(
                VerifyContext(
                    auth_type=connection.auth_type,
                    credentials=credentials,
                    config=dict(connection.config_json),
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Provider tool discovery failed: {type(exc).__name__}",
            ) from None
        if discovery:
            connection.config_json = {**connection.config_json, **discovery}
            audit.record(
                db,
                action="connection.tools_discovered",
                target_type="connection",
                target_id=connection.id,
                workspace_id=ctx.workspace_id,
                actor_id=ctx.user.id,
                request_id=request_id,
                ip_hash=ip_hash,
                metadata={"tool_count": len(stored_tools(connection.config_json))},
            )
            await db.commit()
    return _tools_listing(connection)


async def update_tool_risk_overrides(
    db: AsyncSession,
    ctx: WorkspaceContext,
    connection_id: UUID,
    *,
    overrides: dict[str, str | None],
    request_id: UUID,
    ip_hash: str,
) -> dict[str, object]:
    """Merge admin risk overrides for discovered tools (None removes one).

    Only slugs present in the stored discovery are accepted; the override is
    the risk the gateway enforces from the next call on, and the executor
    still refuses a tool whose live annotations report a *higher* risk than
    the one reviewed here."""
    connection = await get_connection(db, ctx.workspace_id, connection_id)
    if not _is_dynamic(connection):
        raise _bad_request("Connector has no per-tool risk overrides")
    known = {tool.slug for tool in stored_tools(connection.config_json)}
    current = {slug: risk.value for slug, risk in stored_overrides(connection.config_json).items()}
    changed: list[str] = []
    for slug, risk in overrides.items():
        if slug not in known:
            raise _bad_request("Unknown tool for this connection")
        if risk is None:
            if current.pop(slug, None) is not None:
                changed.append(slug)
            continue
        if risk not in _VALID_RISKS:
            raise _bad_request("Invalid risk level")
        if current.get(slug) != risk:
            current[slug] = risk
            changed.append(slug)
    connection.config_json = {**connection.config_json, OVERRIDES_KEY: current}
    audit.record(
        db,
        action="connection.tool_risk_overrides_updated",
        target_type="connection",
        target_id=connection.id,
        workspace_id=ctx.workspace_id,
        actor_id=ctx.user.id,
        request_id=request_id,
        ip_hash=ip_hash,
        metadata={"changed": sorted(changed), "overrides": dict(current)},
    )
    await db.commit()
    return _tools_listing(connection)
