"""Build the CORD v2 receipt task data (text, four labels, splits, manifests).

Source: the Hugging Face dataset ``naver-clova-ix/cord-v2`` (CC BY 4.0), pinned to
one commit. Only the ``ground_truth`` column is fetched, by HTTP range requests on
that commit's parquet files, so no image byte is ever downloaded. Raw downloads are
cached outside the repository. Everything committed under ``examples/receipts`` is
derived from the cache by the fixed rules in this file:

    uv run python scripts/fetch_bench_data.py            # fetch (cached) and write
    uv run python scripts/fetch_bench_data.py --verify   # re-derive and compare hashes
    uv run python scripts/fetch_bench_data.py --verify --refetch   # from the source again

``--verify`` re-derives every output in memory (from the cache, fetching first when
the cache is missing) and compares it byte for byte, by sha256, with the files on
disk. It exits 1 on any difference. No model is ever called.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "examples" / "receipts"

DATASET = "naver-clova-ix/cord-v2"
REVISION = "7f0115a4b758a71d6473b8d085751692da2fef98"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET}"
LICENSE = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
COLUMN = "ground_truth"
USER_AGENT = "architect-fetch-bench-data/1"

# The parquet files of the pinned commit (Hub tree API at REVISION): name, size and
# the git-LFS sha256 of the whole file. Range reads check the size on every response.
SOURCE_FILES: Mapping[str, tuple[tuple[str, int, str], ...]] = {
    "train": (
        (
            "data/train-00000-of-00004-b4aaeceff1d90ecb.parquet",
            490224630,
            "da3994eee1bf9bd3c57f0d53a72c3a6812c8696c5ba26245987949ddf73483cc",
        ),
        (
            "data/train-00001-of-00004-7dbbe248962764c5.parquet",
            441418432,
            "cce4def16a0d6a6c75f80be712f7494c56c318a8829b712f5c62650155c9e58e",
        ),
        (
            "data/train-00002-of-00004-688fe1305a55e5cc.parquet",
            443802181,
            "591e2db8fe8b1d364b054f46e8c375b7f00e72578914ba46a573b6858162cab2",
        ),
        (
            "data/train-00003-of-00004-2d0cd200555ed7fd.parquet",
            455555434,
            "1ffd9de8d6fbcee7630fd4cdfedff05b9b7fabc0fae4fc17557c5fe7cf178748",
        ),
    ),
    "validation": (
        (
            "data/validation-00000-of-00001-cc3c5779fe22e8ca.parquet",
            242080800,
            "0d0f6dac11fdcc549de2746aa9f53136a3bc22a2a1aff2b0b847f7622ad60c15",
        ),
    ),
    "test": (
        (
            "data/test-00000-of-00001-9c204eb3f4e11791.parquet",
            234202795,
            "51c65f1788faff392abe2a0b55b023eb23e9be551c509138eaa3a832514224e7",
        ),
    ),
}
SPLIT_SIZES: Mapping[str, int] = {"train": 800, "validation": 100, "test": 100}


class DataError(RuntimeError):
    """The source, the cache or a derived file is not what the pinned rules expect."""


# -- HTTP ---------------------------------------------------------------------------------


def _resolve_url(path: str) -> str:
    return f"{DATASET_URL}/resolve/{REVISION}/{path}"


def _http_get(
    url: str,
    headers: Mapping[str, str] | None = None,
    accept: Callable[[int, Mapping[str, str]], None] | None = None,
) -> tuple[bytes, Mapping[str, str]]:
    """GET with a few retries on 429/5xx and network errors; no credentials are sent.

    ``accept`` sees the status and headers before any body byte is read and raises to
    refuse the response, so a server that ignores a Range header never gets to send a
    whole parquet file (and its images).
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    delay = 2.0
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                received = {key.lower(): value for key, value in response.headers.items()}
                if accept is not None:
                    accept(response.status, received)
                return response.read(), received
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise DataError(f"GET {url} failed: HTTP {exc.code}") from None
            retry = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry) if retry and retry.isdigit() else delay
        except (OSError, http.client.HTTPException) as exc:
            if attempt == 3:
                raise DataError(f"GET {url} failed: {exc}") from None
            wait = delay
        time.sleep(min(wait, 30.0))
        delay *= 2
    raise DataError(f"GET {url} failed")  # pragma: no cover - the loop always returns/raises


