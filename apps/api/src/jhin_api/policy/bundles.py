"""Capability bundles on an agent: the one obvious action behind "give the
engineer GitHub" (docs/operations/agent-access.md).

Applying a bundle is one transaction under the agent's row lock: the plan is
computed against the live catalog and connections
(:func:`jhin_policy.plan_bundle`), refused by sentence when it cannot be
written, and otherwise written through the same ``_write_grant`` the grants
endpoint uses — so a bundle row and a hand-made row are the same kind of row
with the same audit trail. The CLI calls exactly these functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.access.route_scopes import WORKSPACE_PREFIX, required_scope
from jhin_api.agents import service as agents_service
from jhin_api.audit import service as audit
from jhin_api.connections import service as connections_service
from jhin_api.connections.router import serialize_connection
from jhin_api.deps import WorkspaceContext
from jhin_api.policy import service as grants
from jhin_api.policy.refs import connection_ref, connection_refs
from jhin_api.policy.schemas import (
    BundleApply,
    BundleApplyOut,
    BundleGrantProblemOut,
    BundleNeedChoiceOut,
    BundleNeedOut,
    BundleOut,
    BundleReadinessOut,
    BundleRemoveOut,
    BundleStatusOut,
    BundleToolOut,
    GrantOut,
    PolicyRuleIn,
)
from jhin_db.models import AgentCapabilityGrant
from jhin_domain import ActorType, WorkspaceRole, role_satisfies
from jhin_policy import (
    BUNDLES,
    Bundle,
    ConnectionRef,
    Need,
    PolicyRule,
    ToolDefinition,
    bundle_by_id,
    bundle_capabilities,
    bundle_state,
    capability_matches,
    covered_capabilities,
    is_repository_pattern,
    plan_bundle,
)
from jhin_policy.bundles import (
    AGENT_BRANCH_PATTERN,
    BUNDLE_IDS,
    CODE_EDITING_BUNDLE_ID,
    AnnotatedGrant,
    GrantSpec,
)
from jhin_secrets import SecretCrypto

# Stands in for the sandbox that does not exist yet while a plan is
# previewed; never written anywhere.
PLACEHOLDER_SANDBOX_ID = "00000000-0000-4000-8000-000000000000"
_NIL_UUID = UUID(int=0)
_MAX_NAME_CHARS = 200


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _bundle_or_404(bundle_id: str) -> Bundle:
    bundle = bundle_by_id(bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"No capability bundle '{bundle_id}'. Choose one of: {', '.join(BUNDLE_IDS)}."),
        )
    return bundle


def _rule_out(rule: PolicyRule) -> PolicyRuleIn:
    return PolicyRuleIn.model_validate(rule.model_dump(mode="json"))


def may_read_connections(ctx: WorkspaceContext) -> bool:
    """Whether this caller could list the workspace's connections itself.

    Bundle reads are viewer+, but a need's ``choices`` are connection ids,
    names, statuses and allow-lists — the inventory ``GET /connections``
    shows admins only, and API keys only with its scope. So the choices go
    to a caller who holds the admin role and, for a key, that route's own
    scope; everyone else gets the need without them.
    """
    if not role_satisfies(ctx.role, WorkspaceRole.ADMIN):
        return False
    if ctx.api_key is None:
        return True
    needed = required_scope("GET", f"{WORKSPACE_PREFIX}/connections")
    return needed is not None and needed in ctx.api_key.scopes


def _need_out(need: Need, *, include_connections: bool) -> BundleNeedOut:
    return BundleNeedOut(
        kind=need.kind,
        connector_type=need.connector_type,
        choices=[
            BundleNeedChoiceOut(
                id=UUID(choice.id),
                name=choice.name,
                status=choice.status,
                allowed_repositories=(
                    list(choice.allowed_repositories)
                    if choice.allowed_repositories is not None
                    else None
                ),
            )
            for choice in need.choices
        ]
        if include_connections
        else [],
        detail=need.detail,
    )


def _grant_out(
    row: AgentCapabilityGrant, problems: list[str], connection_name: str | None
) -> GrantOut:
    out = GrantOut.model_validate(row, from_attributes=True)
    return out.model_copy(update={"problems": problems, "connection_name": connection_name})


def _preview_grant_out(agent_id: UUID, spec: GrantSpec, refs: list[ConnectionRef]) -> GrantOut:
    pinned = spec.scope.get("connection_id")
    return GrantOut(
        id=_NIL_UUID,
        agent_id=agent_id,
        capability=spec.capability,
        scope_json=dict(spec.scope),
        effect=spec.effect,
        created_at=datetime.now(UTC),
        problems=[],
        connection_name=next((ref.name for ref in refs if ref.id == pinned), None),
    )


def _annotated_tuples(rows: list[grants.AnnotatedGrantRow]) -> list[AnnotatedGrant]:
    return [
        (row.capability, row.scope_json, row.effect, tuple(problems))
        for row, problems, _name in rows
    ]


def _bundle_out(
    bundle: Bundle,
    catalog: list[ToolDefinition],
    refs: list[ConnectionRef],
    *,
    include_connections: bool,
) -> BundleOut:
    by_name = {definition.name: definition for definition in catalog}
    plan = plan_bundle(bundle, catalog=catalog, connections=refs)
    missing = [name for name in bundle.tools if name not in by_name]
    if len(missing) == len(bundle.tools):
        state = "unavailable"
    elif plan.needs:
        state = "needs"
    else:
        state = "ready"
    return BundleOut(
        id=bundle.id,
        label=bundle.label,
        summary=bundle.summary,
        description=bundle.description,
        tools=[
            BundleToolOut(
                name=name,
                capability=by_name[name].required_capability if name in by_name else name,
                scope=dict(scope),
            )
            for name, scope in bundle.tools.items()
        ],
        rules=[_rule_out(rule) for rule in bundle.policy_rules],
        not_included=list(bundle.not_included),
        readiness=BundleReadinessOut(
            state=state,
            needs=[_need_out(need, include_connections=include_connections) for need in plan.needs],
            missing_tools=missing,
        ),
    )


async def workspace_bundles(
    db: AsyncSession, workspace_id: UUID, *, include_connections: bool = True
) -> list[BundleOut]:
    """Every bundle and its readiness. ``include_connections`` is what
    :func:`may_read_connections` answers for the caller; the console, which
    is the operator, always sees the choices."""
    catalog = await grants.workspace_catalog(db, workspace_id)
    refs = await connection_refs(db, workspace_id)
    return [
        _bundle_out(bundle, catalog, refs, include_connections=include_connections)
        for bundle in BUNDLES
    ]


async def _agent_grant_rows(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID
) -> list[AgentCapabilityGrant]:
    rows = await db.scalars(
        select(AgentCapabilityGrant)
        .where(
            AgentCapabilityGrant.agent_id == agent_id,
            AgentCapabilityGrant.workspace_id == workspace_id,
        )
        .order_by(AgentCapabilityGrant.created_at)
    )
    return list(rows)


async def agent_bundles(
    db: AsyncSession, workspace_id: UUID, agent_id: UUID, *, include_connections: bool = True
) -> list[BundleStatusOut]:
    await agents_service.get_agent(db, workspace_id, agent_id)
    catalog = await grants.workspace_catalog(db, workspace_id)
    refs = await connection_refs(db, workspace_id)
    rows = await _agent_grant_rows(db, workspace_id, agent_id)
    annotated = [
        (
            row,
            *grants.annotate_grant(
                row, catalog=catalog, connections=refs, redact=not include_connections
            ),
        )
        for row in rows
    ]
    tuples = _annotated_tuples(annotated)
    out: list[BundleStatusOut] = []
    for bundle in BUNDLES:
        capabilities = bundle_capabilities(bundle, catalog)
        covered = covered_capabilities(capabilities, tuples)
        out.append(
            BundleStatusOut(
                **_bundle_out(
                    bundle, catalog, refs, include_connections=include_connections
                ).model_dump(),
                state=bundle_state(bundle, grants=tuples, catalog=catalog),
                granted_capabilities=list(covered),
                missing_capabilities=[cap for cap in capabilities if cap not in covered],
                problems=[
                    BundleGrantProblemOut(
                        grant_id=row.id, capability=row.capability, problems=list(problems)
                    )
                    for row, problems, _name in annotated
                    if problems
                    and any(capability_matches(row.capability, cap) for cap in capabilities)
                ],
            )
        )
    return out


def _validate_sandbox(
    bundle: Bundle, request: BundleApply, refs: list[ConnectionRef]
) -> tuple[str, ConnectionRef, list[str]]:
    """The sandbox the request asks to create, checked before anything is
    written: its name, the GitHub connection it borrows from, and its
    allow-list."""
    sandbox = request.sandbox
    assert sandbox is not None
    if bundle.id != CODE_EDITING_BUNDLE_ID:
        raise _bad_request("Only Code editing creates a sandbox.")
    if "cli" in request.connections:
        raise _bad_request("Pass either a sandbox to create or an existing one, not both.")
    git_id = str(sandbox.git_connection_id)
    github = next(
        (
            ref
            for ref in refs
            if ref.id == git_id and ref.connector_type == "github" and ref.status == "active"
        ),
        None,
    )
    if github is None:
        raise _bad_request(f"'{git_id}' is not an active GitHub connection in this workspace.")
    taken = next(
        (
            ref
            for ref in refs
            if ref.connector_type == "cli"
            and ref.status == "active"
            and ref.git_connection_id == git_id
        ),
        None,
    )
    if taken is not None:
        raise _bad_request(
            f"A CLI Sandbox connection '{taken.name}' already uses '{github.name}' for "
            "repository jobs; pick it under connections.cli instead of creating another."
        )
    allowed: list[str] = []
    for raw in sandbox.allowed_repositories:
        entry = raw.strip()
        if not entry:
            continue
        if not is_repository_pattern(entry):
            raise _bad_request(
                f"'{entry}' is not a repository: use owner/name, owner/*, or * for every "
                "repository."
            )
        if entry not in allowed:
            allowed.append(entry)
    if not allowed:
        raise _bad_request(
            "A sandbox with no allowed repositories can check out nothing; list at least one, "
            "or * for every repository."
        )
    name = sandbox.name.strip() or f"Sandbox for {github.name}"
    return name[:_MAX_NAME_CHARS], github, allowed


def _warnings(
    bundle: Bundle,
    catalog: list[ToolDefinition],
    existing: list[grants.AnnotatedGrantRow],
) -> list[str]:
    capabilities = bundle_capabilities(bundle, catalog)
    by_name = {definition.name: definition for definition in catalog}
    requiring = [
        by_name[name].required_capability
        for name in bundle.tools
        if name in by_name and by_name[name].required_grant_scope_keys
    ]
    warnings: list[str] = []
    for row, _problems, _name in existing:
        if row.effect == "deny" and any(
            capability_matches(row.capability, cap) for cap in capabilities
        ):
            warnings.append(
                f"An explicit deny on {row.capability} for this agent still wins; remove it "
                "under Capability grants if the agent should use it."
            )
        elif (
            row.effect == "allow"
            and (row.capability == "*" or row.capability.endswith(".*"))
            and any(capability_matches(row.capability, cap) for cap in requiring)
        ):
            warnings.append(
                f"A wildcard grant ({row.capability}) also covers these tools; the rows written "
                "here are what make checkout, push and pull-request calls pass."
            )
    return warnings


def _callable_tools(
    catalog: list[ToolDefinition],
    planned: tuple[GrantSpec, ...],
    existing: list[grants.AnnotatedGrantRow],
) -> list[str]:
    patterns = [spec.capability for spec in planned] + [
        row.capability
        for row, problems, _name in existing
        if row.effect == "allow" and not problems
    ]
    return [
        definition.name
        for definition in catalog
        if any(capability_matches(pattern, definition.required_capability) for pattern in patterns)
    ]


async def apply_bundle(
    db: AsyncSession,
    crypto: SecretCrypto | None,
    ctx: WorkspaceContext,
    agent_id: UUID,
    bundle_id: str,
    request: BundleApply,
    *,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
    include_connections: bool = True,
) -> BundleApplyOut:
    """Turn a bundle on for one agent, in one transaction.

    Refusals are 422 with the planner's sentences and write nothing; an open
    question comes back as ``needs`` (200) and writes nothing; ``dry_run``
    computes everything and writes nothing. A sandbox is created only once
    the plan against a stand-in for it has no refusal, so a refused request
    never leaves a connection behind. ``include_connections`` decides whether
    a need carries the connection choices (:func:`may_read_connections`).
    """
    agent = await agents_service._get_locked_agent(db, ctx.workspace_id, agent_id)
    bundle = _bundle_or_404(bundle_id)
    catalog = await grants.workspace_catalog(db, ctx.workspace_id)
    refs = await connection_refs(db, ctx.workspace_id)
    existing_rules = grants.parse_rules(list(agent.approval_policy_json or []))
    chosen = {kind: str(connection_id) for kind, connection_id in request.connections.items()}
    metadata = {"bundle": bundle.id, **(extra_metadata or {})}

    planning_refs = list(refs)
    sandbox_plan: tuple[str, ConnectionRef, list[str]] | None = None
    if request.sandbox is not None:
        sandbox_plan = _validate_sandbox(bundle, request, refs)
        name, github, allowed = sandbox_plan
        planning_refs.append(
            ConnectionRef(
                id=PLACEHOLDER_SANDBOX_ID,
                connector_type="cli",
                name=name,
                status="active",
                git_connection_id=github.id,
                allowed_repositories=tuple(allowed),
            )
        )
        chosen["cli"] = PLACEHOLDER_SANDBOX_ID

    def plan_against(connections: list[ConnectionRef]) -> Any:
        return plan_bundle(
            bundle,
            catalog=catalog,
            connections=connections,
            existing_rules=existing_rules,
            chosen=chosen,
            repositories=request.repositories,
            base=request.base,
        )

    plan = plan_against(planning_refs)
    if plan.refusals:
        raise _bad_request(" ".join(plan.refusals))

    rows = await _agent_grant_rows(db, ctx.workspace_id, agent_id)
    existing = [
        (row, *grants.annotate_grant(row, catalog=catalog, connections=refs)) for row in rows
    ]
    if plan.needs:
        return BundleApplyOut(
            bundle_id=bundle.id,
            dry_run=request.dry_run,
            needs=[_need_out(need, include_connections=include_connections) for need in plan.needs],
            callable_tools=_callable_tools(catalog, (), existing),
            warnings=_warnings(bundle, catalog, existing),
        )

    created_connection = None
    if sandbox_plan is not None and not request.dry_run:
        name, github, allowed = sandbox_plan
        if crypto is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Secret encryption is unavailable: no master key configured",
            )
        connection, _secret = await connections_service._create_connection(
            db,
            crypto,
            ctx,
            connector_type="cli",
            name=name,
            auth_type="none",
            credentials={},
            config={
                "default_network": "none",
                "git_connection_id": github.id,
                "allowed_repositories": allowed,
            },
            request_id=request_id,
            ip_hash=ip_hash,
            actor_type=actor_type,
            extra_audit_metadata={**metadata, "agent_id": str(agent_id)},
        )
        refs = [*refs, connection_ref(connection)]
        chosen["cli"] = str(connection.id)
        plan = plan_against(refs)
        if plan.refusals or plan.needs:  # pragma: no cover - same inputs, real id
            raise _bad_request(" ".join(plan.refusals) or "The sandbox could not be planned.")
        created_connection = await serialize_connection(db, connection)
    elif sandbox_plan is not None:
        refs = planning_refs

    grants_created: list[GrantOut] = []
    grants_existing: list[GrantOut] = []
    bundle_rule_capabilities = {rule.capability for rule in bundle.policy_rules}
    rules_kept = [rule for rule in existing_rules if rule.capability in bundle_rule_capabilities]
    if request.dry_run:
        for spec in plan.grants:
            match = next(
                (
                    entry
                    for entry in existing
                    if entry[0].capability == spec.capability
                    and entry[0].effect == spec.effect
                    and entry[0].scope_json == spec.scope
                ),
                None,
            )
            if match is not None:
                grants_existing.append(_grant_out(*match))
            else:
                grants_created.append(_preview_grant_out(agent_id, spec, refs))
        rules_added = list(plan.rules)
    else:
        for spec in plan.grants:
            row, created = await grants._write_grant(
                db,
                ctx,
                agent_id,
                capability=spec.capability,
                scope=spec.scope,
                effect=spec.effect,
                request_id=request_id,
                ip_hash=ip_hash,
                actor_type=actor_type,
                extra_metadata=metadata,
            )
            problems, connection_name = grants.annotate_grant(
                row, catalog=catalog, connections=refs
            )
            out = _grant_out(row, problems, connection_name)
            (grants_created if created else grants_existing).append(out)
        rules_added = await grants.ensure_capability_rules(
            db,
            ctx,
            agent,
            plan.rules,
            request_id=request_id,
            ip_hash=ip_hash,
            actor_type=actor_type,
            extra_metadata=extra_metadata,
            bundle_id=bundle.id,
        )
        await db.commit()

    return BundleApplyOut(
        bundle_id=bundle.id,
        dry_run=request.dry_run,
        created_connection=created_connection,
        grants_created=grants_created,
        grants_existing=grants_existing,
        rules_added=[_rule_out(rule) for rule in rules_added],
        rules_kept=[_rule_out(rule) for rule in rules_kept],
        callable_tools=_callable_tools(catalog, plan.grants, existing),
        warnings=_warnings(bundle, catalog, existing),
    )


_SCOPE_KEYS_THE_BUNDLE_FILLS = frozenset({"connection_id", "repository", "branch", "base"})


def _bundle_would_write(
    bundle: Bundle, catalog: list[ToolDefinition], row: AgentCapabilityGrant
) -> bool:
    """Is this row's scope one the bundle itself writes for this capability?

    The connection, the repository entries and the base branch vary per
    apply; everything else is fixed, and a row with any other key or value
    was made by hand.
    """
    scope = row.scope_json if isinstance(row.scope_json, dict) else {}
    by_name = {definition.name: definition for definition in catalog}
    for name, fixed in bundle.tools.items():
        definition = by_name.get(name)
        if definition is None or definition.required_capability != row.capability:
            continue
        expected_keys = {
            key
            for key in definition.scope_keys
            if key in _SCOPE_KEYS_THE_BUNDLE_FILLS or key in fixed
        }
        if set(scope) != expected_keys:
            continue
        if any(scope.get(key) != value for key, value in fixed.items() if key != "repository"):
            continue
        if "branch" in scope and scope["branch"] != AGENT_BRANCH_PATTERN:
            continue
        return True
    return False


async def remove_bundle(
    db: AsyncSession,
    ctx: WorkspaceContext,
    agent_id: UUID,
    bundle_id: str,
    *,
    dry_run: bool,
    request_id: UUID,
    ip_hash: str | None,
    actor_type: ActorType = ActorType.USER,
    extra_metadata: dict[str, Any] | None = None,
) -> BundleRemoveOut:
    """Revoke the allow grants a bundle owns and no other bundle that is on
    still needs. Policy rules are left alone; ``dry_run`` names the rows."""
    await agents_service._get_locked_agent(db, ctx.workspace_id, agent_id)
    bundle = _bundle_or_404(bundle_id)
    catalog = await grants.workspace_catalog(db, ctx.workspace_id)
    refs = await connection_refs(db, ctx.workspace_id)
    rows = await _agent_grant_rows(db, ctx.workspace_id, agent_id)
    annotated = [
        (row, *grants.annotate_grant(row, catalog=catalog, connections=refs)) for row in rows
    ]
    tuples = _annotated_tuples(annotated)
    owned = set(bundle_capabilities(bundle, catalog))
    for other in BUNDLES:
        if other.id == bundle.id:
            continue
        if bundle_state(other, grants=tuples, catalog=catalog) == "on":
            owned -= set(bundle_capabilities(other, catalog))
    targets = [
        entry for entry in annotated if entry[0].effect == "allow" and entry[0].capability in owned
    ]
    hand_made = [entry for entry in targets if not _bundle_would_write(bundle, catalog, entry[0])]
    revoked = [_grant_out(*entry) for entry in targets]
    if not dry_run:
        for row, _problems, _name in targets:
            audit.record(
                db,
                action="agent.permission.revoked",
                target_type="agent",
                target_id=agent_id,
                workspace_id=ctx.workspace_id,
                actor_type=actor_type,
                actor_id=ctx.user.id,
                request_id=request_id,
                ip_hash=ip_hash,
                metadata={
                    "grant_id": str(row.id),
                    "capability": row.capability,
                    "effect": row.effect,
                    "bundle": bundle.id,
                    **(extra_metadata or {}),
                },
            )
            await db.delete(row)
        await db.commit()
    return BundleRemoveOut(
        bundle_id=bundle.id,
        dry_run=dry_run,
        revoked=revoked,
        hand_made=[_grant_out(*entry) for entry in hand_made],
    )


__all__ = [
    "PLACEHOLDER_SANDBOX_ID",
    "agent_bundles",
    "apply_bundle",
    "may_read_connections",
    "remove_bundle",
    "workspace_bundles",
]
