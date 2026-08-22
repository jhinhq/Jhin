"""Encrypted secret rows (plan 6.10).

Only ciphertext ever reaches this table. Encryption/decryption lives in
``jhin_secrets``; nothing in this model can produce plaintext.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin
from jhin_domain import SecretType


class Secret(Base, UuidPkMixin, TimestampMixin):
    __tablename__ = "secret"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(32), default=SecretType.API_KEY.value)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer)
    # Keyed HMAC of the plaintext: supports duplicate detection and redaction
    # matching without ever storing recoverable material.
    secret_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Display-only hint ("••••7A2F"); persisted at write time so listing
    # secrets never requires decryption.
    masked_hint: Mapped[str] = mapped_column(String(16), default="")
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    rotated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