def _range_get(path: str, size: int, start: int, end: int) -> bytes:
    """Bytes [start, end) of a pinned file; the server must report the pinned size."""
    if not 0 <= start < end <= size:
        raise DataError(f"bad range {start}-{end} for {path}")

    def accept(status: int, headers: Mapping[str, str]) -> None:
        content_range = headers.get("content-range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
        expected = (start, end, size)
        if (
            status != 206
            or match is None
            or (int(match[1]), int(match[2]) + 1, int(match[3])) != expected
        ):
            raise DataError(f"{path}: no partial content for bytes {start}-{end - 1}")

    data, _headers = _http_get(_resolve_url(path), {"Range": f"bytes={start}-{end - 1}"}, accept)
    if len(data) != end - start:
        raise DataError(f"short read for {path}")
    return data


# -- Parquet (just enough to read one flat string column) ---------------------------------
# Thrift compact protocol, snappy, the RLE/bit-packed hybrid, PLAIN and dictionary pages.
# Only what the pinned files use is supported; anything else raises DataError.


class _Thrift:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    def _byte(self) -> int:
        if self.position >= len(self.data):
            raise DataError("truncated thrift data")
        value = self.data[self.position]
        self.position += 1
        return value

    def varint(self) -> int:
        shift = result = 0
        while True:
            byte = self._byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 70:
                raise DataError("varint too long")

    def zigzag(self) -> int:
        value = self.varint()
        return (value >> 1) ^ -(value & 1)

    def _value(self, kind: int) -> Any:
        if kind in (1, 2):
            return kind == 1
        if kind == 3:
            return struct.unpack("<b", bytes([self._byte()]))[0]
        if kind in (4, 5, 6):
            return self.zigzag()
        if kind == 7:
            end = self.position + 8
            if end > len(self.data):
                raise DataError("truncated thrift data")
            value = struct.unpack("<d", self.data[self.position : end])[0]
            self.position = end
            return value
        if kind == 8:
            length = self.varint()
            end = self.position + length
            if end > len(self.data):
                raise DataError("truncated thrift data")
            value = self.data[self.position : end]
            self.position = end
            return value
        if kind in (9, 10):
            header = self._byte()
            count, element = header >> 4, header & 0x0F
            if count == 15:
                count = self.varint()
            if element in (1, 2):
                return [self._byte() == 1 for _ in range(count)]
            return [self._value(element) for _ in range(count)]
        if kind == 12:
            return self.read_struct()
        raise DataError(f"unsupported thrift type {kind}")

    def read_struct(self) -> dict[int, Any]:
        fields: dict[int, Any] = {}
        last = 0
        while True:
            header = self._byte()
            if header == 0:
                return fields
            delta, kind = header >> 4, header & 0x0F
            field = last + delta if delta else self.zigzag()
            last = field
            fields[field] = self._value(kind)


def snappy_decompress(data: bytes) -> bytes:
    """Raw snappy (no framing), as parquet writes it."""
    position = 0

    def varint() -> int:
        nonlocal position
        shift = result = 0
        while True:
            if position >= len(data):
                raise DataError("truncated snappy data")
            byte = data[position]
            position += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    expected = varint()
    out = bytearray()
    while position < len(data):
        tag = data[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                length = int.from_bytes(data[position : position + extra], "little")
                position += extra
            length += 1
            if position + length > len(data):
                raise DataError("truncated snappy literal")
            out += data[position : position + length]
            position += length
            continue
        if kind == 1:
            length = 4 + ((tag >> 2) & 0x07)
            offset = ((tag >> 5) << 8) | data[position]
            position += 1
        else:
            width = 2 if kind == 2 else 4
            length = 1 + (tag >> 2)
            offset = int.from_bytes(data[position : position + width], "little")
            position += width
        if offset <= 0 or offset > len(out):
            raise DataError("bad snappy copy offset")
        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:
            for index in range(length):  # overlapping copy repeats the window
                out.append(out[start + index])
    if len(out) != expected:
        raise DataError("snappy length mismatch")
    return bytes(out)


def rle_hybrid(data: bytes, bit_width: int, count: int) -> list[int]:
    """Parquet's RLE/bit-packed hybrid: ``count`` values of ``bit_width`` bits."""
    values: list[int] = []
    reader = _Thrift(data)
    width_bytes = (bit_width + 7) // 8
    mask = (1 << bit_width) - 1
    while len(values) < count:
        header = reader.varint()
        if header & 1:
            groups = header >> 1
            nbytes = groups * bit_width
            chunk = data[reader.position : reader.position + nbytes]
            if len(chunk) != nbytes:
                raise DataError("truncated bit-packed run")
            reader.position += nbytes
            packed = int.from_bytes(chunk, "little")
            for index in range(groups * 8):
                values.append((packed >> (index * bit_width)) & mask if bit_width else 0)
        else:
            run = header >> 1
            chunk = data[reader.position : reader.position + width_bytes]
            if len(chunk) != width_bytes:
                raise DataError("truncated RLE run")
            reader.position += width_bytes
            values.extend([int.from_bytes(chunk, "little")] * run)
    return values[:count]


def _plain_byte_arrays(data: bytes, count: int) -> list[bytes]:
    values: list[bytes] = []
    position = 0
    for _ in range(count):
        if position + 4 > len(data):
            raise DataError("truncated PLAIN value")
        length = struct.unpack("<I", data[position : position + 4])[0]
        position += 4
        if position + length > len(data):
            raise DataError("truncated PLAIN value")
        values.append(data[position : position + length])
        position += length
    return values


_PLAIN, _PLAIN_DICTIONARY, _RLE, _RLE_DICTIONARY = 0, 2, 3, 8
_DATA_PAGE, _DICTIONARY_PAGE, _DATA_PAGE_V2 = 0, 2, 3
_BYTE_ARRAY = 6


def _decompress(codec: int, data: bytes) -> bytes:
    if codec == 0:
        return data
    if codec == 1:
        return snappy_decompress(data)
    if codec == 2:
        return zlib.decompress(data, wbits=47)  # gzip or zlib header
    raise DataError(f"unsupported parquet codec {codec}")


@dataclass(frozen=True)
class ColumnChunk:
    """Where one row group's chunk of the string column sits in the file."""

    start: int
    length: int
    codec: int
    num_values: int
    optional: bool


def footer_length(tail: bytes) -> int:
    if len(tail) != 8 or tail[4:] != b"PAR1":
        raise DataError("not a parquet file")
    return int(struct.unpack("<I", tail[:4])[0])


_MALFORMED = (KeyError, IndexError, TypeError, AttributeError, ValueError, struct.error, zlib.error)


def column_chunks(footer: bytes, column: str) -> tuple[int, list[ColumnChunk]]:
    """(num_rows, chunks) of a flat top-level BYTE_ARRAY column, from the file footer."""
    try:
        return _column_chunks(footer, column)
    except _MALFORMED as exc:
        raise DataError(f"malformed parquet footer ({exc!r})") from None


def _column_chunks(footer: bytes, column: str) -> tuple[int, list[ColumnChunk]]:
    metadata = _Thrift(footer).read_struct()
    schema = metadata.get(2) or []
    names = [entry.get(4, b"").decode("utf-8") for entry in schema]
    matches = [entry for entry in schema[1:] if entry.get(4, b"") == column.encode()]
    if len(matches) != 1 or matches[0].get(1) != _BYTE_ARRAY or matches[0].get(5):
        raise DataError(f"column {column!r} is not a flat string column ({names})")
    optional = matches[0].get(3, 0) == 1
    if matches[0].get(3, 0) not in (0, 1):
        raise DataError("repeated columns are not supported")
    chunks: list[ColumnChunk] = []
    for group in metadata.get(4) or []:
        found = [
            chunk[3]
            for chunk in group.get(1) or []
            if [part.decode("utf-8") for part in chunk[3].get(3, [])] == [column]
        ]
        if len(found) != 1:
            raise DataError(f"row group without column {column!r}")
        meta = found[0]
        data_offset = meta[9]
        dictionary_offset = meta.get(11)
        start = (
            dictionary_offset
            if dictionary_offset is not None and 0 < dictionary_offset < data_offset
            else data_offset
        )
        chunks.append(ColumnChunk(start, meta[7], meta[4], meta[5], optional))
    return int(metadata.get(3, 0)), chunks


def decode_chunk(data: bytes, chunk: ColumnChunk) -> list[str | None]:
    """Every value of one column chunk, in row order (None for a null)."""
    try:
        return _decode_chunk(data, chunk)
    except _MALFORMED as exc:
        raise DataError(f"malformed parquet column chunk ({exc!r})") from None


def _decode_chunk(data: bytes, chunk: ColumnChunk) -> list[str | None]:
    reader = _Thrift(data)
    dictionary: list[bytes] | None = None
    values: list[str | None] = []
    while len(values) < chunk.num_values:
        header = reader.read_struct()
        page_type, uncompressed_size, compressed_size = header.get(1), header.get(2), header[3]
        if not isinstance(compressed_size, int) or compressed_size < 0:
            raise DataError("bad parquet page header")
        body = data[reader.position : reader.position + compressed_size]
        if len(body) != compressed_size:
            raise DataError("truncated parquet page")
        reader.position += compressed_size
        if page_type == _DICTIONARY_PAGE:
            page = _decompress(chunk.codec, body)
            info = header[7]
            if info.get(2) not in (_PLAIN, _PLAIN_DICTIONARY) or len(page) != uncompressed_size:
                raise DataError("unsupported dictionary page")
            dictionary = _plain_byte_arrays(page, info[1])
            continue
        if page_type == _DATA_PAGE:
            info = header[5]
            page = _decompress(chunk.codec, body)
            if len(page) != uncompressed_size:
                raise DataError("parquet page length mismatch")
            count, encoding = info[1], info[2]
            position = 0
            if chunk.optional:
                if info.get(3) != _RLE:
                    raise DataError("unsupported definition level encoding")
                size = struct.unpack("<I", page[:4])[0]
                levels = rle_hybrid(page[4 : 4 + size], 1, count)
                position = 4 + size
            else:
                levels = [1] * count
            payload = page[position:]
        elif page_type == _DATA_PAGE_V2:
            info = header[8]
            count, encoding = info[1], info[4]
            levels_size, repetition_size = info[5], info[6]
            if repetition_size:
                raise DataError("repeated columns are not supported")
            levels = rle_hybrid(body[:levels_size], 1, count) if chunk.optional else [1] * count
            payload = body[levels_size:]
            if info.get(7, True):
                payload = _decompress(chunk.codec, payload)
        else:
            raise DataError(f"unsupported parquet page type {page_type}")
        present = sum(levels)
        if encoding == _PLAIN:
            raw = _plain_byte_arrays(payload, present)
        elif encoding in (_PLAIN_DICTIONARY, _RLE_DICTIONARY):
            if dictionary is None:
                raise DataError("dictionary-encoded page without a dictionary")
            width = payload[0] if payload else 0
            indexes = rle_hybrid(payload[1:], width, present) if present else []
            if any(index >= len(dictionary) for index in indexes):
                raise DataError("dictionary index out of range")
            raw = [dictionary[index] for index in indexes]
        else:
            raise DataError(f"unsupported parquet encoding {encoding}")
        stream = iter(raw)
        for level in levels:
            values.append(next(stream).decode("utf-8") if level else None)
    if len(values) != chunk.num_values:
        raise DataError("column chunk value count mismatch")
    return values


# -- Fetch and cache -----------------------------------------------------------------------


def default_cache() -> Path:
    override = os.environ.get("CORD_CACHE")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path("C:/t/cord-cache")
    return Path.home() / ".cache" / "architect" / "cord-cache"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _cache_dir(cache: Path) -> Path:
    resolved = cache.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise DataError("the raw cache must live outside the repository")
    return resolved / REVISION


def _footer_path(cache: Path, name: str) -> Path:
    return _cache_dir(cache) / f"{Path(name).name}.footer"


def _chunk_path(cache: Path, name: str, group: int) -> Path:
    return _cache_dir(cache) / f"{Path(name).name}.rg{group}.{COLUMN}"


def check_hub(fetch_json: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """The pinned commit's licence tag and parquet files, as the Hub API reports them."""
    get = fetch_json or (lambda url: json.loads(_http_get(url)[0]))
    info = get(f"https://huggingface.co/api/datasets/{DATASET}/revision/{REVISION}")
    if info.get("sha") != REVISION:
        raise DataError("the Hub did not resolve the pinned revision")
    licence = (info.get("cardData") or {}).get("license")
    if licence != "cc-by-4.0":
        raise DataError(f"unexpected licence {licence!r} at the pinned revision")
    tree = get(f"https://huggingface.co/api/datasets/{DATASET}/tree/{REVISION}/data")
    listed = {
        entry["path"]: (entry["size"], (entry.get("lfs") or {}).get("oid"))
        for entry in tree
        if entry.get("type") == "file"
    }
    pinned = {name: (size, oid) for files in SOURCE_FILES.values() for name, size, oid in files}
    if listed != pinned:
        raise DataError("the pinned revision lists different parquet files")
    return {"sha": info["sha"], "license": licence, "last_modified": info.get("lastModified")}


def fetch(cache: Path, *, log: Callable[[str], None] = print) -> None:
    """Fill the cache with each pinned file's footer and ground_truth column chunks."""
    check_hub()
    for split, files in SOURCE_FILES.items():
        for name, size, _oid in files:
            tail = _range_get(name, size, size - 8, size)
            length = footer_length(tail)
            footer = _range_get(name, size, size - 8 - length, size - 8)
            rows, chunks = column_chunks(footer, COLUMN)
            raw = []
            for chunk in chunks:
                if chunk.start + chunk.length > size - 8 - length:
                    raise DataError(f"column chunk outside the data area of {name}")
                data = _range_get(name, size, chunk.start, chunk.start + chunk.length)
                decode_chunk(data, chunk)  # fail now rather than at derive time
                raw.append(data)
            if sum(chunk.num_values for chunk in chunks) != rows:
                raise DataError(f"{name}: column values do not match the row count")
            for group, data in enumerate(raw):
                _write_bytes(_chunk_path(cache, name, group), data)
            _write_bytes(_footer_path(cache, name), footer)
            fetched = sum(len(data) for data in raw) + length + 8
            log(f"fetched {split} {Path(name).name}: {rows} rows, {fetched:,} of {size:,} bytes")


def cache_complete(cache: Path) -> bool:
    for files in SOURCE_FILES.values():
        for name, _size, _oid in files:
            footer = _footer_path(cache, name)
            if not footer.is_file():
                return False
            try:
                _rows, chunks = column_chunks(footer.read_bytes(), COLUMN)
            except DataError:
                return False
            if not all(_chunk_path(cache, name, k).is_file() for k in range(len(chunks))):
                return False
    return True


@dataclass(frozen=True)
class SourceRow:
    split: str
    index: int  # row position within the split, in file order
    ground_truth: str


def read_cache(cache: Path) -> tuple[dict[str, list[SourceRow]], dict[str, Any]]:
    """Every split's rows from the cached raw bytes, and the hashes of those bytes."""
    try:
        return _read_cache(cache)
    except OSError as exc:
        raise DataError(f"unreadable cache at {cache} ({exc})") from None


def _read_cache(cache: Path) -> tuple[dict[str, list[SourceRow]], dict[str, Any]]:
    splits: dict[str, list[SourceRow]] = {}
    files_record: list[dict[str, Any]] = []
    for split, files in SOURCE_FILES.items():
        rows: list[SourceRow] = []
        for name, size, oid in files:
            footer = _footer_path(cache, name).read_bytes()
            count, chunks = column_chunks(footer, COLUMN)
            chunk_hashes = []
            values: list[str | None] = []
            for group, chunk in enumerate(chunks):
                data = _chunk_path(cache, name, group).read_bytes()
                if len(data) != chunk.length:
                    raise DataError(f"cached chunk of {name} has the wrong length")
                chunk_hashes.append(_sha256(data))
                values.extend(decode_chunk(data, chunk))
            if len(values) != count:
                raise DataError(f"{name}: decoded {len(values)} of {count} rows")
            for value in values:
                if value is None:
                    raise DataError(f"{name}: a row has no ground_truth")
                rows.append(SourceRow(split, len(rows), value))
            files_record.append(
                {
                    "path": name,
                    "split": split,
                    "size": size,
                    "lfs_sha256": oid,
                    "rows": count,
                    "footer_sha256": _sha256(footer),
                    "column_chunk_sha256": chunk_hashes,
                }
            )
        if len(rows) != SPLIT_SIZES[split]:
            raise DataError(f"{split}: {len(rows)} rows, expected {SPLIT_SIZES[split]}")
        splits[split] = rows
    return splits, {"files": files_record}


# -- The rules -----------------------------------------------------------------------------

RULE_VERSION = "receipts-v2"
CASE_PREFIX = "cord"
# Train receipts, in sha256-of-text order, fill these in turn; the delivery set is the
# rest of train plus every validation and test receipt (section 4 of the hackathon plan).
# The owner cut rehearsal from 150 to 100 on 2026-10-03 so that delivery grows.
SPLITS: tuple[tuple[str, int], ...] = (("pilot", 150), ("rehearsal", 100), ("evidence", 200))
FLOORS = ("rehearsal", "evidence")
SPLIT_NAMES = (*(name for name, _ in SPLITS), "delivery")
# A duplicate group keeps one case: the official held-out copy first, then the lowest index.
SOURCE_PRIORITY = {"test": 0, "validation": 1, "train": 2}
LABEL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("total", "total", "total_price"),
    ("subtotal", "sub_total", "subtotal_price"),
    ("tax", "sub_total", "tax_price"),
)
LABEL_KEYS = ("total", "subtotal", "tax", "item_count")

# Quoted word for word in the spec and in every plain prompt (sha256 in every manifest).
# receipts-v2 states the conventions CORD's labels follow (examples/receipts/AUDIT.md,
# section 5.1); receipts whose labels follow the other side are in EXCLUDED.
PROMPT_RULES = "\n".join(
    (
        "Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.",
        "- total: the total amount printed on the receipt, including any tax and service, on"
        " a line such as TOTAL, GRAND TOTAL, TL or PAID. A line that names a tender (cash,"
        " card, e-wallet, voucher), a payment discount or change is never the total. If no"
        " total is printed and nothing after the subtotal changes the amount (only tender and"
        " change lines, or a 0% discount), the subtotal is the total and subtotal is null.",
        "- subtotal: the first amount printed after the items that sums them, apart from the"
        " total, whatever the receipt calls it: SUBTOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET"
        " TOTAL, the amount on an ITEMS line, a TOTAL line that a later GRAND TOTAL follows,"
        " or a bare amount under the items. A sum for one group of items (FOOD, BEVERAGE) is"
        " not the subtotal.",
        "- tax: the tax amount printed on the receipt (TAX, VAT, PPN, PB1, PAJAK), not its"
        " rate, its base (DPP) or a service charge.",
        "- item_count: the number of items listed on the receipt. Count each listed item"
        " once, whatever its quantity; the same item on two separate item lines counts twice."
        " Bags and charges listed as items (plastic bag, take-away charge) count, even at"
        " price 0. Lines that belong to an item (options, toppings, extras, notes, package"
        " contents) are part of that item, even with their own quantity and price. Discount"
        " lines are not items. A void line that cancels an item is not an item; the cancelled"
        " item still counts.",
        "- Amounts are integers: take the amount itself, without its label or rate, and delete"
        ' every character that is not a digit 0-9. For example "75,000" is 75000, "Rp 12.500"'
        ' is 12500, "-2.727" is 2727, "226,500.00" is 22650000 and "PB1 10% 4,818" is 4818.',
        "- An amount the receipt does not print is null; never compute it from other"
        ' amounts. A printed value without any digit, such as "-", is null.',
        "- If the receipt prints the same field more than once, use the first (topmost) one.",
    )
)

# Receipts left out by hand (examples/receipts/AUDIT.md, section 5.2), as source rows.
# A duplicate group with a listed member is dropped whole, like a label conflict; a group
# whose copies disagree on a label is recorded as label_conflict, with the note kept.
EXCLUSION_REASONS: Mapping[str, str] = {
    "label_wrong": "a label contradicts the printed text",
    "rule_conflict": "a label follows a CORD habit that the prompt rules settle the other way",
    "garbled_text": "the rebuilt text pairs amounts with the wrong lines, because CORD put"
    " them in the row_id of a neighbouring line",
}
REASON_MEANINGS: Mapping[str, str] = {
    "duplicate": "a copy of the kept case",
    "label_conflict": "copies of one receipt carry different labels, so it has no single answer",
    **EXCLUSION_REASONS,
}
_LONE_SUBTOTAL = (
    'the lone "Sub Total", with only tender and change lines after it, is labelled the'
    " subtotal with no total; the rules make it the total"
)
EXCLUDED: tuple[tuple[str, str, str], ...] = (
    ("test/9", "rule_conflict", _LONE_SUBTOTAL),
    ("test/60", "rule_conflict", 'the topping line "Topping Milo Sparkle" counts as an item'),
    ("train/35", "rule_conflict", _LONE_SUBTOTAL),
    ("train/52", "rule_conflict", _LONE_SUBTOTAL),
    (
        "train/191",
        "label_wrong",
        'the tax label joins the DPP amounts "30.273" and "200"; the receipt prints "PPN= 3.027"',
    ),
    ("train/211", "rule_conflict", 'the extra "1 Extra Jelly Lychee" counts as an item'),
    (
        "train/271",
        "label_wrong",
        'the subtotal label is the "3 Total 36.000" line; no subtotal is printed apart from'
        " the total",
    ),
    (
        "train/290",
        "label_wrong",
        'tax is null, but the receipt prints "Pajak Resto 9,818" (annotated as a discount)',
    ),
    (
        "train/293",
        "garbled_text",
        "three item prices sit in the row_id of the next item, so the text pairs them with"
        " the wrong items",
    ),
    (
        "train/308",
        "rule_conflict",
        'the extras "Extra Jelly Lychee" and "Extra Ice Cream" count as items',
    ),
    (
        "train/333",
        "label_wrong",
        'the subtotal label is the item line "TMP KpK 54,000"; no subtotal is printed',
    ),
    ("train/357", "rule_conflict", 'the extra "1 Extra Whipping Cream" counts as an item'),
    ("train/398", "rule_conflict", 'the option "CHANGE TO CHICKEN BOX" counts as an item'),
    ("train/421", "label_wrong", 'the tax label keeps the rate: "Pb1 10% 4,818" became 104818'),
    (
        "train/425",
        "garbled_text",
        'the amounts sit one row_id above their labels, so the text reads "VAT 10% 135,003"',
    ),
    ("train/435", "label_wrong", 'the tax label keeps the rate: "Pb1 10% 7,000" became 1107000'),
    ("train/460", "rule_conflict", _LONE_SUBTOTAL),
    (
        "train/490",
        "label_wrong",
        'total is null, but the receipt prints "Total Rp. 250.000" (annotated as the item price)',
    ),
    ("train/495", "label_wrong", 'the tax label is the rate "10.000%"; the amount is "546"'),
    ("train/500", "label_wrong", 'total is null, but the receipt prints "GRAND TOTAL 70,000"'),
    (
        "train/503",
        "label_wrong",
        'the merchandise subtotal "MDSE ST RP36362" counts as an item',
    ),
    (
        "train/507",
        "rule_conflict",
        'total is null, but the only amount line, "Jml Rupiah Rp. 217,685", is the total',
    ),
    (
        "train/555",
        "rule_conflict",
        'the upgrade lines "1 UP Drink" and "1 UP Orange" count as items',
    ),
    (
        "train/568",
        "garbled_text",
        "each item price sits in the row_id of the line above its item",
    ),
    ("train/572", "rule_conflict", _LONE_SUBTOTAL),
    (
        "train/592",
        "label_wrong",
        'tax is null, but the receipt prints "PB 1 4,955" (annotated as a discount)',
    ),
    (
        "train/593",
        "rule_conflict",
        '"TL 46,000" is labelled the subtotal above "TOTAL 46,000"; the rules make TL a total',
    ),
    (
        "train/607",
        "rule_conflict",
        'the drink "Milo 1 0" is labelled part of the steak, but the receipt prints "ITEMS: 2"',
    ),
    (
        "train/670",
        "label_wrong",
        'total is null, but the receipt prints "Total Rp. 200.000" (annotated as the item price)',
    ),
    (
        "train/676",
        "label_wrong",
        'the subtotal label is "GT 22,000", which reads as the item line under "1 22,000"',
    ),
    ("train/683", "rule_conflict", _LONE_SUBTOTAL),
    ("train/717", "rule_conflict", 'the extra "1 Extra Jelly Lychee" counts as an item'),
    (
        "train/722",
        "rule_conflict",
        "the bag and knife lines are labelled part of the cake, while the free candles count"
        " as an item",
    ),
    ("train/739", "rule_conflict", 'the extra "2 Extra Jelly Lychee" counts as an item'),
    ("validation/21", "label_wrong", 'the line "Discount BCA 15%" counts as an item'),
    ("validation/29", "rule_conflict", _LONE_SUBTOTAL),
    (
        "validation/53",
        "rule_conflict",
        'the bag line "*Plastik kcl" is labelled part of the pastry',
    ),
)

# Copies of one purchase that neither content key joins, each confirmed by reading both
# texts: they differ only by line breaks, a name, or a line one copy lacks, and every
# amount both print agrees, cash and change included. Merged like any duplicate group.
NEAR_DUPLICATES: tuple[tuple[str, str, str], ...] = (
    ("test/44", "train/662", 'the item name, "Combo 1" or "Combo 2"'),
    ("test/84", "validation/14", "the order of the two items"),
    ("test/96", "validation/4", 'the option, "Tidak Pedas" or "Sedang", and the row breaks'),
    ("train/36", "train/362", "line wraps, and one copy prints the card and change lines"),
    ("train/56", "validation/65", 'one copy prints "DPP 49.636"'),
    ("train/66", "train/691", 'one copy prints "DPP 52.636"'),
    ("train/147", "train/692", 'one copy prints the option "*Gula Murni 100%"'),
    ("train/159", "train/593", 'one copy prints "TL 46,000"; the subtotal labels differ'),
    ("train/197", "train/215", 'the payment label, "Cash" or "Apply Amount"'),
    ("train/212", "train/227", 'the item code, "2005 CHEESE JOHN" or "2005-CHEESE JOHN"'),
    ("train/276", "train/732", 'line breaks, and one copy prints "TAKEOUT(1):"'),
    ("train/330", "train/348", 'one copy prints "Sales included PB1"'),
    ("train/390", "validation/47", 'the option, "Pedas" or "Sedang", and the row breaks'),
    ("train/443", "train/675", 'the spelling "Yalk" or "Yolk", and one option line'),
    ("train/474", "train/753", 'one copy prints "Incl. 10% 2,955"; the tax labels differ'),
    (
        "train/530",
        "validation/77",
        'one copy prints "Total Tax Included: Rp 5272.73"; the tax labels differ',
    ),
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"receipt": {"type": "string"}},
    "required": ["receipt"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "total": {"type": ["integer", "null"]},
        "subtotal": {"type": ["integer", "null"]},
        "tax": {"type": ["integer", "null"]},
        "item_count": {"type": "integer"},
    },
    "required": list(LABEL_KEYS),
    "additionalProperties": False,
}


# -- Receipt text and labels ---------------------------------------------------------------


def _coordinate(value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise DataError("quad coordinates must be finite numbers")
    return Fraction(value)


@dataclass(frozen=True)
class Word:
    """One annotated word: its place in annotation order, row_id, text and quad geometry."""

    order: int  # position among every word of valid_line, empty ones included
    row_id: int
    text: str
    x: Fraction  # quad centre: the mean of the four corners
    y: Fraction
    left: Fraction  # the quad's bounding box
    right: Fraction
    top: Fraction
    bottom: Fraction


def receipt_words(valid_line: Sequence[Mapping[str, Any]]) -> list[Word]:
    """Every word of every ``valid_line`` entry (key words included), in annotation order.

    A word's text has its whitespace runs collapsed to one space; a word left empty is
    dropped but still takes its place in the order.
    """
    words: list[Word] = []
    order = 0
    for line in valid_line:
        for word in line["words"]:
            position = order
            order += 1
            text = " ".join(str(word["text"]).split())
            if not text:
                continue
            row_id = word["row_id"]
            if isinstance(row_id, bool) or not isinstance(row_id, int):
                raise DataError("row_id must be an integer")
            quad = word["quad"]
            xs = [_coordinate(quad[f"x{corner}"]) for corner in (1, 2, 3, 4)]
            ys = [_coordinate(quad[f"y{corner}"]) for corner in (1, 2, 3, 4)]
            centre_x = sum(xs, Fraction(0)) / 4
            centre_y = sum(ys, Fraction(0)) / 4
            words.append(
                Word(position, row_id, text, centre_x, centre_y, min(xs), max(xs), min(ys), max(ys))
            )
    return words


def stacked(a: Word, b: Word) -> bool:
    """Two words printed one above the other: their boxes overlap horizontally by more
    than 30% of the narrower box, and their centres lie more than half the shorter box's
    height apart vertically."""
    overlap = min(a.right, b.right) - max(a.left, b.left)
    narrower = min(a.right - a.left, b.right - b.left)
    if overlap * 10 <= narrower * 3:
        return False
    return abs(a.y - b.y) * 2 > min(a.bottom - a.top, b.bottom - b.top)


def _median(values: Sequence[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def receipt_tilt(rows: Mapping[int, Sequence[Word]], skip: Iterable[int] = ()) -> Fraction:
    """The photo's slope (dy/dx): the median, over rows outside ``skip`` whose leftmost and
    rightmost word centres differ in x, of the slope from the one to the other; 0 when no
    row qualifies."""
    skipped = set(skip)
    slopes = []
    for row_id, words in rows.items():
        if row_id in skipped:
            continue
        first = min(words, key=lambda word: (word.x, word.order))
        last = max(words, key=lambda word: (word.x, word.order))
        if last.x != first.x:
            slopes.append((last.y - first.y) / (last.x - first.x))
    return _median(slopes) if slopes else Fraction(0)


def _largest_stacks(words: Sequence[Word]) -> list[tuple[int, ...]]:
    """The largest sets of words (by index) in which every two words are stacked."""
    linked = [
        {other for other in range(len(words)) if other != index and stacked(word, words[other])}
        for index, word in enumerate(words)
    ]
    found: list[tuple[int, ...]] = []

    def extend(chosen: tuple[int, ...], candidates: set[int], done: set[int]) -> None:
        if not candidates and not done:
            found.append(chosen)
            return
        for index in sorted(candidates):
            extend((*chosen, index), candidates & linked[index], done & linked[index])
            candidates = candidates - {index}
            done = done | {index}

    extend((), set(range(len(words))), set())
    size = max(len(stack) for stack in found)
    return [stack for stack in found if len(stack) == size]


def split_row(words: Sequence[Word], tilt: Fraction) -> list[list[Word]]:
    """The printed lines of one row whose words are stacked, top to bottom.

    A word's level is its centre y minus the tilt times its centre x. The row holds as
    many lines as its largest stack (a set of words every two of which are stacked). The
    words of every largest stack go, by level from the top, to lines 1 to n. Every other
    word, in annotation order, joins the line whose stack words' mean level is nearest
    its own (the upper line on a tie), never a line holding a word it is stacked with.
    """
    level = [word.y - tilt * word.x for word in words]
    line_of: dict[int, int] = {}
    for stack in _largest_stacks(words):
        ordered = sorted(
            stack, key=lambda index: (level[index], words[index].x, words[index].order)
        )
        for rank, index in enumerate(ordered):
            if line_of.setdefault(index, rank) != rank:
                raise DataError(f"row {words[index].row_id} splits into lines two ways")
    count = max(line_of.values()) + 1
    anchors = []
    for rank in range(count):
        members = [level[index] for index, line in line_of.items() if line == rank]
        anchors.append(sum(members, Fraction(0)) / len(members))
    for index, word in enumerate(words):
        if index in line_of:
            continue
        taken = {line_of[other] for other in line_of if stacked(word, words[other])}
        choices = [(abs(level[index] - anchors[rank]), rank) for rank in range(count)]
        open_choices = [choice for choice in choices if choice[1] not in taken]
        if not open_choices:
            raise DataError(f"row {word.row_id}: a word is stacked with every line")
        line_of[index] = min(open_choices)[1]
    return [
        [word for index, word in enumerate(words) if line_of[index] == rank]
        for rank in range(count)
    ]


def receipt_lines(valid_line: Sequence[Mapping[str, Any]]) -> list[tuple[int, list[Word]]]:
    """(row_id, words) per printed line in reading order: top to bottom, left to right.

    Words are grouped by ``row_id``. A row holding two stacked words spans several
    printed lines and is split by ``split_row``, along the tilt of the photo measured on
    the other rows. Lines are ordered by the mean centre y of their words, then by their
    leftmost centre x, then by ``row_id``, then top to bottom within a split row; words
    in a line by centre x, then by annotation order. Positions are exact fractions, so no
    tie depends on rounding.
    """
    rows: dict[int, list[Word]] = {}
    for word in receipt_words(valid_line):
        rows.setdefault(word.row_id, []).append(word)
    split = {
        row_id
        for row_id, words in rows.items()
        if any(stacked(a, b) for position, a in enumerate(words) for b in words[position + 1 :])
    }
    tilt = receipt_tilt(rows, skip=split) if split else Fraction(0)
    placed: list[tuple[tuple[Fraction, Fraction, int, int], int, list[Word]]] = []
    for row_id, words in rows.items():
        parts = split_row(words, tilt) if row_id in split else [words]
        for rank, part in enumerate(parts):
            mean_y = sum((word.y for word in part), Fraction(0)) / len(part)
            key = (mean_y, min(word.x for word in part), row_id, rank)
            placed.append((key, row_id, sorted(part, key=lambda word: (word.x, word.order))))
    placed.sort(key=lambda item: item[0])
    return [(row_id, part) for _key, row_id, part in placed]


def receipt_rows(valid_line: Sequence[Mapping[str, Any]]) -> list[tuple[int, list[str]]]:
    """(row_id, word texts) per printed line, in reading order (see ``receipt_lines``)."""
    return [(row_id, [word.text for word in words]) for row_id, words in receipt_lines(valid_line)]


def rebuild_text(valid_line: Sequence[Mapping[str, Any]]) -> str:
    """Words joined by one space, lines by one newline, no trailing newline."""
    return "\n".join(" ".join(words) for _row_id, words in receipt_rows(valid_line))


def amount(value: str) -> int | None:
    """Delete every character that is not an ASCII digit; no digit at all means None."""
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def field_values(parse: Mapping[str, Any], section: str, key: str) -> list[str]:
    """Every annotation of ``parse[section][key]``, in annotation order.

    A section may be one object or a list of objects, and a field may be one string or
    a list of strings (it was annotated more than once); both are flattened in order.
    """
    node = parse.get(section)
    groups = node if isinstance(node, list) else [node] if isinstance(node, dict) else []
    values: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        value = group.get(key)
        items = value if isinstance(value, list) else [value] if value is not None else []
        values.extend(item for item in items if isinstance(item, str))
    return values


def first_amount(values: Iterable[str]) -> int | None:
    """The first annotation that holds a digit; None when none does (not printed)."""
    for value in values:
        number = amount(value)
        if number is not None:
            return number
    return None


def item_count(parse: Mapping[str, Any]) -> int:
    """Entries of ``gt_parse.menu``: a list counts its entries, a single object is 1."""
    menu = parse.get("menu")
    if isinstance(menu, list):
        return len(menu)
    return 1 if isinstance(menu, dict) else 0


def receipt_labels(parse: Mapping[str, Any]) -> dict[str, int | None]:
    labels: dict[str, int | None] = {
        name: first_amount(field_values(parse, section, key)) for name, section, key in LABEL_FIELDS
    }
    labels["item_count"] = item_count(parse)
    return labels


def _printings(
    valid_line: Sequence[Mapping[str, Any]], category: str, rank: Mapping[int, int]
) -> list[tuple[str, int]]:
    """(value, topmost line rank) of each annotated printing of one category, in order.

    gt_parse holds each printing as the non-key words of its ``valid_line`` entry joined
    by spaces; an entry with no non-key word is not a printing. ``rank`` maps a word's
    annotation order to the position of its printed line.
    """
    found: list[tuple[str, int]] = []
    order = 0
    for line in valid_line:
        numbered = list(enumerate(line["words"], start=order))
        order += len(numbered)
        if line.get("category") != category:
            continue
        words = [(position, word) for position, word in numbered if not word.get("is_key")]
        value = " ".join(str(word["text"]) for _position, word in words)
        if value:
            found.append((value, min(rank.get(position, 1 << 30) for position, _word in words)))
    return found


def _quantity(entry: Mapping[str, Any]) -> float:
    """The printed quantity of a menu entry (its first number), or 1 when there is none."""
    count = entry.get("cnt")
    parts = count if isinstance(count, list) else [count]
    text = " ".join(part for part in parts if isinstance(part, str))
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(match[0].replace(",", ".")) if match else 1.0


_TOTAL_WORD = re.compile(r"total", re.IGNORECASE)
_SUB_WORD = re.compile(r"sub", re.IGNORECASE)


def _says_total(line: str) -> bool:
    """A line naming a total (not a subtotal) beside an amount of four or more digits."""
    amounts = (token for token in line.split() if len(re.sub(r"[^0-9]", "", token)) >= 4)
    return bool(_TOTAL_WORD.search(line)) and not _SUB_WORD.search(line) and any(amounts)


def receipt_flags(
    data: Mapping[str, Any], lines: Sequence[tuple[int, Sequence[Word]]]
) -> list[str]:
    """Diagnostic flags (meanings in RULES.md). They never change a label.

    ``lines`` is the receipt's ``receipt_lines``.
    """
    parse, valid_line = data["gt_parse"], data["valid_line"]
    rank = {
        word.order: position for position, (_row_id, words) in enumerate(lines) for word in words
    }
    flags: list[str] = []
    for name, section, key in LABEL_FIELDS:
        values = field_values(parse, section, key)
        printed = [value for value in values if amount(value) is not None]
        if not values:
            continue
        if not printed:
            flags.append(f"no_digit:{name}")
            continue
        chosen = printed[0]
        if len(values) > 1:
            numbers = {amount(value) for value in printed}
            kind = "multi_value_disagree" if len(numbers) > 1 else "multi_value"
            flags.append(f"{kind}:{name}")
            printings = _printings(valid_line, f"{section}.{key}", rank)
            if [value for value, _row in printings] != values:
                flags.append(f"printing_mismatch:{name}")
            else:
                tops = [row for value, row in printings if amount(value) is not None]
                if tops and tops[0] != min(tops):
                    flags.append(f"first_not_topmost:{name}")
        if sum(1 for token in chosen.split() if amount(token) is not None) > 1:
            flags.append(f"multi_number:{name}")
        if re.search(r"\d[.,]\d{1,2}\s*$", chosen):
            flags.append(f"decimal:{name}")
        if "-" in chosen or "(" in chosen:
            flags.append(f"sign:{name}")
    if not field_values(parse, "total", "total_price"):
        flags.append("total_missing")
        if any(_says_total(" ".join(word.text for word in words)) for _row_id, words in lines):
            flags.append("total_missing_total_word")
    menu = parse.get("menu")
    entries = menu if isinstance(menu, list) else [menu] if isinstance(menu, dict) else []
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if any("sub" in entry for entry in entries):
        flags.append("sub_items")
    if any("nm" not in entry for entry in entries):
        flags.append("menu_entry_without_name")
    if any(_quantity(entry) > 1 for entry in entries):
        flags.append("quantity_over_1")
    if parse.get("void_menu"):
        flags.append("void_menu")
    if len({row_id for row_id, _words in lines}) < len(lines):
        flags.append("row_split")
    return flags


@dataclass(frozen=True)
class Receipt:
    split: str
    index: int
    ground_truth_sha256: str
    text: str
    labels: Mapping[str, int | None]
    flags: tuple[str, ...]

    @property
    def case_id(self) -> str:
        return f"{CASE_PREFIX}-{self.split}-{self.index:04d}"

    @property
    def source(self) -> str:
        return f"{self.split}/{self.index}"

    @property
    def text_sha256(self) -> str:
        return _sha256(self.text.encode("utf-8"))

    def case(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "task": {"receipt": self.text},
            "expected": {key: self.labels[key] for key in LABEL_KEYS},
        }


def parse_receipt(row: SourceRow) -> Receipt:
    try:
        data = json.loads(row.ground_truth)
        lines = receipt_lines(data["valid_line"])
        labels = receipt_labels(data["gt_parse"])
        flags = receipt_flags(data, lines)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError(f"{row.split}/{row.index}: unexpected ground_truth ({exc!r})") from None
    text = "\n".join(" ".join(word.text for word in words) for _row_id, words in lines)
    if not text:
        raise DataError(f"{row.split}/{row.index}: no text")
    digest = _sha256(row.ground_truth.encode("utf-8"))
    return Receipt(row.split, row.index, digest, text, labels, tuple(flags))


# -- Duplicates and splits -----------------------------------------------------------------


def content_keys(text: str) -> tuple[str, ...]:
    """Two receipts are one content when they share either key.

    ``words``: the multiset of lowercase letter/digit runs, so case, punctuation, spacing
    and line breaks are ignored (the same receipt annotated twice). ``digits``: every
    digit in reading order, so receipts printing the same amounts in the same order are
    one content even when an item name differs (the four labels are read from those
    amounts).
    """
    words = " ".join(sorted(re.findall(r"[a-z0-9]+", text.lower())))
    digits = re.sub(r"[^0-9]", "", text)
    return (f"words:{words}", *((f"digits:{digits}",) if digits else ()))


def _preference(receipt: Receipt) -> tuple[int, int]:
    return SOURCE_PRIORITY[receipt.split], receipt.index


def duplicate_groups(
    receipts: Sequence[Receipt], links: Iterable[tuple[str, str]] = ()
) -> list[list[Receipt]]:
    """Receipts joined by any shared content key or by a link between two sources
    (union-find). Each group is sorted by preference (test, validation, train; then
    index); groups by their first member. A link must join two different contents."""
    parent = list(range(len(receipts)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    first: dict[str, int] = {}
    for position, receipt in enumerate(receipts):
        for key in content_keys(receipt.text):
            if key in first:
                parent[find(position)] = find(first[key])
            else:
                first[key] = position
    where = {receipt.source: position for position, receipt in enumerate(receipts)}
    for a, b in links:
        if a not in where or b not in where:
            raise DataError(f"near duplicate {a} / {b}: no such source row")
        if find(where[a]) == find(where[b]):
            raise DataError(f"near duplicate {a} / {b}: already one content")
        parent[find(where[b])] = find(where[a])
    groups: dict[int, list[Receipt]] = {}
    for position, receipt in enumerate(receipts):
        groups.setdefault(find(position), []).append(receipt)
    ordered = [sorted(group, key=_preference) for group in groups.values()]
    return sorted(ordered, key=lambda group: _preference(group[0]))


@dataclass(frozen=True)
class Derivation:
    receipts: tuple[Receipt, ...]
    groups: tuple[tuple[Receipt, ...], ...]
    splits: Mapping[str, tuple[Receipt, ...]]  # each sorted by case_id
    excluded: tuple[dict[str, Any], ...]  # in source order


def _exclusions(receipts: Sequence[Receipt]) -> dict[str, tuple[str, str]]:
    """EXCLUDED as {source: (reason, note)}, checked against the parsed receipts."""
    sources = {receipt.source for receipt in receipts}
    listed: dict[str, tuple[str, str]] = {}
    for source, reason, note in EXCLUDED:
        if source not in sources:
            raise DataError(f"excluded {source}: no such source row")
        if reason not in EXCLUSION_REASONS:
            raise DataError(f"excluded {source}: unknown reason {reason!r}")
        if source in listed:
            raise DataError(f"excluded {source}: listed twice")
        listed[source] = (reason, note)
    return listed


def derive(rows: Mapping[str, Sequence[SourceRow]]) -> Derivation:
    """Parse every receipt, merge duplicates, drop label conflicts and the receipts left
    out by hand, assign the splits."""
    receipts = tuple(parse_receipt(row) for split in SOURCE_FILES for row in rows[split])
    groups = duplicate_groups(receipts, [(a, b) for a, b, _differs in NEAR_DUPLICATES])
    listed = _exclusions(receipts)
    excluded: list[dict[str, Any]] = []
    train: list[Receipt] = []
    held: list[Receipt] = []
    for group in groups:
        keep = group[0]
        if len({tuple(member.labels[key] for key in LABEL_KEYS) for member in group}) > 1:
            sources = [member.source for member in group]
            for member in group:
                entry: dict[str, Any] = {
                    "source": member.source,
                    "reason": "label_conflict",
                    "group": sources,
                }
                if member.source in listed:
                    entry["note"] = listed[member.source][1]
                excluded.append(entry)
            continue
        marked = [member for member in group if member.source in listed]
        if marked:
            for member in group:
                reason, note = listed.get(member.source) or (
                    listed[marked[0].source][0],
                    f"a copy of {marked[0].source}",
                )
                excluded.append({"source": member.source, "reason": reason, "note": note})
            continue
        excluded.extend(
            {"source": member.source, "reason": "duplicate", "kept": keep.case_id}
            for member in group[1:]
        )
        (train if all(member.split == "train" for member in group) else held).append(keep)
    train.sort(key=lambda receipt: (receipt.text_sha256, receipt.case_id))
    needed = sum(size for _name, size in SPLITS)
    if len(train) < needed:
        raise DataError(f"only {len(train)} distinct train receipts; the splits need {needed}")
    splits: dict[str, tuple[Receipt, ...]] = {}
    start = 0
    for name, size in SPLITS:
        splits[name] = tuple(sorted(train[start : start + size], key=lambda r: r.case_id))
        start += size
    splits["delivery"] = tuple(sorted([*train[start:], *held], key=lambda r: r.case_id))
    order = {receipt.source: position for position, receipt in enumerate(receipts)}
    excluded.sort(key=lambda entry: order[entry["source"]])
    return Derivation(receipts, tuple(tuple(group) for group in groups), splits, tuple(excluded))


def partition(case_ids: Iterable[str]) -> dict[str, str]:
    """Architect's floor rule (architect/floors/base.py): sorted ids, even positions
    tune, odd positions are held out."""
    return {
        case_id: "tuning" if position % 2 == 0 else "held_out"
        for position, case_id in enumerate(sorted(case_ids))
    }


# -- Statistics ----------------------------------------------------------------------------

# Flags that point at a label the rules may not reproduce from the text (hand-check first).
SUSPECT_FLAGS = (
    "multi_value_disagree",
    "multi_number",
    "first_not_topmost",
    "printing_mismatch",
    "total_missing_total_word",
    "menu_entry_without_name",
)


def _distribution(values: Sequence[int]) -> dict[str, int | float]:
    """Nearest-rank percentiles (exact integer arithmetic) and the mean to one decimal."""
    ordered = sorted(values)
    if not ordered:
        return {}
    count = len(ordered)

    def rank(percent: int) -> int:
        return ordered[max(0, (percent * count + 99) // 100 - 1)]

    return {
        "min": ordered[0],
        "p10": rank(10),
        "p25": rank(25),
        "median": rank(50),
        "p75": rank(75),
        "p90": rank(90),
        "max": ordered[-1],
        "mean": round(sum(ordered) / count, 1),
    }


def _split_stats(cases: Sequence[Receipt]) -> dict[str, Any]:
    count = len(cases)
    nulls = {
        name: sum(1 for case in cases if case.labels[name] is None) for name, *_ in LABEL_FIELDS
    }
    items = Counter(case.labels["item_count"] or 0 for case in cases)  # never None
    flags = Counter(flag for case in cases for flag in case.flags)
    totals = [case.labels["total"] for case in cases]
    return {
        "count": count,
        "sources": {
            split: sum(1 for case in cases if case.split == split) for split in SOURCE_FILES
        },
        "null": {
            name: {"count": value, "rate": round(value / count, 4)} for name, value in nulls.items()
        },
        "item_count": {str(value): items[value] for value in sorted(items)},
        "text_chars": _distribution([len(case.text) for case in cases]),
        "text_lines": _distribution([case.text.count("\n") + 1 for case in cases]),
        "total_digits": _distribution([len(str(total)) for total in totals if total is not None]),
        "flags": {flag: flags[flag] for flag in sorted(flags)},
    }


def statistics(derivation: Derivation) -> dict[str, Any]:
    receipts = derivation.receipts
    groups = derivation.groups
    copies = [group for group in groups if len(group) > 1]
    conflicts = [entry for entry in derivation.excluded if entry["reason"] == "label_conflict"]
    cases = [case for name in SPLIT_NAMES for case in derivation.splits[name]]
    flagged: dict[str, list[str]] = {}
    for case in sorted(cases, key=lambda receipt: receipt.case_id):
        for flag in case.flags:
            flagged.setdefault(flag, []).append(case.case_id)
    source_flags = Counter(flag for receipt in receipts for flag in receipt.flags)
    return {
        "dataset": DATASET,
        "dataset_revision": REVISION,
        "rule_version": RULE_VERSION,
        "source": {
            "receipts": len(receipts),
            "by_split": {
                split: sum(1 for receipt in receipts if receipt.split == split)
                for split in SOURCE_FILES
            },
            "flags": {flag: source_flags[flag] for flag in sorted(source_flags)},
        },
        "duplicates": {
            "contents": len(groups),
            "groups_with_copies": len(copies),
            "receipts_in_groups_with_copies": sum(len(group) for group in copies),
            "groups_across_source_splits": sum(
                1 for group in copies if len({member.split for member in group}) > 1
            ),
            "label_conflict_groups": len({tuple(entry["group"]) for entry in conflicts}),
            "label_conflict_receipts": len(conflicts),
            "copies_dropped": sum(
                1 for entry in derivation.excluded if entry["reason"] == "duplicate"
            ),
            "near_duplicate_links": len(NEAR_DUPLICATES),
        },
        "excluded": {
            "receipts": len(derivation.excluded),
            "reasons": dict(
                sorted(Counter(entry["reason"] for entry in derivation.excluded).items())
            ),
        },
        "splits": {
            **{name: _split_stats(derivation.splits[name]) for name in SPLIT_NAMES},
            "all": _split_stats(cases),
        },
        "suspect_flags": list(SUSPECT_FLAGS),
        "flagged_cases": {flag: flagged[flag] for flag in sorted(flagged)},
    }


# -- Output files --------------------------------------------------------------------------

MANAGED_DIRS = ("cases", "floors", "manifests")


def canonical_json(value: Any) -> str:
    """Architect's canonical form (architect.bench.text_tasks.canonical_json)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def case_sha256(case: Mapping[str, Any]) -> str:
    """sha256 of the case as Architect's EvaluationCase dumps it (metadata {})."""
    return _sha256(canonical_json({**case, "metadata": {}}).encode("utf-8"))


def cases_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    """Equals architect.bench.text_tasks.cases_sha256 of the loaded cases, in file order."""
    dumped = [{**case, "metadata": {}} for case in cases]
    return _sha256(canonical_json(dumped).encode("utf-8"))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    lines = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    return "".join(lines).encode("utf-8")


def rules_sha256() -> str:
    return _sha256(PROMPT_RULES.encode("utf-8"))


def _provenance() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "dataset": DATASET,
        "dataset_url": DATASET_URL,
        "dataset_revision": REVISION,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "rule_version": RULE_VERSION,
        "rules_sha256": rules_sha256(),
    }


def render(
    derivation: Derivation, source: Mapping[str, Any], rows: Mapping[str, Sequence[SourceRow]]
) -> dict[str, bytes]:
    """Every output file (relative POSIX path -> exact bytes), derived only from the inputs."""
    files: dict[str, bytes] = {}
    for name in SPLIT_NAMES:
        split = derivation.splits[name]
        cases = [receipt.case() for receipt in split]
        jsonl = files[f"cases/{name}.jsonl"] = _jsonl_bytes(cases)
        listed: dict[str, Any] = {
            f"cases/{name}.jsonl": {
                "sha256": _sha256(jsonl),
                "cases_tag": "cases:" + _sha256(jsonl)[:16],
            }
        }
        if name in FLOORS:
            floor = files[f"floors/{name}.json"] = _json_bytes(cases)
            listed[f"floors/{name}.json"] = {"sha256": _sha256(floor)}
        sides = partition(receipt.case_id for receipt in split) if name in FLOORS else {}
        manifest: dict[str, Any] = {
            **_provenance(),
            "split": name,
            "count": len(split),
            "sources": {key: sum(1 for r in split if r.split == key) for key in SOURCE_FILES},
            "files": listed,
            "cases_sha256": cases_sha256(cases),
        }
        if name in FLOORS:
            manifest["partition"] = {
                "rule": "sorted case_id; even positions tune, odd positions are held out",
                "tuning": sum(1 for side in sides.values() if side == "tuning"),
                "held_out": sum(1 for side in sides.values() if side == "held_out"),
            }
        manifest["cases"] = [
            {
                "case_id": receipt.case_id,
                "source": receipt.source,
                "ground_truth_sha256": receipt.ground_truth_sha256,
                "text_sha256": receipt.text_sha256,
                "case_sha256": case_sha256(case),
                **({"partition": sides[receipt.case_id]} if sides else {}),
                "flags": list(receipt.flags),
            }
            for receipt, case in zip(split, cases, strict=True)
        ]
        files[f"manifests/{name}.json"] = _json_bytes(manifest)
    digests = {receipt.source: receipt.ground_truth_sha256 for receipt in derivation.receipts}
    reasons = dict(sorted(Counter(entry["reason"] for entry in derivation.excluded).items()))
    files["manifests/excluded.json"] = _json_bytes(
        {
            **_provenance(),
            "count": len(derivation.excluded),
            "reasons": reasons,
            "meanings": {reason: REASON_MEANINGS[reason] for reason in reasons},
            "near_duplicates": [
                {"sources": [a, b], "differs": differs} for a, b, differs in NEAR_DUPLICATES
            ],
            "receipts": [
                {**entry, "ground_truth_sha256": digests[entry["source"]]}
                for entry in derivation.excluded
            ],
        }
    )
    files["manifests/source.json"] = _json_bytes(
        {
            **{key: value for key, value in _provenance().items() if not key.startswith("rule")},
            "column": COLUMN,
            "fetched_by": (
                "HTTP range requests on the revision's resolve URLs: each parquet footer and "
                "its ground_truth column chunks only; no image bytes"
            ),
            "files": list(source["files"]),
            "ground_truth_sha256": {
                split: [_sha256(row.ground_truth.encode("utf-8")) for row in rows[split]]
                for split in SOURCE_FILES
            },
        }
    )
    files["task.json"] = _json_bytes(
        {
            "rule_version": RULE_VERSION,
            "rules": PROMPT_RULES,
            "rules_sha256": rules_sha256(),
            "input_schema": INPUT_SCHEMA,
            "output_schema": OUTPUT_SCHEMA,
            "dataset": DATASET,
            "dataset_revision": REVISION,
            "license": LICENSE,
        }
    )
    stats = statistics(derivation)
    files["stats.json"] = _json_bytes(stats)
    files["RULES.md"] = rules_markdown(stats).encode("utf-8")
    files["NOTICE.md"] = notice_markdown().encode("utf-8")
    files["README.md"] = readme_markdown(derivation, stats).encode("utf-8")
    return files


def write(out: Path, files: Mapping[str, bytes]) -> None:
    """Write every file and remove stale files from the managed directories."""
    for relative, data in files.items():
        _write_bytes(out / relative, data)
    for directory in MANAGED_DIRS:
        for path in sorted((out / directory).glob("*")):
            if path.is_file() and f"{directory}/{path.name}" not in files:
                path.unlink()


def verify(out: Path, files: Mapping[str, bytes]) -> list[str]:
    """Problems found comparing the derived files with ``out`` (empty when identical)."""
    problems: list[str] = []
    for relative, data in sorted(files.items()):
        path = out / relative
        if not path.is_file():
            problems.append(f"missing {relative}")
        elif _sha256(path.read_bytes()) != _sha256(data):
            problems.append(f"differs {relative}")
    for directory in MANAGED_DIRS:
        for path in sorted((out / directory).glob("*")):
            if path.is_file() and f"{directory}/{path.name}" not in files:
                problems.append(f"unexpected {directory}/{path.name}")
    return problems


# -- Documents -----------------------------------------------------------------------------

_GENERATED = "Generated by `scripts/fetch_bench_data.py`; edit the script, never this file."

FLAG_MEANINGS: tuple[tuple[str, str], ...] = (
    ("multi_value:<f>", "annotated more than once, every time with the same amount"),
    (
        "multi_value_disagree:<f>",
        "annotated more than once with different amounts; the first is the label",
    ),
    ("first_not_topmost:<f>", "the first annotation is not the topmost printing"),
    ("printing_mismatch:<f>", "the annotations could not be matched to their printed lines"),
    (
        "multi_number:<f>",
        "the annotation holds more than one number (`Pb1 10% 7,000`, or `154 000` with a"
        " space between thousands), so deleting the non-digits joins them",
    ),
    (
        "decimal:<f>",
        "the amount ends in one or two decimals (such as `226,500.00`), which stay as digits",
    ),
    ("sign:<f>", "the amount carries a minus sign or a parenthesis"),
    ("no_digit:<f>", "annotated without any digit (such as `-`), so the label is null"),
    ("total_missing", "no `total_price` annotation, so `total` is null"),
    (
        "total_missing_total_word",
        "no `total_price` annotation, yet a line says total (not subtotal) beside an"
        " amount of four or more digits",
    ),
    ("sub_items", "a menu entry has sub-items"),
    ("menu_entry_without_name", "a menu entry has no name"),
    ("quantity_over_1", "a menu entry has a quantity above 1"),
    ("void_menu", "the receipt has a void line"),
    ("row_split", "a `row_id` spans several printed lines, so its row was split (Text, step 4)"),
)


def rules_markdown(stats: Mapping[str, Any]) -> str:
    flags = "\n".join(f"| `{flag}` | {meaning} |" for flag, meaning in FLAG_MEANINGS)
    suspect = ", ".join(f"`{flag}`" for flag in SUSPECT_FLAGS)
    sizes = {name: size for name, size in SPLITS}
    counts = stats["excluded"]["reasons"]
    by_hand = "\n".join(
        f"| `{reason}` | {meaning} | {counts.get(reason, 0)} |"
        for reason, meaning in EXCLUSION_REASONS.items()
    )
    split_rows = stats["source"]["flags"].get("row_split", 0)
    return f"""# Receipt rules ({RULE_VERSION})

{_GENERATED}

## Prompt rules

The spec and every plain prompt quote this block word for word. Its sha256 (UTF-8 bytes, \
no trailing newline) is `{rules_sha256()}`, and every manifest records it.

```text
{PROMPT_RULES}
```

The input is `{{"receipt": "<text>"}}` and the output holds the four keys above; \
`task.json` has both JSON Schemas. A case is correct only when all four values are \
exactly equal.

## Labels

Each label is read from the receipt's CORD `gt_parse` annotation:

| Key | Annotation |
|---|---|
| `total` | `gt_parse.total.total_price` |
| `subtotal` | `gt_parse.sub_total.subtotal_price` |
| `tax` | `gt_parse.sub_total.tax_price` |
| `item_count` | the number of entries in `gt_parse.menu` |

1. `total` and `sub_total` may be one object or a list of objects, and a field may be one \
string or a list of strings when it was annotated more than once. Annotations are read in \
annotation order, which runs top to bottom on the receipt (`first_not_topmost` checks it).
2. An annotation's amount is the integer left after deleting every character that is not \
an ASCII digit 0-9. An annotation without a digit has no amount.
3. A field's label is the amount of its first annotation that has one. It is `null` when \
the field is not annotated or none of its annotations has a digit.
4. `item_count` counts menu entries: a single object counts as 1 and a missing menu as 0. \
Quantities (`cnt`) and sub-items (`sub`, inside their entry) do not change it. A void \
line (`void_menu`, kept apart from `menu`) is not counted, while the entry it cancels \
stays in `menu` and counts.

## Text

The input is one text blob per receipt, rebuilt from the annotated words (`valid_line`), \
never from the image:

1. Every word of every `valid_line` entry takes part, key words included. Whitespace runs \
inside a word become one space; a word left empty is dropped.
2. Words are grouped into rows by `row_id`.
3. A word sits at the centre of its quad, the mean of its four x and its four y \
coordinates. Its box is the smallest upright rectangle around the quad.
4. Two words of one row are stacked when their boxes overlap horizontally by more than \
30% of the narrower box and their centres lie more than half the shorter box's height \
apart vertically. A row with a stacked pair spans several printed lines, and it is split:
   - Levels follow the tilt of the photo: a word's level is its centre y minus the tilt \
times its centre x. The tilt is the median, over the rows without a stacked pair, of the \
slope from the row's leftmost word centre to its rightmost; a row whose two ends share a \
centre x does not count, and the tilt is 0 when no row does.
   - The row holds as many lines as its largest stack, a set of words every two of which \
are stacked. The words of each largest stack go, by level from the top, to lines 1 to n.
   - Every other word, in annotation order, joins the line whose stack words have the \
mean level nearest its own (the upper line on a tie), but never a line that holds a word \
it is stacked with.

   This repairs the receipts where CORD gave one `row_id` to words on several printed \
lines ({split_rows} of CORD's receipts, flagged `row_split`).
5. Lines are ordered by the mean centre y of their words, then by their leftmost centre \
x, then by `row_id`, then top to bottom within a split row. Words in a line are ordered by \
centre x, then by annotation order. Positions are compared as exact fractions.
6. Words are joined by one space and lines by one newline, with no trailing newline.

CORD v2 annotates only menu, subtotal, total and void lines (store, date and payment \
details were never released), so the text holds only those lines.

## Duplicates

CORD holds some receipts more than once, sometimes in two official splits, annotated \
again with small differences. Receipts are grouped, transitively, when

- their texts hold the same multiset of lowercase letter and digit runs (case, \
punctuation, spacing and line breaks do not matter),
- their texts hold the same sequence of digits (the same amounts in the same order; the \
four labels are read from those amounts), or
- a hand review found them to be copies of one purchase that neither key joins: the \
texts differ only by line breaks, an item or option name, or a line one copy lacks, and \
every amount both print agrees, cash and change included. The \
{len(NEAR_DUPLICATES)} pairs are listed in the script and under `near_duplicates` in \
`manifests/excluded.json`, with what differs. Repeat purchases of the same order, which \
differ in the cash tendered or the way of paying, stay separate receipts.

A group whose members carry different labels is dropped whole (`label_conflict`): two \
annotations of one receipt disagree, so it has no single answer. Any other group keeps \
one case: a test copy first, then validation, then train, and the lowest row index \
within a split. The other copies are dropped (`duplicate`). `manifests/excluded.json` \
lists every dropped receipt with its reason.

## Exclusions

Some receipts are left out by hand before the splits are assigned (the list is in the \
script, from the label audit in AUDIT.md). Each carries a reason and a note in \
`manifests/excluded.json`:

| Reason | Meaning | Receipts |
|---|---|---|
{by_hand}

A duplicate group with a listed member is dropped whole, its unlisted copies noted as \
copies. When the copies of a listed receipt disagree on a label, the group is recorded as \
`label_conflict` and the listed receipt keeps its note.

## Splits

1. Groups made only of train receipts are ordered by the sha256 (hex) of the kept \
case's text.
2. The first {sizes["pilot"]} are the pilot, the next {sizes["rehearsal"]} the rehearsal \
set and the next {sizes["evidence"]} the evidence floor.
3. The delivery set is every other train group plus every group with a validation or \
test copy.
4. The rehearsal set and the evidence floor are Architect floors. Architect sorts their \
case ids and gives even positions to tuning and odd positions to the held-out exam \
(`architect/floors/base.py`), so they split \
{sizes["rehearsal"] // 2}/{sizes["rehearsal"] // 2} and \
{sizes["evidence"] // 2}/{sizes["evidence"] // 2}.

No two sets share a case or a duplicate group, hand-joined copies included. A case id is \
`{CASE_PREFIX}-<split>-<row>`: the CORD split and the row index within it.

## Flags

Flags are diagnostics and never change a label. Each manifest lists a case's flags, and \
`stats.json` lists the cases under each flag. `<f>` names the label concerned.

| Flag | Meaning |
|---|---|
{flags}

Hand-check the suspect flags first: {suspect}.
"""


def notice_markdown() -> str:
    return f"""# Notice

{_GENERATED}

The receipt data in this directory is derived from CORD v2, the Consolidated Receipt \
Dataset for Post-OCR Parsing, by NAVER Clova (Seunghyun Park, Seung Shin, Bado Lee, \
Junyeop Lee, Jaeheung Surh, Minjoon Seo and Hwalsuk Lee).

- Source: {DATASET_URL} at revision `{REVISION}`
- Project: https://github.com/clovaai/cord
- Paper: Park et al., "CORD: A Consolidated Receipt Dataset for Post-OCR Parsing", \
Document Intelligence Workshop at NeurIPS 2019, https://openreview.net/pdf?id=SJl3z659UH
- License: Creative Commons Attribution 4.0 International (CC BY 4.0), {LICENSE_URL}

Text rebuilt from annotations; no images.

Changes made: each receipt's text was rebuilt from CORD's word annotations, and four \
labels (total, subtotal, tax and item count) were derived from its parse annotation by \
the rules in RULES.md. Duplicate receipts were removed, and so were receipts whose labels \
contradict their text or those rules; the rest were assigned to new sets. No receipt \
image was downloaded or included. The derived files are shared under CC BY 4.0.

CORD is provided as is, without warranties. NAVER Clova and the CORD authors do not \
endorse this project.
"""


def readme_markdown(derivation: Derivation, stats: Mapping[str, Any]) -> str:
    def sources(name: str) -> str:
        counts = stats["splits"][name]["sources"]
        return ", ".join(f"{counts[split]} {split}" for split in SOURCE_FILES if counts[split])

    def size(name: str) -> str:
        count = len(derivation.splits[name])
        return (
            f"{count} ({count // 2} practice, {count - count // 2} exam)"
            if name in FLOORS
            else str(count)
        )

    uses = {
        "pilot": "measures the plain models and sets the bar; no build sees it",
        "rehearsal": "development builds only",
        "evidence": "the floor of the pre-registered builds",
        "delivery": "never seen by any build step",
    }
    table = "\n".join(
        f"| {name} | {size(name)} | {sources(name)} | {uses[name]} |" for name in SPLIT_NAMES
    )
    duplicates = stats["duplicates"]
    by_hand = sum(stats["excluded"]["reasons"].get(reason, 0) for reason in EXCLUSION_REASONS)
    case_shape = (
        '{"case_id", "task": {"receipt"}, "expected": {"total", "subtotal", "tax", "item_count"}}'
    )
    floor_entry = (
        '"floors": {"cord-receipts": {"kind": "held_out_tasks",'
        ' "path": "<path>/floors/evidence.json"}}'
    )
    return f"""# Receipt data (CORD v2)

{_GENERATED}

One text blob per receipt and four exact labels (`total`, `subtotal`, `tax`, \
`item_count`) for the receipt reader. Derived from CORD v2 under CC BY 4.0: see \
NOTICE.md. The rules, quoted word for word in every prompt, are in RULES.md and \
`task.json`.

| Set | Cases | Sources | Use |
|---|---|---|---|
{table}

Of CORD's {stats["source"]["receipts"]:,} receipts, {duplicates["copies_dropped"]} were \
dropped as copies of another receipt, {duplicates["label_conflict_receipts"]} because \
their copies disagree on a label, and {by_hand} by hand because a label contradicts the \
text or the rules, or the text cannot be rebuilt in order. `manifests/excluded.json` lists \
each one with its reason, and RULES.md explains them.

- `cases/<set>.jsonl`: one case per line, `{case_shape}`, the format `forge run --cases` \
reads.
- `floors/<set>.json`: the rehearsal and evidence sets as Architect profile floors (a \
JSON array of the same cases). A profile names one with `{floor_entry}`, the path \
relative to the profile file.
- `manifests/<set>.json`: the dataset revision, rule version, rules sha256 and file \
sha256s, then per case its source row, sha256s (ground truth, text, case) and flags. \
`manifests/source.json` pins the source files and every source row.
- `stats.json`: null rates, item counts, text lengths and flags per set.

Raw downloads are cached outside the repository (`C:/t/cord-cache` on Windows, \
`~/.cache/architect/cord-cache` elsewhere, or `CORD_CACHE`):

    uv run python scripts/fetch_bench_data.py                     # fetch and write
    uv run python scripts/fetch_bench_data.py --verify            # re-derive, compare hashes
    uv run python scripts/fetch_bench_data.py --verify --refetch  # from the source again
"""


# -- Command line --------------------------------------------------------------------------


def _summary(derivation: Derivation, stats: Mapping[str, Any]) -> list[str]:
    lines = []
    for name in (*SPLIT_NAMES, "all"):
        split = stats["splits"][name]
        null = ", ".join(f"{key} {value['rate']:.1%}" for key, value in split["null"].items())
        lines.append(f"{name:<9} {split['count']:>4} cases  null: {null}")
    duplicates = stats["duplicates"]
    lines.append(
        f"duplicates: {duplicates['copies_dropped']} copies dropped, "
        f"{duplicates['label_conflict_receipts']} receipts in "
        f"{duplicates['label_conflict_groups']} label conflicts dropped"
    )
    reasons = stats["excluded"]["reasons"]
    lines.append(
        "left out by hand: "
        + ", ".join(f"{reasons.get(reason, 0)} {reason}" for reason in EXCLUSION_REASONS)
    )
    suspect = sorted(
        {
            case
            for flag, cases in stats["flagged_cases"].items()
            if flag.split(":")[0] in SUSPECT_FLAGS
            for case in cases
        }
    )
    lines.append(f"suspect labels: {len(suspect)} cases (stats.json flagged_cases)")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--cache", type=Path, default=None, help="raw download cache")
    parser.add_argument("--verify", action="store_true", help="re-derive and compare hashes")
    parser.add_argument("--refetch", action="store_true", help="download from the source again")
    parser.add_argument("--offline", action="store_true", help="never use the network")
    args = parser.parse_args(argv)
    if args.refetch and args.offline:
        parser.error("--refetch needs the network")
    cache = args.cache or default_cache()
    try:
        with tempfile.TemporaryDirectory(prefix="cord-verify-") as scratch:
            if args.refetch and args.verify:
                cache = Path(scratch)  # verify against a fresh download, never the cache
            if args.refetch or not cache_complete(cache):
                if args.offline:
                    raise DataError(f"the cache at {cache} is incomplete (run without --offline)")
                fetch(cache)
            rows, source = read_cache(cache)
        derivation = derive(rows)
        files = render(derivation, source, rows)
        stats = json.loads(files["stats.json"])
        if args.verify:
            problems = verify(args.out, files)
            for problem in problems:
                print(problem)
            print(
                f"verify: {len(files) - len(problems)} of {len(files)} files match"
                if problems
                else f"verify: all {len(files)} files match"
            )
            return 1 if problems else 0
        write(args.out, files)
        for line in _summary(derivation, stats):
            print(line)
        print(f"wrote {len(files)} files to {args.out}")
        return 0
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
