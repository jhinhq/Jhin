"""Sanitizer: secret redaction + size caps (plan 21.8-9, invariant 48.9)."""

import json

from jhin_secrets.redaction import SecretRedactor
from jhin_tools.sanitize import TRUNCATION_MARKER, sanitize_payload


def test_known_secrets_are_redacted_recursively() -> None:
    redactor = SecretRedactor()
    redactor.register("sk-super-secret-key")
    payload = {
        "text": "the key is sk-super-secret-key",
        "nested": {"list": ["sk-super-secret-key", 42, None, True]},
    }
    clean = sanitize_payload(payload, redactor=redactor)
    serialized = json.dumps(clean)
    assert "sk-super-secret-key" not in serialized
    assert "[REDACTED]" in clean["text"]
    assert clean["nested"]["list"][0] == "[REDACTED]"
    assert clean["nested"]["list"][1:] == [42, None, True]


def test_long_strings_are_truncated_with_marker() -> None:
    clean = sanitize_payload({"blob": "x" * 20_000}, max_string_chars=1_000)
    assert len(clean["blob"]) == 1_000
    assert clean["blob"].endswith(TRUNCATION_MARKER)


def test_oversized_document_is_replaced_by_marker_object() -> None:
    payload = {f"key_{i}": "v" * 500 for i in range(100)}
    clean = sanitize_payload(payload, max_document_bytes=4_096)
    assert clean["truncated"] is True
    assert clean["original_size_bytes"] > 4_096
    assert clean["preview"].endswith(TRUNCATION_MARKER)
    assert len(json.dumps(clean).encode()) < 4_096


def test_small_clean_payload_passes_through() -> None:
    payload = {"a": 1, "b": ["x", {"c": False}]}
    assert sanitize_payload(payload, redactor=SecretRedactor()) == payload


def test_non_json_types_become_redacted_strings() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-object"

    clean = sanitize_payload({"obj": Weird()}, redactor=SecretRedactor())
    assert clean["obj"] == "weird-object"
