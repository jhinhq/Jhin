"""Commit the binary-read regression shape to real PostgreSQL JSONB."""

import os

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

from jhin_secrets.redaction import SecretRedactor
from jhin_tools.sanitize import sanitize_payload

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="isolated TEST_DATABASE_URL required")


async def test_binary_tool_result_commits_and_replays_as_postgres_jsonb():
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("CREATE TEMP TABLE jhin_safe_tool_result (payload jsonb)")
            )
            value = sanitize_payload(
                {
                    "content": "PK\x03\x04\x00document\ud800",
                    "nested": {"null\x00key": ["before\x00after"]},
                },
                redactor=SecretRedactor(),
            )
            await connection.execute(
                text("INSERT INTO jhin_safe_tool_result VALUES (:value)").bindparams(
                    bindparam("value", type_=JSONB)
                ),
                {"value": value},
            )
            await connection.commit()
            persisted = await connection.scalar(text("SELECT payload FROM jhin_safe_tool_result"))
            assert persisted == value
            assert persisted["content"] == "PK\x03\x04\ufffddocument\ufffd"
    finally:
        await engine.dispose()
