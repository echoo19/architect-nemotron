"""Image inputs: header sniffing, validated declarations, ceilings, estimates, PNG encoding."""

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from image_support import gif_bytes, jpeg_bytes, png_bytes, webp_bytes
from pydantic import ValidationError

from architect.models import AdapterError, ModelRequest
from architect.models import pricing as pricing_module
from architect.models.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_REQUEST,
    ImageInput,
    ImageStub,
    b64,
    data_url,
    encode_png_gray,
    sniff,
)
from architect.models.pricing import (
    DEFAULT_PRICES,
    ImagePricing,
    Price,
    image_estimate,
    image_reservation,
    load_prices,
    price_for,
)

LETTER = png_bytes(816, 1056)
PRICES = load_prices()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png_bytes(3, 2), ("image/png", 3, 2)),
        (jpeg_bytes(640, 480), ("image/jpeg", 640, 480)),
        (jpeg_bytes(31, 17, progressive=True), ("image/jpeg", 31, 17)),
        (
            jpeg_bytes(9, 8, comment=b"made by a test", fill=True, restart=True),
            ("image/jpeg", 9, 8),
        ),
        (webp_bytes(300, 200, chunk="VP8 "), ("image/webp", 300, 200)),
        (webp_bytes(300, 200, chunk="VP8L"), ("image/webp", 300, 200)),
        (webp_bytes(4000, 3000, chunk="VP8X"), ("image/webp", 4000, 3000)),
        (gif_bytes(12, 34), ("image/gif", 12, 34)),
        (gif_bytes(12, 34, version=b"GIF87a"), ("image/gif", 12, 34)),
    ],
)
def test_sniff_and_from_bytes(data: bytes, expected: tuple[str, int, int]) -> None:
    assert sniff(data) == expected
    image = ImageInput.from_bytes(data)
    assert (image.media_type, image.width, image.height) == expected
    assert image.data == data
    assert image.cache is False
    assert ImageInput.from_bytes(data, cache=True).cache is True


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image at all",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,  # truncated before IHDR
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 8,  # zero dimensions
        b"\xff\xd8",  # bare SOI
        b"\xff\xd8\xff\xd9",  # EOI before any frame header
        b"\xff\xd8\xff\xda\x00\x02",  # scan before any frame header
        b"\xff\xd8\x00\xc0",  # marker byte missing
        b"\xff\xd8\xff\xc0\x00\x01",  # impossible segment length
        b"RIFF\x00\x00\x00\x00WAVE",  # RIFF but not WebP
        b"RIFF\x00\x00\x00\x00WEBPALPH" + b"\x00" * 20,  # unknown first chunk
        webp_bytes(1, 1, chunk="VP8 ")[:23] + b"\x00\x00\x00" + b"\x00" * 4,  # bad start code
        webp_bytes(1, 1, chunk="VP8L")[:20] + b"\x00" + b"\x00" * 4,  # bad VP8L signature
        b"GIF89a\x00\x00\x00\x00\x00\x00\x00;",  # zero dimensions
        b"GIF89",  # truncated
        b"BM" + b"\x00" * 60,  # BMP is not accepted
    ],
)
def test_sniff_rejects_unsupported(data: bytes) -> None:
    with pytest.raises(ValueError, match="unsupported_image"):
        sniff(data)
    with pytest.raises(ValueError, match="unsupported_image"):
        ImageInput.from_bytes(data)


def test_declaration_must_match_header() -> None:
    data = png_bytes(4, 5)
    ImageInput(data=data, media_type="image/png", width=4, height=5)
    for bad in (
        {"media_type": "image/jpeg", "width": 4, "height": 5},
        {"media_type": "image/png", "width": 5, "height": 4},
        {"media_type": "image/png", "width": 4, "height": 6},
    ):
        with pytest.raises(ValidationError, match="header does not match"):
            ImageInput(data=data, **bad)
    with pytest.raises(ValidationError, match="unsupported_image"):
        ImageInput(data=b"nope", media_type="image/png", width=1, height=1)


