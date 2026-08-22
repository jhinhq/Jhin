"""Strict request and display-safe response models for Supabase tools."""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

PROJECT_REF_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
FUNCTION_SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9_-]{0,98}[a-z0-9])?$"
MAX_FUNCTION_FILE_BYTES = 6_144
MAX_FUNCTION_SOURCE_BYTES = 24_576
MAX_DATABASE_SQL_BYTES = 7_000
MAX_DATABASE_PARAM_BYTES = 8_192
MAX_DATABASE_PARAMS_BYTES = 16_000
DATABASE_SCHEMA_PATTERN = r"^[a-z_][a-z0-9_$]{0,62}$"

LogSource = Literal[
    "edge_logs",
    "postgres_logs",
    "auth_logs",
    "storage_logs",
    "realtime_logs",
    "function_edge_logs",
    "function_logs",
]
StrictFiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
DatabaseScalar = None | StrictBool | StrictInt | StrictFiniteFloat | StrictStr


class SupabaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1, max_length=100)


class SupabaseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectScopedInput(SupabaseInput):
    project_ref: str = Field(min_length=1, max_length=63, pattern=PROJECT_REF_PATTERN)


class ProjectReadInput(ProjectScopedInput):
    pass


class ProjectReadOutput(SupabaseOutput):
    project_id: str = Field(max_length=200)
    project_ref: str = Field(max_length=63)
    organization_id: str = Field(default="", max_length=200)
    organization_slug: str = Field(default="", max_length=200)
    name: str = Field(max_length=256)
    region: str = Field(default="", max_length=100)
    created_at: str = Field(default="", max_length=64)
    status: str = Field(default="", max_length=50)


class LogsReadInput(ProjectScopedInput):
    source: LogSource
    start: datetime
    end: datetime
    limit: int = Field(default=100, ge=1, le=200)
    text_filter: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("start", "end")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("log timestamps require a timezone")
        return value

    @field_validator("text_filter")
    @classmethod
    def _require_utf8_text_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("log text filter must be valid UTF-8") from None
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.end < self.start:
            raise ValueError("log end must not be earlier than start")
        if self.end - self.start > timedelta(hours=24):
            raise ValueError("log window cannot exceed 24 hours")
        return self


class LogRecord(SupabaseOutput):
    timestamp: str = Field(max_length=64)
    source: LogSource
    event_message: str = Field(default="", max_length=4_000)
    path: str = Field(default="", max_length=2_048)
    status_code: int = Field(default=0, ge=0, le=999)
    method: str = Field(default="", max_length=32)


class LogsReadOutput(SupabaseOutput):
    logs: list[LogRecord] = Field(max_length=200)
    truncated: bool = False


class FunctionListInput(ProjectScopedInput):
    limit: int = Field(default=100, ge=1, le=200)


class FunctionInfo(SupabaseOutput):
    project_ref: str = Field(max_length=63)
    function_id: str = Field(max_length=200)
    slug: str = Field(max_length=100)
    name: str = Field(max_length=256)
    status: str = Field(default="", max_length=50)
    version: int = Field(ge=0, le=2**63 - 1)
    created_at: int = Field(ge=0, le=2**63 - 1)
    updated_at: int = Field(ge=0, le=2**63 - 1)
    verify_jwt: bool
    entrypoint_path: str = Field(default="", max_length=256)


class FunctionListOutput(SupabaseOutput):
    functions: list[FunctionInfo] = Field(max_length=200)
    truncated: bool = False


def validate_source_path(value: str) -> str:
    if (
        not value
        or len(value) > 256
        or value.startswith("/")
        or "\\" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("function source path is invalid")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("function source path is invalid")
    return value


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=256)
    content: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_source_path(value)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_FUNCTION_FILE_BYTES:
            raise ValueError("function source file is too large")
        return value


