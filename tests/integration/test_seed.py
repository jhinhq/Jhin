"""Seed data integration test: `make seed` content is correct and idempotent."""

from __future__ import annotations

import pytest

from jhin_api.seed import DEV_OWNER_EMAIL, DEV_OWNER_PASSWORD, seed

from .test_phase2_api import ApiHarness, api, migrated_db_url  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


async def test_seed_creates_documented_dev_org(api: ApiHarness) -> None:  # noqa: F811
    async with api.session_factory() as session:
        first = await seed(session)
        assert first.startswith("seeded:")
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
