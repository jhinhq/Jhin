"""Outbound endpoint policy for HTTP providers and PostgreSQL targets."""

import pytest

from jhin_connectors.endpoints import (
    EndpointPolicyError,
    validate_http_origin,
    validate_postgres_target,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:9000",
        "https://user:pass@example.com",
    ],
)
def test_unapproved_http_target_is_rejected(url: str) -> None:
    with pytest.raises(EndpointPolicyError):
        validate_http_origin(url, official_origins=("https://api.vercel.com",))


def test_exact_operator_allowlisted_origin_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "JHIN_CONNECTOR_ALLOWED_HTTP_ORIGINS",
        "https://another.example,http://127.0.0.1:9000/",
    )

    assert (
        validate_http_origin(
            "http://127.0.0.1:9000/",
            official_origins=("https://api.vercel.com",),
        )
        == "http://127.0.0.1:9000"
    )


def test_official_http_origin_is_normalized_before_exact_comparison() -> None:
    assert (
        validate_http_origin(
            "https://API.VERCEL.com:443/",
            official_origins=("https://api.vercel.com",),
        )
        == "https://api.vercel.com"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.vercel.com/v1",
        "https://api.vercel.com?token=secret",
        "https://api.vercel.com/#fragment",
        "ftp://api.vercel.com",
    ],
)
def test_http_origin_rejects_non_origin_url_components(url: str) -> None:
    with pytest.raises(EndpointPolicyError):
        validate_http_origin(url, official_origins=("https://api.vercel.com",))


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:secret@db.abc123.supabase.co:5432/postgres?sslmode=disable",
        "postgresql://reader:secret@db.wrong.supabase.co:5432/postgres?sslmode=require",
    ],
)
def test_hosted_supabase_dsn_requires_tls_and_expected_host_shape(dsn: str) -> None:
    with pytest.raises(EndpointPolicyError):
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres:secret@db.abc123.supabase.co:5432/postgres?sslmode=require",
        (
            "postgresql://postgres.abc123:secret@aws-0-us-west-1.pooler.supabase.com:5432/"
            "postgres?sslmode=verify-full"
        ),
    ],
)
def test_valid_hosted_supabase_targets_return_the_original_dsn(dsn: str) -> None:
    assert validate_postgres_target(dsn, project_ref="abc123", app_database_url=None) == dsn


def test_pooler_username_must_end_with_project_ref() -> None:
    dsn = (
        "postgresql://postgres.wrong:secret@aws-0-us-west-1.pooler.supabase.com:5432/"
        "postgres?sslmode=require"
    )

    with pytest.raises(EndpointPolicyError):
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)


def test_official_transaction_pooler_port_is_rejected_for_session_execution() -> None:
    dsn = (
        "postgresql://postgres.abc123:secret@aws-0-us-west-1.pooler.supabase.com:6543/"
        "postgres?sslmode=require"
    )

    with pytest.raises(EndpointPolicyError):
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=require&host=127.0.0.1",
        "sslmode=require&hostaddr=127.0.0.1",
        "sslmode=require&port=5433",
        "sslmode=require&dbname=other",
        "sslmode=require&user=other",
        "sslmode=require&service=unsafe",
        "sslmode=require&application_name=jhin",
        "sslmode=require&options=-csearch_path%3Dpublic",
        "sslmode=require&role=postgres",
        "sslmode=require&search_path=public",
        "sslmode=require&passfile=/tmp/pass",
        "sslmode=require&sslcert=/tmp/cert",
        "sslmode=require&SSLKEY=/tmp/key",
        "sslmode=require&sslmode=disable",
        "sslmode=require&SSLMODE=verify-full",
    ],
)
def test_database_query_cannot_override_validated_connection_target(query: str) -> None:
    dsn = f"postgresql://reader:secret@db.abc123.supabase.co:5432/postgres?{query}"

    with pytest.raises(EndpointPolicyError):
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader@db.abc123.supabase.co:5432/postgres?sslmode=require",
        "postgresql://reader:@db.abc123.supabase.co:5432/postgres?sslmode=require",
    ],
)
def test_database_target_requires_an_explicit_nonempty_password(dsn: str) -> None:
    with pytest.raises(EndpointPolicyError) as exc_info:
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)

    assert "reader" not in str(exc_info.value)


def test_sslmode_query_key_is_case_insensitive_when_it_is_the_only_key() -> None:
    dsn = "postgresql://reader:secret@db.abc123.supabase.co:5432/postgres?SSLMODE=VERIFY-FULL"

    assert validate_postgres_target(dsn, project_ref="abc123", app_database_url=None) == dsn


def test_exact_allowlisted_database_host_bypasses_hosted_tls_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "fake-supabase-db:5432")
    dsn = "postgresql://reader:secret@fake-supabase-db:5432/fixture?sslmode=disable"

    assert validate_postgres_target(dsn, project_ref="abc123", app_database_url=None) == dsn


def test_exact_allowlisted_dev_host_may_use_transaction_pooler_port_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "fake-supabase-db:6543")
    dsn = "postgresql://reader:secret@fake-supabase-db:6543/fixture?sslmode=disable"

    assert validate_postgres_target(dsn, project_ref="abc123", app_database_url=None) == dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:secret@db.abc123.supabase.co:5432/postgres?sslmode=disable",
        (
            "postgresql://postgres.abc123:secret@"
            "aws-0-us-west-1.pooler.supabase.com:5432/postgres?sslmode=disable"
        ),
    ],
)
def test_allowlist_cannot_downgrade_official_supabase_tls(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
) -> None:
    monkeypatch.setenv(
        "JHIN_CONNECTOR_ALLOWED_DB_HOSTS",
        "db.abc123.supabase.co:5432,aws-0-us-west-1.pooler.supabase.com:5432",
    )

    with pytest.raises(EndpointPolicyError):
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)


def test_jhin_database_target_is_rejected() -> None:
    target_password = "target-password"
    app_password = "application-password"
    target = f"postgresql://target:{target_password}@postgres.internal:5432/jhin?sslmode=require"
    application = f"postgresql+asyncpg://jhin:{app_password}@postgres.internal/jhin?sslmode=require"

    with pytest.raises(EndpointPolicyError) as exc_info:
        validate_postgres_target(
            target,
            project_ref="abc123",
            app_database_url=application,
        )

    rendered = str(exc_info.value)
    assert target_password not in rendered
    assert app_password not in rendered


def test_percent_encoded_database_credentials_survive_successful_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHIN_CONNECTOR_ALLOWED_DB_HOSTS", "database.internal:5432")
    dsn = "postgresql+asyncpg://reader%40tenant:p%40ss%2Fword@database.internal:5432/fixture"

    assert validate_postgres_target(dsn, project_ref="abc123", app_database_url=None) == dsn


def test_rejected_database_error_never_contains_username_or_password() -> None:
    username = "sensitive-user"
    password = "sensitive-password"
    dsn = f"postgresql://{username}:{password}@127.0.0.1:5432/fixture"

    with pytest.raises(EndpointPolicyError) as exc_info:
        validate_postgres_target(dsn, project_ref="abc123", app_database_url=None)

    rendered = str(exc_info.value)
    assert username not in rendered
    assert password not in rendered
