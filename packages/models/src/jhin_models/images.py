"""Optional image-generation capability (experience design: agent avatars).

Chat providers are never required to generate images. A provider that can
implements :class:`ImageGenerationClient`; callers use
:func:`as_image_generation_client` to discover support and receive a clean
:class:`ImageGenerationUnsupported` otherwise. Results are raw bytes that the
caller must pass through the safe normalizer before storing or serving.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from jhin_models.base import ModelClient, ModelProviderError

DEFAULT_IMAGE_SIZE = "1024x1024"
IMAGE_GENERATION_CONFIG_KEY = "image_generation"


class ImageGenerationUnsupported(ModelProviderError):
    """The provider (or profile) cannot generate images."""

    def __init__(self, message: str = "image generation is not supported") -> None:
        super().__init__(message, retryable=False)


class GeneratedImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: bytes
    content_type: str
    model: str = ""
    provider_request_id: str | None = None
    latency_ms: int = 0


class ImageGenerationConfig(BaseModel):
    """Per-profile ``config_json.image_generation`` block.

    ``cost_micros`` is the operator-declared price per image in micro-dollars
    so the UI can disclose cost before the call is made.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    enabled: bool = False
    model: str = ""
    size: str = DEFAULT_IMAGE_SIZE
    cost_micros: int | None = None

    @classmethod
    def from_profile_config(cls, config_json: dict[str, Any] | None) -> ImageGenerationConfig:
        raw = (config_json or {}).get(IMAGE_GENERATION_CONFIG_KEY)
        if not isinstance(raw, dict):
            return cls()
        return cls.model_validate(raw)


@runtime_checkable
class ImageGenerationClient(Protocol):
    async def generate_image(
        self, prompt: str, *, model: str, size: str = DEFAULT_IMAGE_SIZE
    ) -> GeneratedImage:
        """Render one still image for ``prompt``. Never fetches caller URLs."""


def as_image_generation_client(client: ModelClient) -> ImageGenerationClient:
    if isinstance(client, ImageGenerationClient):
        return client
    provider = getattr(client, "provider_name", type(client).__name__)
    raise ImageGenerationUnsupported(f"{provider}: image generation is not supported")
