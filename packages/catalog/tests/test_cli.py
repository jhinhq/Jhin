"""The scheduled entry point: exit codes and one line of output.

A cron job is read by a machine, so these tests pin the two things a
scheduler actually consumes — the exit code and, under ``--json``, exactly
one canonical object on stdout. The sync itself is stubbed: the network and
the loader have their own files.

Every test here is synchronous on purpose. :func:`main` owns its event loop
via ``asyncio.run``, and a test already inside one would deadlock it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from jhin_catalog_sync import cli as cli_module
from jhin_catalog_sync.cli import (
    EXIT_ERROR,
    EXIT_FETCH,
    EXIT_FORMAT,
    EXIT_INTEGRITY,
    EXIT_OK,
    main,
    reserved_slugs,
)
from jhin_catalog_sync.loader import SyncOutcome
from jhin_catalog_sync.types import (
    CatalogFetchError,
    CatalogFormatError,
    CatalogIntegrityError,
    CatalogSyncError,
)

DATABASE_URL = "postgresql+asyncpg://jhin:jhin@postgres:5432/jhin"
VERSION_ID = UUID("0192f4d2-0000-7000-8000-00000000abcd")

OUTCOME = SyncOutcome(
    version_id=VERSION_ID,
    release_tag="2026.08.28",
    data_sha256="d" * 64,
    changed=True,
    entry_count=1_240,
    mcp_count=1_100,
    skill_count=140,
    rejected_count=7,
    resumed=False,
)


def _stub(monkeypatch: pytest.MonkeyPatch, result: SyncOutcome | Exception) -> list[dict[str, Any]]:
    """Replace the sync with a recorded, instant one."""
    calls: list[dict[str, Any]] = []

    async def fake_sync_once(
        *, database_url: str, repo: str, tag: str | None = None
    ) -> SyncOutcome:
        calls.append({"database_url": database_url, "repo": repo, "tag": tag})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli_module, "sync_once", fake_sync_once)
    return calls


# --- exit codes ---


def test_a_successful_sync_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, OUTCOME)

    assert main(["--database-url", DATABASE_URL]) == EXIT_OK

    out = capsys.readouterr().out
    assert "2026.08.28" in out
    assert "1240 entries" in out


def test_a_release_already_active_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing to do is a success. A scheduler that treats it as a failure
    would page somebody every night the upstream did not publish."""
    unchanged = SyncOutcome(
        version_id=VERSION_ID,
        release_tag="2026.08.28",
        data_sha256="d" * 64,
        changed=False,
        entry_count=1_240,
        mcp_count=1_100,
        skill_count=140,
        rejected_count=0,
        resumed=False,
    )
    _stub(monkeypatch, unchanged)

    assert main(["--database-url", DATABASE_URL]) == EXIT_OK
    assert "already active" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (CatalogIntegrityError("the catalog data archive does not match its published digest"), 3),
        (CatalogFetchError("the catalog asset request failed"), 4),
        (CatalogFormatError("the catalog archive holds an unexpected member name"), 5),
        (CatalogSyncError("database error: IntegrityError"), 1),
        (RuntimeError("something nobody predicted"), 1),
    ],
    ids=["integrity", "fetch", "format", "sync", "unexpected"],
)
def test_each_failure_has_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    _stub(monkeypatch, error)

    assert main(["--database-url", DATABASE_URL]) == code

    captured = capsys.readouterr()
    assert captured.out == "", "a failed run prints nothing a parser could mistake for a result"
    assert "catalog sync failed" in captured.err


def test_the_exit_codes_are_the_documented_ones() -> None:
    assert (EXIT_OK, EXIT_ERROR, EXIT_INTEGRITY, EXIT_FETCH, EXIT_FORMAT) == (0, 1, 3, 4, 5)


def test_an_unexpected_exception_is_reported_as_a_type_not_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, RuntimeError("CANARY-prose-from-somewhere-else"))

    assert main(["--database-url", DATABASE_URL]) == EXIT_ERROR

    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "CANARY" not in err


def test_a_missing_database_url_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls = _stub(monkeypatch, OUTCOME)

    assert main([]) == EXIT_ERROR

    assert calls == []
    assert "DATABASE_URL is required" in capsys.readouterr().err


# --- --json ---


def test_json_prints_exactly_one_canonical_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, OUTCOME)

    assert main(["--database-url", DATABASE_URL, "--json"]) == EXIT_OK

    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line, so a wrapper can read it whole"
    payload = json.loads(out)
    assert payload == {
        "changed": True,
        "data_sha256": "d" * 64,
        "entry_count": 1_240,
        "mcp_count": 1_100,
        "rejected_count": 7,
        "release_tag": "2026.08.28",
        "resumed": False,
        "skill_count": 140,
        "version_id": str(VERSION_ID),
    }
    assert out.strip() == json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_json_says_nothing_on_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, CatalogFetchError("the catalog asset request failed"))

    assert main(["--database-url", DATABASE_URL, "--json"]) == EXIT_FETCH
    assert capsys.readouterr().out == ""


# --- arguments ---


def test_the_repository_defaults_to_the_configured_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CATALOG_SOURCE_REPO", raising=False)
    calls = _stub(monkeypatch, OUTCOME)

    main(["--database-url", DATABASE_URL])

    assert calls == [{"database_url": DATABASE_URL, "repo": "jhinhq/jhin-catalog", "tag": None}]
    capsys.readouterr()


def test_the_environment_can_point_the_sync_at_a_fork(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALOG_SOURCE_REPO", "acme/internal-catalog")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    calls = _stub(monkeypatch, OUTCOME)

    assert main([]) == EXIT_OK

    assert calls == [{"database_url": DATABASE_URL, "repo": "acme/internal-catalog", "tag": None}]
    capsys.readouterr()


def test_an_explicit_flag_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALOG_SOURCE_REPO", "acme/internal-catalog")
    calls = _stub(monkeypatch, OUTCOME)

    main(["--database-url", DATABASE_URL, "--repo", "jhinhq/jhin-catalog", "--tag", "2026.07.01"])

    assert calls == [
        {"database_url": DATABASE_URL, "repo": "jhinhq/jhin-catalog", "tag": "2026.07.01"}
    ]
    capsys.readouterr()


# --- reserved slugs ---


def test_the_curated_slugs_are_reserved_when_the_connectors_are_installed() -> None:
    slugs = reserved_slugs()

    assert slugs, "the built-in library must be reserved against synced rows"
    assert all(slug == slug.lower() for slug in slugs)


def test_reserving_slugs_never_fails_without_the_connector_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync package does not depend on ``jhin_connectors``; a deployment
    that ships the job alone still runs, and the API's read-time gate remains."""
    real_import = __import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("jhin_connectors"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked)

    assert reserved_slugs() == frozenset()


# --- wiring ---


def test_sync_once_is_the_only_thing_main_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the sequencing that makes integrity non-optional: nothing may
    reach the loader except through ``sync_once``."""

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("nothing may reach the loader except through sync_once")

    monkeypatch.setattr(cli_module, "load_archive", forbidden)
    monkeypatch.setattr(cli_module, "open_archive", forbidden)
    _stub(monkeypatch, OUTCOME)

    assert main(["--database-url", DATABASE_URL, "--json"]) == EXIT_OK
