"""GitHub tool executors end-to-end against the in-process fake GitHub
server: real httpx, real credential decryption, both auth schemes."""

from collections.abc import Iterator

import pytest

from jhin_connectors.github.auth import InstallationTokenCache, resolve_access_token
from jhin_connectors.github.connector import GitHubConnector
from jhin_connectors.github.schemas import (
    BranchCreateInput,
    BranchCreateOutput,
    BranchListInput,
    BranchListOutput,
    CommentOutput,
    FileReadInput,
    FileReadOutput,
    IssueCommentInput,
    PullRequestCreateInput,
    PullRequestCreateOutput,
    RepositoryReadInput,
    RepositoryReadOutput,
)
from jhin_connectors.testing.fake_github import FakeGitHubServer
from jhin_db.models import Workspace
from jhin_tools.builtin import ToolExecutionContext

EXECUTORS = {definition.name: executor for definition, executor in GitHubConnector().tools()}


@pytest.fixture
def fake_github() -> Iterator[FakeGitHubServer]:
    with FakeGitHubServer() as server:
        yield server


@pytest.fixture
async def github_connection(  # type: ignore[no-untyped-def]
    workspace: Workspace, make_connection, fake_github: FakeGitHubServer
):
    return await make_connection(
        workspace,
        credentials={"token": "fake-github-pat"},
        config={"base_url": fake_github.base_url},
    )


async def test_repository_read_and_branch_list(
    context: ToolExecutionContext, github_connection, fake_github: FakeGitHubServer
) -> None:
    output = await EXECUTORS["github.repository.read"](
        context,
        RepositoryReadInput(connection_id=str(github_connection.id), repository="octo/alpha"),
    )
    assert isinstance(output, RepositoryReadOutput)
    assert output.full_name == "octo/alpha"
    assert output.default_branch == "main"

    branches = await EXECUTORS["github.branch.list"](
        context, BranchListInput(connection_id=str(github_connection.id), repository="octo/alpha")
    )
    assert isinstance(branches, BranchListOutput)
    assert [b.name for b in branches.branches] == ["main"]


async def test_file_read(context: ToolExecutionContext, github_connection) -> None:
    output = await EXECUTORS["github.file.read"](
        context,
        FileReadInput(
            connection_id=str(github_connection.id), repository="octo/alpha", path="README.md"
        ),
    )
    assert isinstance(output, FileReadOutput)
    assert "Seeded fake repository" in output.content
    assert not output.truncated


async def test_branch_create_then_pull_request_flow(
    context: ToolExecutionContext, github_connection, fake_github: FakeGitHubServer
) -> None:
    branch = await EXECUTORS["github.branch.create"](
        context,
        BranchCreateInput(
            connection_id=str(github_connection.id),
            repository="octo/alpha",
            branch="agent/fix-login",
        ),
    )
    assert isinstance(branch, BranchCreateOutput)
    assert branch.ref == "refs/heads/agent/fix-login"

    pull = await EXECUTORS["github.pull_request.create"](
        context,
        PullRequestCreateInput(
            connection_id=str(github_connection.id),
            repository="octo/alpha",
            title="Fix login",
            head="agent/fix-login",
            base="main",
            body="Automated fix.",
        ),
    )
    assert isinstance(pull, PullRequestCreateOutput)
    assert pull.number == 100
    assert pull.state == "open"

    comment = await EXECUTORS["github.pull_request.comment"](
        context,
        IssueCommentInput(
            connection_id=str(github_connection.id),
            repository="octo/alpha",
            number=pull.number,
            body="Ready for review.",
        ),
    )
    assert isinstance(comment, CommentOutput)

    state = fake_github.state.snapshot()
    repo = state["repos"]["octo/alpha"]
    assert "agent/fix-login" in repo["branches"]
    assert repo["pulls"]["100"]["title"] == "Fix login"
    assert repo["pulls"]["100"]["comments"][0]["body"] == "Ready for review."


async def test_error_message_is_safe_and_bounded(
    context: ToolExecutionContext, github_connection
) -> None:
    with pytest.raises(Exception) as excinfo:
        await EXECUTORS["github.repository.read"](
            context,
            RepositoryReadInput(connection_id=str(github_connection.id), repository="octo/missing"),
        )
    message = str(excinfo.value)
    assert "404" in message
    assert "fake-github-pat" not in message  # token never in errors


async def test_github_app_auth_mints_and_caches_installation_token(
    fake_github: FakeGitHubServer,
) -> None:
    # A structurally JWT-shaped credential is enough for the fake server.
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    credentials = {"app_id": "12345", "private_key": pem, "installation_id": "77"}

    cache = InstallationTokenCache()
    token1 = await resolve_access_token(
        "github_app", credentials, fake_github.base_url, cache=cache
    )
    token2 = await resolve_access_token(
        "github_app", credentials, fake_github.base_url, cache=cache
    )
    assert token1.startswith("ghs_fake_77_")
    assert token1 == token2  # cached, not re-minted
    assert fake_github.state.mint_count == 1
    assert pyjwt  # imported for realism of the credential shape


async def test_pat_verify_connection_against_fake(fake_github: FakeGitHubServer) -> None:
    from jhin_connectors.base import VerifyContext

    connector = GitHubConnector()
    good = await connector.verify_connection(
        VerifyContext(
            auth_type="pat",
            credentials={"token": "fake-github-pat"},
            config={"base_url": fake_github.base_url},
        )
    )
    assert good.ok
    assert good.details["login"] == "fake-user"

    bad = await connector.verify_connection(
        VerifyContext(
            auth_type="pat",
            credentials={"token": "wrong"},
            config={"base_url": fake_github.base_url},
        )
    )
    assert not bad.ok
    assert "401" in bad.message


async def test_app_verify_connection_against_fake(fake_github: FakeGitHubServer) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from jhin_connectors.base import VerifyContext

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    health = await GitHubConnector().verify_connection(
        VerifyContext(
            auth_type="github_app",
            credentials={"app_id": "12345", "private_key": pem, "installation_id": "9"},
            config={"base_url": fake_github.base_url},
        )
    )
    assert health.ok, health.message
    assert health.details["app"] == "jhin-fake-app"
