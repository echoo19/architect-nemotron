"""Deterministic oracle adapters for the fake provider.

An ``OracleAdapter`` answers by what it is DELIVERED, never by what a floor
expects: for a request with images it looks up ``sha256(request.images[0].data)``
in the state tables; for text it parses the JSON prompt the presets build
(``{"instruction", "task_description", "input", "output_schema"}``) and looks
up ``canonical_key(input)``. ``verify_candidate`` self-checks pass. Pipeline
solver and verifier stages compute their answer from the stage-1 output they
receive in the envelope. An unknown hash returns a schema-valid WRONG answer
so a broken image path or a truncated payload fails the floor loudly.

``error_rate > 0`` (``NoisyOracle``) is a MECHANISM CHECK with a guaranteed,
known effect size, not a realistic model of a weaker model's errors. For every
floor the suite registers (``OracleState.add_floor``: case ids and lookup keys
only, never expected values) the oracle splits the cases by the floor's own
public rule (sorted ``case_id``, even index -> tuning, odd -> held-out) and
flips a deterministic QUOTA in each partition, choosing the cases by a
``sha256(seed:key)`` ordering:

- held-out: ``max(ceil(rate * n), misses_allowed + 1)`` capped at ``n``, where
  ``misses_allowed`` is the most misses the floor ``limit`` tolerates, so the
  held-out score is strictly below the limit and the cell is unlicensed with
  ``primary_floor_failed``;
- tuning: ``max(ceil(rate * n), 6)`` capped at ``n``, so a ``default`` vs
  ``noisy`` compare has at least 6 discordant pairs in one direction
  (``mcnemar_exact(6, 0) = 0.03125 < 0.05``) and is a ``regression``.

A flip is decided by the lookup key alone, so every call for the same input
(retries, self-checks, candidates) gets the same answer. Pipeline solver and
verifier stages never flip on their own: the only injected error is the
stage-1 flip they inherit, and a stage-1 flip always toggles one printed-key
disagreement so the final output changes. Keys outside every registered floor
(ad hoc tables) keep a per-key Bernoulli draw at ``error_rate``. Usage is 80 in
/ 40 out text tokens plus the fake image reservation per image, priced from the
price table, so every synthetic cell costs a nonzero, dollar-bound amount.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from architect.models import (
    AdapterError,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    UsageQuote,
)
from architect.models.fake import FakeAdapter, RequestShape
from architect.models.pricing import image_reservation

from .pages import LABELS, Question, solve_question
from .text_tasks import canonical_key

__all__ = [
    "FORGE_EXAMPLES",
    "FloorPartition",
    "ForgeScript",
    "NoisyOracle",
    "OracleAdapter",
    "OracleState",
    "held_out_flips",
    "lookup_key",
    "oracle_factory",
    "tuning_flips",
]

TEXT_TOKENS_IN = 80
TEXT_TOKENS_OUT = 40
# Fewest one-directional discordant pairs with mcnemar_exact(k, 0) < 0.05 (k=6: 0.03125).
MIN_TUNING_FLIPS = 6
_RATE_EPS = 1e-9  # 0.15 * 40 must be the 6 it is, never 7


def lookup_key(task: JsonValue) -> str:
    """The key the oracle answers a case's task by, computed from the task alone.

    A task carrying ``images`` keys by ``sha256`` of the first image's bytes
    (the bytes delivered as ``request.images[0]``); any other task keys by
    ``canonical_key(task)``. Expected values are never an input.
    """
    if isinstance(task, dict):
        images = task.get("images")
        first = images[0] if isinstance(images, list) and images else None
        literal = first.get("$image") if isinstance(first, dict) else None
        encoded = literal.get("base64") if isinstance(literal, dict) else None
        if isinstance(encoded, str):
            try:
                return hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
            except (binascii.Error, ValueError):
                pass
    return canonical_key(task)


def held_out_flips(n: int, rate: float, limit: float) -> int:
    """Held-out quota: enough misses that ``(n - flips) / n < limit`` whenever possible."""
    if n <= 0 or rate <= 0.0:
        return 0
    misses_allowed = 0  # the most misses the floor tolerates, by the floor's own ``>=``
    while misses_allowed < n and (n - misses_allowed - 1) / n >= limit:
        misses_allowed += 1
    return min(n, max(math.ceil(rate * n - _RATE_EPS), misses_allowed + 1))


def tuning_flips(n: int, rate: float) -> int:
    """Tuning quota: at least ``MIN_TUNING_FLIPS`` discordant pairs, capped at ``n``."""
    if n <= 0 or rate <= 0.0:
        return 0
    return min(n, max(math.ceil(rate * n - _RATE_EPS), MIN_TUNING_FLIPS))


def _rank(seed: int, key: str) -> bytes:
    return hashlib.sha256(f"{seed}:{key}".encode()).digest()


@dataclass(frozen=True)
class FloorPartition:
    """Lookup keys of one floor's cases, split by the floor's public rule; no answers."""

    tuning: tuple[str, ...]
    held_out: tuple[str, ...]
    limit: float

    @classmethod
    def split(cls, cases: Sequence[tuple[str, str]], limit: float) -> FloorPartition:
        """``(case_id, lookup_key)`` pairs -> sorted case_id, even tuning, odd held-out.

        The same rule as ``FloorEvaluator`` (``architect/floors/base.py``); it needs
        only the case ids, so no held-out capability or expected value is involved.
        """
        keys = tuple(key for _, key in sorted(cases, key=lambda pair: pair[0]))
        return cls(tuning=keys[::2], held_out=keys[1::2], limit=limit)

    def flips(self, rate: float, seed: int) -> set[str]:
        """Keys of the first quota cases per partition in ``sha256(seed:key)`` order."""
        chosen: set[str] = set()
        for keys, quota in (
            (self.tuning, tuning_flips(len(self.tuning), rate)),
            (self.held_out, held_out_flips(len(self.held_out), rate, self.limit)),
        ):
            ranked = sorted(range(len(keys)), key=lambda i: (_rank(seed, keys[i]), i))
            chosen.update(keys[i] for i in ranked[:quota])
        return chosen


class OracleState:
    """Answer tables built in-process by the suite for one bench run."""

    def __init__(
        self,
        vision: dict[str, JsonValue] | None = None,
        text: dict[str, JsonValue] | None = None,
        digitizer: dict[str, JsonValue] | None = None,
        floors: Sequence[FloorPartition] = (),
    ) -> None:
        self.vision: dict[str, JsonValue] = dict(vision or {})  # sha256(page) -> extraction
        self.text: dict[str, JsonValue] = dict(text or {})  # canonical input key -> answer
        self.digitizer: dict[str, JsonValue] = dict(digitizer or {})  # sha256(page) -> stage 1
        self.floors: list[FloorPartition] = list(floors)  # keys only; drives the noisy quotas

    def add_floor(self, cases: Sequence[tuple[str, str]], limit: float) -> None:
        """Register one floor's ``(case_id, lookup_key)`` pairs for the noisy quotas."""
        if cases:
            self.floors.append(FloorPartition.split(cases, limit))

    def merge(self, other: OracleState) -> None:
        self.vision.update(other.vision)
        self.text.update(other.text)
        self.digitizer.update(other.digitizer)
        self.floors.extend(other.floors)

    def __len__(self) -> int:
        return len(self.vision) + len(self.text) + len(self.digitizer)


