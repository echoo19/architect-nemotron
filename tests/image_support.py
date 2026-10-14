"""Hand-built image headers shared by tests: struct + zlib only, no imaging library.

``png_bytes`` is a complete, decodable greyscale PNG. The JPEG, WebP and GIF
builders produce headers that carry a valid dimension record (what ``sniff``
reads) without a decodable payload, which is all the adapters ever inspect.
"""

import struct
import zlib
from typing import Literal

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def png_bytes(width: int, height: int, *, shade: int = 0xFF) -> bytes:
    """A valid 8-bit greyscale PNG filled with one shade."""
    raw = b"".join(b"\x00" + bytes([shade]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def jpeg_bytes(
    width: int,
    height: int,
    *,
    progressive: bool = False,
    comment: bytes = b"",
    fill: bool = False,
    restart: bool = False,
) -> bytes:
    """SOI, APP0 (JFIF), optional COM, optional fill/RST markers, SOF0 or SOF2, EOI."""
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    out = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 2 + len(app0)) + app0
    if comment:
        out += b"\xff\xfe" + struct.pack(">H", 2 + len(comment)) + comment
    if fill:
        out += b"\xff"  # a fill byte before the next marker
    if restart:
        out += b"\xff\xd0"  # standalone RST0 marker, no length
    components = b"\x01\x22\x00" + b"\x02\x11\x01" + b"\x03\x11\x01"
    frame = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + components
    out += (b"\xff\xc2" if progressive else b"\xff\xc0") + struct.pack(">H", 2 + len(frame))
    out += frame
    return out + b"\xff\xd9"


WebpChunk = Literal["VP8 ", "VP8L", "VP8X"]


def webp_bytes(width: int, height: int, *, chunk: WebpChunk = "VP8 ") -> bytes:
    if chunk == "VP8 ":
        body = b"\x10\x02\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    elif chunk == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        body = b"\x2f" + struct.pack("<I", bits)
    else:
        body = b"\x00" + b"\x00\x00\x00"
        body += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    payload = chunk.encode("ascii") + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


def gif_bytes(width: int, height: int, *, version: bytes = b"GIF89a") -> bytes:
    return version + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b"\x3b"
