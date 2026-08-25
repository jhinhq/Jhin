"""Model provider abstraction for Jhin (plan section 15).

All provider-specific logic lives in this package (plan 47): the rest of the
platform sees only :class:`ModelClient`, :class:`ModelRequest`, and
:class:`ModelResponse`.
"""

from jhin_models.base import (
    INSUFFICIENT_FUNDS,
    MODEL_INCOMPATIBLE_REQUEST,
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
from jhin_models.reasoning import (
    REASONING_CONFIG_KEY,
    ReasoningConfig,
    is_reasoning_model,
    reasoning_unsupported_reason,
)
from jhin_models.tool_arguments import normalize_tool_arguments
from jhin_models.web_search import (
    WEB_SEARCH_CONFIG_KEY,
    WebSearchConfig,
    web_search_unsupported_reason,
)

__all__ = [
    "INSUFFICIENT_FUNDS",
    "MODEL_INCOMPATIBLE_REQUEST",
    "REASONING_CONFIG_KEY",
    "WEB_SEARCH_CONFIG_KEY",
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
    "ReasoningConfig",
    "ToolSchema",
    "WebSearchConfig",
    "as_embedding_client",
    "as_image_generation_client",
    "build_model_client",
    "is_reasoning_model",
    "normalize_tool_arguments",
    "reasoning_unsupported_reason",
    "web_search_unsupported_reason",
]
