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
    ModelUsage,
    ToolSchema,
)
from jhin_models.factory import build_model_client

__all__ = [
    "ModelClient",
    "ModelMessage",
    "ModelProviderError",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ToolSchema",
    "build_model_client",
]
