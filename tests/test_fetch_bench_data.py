"""scripts/fetch_bench_data.py: CORD v2 receipt text, labels, splits and manifests.

Nothing here touches the network or a model. The parquet reader runs on tiny files that
pyarrow wrote (embedded below), the fetch on a fake server, the derivation on synthetic
CORD-shaped annotations, and the committed examples/receipts files are checked against
their own manifests and Architect's floor loaders. When the raw cache exists on this
machine, the committed files are also re-derived from it byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from architect.bench.text_tasks import canonical_json as architect_canonical_json
from architect.bench.text_tasks import cases_sha256 as architect_cases_sha256
from architect.floors import EvaluationCase, HeldOutTasks
from architect.forge_modes.cases import load_cases as load_forge_cases
from architect.session.floors import load_cases as load_floor_cases
from architect.spec import FloorDefinition, FloorKind

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples" / "receipts"
SETS = ("pilot", "rehearsal", "evidence", "delivery")


def _load_script() -> ModuleType:
    name = "fetch_bench_data_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "fetch_bench_data.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while being defined
    spec.loader.exec_module(module)
    return module


fbd = _load_script()

GROUND_TRUTHS: list[str | None] = [
    '{"gt_parse": {"menu": {"nm": "TEA", "price": "8,000"}}}',
    '{"gt_parse": {"menu": [{"nm": "A"}, {"nm": "B"}]}}',
    "plain text value with unicode é",
    '{"gt_parse": {}}',
    "x" * 70,
]
WITH_NULL: list[str | None] = [GROUND_TRUTHS[0], None, *GROUND_TRUTHS[2:]]
# Written by pyarrow 25.0.1 (pq.write_table) and read back by pyarrow to these values:
# dictionary pages, PLAIN pages, data pages v1 and v2, snappy, gzip and no compression,
# optional and required columns, several row groups and several pages per chunk. The
# first file also has an image struct column before ground_truth, as CORD's files do.
PARQUET_FIXTURES: dict[str, tuple[str, list[str | None]]] = {
    "dictionary_snappy_v1": (
        (
            "UEFSMRUEFWQVUEwVChUAEgAAMhgCAAAAAAEEBQYIAAEGDQgIAAEIFQo8AAEKAAAAAAEAAQABAAEAARUA"
            "FRYVGiwVChUQFQYVBhw2ACgKAAEAAQABAAEAARgCAAEREQAAAAsoAgAAAAoCAwOIRgAVBBUAFQJMFQAV"
            "ABIAAAAVABUOFRIsFQoVEBUGFQYcNgoAAAAHGAIAAAAKAQAVBBXmAxWQAkwVChUAEgAA8wFYNwAAAHsi"
            "Z3RfcGFyc2UiOiB7Im1lbnUFCUBubSI6ICJURUEiLCAicHJpYwEgKCI4LDAwMCJ9fX0yYjsABFt7DTwQ"
            "QSJ9LCARDahCIn1dfX0gAAAAcGxhaW4gdGV4dCB2YWx1ZSB3aXRoIHVuaWNvZGUgw6kQQloAGH19RgAA"
            "AHj+AQAFARUAFRYVGiwVChUQFQYVBhw2ACgQeyJndF9wYXJzZSI6IHt9fRggcGxhaW4gdGV4dCB2YWx1"
            "ZSB3aXRoIHVuaWNvZGUgw6kREQAAAAsoAgAAAAoBAwOIRgAVBBlcNQAYBnNjaGVtYRUEADUCGAVpbWFn"
            "ZRUEABUMJQIYBWJ5dGVzABUMJQIYBHBhdGglAEwcAAAAFQwlAhgMZ3JvdW5kX3RydXRoJQBMHAAAABYK"
            "GRwZPCYAHBUMGTUABhAZKAVpbWFnZQVieXRlcxUCFgoW5AEW1AEmdCYIHDYAKAoAAQABAAEAAQABGAIA"
            "ARERABksFQQVABUCABUAFRAVAgA8FjwZBhk2AAAKAAAAJgAcFQwZNQAGEBkoBWltYWdlBHBhdGgVAhYK"
            "FlQWWib6ASbcARw2CgAZLBUEFQAVAgAVABUQFQIAPBYAGQYZNgAKAAAAACYAHBUMGTUABhAZGAxncm91"
            "bmRfdHJ1dGgVAhYKFrIFFuADJuYEJrYCHDYAKBB7Imd0X3BhcnNlIjoge319GCBwbGFpbiB0ZXh0IHZh"
            "bHVlIHdpdGggdW5pY29kZSDDqRERABksFQQVABUCABUAFRAVAgA8Fr4DGQYZJgAKAAAAFuoHFgomCBaO"
            "BgAZHBgMQVJST1c6c2NoZW1hGOACLy8vLy93QUJBQUFRQUFBQUFBQUtBQXdBQmdBRkFBZ0FDZ0FBQUFB"
            "QkJBQU1BQUFBQ0FBSUFBQUFCQUFJQUFBQUJBQUFBQUlBQUFBNEFBQUFCQUFBQUhqLy8vOEFBQUVGRUFB"
            "QUFDQUFBQUFFQUFBQUFBQUFBQXdBQUFCbmNtOTFibVJmZEhKMWRHZ0FBQUFBY1AvLy82ai8vLzhBQUFF"
            "TkdBQUFBQ0FBQUFBRUFBQUFBZ0FBQUZBQUFBQVVBQUFBQlFBQUFHbHRZV2RsQUFBQW9QLy8vOWovLy84"
            "QUFBRUZFQUFBQUJnQUFBQUVBQUFBQUFBQUFBUUFBQUJ3WVhSb0FBQUFBTWovLy84UUFCUUFDQUFHQUFj"
            "QURBQUFBQkFBRUFBQUFBQUFBUVFRQUFBQUhBQUFBQVFBQUFBQUFBQUFCUUFBQUdKNWRHVnpBQUFBQkFB"
            "RUFBUUFBQUFBQUFBQQAYIHBhcnF1ZXQtY3BwLWFycm93IHZlcnNpb24gMjUuMC4xGTwcAAAcAAAcAAAA"
            "JQMAAFBBUjE="
        ),
        GROUND_TRUTHS,
    ),
    "plain_none_v2_groups": (
        (
            "UEFSMRUGFXoVelwVBBUCFQQVABUEFQASHAAAAAMBNwAAAHsiZ3RfcGFyc2UiOiB7Im1lbnUiOiB7Im5t"
            "IjogIlRFQSIsICJwcmljZSI6ICI4LDAwMCJ9fX0VBhV0FXRcFQQVABUEFQAVBBUAEhwAAAAEASAAAABw"
            "bGFpbiB0ZXh0IHZhbHVlIHdpdGggdW5pY29kZSDDqRAAAAB7Imd0X3BhcnNlIjoge319FQYVmAEVmAFc"
            "FQIVABUCFQAVBBUAEhwAAAACAUYAAAB4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
            "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4FQQZLDUAGAZzY2hlbWEVAgAVDCUCGAxncm91"
            "bmRfdHJ1dGglAEwcAAAAFgoZPBkcJgAcFQwZJQYAGRgMZ3JvdW5kX3RydXRoFQAWBBaqARaqASYISRwV"
            "ABUAFQIAPBZuGQYZJgICAAAAFqoBFgQmCBaqAQAZHCYAHBUMGSUGABkYDGdyb3VuZF90cnV0aBUAFgQW"
            "pAEWpAEmsgFJHBUAFQAVAgA8FmAZBhkmAAQAAAAWpAEWBCayARakAQAZHCYAHBUMGSUGABkYDGdyb3Vu"
            "ZF90cnV0aBUAFgIWzAEWzAEm1gJJHBUAFQAVAgA8FowBGQYZJgACAAAAFswBFgIm1gIWzAEAGRwYDEFS"
            "Uk9XOnNjaGVtYRisAS8vLy8vM2dBQUFBUUFBQUFBQUFLQUF3QUJnQUZBQWdBQ2dBQUFBQUJCQUFNQUFB"
            "QUNBQUlBQUFBQkFBSUFBQUFCQUFBQUFFQUFBQVVBQUFBRUFBVUFBZ0FCZ0FIQUF3QUFBQVFBQkFBQUFB"
            "QUFBRUZFQUFBQUNRQUFBQUVBQUFBQUFBQUFBd0FBQUJuY205MWJtUmZkSEoxZEdnQUFBQUFCQUFFQUFR"
            "QUFBQT0AGCBwYXJxdWV0LWNwcC1hcnJvdyB2ZXJzaW9uIDI1LjAuMRkcHAAAAO0BAABQQVIx"
        ),
        WITH_NULL,
    ),
    "dictionary_gzip_v1_pages": (
        (
            "UEFSMRUEFeYDFZICTBUKFQASAAAfiwgAAAAAAAIKM2dgYKhWSi+JL0gsKk5VslKoVspNzSsFM/JygZRS"
            "iKujko6CUkFRZjJIXslCx8DAQKm2ttYIp9ZomF5HpVoduEFOSrWxtbUKQF0FOYmZeQolqRUlCmWJOaWp"
            "CuWZJRkKpXmZyfkpqQqHVwpgmFxb6wYUq6AKAABGdIdY8wAAABUAFRIVOiwVAhUQFQYVBhw2ACg3eyJn"
            "dF9wYXJzZSI6IHsibWVudSI6IHsibm0iOiAiVEVBIiwgInByaWNlIjogIjgsMDAwIn19fRg3eyJndF9w"
            "YXJzZSI6IHsibWVudSI6IHsibm0iOiAiVEVBIiwgInByaWNlIjogIjgsMDAwIn19fRERAAAAH4sIAAAA"
            "AAACCmNiYGBgYmRkYgAAmAB3OQkAAAAVABUSFTosFQIVEBUGFQYcNgAoMnsiZ3RfcGFyc2UiOiB7Im1l"
            "bnUiOiBbeyJubSI6ICJBIn0sIHsibm0iOiAiQiJ9XX19GDJ7Imd0X3BhcnNlIjogeyJtZW51IjogW3si"
            "bm0iOiAiQSJ9LCB7Im5tIjogIkIifV19fRERAAAAH4sIAAAAAAACCmNiYGBgYmRkYgQADjBwTgkAAAAV"
            "ABUSFTosFQIVEBUGFQYcNgAoIHBsYWluIHRleHQgdmFsdWUgd2l0aCB1bmljb2RlIMOpGCBwbGFpbiB0"
            "ZXh0IHZhbHVlIHdpdGggdW5pY29kZSDDqRERAAAAH4sIAAAAAAACCmNiYGBgYmRiYgIA7d8/1QkAAAAV"
            "ABUSFTosFQIVEBUGFQYcNgAoEHsiZ3RfcGFyc2UiOiB7fX0YEHsiZ3RfcGFyc2UiOiB7fX0REQAAAB+L"
            "CAAAAAAAAgpjYmBgYGJkYmIGAHvvOKIJAAAAFQAVEhU6LBUCFRAVBhUGHDYAKEZ4eHh4eHh4eHh4eHh4"
            "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4GEZ4"
            "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4"
            "eHh4eHh4eHh4EREAAAAfiwgAAAAAAAIKY2JgYGBiZGZiAQDvEJ49CQAAABUEGSw1ABgGc2NoZW1hFQIA"
            "FQwlAhgMZ3JvdW5kX3RydXRoJQBMHAAAABYKGRwZHCYAHBUMGTUABhAZGAxncm91bmRfdHJ1dGgVBBYK"
            "FuoNFt4NJroCJggcNgAoEHsiZ3RfcGFyc2UiOiB7fX0YIHBsYWluIHRleHQgdmFsdWUgd2l0aCB1bmlj"
            "b2RlIMOpEREAGSwVBBUAFQIAFQAVEBUKADwWvgMZBhkmAAoAAAAW6g0WCiYIFt4NABkcGAxBUlJPVzpz"
            "Y2hlbWEYrAEvLy8vLzNnQUFBQVFBQUFBQUFBS0FBd0FCZ0FGQUFnQUNnQUFBQUFCQkFBTUFBQUFDQUFJ"
            "QUFBQUJBQUlBQUFBQkFBQUFBRUFBQUFVQUFBQUVBQVVBQWdBQmdBSEFBd0FBQUFRQUJBQUFBQUFBQUVG"
            "RUFBQUFDUUFBQUFFQUFBQUFBQUFBQXdBQUFCbmNtOTFibVJmZEhKMWRHZ0FBQUFBQkFBRUFBUUFBQUE9"
            "ABggcGFycXVldC1jcHAtYXJyb3cgdmVyc2lvbiAyNS4wLjEZHBwAAACiAQAAUEFSMQ=="
        ),
        GROUND_TRUTHS,
    ),
    "required_snappy_plain": (
        (
            "UEFSMRUAFeYDFZACLBUKFQAVBhUGHDYAKBB7Imd0X3BhcnNlIjoge319GCBwbGFpbiB0ZXh0IHZhbHVl"
            "IHdpdGggdW5pY29kZSDDqRERAAAA8wFYNwAAAHsiZ3RfcGFyc2UiOiB7Im1lbnUFCUBubSI6ICJURUEi"
            "LCAicHJpYwEgKCI4LDAwMCJ9fX0yYjsABFt7DTwQQSJ9LCARDahCIn1dfX0gAAAAcGxhaW4gdGV4dCB2"
            "YWx1ZSB3aXRoIHVuaWNvZGUgw6kQQloAGH19RgAAAHj+AQAFARUEGSw1ABgGc2NoZW1hFQIAFQwlABgM"
            "Z3JvdW5kX3RydXRoJQBMHAAAABYKGRwZHCYAHBUMGSUGABkYDGdyb3VuZF90cnV0aBUCFgoWgAUWqgMm"
            "CDw2ACgQeyJndF9wYXJzZSI6IHt9fRggcGxhaW4gdGV4dCB2YWx1ZSB3aXRoIHVuaWNvZGUgw6kREQAZ"
            "HBUAFQAVAgA8Fr4DGQYZBgAAABaABRYKJggWqgMAGRwYDEFSUk9XOnNjaGVtYRisAS8vLy8vM2dBQUFB"
            "UUFBQUFBQUFLQUF3QUJnQUZBQWdBQ2dBQUFBQUJCQUFNQUFBQUNBQUlBQUFBQkFBSUFBQUFCQUFBQUFF"
            "QUFBQVVBQUFBRUFBVUFBZ0FBQUFIQUF3QUFBQVFBQkFBQUFBQUFBQUZFQUFBQUNRQUFBQUVBQUFBQUFB"
            "QUFBd0FBQUJuY205MWJtUmZkSEoxZEdnQUFBQUFCQUFFQUFRQUFBQT0AGCBwYXJxdWV0LWNwcC1hcnJv"
            "dyB2ZXJzaW9uIDI1LjAuMRkcHAAAAJUBAABQQVIx"
        ),
        GROUND_TRUTHS,
    ),
}


# -- Synthetic CORD-shaped annotations ----------------------------------------------------


def word(
    text: str, row_id: int, x: int, y: int, *, width: int = 20, height: int = 10, key: int = 0
) -> dict[str, Any]:
    quad = {
        "x1": x,
        "y1": y,
        "x2": x + width,
        "y2": y,
        "x3": x + width,
        "y3": y + height,
        "x4": x,
        "y4": y + height,
    }
    return {"quad": quad, "is_key": key, "row_id": row_id, "text": text}


def line(category: str, *words: dict[str, Any]) -> dict[str, Any]:
    return {"words": list(words), "category": category, "group_id": 0, "sub_group_id": 0}


def ground_truth(valid_line: list[dict[str, Any]], parse: dict[str, Any]) -> str:
    return json.dumps(
        {
            "gt_parse": parse,
            "meta": {"version": "2.0.0"},
            "valid_line": valid_line,
            "roi": {},
            "repeating_symbol": [],
            "dontcare": [],
        }
    )


def receipt(
    item: str, price: int, *, separator: str = ",", total: bool = True, cash: int | None = None
) -> str:
    """One item line and a TOTAL line, and a CASH line when ``cash`` is given; distinct
    prices keep receipts distinct."""
    amount = f"{price:,}".replace(",", separator)
    category = "total.total_price" if total else "total.total_etc"
    valid = [
        line("menu.nm", word(item, 1, 10, 10)),
        line("menu.price", word(amount, 1, 200, 10)),
        line(category, word("TOTAL", 2, 10, 40, key=1), word(amount, 2, 200, 40)),
    ]
    totals: dict[str, str] = {"total_price": amount} if total else {"total_etc": amount}
    if cash is not None:
        paid = f"{cash:,}".replace(",", separator)
        valid.append(
            line("total.cashprice", word("CASH", 3, 10, 70, key=1), word(paid, 3, 200, 70))
        )
        totals["cashprice"] = paid
    return ground_truth(valid, {"menu": {"nm": item, "price": amount}, "total": totals})


def source_rows(train: list[str], validation: list[str], test: list[str]) -> dict[str, list[Any]]:
    return {
        split: [fbd.SourceRow(split, index, text) for index, text in enumerate(texts)]
        for split, texts in (("train", train), ("validation", validation), ("test", test))
    }


SMALL_SPLITS = (("pilot", 3), ("rehearsal", 4), ("evidence", 6))


def _small(monkeypatch: pytest.MonkeyPatch) -> None:
    """Small splits, and no hand lists (they name rows of the real CORD data)."""
    monkeypatch.setattr(fbd, "SPLITS", SMALL_SPLITS)
    monkeypatch.setattr(fbd, "EXCLUDED", ())
    monkeypatch.setattr(fbd, "NEAR_DUPLICATES", ())


@pytest.fixture
def small_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    _small(monkeypatch)


def synthetic(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any], dict[str, list[Any]]]:
    _small(monkeypatch)
    rows = source_rows(
        [receipt(f"ITEM {index}", 2_000 + 41 * index) for index in range(20)],
        [receipt("VALIDATION", 90_000)],
        [receipt("TEST", 91_000)],
    )
    source = {
        "files": [
            {
                "path": "data/fake.parquet",
                "split": "train",
                "size": 1,
                "lfs_sha256": "0" * 64,
                "rows": 20,
                "footer_sha256": "0" * 64,
                "column_chunk_sha256": [],
            }
        ]
    }
    return fbd.derive(rows), source, rows


# -- Parquet ------------------------------------------------------------------------------


def test_snappy_literals_and_overlapping_copies() -> None:
    # "abc", then a copy of 9 bytes from 3 back (it overlaps itself), then "X".
    data = bytes([13, 0x08]) + b"abc" + bytes([0x15, 0x03, 0x00]) + b"X"
    assert fbd.snappy_decompress(data) == b"abcabcabcabcX"


def test_snappy_long_literals_and_two_byte_offsets() -> None:
    literal = bytes(range(70))  # past 60 bytes a literal stores its length in a byte
    copy = bytes([((4 - 1) << 2) | 2]) + (70).to_bytes(2, "little")
    data = bytes([74, 60 << 2, 69]) + literal + copy
    assert fbd.snappy_decompress(data) == literal + literal[:4]


def test_snappy_refuses_bad_streams() -> None:
    with pytest.raises(fbd.DataError, match="length mismatch"):
        fbd.snappy_decompress(bytes([5, 0x08]) + b"abc")
    with pytest.raises(fbd.DataError, match="offset"):
        fbd.snappy_decompress(bytes([9, 0x15, 0x03]))  # a copy before any output


def test_rle_bit_packed_hybrid() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 0]
    packed = sum(value << (3 * index) for index, value in enumerate(values)).to_bytes(3, "little")
    data = bytes([3 << 1, 5]) + bytes([(1 << 1) | 1]) + packed  # RLE 3 x 5, 8 bit-packed
    assert fbd.rle_hybrid(data, 3, 11) == [5, 5, 5, *values]
    assert fbd.rle_hybrid(data, 3, 4) == [5, 5, 5, 1]
    with pytest.raises(fbd.DataError):
        fbd.rle_hybrid(bytes([(1 << 1) | 1, 0xFF]), 3, 8)


def test_thrift_compact_structs() -> None:
    data = (
        bytes([0x15, 0x05])  # 1: i32 -3
        + bytes([0x18, 0x02])
        + b"ab"  # 2: binary
        + bytes([0x29, 0x25, 0x02, 0x04])  # 4: list<i32> [1, 2]
        + bytes([0x1C, 0x11, 0x00])  # 5: struct {1: true}
        + bytes([0x05, 0xC8, 0x01, 0x0E])  # 100: i32 7 (long field id form)
        + bytes([0x00])
    )
    assert fbd._Thrift(data).read_struct() == {1: -3, 2: b"ab", 4: [1, 2], 5: {1: True}, 100: 7}
    with pytest.raises(fbd.DataError, match="truncated"):
        fbd._Thrift(data[:5]).read_struct()


@pytest.mark.parametrize("name", sorted(PARQUET_FIXTURES))
def test_parquet_column_reader_matches_pyarrow(name: str) -> None:
    encoded, expected = PARQUET_FIXTURES[name]
    data = base64.b64decode(encoded)
    length = fbd.footer_length(data[-8:])
    rows, chunks = fbd.column_chunks(data[-8 - length : -8], "ground_truth")
    values = [
        value
        for chunk in chunks
        for value in fbd.decode_chunk(data[chunk.start : chunk.start + chunk.length], chunk)
    ]
    assert rows == len(expected)
    assert values == expected


def test_parquet_reader_refuses_what_it_cannot_read() -> None:
    data = base64.b64decode(PARQUET_FIXTURES["dictionary_snappy_v1"][0])
    with pytest.raises(fbd.DataError, match="not a parquet file"):
        fbd.footer_length(b"12345678")
    length = fbd.footer_length(data[-8:])
    footer = data[-8 - length : -8]
    with pytest.raises(fbd.DataError, match="not a flat string column"):
        fbd.column_chunks(footer, "image")
    with pytest.raises(fbd.DataError):
        fbd.column_chunks(footer[: length // 2], "ground_truth")
    _rows, [chunk] = fbd.column_chunks(footer, "ground_truth")
    with pytest.raises(fbd.DataError):
        fbd.decode_chunk(data[chunk.start : chunk.start + chunk.length - 5], chunk)


# -- Fetch --------------------------------------------------------------------------------


class _Response:
    def __init__(self, hub: FakeHub, status: int, headers: dict[str, str], body: bytes) -> None:
        self.hub, self.status, self.headers, self.body = hub, status, headers, body

    def read(self) -> bytes:
        self.hub.sent += len(self.body)
        return self.body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeHub:
    """Serves pinned files by byte range; each file is a head and a tail around zeros."""

    def __init__(self, files: dict[str, tuple[int, bytes, bytes]], **behaviour: Any) -> None:
        self.files = files
        self.behaviour = behaviour
        self.sent = 0

    def content(self, path: str, start: int, end: int) -> bytes:
        size, head, tail = self.files[path]
        out = bytearray(end - start)
        for offset, part in ((0, head), (size - len(tail), tail)):
            low, high = max(start, offset), min(end, offset + len(part))
            if low < high:
                out[low - start : high - start] = part[low - offset : high - offset]
        return bytes(out)

    def urlopen(self, request: Any, timeout: float) -> _Response:
        path = request.full_url.split(f"/resolve/{fbd.REVISION}/", 1)[1]
        size = self.files[path][0]
        start, last = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
        if self.behaviour.get("ignore_range"):
            return _Response(self, 200, {"Content-Length": str(size)}, self.content(path, 0, size))
        reported = self.behaviour.get("reported_size", size)
        headers = {"Content-Range": f"bytes {start}-{last}/{reported}"}
        return _Response(self, 206, headers, self.content(path, start, last + 1))


def _pinned_fake(monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> tuple[FakeHub, str, int]:
    """One pinned 50 MB train file holding the first fixture's columns."""
    data = base64.b64decode(PARQUET_FIXTURES["dictionary_snappy_v1"][0])
    length = fbd.footer_length(data[-8:])
    name, size = "data/train-00000-of-00001-fake.parquet", 50_000_000
    hub = FakeHub({name: (size, data[: -8 - length], data[-8 - length :])}, **behaviour)
    monkeypatch.setattr(fbd, "SOURCE_FILES", {"train": ((name, size, "0" * 64),)})
    monkeypatch.setattr(fbd, "SPLIT_SIZES", {"train": len(GROUND_TRUTHS)})
    monkeypatch.setattr(fbd, "check_hub", lambda: {})
    monkeypatch.setattr(fbd.urllib.request, "urlopen", hub.urlopen)
    return hub, name, size


