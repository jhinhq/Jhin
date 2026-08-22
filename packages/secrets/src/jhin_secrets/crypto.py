"""Envelope encryption for user secrets (plan 13.2).

Every secret gets its own random data-encryption key (DEK). The plaintext is
encrypted with AES-256-GCM under the DEK; the DEK is then wrapped (also
AES-256-GCM) under the master key. Postgres stores only ciphertext, nonces,
the wrapped DEK, and the master-key version — never the master key itself.

The master key is loaded from the file named by ``MASTER_KEY_FILE``
(``/run/secrets/jhin_master_key`` in the compose stack). A bare ``MASTER_KEY``
environment variable is accepted as a dev-only fallback and warned about
loudly, because environment variables leak into inspect output and crash
reports far more easily than mounted files.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as stdlib_secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jhin_observability import get_logger

logger = get_logger(__name__)

MASTER_KEY_FILE_ENV = "MASTER_KEY_FILE"
MASTER_KEY_ENV = "MASTER_KEY"
CURRENT_KEY_VERSION = 1

_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # standard GCM nonce size


class SecretDecryptionError(Exception):
    """Raised when a ciphertext cannot be decrypted (wrong key, corrupt row)."""


class MasterKeyError(Exception):
    """Raised when no usable master key can be loaded."""


@dataclass(frozen=True)
class MasterKey:
    key: bytes
    version: int = CURRENT_KEY_VERSION

    def __post_init__(self) -> None:
        if len(self.key) != _KEY_BYTES:
            raise MasterKeyError(f"master key must be {_KEY_BYTES} bytes, got {len(self.key)}")


@dataclass(frozen=True)
class EncryptedPayload:
    """Everything persisted for one encrypted value (plan 6.10 fields)."""

    ciphertext: bytes
    nonce: bytes
    wrapped_data_key: bytes
    key_version: int
    fingerprint: str


def generate_master_key_material() -> str:
    """Random base64 master-key material suitable for the key file."""
    return base64.b64encode(stdlib_secrets.token_bytes(_KEY_BYTES)).decode()


def decode_master_key_material(material: str) -> bytes:
    """Accept base64 (44 chars) or hex (64 chars) encoded 32-byte keys."""
    text = material.strip()
    try:
        if len(text) == _KEY_BYTES * 2:
            return bytes.fromhex(text)
        return base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise MasterKeyError(f"master key material is not valid base64/hex: {exc}") from exc


def load_master_key(environ: dict[str, str] | None = None) -> MasterKey:
    """Load the master key from MASTER_KEY_FILE, falling back to MASTER_KEY."""
    env = environ if environ is not None else dict(os.environ)
    key_file = env.get(MASTER_KEY_FILE_ENV)
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as handle:
                material = handle.read()
        except OSError as exc:
            raise MasterKeyError(f"cannot read {MASTER_KEY_FILE_ENV}={key_file}: {exc}") from exc
        return MasterKey(key=decode_master_key_material(material))

    inline = env.get(MASTER_KEY_ENV)
    if inline:
        logger.warning("security.master_key_env_source")
        return MasterKey(key=decode_master_key_material(inline))

    raise MasterKeyError(
        f"no master key configured: set {MASTER_KEY_FILE_ENV} to a key file path "
        f"(generate one with scripts/generate_master_key.py or `make master-key`)"
    )


class SecretCrypto:
    """Stateless envelope encrypt/decrypt bound to one master key."""

    def __init__(self, master_key: MasterKey) -> None:
        self._master = master_key

    @property
    def key_version(self) -> int:
        return self._master.version

    def fingerprint(self, plaintext: str) -> str:
        """Deterministic keyed fingerprint of a plaintext (plan 6.10).

        HMAC under the master key rather than a bare hash, so a database dump
        cannot be used to brute-force low-entropy secrets offline.
        """
        return hmac.new(self._master.key, plaintext.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, plaintext: str) -> EncryptedPayload:
        dek = stdlib_secrets.token_bytes(_KEY_BYTES)
        nonce = stdlib_secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode(), None)

        wrap_nonce = stdlib_secrets.token_bytes(_NONCE_BYTES)
        wrapped = AESGCM(self._master.key).encrypt(wrap_nonce, dek, None)
        return EncryptedPayload(
            ciphertext=ciphertext,
            nonce=nonce,
            # Wrap nonce travels with the wrapped DEK in a single column.
            wrapped_data_key=wrap_nonce + wrapped,
            key_version=self._master.version,
            fingerprint=self.fingerprint(plaintext),
        )

    def decrypt(self, payload: EncryptedPayload) -> str:
        if payload.key_version != self._master.version:
            raise SecretDecryptionError(
                f"secret was wrapped with key version {payload.key_version}, "
                f"but this process holds version {self._master.version}"
            )
        wrap_nonce = payload.wrapped_data_key[:_NONCE_BYTES]
        wrapped = payload.wrapped_data_key[_NONCE_BYTES:]
        try:
            dek = AESGCM(self._master.key).decrypt(wrap_nonce, wrapped, None)
            plaintext = AESGCM(dek).decrypt(payload.nonce, payload.ciphertext, None)
        except InvalidTag as exc:
            raise SecretDecryptionError(
                "decryption failed: wrong master key or corrupt data"
            ) from exc
        return plaintext.decode()
