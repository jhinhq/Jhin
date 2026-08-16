"""Secret API contract tests (plan 32.5: secret never returned by GET).

The API's only secret response shape is ``SecretOut``. These tests prove by
construction that no plaintext (or ciphertext) can flow through it.
"""

import secrets as stdlib_secrets
from datetime import UTC, datetime
from uuid import uuid4

from jhin_api.secrets.schemas import SecretOut
from jhin_db.models import Secret
from jhin_secrets import MasterKey, SecretCrypto, mask_hint


def make_secret_row(plaintext: str) -> Secret:
    crypto = SecretCrypto(MasterKey(key=stdlib_secrets.token_bytes(32)))
    payload = crypto.encrypt(plaintext)
    now = datetime.now(UTC)
    return Secret(
        id=uuid4(),
        workspace_id=uuid4(),
        name="OpenAI API Key",
        type="api_key",
        ciphertext=payload.ciphertext,
        nonce=payload.nonce,
        wrapped_data_key=payload.wrapped_data_key,
        key_version=payload.key_version,
        secret_fingerprint=payload.fingerprint,
        masked_hint=mask_hint(plaintext),
        created_at=now,
        updated_at=now,
    )


def test_secret_out_has_no_material_fields() -> None:
    fields = set(SecretOut.model_fields)
    forbidden = {"value", "plaintext", "ciphertext", "nonce", "wrapped_data_key"}
    assert fields & forbidden == set()


def test_secret_response_never_contains_plaintext() -> None:
    plaintext = "sk-live-supersecret-7A2F"
    row = make_secret_row(plaintext)
    out = SecretOut.model_validate(row, from_attributes=True)
    serialized = out.model_dump_json()
    assert plaintext not in serialized
    # The hint shows only the last four characters.
    assert out.masked_hint == "\u2022\u2022\u2022\u20227A2F"
    assert "supersecret" not in serialized


def test_short_secrets_get_no_tail_in_hint() -> None:
    assert mask_hint("abc") == "\u2022\u2022\u2022\u2022"
    assert mask_hint("1234567") == "\u2022\u2022\u2022\u2022"
    assert mask_hint("12345678") == "\u2022\u2022\u2022\u20225678"