def test_fetch_caches_only_the_footer_and_the_ground_truth_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, name, _size = _pinned_fake(monkeypatch)
    cache = tmp_path / "cache"
    assert not fbd.cache_complete(cache)
    fbd.fetch(cache, log=lambda message: None)
    assert fbd.cache_complete(cache)
    rows, source = fbd.read_cache(cache)
    assert [row.ground_truth for row in rows["train"]] == GROUND_TRUTHS
    [record] = source["files"]
    assert record["path"] == name and record["rows"] == len(GROUND_TRUTHS)
    footer = (cache / fbd.REVISION / f"{Path(name).name}.footer").read_bytes()
    _rows, [chunk] = fbd.column_chunks(footer, "ground_truth")
    # The tail, the footer and the one column chunk: never the image column.
    assert hub.sent == 8 + len(footer) + chunk.length


def test_a_server_that_ignores_the_range_never_sends_a_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, name, size = _pinned_fake(monkeypatch, ignore_range=True)
    with pytest.raises(fbd.DataError, match="no partial content"):
        fbd._range_get(name, size, size - 8, size)
    assert hub.sent == 0


def test_a_range_of_another_file_size_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, name, size = _pinned_fake(monkeypatch, reported_size=1234)
    with pytest.raises(fbd.DataError, match="no partial content"):
        fbd._range_get(name, size, size - 8, size)
    assert hub.sent == 0


