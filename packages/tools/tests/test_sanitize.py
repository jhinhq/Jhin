"""Sanitizer: secret redaction + size caps (plan 21.8-9, invariant 48.9)."""

import json

from jhin_secrets.redaction import SecretRedactor
from jhin_tools.sanitize import TRUNCATION_MARKER, sanitize_payload, strict_json_loads


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


def test_secret_bearing_mapping_keys_are_redacted_before_the_key_cap() -> None:
    redactor = SecretRedactor()
    secret = "provider-key-secret-abcdef"
    redactor.register(secret)
    raw_key = f"{'k' * 1_970}{secret}:{'x' * 3_000}"

    clean = sanitize_payload(
        {"nested": {raw_key: "safe-value"}},
        redactor=redactor,
        max_string_chars=2_000,
    )

    [safe_key] = clean["nested"]
    assert secret not in safe_key
    assert secret[:18] not in safe_key
    assert "[REDACTED]" in safe_key
    assert len(safe_key) == 2_000
    assert safe_key.endswith(TRUNCATION_MARKER)


def test_redacted_mapping_key_collisions_are_deterministic_and_lossless() -> None:
    redactor = SecretRedactor()
    redactor.register("provider-secret-one")
    redactor.register("provider-secret-two")
    payload = {
        "nested": {
            "label-provider-secret-one": "first",
            "label-provider-secret-two": "second",
            "label-[REDACTED]#2": "third",
        }
    }

    first = sanitize_payload(payload, redactor=redactor)
    second = sanitize_payload(payload, redactor=redactor)

    assert first == second
    nested = first["nested"]
    assert len(nested) == 3
    assert set(nested.values()) == {"first", "second", "third"}
    assert all("provider-secret" not in key for key in nested)


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


def test_strict_json_decoder_rejects_non_finite_numbers_and_duplicate_keys() -> None:
    for document in (
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e999}',
        '{"value":-1e999}',
    ):
        try:
            strict_json_loads(document)
        except ValueError as error:
            assert "JSON" in str(error)
        else:
            raise AssertionError(f"strict decoder accepted {document}")

    try:
        strict_json_loads('{"value":1,"value":2}')
    except ValueError as error:
        assert "duplicate JSON object key" in str(error)
    else:
        raise AssertionError("strict decoder accepted a duplicate object key")