@pytest.mark.parametrize(
    "data",
    [
        {"data": b"", "media_type": "image/png", "width": 1, "height": 1},
        {"data": "text", "media_type": "image/png", "width": 1, "height": 1},
        {"data": png_bytes(1, 1), "media_type": "image/bmp", "width": 1, "height": 1},
        {"data": png_bytes(1, 1), "media_type": "image/png", "width": 1.0, "height": 1},
        {"data": png_bytes(1, 1), "media_type": "image/png", "width": 1, "height": True},
        {"data": png_bytes(1, 1), "media_type": "image/png", "width": 1, "height": 1, "x": 1},
    ],
)
def test_image_input_validation(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ImageInput.model_validate(data)


def test_image_input_is_frozen_with_content_free_stub() -> None:
    image = ImageInput.from_bytes(png_bytes(7, 3))
    with pytest.raises(ValidationError):
        image.width = 8  # type: ignore[misc]
    assert image.sha256 == hashlib.sha256(image.data).hexdigest()
    assert image.pixels == 21
    assert hash(image) == hash(ImageInput.from_bytes(png_bytes(7, 3)))
    stub = image.stub()
    assert stub == ImageStub(
        sha256=image.sha256, media_type="image/png", width=7, height=3, attached=1
    )
    assert image.stub(attached=3).attached == 3
    assert "data" not in stub.model_dump()
    assert base64.b64encode(image.data).decode() not in stub.model_dump_json()
    for bad in (
        {"sha256": "abc"},
        {"sha256": "A" * 64},
        {"attached": 0},
        {"attached": "1"},
        {"width": 0},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            ImageStub.model_validate({**stub.model_dump(), **bad})


def test_request_level_caps_and_defaults() -> None:
    assert MAX_IMAGE_BYTES == 20 * 1024 * 1024
    assert MAX_IMAGES_PER_REQUEST == 100
    request = ModelRequest(prompt="hi")
    assert request.images == ()
    assert request.image_detail == "auto"
    image = ImageInput.from_bytes(png_bytes(1, 1))
    many = ModelRequest(prompt="hi", images=(image,) * MAX_IMAGES_PER_REQUEST)
    assert len(many.images) == MAX_IMAGES_PER_REQUEST
    assert hash(many)  # frozen and hashable with images
    with pytest.raises(ValidationError):
        ModelRequest(prompt="hi", images=(image,) * (MAX_IMAGES_PER_REQUEST + 1))
    with pytest.raises(ValidationError):
        ModelRequest(prompt="hi", image_detail="medium")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ModelRequest.model_validate({"prompt": "hi", "images": (), "extra": 1})
    with pytest.raises(ValidationError):
        ModelRequest(prompt="hi", cache_prefix="no", images=(image,))


def test_image_input_upper_byte_cap_is_enforced_by_schema() -> None:
    field = ImageInput.model_fields["data"]
    assert any(getattr(item, "max_length", None) == MAX_IMAGE_BYTES for item in field.metadata)


@pytest.mark.parametrize(
    ("model", "ceiling"),
    [
        ("fake", 100),
        ("claude-sonnet-4-20250514", 1600),
        ("claude-haiku-4-5-20251001", 1600),
        ("claude-sonnet-5", 1600),
        ("claude-opus-5-5", 1600),
        ("claude-fable-5-1", 1600),
        ("gpt-4o-mini", 48170),
        ("gpt-5.6-terra", 8192),
        ("llama3.2-vision", 1600),
    ],
)
def test_reservation_is_count_times_ceiling_for_every_rule(model: str, ceiling: int) -> None:
    price = price_for(PRICES, model)
    assert price.image is not None
    small = ImageInput.from_bytes(png_bytes(1, 1))
    letter = ImageInput.from_bytes(LETTER)
    assert image_reservation(price, []) == 0
    assert image_reservation(price, [small]) == ceiling
    assert image_reservation(price, [letter]) == ceiling  # size never changes the reservation
    assert image_reservation(price, [small, letter, small]) == 3 * ceiling
    # The estimate never exceeds the reservation for a letter page.
    assert image_estimate(price, letter) <= ceiling


def test_unconfigured_model_refuses_images_and_text_only_is_free() -> None:
    text_only = price_for(PRICES, "llama3.2")
    assert text_only.image is None
    assert image_reservation(text_only, []) == 0
    with pytest.raises(AdapterError, match="Model is not configured for image input"):
        image_reservation(text_only, [ImageInput.from_bytes(png_bytes(1, 1))])
    with pytest.raises(AdapterError, match="Model is not configured for image input"):
        image_estimate(text_only, ImageInput.from_bytes(png_bytes(1, 1)))


def test_per_model_byte_and_count_caps() -> None:
    price = Price(
        input_per_million=1.0,
        output_per_million=1.0,
        input_overhead_tokens=0,
        output_capacity_tokens=1,
        image=ImagePricing(
            tokens_per_image_max=10, max_bytes=200, max_images=2, basis="assumption"
        ),
    )
    tiny = ImageInput.from_bytes(png_bytes(1, 1))
    assert len(tiny.data) <= 200
    big = ImageInput.from_bytes(png_bytes(64, 64, shade=0x00))
    assert len(big.data) <= 200  # solid fill compresses well; pad via a wider row instead
    wide = ImageInput.from_bytes(encode_png_gray([bytes(range(256)) * 2] * 2, 512, 2))
    assert len(wide.data) > 200
    assert image_reservation(price, [tiny, tiny]) == 20
    with pytest.raises(AdapterError, match="Too many images for model"):
        image_reservation(price, [tiny, tiny, tiny])
    with pytest.raises(AdapterError, match="Image exceeds model byte limit"):
        image_reservation(price, [tiny, wide])
    # The bundled limits: Anthropic 5 MB / 100 images, OpenAI 20 MB / 20 images.
    anthropic = price_for(PRICES, "claude-haiku-4-5-20251001").image
    openai = price_for(PRICES, "gpt-4o-mini").image
    assert anthropic is not None and openai is not None
    assert (anthropic.max_bytes, anthropic.max_images) == (5_000_000, 100)
    assert (openai.max_bytes, openai.max_images) == (20_000_000, 20)
    with pytest.raises(AdapterError, match="Too many images"):
        image_reservation(price_for(PRICES, "gpt-4o-mini"), [tiny] * 21)


def test_estimate_numbers_per_rule() -> None:
    letter = ImageInput.from_bytes(LETTER)
    square = ImageInput.from_bytes(png_bytes(200, 200))
    anthropic = price_for(PRICES, "claude-sonnet-5")
    assert image_estimate(anthropic, letter) == 1149
    assert image_estimate(anthropic, square) == 54
    mini = price_for(PRICES, "gpt-4o-mini")
    assert image_estimate(mini, letter) == 25497  # 4 tiles -> 765 x 33.33
    assert image_estimate(mini, letter, "high") == 25497
    assert image_estimate(mini, letter, "low") == round(85 * 33.33)
    assert image_estimate(mini, square) == round((85 + 170) * 33.33)  # one tile
    huge = ImageInput.from_bytes(webp_bytes(4096, 4096, chunk="VP8X"))
    assert image_estimate(mini, huge) == round((85 + 170 * 4) * 33.33)  # 2048 fit -> 768 short
    tall = ImageInput.from_bytes(webp_bytes(768, 2048, chunk="VP8X"))
    assert image_estimate(mini, tall) == round((85 + 170 * 8) * 33.33) < 48170
    patches = anthropic.model_copy(
        update={
            "image": ImagePricing(
                tokens_per_image_max=1536, rule="openai_patches", basis="assumption"
            )
        }
    )
    assert image_estimate(patches, letter) == 26 * 33
    assert image_estimate(patches, huge) == 1536
    fixed = price_for(PRICES, "gpt-5.6-terra")
    assert image_estimate(fixed, letter) == image_estimate(fixed, square) == 8192
    assert image_estimate(price_for(PRICES, "fake"), letter) == 100


def test_image_pricing_model_is_closed() -> None:
    with pytest.raises(ValidationError):
        ImagePricing(tokens_per_image_max=1)  # type: ignore[call-arg]  # basis required
    with pytest.raises(ValidationError):
        ImagePricing(tokens_per_image_max=0, basis="assumption")
    with pytest.raises(ValidationError):
        ImagePricing(tokens_per_image_max=1, basis="guess")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ImagePricing(tokens_per_image_max=1, basis="assumption", rule="tiles")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ImagePricing.model_validate({"tokens_per_image_max": 1, "basis": "assumption", "x": 1})
    pricing = ImagePricing(tokens_per_image_max=1, basis="measured")
    with pytest.raises(ValidationError):
        pricing.tokens_per_image_max = 2  # type: ignore[misc]
    assert (pricing.rule, pricing.detail, pricing.max_images) == ("fixed", "auto", 20)
    assert pricing.max_bytes == 20 * 1024 * 1024
    assert Price.model_fields["image"].default is None


def test_every_bundled_ceiling_has_a_basis_documented_in_the_module() -> None:
    raw = json.loads(DEFAULT_PRICES.read_text(encoding="utf-8"))["models"]
    docstring = pricing_module.__doc__ or ""
    # Text-only models carry no image ceiling; the Nemotron ids cite their price source.
    text_only = {"llama3.2", *(name for name in raw if name.startswith("nvidia/"))}
    assert len(text_only) == 5
    assert "https://tokenfactory.nebius.com/api/public/models_info" in docstring
    for name, entry in raw.items():
        assert name in docstring
        if name in text_only:
            assert "image" not in entry
            continue
        image = entry["image"]
        assert image["basis"] in ("provider_docs", "assumption", "measured")
        assert str(image["tokens_per_image_max"]) in docstring
        assert image["basis"] in docstring
    assumed = {
        name for name, entry in raw.items()
        if entry.get("image", {}).get("basis") == "assumption"
    }
    assert assumed == {"fake", "gpt-5.6-terra", "llama3.2-vision"}
    vision = raw["llama3.2-vision"]
    assert vision["allowed_response_models"] == ["llama3.2-vision:latest"]
    assert (vision["output_capacity_tokens"], vision["input_overhead_tokens"]) == (131072, 1024)
    assert (vision["input_per_million"], vision["output_per_million"]) == (0.0, 0.0)


def test_b64_and_data_url() -> None:
    image = ImageInput.from_bytes(gif_bytes(2, 2))
    encoded = base64.b64encode(image.data).decode("ascii")
    assert b64(image) == encoded
    assert data_url(image) == f"data:image/gif;base64,{encoded}"


def test_encode_png_gray_round_trips_deterministically(tmp_path: Path) -> None:
    rows = [bytes([(x * 7 + y) % 256 for x in range(5)]) for y in range(3)]
    first = encode_png_gray(rows, 5, 3)
    assert first == encode_png_gray(rows, 5, 3)
    assert sniff(first) == ("image/png", 5, 3)
    image = ImageInput.from_bytes(first)
    assert image.pixels == 15
    assert first != encode_png_gray([bytes(5)] * 3, 5, 3)
    for bad_rows, width, height in (
        (rows, 4, 3),
        (rows, 5, 2),
        (rows[:2], 5, 3),
        (rows, 0, 3),
        ([], 1, 0),
    ):
        with pytest.raises(ValueError):
            encode_png_gray(bad_rows, width, height)
