"""Model provider abstraction for Jhin (plan section 15).

All provider-specific logic lives in this package (plan 47): the rest of the
platform sees only :class:`ModelClient`, :class:`ModelRequest`, and
:class:`ModelResponse`.
"""

from jhin_models.base import (
    INSUFFICIENT_FUNDS,
    AccountStatus,
    AccountStatusUnsupported,
    ModelClient,
    ModelListing,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    ToolSchema,
)
from jhin_models.embeddings import (
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingResult,
    EmbeddingUnsupported,
    as_embedding_client,
)
from jhin_models.factory import build_model_client
from jhin_models.images import (
    GeneratedImage,
    ImageGenerationClient,
    ImageGenerationConfig,
    ImageGenerationUnsupported,
    as_image_generation_client,
)
from jhin_models.tool_arguments import normalize_tool_arguments

__all__ = [
    "INSUFFICIENT_FUNDS",
    "AccountStatus",
    "AccountStatusUnsupported",
    "EmbeddingClient",
    "EmbeddingConfig",
    "EmbeddingResult",
    "EmbeddingUnsupported",
    "GeneratedImage",
    "ImageGenerationClient",
    "ImageGenerationConfig",
    "ImageGenerationUnsupported",
    "ModelClient",
    "ModelListing",
    "ModelMessage",
    "ModelProviderError",
    "ModelRequest",
    "ModelResponse",
    "ModelToolCall",
    "ModelUsage",
    "ToolSchema",
    "as_embedding_client",
    "as_image_generation_client",
    "build_model_client",
    "normalize_tool_arguments",
]
