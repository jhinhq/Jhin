"""Model provider abstraction for Jhin (plan section 15).

All provider-specific logic lives in this package (plan 47): the rest of the
platform sees only :class:`ModelClient`, :class:`ModelRequest`, and
:class:`ModelResponse`.
"""

from jhin_models.base import (
    ModelClient,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
    ToolSchema,
)
from jhin_models.factory import build_model_client
from jhin_models.images import (
    GeneratedImage,
    ImageGenerationClient,
    ImageGenerationConfig,
    ImageGenerationUnsupported,
    as_image_generation_client,
)

__all__ = [
    "GeneratedImage",
    "ImageGenerationClient",
    "ImageGenerationConfig",
    "ImageGenerationUnsupported",
    "ModelClient",
    "ModelMessage",
    "ModelProviderError",
    "ModelRequest",
    "ModelResponse",
    "ModelToolCall",
    "ModelUsage",
    "ToolSchema",
    "as_image_generation_client",
    "build_model_client",
]
