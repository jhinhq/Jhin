"""Image-safety invariants for the avatar normalizer."""

from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from jhin_media import (
    VARIANT_SIZES,
    ImageRejected,
    build_avatar_prompt,
    normalize_avatar,
)
from jhin_media.images import MAX_UPLOAD_BYTES


def _encode(image: Image.Image, fmt: str, **kwargs: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _gradient(width: int = 120, height: int = 80, mode: str = "RGB") -> Image.Image:
    image = Image.new(mode, (width, height))
    pixels = [
        (x * 255 // max(width - 1, 1), y * 255 // max(height - 1, 1), 90)
        + ((200,) if mode == "RGBA" else ())
        for y in range(height)
        for x in range(width)
    ]
    image.putdata(pixels)
    return image


def _png_header_only(width: int, height: int) -> bytes:
    """A syntactically valid PNG whose header claims ``width x height``."""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 10)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_accepted_formats_produce_three_square_webp_variants(fmt: str, content_type: str) -> None:
    data = _encode(_gradient(), fmt)
    result = normalize_avatar(data, declared_content_type=content_type)
    assert set(result.variants) == set(VARIANT_SIZES)
    assert result.source_format == fmt
    assert result.content_type == "image/webp"
    assert result.width == result.height == 256
    for size, payload in result.variants.items():
        decoded = Image.open(io.BytesIO(payload))
        assert decoded.format == "WEBP"
        assert decoded.size == (size, size)
    assert len(result.sha256) == 64


def test_normalization_is_deterministic() -> None:
    data = _encode(_gradient(), "PNG")
    assert normalize_avatar(data).sha256 == normalize_avatar(data).sha256


def test_transparency_is_preserved_for_png_with_alpha() -> None:
    data = _encode(_gradient(mode="RGBA"), "PNG")
    decoded = Image.open(io.BytesIO(normalize_avatar(data).variant(64)))
    assert decoded.mode == "RGBA"


def test_declared_content_type_is_optional() -> None:
    assert normalize_avatar(_encode(_gradient(), "PNG")).source_format == "PNG"


def test_metadata_is_stripped() -> None:
    image = _gradient()
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    exif[0x0112] = 1  # Orientation
    data = _encode(image, "JPEG", exif=exif.tobytes(), comment=b"hidden comment")
    assert b"secret description" in data
    result = normalize_avatar(data, declared_content_type="image/jpeg")
    for payload in result.variants.values():
        assert b"secret description" not in payload
        assert b"hidden comment" not in payload
        decoded = Image.open(io.BytesIO(payload))
        assert not decoded.getexif()
        assert "icc_profile" not in decoded.info
        assert "exif" not in decoded.info


def test_svg_is_rejected() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(svg, declared_content_type="image/svg+xml")
    assert excinfo.value.code == "unsupported_format"


def test_gif_even_static_is_rejected() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(_encode(_gradient(), "GIF"))
    assert excinfo.value.code == "unsupported_format"


def test_animated_webp_is_rejected() -> None:
    frames = [_gradient(), _gradient().rotate(180)]
    data = _encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(data, declared_content_type="image/webp")
    assert excinfo.value.code == "animated"


def test_multi_frame_png_is_rejected() -> None:
    frames = [_gradient(), _gradient().rotate(90)]
    data = _encode(frames[0], "PNG", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(data, declared_content_type="image/png")
    assert excinfo.value.code == "animated"


def test_mime_mismatch_is_rejected() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(_encode(_gradient(), "PNG"), declared_content_type="image/jpeg")
    assert excinfo.value.code == "content_type_mismatch"


def test_oversized_bytes_are_rejected_before_decoding() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(b"\x89PNG" + b"\x00" * MAX_UPLOAD_BYTES)
    assert excinfo.value.code == "too_large"


def test_oversized_pixel_dimensions_are_rejected() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(_png_header_only(5000, 100))
    assert excinfo.value.code == "too_many_pixels"


def test_decompression_bomb_header_is_rejected() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(_png_header_only(30_000, 30_000))
    assert excinfo.value.code == "decompression_bomb"


def test_truncated_file_is_rejected() -> None:
    data = _encode(_gradient(400, 400), "PNG")
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(data[: len(data) // 2])
    assert excinfo.value.code == "undecodable"


def test_empty_and_garbage_are_rejected() -> None:
    with pytest.raises(ImageRejected) as empty:
        normalize_avatar(b"")
    assert empty.value.code == "empty"
    with pytest.raises(ImageRejected) as garbage:
        normalize_avatar(b"definitely not an image")
    assert garbage.value.code == "unsupported_format"


def test_tiny_images_are_rejected() -> None:
    with pytest.raises(ImageRejected) as excinfo:
        normalize_avatar(_encode(_gradient(8, 8), "PNG"))
    assert excinfo.value.code == "too_small"


def test_prompt_uses_public_identity_and_hint_only() -> None:
    prompt = build_avatar_prompt(
        name="Ada",
        role_title="Staff Engineer",
        public_purpose="Keeps the build green",
        expertise=["ci", "python"],
        prompt_hint="warm colors\nplease",
    )
    assert "Ada" in prompt
    assert "Staff Engineer" in prompt
    assert "Keeps the build green" in prompt
    assert "ci, python" in prompt
    assert "warm colors please" in prompt
    assert "not photorealistic" in prompt
