"""Validated image inputs, content-free stubs, and pure-stdlib image codecs.

``ImageInput`` carries the bytes a model is shown; its header is sniffed on
construction so ``media_type``, ``width`` and ``height`` are always what the
bytes say, never what a caller claims. ``ImageStub`` is the content-free
reference that crosses the sandbox pipe and sits inside task JSON: a sha256,
the declared shape, and a 1-based attachment index. ``sniff`` and
``encode_png_gray`` are ``struct``/``zlib`` only so the default test suite and
the bitmap page renderer need no imaging library.

``MAX_IMAGE_BYTES`` is the request-level cap; per-model byte and count limits
live in ``pricing.ImagePricing`` so one provider's limit never constrains
another.
"""

import base64
import hashlib
import struct
import zlib
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # request-level cap; per-model caps live in ImagePricing
MAX_IMAGES_PER_REQUEST = 100
SHA256_HEX = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2})
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})


class ImageInput(BaseModel):
    """One image shown to a model; validated against its own header."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data: Annotated[bytes, Field(min_length=1, max_length=MAX_IMAGE_BYTES, strict=True)]
    media_type: MediaType
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    cache: bool = False  # Anthropic: cache_control on this image block

    @model_validator(mode="after")
    def _header_matches_declaration(self) -> Self:
        if sniff(self.data) != (self.media_type, self.width, self.height):
            raise ValueError("Image header does not match its declared media type and size")
        return self

    @classmethod
    def from_bytes(cls, data: bytes, *, cache: bool = False) -> "ImageInput":
        media_type, width, height = sniff(data)
        return cls(data=data, media_type=media_type, width=width, height=height, cache=cache)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def stub(self, attached: int = 1) -> "ImageStub":
        return ImageStub(
            sha256=self.sha256,
            media_type=self.media_type,
            width=self.width,
            height=self.height,
            attached=attached,
        )


class ImageStub(BaseModel):
    """Content-free reference to an attached image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: SHA256_HEX
    media_type: MediaType
    width: Annotated[int, Field(gt=0, strict=True)]
    height: Annotated[int, Field(gt=0, strict=True)]
    attached: Annotated[int, Field(ge=1, strict=True)]  # 1-based position in the task


ImageRef = ImageInput | ImageStub


def _unsupported() -> ValueError:
    return ValueError("unsupported_image")


def _sniff_png(data: bytes) -> tuple[MediaType, int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise _unsupported()
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise _unsupported()
    return "image/png", width, height


def _sniff_jpeg(data: bytes) -> tuple[MediaType, int, int]:
    position = 2
    while True:
        if position + 4 > len(data) or data[position] != 0xFF:
            raise _unsupported()
        marker = data[position + 1]
        if marker == 0xFF:  # fill byte before a marker
            position += 1
            continue
        if marker in _JPEG_STANDALONE:
            position += 2
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS before any frame header
            raise _unsupported()
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        if length < 2:
            raise _unsupported()
        if marker in _JPEG_SOF:
            if position + 9 > len(data) or length < 7:
                raise _unsupported()
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            if width <= 0 or height <= 0:
                raise _unsupported()
            return "image/jpeg", width, height
        position += 2 + length


def _sniff_webp(data: bytes) -> tuple[MediaType, int, int]:
    if len(data) < 16 or data[8:12] != b"WEBP":
        raise _unsupported()
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            raise _unsupported()
        raw_width, raw_height = struct.unpack("<HH", data[26:30])
        width, height = raw_width & 0x3FFF, raw_height & 0x3FFF
    elif chunk == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            raise _unsupported()
        (bits,) = struct.unpack("<I", data[21:25])
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8X":
        if len(data) < 30:
            raise _unsupported()
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    else:
        raise _unsupported()
    if width <= 0 or height <= 0:
        raise _unsupported()
    return "image/webp", width, height


def _sniff_gif(data: bytes) -> tuple[MediaType, int, int]:
    if len(data) < 10:
        raise _unsupported()
    width, height = struct.unpack("<HH", data[6:10])
    if width <= 0 or height <= 0:
        raise _unsupported()
    return "image/gif", width, height


def sniff(data: bytes) -> tuple[MediaType, int, int]:
    """Return (media type, width, height) from the header alone.

    PNG IHDR; JPEG SOF0/1/2 found by walking the marker segments; WebP
    ``VP8 ``/``VP8L``/``VP8X`` chunks; GIF logical screen descriptor. Pure
    ``struct``. Raises ``ValueError("unsupported_image")`` for anything else.
    """
    if data.startswith(_PNG_SIGNATURE):
        return _sniff_png(data)
    if data.startswith(b"\xff\xd8"):
        return _sniff_jpeg(data)
    if data.startswith(b"RIFF"):
        return _sniff_webp(data)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _sniff_gif(data)
    raise _unsupported()


def b64(image: ImageInput) -> str:
    return base64.b64encode(image.data).decode("ascii")


def data_url(image: ImageInput) -> str:
    return f"data:{image.media_type};base64,{b64(image)}"


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def encode_png_gray(rows: Sequence[bytes], width: int, height: int) -> bytes:
    """Encode 8-bit greyscale rows (one byte per pixel) as a PNG with stdlib zlib.

    Colour type 0, bit depth 8, filter type 0 on every scanline, one IDAT.
    """
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    if len(rows) != height or any(len(row) != width for row in rows):
        raise ValueError("PNG rows must match the declared dimensions")
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
