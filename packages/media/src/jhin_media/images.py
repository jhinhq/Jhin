"""Safe avatar normalization (experience design: agent avatars and media).

Every avatar — uploaded or generated — passes through :func:`normalize_avatar`
before a single byte is stored. The pipeline only accepts bytes that Pillow
decodes as PNG, JPEG, or WebP; everything else (SVG, GIF, video, a PNG with a
JPEG label) is rejected with a stable ``code``. Animated and multi-frame
images, oversized bytes/pixels, and decompression bombs are rejected before
any resize. Output is metadata-free square WebP at 64/128/256 px, so the
original bytes are never needed again.

This module never fetches remote URLs: callers hand it bytes they already
hold.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = MAX_DIMENSION * MAX_DIMENSION
MIN_DIMENSION = 16
VARIANT_SIZES: tuple[int, ...] = (64, 128, 256)
OUTPUT_CONTENT_TYPE = "image/webp"
WEBP_QUALITY = 85

# Pillow format name -> the MIME type a client must declare for it.
ACCEPTED_FORMATS: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
ACCEPTED_CONTENT_TYPES = frozenset(ACCEPTED_FORMATS.values())


class ImageRejected(ValueError):
    """The bytes were refused. ``code`` is stable and safe to show users."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NormalizedAvatar:
    """All variants of one avatar, already validated and re-encoded."""

    variants: dict[int, bytes]
    width: int
    height: int
    sha256: str
    source_format: str
    content_type: str = OUTPUT_CONTENT_TYPE

    def variant(self, size: int) -> bytes:
        return self.variants[size]


def _open_bounded(data: bytes) -> Image.Image:
    if not data:
        raise ImageRejected("empty", "The image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected(
            "too_large", f"The image exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )
    # Pillow's bomb guard: a header claiming more pixels than the limit is an
    # error, not a warning, so a 1-KB "30000x30000" PNG never allocates.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                image = Image.open(io.BytesIO(data))
            except Image.DecompressionBombError as exc:
                raise ImageRejected("decompression_bomb", "The image is too large") from exc
            except Image.DecompressionBombWarning as exc:
                raise ImageRejected("decompression_bomb", "The image is too large") from exc
            except UnidentifiedImageError as exc:
                raise ImageRejected(
                    "unsupported_format", "Only PNG, JPEG, or WebP images are accepted"
                ) from exc
            except (OSError, ValueError) as exc:
                raise ImageRejected("undecodable", "The image could not be decoded") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return image


def _validate_decoded(image: Image.Image, declared_content_type: str | None) -> str:
    image_format = image.format or ""
    expected_content_type = ACCEPTED_FORMATS.get(image_format)
    if expected_content_type is None:
        raise ImageRejected("unsupported_format", "Only PNG, JPEG, or WebP images are accepted")
    if declared_content_type is not None:
        declared = declared_content_type.split(";", 1)[0].strip().lower()
        if declared != expected_content_type:
            raise ImageRejected(
                "content_type_mismatch",
                f"Declared type {declared!r} does not match the decoded {image_format} image",
            )
    if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
        raise ImageRejected("animated", "Animated or multi-frame images are not accepted")
    width, height = image.size
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise ImageRejected(
            "too_many_pixels", f"The image exceeds {MAX_DIMENSION}x{MAX_DIMENSION} pixels"
        )
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise ImageRejected("too_small", f"The image must be at least {MIN_DIMENSION} px")
    return image_format


def _decode_fully(image: Image.Image) -> Image.Image:
    """Materialize pixels (truncated files fail here) and drop every tag.

    ``exif_transpose`` applies orientation *before* metadata is discarded so
    the visible result matches what the uploader saw. Converting to a fresh
    RGB/RGBA image leaves no ``info`` (EXIF, ICC, XMP, comments) behind.
    """
    try:
        image.load()
        oriented = ImageOps.exif_transpose(image) or image
        mode = "RGBA" if "A" in oriented.getbands() or oriented.mode == "P" else "RGB"
        converted = oriented.convert(mode)
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageRejected("undecodable", "The image could not be decoded") from exc
    return Image.frombytes(converted.mode, converted.size, converted.tobytes())


def _center_square(image: Image.Image) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _encode_webp(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    # No exif/icc_profile kwargs: the encoder writes pixels only.
    image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
    return buffer.getvalue()


def normalize_avatar(data: bytes, *, declared_content_type: str | None = None) -> NormalizedAvatar:
    """Validate ``data`` and produce metadata-free square WebP variants.

    Raises :class:`ImageRejected` for anything that is not a plain, decoded,
    bounded PNG/JPEG/WebP still image.
    """
    image = _open_bounded(data)
    source_format = _validate_decoded(image, declared_content_type)
    square = _center_square(_decode_fully(image))
    variants: dict[int, bytes] = {}
    for size in VARIANT_SIZES:
        resized = square.resize((size, size), Image.Resampling.LANCZOS)
        variants[size] = _encode_webp(resized)
    digest = hashlib.sha256()
    for size in VARIANT_SIZES:
        digest.update(variants[size])
    largest = max(VARIANT_SIZES)
    return NormalizedAvatar(
        variants=variants,
        width=largest,
        height=largest,
        sha256=digest.hexdigest(),
        source_format=source_format,
    )
