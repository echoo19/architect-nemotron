"""Declared rule checks (``architect/presets/checks.py``) against the frozen reference.

The receipt reader's spec declares two checks, digits first, then the copy check. Here they
answer exactly as the frozen functions (``docs/bench/eval-2026-10-06/frozen_checks.py``,
their source hashes checked first): on every reply plain Lightning and plain Super gave on
the 100 rehearsal receipts on 2026-10-06 (gate 1, ``tests/fixtures/checks``), on generated
receipts and on edge cases. They change no label of the pilot, the rehearsal floor or the
evidence floor's practice half. The exam half is never parsed, scored or printed: its lines
are skipped by their leading case id before any JSON is read.

Also: checks run only when declared, after the reply and before the output schema; a
declaration that names a field the spec does not have is refused; the spec hash covers
them; the emitted ``harness.py`` carries them; and a spec without checks emits the same
bytes as before (the 2026-10-05 ladder's artifact digests rebuild).
Offline: scripted adapters only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import random
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import JsonValue

from architect.floors.checkers import receipt_v1
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.presets import PresetError, execute_preset, get_preset
from architect.presets._json import parse_reply
from architect.presets.checks import (
    CHECKS_KEY,
    Check,
    KeepDigits,
    NotCopied,
    apply_check,
    bind,
    check_declaration,
    declared,
    rule_sentences,
)
from architect.runtime import RunContext, load_harness
from architect.spec import HarnessArtifact, PipelineStep, Spec

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "examples" / "receipts"
OCT5 = ROOT / "examples" / "bench" / "eval-2026-10-05" / "ladder"
OCT6 = ROOT / "examples" / "bench" / "eval-2026-10-06" / "ladder"
APP_SPEC = ROOT / "examples" / "expense-app" / "receipt-reader.json"
FROZEN = ROOT / "docs" / "bench" / "eval-2026-10-06" / "frozen_checks.py"
REPLIES = Path(__file__).resolve().parent / "fixtures" / "checks" / "gate1-replies.jsonl"

OCT5_HASH = "ff985dd9d7cdea94938fabb5fc6ec909c28996cd6e7c693af60b2778cf6ebfdc"
OCT6_HASH = "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb"
# docs/bench/prereg-nebius.md, "Changes after publication", 2026-10-06.
FUNCTION_SHA256 = {
    "guard_not_total_copy": "026ee79d7a94d7b635dd38f2bdc0b2f39c6d73a1eaebc4197090498044bc8e33",
    "guard_digits": "b2bf2c7f2995072023141c7afc885b5ac3299c4c4ae3346aab9ebc6612f047bf",
}
RULES = (
    'Amounts keep every printed digit, so "226,500.00" is 22650000.',
    "A subtotal equal to the total is null unless the receipt prints that amount as a subtotal.",
)
LEADING_ID = re.compile(r'^\{"case_id":"([^"]+)"')


# ------------------------------------------------------------------ fixtures


def _frozen() -> ModuleType:
    """The public frozen reference, after both function hashes match the plan's."""
    spec = importlib.util.spec_from_file_location("frozen_checks_reference", FROZEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, want in FUNCTION_SHA256.items():
        source = inspect.getsource(getattr(module, name)).replace("\r\n", "\n")
        assert hashlib.sha256(source.encode("utf-8")).hexdigest() == want, name
    return module


@pytest.fixture(scope="module")
def frozen() -> ModuleType:
    return _frozen()


def _spec_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def checks() -> tuple[Check, ...]:
    return declared(_spec_json(OCT6 / "spec.json")["execution_config"])


def _cases(split: str) -> list[dict[str, Any]]:
    path = RECEIPTS / "cases" / f"{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _evidence_practice() -> list[dict[str, Any]]:
    """The evidence floor's practice half, read without parsing its exam half.

    The floor gives sorted case ids at even positions to practice
    (``architect/floors/base.py``). The ids come from the manifest; a line of the cases
    file whose leading case id is not a practice id is skipped unparsed.
    """
    manifest = json.loads((RECEIPTS / "manifests" / "evidence.json").read_text("utf-8"))
    ordered = sorted(case["case_id"] for case in manifest["cases"])
    assert len(ordered) == 200
    practice = set(ordered[::2])
    rows: list[dict[str, Any]] = []
    with (RECEIPTS / "cases" / "evidence.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            found = LEADING_ID.match(line)
            assert found is not None, "a line of the evidence cases has no leading case id"
            if found.group(1) in practice:
                rows.append(json.loads(line))
    assert len(rows) == 100 and {row["case_id"] for row in rows} == practice
    return rows


def _replies() -> list[dict[str, str]]:
    rows = [json.loads(line) for line in REPLIES.read_text("utf-8").splitlines() if line]
    return rows


def _dump(value: object) -> str:
    """Canonical JSON, so 1 and true or 1 and 1.0 never compare equal."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _frozen_order(frozen: ModuleType, digits_first: bool) -> Callable[[str, Any], Any]:
    def run(text: str, out: Any) -> Any:
        if digits_first:
            return frozen.guard_not_total_copy(text, frozen.guard_digits(text, dict(out)))
        return frozen.guard_digits(text, frozen.guard_not_total_copy(text, dict(out)))

    return run


# ------------------------------------------------------------------ the declaration


def test_the_public_reference_is_the_frozen_code_without_a_local_path(
    frozen: ModuleType,
) -> None:
    text = FROZEN.read_text(encoding="utf-8")
    for marker in ("C:/", "C:\\", "Users", "AppData", "scratchpad", "/home/"):
        assert marker not in text
    plan = (ROOT / "docs" / "bench" / "prereg-nebius.md").read_text(encoding="utf-8")
    assert all(digest in plan for digest in FUNCTION_SHA256.values())
    assert callable(frozen.guard_digits) and callable(frozen.guard_not_total_copy)


def test_the_receipt_spec_declares_digits_first_then_the_copy_check(
    frozen: ModuleType, checks: tuple[Check, ...]
) -> None:
    digits, copy = checks
    assert isinstance(digits, KeepDigits) and isinstance(copy, NotCopied)
    assert (digits.input, digits.fields) == ("receipt", ("total", "subtotal", "tax"))
    assert (copy.input, copy.field, copy.copy_of) == ("receipt", "subtotal", "total")
    # The words are the frozen functions' own patterns, matched the same way.
    assert copy.named == frozen.SUB_WORDS.pattern
    assert copy.followed is not None
    assert (copy.followed.line, copy.followed.by) == (frozen.TOTALW.pattern, frozen.GRAND.pattern)
    for pattern in (frozen.SUB_WORDS, frozen.TOTALW, frozen.GRAND):
        assert pattern.flags == re.compile(pattern.pattern, re.IGNORECASE).flags
    assert copy.bare_amount is True
    assert rule_sentences(_spec_json(OCT6 / "spec.json")["execution_config"]) == RULES


def test_architects_check_code_holds_no_receipt_word() -> None:
    source = (ROOT / "architect" / "presets" / "checks.py").read_text(encoding="utf-8")
    words = r"\b(sub\W*total|total|grand|harga|jual|dpp|receipts?|tax|items?|item_count)\b"
    assert re.search(words, source, re.IGNORECASE) is None


def test_the_new_spec_is_the_oct5_spec_plus_the_checks_and_the_app_uses_it() -> None:
    old, new = _spec_json(OCT5 / "spec.json"), _spec_json(OCT6 / "spec.json")
    assert old["execution_config"] == {} and set(new["execution_config"]) == {CHECKS_KEY}
    assert {**new, "execution_config": {}} == old
    assert Spec.model_validate(old).spec_hash == OCT5_HASH
    assert Spec.model_validate(new).spec_hash == OCT6_HASH
    assert APP_SPEC.read_bytes() == (OCT6 / "spec.json").read_bytes()


def test_the_spec_hash_covers_the_checks() -> None:
    base = _spec_json(OCT6 / "spec.json")
    declared_checks = base["execution_config"][CHECKS_KEY]

    def hashed(checks: list[Any]) -> str:
        return Spec.model_validate({**base, "execution_config": {CHECKS_KEY: checks}}).spec_hash

    reworded = [{**declared_checks[0], "rule": "Amounts keep their digits."}, declared_checks[1]]
    narrower = [declared_checks[0], {**declared_checks[1], "named": "subtotal"}]
    variants = {
        hashed(declared_checks),
        hashed(declared_checks[::-1]),
        hashed(declared_checks[:1]),
        hashed(reworded),
        hashed(narrower),
        Spec.model_validate({**base, "execution_config": {}}).spec_hash,
    }
    assert len(variants) == 6 and OCT6_HASH in variants and OCT5_HASH in variants


@pytest.mark.parametrize(
    "bad",
    [
        {"checks": "keep_digits"},
        {"checks": [{"check": "spell_check", "rule": "x", "input": "receipt"}]},
        {"checks": [{"check": "keep_digits", "rule": " ", "input": "r", "fields": ["a"]}]},
        {"checks": [{"check": "keep_digits", "rule": "x", "input": "r", "fields": []}]},
        {"checks": [{"check": "keep_digits", "rule": "x", "input": "r", "fields": ["a"],
                     "extra": 1}]},
        {"checks": [{"check": "not_copied", "rule": "x", "input": "r", "field": "a",
                     "copy_of": "b", "named": "("}]},
        {"checks": [{"check": "not_copied", "rule": "x", "input": "r", "field": "a",
                     "copy_of": "b", "followed": {"line": "t"}}]},
        {"checks": [{"check": "not_copied", "rule": "x", "input": "r", "field": "a",
                     "copy_of": "b", "bare_amount": "yes"}]},
    ],
)
def test_a_declaration_that_is_not_valid_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(PresetError, match="Invalid checks declaration"):
        declared(bad)
    spec = Spec.model_validate({**_spec_json(OCT5 / "spec.json"), "execution_config": bad})
    with pytest.raises(PresetError):
        get_preset("extractor").build(spec)


def test_no_declaration_is_no_check() -> None:
    assert declared({}) == () and declared({"kind": "standard"}) == ()
    assert declared({CHECKS_KEY: []}) == () and rule_sentences({}) == ()
    assert rule_sentences(
        {CHECKS_KEY: [{"check": "keep_digits", "rule": " Keep digits ", "input": "t",
                       "fields": ["n"]}]}
    ) == ("Keep digits.",)


# ------------------------------------------------------------------ equivalence


def test_every_gate1_reply_gets_the_frozen_answer(
    frozen: ModuleType, checks: tuple[Check, ...]
) -> None:
    """All 200 replies, through the parse the spec uses: both orders, each check alone.
    The counts are gate 1's: Lightning 29 to 41, Super 50 to 64, and with the copy check
    first one receipt fewer each, cord-train-0447."""
    cases = {case["case_id"]: case for case in _cases("rehearsal")}
    replies = _replies()
    assert len(replies) == 200 and {row["case_id"] for row in replies} == set(cases)
    digits, copy = checks
    declared_order = _frozen_order(frozen, digits_first=True)
    copy_first = _frozen_order(frozen, digits_first=False)
    tally: dict[str, dict[str, Any]] = {}
    for row in replies:
        case = cases[row["case_id"]]
        text, expected = case["task"]["receipt"], case["expected"]
        out = parse_reply(row["reply"], "first")
        ours = bind(checks, case["task"])(out)
        assert _dump(ours) == _dump(declared_order(text, out)), row["case_id"]
        reversed_ = bind(checks[::-1], case["task"])(out)
        assert _dump(reversed_) == _dump(copy_first(text, out)), row["case_id"]
        assert _dump(apply_check(digits, text, out)) == _dump(frozen.guard_digits(text, out))
        assert _dump(apply_check(copy, text, out)) == _dump(
            frozen.guard_not_total_copy(text, out))
        counts = tally.setdefault(
            row["model"], {"before": 0, "after": 0, "copy_first": 0, "changed": 0, "lost": []})
        counts["before"] += receipt_v1(expected, out)
        counts["after"] += receipt_v1(expected, ours)
        counts["copy_first"] += receipt_v1(expected, reversed_)
        counts["changed"] += _dump(ours) != _dump(out)
        if receipt_v1(expected, ours) and not receipt_v1(expected, reversed_):
            counts["lost"].append(row["case_id"])
        # A check never turns a right answer wrong.
        assert receipt_v1(expected, ours) or not receipt_v1(expected, out)
    assert tally == {
        "lightning": {"before": 29, "after": 41, "copy_first": 40, "changed": 23,
                      "lost": ["cord-train-0447"]},
        "super": {"before": 50, "after": 64, "copy_first": 63, "changed": 17,
                  "lost": ["cord-train-0447"]},
    }


def test_the_order_matters_on_cord_train_0447(
    frozen: ModuleType, checks: tuple[Check, ...]
) -> None:
    """It prints "Subtotal: Rp 29000.00" and "Total: Rp 29000.00", and both models read
    29000 twice. Digits first repairs both values. The copy check first nulls the
    subtotal: its line prints 29000.00, which is 2900000, not 29000."""
    [case] = [case for case in _cases("rehearsal") if case["case_id"] == "cord-train-0447"]
    text = case["task"]["receipt"]
    assert "Subtotal: Rp 29000.00" in text and "Total: Rp 29000.00" in text
    for row in [row for row in _replies() if row["case_id"] == "cord-train-0447"]:
        out = parse_reply(row["reply"], "first")
        assert (out["total"], out["subtotal"]) == (29000, 29000)
        right = bind(checks, case["task"])(out)
        wrong = bind(checks[::-1], case["task"])(out)
        assert right == case["expected"] == _frozen_order(frozen, True)(text, out)
        assert (right["total"], right["subtotal"]) == (2900000, 2900000)
        assert wrong == _frozen_order(frozen, False)(text, out)
        assert (wrong["total"], wrong["subtotal"]) == (2900000, None)


_WORDS = (
    "SUBTOTAL", "Sub Total", "sub-total", "SUB_TOTAL", "Sub. Total", "ITEMS TOTAL",
    "Item Total", "HARGA JUAL", "DPP", "NET TOTAL", "ITEMS", "items", "Item", "TOTAL",
    "Total", "total:", "GRAND TOTAL", "GrandTotal", "Grand  Total", "TAX", "PB1 10%",
    "CASH", "CHANGE", "Rp", "Kopi Susu", "NASI", "Qty", "x", "Disc", "TOTAL QTY", "SUBTOTALS",
    "dppx", "subitem", "-", "@", "Tl", "network total",
)


def _amount(rng: random.Random, base: int) -> str:
    style = rng.randrange(10)
    if style == 0:
        return str(base)
    if style == 1:
        return f"{base:,}"
    if style == 2:
        return f"{base:,}".replace(",", ".")
    if style == 3:
        return f"{base:,}.00"
    if style == 4:
        return f"{base:,}".replace(",", ".") + ",00"
    if style == 5:
        return f"-{base:,}"
    if style == 6:
        return f"({base:,})"
    if style == 7:
        return f"{base}.{rng.randrange(10)}"
    if style == 8:
        return f"{base // 1000} {base % 1000:03d}"
    return "".join(chr(0x0660 + int(digit)) for digit in str(base)) + ".00"


def _receipt(rng: random.Random) -> str:
    lines: list[str] = []
    for _ in range(rng.randrange(0, 9)):
        kind = rng.randrange(6)
        if kind == 0:
            lines.append("")
        elif kind == 1:
            lines.append(_amount(rng, rng.choice((500, 12_500, 29_000, 226_500))))
        else:
            parts = [rng.choice(_WORDS) for _ in range(rng.randrange(0, 3))]
            parts += [_amount(rng, rng.choice((7, 500, 12_500, 29_000, 226_500, 1_234_567)))
                      for _ in range(rng.randrange(0, 3))]
            rng.shuffle(parts)
            lines.append(" ".join(parts) + ("\r" if rng.random() < 0.1 else ""))
    return "\n".join(lines)


def _output(rng: random.Random, text: str) -> dict[str, JsonValue]:
    tokens = re.findall(r"\d[\d.,]*", text)
    pool: list[JsonValue] = [None, rng.randrange(10**7), True, False, 29000.0, "29000"]
    for token in tokens:
        digits = int(re.sub(r"\D", "", token))
        pool += [digits, digits // 100, digits // 10]
    out: dict[str, JsonValue] = {}
    for field in ("total", "subtotal", "tax", "item_count"):
        if rng.random() < 0.9:
            out[field] = rng.choice(pool)
    if "total" in out and rng.random() < 0.5:
        out["subtotal"] = out["total"]  # the copy the rules forbid
    return out


def _generated(count: int, seed: int) -> Iterator[tuple[str, dict[str, JsonValue]]]:
    rng = random.Random(seed)
    for _ in range(count):
        text = _receipt(rng)
        yield text, _output(rng, text)


def test_generated_receipts_get_the_frozen_answer(
    frozen: ModuleType, checks: tuple[Check, ...]
) -> None:
    digits, copy = checks
    changed = 0
    for text, out in _generated(4000, seed=20261006):
        task: dict[str, JsonValue] = {"receipt": text}
        before = _dump(out)
        ours = bind(checks, task)(out)
        assert _dump(ours) == _dump(_frozen_order(frozen, True)(text, out)), (text, out)
        assert _dump(bind(checks[::-1], task)(out)) == _dump(
            _frozen_order(frozen, False)(text, out)), (text, out)
        assert _dump(apply_check(digits, text, out)) == _dump(frozen.guard_digits(text, out))
        assert _dump(apply_check(copy, text, out)) == _dump(
            frozen.guard_not_total_copy(text, out))
        assert _dump(out) == before  # the value read from the reply is never changed in place
        changed += _dump(ours) != before
    assert changed > 400  # the generator reaches both checks


@pytest.mark.parametrize(
    ("text", "out", "want"),
    [
        # A copy with no line that may print it.
        ("", {"total": 5, "subtotal": 5}, {"total": 5, "subtotal": None}),
        ("1 KOPI 5.000\nTOTAL 5.000", {"total": 5000, "subtotal": 5000},
         {"total": 5000, "subtotal": None}),
        # A subtotal line prints it, whatever its case or spacing.
        ("1 KOPI 5.000\nsub total 5.000\nTOTAL 5.000", {"total": 5000, "subtotal": 5000}, None),
        ("1 KOPI 5.000\r\nSubTotal 5.000\r\nTOTAL 5.000\r", {"total": 5000, "subtotal": 5000},
         None),
        # A TOTAL line that a later GRAND TOTAL follows; not one after it.
        ("TOTAL 5.000\nTAX 500\nGRAND TOTAL 5.500", {"total": 5000, "subtotal": 5000}, None),
        ("GRAND TOTAL 5.000\nTOTAL 5.000", {"total": 5000, "subtotal": 5000},
         {"total": 5000, "subtotal": None}),
        ("Grand Total 5.000\nGrandTotal 5.000", {"total": 5000, "subtotal": 5000},
         {"total": 5000, "subtotal": None}),
        # A bare amount line; a line with a letter is not one.
        ("1 KOPI 5.000\n5.000\nTOTAL 5.000", {"total": 5000, "subtotal": 5000}, None),
        ("1 KOPI 5.000\nx 5.000\nTOTAL 5.000", {"total": 5000, "subtotal": 5000},
         {"total": 5000, "subtotal": None}),
        # Different values, a null subtotal and a missing field are left alone.
        ("TOTAL 5.000", {"total": 5000, "subtotal": 4000}, None),
        ("TOTAL 5.000", {"total": 5000, "subtotal": None}, None),
        ("TOTAL 5.000", {"total": 5000}, None),
        ("TOTAL 5.000", {}, None),
        # Dropped decimals take every digit; one decimal or a full value does not.
        ("Total 226,500.00", {"total": 226500}, {"total": 22650000}),
        ("Total 226,500.00", {"total": 22650000}, None),
        ("Total 226,500.0", {"total": 226500}, None),
        ("Tax 1.234,56\nTotal 9", {"tax": 1234}, {"tax": 123456}),
        # The first amount that fits wins.
        ("A 1.50\nB 1,55", {"total": 1}, {"total": 150}),
        # Digits are any digits, as in the frozen code.
        ("Total \u0661\u0662\u0663.00", {"total": 123}, {"total": 12300}),
        # A field the declaration does not list is never touched.
        ("Total 226,500.00", {"item_count": 226500}, None),
        # The digits check reads true as 1 and 29000.0 as 29000, as Python does.
        ("Total 1.00", {"total": True}, {"total": 100}),
        ("Total 29,000.00", {"total": 29000.0}, {"total": 2900000}),
        ("Total 29,000.00", {"total": "29000"}, None),
    ],
)
def test_edge_cases_match_the_frozen_functions(
    frozen: ModuleType,
    checks: tuple[Check, ...],
    text: str,
    out: dict[str, JsonValue],
    want: dict[str, JsonValue] | None,
) -> None:
    ours = bind(checks, {"receipt": text})(dict(out))
    assert _dump(ours) == _dump(want if want is not None else out)
    assert _dump(ours) == _dump(_frozen_order(frozen, True)(text, out))


def test_values_the_frozen_functions_cannot_take_are_left_for_the_schema(
    frozen: ModuleType, checks: tuple[Check, ...]
) -> None:
    run = bind(checks, {"receipt": "Total 226,500.00"})
    for value in ([226500], "226500", 226500.5, None):
        assert run(value) == value  # not an object: the output schema decides
    listed: dict[str, JsonValue] = {"total": [226500], "subtotal": {"a": 1}}
    assert run(listed) == listed
    with pytest.raises(TypeError):
        frozen.guard_digits("Total 226,500.00", listed)  # unhashable: the frozen code stops


# ------------------------------------------------------------------ labels


@pytest.mark.parametrize("split", ["pilot", "rehearsal", "evidence practice"])
def test_no_label_changes(
    frozen: ModuleType, checks: tuple[Check, ...], split: str
) -> None:
    cases = _evidence_practice() if split == "evidence practice" else _cases(split)
    assert len(cases) == {"pilot": 150, "rehearsal": 100, "evidence practice": 100}[split]
    for case in cases:
        expected = case["expected"]
        assert bind(checks, case["task"])(dict(expected)) == expected, case["case_id"]
        assert _frozen_order(frozen, True)(case["task"]["receipt"], expected) == expected


# ------------------------------------------------------------------ in execute()


class Scripted(FakeAdapter):
    """Answers each call with the next scripted text and keeps every request."""

    def __init__(self, *texts: str) -> None:
        super().__init__(responses=[
            ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=5, cost_usd=1e-5)
            for text in texts
        ])
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


NO_SUBTOTAL = "2 ROTI BAKAR 36.000\n1 KOPI SUSU 15.000\nTOTAL 51.000\nTUNAI 100.000"
COPIED = '{"total": 51000, "subtotal": 51000, "tax": null, "item_count": 2}'
CHECKED = {"total": 51000, "subtotal": None, "tax": None, "item_count": 2}
PLAIN = {"verifier_depth": 0, "retries": 0}


def _oct(path: Path, **changes: Any) -> Spec:
    return Spec.model_validate({**_spec_json(path / "spec.json"), **changes})


async def _run(spec: Spec, adapter: Scripted, params: dict[str, JsonValue] = PLAIN,
               task: JsonValue | None = None) -> JsonValue:
    harness = get_preset("extractor").build(spec, params)
    return await harness(RunContext(adapter, spec.budget), task or {"receipt": NO_SUBTOTAL})


async def test_checks_run_only_when_declared() -> None:
    assert await _run(_oct(OCT6), Scripted(COPIED)) == CHECKED
    assert await _run(_oct(OCT5), Scripted(COPIED)) == json.loads(COPIED)


async def test_every_gate1_reply_through_the_harness_gets_the_frozen_answer(
    frozen: ModuleType,
) -> None:
    spec = _oct(OCT6)
    cases = {case["case_id"]: case for case in _cases("rehearsal")}
    harness = get_preset("extractor").build(spec, PLAIN)
    for row in _replies():
        case = cases[row["case_id"]]
        adapter = Scripted(row["reply"])
        got = await harness(RunContext(adapter, spec.budget), case["task"])
        want = _frozen_order(frozen, True)(case["task"]["receipt"],
                                           parse_reply(row["reply"], "first"))
        assert _dump(got) == _dump(want), row["case_id"]
        assert adapter.calls == 1


async def test_the_output_schema_is_checked_after_the_checks() -> None:
    schema = _spec_json(OCT6 / "spec.json")["output_schema"]
    strict = {**schema, "properties": {**schema["properties"], "subtotal": {"type": "integer"}}}
    with pytest.raises(PresetError, match="output or input schema"):
        await _run(_oct(OCT6, output_schema=strict), Scripted(COPIED))


async def test_the_self_check_and_the_answer_see_the_checked_value() -> None:
    adapter = Scripted(COPIED, '{"passed": true, "reason": "ok"}')
    params: dict[str, JsonValue] = {"verifier_depth": 1, "retries": 0}
    assert await _run(_oct(OCT6), adapter, params) == CHECKED
    verify = json.loads(adapter.requests[1].prompt)
    assert verify["operation"] == "verify_candidate" and verify["candidate"] == CHECKED


async def test_a_task_without_the_declared_text_stops_before_any_call() -> None:
    # The spec names the input field, but this task does not hold it as text.
    schema: dict[str, JsonValue] = {
        "type": "object", "properties": {"receipt": {}, "note": {"type": "string"}}}
    for task in ({"note": "x"}, {"receipt": 5, "note": "x"}):
        adapter = Scripted(COPIED)
        with pytest.raises(PresetError, match="input field as text"):
            await _run(_oct(OCT6, input_schema=schema), adapter, task=task)
        assert adapter.calls == 0


_DECLARED = _spec_json(OCT6 / "spec.json")["execution_config"][CHECKS_KEY]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"input_schema": {"type": "object", "properties": {"note": {"type": "string"}}}},
         "reads an input field"),
        ({"input_schema": {"type": "object"}}, "reads an input field"),
        ({"execution_config": {CHECKS_KEY: [{**_DECLARED[0], "input": "receit"}, _DECLARED[1]]}},
         "reads an input field"),
        ({"execution_config": {CHECKS_KEY: [
            {**_DECLARED[0], "fields": ["total", "subtotl", "tax"]}, _DECLARED[1]]}},
         "names an output field"),
        ({"execution_config": {CHECKS_KEY: [_DECLARED[0], {**_DECLARED[1], "field": "sub"}]}},
         "names an output field"),
        ({"execution_config": {CHECKS_KEY: [_DECLARED[0], {**_DECLARED[1], "copy_of": "totl"}]}},
         "names an output field"),
        ({"output_schema": {"type": "string"}}, "names an output field"),
    ],
)
async def test_a_declaration_naming_a_field_the_spec_lacks_is_refused(
    changes: dict[str, Any], message: str
) -> None:
    """A misspelled field would leave a rule on the plan that no code applies: the preset
    is not built, and a run of such a spec stops before any call."""
    spec = _oct(OCT6, **changes)
    with pytest.raises(PresetError, match=message):
        check_declaration(spec)
    with pytest.raises(PresetError, match=message):
        get_preset("extractor").build(spec, PLAIN)
    adapter = Scripted(COPIED)
    with pytest.raises(PresetError, match=message):
        await execute_preset("extractor", PLAIN, ("extraction",), spec.model_dump(mode="json"),
                             RunContext(adapter, spec.budget), {"receipt": NO_SUBTOTAL})
    assert adapter.calls == 0


def test_the_receipt_declaration_fits_its_spec(checks: tuple[Check, ...]) -> None:
    assert check_declaration(_oct(OCT6)) == checks and check_declaration(_oct(OCT5)) == ()


def test_checks_are_refused_on_a_pipeline_or_a_game() -> None:
    step = PipelineStep(name="read", preset="extractor", model="fake",
                        budget={"cost_usd": 0.01, "compute_s": 10})
    config = _spec_json(OCT6 / "spec.json")["execution_config"]
    for spec in (
        _oct(OCT6, pipeline=[step.model_dump(mode="json")], plain=False),
        _oct(OCT6, execution_config={**config, "kind": "game"}),
    ):
        with pytest.raises(PresetError, match="not a pipeline or a game"):
            get_preset("extractor").build(spec)


# ------------------------------------------------------------------ the emitted harness


def test_the_emitted_harness_carries_the_checks_and_runs_them(tmp_path: Path) -> None:
    import asyncio

    spec = _oct(OCT6)
    get_preset("extractor").emit(tmp_path, spec, PLAIN, ("extraction",))
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    assert "'keep_digits'" in source and "'not_copied'" in source and RULES[1] in source
    adapter = Scripted(COPIED)
    output = asyncio.run(
        load_harness(tmp_path)(RunContext(adapter, spec.budget), {"receipt": NO_SUBTOTAL}))
    assert output == CHECKED


def test_a_spec_without_checks_emits_the_same_bytes_as_on_2026_10_05(tmp_path: Path) -> None:
    """The Oct 5 ladder's signed artifact digests rebuild from its spec: the same spec
    hash, configuration and harness.py bytes as the build that sat that exam."""
    spec = _oct(OCT5)
    assert spec.spec_hash == OCT5_HASH
    get_preset("extractor").emit(tmp_path, spec, PLAIN, ("extraction",))
    code = (tmp_path / "harness.py").read_bytes()
    assert hashlib.sha256(code).hexdigest() == (
        "098b37e5778a6905c2cb929c4e5795a7965f946c510c8b1569c94292a05a7b1e")
    outcome = json.loads((OCT5 / "outcome.json").read_text(encoding="utf-8"))
    versions = {"nvidia/Nemotron-3_5-Lightning": 1, "nvidia/nemotron-3-super-120b-a12b": 5,
                "nvidia/Nemotron-3-Ultra-550b-a55b": 6}
    for rung in outcome["rungs"]:
        artifact = HarnessArtifact(
            spec=spec, version=versions[rung["model"]], preset="extractor", model=rung["model"],
            components=("extraction",), params=PLAIN, directory=tmp_path,
        )
        assert artifact.artifact_digest == rung["artifact_digest"]
