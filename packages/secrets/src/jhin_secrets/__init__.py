"""Envelope-encrypted secret storage for Jhin (plan section 13).

Layout mirrors the plan's package map: ``crypto`` (envelope encryption),
``store`` (database-backed secret store), ``redaction`` (log scrubbing).
"""

from jhin_secrets.crypto import (
    EncryptedPayload,
    MasterKey,
    SecretCrypto,
    SecretDecryptionError,
    load_master_key,
)
from jhin_secrets.redaction import SecretRedactor, get_redactor
from jhin_secrets.store import SecretStore, mask_hint

__all__ = [
    "EncryptedPayload",
    "MasterKey",
    "SecretCrypto",
    "SecretDecryptionError",
    "SecretRedactor",
    "SecretStore",
    "get_redactor",
    "load_master_key",
    "mask_hint",
]
