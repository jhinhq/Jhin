"""Skills management service: RBAC, CRUD with versioning, starter install,
import review flow, per-agent enablement, and audit — against SQLite."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_api.deps import WorkspaceContext
from jhin_api.skills import service
from jhin_api.skills.schemas import SkillCreate, SkillFile, SkillUpdate
from jhin_db.models import Agent, AgentSkill, AuditEvent, Skill, User
from jhin_domain import WorkspaceRole, new_uuid7
from jhin_skills import load_zip

GOOD_SKILL = "---\nname: {name}\ndescription: Description of {name}.\n---\n\nInstructions.\n"


async def ctx_with_role(
    session: AsyncSession, admin_ctx: WorkspaceContext, role: WorkspaceRole
) -> WorkspaceContext:
    user = User(
        email=f"{role.value}-{new_uuid7().hex[:6]}@example.com", display_name="U", password_hash="x"
    )
    session.add(user)
    await session.flush()
    return WorkspaceContext(user=user, workspace_id=admin_ctx.workspace_id, role=role)


@pytest.fixture
async def member_ctx(session: AsyncSession, admin_ctx: WorkspaceContext) -> WorkspaceContext:
    return await ctx_with_role(session, admin_ctx, WorkspaceRole.MEMBER)


@pytest.fixture
async def agent(session: AsyncSession, admin_ctx: WorkspaceContext) -> Agent:
    record = Agent(workspace_id=admin_ctx.workspace_id, name="Ava", slug="ava")
    session.add(record)
    await session.flush()
    return record


def payload(name: str = "release-notes", **overrides: object) -> SkillCreate:
    values: dict[str, object] = {
        "name": name,
        "description": f"Description of {name}.",
        "content": f"# {name}\n\nDo it well.",
    }
    values.update(overrides)
    return SkillCreate.model_validate(values)


async def audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id))
    return [row.action for row in rows]


def make_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


class TestCreate:
    async def test_admin_creates_a_custom_enabled_skill(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        record = await service.create_skill(session, admin_ctx, payload())
        assert record.source == "custom"
        assert record.enabled is True
        assert record.version == 1
        assert "skill.created" in await audit_actions(session)

    async def test_member_cannot_create(
        self, session: AsyncSession, member_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_skill(session, member_ctx, payload())
        assert exc.value.status_code == 403

    async def test_duplicate_name_conflicts(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        await service.create_skill(session, admin_ctx, payload())
        with pytest.raises(HTTPException) as exc:
            await service.create_skill(session, admin_ctx, payload())
        assert exc.value.status_code == 409

    async def test_secret_content_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_skill(session, admin_ctx, payload(content="token ghp_" + "a" * 36))
        assert exc.value.status_code == 422

    async def test_oversize_file_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.create_skill(
                session,
                admin_ctx,
                payload(files=[{"path": "big.md", "content": "x" * (64 * 1024 + 1)}]),
            )
        assert exc.value.status_code == 422

    async def test_bad_file_path_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException):
            await service.create_skill(
                session, admin_ctx, payload(files=[{"path": "../up.md", "content": "x"}])
            )


class TestUpdateAndDelete:
    async def test_content_change_bumps_the_version(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        record = await service.create_skill(session, admin_ctx, payload())
        updated = await service.update_skill(
            session, admin_ctx, record.id, SkillUpdate(content="# new body")
        )
        assert updated.version == 2
        again = await service.update_skill(
            session, admin_ctx, record.id, SkillUpdate(description="New description.")
        )
        assert again.version == 2  # metadata-only edits do not bump

    async def test_enable_toggle_is_audited(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        record = await service.create_skill(session, admin_ctx, payload())
        await service.update_skill(session, admin_ctx, record.id, SkillUpdate(enabled=False))
        actions = await audit_actions(session)
        assert "skill.disabled" in actions
        await service.update_skill(session, admin_ctx, record.id, SkillUpdate(enabled=True))
        assert "skill.enabled" in await audit_actions(session)

    async def test_update_rejects_secrets(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        record = await service.create_skill(session, admin_ctx, payload())
        with pytest.raises(HTTPException) as exc:
            await service.update_skill(
                session,
                admin_ctx,
                record.id,
                SkillUpdate(files=[SkillFile(path="k.md", content="AKIAABCDEFGHIJKLMNOP")]),
            )
        assert exc.value.status_code == 422

    async def test_delete_removes_the_skill_and_enablements(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
    ) -> None:
        record = await service.create_skill(session, admin_ctx, payload())
        await service.set_agent_skills(session, admin_ctx, agent.id, [record.id])
        await service.delete_skill(session, admin_ctx, record.id)
        assert await session.scalar(select(Skill)) is None
        assert await session.scalar(select(AgentSkill)) is None
        assert "skill.deleted" in await audit_actions(session)


class TestInstallBuiltins:
    async def test_installs_five_then_skips(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        installed, skipped = await service.install_builtins(session, admin_ctx)
        assert len(installed) == 5
        assert skipped == []
        rows = list(await session.scalars(select(Skill)))
        assert all(row.source == "built_in" and row.enabled for row in rows)
        release = next(row for row in rows if row.name == "release-notes")
        assert [entry["path"] for entry in release.files_json] == ["template.md"]

        installed_again, skipped_again = await service.install_builtins(session, admin_ctx)
        assert installed_again == []
        assert len(skipped_again) == 5
        assert "skill.builtins_installed" in await audit_actions(session)

    async def test_member_cannot_install(
        self, session: AsyncSession, member_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.install_builtins(session, member_ctx)
        assert exc.value.status_code == 403


class TestImport:
    async def test_imported_skills_arrive_disabled_for_review(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        bundle = load_zip(
            make_zip(
                {
                    "repo-HEAD/writing/blog/SKILL.md": GOOD_SKILL.format(name="blog"),
                    "repo-HEAD/writing/blog/outline.md": "## outline",
                }
            )
        )
        results, created, skipped = await service.import_bundle(
            session, admin_ctx, bundle, source_url="https://github.com/a/b"
        )
        assert (created, skipped) == (1, 0)
        assert results[0]["status"] == "proposed"
        record = await session.scalar(select(Skill))
        assert record is not None
        assert record.enabled is False
        assert record.source == "imported"
        assert record.source_url == "https://github.com/a/b"
        assert [entry["path"] for entry in record.files_json] == ["outline.md"]
        assert "skill.imported" in await audit_actions(session)

    async def test_name_conflicts_are_skipped(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        await service.create_skill(session, admin_ctx, payload(name="blog"))
        bundle = load_zip(make_zip({"blog/SKILL.md": GOOD_SKILL.format(name="blog")}))
        results, created, skipped = await service.import_bundle(
            session, admin_ctx, bundle, source_url=""
        )
        assert (created, skipped) == (0, 1)
        assert results[0]["status"] == "skipped"


class TestAgentSkills:
    async def test_replace_set_and_list(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
    ) -> None:
        first = await service.create_skill(session, admin_ctx, payload(name="first"))
        second = await service.create_skill(session, admin_ctx, payload(name="second"))
        pairs = await service.set_agent_skills(session, admin_ctx, agent.id, [first.id])
        assert [(skill.name, enabled) for skill, enabled in pairs] == [
            ("first", True),
            ("second", False),
        ]
        pairs = await service.set_agent_skills(session, admin_ctx, agent.id, [second.id])
        assert [(skill.name, enabled) for skill, enabled in pairs] == [
            ("first", False),
            ("second", True),
        ]
        assert "agent.skills_updated" in await audit_actions(session)

    async def test_unknown_skill_id_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, agent: Agent
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.set_agent_skills(session, admin_ctx, agent.id, [new_uuid7()])
        assert exc.value.status_code == 422

    async def test_unknown_agent_is_404(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.list_agent_skills(session, admin_ctx.workspace_id, new_uuid7())
        assert exc.value.status_code == 404

    async def test_member_cannot_change_enablement(
        self, session: AsyncSession, member_ctx: WorkspaceContext, agent: Agent
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.set_agent_skills(session, member_ctx, agent.id, [])
        assert exc.value.status_code == 403


class TestListing:
    async def test_search_and_source_filters(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        await service.create_skill(session, admin_ctx, payload(name="release-notes"))
        await service.create_skill(session, admin_ctx, payload(name="bug-triage"))
        items, total = await service.list_skills(session, admin_ctx.workspace_id, q="release")
        assert total == 1
        assert items[0].name == "release-notes"
        items, total = await service.list_skills(session, admin_ctx.workspace_id, source="built_in")
        assert total == 0
        with pytest.raises(HTTPException):
            await service.list_skills(session, admin_ctx.workspace_id, source="nope")
