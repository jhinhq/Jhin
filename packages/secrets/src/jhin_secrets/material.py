"""Strict decoding and bounded in-memory registration of secret material."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from jhin_secrets.redaction import get_redactor

MAX_SECRET_MATERIAL_BYTES = 256 * 1024
MAX_SECRET_MATERIAL_DEPTH = 16
MAX_SECRET_MATERIAL_FRAGMENTS = 256
MAX_SECRET_URL_QUERY_FIELDS = 64
_CREDENTIAL_URL_SCHEMES = frozenset(
    {
        "amqp",
        "amqps",
        "http",
        "https",
        "mariadb",
        "mongodb",
        "mongodb+srv",
        "mysql",
        "postgres",
        "postgresql",
        "redis",
        "rediss",
    }
)


class SecretMaterialError(ValueError):
    """Decrypted material does not match its expected safe shape or bounds."""


def _ensure_plaintext_size(plaintext: str) -> None:
    if len(plaintext.encode()) > MAX_SECRET_MATERIAL_BYTES:
        raise SecretMaterialError("secret material exceeds the size limit")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SecretMaterialError("secret material contains a duplicate key")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, object_pairs_hook=_unique_object)


def decode_string_secret_map(plaintext: str) -> dict[str, str]:
    """Decode a bounded JSON object with exclusively string keys and values.

    Error messages intentionally describe only shape, never decrypted data.
    """
    _ensure_plaintext_size(plaintext)
    try:
        decoded = _strict_json_loads(plaintext)
    except json.JSONDecodeError:
        raise SecretMaterialError("secret material is not valid JSON") from None
    except RecursionError:
        raise SecretMaterialError("secret material exceeds the depth limit") from None
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise SecretMaterialError("secret material must be a string-to-string object")
    return decoded


def register_secret_material(plaintext: str) -> None:
    """Register a bounded secret blob and every independently leakable piece.

    JSON string leaves, nested JSON strings, URL username/password fields,
    and every non-empty URL query value are collected first. Nothing is
    registered unless the whole traversal stays within its explicit bounds.
    The redactor remains memory-only; no extracted fragment is persisted.
    """
    _ensure_plaintext_size(plaintext)
    fragments: set[str] = set()
    visited_strings: set[str] = set()

    def collect(fragment: str) -> None:
        if not fragment or fragment in fragments:
            return
        if len(fragments) >= MAX_SECRET_MATERIAL_FRAGMENTS:
            raise SecretMaterialError("secret material exceeds the fragment limit")
        fragments.add(fragment)

    def collect_url(value: str) -> None:
        try:
            parsed = urlsplit(value)
        except ValueError:
            if "://" in value:
                raise SecretMaterialError(
                    "secret material contains an invalid credential URL"
                ) from None
            return
        if parsed.scheme.lower() not in _CREDENTIAL_URL_SCHEMES:
            return
        if parsed.netloc and parsed.username:
            collect(unquote(parsed.username))
        if parsed.netloc and parsed.password:
            collect(unquote(parsed.password))
        try:
            query_fields = parse_qsl(
                parsed.query,
                keep_blank_values=False,
                max_num_fields=MAX_SECRET_URL_QUERY_FIELDS,
            )
        except ValueError:
            raise SecretMaterialError("secret material exceeds the URL query-field limit") from None
        for _key, query_value in query_fields:
            collect(query_value)

    def visit(value: Any, depth: int) -> None:
        if depth > MAX_SECRET_MATERIAL_DEPTH:
            raise SecretMaterialError("secret material exceeds the depth limit")
        if isinstance(value, str):
            collect(value)
            if value in visited_strings:
                return
            visited_strings.add(value)
            collect_url(value)
            if not value.lstrip().startswith(("{", "[", '"')):
                return
            try:
                decoded = _strict_json_loads(value)
            except json.JSONDecodeError:
                return
            except RecursionError:
                raise SecretMaterialError("secret material exceeds the depth limit") from None
            if decoded != value:
                visit(decoded, depth + 1)
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth + 1)

    visit(plaintext, 0)
    redactor = get_redactor()
    for fragment in fragments:
        redactor.register(fragment)