def test_check_hub_confirms_the_pinned_revision_licence_and_files() -> None:
    tree = [
        {"type": "file", "path": name, "size": size, "lfs": {"oid": oid}}
        for files in fbd.SOURCE_FILES.values()
        for name, size, oid in files
    ]
    info = {"sha": fbd.REVISION, "cardData": {"license": "cc-by-4.0"}}

    def hub(info: dict[str, Any], tree: list[dict[str, Any]]) -> Any:
        return lambda url: tree if "/tree/" in url else info

    assert fbd.check_hub(hub(info, tree))["sha"] == fbd.REVISION
    with pytest.raises(fbd.DataError, match="licence"):
        fbd.check_hub(hub({**info, "cardData": {"license": "mit"}}, tree))
    with pytest.raises(fbd.DataError, match="revision"):
        fbd.check_hub(hub({**info, "sha": "0" * 40}, tree))
    with pytest.raises(fbd.DataError, match="different parquet files"):
        fbd.check_hub(hub(info, tree[:-1]))


def test_the_raw_cache_stays_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(fbd.DataError, match="outside the repository"):
        fbd.cache_complete(ROOT / ".cord-cache")
    monkeypatch.setenv("CORD_CACHE", str(tmp_path))
    assert fbd.default_cache() == tmp_path


