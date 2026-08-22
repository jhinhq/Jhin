"""Safe avatar media for Jhin: normalization pipeline and the MediaStore
boundary (experience design: agent avatars and media)."""

from jhin_media.avatars import AgentNotFound, activate_avatar, clear_avatar
from jhin_media.base import MediaStore, StoredVariant
from jhin_media.images import (
    MAX_DIMENSION,
    MAX_UPLOAD_BYTES,
    VARIANT_SIZES,
    ImageRejected,
    normalize_avatar,
)
from jhin_media.postgres import PostgresMediaStore
from jhin_media.prompts import MAX_PROMPT_HINT_LENGTH, build_avatar_prompt

__all__ = [
    "MAX_DIMENSION",
    "MAX_PROMPT_HINT_LENGTH",
    "MAX_UPLOAD_BYTES",
    "VARIANT_SIZES",
    "AgentNotFound",
    "ImageRejected",
    "MediaStore",
    "PostgresMediaStore",
    "StoredVariant",
    "activate_avatar",
    "build_avatar_prompt",
    "clear_avatar",
    "normalize_avatar",
]
