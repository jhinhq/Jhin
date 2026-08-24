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
from jhin_skills import SkillImportError, load_zip

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


# --- Browse gallery (docs/architecture/skills.md) ---------------------------
#
# The fixture below mirrors the *real* top-level layout of
# github.com/anthropics/skills as of this writing, confirmed with one live
# codeload fetch during development: a wrapped "{repo}-{ref}/" root, an
# inner "skills/" folder holding one subfolder per skill, and a lone
# "template/SKILL.md" at the repo root. Fixturing it here keeps the tests
# offline and fast.
ANTHROPICS_SOURCE = "anthropics/skills"


def make_anthropics_like_zip() -> bytes:
    return make_zip(
        {
            "skills-HEAD/README.md": "# skills\n",
            "skills-HEAD/skills/pdf/SKILL.md": GOOD_SKILL.format(name="pdf"),
            "skills-HEAD/skills/pdf/reference.md": "## PDF forms\n",
            "skills-HEAD/skills/docx/SKILL.md": GOOD_SKILL.format(name="docx"),
            "skills-HEAD/template/SKILL.md": GOOD_SKILL.format(name="template-skill"),
        }
    )


class FakeFetch:
    """A stand-in for ``jhin_skills.fetch_github_repo_zip`` that returns the
    fixture zip for any ref under ``anthropics/skills``, computing the real
    (path_prefix, source_url) the live function would from the ref — so the
    service's path-prefix filtering and source_url provenance are exercised
    exactly as they would be against the real repository."""

    def __init__(self, zip_bytes: bytes) -> None:
        self.zip_bytes = zip_bytes
        self.calls: list[str] = []

    async def __call__(self, ref: str) -> tuple[bytes, str, str]:
        from jhin_skills import parse_github_ref, source_url_for

        self.calls.append(ref)
        owner, repo, path = parse_github_ref(ref)
        return self.zip_bytes, path, source_url_for(owner, repo, path)


def fake_fetch(zip_bytes: bytes) -> FakeFetch:
    return FakeFetch(zip_bytes)


class TestSkillSources:
    def test_catalog_includes_anthropics_skills(self) -> None:
        sources = service.list_skill_sources()
        assert any(entry["source"] == ANTHROPICS_SOURCE for entry in sources)
        assert all({"source", "label", "description", "url"} <= entry.keys() for entry in sources)