# -- Text ---------------------------------------------------------------------------------


def test_rows_group_by_row_id_and_read_top_to_bottom() -> None:
    # Row ids run against the page (9 at the top), and one row's words come from
    # different categories.
    valid = [
        line("menu.nm", word("TEA", 3, 10, 100), word("COFFEE", 9, 10, 50)),
        line("menu.price", word("8,000", 3, 200, 104), word("12,000", 9, 200, 46)),
        line("total.total_price", word("TOTAL", 1, 10, 150, key=1), word("20,000", 1, 200, 150)),
    ]
    assert fbd.rebuild_text(valid) == "COFFEE 12,000\nTEA 8,000\nTOTAL 20,000"


def test_a_row_sits_at_the_mean_centre_of_its_quads() -> None:
    tall = word("TALL", 1, 10, 0, height=200)  # top at 0, centre at 100
    valid = [line("menu.nm", tall, word("SHORT", 2, 10, 60))]  # centre at 65
    assert fbd.rebuild_text(valid) == "SHORT\nTALL"


def test_words_read_left_to_right_with_whitespace_collapsed() -> None:
    valid = [
        line("menu.price", word(" 12.500 ", 4, 300, 10)),
        line(
            "menu.nm", word("Rp\t", 4, 200, 10), word("ES   TEH", 4, 10, 10), word("  ", 4, 100, 10)
        ),
    ]
    assert fbd.rebuild_text(valid) == "ES TEH Rp 12.500"


def test_ties_break_by_leftmost_word_then_row_id_then_annotation_order() -> None:
    valid = [
        line(
            "menu.nm",
            word("RIGHT", 5, 300, 10),
            word("LEFT", 6, 10, 10),
            word("SECOND", 2, 50, 30),
            word("FIRST", 1, 50, 30),
            word("A", 7, 10, 60),
            word("B", 7, 10, 60),
        )
    ]
    assert fbd.rebuild_text(valid).split("\n") == ["LEFT", "RIGHT", "FIRST", "SECOND", "A B"]


def test_a_row_id_must_be_an_integer() -> None:
    with pytest.raises(fbd.DataError, match="row_id"):
        fbd.rebuild_text([line("menu.nm", {**word("A", 1, 0, 0), "row_id": "1"})])


def test_stacked_words_overlap_horizontally_and_sit_apart_vertically() -> None:
    def at(x: int, y: int, width: int = 20) -> Any:
        return fbd.receipt_words([line("menu.nm", word("W", 1, x, y, width=width))])[0]

    top = at(0, 0)
    assert fbd.stacked(top, at(0, 30)) and fbd.stacked(at(0, 30), top)
    assert not fbd.stacked(top, at(14, 30))  # overlap 6 of 20 is exactly 30%: not more
    assert fbd.stacked(top, at(13, 30))
    assert not fbd.stacked(top, at(0, 5))  # centres exactly half a box height apart
    assert fbd.stacked(top, at(0, 6))
    assert not fbd.stacked(top, at(0, 30, width=0))  # a zero-width box overlaps nothing


