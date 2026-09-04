"""Seed data integration test: `make seed` content is correct and idempotent."""

from __future__ import annotations

import base64
import os

import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD, SHOWCASE_TRIGGER_NAME, seed

from .test_phase2_api import ApiHarness, api, migrated_db_url  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


async def test_seed_creates_documented_dev_org(
    api: ApiHarness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A master key makes the seed create the Phase 7 showcase (fake Linear
    # connection + trigger) instead of skipping it.
    monkeypatch.setenv("MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    async with api.session_factory() as session:
        first = await seed(session)
        assert first.startswith("seeded:")
        assert SHOWCASE_TRIGGER_NAME in first
    async with api.session_factory() as session:
        second = await seed(session)
        assert second.startswith("already seeded")

    login = await api.client.post(
        "/api/v1/auth/login", json={"email": DEV_OWNER_EMAIL, "password": DEV_OWNER_PASSWORD}
    )
    assert login.status_code == 200, login.text
    ws = login.json()["memberships"][0]["workspace_id"]

    graph = (await api.client.get(f"/api/v1/workspaces/{ws}/org-graph")).json()
    teams = {t["name"]: t for t in graph["teams"]}
    agents = {a["name"]: a for a in graph["agents"]}
    assert set(teams) == {"Engineering", "Marketing"}
    assert set(agents) == {
        "CTO",
        "Senior Software Engineer",
        "QA Engineer",
        "Marketing Director",
        "Blogger",
    }
    assert agents["Senior Software Engineer"]["manager_agent_id"] == agents["CTO"]["id"]
    assert agents["QA Engineer"]["manager_agent_id"] == agents["CTO"]["id"]
    assert agents["Blogger"]["manager_agent_id"] == agents["Marketing Director"]["id"]
    assert teams["Engineering"]["manager_agent_id"] == agents["CTO"]["id"]
    assert teams["Marketing"]["manager_agent_id"] == agents["Marketing Director"]["id"]

    # Phase 7 showcase: the fake Linear connection and the ready-to-demo
    # trigger, wired to the Senior Software Engineer with comment-back on.
    connections = (await api.client.get(f"/api/v1/workspaces/{ws}/connections")).json()
    linear = [c for c in connections if c["connector_type"] == "linear"]
    assert len(linear) == 1, connections

    triggers = (await api.client.get(f"/api/v1/workspaces/{ws}/triggers")).json()
    showcase = {t["name"]: t for t in triggers}[SHOWCASE_TRIGGER_NAME]
    assert showcase["enabled"] is True
    assert showcase["connection_id"] == linear[0]["id"]
    assert showcase["event_type"] == "connector.linear.issue.updated"
    assert showcase["target_agent_id"] == agents["Senior Software Engineer"]["id"]
    assert showcase["action_config_json"] == {"comment_back": True}

    # Phase 8: the showcase selects the engineering template with QA wired in.
    assert showcase["workflow_definition"]["template"] == "engineering_ticket"
    assert showcase["workflow_definition"]["qa_agent_id"] == agents["QA Engineer"]["id"]

    grants = (
        await api.client.get(
            f"/api/v1/workspaces/{ws}/agents/{agents['Senior Software Engineer']['id']}/grants"
        )
    ).json()
    granted = {g["capability"] for g in grants}
    assert {
        "linear.issue.read",
        "linear.issue.search",
        "linear.metadata.read",
        "linear.comment.create",
    } <= granted

    # Phase 8 delegation defaults (plan 27): CTO delegates to subordinates,
    # SWE delegates to QA (team + pin), QA gets read/test sandbox + GitHub read.
    swe_delegate = [g for g in grants if g["capability"] == "organization.delegate"]
    assert len(swe_delegate) == 1
    assert swe_delegate[0]["scope_json"]["targets"] == "team"
    assert swe_delegate[0]["scope_json"]["target_agent_id"] == [agents["QA Engineer"]["id"]]

    cto_grants = (
        await api.client.get(f"/api/v1/workspaces/{ws}/agents/{agents['CTO']['id']}/grants")
    ).json()
    cto_delegate = [g for g in cto_grants if g["capability"] == "organization.delegate"]
    assert len(cto_delegate) == 1
    assert cto_delegate[0]["scope_json"] == {"targets": "subordinates"}

    qa_grants = (
        await api.client.get(f"/api/v1/workspaces/{ws}/agents/{agents['QA Engineer']['id']}/grants")
    ).json()
    qa_granted = {g["capability"] for g in qa_grants}
    assert {
        "organization.report_result",
        "github.repository.read",
        "github.pull_request.read",
        "github.check.read",
    } <= qa_granted
    assert "organization.delegate" not in qa_granted  # QA never delegates
    # No sandbox grants are seeded. cli.repository.checkout requires a grant
    # that names a connection and a repository, and a fresh stack has no CLI
    # Sandbox connection to name — an unscoped grant would only produce a
    # required_scope_missing denial the first time an agent tried to use it.
    assert not {capability for capability in qa_granted if capability.startswith("cli.")}

    # Every seeded teammate gets the safe-by-default collaboration baseline so
    # a fresh workspace can ask colleagues for help out of the box. QA has no
    # delegate grant but can still ask/answer and find colleagues.
    collaboration = {
        "organization.directory.read",
        "organization.work.request",
        "organization.work.respond",
    }
    assert collaboration <= qa_granted
    assert collaboration <= granted  # the Senior Software Engineer too
    qa_request = [g for g in qa_grants if g["capability"] == "organization.work.request"]
    assert qa_request and qa_request[0]["scope_json"] == {"targets": "any"}