class TestBrowse:
    def setup_method(self) -> None:
        service.reset_browse_cache()

    def teardown_method(self) -> None:
        service.reset_browse_cache()

    async def test_lists_parsed_skills_without_creating_any(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        entries = await service.browse_source(
            session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE
        )
        names = {entry["name"] for entry in entries}
        assert names == {"pdf", "docx", "template-skill"}
        assert all(entry["installed"] is False for entry in entries)
        assert await session.scalar(select(Skill)) is None  # browsing never creates skills

    async def test_search_filters_name_and_description(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        entries = await service.browse_source(
            session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE, q="pdf"
        )
        assert [entry["name"] for entry in entries] == ["pdf"]
        empty = await service.browse_source(
            session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE, q="nothing-matches-this"
        )
        assert empty == []

    async def test_unknown_source_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.browse_source(
                session, admin_ctx.workspace_id, source="someone/other-repo"
            )
        assert exc.value.status_code == 422

    async def test_unreachable_github_is_a_friendly_422(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(ref: str) -> tuple[bytes, str, str]:
            raise SkillImportError(f"could not reach GitHub for {ref}")

        monkeypatch.setattr(service, "fetch_github_repo_zip", _boom)
        with pytest.raises(HTTPException) as exc:
            await service.browse_source(session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE)
        assert exc.value.status_code == 422
        assert "could not reach GitHub" in exc.value.detail

    async def test_repeated_browse_hits_the_cache(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = fake_fetch(make_anthropics_like_zip())
        monkeypatch.setattr(service, "fetch_github_repo_zip", fetch)
        await service.browse_source(session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE)
        await service.browse_source(
            session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE, q="p"
        )
        assert fetch.calls == [ANTHROPICS_SOURCE]  # second call served from cache

    async def test_expired_cache_entry_is_refetched(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = fake_fetch(make_anthropics_like_zip())
        monkeypatch.setattr(service, "fetch_github_repo_zip", fetch)
        await service.browse_source(session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE)
        cached = service._browse_cache[ANTHROPICS_SOURCE]
        cached.fetched_at -= service._BROWSE_CACHE_TTL_SECONDS + 1
        await service.browse_source(session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE)
        assert fetch.calls == [ANTHROPICS_SOURCE, ANTHROPICS_SOURCE]

    async def test_previously_installed_skill_is_marked(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        await service.install_from_browse(
            session, admin_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
        )
        service.reset_browse_cache()
        entries = await service.browse_source(
            session, admin_ctx.workspace_id, source=ANTHROPICS_SOURCE
        )
        by_name = {entry["name"]: entry for entry in entries}
        assert by_name["pdf"]["installed"] is True
        assert by_name["docx"]["installed"] is False


class TestBrowseInstall:
    async def test_installs_one_skill_enabled_and_audited(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = fake_fetch(make_anthropics_like_zip())
        monkeypatch.setattr(service, "fetch_github_repo_zip", fetch)
        record, created = await service.install_from_browse(
            session, admin_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
        )
        assert created is True
        assert record.name == "pdf"
        assert record.source == "imported"
        assert record.enabled is True  # curated source: enabled immediately, no review queue
        assert record.source_url == "https://github.com/anthropics/skills/tree/HEAD/skills/pdf"
        assert fetch.calls == ["anthropics/skills/skills/pdf"]
        assert "skill.browse_installed" in await audit_actions(session)

    async def test_retry_is_idempotent(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        first, first_created = await service.install_from_browse(
            session, admin_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
        )
        second, second_created = await service.install_from_browse(
            session, admin_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
        )
        assert first_created is True
        assert second_created is False
        assert second.id == first.id
        rows = list(await session.scalars(select(Skill)))
        assert len(rows) == 1  # no duplicate

    async def test_member_cannot_install(
        self, session: AsyncSession, member_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        with pytest.raises(HTTPException) as exc:
            await service.install_from_browse(
                session, member_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
            )
        assert exc.value.status_code == 403

    async def test_unknown_source_is_rejected(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await service.install_from_browse(
                session, admin_ctx, source="someone/other-repo", skill_path="skills/pdf"
            )
        assert exc.value.status_code == 422

    async def test_name_collision_from_a_different_source_conflicts(
        self, session: AsyncSession, admin_ctx: WorkspaceContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await service.create_skill(session, admin_ctx, payload(name="pdf"))
        monkeypatch.setattr(
            service, "fetch_github_repo_zip", fake_fetch(make_anthropics_like_zip())
        )
        with pytest.raises(HTTPException) as exc:
            await service.install_from_browse(
                session, admin_ctx, source=ANTHROPICS_SOURCE, skill_path="skills/pdf"
            )
        assert exc.value.status_code == 409


class TestNewWorkspaceDefaults:
    async def test_installs_five_active_starters(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        installed, skipped = await service.install_builtins_for_new_workspace(
            session, admin_ctx.workspace_id, actor_id=admin_ctx.user.id
        )
        await session.commit()
        assert len(installed) == 5
        assert skipped == []
        rows = list(await session.scalars(select(Skill)))
        assert len(rows) == 5
        assert all(row.enabled and row.source == "built_in" for row in rows)
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "skill.builtins_installed")
        )
        assert event is not None
        assert event.metadata_json["source"] == "default"

    async def test_install_missing_defaults_only_adds_what_is_missing(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        await service.create_skill(session, admin_ctx, payload(name="release-notes"))
        installed, skipped = await service.install_builtins(session, admin_ctx)
        assert "release-notes" not in installed
        assert "release-notes" in skipped
        assert len(installed) == 4
        rows = list(await session.scalars(select(Skill)))
        assert len(rows) == 5
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "skill.builtins_installed")
        )
        assert event is not None
        assert event.metadata_json["source"] == "manual"

    async def test_install_missing_defaults_is_idempotent(
        self, session: AsyncSession, admin_ctx: WorkspaceContext
    ) -> None:
        await service.install_builtins(session, admin_ctx)
        installed_again, skipped_again = await service.install_builtins(session, admin_ctx)
        assert installed_again == []
        assert len(skipped_again) == 5