def test_a_row_spanning_two_printed_lines_is_split_along_the_tilt() -> None:
    # The photo is tilted (right side higher by a tenth of the width), and one row_id holds
    # two printed lines. "50,000" is the only word at the right of that row: its raw height
    # is nearer TOTAL, but along the tilt it belongs to CASH.
    valid = [
        line("menu.nm", word("TEA", 1, 20, 98), word("COFFEE", 2, 20, 138)),
        line("menu.price", word("8,000", 1, 400, 60), word("12,000", 2, 400, 100)),
        line("total.total_price", word("TOTAL", 3, 20, 198, key=1)),
        line("total.cashprice", word("CASH", 3, 20, 228, key=1), word("50,000", 3, 400, 190)),
    ]
    rows: dict[int, list[Any]] = {}
    for item in fbd.receipt_words(valid):
        rows.setdefault(item.row_id, []).append(item)
    assert fbd.receipt_tilt(rows, skip=[3]) == fbd.Fraction(-1, 10)
    assert fbd.rebuild_text(valid).split("\n") == [
        "TEA 8,000",
        "COFFEE 12,000",
        "TOTAL",
        "CASH 50,000",
    ]
    split = fbd.split_row(rows[3], fbd.Fraction(0))  # without the tilt it would go wrong
    assert [[item.text for item in part] for part in split] == [["TOTAL", "50,000"], ["CASH"]]


def test_the_largest_stacks_set_the_lines_and_their_order() -> None:
    # Three printed lines in one row_id: the count, name and price columns each stack three
    # words, so each word goes to the line of its rank in its column.
    valid = [
        line("menu.cnt", word("2", 7, 0, 60), word("1", 7, 0, 0), word("1", 7, 0, 30)),
        line("menu.nm", word("COFFEE", 7, 50, 30), word("JUICE", 7, 50, 60), word("TEA", 7, 50, 0)),
        line(
            "menu.price",
            word("8,000", 7, 400, 0),
            word("9,000", 7, 400, 60),
            word("12,000", 7, 400, 30),
        ),
    ]
    assert fbd.rebuild_text(valid) == "1 TEA 8,000\n1 COFFEE 12,000\n2 JUICE 9,000"


def test_a_word_never_joins_a_line_holding_a_word_it_is_stacked_with() -> None:
    # The left column stacks three words, so the row holds three lines (heights 5, 35, 65).
    # P sits nearest line 1. Q sits nearest line 1 too, but it is stacked with P, so it
    # takes line 2.
    valid = [
        line("menu.nm", word("A", 4, 0, 0), word("B", 4, 0, 30), word("C", 4, 0, 60)),
        line("menu.price", word("P", 4, 100, 3), word("Q", 4, 100, 14)),
    ]
    assert fbd.rebuild_text(valid) == "A P\nB Q\nC"
    words = fbd.receipt_words(valid)
    assert fbd.stacked(words[3], words[4]) and not fbd.stacked(words[0], words[3])


def test_the_tilt_is_the_median_slope_of_the_other_rows() -> None:
    def row(row_id: int, *points: tuple[int, int]) -> list[Any]:
        return fbd.receipt_words([line("menu.nm", *(word("W", row_id, x, y) for x, y in points))])

    rows = {
        1: row(1, (0, 0), (100, 10)),  # 0.1
        2: row(2, (0, 50), (100, 30), (40, 99)),  # leftmost to rightmost: -0.2
        3: row(3, (0, 80), (100, 110)),  # 0.3
        4: row(4, (10, 120)),  # one word: no slope
        5: row(5, (10, 150), (10, 170)),  # both ends share a centre x: no slope
    }
    assert fbd.receipt_tilt(rows) == fbd.Fraction(1, 10)
    assert fbd.receipt_tilt(rows, skip=[2]) == fbd.Fraction(2, 10)
    assert fbd.receipt_tilt({4: rows[4]}) == 0


def test_a_split_row_is_flagged_and_its_lines_rank_printings() -> None:
    valid = [
        line("menu.nm", word("TEA", 1, 10, 10)),
        line("menu.price", word("8,000", 1, 200, 10)),
        line("total.total_price", word("TOTAL", 2, 10, 40, key=1), word("8,000", 2, 200, 40)),
        line("total.total_price", word("CASH", 2, 10, 70, key=1), word("9,000", 2, 200, 70)),
    ]
    parse = {"menu": {"nm": "TEA", "price": "8,000"}, "total": {"total_price": ["8,000", "9,000"]}}
    parsed = fbd.parse_receipt(fbd.SourceRow("train", 1, ground_truth(valid, parse)))
    assert parsed.text == "TEA 8,000\nTOTAL 8,000\nCASH 9,000"
    assert set(parsed.flags) == {"multi_value_disagree:total", "row_split"}


# -- Labels -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("75,000", 75000),
        ("45.500", 45500),
        ("1,591,600", 1591600),
        ("Rp 12.500", 12500),
        ("Rp. 230.999", 230999),
        ("Rp.118.000", 118000),
        ("226,500.00", 22650000),
        ("35.000,00", 3500000),
        ("9,817.8", 98178),
        ("-2.727", 2727),
        ("( 1,236)", 1236),
        ("Discount -5,000", 5000),
        ("Pb1 10% 7,000", 1107000),
        ("10% 4,818", 104818),
        ("154 000", 154000),
        ("TOTAL 47,499", 47499),
        ("0", 0),
        ("Rp. 0000", 0),
        ("-", None),
        ("", None),
        ("Rp", None),
        ("١٢٣", None),
        ("１２", None),
    ],
)
def test_amounts_keep_only_ascii_digits(printed: str, expected: int | None) -> None:
    assert fbd.amount(printed) == expected


def test_the_prompt_rules_examples_follow_the_amount_rule() -> None:
    # "Take the amount itself, without its label or rate": the last number on the line.
    examples = re.findall(r'"([^"]+)" is (\d+)', fbd.PROMPT_RULES)
    assert len(examples) == 5
    for printed, value in examples:
        assert fbd.amount(printed.split()[-1]) == int(value)
    assert fbd.amount("PB1 10% 4,818") != 4818  # why the rule names the rate
    assert 'such as "-", is null' in fbd.PROMPT_RULES and fbd.amount("-") is None


def test_the_prompt_rules_are_plain_text_without_dashes_or_curly_quotes() -> None:
    assert fbd.RULE_VERSION == "receipts-v2"
    assert not {chr(code) for code in (0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D)} & set(
        fbd.PROMPT_RULES
    )
    assert fbd.PROMPT_RULES.isascii() and not fbd.PROMPT_RULES.endswith("\n")
    lines = fbd.PROMPT_RULES.split("\n")
    assert [text.split(":")[0] for text in lines[1:5]] == [
        "- total",
        "- subtotal",
        "- tax",
        "- item_count",
    ]


def test_first_amount_skips_printings_without_a_digit() -> None:
    assert fbd.first_amount(["-", "1,000", "2,000"]) == 1000
    assert fbd.first_amount(["10.000%", "546"]) == 10000
    assert fbd.first_amount(["-", "Rp"]) is None
    assert fbd.first_amount([]) is None


def test_field_values_flatten_sections_and_repeated_annotations() -> None:
    parse = {
        "sub_total": [
            {"subtotal_price": "18,182", "tax_price": ["1,546", "0"]},
            {"subtotal_price": "15,455"},
            "not a group",
        ],
        "total": {"total_price": ["55,834", "55,800"], "cashprice": "60,000"},
    }
    assert fbd.field_values(parse, "sub_total", "subtotal_price") == ["18,182", "15,455"]
    assert fbd.field_values(parse, "sub_total", "tax_price") == ["1,546", "0"]
    assert fbd.field_values(parse, "total", "total_price") == ["55,834", "55,800"]
    assert fbd.field_values({}, "total", "total_price") == []
    assert fbd.field_values({"total": {"total_price": [7, None, "1"]}}, "total", "total_price") == [
        "1"
    ]


def test_labels_take_the_first_printed_amount_and_count_menu_entries() -> None:
    parse = {
        "menu": [
            {"nm": "A", "cnt": "2", "price": "10,000", "sub": {"nm": "ICE"}},
            {"nm": "B", "price": "5,000"},
        ],
        "void_menu": {"nm": "A", "price": "-10,000"},
        "sub_total": {"subtotal_price": "15,000", "tax_price": ["-", "1,500", "0"]},
        "total": {"total_price": ["16,500", "16,000"]},
    }
    labels = {"total": 16500, "subtotal": 15000, "tax": 1500, "item_count": 2}
    assert fbd.receipt_labels(parse) == labels
    single = {"menu": {"nm": "TEA"}, "sub_total": {"tax_price": "-"}}
    assert fbd.receipt_labels(single) == {
        "total": None,
        "subtotal": None,
        "tax": None,
        "item_count": 1,
    }
    assert fbd.item_count({}) == 0


