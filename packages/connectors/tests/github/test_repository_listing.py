"""``github.repository.list`` end to end against the in-process GitHub
double: the page walk, the filters, and — the point of the tool — the rows a
grant leaves visible.

The executor is handed the allow grants that authorized the call
(``ToolExecutionContext.authorizing_grants``, filled by the gateway) and
narrows its own output to them. That is a narrowing, not a decision: every
call here has already been allowed by the evaluator in the real path.
"""

from collections.abc import Iterator
from dataclasses import replace

import pytest

from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.schemas import RepositoryListInput, RepositoryListOutput
from jhin_connectors.testing.fake_github import DEFAULT_TOKEN, FakeGitHubServer
from jhin_db.models import Workspace
from jhin_policy import Grant, GrantEffect
from jhin_tools.builtin import ToolExecutionContext

LIST = {definition.name: executor for definition, executor in GitHubConnector().tools()}[
    "github.repository.list"
]

REPOSITORIES = "octo/alpha,octo/beta,other/gamma,Password1-io/Front-end"


def _grant(**scope: str) -> Grant:
    return Grant(capability="github.repository.list", scope=dict(scope), effect=GrantEffect.ALLOW)


EVERY_REPOSITORY = _grant(repository="*")


@pytest.fixture
def many_owners(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubServer]:
    """A token reaching several owners, and installed nowhere.

    ``installation_count=0`` is the state the operator's own browser sign-in
    is in: ``GET /user/installations`` answers zero while the token still
    reaches repositories. A listing built on installations would find none,
    so this fixture is what proves the tool does not consult them.
    """
    with FakeGitHubServer(repos=REPOSITORIES, installation_count=0) as server:
        monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
        yield server


@pytest.fixture
async def connection(  # type: ignore[no-untyped-def]
    workspace: Workspace, make_connection, many_owners: FakeGitHubServer
):
    return await make_connection(
        workspace,
        credentials={"token": DEFAULT_TOKEN},
        config={"base_url": many_owners.base_url},
    )


async def _list(
    context: ToolExecutionContext,
    connection_id: str,
    *grants: Grant,
    **arguments: object,
) -> RepositoryListOutput:
    output = await LIST(
        replace(context, authorizing_grants=grants),
        RepositoryListInput(connection_id=connection_id, **arguments),  # type: ignore[arg-type]
    )
    assert isinstance(output, RepositoryListOutput)
    # Count only the final visible rows: never provider rows removed by grants,
    # query filters, pagination limits, or the tool-result byte budget.
    assert output.returned_count == len(output.repositories)
    assert output.model_dump(mode="json")["returned_count"] == len(output.repositories)
    return output


def test_returned_count_is_part_of_the_output_schema() -> None:
    schema = RepositoryListOutput.model_json_schema()
    count = schema["properties"]["returned_count"]

    assert count["type"] == "integer"
    assert count["minimum"] == 0
    assert "returned_count" in schema["required"]
    assert "returned" in count["description"]


async def test_a_query_finds_the_repository_a_person_named_loosely(
    context: ToolExecutionContext, connection
) -> None:
    """The call the agent could not make: "the Password1 repo" -> owner/name."""
    output = await _list(context, str(connection.id), EVERY_REPOSITORY, query="password")

    assert [row.full_name for row in output.repositories] == ["Password1-io/Front-end"]
    assert not output.truncated
    row = output.repositories[0]
    assert row.private is True
    assert row.default_branch == "main"
    assert row.can_push is True
    assert row.description


async def test_it_lists_every_reachable_repository_in_name_order(
    context: ToolExecutionContext, connection
) -> None:
    output = await _list(context, str(connection.id), EVERY_REPOSITORY)

    assert [row.full_name for row in output.repositories] == [
        "Password1-io/Front-end",
        "octo/alpha",
        "octo/beta",
        "other/gamma",
    ]


async def test_a_grant_scoped_to_one_owner_hides_the_other_owners(
    context: ToolExecutionContext, connection
) -> None:
    output = await _list(context, str(connection.id), _grant(repository="octo/*"))

    assert [row.full_name for row in output.repositories] == ["octo/alpha", "octo/beta"]


async def test_a_grant_naming_one_repository_lists_only_that_one(
    context: ToolExecutionContext, connection
) -> None:
    output = await _list(context, str(connection.id), _grant(repository="octo/beta"))

    assert [row.full_name for row in output.repositories] == ["octo/beta"]


async def test_two_grants_list_the_union_of_what_they_cover(
    context: ToolExecutionContext, connection
) -> None:
    output = await _list(
        context,
        str(connection.id),
        _grant(repository="octo/alpha"),
        _grant(repository="other/*"),
    )

    assert [row.full_name for row in output.repositories] == ["octo/alpha", "other/gamma"]


async def test_a_grant_constraining_no_repository_lists_everything(
    context: ToolExecutionContext, connection
) -> None:
    """An unscoped grant is an unlimited one, exactly as ``scope_matches``
    reads it everywhere else."""
    output = await _list(context, str(connection.id), _grant(connection_id=str(connection.id)))

    assert len(output.repositories) == 4


