"""Safe avatar media for Jhin: normalization pipeline and the MediaStore
boundary (experience design: agent avatars and media)."""

from jhin_media.avatars import AgentNotFound, activate_avatar, clear_avatar
from jhin_media.base import MediaStore, StoredVariant
from jhin_media.images import (
    ACCEPTED_CONTENT_TYPES,
    MAX_DIMENSION,
    MAX_UPLOAD_BYTES,
    OUTPUT_CONTENT_TYPE,
    VARIANT_SIZES,
    ImageRejected,
    NormalizedAvatar,
    normalize_avatar,
)
from jhin_media.postgres import PostgresMediaStore
from jhin_media.prompts import MAX_PROMPT_HINT_LENGTH, build_avatar_prompt
from jhin_media.s3 import S3MediaStore, S3MediaStoreConfig

__all__ = [
    "ACCEPTED_CONTENT_TYPES",
    "MAX_DIMENSION",
    "MAX_PROMPT_HINT_LENGTH",
    "MAX_UPLOAD_BYTES",
    "OUTPUT_CONTENT_TYPE",
    "VARIANT_SIZES",
    "AgentNotFound",
    "ImageRejected",
    "MediaStore",
    "NormalizedAvatar",
    "PostgresMediaStore",
    "S3MediaStore",
    "S3MediaStoreConfig",
    "StoredVariant",
    "activate_avatar",
    "build_avatar_prompt",
    "clear_avatar",
    "normalize_avatar",
]