def test_flags_point_at_labels_the_rules_may_not_reproduce() -> None:
    valid = [
        line("menu.nm", word("A", 1, 10, 10)),
        line("menu.cnt", word("2 x", 1, 100, 10)),
        line("menu.price", word("10,000", 1, 200, 10)),
        line("menu.sub_nm", word("ICE", 2, 20, 30)),
        line("menu.price", word("5,000", 3, 200, 50)),
        # The subtotal is annotated twice and the first annotation sits lower down.
        line(
            "sub_total.subtotal_price",
            word("SUBTOTAL", 6, 10, 110, key=1),
            word("15,500", 6, 200, 110),
        ),
        line(
            "sub_total.subtotal_price", word("ITEMS", 4, 10, 70, key=1), word("15,000", 4, 200, 70)
        ),
        line(
            "sub_total.tax_price",
            word("Pb1", 5, 10, 90),
            word("10%", 5, 60, 90),
            word("1,500", 5, 200, 90),
        ),
        line("total.total_etc", word("TOTAL", 7, 10, 130), word("16,500.00", 7, 200, 130)),
        line("void_menu.price", word("-5,000", 8, 200, 150)),
    ]
    parse = {
        "menu": [
            {"nm": "A", "cnt": "2 x", "price": "10,000", "sub": {"nm": "ICE"}},
            {"price": "5,000"},
        ],
        "sub_total": {"subtotal_price": ["15,500", "15,000"], "tax_price": "Pb1 10% 1,500"},
        "total": {"total_etc": "16,500.00"},
        "void_menu": {"price": "-5,000"},
    }
    parsed = fbd.parse_receipt(fbd.SourceRow("train", 0, ground_truth(valid, parse)))
    assert set(parsed.flags) == {
        "multi_value_disagree:subtotal",
        "first_not_topmost:subtotal",
        "multi_number:tax",
        "total_missing",
        "total_missing_total_word",
        "sub_items",
        "menu_entry_without_name",
        "quantity_over_1",
        "void_menu",
    }
    assert parsed.labels == {"total": None, "subtotal": 15500, "tax": 1101500, "item_count": 2}


def test_flags_for_decimals_signs_missing_digits_and_equal_repeats() -> None:
    valid = [
        line("menu.nm", word("TEA", 1, 10, 10)),
        line("sub_total.subtotal_price", word("226,500.00", 2, 200, 30)),
        line("sub_total.subtotal_price", word("226.500,00", 3, 200, 50)),
        line("sub_total.tax_price", word("( 1,236)", 4, 200, 70)),
        line("total.total_price", word("-", 5, 200, 90)),
    ]
    parse = {
        "menu": {"nm": "TEA"},
        "sub_total": {"subtotal_price": ["226,500.00", "226.500,00"], "tax_price": "( 1,236)"},
        "total": {"total_price": "-"},
    }
    parsed = fbd.parse_receipt(fbd.SourceRow("test", 3, ground_truth(valid, parse)))
    assert set(parsed.flags) == {
        "multi_value:subtotal",
        "decimal:subtotal",
        "sign:tax",
        "no_digit:total",
    }
    assert parsed.labels == {"total": None, "subtotal": 22650000, "tax": 1236, "item_count": 1}
    assert parsed.case_id == "cord-test-0003"


def test_a_malformed_annotation_is_a_data_error() -> None:
    with pytest.raises(fbd.DataError, match="train/4"):
        fbd.parse_receipt(fbd.SourceRow("train", 4, '{"gt_parse": {}}'))


# -- Duplicates and splits ----------------------------------------------------------------


def test_content_keys_ignore_layout_but_keep_amount_order() -> None:
    assert fbd.content_keys("SUB TOTAL 15,000\nCash: 20,000") == fbd.content_keys(
        "sub total 15,000 CASH 20,000"
    )
    swapped = (
        fbd.content_keys("TEA 8,000\nCOFFEE 12,000"),
        fbd.content_keys("COFFEE 12,000\nTEA 8,000"),
    )
    assert swapped[0][0] == swapped[1][0] and swapped[0][1] != swapped[1][1]
    renamed = (
        fbd.content_keys("TEA 8,000\nTOTAL 8,000"),
        fbd.content_keys("Kopi 8.000\nTotal 8.000"),
    )
    assert renamed[0][0] != renamed[1][0] and renamed[0][1] == renamed[1][1]
    assert fbd.content_keys("no digits here") == ("words:digits here no",)


def test_duplicates_keep_the_held_out_copy_and_conflicts_drop_the_group(small_splits: None) -> None:
    train = [receipt(f"ITEM {index}", 1_000 + 37 * index) for index in range(20)]
    # train 1 and train 2 print the same digits ("1" in the name, then the amounts), but
    # only train 1 annotates a total.
    train[2] = receipt("OTHER 1", 1_037, total=False)
    rows = source_rows(
        train,
        [receipt("VALIDATION", 50_000), receipt("SHARED", 60_000)],
        [receipt("ITEM 0", 1_000, separator="."), receipt("SHARED", 60_000, separator=".")],
    )
    derivation = fbd.derive(rows)
    excluded = {entry["source"]: entry for entry in derivation.excluded}
    assert excluded["train/0"] == {
        "source": "train/0",
        "reason": "duplicate",
        "kept": "cord-test-0000",
    }
    assert excluded["validation/1"]["kept"] == "cord-test-0001"
    assert excluded["train/1"]["reason"] == excluded["train/2"]["reason"] == "label_conflict"
    assert excluded["train/1"]["group"] == ["train/1", "train/2"]
    assert set(excluded) == {"train/0", "train/1", "train/2", "validation/1"}
    delivery = {case.case_id for case in derivation.splits["delivery"]}
    assert {"cord-test-0000", "cord-test-0001", "cord-validation-0000"} <= delivery
    assert len(delivery) == 3 + (20 - 3 - 13)


def test_splits_follow_text_sha256_and_never_overlap(small_splits: None) -> None:
    rows = source_rows(
        [receipt(f"ITEM {index}", 2_000 + 41 * index) for index in range(20)],
        [receipt("VALIDATION", 90_000)],
        [receipt("TEST", 91_000)],
    )
    first, again = fbd.derive(rows), fbd.derive(rows)
    assert first.splits == again.splits and first.excluded == again.excluded == ()
    by_sha = [
        case.case_id
        for case in sorted(
            (case for case in first.receipts if case.split == "train"),
            key=lambda case: hashlib.sha256(case.text.encode()).hexdigest(),
        )
    ]
    ids = {name: {case.case_id for case in cases} for name, cases in first.splits.items()}
    assert ids["pilot"] == set(by_sha[:3])
    assert ids["rehearsal"] == set(by_sha[3:7])
    assert ids["evidence"] == set(by_sha[7:13])
    assert ids["delivery"] == set(by_sha[13:]) | {"cord-validation-0000", "cord-test-0000"}
    owners: dict[str, str] = {}
    for name, cases in first.splits.items():
        assert [case.case_id for case in cases] == sorted(case.case_id for case in cases)
        for case in cases:
            for key in fbd.content_keys(case.text):
                assert owners.setdefault(key, name) == name


def test_too_few_distinct_train_receipts_is_an_error(small_splits: None) -> None:
    rows = source_rows([receipt(f"ITEM {index}", 3_000 + index) for index in range(12)], [], [])
    with pytest.raises(fbd.DataError, match="distinct train receipts"):
        fbd.derive(rows)