async def test_a_listing_with_no_authorizing_grants_returns_nothing(
    context: ToolExecutionContext, connection
) -> None:
    """Fail closed: a call that lost its provenance is an empty page, never
    the whole inventory."""
    output = await _list(context, str(connection.id))

    assert output.repositories == []


async def test_the_owner_filter_narrows_further_than_the_grant(
    context: ToolExecutionContext, connection
) -> None:
    output = await _list(context, str(connection.id), EVERY_REPOSITORY, owner="OTHER")

    assert [row.full_name for row in output.repositories] == ["other/gamma"]


async def test_the_limit_cuts_and_says_so(context: ToolExecutionContext, connection) -> None:
    output = await _list(context, str(connection.id), EVERY_REPOSITORY, limit=2)

    assert [row.full_name for row in output.repositories] == [
        "Password1-io/Front-end",
        "octo/alpha",
    ]
    assert output.truncated


async def test_an_app_connection_reads_the_installation_inventory(
    context: ToolExecutionContext,
    workspace: Workspace,
    make_connection,  # type: ignore[no-untyped-def]
    many_owners: FakeGitHubServer,
) -> None:
    """An installation token may not call ``/user/repos`` at all, so the
    connection's auth type — not a probe — picks the endpoint."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    app_connection = await make_connection(
        workspace,
        auth_type="github_app",
        credentials={"app_id": "12345", "private_key": pem, "installation_id": "9"},
        config={"base_url": many_owners.base_url},
    )

    output = await _list(context, str(app_connection.id), _grant(repository="octo/*"))

    assert [row.full_name for row in output.repositories] == ["octo/alpha", "octo/beta"]


class TestManyRepositories:
    """A token reaching more repositories than one page holds."""

    NAMES = tuple(f"octo/repo-{index:03d}" for index in range(150))

    @pytest.fixture
    def crowded(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHubServer]:
        with FakeGitHubServer(repos=",".join(self.NAMES)) as server:
            monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS", server.base_url)
            yield server

    @pytest.fixture
    async def connection(  # type: ignore[no-untyped-def]
        self, workspace: Workspace, make_connection, crowded: FakeGitHubServer
    ):
        return await make_connection(
            workspace,
            credentials={"token": DEFAULT_TOKEN},
            config={"base_url": crowded.base_url},
        )

    async def test_it_walks_past_the_first_page(
        self, context: ToolExecutionContext, connection
    ) -> None:
        """A match on page two is still found, and a repository the grant
        does not cover is not — the filter runs on every page, not the
        first."""
        output = await _list(
            context, str(connection.id), _grant(repository="octo/repo-14*"), limit=100
        )

        assert [row.full_name for row in output.repositories] == [
            f"octo/repo-{index}" for index in range(140, 150)
        ]
        assert not output.truncated

    async def test_the_page_cap_is_reported_as_truncation(
        self, context: ToolExecutionContext, connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping at the cap is not the same as reaching the end, and the
        output says which happened."""
        monkeypatch.setattr("jhin_connectors.github.tools._MAX_LIST_PAGES", 1)
        output = await _list(context, str(connection.id), EVERY_REPOSITORY, limit=100)

        assert len(output.repositories) == 100
        assert output.truncated


async def test_an_owner_pattern_does_not_leak_a_similarly_named_owner(
    context: ToolExecutionContext, connection
) -> None:
    """``fnmatch``'s ``*`` crosses ``/``, so a grant of ``octo*`` would show
    every repository of ``octo-labs`` too. A filter that decides what an
    agent may SEE uses the segment-at-a-time repository matcher instead.
    """
    output = await _list(context, str(connection.id), _grant(repository="octo*"))

    assert [row.full_name for row in output.repositories] == []


async def test_it_says_when_a_grant_narrowed_the_listing(
    context: ToolExecutionContext, connection
) -> None:
    """An agent granted one owner must not report its own view as the
    connection's whole inventory. The flag says which it is; the count of
    what was withheld is not the agent's to know.
    """
    narrowed = await _list(context, str(connection.id), _grant(repository="octo/*"))
    assert narrowed.limited_by_grant is True

    whole = await _list(context, str(connection.id), EVERY_REPOSITORY)
    assert whole.limited_by_grant is False
    assert len(whole.repositories) == 4


async def test_a_long_listing_is_trimmed_to_fit_a_tool_result(
    context: ToolExecutionContext, connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway does not shorten an oversized result, it replaces the
    whole document with a preview marker -- which would throw the listing
    away and hand back a ``truncated`` that means something else. So the
    executor cuts to fit and reports that as its own truncation, and what
    it returns still survives the gateway's sanitizer intact.
    """
    from jhin_connectors.github import tools as github_tools
    from jhin_tools.sanitize import sanitize_payload

    # A budget small enough that four rows cannot fit.
    monkeypatch.setattr(github_tools, "MAX_DOCUMENT_BYTES", 400)
    monkeypatch.setattr(github_tools, "_RESULT_BYTES_HEADROOM", 100)

    output = await _list(context, str(connection.id), EVERY_REPOSITORY)

    assert output.truncated is True
    assert 0 < len(output.repositories) < 4
    sanitized = sanitize_payload(output.model_dump(mode="json"))
    assert "original_size_bytes" not in sanitized
    assert len(sanitized["repositories"]) == len(output.repositories)