def _minimal(schema: JsonValue) -> JsonValue:
    """A minimal instance of a JSON Schema; the oracle's schema-valid wrong answer."""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return deepcopy(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return deepcopy(enum[0])
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind == "object":
        required = schema.get("required")
        properties = schema.get("properties")
        names = [name for name in required if isinstance(name, str)] if isinstance(
            required, list
        ) else []
        table = properties if isinstance(properties, dict) else {}
        return {name: _minimal(table.get(name, {})) for name in names}
    if kind == "array":
        minimum = schema.get("minItems")
        count = minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else 0
        return [_minimal(schema.get("items", {})) for _ in range(count)]
    if kind == "string":
        length = schema.get("minLength")
        return "x" * length if isinstance(length, int) and not isinstance(length, bool) else ""
    if kind == "integer":
        minimum = schema.get("minimum")
        return minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return None


def _next_label(label: object) -> str:
    if isinstance(label, str) and label in LABELS:
        index = list(LABELS).index(label)
        return LABELS[(index + 1) % len(LABELS)]
    return "A"


def _corrupt(answer: JsonValue, schema: JsonValue) -> JsonValue:
    """A different, still schema-valid answer for the noisy variant."""
    wrong = deepcopy(answer)
    if isinstance(wrong, dict):
        label = wrong.get("label")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        options = properties.get("label") if isinstance(properties, dict) else None
        values = options.get("enum") if isinstance(options, dict) else None
        if isinstance(label, str) and isinstance(values, list) and label in values:
            return {**wrong, "label": values[(values.index(label) + 1) % len(values)]}
        questions = wrong.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            first = dict(questions[0])
            first["answer_key"] = _next_label(first.get("answer_key"))
            return {**wrong, "questions": [first, *questions[1:]]}
        if "answer_key_page" in wrong and isinstance(wrong["answer_key_page"], bool):
            return {**wrong, "answer_key_page": not wrong["answer_key_page"]}
        keys = wrong.get("answer_keys")
        if isinstance(keys, list) and keys and isinstance(keys[0], dict):
            first = dict(keys[0])
            first["answer"] = _next_label(first.get("answer"))
            return {**wrong, "answer_keys": [first, *keys[1:]]}
        items = wrong.get("line_items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            # Quantities are compared exactly by the invoice checker; a vendor suffix
            # could stay above its 0.9 similarity threshold and pass by accident.
            first = dict(items[0])
            quantity = first.get("quantity")
            count = quantity if isinstance(quantity, int) and not isinstance(quantity, bool) else 0
            first["quantity"] = count + 1
            return {**wrong, "line_items": [first, *items[1:]]}
        item_count = wrong.get("item_count")
        if isinstance(item_count, int) and not isinstance(item_count, bool):
            # Receipts (Phase 14): receipt_v1 compares item_count exactly, and one more
            # item keeps the answer schema-valid.
            return {**wrong, "item_count": item_count + 1}
        if "total" in wrong:
            total = wrong["total"]
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                return {**wrong, "total": float(total) + 1.0}
            return {**wrong, "total": 1.0}
    fallback = _minimal(schema)
    return fallback if fallback != answer else wrong


def _corrupt_stage1(stage1: dict[str, JsonValue]) -> JsonValue:
    """Toggle the first question's printed-key disagreement so the verifier output changes.

    Moving a printed key that is already wrong to another wrong label would
    leave the final disagreement list unchanged; this always flips agree <->
    disagree, deriving the answer from the delivered question like the solver.
    """
    questions = stage1.get("questions")
    if not isinstance(questions, list) or not questions or not isinstance(questions[0], dict):
        return {**stage1, "questions": []}
    first = dict(questions[0])
    derived = _solve(first)
    first["answer_key"] = _next_label(derived) if first.get("answer_key") == derived else derived
    return {**stage1, "questions": [first, *questions[1:]]}


def _is_envelope(value: JsonValue) -> bool:
    return isinstance(value, dict) and set(value) == {"original", "previous", "steps"}


def _required(schema: JsonValue) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    required = schema.get("required")
    return {name for name in required if isinstance(name, str)} if isinstance(
        required, list
    ) else set()


def _solve(question: JsonValue) -> str:
    try:
        return solve_question(Question.model_validate(question))
    except ValueError:
        return "A"


def _stage_answer(envelope: dict[str, JsonValue], schema: JsonValue) -> JsonValue:
    """Solver and verifier stages derive from the stage-1 questions they received."""
    steps = envelope.get("steps")
    stage1: dict[str, JsonValue] | None = None
    solver: dict[str, JsonValue] | None = None
    if isinstance(steps, dict):
        for output in steps.values():
            if isinstance(output, dict) and "layout_source" in output:
                stage1 = output
            elif isinstance(output, dict) and "answer_keys" in output:
                solver = output
    questions = stage1.get("questions") if stage1 is not None else None
    items = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
    derived: list[JsonValue] = [
        {"id": f"q{q.get('number')}", "answer": _solve(q)} for q in items
    ]
    if "disagreements" not in _required(schema):
        return {"answer_keys": derived}
    printed = {f"q{q.get('number')}": q.get("answer_key") for q in items}
    keys = solver.get("answer_keys") if solver is not None else derived
    answers = [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []
    disagreements: list[JsonValue] = []
    for key in answers:
        identifier = key.get("id")
        if isinstance(identifier, str) and printed.get(identifier) != key.get("answer"):
            disagreements.append(identifier)
    keys_out: list[JsonValue] = list(answers)
    return {"answer_keys": keys_out, "disagreements": disagreements, "review_required": True}


class OracleAdapter(FakeAdapter):
    """Answers by delivered bytes or delivered input; see the module docstring."""

    def __init__(
        self,
        model: str,
        state: OracleState,
        *,
        error_rate: float = 0.0,
        seed: int = 0,
        prices_path: Path | None = None,
    ) -> None:
        if not 0.0 <= error_rate <= 1.0:
            raise ValueError("error_rate must be within [0, 1]")
        super().__init__(model, prices_path=prices_path)
        self._state = state
        self._error_rate = error_rate
        self._seed = seed
        self.flipped = 0  # content-free count of deliberately wrong answers
        self._quota: tuple[int, frozenset[str], frozenset[str]] | None = None

    def _quota_keys(self) -> tuple[frozenset[str], frozenset[str]]:
        """(keys to flip, keys covered by a registered floor); recomputed after a merge."""
        count = len(self._state.floors)
        if self._quota is None or self._quota[0] != count:
            flips: set[str] = set()
            covered: set[str] = set()
            for floor in self._state.floors:
                flips |= floor.flips(self._error_rate, self._seed)
                covered.update(floor.tuning, floor.held_out)
            self._quota = (count, frozenset(flips), frozenset(covered))
        return self._quota[1], self._quota[2]

    def _flip(self, key: str) -> bool:
        if self._error_rate <= 0.0:
            return False
        flips, covered = self._quota_keys()
        if key in covered:
            return key in flips
        digest = _rank(self._seed, key)
        return int.from_bytes(digest[:8], "big") / 2.0**64 < self._error_rate

    def _usage(self, request: ModelRequest) -> tuple[int, int, float]:
        tokens_in = TEXT_TOKENS_IN + image_reservation(self._price, request.images)
        return tokens_in, TEXT_TOKENS_OUT, self._price.cost(tokens_in, TEXT_TOKENS_OUT)

    def quote(self, request: ModelRequest) -> UsageQuote:
        tokens_in, tokens_out, cost = self._usage(request)
        return UsageQuote(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)

    def _answer(self, request: ModelRequest) -> JsonValue:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(request.prompt)
        except ValueError:
            return {"passed": False, "reason": "oracle could not parse the prompt"}
        if not isinstance(parsed, dict):
            return None
        prompt = cast(dict[str, JsonValue], parsed)
        schema = prompt.get("output_schema", {})
        if prompt.get("operation") == "verify_candidate":
            return {"passed": True, "reason": "oracle self-check"}
        if "operation" in prompt:
            return _minimal(schema)
        task_input = prompt.get("input")
        if _is_envelope(task_input):
            # Later stages derive from what they receive; a stage-1 flip is their only error.
            return _stage_answer(cast(dict[str, JsonValue], task_input), schema)
        if request.images:
            key = hashlib.sha256(request.images[0].data).hexdigest()
            table = self._state.digitizer if "layout_source" in _required(schema) else (
                self._state.vision
            )
            if key not in table:
                return _minimal(schema)
            answer = deepcopy(table[key])
        else:
            key = canonical_key(task_input if task_input is not None else None)
            if key not in self._state.text:
                return _minimal(schema)
            answer = deepcopy(self._state.text[key])
        if self._flip(key):
            self.flipped += 1
            if isinstance(answer, dict) and "layout_source" in answer:
                return _corrupt_stage1(answer)
            return _corrupt(answer, schema)
        return answer

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        tokens_in, tokens_out, cost = self._usage(request)
        text = json.dumps(self._answer(request), sort_keys=True, allow_nan=False)
        self.seen.append(
            RequestShape(
                prompt_bytes=len(request.prompt.encode("utf-8")),
                image_count=len(request.images),
                image_bytes=sum(len(image.data) for image in request.images),
                sha256s=tuple(image.sha256 for image in request.images),
            )
        )
        return ModelResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            model=self.model,
        )


class NoisyOracle(OracleAdapter):
    """The suite's ``noisy`` variant: a mechanism check with a guaranteed effect size.

    Not a realistic model. On the floors the suite registers it flips the
    quotas in the module docstring (at least 15% per partition, enough held-out
    misses to fail every floor, at least 6 tuning cases); keys outside every
    registered floor flip at 15% per key.
    """

    def __init__(
        self,
        model: str,
        state: OracleState,
        *,
        error_rate: float = 0.15,
        seed: int = 0,
        prices_path: Path | None = None,
    ) -> None:
        super().__init__(model, state, error_rate=error_rate, seed=seed, prices_path=prices_path)


def oracle_factory(
    state: OracleState, *, error_rate: float = 0.0, seed: int = 0
) -> Callable[[str], ModelAdapter]:
    """One adapter per model name, shared across calls so counters accumulate."""
    adapters: dict[str, OracleAdapter] = {}

    def factory(model: str) -> ModelAdapter:
        if model not in adapters:
            adapters[model] = OracleAdapter(model, state, error_rate=error_rate, seed=seed)
        return adapters[model]

    return factory


# ``examples/forge`` in the checkout: committed v2 sources and ``scripts/<task_id>.json``.
FORGE_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "forge"
_SCRIPT_KEYS = frozenset({"text", "tool_calls"})
_SCRIPT_LIMIT = 1_048_576
FORGE_SCRIPT_FINAL = "done"


class ForgeScript:
    """A $0, deterministic Forge agent for ``provider=fake`` (Section 13).

    It replays a committed Pi control script, ``scripts/<task_id>.json``: a JSON
    array whose items are ``{"text"?: str, "tool_calls"?: [{id, name, arguments}]}``
    envelopes, one per model turn. After the last item it answers the plain final
    text ``"done"``, which ends the Pi session. A ``write`` call may name a
    committed source with ``"content_from": "<file>"`` (relative to ``examples/forge``)
    instead of ``content``; it is inlined at load time, so the script stays a
    reference to the committed v2 harness rather than a copy of it. Nothing it
    returns depends on the prompt: the tools, not this adapter, decide what the
    session sees. Usage is a fixed 80 in / 40 out tokens per turn at $0, so rows are
    deterministic whatever Pi puts in its context.
    """

    def __init__(
        self, task_id: str, *, model: str = "fake", root: Path | None = None
    ) -> None:
        self.model = model
        self.task_id = task_id
        self.calls = 0
        self._root = (root or FORGE_EXAMPLES).resolve()
        self._steps = self._load(self._root / "scripts" / f"{task_id}.json")

    def _inline(self, name: str) -> str:
        path = (self._root / name).resolve()
        if not path.is_relative_to(self._root) or not path.is_file():
            raise AdapterError("Invalid Forge script source")
        return path.read_text(encoding="utf-8")

    def _load(self, path: Path) -> tuple[str, ...]:
        try:
            if path.stat().st_size > _SCRIPT_LIMIT:
                raise AdapterError("Invalid Forge script")
            steps = json.loads(path.read_text(encoding="utf-8"))
        except AdapterError:
            raise
        except (OSError, ValueError) as exc:
            raise AdapterError("Invalid or missing Forge script") from exc
        if not isinstance(steps, list) or not steps:
            raise AdapterError("Invalid Forge script")
        texts: list[str] = []
        for step in steps:
            if not isinstance(step, dict) or not step or set(step) - _SCRIPT_KEYS:
                raise AdapterError("Invalid Forge script")
            calls = step.get("tool_calls", [])
            if not isinstance(calls, list) or not isinstance(step.get("text", ""), str):
                raise AdapterError("Invalid Forge script")
            for call in calls:
                arguments = call.get("arguments") if isinstance(call, dict) else None
                if not isinstance(arguments, dict):
                    raise AdapterError("Invalid Forge script")
                source = arguments.pop("content_from", None)
                if source is not None:
                    if not isinstance(source, str) or "content" in arguments:
                        raise AdapterError("Invalid Forge script")
                    arguments["content"] = self._inline(source)
            texts.append(json.dumps(step, sort_keys=True, ensure_ascii=True))
        return tuple(texts)

    def _text(self, index: int) -> str:
        return self._steps[index] if index < len(self._steps) else FORGE_SCRIPT_FINAL

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=TEXT_TOKENS_IN, tokens_out=TEXT_TOKENS_OUT, cost_usd=0.0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text = self._text(self.calls)
        self.calls += 1
        return ModelResponse(
            text=text,
            tokens_in=TEXT_TOKENS_IN,
            tokens_out=TEXT_TOKENS_OUT,
            cost_usd=0.0,
            model=self.model,
        )
