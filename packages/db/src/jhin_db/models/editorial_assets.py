"""Persisted image choice and conservative provider tracking reservation."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jhin_db.base import Base
from jhin_db.columns import JsonDict, StdUuid, TimestampMixin, UtcDateTime, UuidPkMixin


class EditorialAsset(Base, UuidPkMixin, TimestampMixin):
    """One chosen photo, and the record of who was allowed to choose it.

    ``selection_mode`` is the part a later reader must not have to infer.
    ``human`` means a person's recorded ``unsplash_photo`` answer named this
    photo, and ``question_id``/``selected_by_user_id`` are that answer.
    ``autonomous`` means an operator approved the writer choosing among the
    photos it retrieved, so the question is the person's ``unsplash_search``
    request and ``selected_by_user_id`` is who asked for the search -- not who
    picked the image. ``selection_authority_json`` carries the proof for that
    mode: the approving operator, when they approved, and the completed search
    call the photo actually came from.
    """

    __tablename__ = "editorial_asset"
    __table_args__ = (
        UniqueConstraint("assignment_id", "question_id"),
        UniqueConstraint("workspace_id", "question_id", name="uq_asset_human_choice"),
    )
    workspace_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("workspace.id", ondelete="CASCADE")
    )
    assignment_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("editorial_assignment.id", ondelete="CASCADE")
    )
    connection_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("connection.id", ondelete="RESTRICT")
    )
    question_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("user_question.id", ondelete="RESTRICT")
    )
    photo_id: Mapped[str] = mapped_column(String(100))
    selected_by_user_id: Mapped[UUID] = mapped_column(
        StdUuid, ForeignKey("user.id", ondelete="RESTRICT")
    )
    selected_at: Mapped[datetime] = mapped_column(UtcDateTime)
    selection_mode: Mapped[str] = mapped_column(String(16), default="human")
    selection_authority_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    tracking_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    tracking_receipt_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="tracking")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonDict, default=dict)