def _rows_with_copies() -> dict[str, list[Any]]:
    """20 distinct train receipts; train 3 and validation 0 are one purchase that neither
    key joins (one copy prints the cash line); train 6 and train 7 likewise, but only
    train 6 annotates its total; train 9 is train 8 with dots for commas (the keys join
    them)."""
    train = [receipt(f"ITEM {index}", 2_000 + 41 * index) for index in range(20)]
    train[3] = receipt("TEA", 5_000)
    train[6] = receipt("MILK", 7_000)
    train[7] = receipt("MILK", 7_000, total=False, cash=10_000)
    train[9] = receipt("ITEM 8", 2_328, separator=".")
    return source_rows(train, [receipt("TEA", 5_000, cash=10_000)], [receipt("TEST", 91_000)])


def test_near_duplicate_links_join_copies_the_keys_miss(
    small_splits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows_with_copies()
    assert fbd.derive(rows).excluded == (
        {"source": "train/9", "reason": "duplicate", "kept": "cord-train-0008"},
    )
    links = (("train/3", "validation/0", "the cash line"), ("train/6", "train/7", "the cash line"))
    monkeypatch.setattr(fbd, "NEAR_DUPLICATES", links)
    derivation = fbd.derive(rows)
    excluded = {entry["source"]: entry for entry in derivation.excluded}
    assert excluded["train/3"] == {
        "source": "train/3",
        "reason": "duplicate",
        "kept": "cord-validation-0000",
    }
    assert excluded["train/6"]["reason"] == excluded["train/7"]["reason"] == "label_conflict"
    assert excluded["train/7"]["group"] == ["train/6", "train/7"]
    assert "cord-validation-0000" in {case.case_id for case in derivation.splits["delivery"]}
    for bad, message in (
        (("train/3", "validation/9", ""), "no such source row"),
        (("train/8", "train/9", ""), "already one content"),
    ):
        monkeypatch.setattr(fbd, "NEAR_DUPLICATES", (*links, bad))
        with pytest.raises(fbd.DataError, match=message):
            fbd.derive(rows)


def test_receipts_left_out_by_hand_take_their_group_with_them(
    small_splits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows_with_copies()
    monkeypatch.setattr(fbd, "NEAR_DUPLICATES", (("train/6", "train/7", "the cash line"),))
    monkeypatch.setattr(
        fbd,
        "EXCLUDED",
        (
            ("train/8", "label_wrong", "the total is the item price"),
            ("train/7", "rule_conflict", "the total is not annotated"),
            ("train/12", "garbled_text", "the amounts sit in another row"),
        ),
    )
    derivation = fbd.derive(rows)
    excluded = {entry["source"]: entry for entry in derivation.excluded}
    assert excluded["train/8"] == {
        "source": "train/8",
        "reason": "label_wrong",
        "note": "the total is the item price",
    }
    assert excluded["train/9"] == {
        "source": "train/9",
        "reason": "label_wrong",
        "note": "a copy of train/8",
    }
    assert excluded["train/12"]["reason"] == "garbled_text"
    assert excluded["train/7"] == {
        "source": "train/7",
        "reason": "label_conflict",
        "group": ["train/6", "train/7"],
        "note": "the total is not annotated",
    }
    assert "note" not in excluded["train/6"]
    kept = {case.source for cases in derivation.splits.values() for case in cases}
    assert not kept & set(excluded) and len(kept) + len(excluded) == 22
    assert [len(derivation.splits[name]) for name, _size in SMALL_SPLITS] == [3, 4, 6]
    for listed, message in (
        (("train/99", "label_wrong", ""), "no such source row"),
        (("train/1", "typo", ""), "unknown reason"),
        (("train/8", "label_wrong", ""), "listed twice"),
    ):
        monkeypatch.setattr(fbd, "EXCLUDED", (*fbd.EXCLUDED, listed))
        with pytest.raises(fbd.DataError, match=message):
            fbd.derive(rows)
        monkeypatch.setattr(fbd, "EXCLUDED", fbd.EXCLUDED[:-1])


def test_partition_is_architects_even_odd_floor_split() -> None:
    cases = [
        EvaluationCase(case_id=f"cord-train-{index:04d}", task={"receipt": str(index)}, expected={})
        for index in (7, 3, 12, 1, 5, 9)
    ]
    definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="receipts")
    floor = HeldOutTasks(definition, cases)
    sides = fbd.partition(case.case_id for case in cases)
    assert {case.case_id for case in floor.tuning} == {
        case_id for case_id, side in sides.items() if side == "tuning"
    }
    assert list(sides.values()).count("held_out") == 3


# -- Files, manifests and verification ----------------------------------------------------


def test_render_write_verify_and_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    derivation, source, rows = synthetic(monkeypatch)
    files = fbd.render(derivation, source, rows)
    assert fbd.render(derivation, source, rows) == files
    assert all(b"\r" not in data for data in files.values())
    fbd.write(tmp_path, files)
    assert fbd.verify(tmp_path, files) == []
    pilot = tmp_path / "cases" / "pilot.jsonl"
    pilot.write_bytes(files["cases/pilot.jsonl"].replace(b"TOTAL", b"T0TAL", 1))
    (tmp_path / "manifests" / "source.json").unlink()
    (tmp_path / "floors" / "stale.json").write_text("[]", encoding="utf-8")
    assert fbd.verify(tmp_path, files) == [
        "differs cases/pilot.jsonl",
        "missing manifests/source.json",
        "unexpected floors/stale.json",
    ]
    fbd.write(tmp_path, files)
    assert fbd.verify(tmp_path, files) == []
    assert not (tmp_path / "floors" / "stale.json").exists()


def test_manifests_pin_files_cases_and_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    derivation, source, rows = synthetic(monkeypatch)
    files = fbd.render(derivation, source, rows)
    task = json.loads(files["task.json"])
    assert task["rules"] == fbd.PROMPT_RULES
    assert task["rules_sha256"] == hashlib.sha256(fbd.PROMPT_RULES.encode()).hexdigest()
    excluded = json.loads(files["manifests/excluded.json"])
    assert excluded["count"] == 0 and excluded["receipts"] == []
    assert excluded["near_duplicates"] == [] and excluded["meanings"] == {}
    for name in SETS:
        manifest = json.loads(files[f"manifests/{name}.json"])
        assert manifest["dataset_revision"] == fbd.REVISION
        assert manifest["rule_version"] == fbd.RULE_VERSION
        assert manifest["rules_sha256"] == task["rules_sha256"]
        data = files[f"cases/{name}.jsonl"]
        cases = [EvaluationCase.model_validate_json(text) for text in data.decode().splitlines()]
        listed = manifest["files"][f"cases/{name}.jsonl"]
        assert listed["sha256"] == hashlib.sha256(data).hexdigest()
        assert listed["cases_tag"] == "cases:" + listed["sha256"][:16]
        assert manifest["cases_sha256"] == architect_cases_sha256(cases)
        assert [entry["case_id"] for entry in manifest["cases"]] == [case.case_id for case in cases]
        for entry, case in zip(manifest["cases"], cases, strict=True):
            dumped = architect_canonical_json(case.model_dump(mode="json")).encode()
            assert entry["case_sha256"] == hashlib.sha256(dumped).hexdigest()
            assert entry["text_sha256"] == hashlib.sha256(case.task["receipt"].encode()).hexdigest()


def test_main_writes_then_verifies_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _derivation, source, rows = synthetic(monkeypatch)
    monkeypatch.setattr(fbd, "cache_complete", lambda cache: True)
    monkeypatch.setattr(fbd, "read_cache", lambda cache: (rows, source))
    arguments = ["--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache"), "--offline"]
    assert fbd.main(arguments) == 0
    assert fbd.main([*arguments, "--verify"]) == 0
    (tmp_path / "out" / "stats.json").write_text("{}", encoding="utf-8")
    assert fbd.main([*arguments, "--verify"]) == 1
    assert "differs stats.json" in capsys.readouterr().out
    monkeypatch.setattr(fbd, "cache_complete", lambda cache: False)
    assert fbd.main([*arguments, "--verify"]) == 2
    assert "incomplete" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        fbd.main(["--refetch", "--offline"])


