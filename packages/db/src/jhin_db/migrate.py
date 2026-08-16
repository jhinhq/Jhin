"""Programmatic Alembic runner.

The Alembic environment ships inside the ``jhin_db`` package so migrations can
run from any installed context (containers, CI, dev machines) without relying
on a repository-relative ``alembic.ini``.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

_SCRIPT_LOCATION = Path(__file__).resolve().parent / "alembic"


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL environment variable is required")
    upgrade_to_head(database_url)
    print("migrations applied: head")


if __name__ == "__main__":
    main()
