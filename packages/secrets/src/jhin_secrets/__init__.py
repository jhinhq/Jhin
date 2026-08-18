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
from jhin_secrets.material import (
    MAX_SECRET_MATERIAL_BYTES,
    MAX_SECRET_MATERIAL_DEPTH,
    MAX_SECRET_MATERIAL_FRAGMENTS,
    MAX_SECRET_URL_QUERY_FIELDS,
    SecretMaterialError,
    decode_secret_mapping,
    decode_string_secret_map,
    register_secret_material,
)
from jhin_secrets.redaction import SecretRedactor, get_redactor
from jhin_secrets.store import SecretStore, mask_hint

__all__ = [
    "MAX_SECRET_MATERIAL_BYTES",
    "MAX_SECRET_MATERIAL_DEPTH",
    "MAX_SECRET_MATERIAL_FRAGMENTS",
    "MAX_SECRET_URL_QUERY_FIELDS",
    "EncryptedPayload",
    "MasterKey",
    "SecretCrypto",
    "SecretDecryptionError",
    "SecretMaterialError",
    "SecretRedactor",
    "SecretStore",
    "decode_secret_mapping",
    "decode_string_secret_map",
    "get_redactor",
    "load_master_key",
    "mask_hint",
    "register_secret_material",
]
