"""Envelope encryption unit tests (plan 32.1: secret encryption/decryption)."""

import base64
import json
import logging
import secrets as stdlib_secrets
from collections.abc import Iterator

import pytest
import structlog

from jhin_observability import configure_json_logging
from jhin_secrets.crypto import (
    EncryptedPayload,
    MasterKey,
    MasterKeyError,
    SecretCrypto,
    SecretDecryptionError,
    decode_master_key_material,
    generate_master_key_material,
    load_master_key,
)


def make_crypto() -> SecretCrypto:
    return SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))


def test_encrypt_decrypt_roundtrip() -> None:
    crypto = make_crypto()
    payload = crypto.encrypt("sk-super-secret-value-7A2F")
    assert crypto.decrypt(payload) == "sk-super-secret-value-7A2F"


def test_ciphertext_does_not_contain_plaintext() -> None:
    crypto = make_crypto()
    payload = crypto.encrypt("sk-super-secret-value-7A2F")
    assert b"sk-super-secret-value-7A2F" not in payload.ciphertext
    assert b"sk-super-secret-value-7A2F" not in payload.wrapped_data_key


def test_each_secret_gets_unique_dek_and_nonce() -> None:
    crypto = make_crypto()
    first = crypto.encrypt("same-plaintext")
    second = crypto.encrypt("same-plaintext")
    assert first.nonce != second.nonce
    assert first.wrapped_data_key != second.wrapped_data_key
    assert first.ciphertext != second.ciphertext
    # But the keyed fingerprint is deterministic for duplicate detection.
    assert first.fingerprint == second.fingerprint


def test_wrong_master_key_fails_decryption() -> None:
    payload = make_crypto().encrypt("secret-value")
    other = make_crypto()
    with pytest.raises(SecretDecryptionError):
        other.decrypt(payload)


def test_tampered_ciphertext_fails() -> None:
    crypto = make_crypto()
    payload = crypto.encrypt("secret-value")
    tampered = EncryptedPayload(
        ciphertext=bytes([payload.ciphertext[0] ^ 0xFF]) + payload.ciphertext[1:],
        nonce=payload.nonce,
        wrapped_data_key=payload.wrapped_data_key,
        key_version=payload.key_version,
        fingerprint=payload.fingerprint,
    )
    with pytest.raises(SecretDecryptionError):
        crypto.decrypt(tampered)


def test_key_version_mismatch_is_rejected() -> None:
    crypto = make_crypto()
    payload = crypto.encrypt("secret-value")
    future = EncryptedPayload(
        ciphertext=payload.ciphertext,
        nonce=payload.nonce,
        wrapped_data_key=payload.wrapped_data_key,
        key_version=payload.key_version + 1,
        fingerprint=payload.fingerprint,
    )
    with pytest.raises(SecretDecryptionError, match="key version"):
        crypto.decrypt(future)


def test_fingerprints_differ_across_master_keys() -> None:
    # HMAC-keyed: a database dump alone cannot brute-force fingerprints.
    a, b = make_crypto(), make_crypto()
    assert a.fingerprint("hello") != b.fingerprint("hello")


def test_load_master_key_from_file(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "key"
    path.write_text(generate_master_key_material() + "\n")
    key = load_master_key({"MASTER_KEY_FILE": str(path)})
    assert len(key.key) == 32


@pytest.fixture
def restore_logging_globals() -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_named = {
        candidate: (list(candidate.handlers), candidate.level, candidate.propagate)
        for candidate in logging.root.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    }
    original_structlog_config = dict(structlog.get_config())
    try:
        yield
    finally:
        installed_handlers = [
            handler for handler in root.handlers if handler not in original_handlers
        ]
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        for named, (handlers, level, propagate) in original_named.items():
            named.handlers[:] = handlers
            named.setLevel(level)
            named.propagate = propagate
        for handler in installed_handlers:
            handler.close()
        structlog.configure(**original_structlog_config)


def test_load_master_key_env_fallback_emits_safe_json_v1(
    capsys: pytest.CaptureFixture[str], restore_logging_globals: None
) -> None:
    material = base64.b64encode(stdlib_secrets.token_bytes(32)).decode()
    configure_json_logging(service="secrets-test", environment="test", level="WARNING")

    key = load_master_key({"MASTER_KEY": material})

    assert len(key.key) == 32
    rendered = capsys.readouterr().out
    record = json.loads(rendered)
    assert record == {
        "environment": "test",
        "event": "security.master_key_env_source",
        "level": "warning",
        "logger": "jhin_secrets.crypto",
        "schema_version": 1,
        "service": "secrets-test",
        "timestamp": record["timestamp"],
    }
    assert "MASTER_KEY" not in rendered
    assert material not in rendered
    assert "local development" not in rendered


def test_missing_master_key_raises() -> None:
    with pytest.raises(MasterKeyError, match="no master key configured"):
        load_master_key({})


def test_decode_master_key_accepts_hex_and_base64() -> None:
    raw = stdlib_secrets.token_bytes(32)
    assert decode_master_key_material(raw.hex()) == raw
    assert decode_master_key_material(base64.b64encode(raw).decode()) == raw