# -- The committed data (examples/receipts) -----------------------------------------------


def _manifest(name: str) -> dict[str, Any]:
    return json.loads((DATA / "manifests" / f"{name}.json").read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


def test_committed_sets_match_their_manifests_and_rules() -> None:
    task = json.loads((DATA / "task.json").read_text(encoding="utf-8"))
    assert task["rules"] == fbd.PROMPT_RULES and task["rule_version"] == fbd.RULE_VERSION
    assert f"```text\n{fbd.PROMPT_RULES}\n```" in (DATA / "RULES.md").read_text(encoding="utf-8")
    sizes = dict(fbd.SPLITS)
    assert sizes == {"pilot": 150, "rehearsal": 100, "evidence": 200}
    for name in SETS:
        manifest = _manifest(name)
        assert manifest["dataset_revision"] == fbd.REVISION and manifest["license"] == "CC-BY-4.0"
        assert manifest["rules_sha256"] == task["rules_sha256"]
        assert manifest["rule_version"] == task["rule_version"]
        for relative, listed in manifest["files"].items():
            assert hashlib.sha256((DATA / relative).read_bytes()).hexdigest() == listed["sha256"]
        cases = [
            EvaluationCase.model_validate(row) for row in _jsonl(DATA / "cases" / f"{name}.jsonl")
        ]
        assert manifest["count"] == len(cases) == sizes.get(name, len(cases))
        assert manifest["cases_sha256"] == architect_cases_sha256(cases)
        assert [entry["case_id"] for entry in manifest["cases"]] == [case.case_id for case in cases]
        for entry, case in zip(manifest["cases"], cases, strict=True):
            assert entry["text_sha256"] == hashlib.sha256(case.task["receipt"].encode()).hexdigest()


def test_committed_sets_are_disjoint_and_account_for_every_source_row() -> None:
    source = json.loads((DATA / "manifests" / "source.json").read_text(encoding="utf-8"))
    digests = source["ground_truth_sha256"]
    assert {split: len(rows) for split, rows in digests.items()} == fbd.SPLIT_SIZES
    seen: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name in SETS:
        texts = {
            row["case_id"]: row["task"]["receipt"]
            for row in _jsonl(DATA / "cases" / f"{name}.jsonl")
        }
        for entry in _manifest(name)["cases"]:
            split, index = entry["source"].split("/")
            assert entry["case_id"] == f"cord-{split}-{int(index):04d}"
            assert entry["ground_truth_sha256"] == digests[split][int(index)]
            assert name == "delivery" or split == "train"
            assert seen.setdefault(entry["source"], name) == name
            for key in fbd.content_keys(texts[entry["case_id"]]):  # no two cases share content
                assert owners.setdefault(key, entry["case_id"]) == entry["case_id"]
    excluded = json.loads((DATA / "manifests" / "excluded.json").read_text(encoding="utf-8"))
    for entry in excluded["receipts"]:
        split, index = entry["source"].split("/")
        assert entry["ground_truth_sha256"] == digests[split][int(index)]
        assert seen.setdefault(entry["source"], "excluded") == "excluded"
    assert len(seen) == sum(fbd.SPLIT_SIZES.values())


def test_committed_exclusions_and_near_duplicates_are_recorded() -> None:
    excluded = json.loads((DATA / "manifests" / "excluded.json").read_text(encoding="utf-8"))
    entries = {entry["source"]: entry for entry in excluded["receipts"]}
    assert excluded["count"] == len(entries) == sum(excluded["reasons"].values())
    assert set(excluded["meanings"]) == set(excluded["reasons"])
    for source, reason, note in fbd.EXCLUDED:  # left out, with the hand reason or as a conflict
        entry = entries[source]
        assert (entry["reason"], entry["note"]) == (reason, note) or (
            entry["reason"] == "label_conflict" and entry["note"] == note
        )
    assert excluded["near_duplicates"] == [
        {"sources": [a, b], "differs": differs} for a, b, differs in fbd.NEAR_DUPLICATES
    ]
    placed = {entry["source"]: name for name in SETS for entry in _manifest(name)["cases"]}
    for a, b, _differs in fbd.NEAR_DUPLICATES:  # never both in the sets
        assert not (a in placed and b in placed)
        assert a in entries or b in entries
    # The stacked rows the text rebuild splits (AUDIT.md section 5.3): every kept one is
    # flagged, and the garbled ones it could not fix stay out.
    flagged = {
        entry["case_id"]
        for name in SETS
        for entry in _manifest(name)["cases"]
        if "row_split" in entry["flags"]
    }
    assert flagged == {
        "cord-test-0011",
        "cord-test-0045",
        "cord-test-0072",
        "cord-test-0096",
        "cord-train-0382",
        "cord-train-0400",
        "cord-train-0516",
        "cord-train-0596",
        "cord-train-0614",
        "cord-train-0759",
        "cord-validation-0047",
    }
    assert {entries[s]["reason"] for s in ("train/293", "train/425", "train/568")} == {
        "garbled_text"
    }


def test_committed_train_sets_follow_text_sha256_order() -> None:
    ranges = []
    for name in SETS:
        shas = sorted(
            entry["text_sha256"]
            for entry in _manifest(name)["cases"]
            if entry["source"].startswith("train/")
        )
        ranges.append((shas[0], shas[-1]))
    for (_low, high), (low, _high) in zip(ranges, ranges[1:], strict=False):
        assert high < low


def test_committed_files_load_with_architects_floor_loaders() -> None:
    task = json.loads((DATA / "task.json").read_text(encoding="utf-8"))
    inputs = Draft202012Validator(task["input_schema"])
    outputs = Draft202012Validator(task["output_schema"])
    for name in SETS:
        manifest = _manifest(name)
        cases, tag = load_forge_cases(DATA / "cases" / f"{name}.jsonl")
        assert tag == manifest["files"][f"cases/{name}.jsonl"]["cases_tag"]
        assert all(inputs.is_valid(case.task) and outputs.is_valid(case.expected) for case in cases)
        if name not in fbd.FLOORS:
            assert set(manifest["files"]) == {f"cases/{name}.jsonl"}
            continue
        floor_cases = load_floor_cases(DATA / "floors" / f"{name}.json")
        assert floor_cases == list(cases)
        definition = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset=name)
        tuning = {case.case_id for case in HeldOutTasks(definition, floor_cases).tuning}
        listed = {entry["case_id"] for entry in manifest["cases"] if entry["partition"] == "tuning"}
        assert tuning == listed
        assert len(tuning) == manifest["partition"]["tuning"] == len(floor_cases) // 2


def test_the_notice_credits_cord_and_says_no_images() -> None:
    notice = (DATA / "NOTICE.md").read_text(encoding="utf-8")
    for needle in (
        "CC BY 4.0",
        "NAVER Clova",
        fbd.REVISION,
        fbd.DATASET_URL,
        "https://creativecommons.org/licenses/by/4.0/",
        "Text rebuilt from annotations; no images.",
    ):
        assert needle in notice


def _cache_ready() -> bool:
    try:
        return bool(fbd.cache_complete(fbd.default_cache()))
    except fbd.DataError:
        return False


@pytest.mark.skipif(not _cache_ready(), reason="no raw CORD cache here (run the script once)")
def test_committed_files_rederive_from_the_raw_cache() -> None:
    rows, source = fbd.read_cache(fbd.default_cache())
    files = fbd.render(fbd.derive(rows), source, rows)
    assert fbd.verify(DATA, files) == []
