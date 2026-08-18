"""Database-backed secret store (plan 13.4, 13.5).

Write path: plaintext in, ciphertext out — plaintext is never persisted or
returned. Read path (``reveal``) exists for credential *use* only (model
calls, connector auth inside workers); it registers the plaintext with the
process redactor and stamps ``last_used_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jhin_db.models import Secret
from jhin_domain import SecretType
from jhin_secrets.crypto import EncryptedPayload, SecretCrypto
from jhin_secrets.material import register_secret_material

MASK_CHAR = "\u2022"  # •


def mask_hint(plaintext: str) -> str:
    """Display hint: bullet padding plus the last four characters (plan 13.4)."""
    tail = plaintext[-4:] if len(plaintext) >= 8 else ""
    return MASK_CHAR * 4 + tail


class SecretNotFoundError(LookupError):
    pass


class SecretStore:
    """Workspace-scoped encrypted secret operations over one AsyncSession."""

    def __init__(self, session: AsyncSession, crypto: SecretCrypto) -> None:
        self._session = session
        self._crypto = crypto

    async def create(
        self,
        *,
        workspace_id: UUID,
        name: str,
        plaintext: str,
        secret_type: SecretType = SecretType.API_KEY,
        created_by_user_id: UUID | None = None,
    ) -> Secret:
        register_secret_material(plaintext)
        payload = self._crypto.encrypt(plaintext)
        secret = Secret(
            workspace_id=workspace_id,
            name=name,
            type=secret_type.value,
            ciphertext=payload.ciphertext,
            nonce=payload.nonce,
            wrapped_data_key=payload.wrapped_data_key,
            key_version=payload.key_version,
            secret_fingerprint=payload.fingerprint,
            masked_hint=mask_hint(plaintext),
            created_by_user_id=created_by_user_id,
        )
        self._session.add(secret)
        await self._session.flush()
        return secret

    async def get(self, workspace_id: UUID, secret_id: UUID) -> Secret:
        secret = await self._session.scalar(
            select(Secret).where(Secret.id == secret_id, Secret.workspace_id == workspace_id)
        )
        if secret is None:
            raise SecretNotFoundError(f"secret {secret_id} not found in workspace")
        return secret

    async def list(self, workspace_id: UUID) -> list[Secret]:
        rows = await self._session.scalars(
            select(Secret).where(Secret.workspace_id == workspace_id).order_by(Secret.created_at)
        )
        return list(rows)

    async def reveal(self, workspace_id: UUID, secret_id: UUID) -> str:
        """Decrypt for in-process use. Never expose the result over an API."""
        secret = await self.get(workspace_id, secret_id)
        plaintext = self._crypto.decrypt(
            EncryptedPayload(
                ciphertext=secret.ciphertext,
                nonce=secret.nonce,
                wrapped_data_key=secret.wrapped_data_key,
                key_version=secret.key_version,
                fingerprint=secret.secret_fingerprint,
            )
        )
        register_secret_material(plaintext)
        secret.last_used_at = datetime.now(UTC)
        return plaintext

    async def rotate(self, workspace_id: UUID, secret_id: UUID, new_plaintext: str) -> Secret:
        secret = await self.get(workspace_id, secret_id)
        # Validate/register before mutating the ORM row. If validation fails,
        # even a later caller commit cannot persist a half-rotated secret.
        register_secret_material(new_plaintext)
        payload = self._crypto.encrypt(new_plaintext)
        secret.ciphertext = payload.ciphertext
        secret.nonce = payload.nonce
        secret.wrapped_data_key = payload.wrapped_data_key
        secret.key_version = payload.key_version
        secret.secret_fingerprint = payload.fingerprint
        secret.masked_hint = mask_hint(new_plaintext)
        secret.rotated_at = datetime.now(UTC)
        await self._session.flush()
        return secret

    async def delete(self, workspace_id: UUID, secret_id: UUID) -> None:
        secret = await self.get(workspace_id, secret_id)
        await self._session.delete(secret)
        await self._session.flush()