class FunctionDeployInput(ProjectScopedInput):
    function_slug: str = Field(
        min_length=1,
        max_length=100,
        pattern=FUNCTION_SLUG_PATTERN,
    )
    entrypoint_path: str = Field(min_length=1, max_length=256)
    verify_jwt: bool
    files: list[SourceFile] = Field(min_length=1, max_length=8)

    @field_validator("entrypoint_path")
    @classmethod
    def _validate_entrypoint_path(cls, value: str) -> str:
        return validate_source_path(value)

    @model_validator(mode="after")
    def _validate_source_bundle(self) -> Self:
        paths = [source.path for source in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("function source paths must be unique")
        if self.entrypoint_path not in set(paths):
            raise ValueError("entrypoint_path must name a supplied file")
        serialized = json.dumps(
            [source.model_dump() for source in self.files],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > MAX_FUNCTION_SOURCE_BYTES:
            raise ValueError("function source bundle is too large")
        return self


class FunctionDeleteInput(ProjectScopedInput):
    function_slug: str = Field(
        min_length=1,
        max_length=100,
        pattern=FUNCTION_SLUG_PATTERN,
    )


class FunctionDeleteOutput(SupabaseOutput):
    project_ref: str = Field(max_length=63)
    function_slug: str = Field(max_length=100)
    deleted: bool


class DatabaseInput(ProjectScopedInput):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
    )

    requested_schema: str = Field(
        alias="schema",
        serialization_alias="schema",
        min_length=1,
        max_length=63,
        pattern=DATABASE_SCHEMA_PATTERN,
    )
    sql: str = Field(min_length=1, max_length=MAX_DATABASE_SQL_BYTES)
    params: list[DatabaseScalar] = Field(default_factory=list, max_length=50)

    @field_validator("sql")
    @classmethod
    def _validate_sql_bytes(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("database SQL must be valid UTF-8") from None
        if len(encoded) > MAX_DATABASE_SQL_BYTES or any(
            unicodedata.category(character).startswith("C") and character not in {"\t", "\n", "\r"}
            for character in value
        ):
            raise ValueError("database SQL is invalid")
        return value

    @field_validator("params")
    @classmethod
    def _validate_params(cls, values: list[DatabaseScalar]) -> list[DatabaseScalar]:
        for value in values:
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, int):
                if not -(2**63) <= value <= 2**63 - 1:
                    raise ValueError("database integer parameter is out of range")
                continue
            if isinstance(value, float):
                continue
            if isinstance(value, str):
                try:
                    encoded = value.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    raise ValueError("database string parameter must be valid UTF-8") from None
                if len(encoded) > MAX_DATABASE_PARAM_BYTES:
                    raise ValueError("database string parameter is too large")
                continue
            raise ValueError("database parameter type is not allowed")
        try:
            serialized = json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ValueError("database parameters are invalid") from None
        if len(serialized) > MAX_DATABASE_PARAMS_BYTES:
            raise ValueError("database parameters are too large")
        return values


class DatabaseReadInput(DatabaseInput):
    pass


class DatabaseMutationInput(DatabaseInput):
    pass


DatabaseCell = Annotated[str, Field(max_length=8_192)] | None


class DatabaseReadOutput(SupabaseOutput):
    columns: list[str] = Field(min_length=1, max_length=64)
    rows: list[list[DatabaseCell]] = Field(max_length=1_000)
    row_count: int = Field(ge=0, le=1_000)
    truncated: bool = False

    @field_validator("columns")
    @classmethod
    def _validate_columns(cls, columns: list[str]) -> list[str]:
        for column in columns:
            try:
                encoded = column.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise ValueError("database column name must be valid UTF-8") from None
            if (
                not column
                or len(encoded) > 256
                or any(unicodedata.category(character).startswith("C") for character in column)
            ):
                raise ValueError("database column name is invalid")
        return columns

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.row_count != len(self.rows) or any(
            len(row) != len(self.columns) for row in self.rows
        ):
            raise ValueError("database result shape is invalid")
        return self


class DatabaseMutationOutput(SupabaseOutput):
    affected_rows: int = Field(ge=0, le=1_000)


__all__ = [
    "DATABASE_SCHEMA_PATTERN",
    "FUNCTION_SLUG_PATTERN",
    "MAX_DATABASE_PARAMS_BYTES",
    "MAX_DATABASE_PARAM_BYTES",
    "MAX_DATABASE_SQL_BYTES",
    "MAX_FUNCTION_FILE_BYTES",
    "MAX_FUNCTION_SOURCE_BYTES",
    "PROJECT_REF_PATTERN",
    "DatabaseMutationInput",
    "DatabaseMutationOutput",
    "DatabaseReadInput",
    "DatabaseReadOutput",
    "FunctionDeleteInput",
    "FunctionDeleteOutput",
    "FunctionDeployInput",
    "FunctionInfo",
    "FunctionListInput",
    "FunctionListOutput",
    "LogRecord",
    "LogSource",
    "LogsReadInput",
    "LogsReadOutput",
    "ProjectReadInput",
    "ProjectReadOutput",
    "SourceFile",
    "validate_source_path",
]
