"""Frozen bench suites: tasks, tiers per suite, spec builders and the suite digest.

Suite ``v1`` is synthetic by construction (``sentiment-24`` is the committed
example fixture; everything else is generated from a seed). A ``v1`` score says
"extracts what this generator prints", never anything about real uploads.
Partitions follow ``FloorEvaluator`` (sorted ``case_id``, even -> tuning, odd
-> held-out), so ``n`` cases give ``n // 2`` held-out cases and every suite
spec sets ``minimum_samples = size // 2`` explicitly.

Suite ``v2`` (Phase 14 Section 10) has one ``public`` tier with the ``receipts``
task: CORD v2 receipt text from ``examples/receipts``, checked against its
manifests by ``public_tasks.load_public``. Its bar and arm settings come from
``examples/bench/suite-v2/receipts.json``, so ``SUITES["v2"]`` is built on first
use and a broken settings file never stops a ``v1`` run. Tiers are kept per suite
(``SUITE_TIERS``), so adding a suite never changes the ``v1`` digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from architect.floors import EvaluationCase
from architect.games.types import GameRef
from architect.models import ModelRequest
from architect.models.images import ImageInput
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    Lifecycle,
    PipelineStep,
    PresetName,
    Spec,
)

from . import pages, public_tasks, text_tasks
from .oracle import OracleState, lookup_key
from .rows import (
    BenchError,
    DeliverySplit,
    FloorOrigin,
    FloorSplit,
    Kind,
    Probe,
    ReplyJson,
    TaskId,
    Tier,
    Variant,
)

__all__ = [
    "BenchTask",
    "CheckerName",
    "DatasetBundle",
    "GENERATOR_VERSIONS",
    "Kind",
    "PLAIN_PRESETS",
    "PROBES",
    "Probe",
    "Requirement",
    "SUITES",
    "SUITE_TIERS",
    "TIERS",
    "TaskId",
    "Tier",
    "Variant",
    "arm_fields",
    "example_manifests",
    "foreign_task",
    "load_siblings",
    "probe_spec",
    "spec_setting",
    "suite_digest",
    "tiers_for",
]

# Mirrors ``architect.floors.checkers.CheckerName`` (WP2; ``receipt_v1`` is Phase 14).
CheckerName = Literal["equality", "exam_extraction_v1", "invoice_extraction_v1", "receipt_v1"]
# Mirrors ``architect.spec.PLAIN_PRESETS`` (Phase 14 Section 3.1): the presets a plain
# arm may pin to one configuration; a test keeps the two equal.
PLAIN_PRESETS: frozenset[str] = frozenset(
    {"extractor", "synthesizer", "recommender", "verifier", "explorer", "planner"}
)
# The Spec fields that carry a Phase 14 arm setting (Section 3.1). Each one leaves the
# spec_hash unchanged at its default, so a v1 cell's Spec hashes exactly as before.
_ARM_FIELDS: tuple[str, ...] = (
    "invalid_policy",
    "max_invalid_fraction",
    "input_limit_chars",
    "reply_json",
    "plain",
)
Requirement = Literal["pillow", "arc"]
Role = Literal["vision", "solver", "verifier"]
ROLES: tuple[Role, ...] = ("vision", "solver", "verifier")
GENERATOR_VERSIONS: Mapping[str, str] = MappingProxyType(
    {"pages": pages.GENERATOR_VERSION, "text": text_tasks.GENERATOR_VERSION}
)
# Routing probes of a ``routed`` cell (Phase 10 Section 14), in execution order.
PROBES: tuple[Probe, ...] = ("exact", "sibling", "foreign")
# ``examples/bench/suite-<version>/siblings.json``: {task_id: committed paraphrase}.
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "bench"
_PLACEHOLDER_BUDGET = Budget(cost_usd=0.5, compute_s=120.0)  # the runner lifts from quotes
_STEP_BUDGET = Budget(cost_usd=0.2, compute_s=60.0)
# Upper bounds on the prompt characters a preset can build for one call, used by
# ``sizing_request``: the extractor's 2048-character input policy plus the same
# again for a self-check candidate and the schema; pipeline envelopes are unbounded
# but hold at most a page of questions.
_PROMPT_CHARS: Mapping[Kind, int] = MappingProxyType(
    {"text": 6144, "vision": 6144, "pipeline": 16384, "game": 8192}
)
# The extractor's input allowance when a spec sets no ``input_limit_chars``.
_INPUT_ALLOWANCE = 2048


def _label_output(labels: Sequence[str]) -> dict[str, JsonValue]:
    options: list[JsonValue] = list(labels)
    return {
        "type": "object",
        "required": ["label"],
        "properties": {"label": {"enum": options}},
        "additionalProperties": False,
    }


def _string_input(name: str) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "required": [name],
        "properties": {name: {"type": "string"}},
        "additionalProperties": False,
    }


_LABEL_OUTPUT = _label_output(text_tasks.SENTIMENT_LABELS)
_TICKET_OUTPUT = _label_output(text_tasks.TICKET_LABELS)
_LABELS: list[JsonValue] = list(pages.LABELS)
_INVOICE_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["vendor", "date", "total", "line_items"],
    "properties": {
        "vendor": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
        "total": {"type": ["number", "null"]},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["description", "quantity", "unit_price"],
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "unit_price": {"type": "number"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}
_SOLVER_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["answer_keys"],
    "properties": {
        "answer_keys": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "answer"],
                "properties": {"id": {"type": "string"}, "answer": {"enum": _LABELS}},
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}
_DIGITIZER_OUTPUT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["answer_keys", "disagreements", "review_required"],
    "properties": {
        "answer_keys": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "answer"],
                "properties": {"id": {"type": "string"}, "answer": {"enum": _LABELS}},
                "additionalProperties": False,
            },
        },
        "disagreements": {"type": "array", "items": {"type": "string"}},
        "review_required": {"const": True},
    },
    "additionalProperties": False,
}
_GAME_INPUT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["game"],
    "properties": {"game": GameRef.model_json_schema()},
    "additionalProperties": False,
}
_EXAM_TASK = (
    "Extract every printed question from the attached exam page: its number, the stem, "
    "the four choices A-D and the printed answer key. A page that only lists answers is an "
    "answer key page with no questions."
)
_DIGITIZER_TASK = (
    "Turn an uploaded test page into an answer key: extract the printed questions and keys, "
    "re-derive each answer from its question, and flag every disagreement for review."
)


def _spec(
    *,
    task: str,
    input_schema: dict[str, JsonValue],
    output_schema: dict[str, JsonValue],
    model: str,
    floor: FloorDefinition,
    preset_hint: PresetName = "extractor",
    execution_config: dict[str, JsonValue] | None = None,
    pipeline: tuple[PipelineStep, ...] = (),
    tools: tuple[str, ...] = (),
    domain: str = "general",
    budget: Budget = _PLACEHOLDER_BUDGET,
) -> Spec:
    return Spec(
        task=task,
        input_schema=input_schema,
        output_schema=output_schema,
        floor=floor,
        budget=budget,
        model_target=model,
        lifecycle=Lifecycle.EXPIRE,
        domain=domain,
        preset_hint=preset_hint,
        execution_config=execution_config or {},
        tools=tools,
        pipeline=pipeline,
    )


def arm_fields(values: Mapping[str, object]) -> dict[str, object]:
    """The Phase 14 arm settings among ``values`` that this build's ``Spec`` declares.

    None values are dropped (the Spec's own default applies). A setting the Spec does
    not declare is left out rather than claimed: rows read their arm settings back
    from the Spec they compiled (``spec_setting``), so a row never reports a setting
    its Spec did not carry.
    """
    declared = Spec.model_fields
    return {
        key: value
        for key, value in values.items()
        if key in _ARM_FIELDS and key in declared and value is not None
    }


def spec_setting(spec: Spec, name: str) -> object:
    """A Phase 14 arm setting of ``spec`` (Section 3.1), or None when undeclared."""
    return getattr(spec, name, None)


def _with_arms(spec: Spec, values: Mapping[str, object]) -> Spec:
    extra = arm_fields(values)
    if not extra:
        return spec
    return Spec.model_validate({**spec.model_dump(mode="json"), **extra})


class DatasetBundle(BaseModel):
    """Cases plus the in-process oracle tables and page manifests behind them."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    cases: list[EvaluationCase]
    state: OracleState
    manifests: tuple[dict[str, JsonValue], ...]

    @property
    def pages_manifest_sha256(self) -> str | None:
        if not self.manifests:
            return None
        return pages.manifest_sha256({"parts": list(self.manifests)})


class BenchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: TaskId
    kind: Kind
    tiers: frozenset[Tier]
    sizes: dict[Tier, int]
    floor_limit: float
    checker: CheckerName
    preset: PresetName
    discriminates: str
    requires: frozenset[Requirement]
    # Phase 14: rows take their floor origin from the task. Not part of the v1 digest.
    floor_origin: FloorOrigin = "synthetic_generated"

    @property
    def is_public(self) -> bool:
        """A task whose floor and delivery cases are a pinned public dataset's splits."""
        return self.floor_origin == "public_dataset"

    # --- spec builders -----------------------------------------------------

    def supports(self, variant: Variant) -> bool:
        # Forge and template arms (Phase 9) run on every task: same spec, same seed.
        # The routed arm (Phase 10) runs on text, vision and pipeline tasks only: a
        # game compile goes through the game factory, which the router does not see.
        # A public suite has no sibling paraphrase or foreign task, so no routed arm.
        if variant == "routed":
            return self.kind != "game" and not self.is_public
        return variant != "images-all-stages" or self.kind == "pipeline"

    def supports_plain(self) -> bool:
        """A plain arm (Section 9.3) needs one plain preset, no pipeline and no game."""
        return self.kind in ("text", "vision") and self.preset in PLAIN_PRESETS

    def default_size(self) -> int:
        return max(self.sizes.values())

    def images_per_case(self) -> int:
        if self.id == "digitizer-multipage-12":
            return 3
        return 1 if self.kind in {"vision", "pipeline"} else 0

    def _floor(self, suite: str, seed: int, size: int) -> FloorDefinition:
        kind = FloorKind.HELD_OUT_GAME_SCORE if self.kind == "game" else FloorKind.HELD_OUT_TASKS
        return FloorDefinition(
            kind=kind,
            limit=self.floor_limit,
            dataset=f"bench-{self.id}-{suite}-{seed}",
            minimum_samples=max(1, size // 2),
        )

    def spec(
        self,
        model: str,
        variant: Variant,
        *,
        route: Mapping[str, str] = {},
        suite: str = "v1",
        seed: int = 7,
        size: int | None = None,
        plain: bool = False,
        reply_json: ReplyJson | None = None,
        floor_split: FloorSplit = "evidence",
    ) -> Spec:
        """The frozen suite spec for one cell; the runner lifts budgets from quotes.

        ``plain`` and ``reply_json`` are the bench's ``--plain`` and ``--reply-json``
        (Phase 14); at their defaults a v1 spec, and so its spec_hash, is unchanged.
        A public task's spec quotes its rules verbatim and takes its bar, run budget
        and remaining arm settings from the task's settings file; ``size`` and
        ``seed`` do not change it, ``floor_split`` does.
        """
        # ``forge`` and ``template`` change only the compile's Stage 3, never the
        # spec: their cells share the default spec and therefore its spec_hash. A
        # ``routed`` cell's exact probe is the default spec too.
        if variant not in ("default", "noisy", "images-all-stages", "forge", "template", "routed"):
            raise BenchError("bench_variant_unknown")
        if set(route) - set(ROLES) or any(not value for value in route.values()):
            raise BenchError("bench_route_invalid")
        if self.is_public:
            return self._public_spec(
                model, suite=suite, floor_split=floor_split, plain=plain, reply_json=reply_json
            )
        spec = self._v1_spec(model, variant, route=route, suite=suite, seed=seed, size=size)
        return _with_arms(spec, {"plain": plain, "reply_json": reply_json})

    def _public_spec(
        self,
        model: str,
        *,
        suite: str,
        floor_split: FloorSplit,
        plain: bool,
        reply_json: ReplyJson | None,
    ) -> Spec:
        """Section 10.4: the rules as the task, the settings' bar and arm settings."""
        data = public_tasks.load_public(self.id)
        settings = data.settings
        floor = FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS,
            limit=self.floor_limit,
            dataset=f"bench-{self.id}-{suite}-{floor_split}",
            minimum_samples=max(1, len(data.splits[floor_split]) // 2),
        )
        spec = _spec(
            task=data.rules,
            input_schema=deepcopy(data.task_schema("input_schema")),
            output_schema=deepcopy(data.task_schema("output_schema")),
            model=model,
            floor=floor,
            preset_hint=self.preset,
            budget=settings.run_budget or _PLACEHOLDER_BUDGET,
        )
        return _with_arms(
            spec,
            {
                "invalid_policy": settings.invalid_policy,
                "max_invalid_fraction": settings.max_invalid_fraction,
                "input_limit_chars": settings.input_limit_chars,
                "reply_json": reply_json or settings.reply_json,
                "plain": plain,
            },
        )

    def _v1_spec(
        self,
        model: str,
        variant: Variant,
        *,
        route: Mapping[str, str],
        suite: str,
        seed: int,
        size: int | None,
    ) -> Spec:
        floor = self._floor(suite, seed, self.default_size() if size is None else size)
        if self.id == "sentiment-24":
            return _spec(
                task=(
                    "Label the sentiment of a short product review as positive, negative or "
                    "neutral."
                ),
                input_schema=_string_input("review"),
                output_schema=_LABEL_OUTPUT,
                model=model,
                floor=floor,
            )
        if self.id == "tickets-40":
            return _spec(
                task=(
                    "Classify a customer support ticket into exactly one of billing, bug, "
                    "feature, other. A ticket that mentions two topics takes the label of "
                    "its main request."
                ),
                input_schema=_string_input("ticket"),
                output_schema=_TICKET_OUTPUT,
                model=model,
                floor=floor,
            )
        if self.id == "invoice-40":
            return _spec(
                task=(
                    "Extract the vendor, the date as YYYY-MM-DD, the printed total and every "
                    "line item (description, quantity, unit price) from a plain-text invoice. "
                    "A field that is not printed must be null; never invent a value."
                ),
                input_schema=_string_input("invoice_text"),
                output_schema=_INVOICE_OUTPUT,
                model=model,
                floor=floor,
            )
        if self.kind == "vision":
            return _spec(
                task=_EXAM_TASK,
                input_schema=pages.VISION_INPUT_SCHEMA,
                output_schema=pages.EXAM_OUTPUT_SCHEMA,
                model=model,
                floor=floor,
            )
        if self.kind == "pipeline":
            stages: list[JsonValue] = ["vision-extract"]
            if variant == "images-all-stages":
                stages = ["vision-extract", "solver-rederive", "verifier-diff"]
            return _spec(
                task=_DIGITIZER_TASK,
                input_schema=pages.VISION_INPUT_SCHEMA,
                output_schema=_DIGITIZER_OUTPUT,
                model=model,
                floor=floor,
                execution_config={"images": {"stages": stages}},
                pipeline=(
                    PipelineStep(
                        name="vision-extract",
                        preset="extractor",
                        model=route.get("vision", model),
                        budget=_STEP_BUDGET,
                        input_schema=pages.VISION_INPUT_SCHEMA,
                        output_schema=pages.STAGE1_OUTPUT_SCHEMA,
                        params={"verifier_depth": 0, "retries": 0},
                    ),
                    PipelineStep(
                        name="solver-rederive",
                        preset="extractor",
                        model=route.get("solver", model),
                        budget=_STEP_BUDGET,
                        output_schema=_SOLVER_OUTPUT,
                        params={"verifier_depth": 0, "retries": 0},
                    ),
                    PipelineStep(
                        name="verifier-diff",
                        preset="verifier",
                        model=route.get("verifier", model),
                        budget=_STEP_BUDGET,
                        output_schema=_DIGITIZER_OUTPUT,
                        params={"verifier_depth": 0, "retries": 0},
                    ),
                ),
            )
        return _spec(
            task="Play a grid game from public observations",
            input_schema=_GAME_INPUT,
            output_schema={"type": "object"},
            model=model,
            floor=floor,
            preset_hint=self.preset,
            execution_config={"kind": "game"},
            tools=("game.observe", "game.step"),
            domain="game",
        )

    # --- data ----------------------------------------------------------------

    def bundle(self, seed: int, size: int) -> DatasetBundle:
        """Cases, oracle tables and page manifests for one (seed, size).

        The state also registers the floor's ``(case_id, lookup_key)`` pairs so the
        ``noisy`` oracle can flip a fixed quota per partition; no expected value
        and no held-out capability is involved (the split is the public id rule).
        A public task has fixed splits: this is its evidence floor, whose size is
        the split's (any other ``size`` is ``bench_size_unsupported``).
        """
        if self.is_public:
            bundle = self.public_bundle("evidence")
            if size != len(bundle.cases):
                raise BenchError("bench_size_unsupported")
            return bundle
        bundle = self._bundle(seed, size)
        _register_floor(bundle.cases, bundle.state, self.floor_limit)
        return bundle

    def public_bundle(self, floor_split: FloorSplit) -> DatasetBundle:
        """A public task's floor split, with oracle tables from every loaded case.

        The fake provider answers by delivered input, so the tables cover every split
        it may be shown (floor, tuning replay and delivery replay alike).
        """
        data = public_tasks.load_public(self.id)
        cases = list(data.splits[floor_split])
        state = OracleState()
        for split in data.splits.values():
            state.text.update(
                {text_tasks.canonical_key(case.task): deepcopy(case.expected) for case in split}
            )
        _register_floor(cases, state, self.floor_limit)
        return DatasetBundle(cases=cases, state=state, manifests=())

    def public_delivery(
        self, split: DeliverySplit, n: int | Literal["all"]
    ) -> tuple[list[EvaluationCase], OracleState]:
        """A public task's replay set: all of ``split`` or its first ``n`` cases in
        manifest order, case ids unchanged; its noisy quota is a floor's."""
        data = public_tasks.load_public(self.id)
        cases = list(public_tasks.delivery_cases(data, split, n))
        state = OracleState()
        state.text.update(
            {text_tasks.canonical_key(case.task): deepcopy(case.expected) for case in cases}
        )
        _register_floor(cases, state, self.floor_limit)
        return cases, state

    def _bundle(self, seed: int, size: int) -> DatasetBundle:
        if size < 0:
            raise ValueError("size must be nonnegative")
        state = OracleState()
        manifests: list[dict[str, JsonValue]] = []
        if self.id == "sentiment-24":
            fixture, _table = text_tasks.load_sentiment_cases()
            fixture = fixture[:size]
            state.text.update(
                {text_tasks.canonical_key(c.task): deepcopy(c.expected) for c in fixture}
            )
            return DatasetBundle(cases=fixture, state=state, manifests=())
        if self.id == "tickets-40":
            generated, table = text_tasks.generate_tickets(seed, size)
            state.text.update(table)
            return DatasetBundle(cases=generated, state=state, manifests=())
        if self.id == "invoice-40":
            generated, table = text_tasks.generate_invoices(seed, size)
            state.text.update(table)
            return DatasetBundle(cases=generated, state=state, manifests=())
        if self.kind == "game":
            return DatasetBundle(cases=[], state=state, manifests=())
        cases: list[EvaluationCase] = []
        if self.id == "exam-clean-40":
            rendered = pages.generate_pages(seed, size, "clean", key_page=False) if size else []
            self._vision(rendered, f"exam-clean-{seed}", cases, state)
            if rendered:
                manifests.append(pages.manifest(rendered, seed=seed))
        elif self.id == "exam-scan-40":
            n_scan, n_dense = (size + 1) // 2, size // 2
            scan = pages.generate_pages(seed, n_scan, "scan", key_page=False) if n_scan else []
            dense = (
                pages.generate_pages(seed + 1, n_dense, "dense", key_page=False) if n_dense else []
            )
            self._vision(scan, f"exam-scan-{seed}", cases, state)
            self._vision(dense, f"exam-dense-{seed}", cases, state)
            if scan:
                manifests.append(pages.manifest(scan, seed=seed))
            if dense:
                manifests.append(pages.manifest(dense, seed=seed + 1))
        elif self.id == "digitizer-24":
            rendered = pages.generate_pages(seed, size, "clean", key_page=False) if size else []
            for page in rendered:
                if page.truth.questions:
                    cases.append(
                        pages.digitizer_case(
                            [page], case_id=f"digitizer-{seed}-{page.index:03d}"
                        )
                    )
                    state.digitizer[page.sha256] = pages.stage1_output([page])
            if rendered:
                manifests.append(pages.manifest(rendered, seed=seed))
        elif self.id == "digitizer-multipage-12":
            for index in range(size):
                quiz_seed = seed * 1009 + index
                rendered = pages.generate_pages(quiz_seed, 2, "clean", key_page=True)
                cases.append(
                    pages.digitizer_case(
                        rendered, case_id=f"digitizer-multipage-{seed}-{index:03d}"
                    )
                )
                state.digitizer[rendered[0].sha256] = pages.stage1_output(rendered)
                manifests.append(pages.manifest(rendered, seed=quiz_seed))
        return DatasetBundle(cases=cases, state=state, manifests=tuple(manifests))

    @staticmethod
    def _vision(
        rendered: Sequence[pages.Page],
        dataset_id: str,
        cases: list[EvaluationCase],
        state: OracleState,
    ) -> None:
        cases.extend(pages.cases_for(rendered, "vision", dataset_id=dataset_id))
        for page in rendered:
            state.vision[page.sha256] = page.truth.model_dump(mode="json")

    def dataset(self, seed: int, size: int) -> tuple[list[EvaluationCase], OracleState]:
        bundle = self.bundle(seed, size)
        return bundle.cases, bundle.state

    def delivery(self, seed: int, n: int) -> list[EvaluationCase]:
        """Disjoint out-of-sample cases from generator seed ``seed + 1000``."""
        return self.delivery_dataset(seed, n)[0]

    def delivery_dataset(self, seed: int, n: int) -> tuple[list[EvaluationCase], OracleState]:
        """Delivery cases and their oracle tables; case ids never collide with the dataset's.

        Tickets come from the disjoint ``delivery`` pool (templates without placeholders
        would otherwise repeat across seeds); pages and invoices differ by seed alone.
        """
        if self.is_public:
            return self.public_delivery("delivery", n)
        if self.id == "sentiment-24":
            return [], OracleState()  # the fixture has no disjoint pool
        if self.id == "tickets-40":
            generated, table = text_tasks.generate_delivery_tickets(seed + 1000, n)
            state = OracleState()
            state.text.update(table)
            cases = generated
            _register_floor(cases, state, self.floor_limit)  # same quota rule as a floor
        else:
            cases, state = self.dataset(seed + 1000, n)
        return [_delivery_id(case) for case in cases], state

    # --- sizing ----------------------------------------------------------------

    def sizing_request(
        self,
        images: Sequence[ImageInput],
        *,
        spec: Spec | None = None,
        cases: Sequence[EvaluationCase] = (),
    ) -> ModelRequest:
        """A representative largest single call for per-call quotes.

        A public task (Section 10.4, confound 5) is sized from the cell itself: its
        task text and output schema, plus twice the longest input it will be shown
        (the cell's floor and replay ``cases``, capped at the input allowance of
        ``input_limit_chars`` or 2,048 characters) and 1,024 characters of prompt.
        """
        if self.is_public:
            spec = spec or self.spec("fake", "default")
            schema = json.dumps(spec.output_schema, sort_keys=True)
            largest = max((len(text_tasks.canonical_json(case.task)) for case in cases), default=0)
            limit = spec_setting(spec, "input_limit_chars")
            allowance = limit if isinstance(limit, int) else _INPUT_ALLOWANCE
            chars = len(spec.task) + len(schema) + 2 * min(largest, allowance) + 1024
            return ModelRequest(prompt="x" * chars, images=tuple(images))
        schema = json.dumps(self.spec("fake", "default").output_schema, sort_keys=True)
        return ModelRequest(
            prompt="x" * (_PROMPT_CHARS[self.kind] + len(schema)), images=tuple(images)
        )

    def max_calls_per_run(self, variant: Variant, *, plain: bool = False) -> int:
        """Extractor grid: (1 + retries) * (1 + verifier_depth) = 4; pipeline sums steps.

        A plain public cell makes one call per run; v1 sizing is unchanged.
        """
        if self.kind == "pipeline":
            return 4 + 1 + 1
        if self.kind == "game":
            return 32  # probe plus a bounded number of action turns; sized by the runner
        if self.is_public and plain:
            return 1
        return 4


def _task(
    id: TaskId,
    kind: Kind,
    sizes: dict[Tier, int],
    floor_limit: float,
    checker: CheckerName,
    preset: PresetName,
    discriminates: str,
    requires: frozenset[Requirement] = frozenset(),
    floor_origin: FloorOrigin = "synthetic_generated",
) -> BenchTask:
    return BenchTask(
        id=id,
        kind=kind,
        tiers=frozenset(sizes),
        sizes=sizes,
        floor_limit=floor_limit,
        checker=checker,
        preset=preset,
        discriminates=discriminates,
        requires=requires,
        floor_origin=floor_origin,
    )


_V1: tuple[BenchTask, ...] = (
    _task(
        "sentiment-24",
        "text",
        {"smoke": 24, "quick": 24, "full": 24},
        0.90,
        "equality",
        "extractor",
        "instruction following and strict JSON; the anchor with live evidence",
        floor_origin="example_fixture",
    ),
    _task(
        "tickets-40",
        "text",
        {"smoke": 24, "full": 40},
        0.85,
        "equality",
        "extractor",
        "terseness/self-check trade-offs on ambiguous inputs",
    ),
    _task(
        "invoice-40",
        "text",
        {"smoke": 24, "quick": 40, "full": 40},
        0.90,
        "invoice_extraction_v1",
        "extractor",
        "do not invent missing values, retries; text-only twin of the vision floor",
    ),
    _task(
        "exam-clean-40",
        "vision",
        {"smoke": 24, "quick": 40, "full": 40},
        0.90,
        "exam_extraction_v1",
        "extractor",
        "vision extraction and whether verifier_depth 1 buys accuracy; no Pillow needed",
    ),
    _task(
        "exam-scan-40",
        "vision",
        {"smoke": 16, "full": 40},
        0.70,
        "exam_extraction_v1",
        "extractor",
        "robustness to skew, noise, two columns",
        frozenset({"pillow"}),
    ),
    _task(
        "digitizer-24",
        "pipeline",
        {"smoke": 12, "full": 24},
        0.90,
        "equality",
        "extractor",
        "pipeline routing, first-stage tuning, image re-send cost",
    ),
    _task(
        "digitizer-multipage-12",
        "pipeline",
        {"full": 12},
        0.75,
        "equality",
        "extractor",
        "cross-page numbering, multi-image context",
    ),
    _task(
        "arc-local-3",
        "game",
        {"smoke": 3, "full": 3},
        1.0,
        "equality",
        "explorer",
        "game presets and prompt-size limits; not an ARC result",
    ),
    _task(
        "arc-preview",
        "game",
        {"arc": 5},
        0.0,
        "equality",
        "explorer",
        "action efficiency on cached offline public games; unverified compliance",
        frozenset({"arc"}),
    ),
)

def _receipts() -> BenchTask:
    """Suite v2's one task (Section 10.4); its bar is the settings file's."""
    settings = public_tasks.load_settings("receipts")
    return BenchTask(
        id="receipts",
        kind="text",
        tiers=frozenset({"public"}),
        sizes={"public": 200},
        floor_limit=settings.floor_limit,
        checker="receipt_v1",
        preset="extractor",
        discriminates="exact four-field extraction from real receipt text",
        requires=frozenset(),
        floor_origin="public_dataset",
    )


class _Suites(Mapping[str, tuple[BenchTask, ...]]):
    """``{"v1": _V1, "v2": (receipts,)}``; v2 is built from its settings file on use.

    A missing or invalid settings file is ``bench_suite_unknown`` when v2 is read,
    so it can never stop a v1 run. Membership and iteration read no file.
    """

    _VERSIONS: tuple[str, ...] = ("v1", "v2")

    def __getitem__(self, version: str) -> tuple[BenchTask, ...]:
        if version == "v1":
            return _V1
        if version == "v2":
            return (_receipts(),)
        raise KeyError(version)

    def __iter__(self) -> Iterator[str]:
        return iter(self._VERSIONS)

    def __len__(self) -> int:
        return len(self._VERSIONS)

    def __contains__(self, version: object) -> bool:
        return version in self._VERSIONS


SUITES: Mapping[str, tuple[BenchTask, ...]] = _Suites()
SUITE_TIERS: Mapping[str, Mapping[Tier, tuple[TaskId, ...]]] = MappingProxyType(
    {
        "v1": MappingProxyType(
            {
                "smoke": (
                    "sentiment-24",
                    "tickets-40",
                    "invoice-40",
                    "exam-clean-40",
                    "exam-scan-40",
                    "digitizer-24",
                    "arc-local-3",
                ),
                "quick": ("sentiment-24", "invoice-40", "exam-clean-40"),
                "full": (
                    "sentiment-24",
                    "tickets-40",
                    "invoice-40",
                    "exam-clean-40",
                    "exam-scan-40",
                    "digitizer-24",
                    "digitizer-multipage-12",
                    "arc-local-3",
                ),
                "arc": ("arc-preview",),
            }
        ),
        "v2": MappingProxyType({"public": ("receipts",)}),
    }
)
# Suite v1's tiers, kept for existing importers.
TIERS: Mapping[Tier, tuple[TaskId, ...]] = SUITE_TIERS["v1"]


def tiers_for(version: str) -> Mapping[Tier, tuple[TaskId, ...]]:
    """The tiers of suite ``version`` (``bench_suite_unknown`` for another version)."""
    tiers = SUITE_TIERS.get(version)
    if tiers is None:
        raise BenchError("bench_suite_unknown")
    return tiers


def load_siblings(version: str = "v1") -> dict[str, str]:
    """The committed sibling paraphrases ``{task_id: text}`` of suite ``version``.

    A missing, unreadable or malformed file is ``bench_suite_unknown``: a sibling
    probe is never improvised.
    """
    if version not in SUITES:
        raise BenchError("bench_suite_unknown")
    path = EXAMPLES / f"suite-{version}" / "siblings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise BenchError("bench_suite_unknown") from None
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value.strip()
        for key, value in payload.items()
    ):
        raise BenchError("bench_suite_unknown")
    return {str(key): str(value) for key, value in payload.items()}


def _shape_key(spec: Spec) -> str:
    """The router's ``shape`` identity (Section 5) without importing the router's
    Spec hashing twice: domain, schemas, tools and pipeline, model included only
    through the pipeline steps, exactly as ``routing.signature`` hashes it."""
    from architect.routing.signature import signature

    return signature(spec).shape


def foreign_task(task: BenchTask, version: str = "v1") -> BenchTask:
    """The next routed-capable task after ``task`` in suite order (wrapping) whose
    ``shape`` differs; deterministic, no new dataset."""
    tasks = SUITES.get(version)
    if tasks is None:
        raise BenchError("bench_suite_unknown")
    index = next(i for i, item in enumerate(tasks) if item.id == task.id)
    own = _shape_key(task.spec("fake", "default", suite=version))
    for offset in range(1, len(tasks)):
        other = tasks[(index + offset) % len(tasks)]
        if not other.supports("routed"):
            continue
        if _shape_key(other.spec("fake", "default", suite=version)) != own:
            return other
    raise BenchError("bench_suite_unknown")


def probe_spec(spec: Spec, probe: Probe, task: BenchTask, *, siblings: Mapping[str, str]) -> Spec:
    """One probe's Spec derived from the cell's own (sized) Spec (Section 14).

    ``exact`` is ``spec`` itself; ``sibling`` keeps schemas, tools and floor and
    swaps the task text for the committed paraphrase; ``foreign`` is built by the
    runner from ``foreign_task`` (its own dataset and floor) and is refused here.
    """
    if probe == "exact":
        return spec
    if probe == "sibling":
        text = siblings.get(task.id)
        if text is None:
            raise BenchError("bench_suite_unknown")
        return Spec.model_validate({**spec.model_dump(mode="json"), "task": text})
    raise BenchError("bench_variant_unknown")


def _digest_payload(task: BenchTask) -> dict[str, JsonValue]:
    tiers: list[JsonValue] = [str(tier) for tier in sorted(task.tiers)]
    requires: list[JsonValue] = [str(item) for item in sorted(task.requires)]
    sizes: dict[str, JsonValue] = {tier: task.sizes[tier] for tier in sorted(task.sizes)}
    return {
        "id": task.id,
        "kind": task.kind,
        "tiers": tiers,
        "sizes": sizes,
        "floor_limit": task.floor_limit,
        "checker": task.checker,
        "preset": task.preset,
        "discriminates": task.discriminates,
        "requires": requires,
    }


def suite_digest(version: str) -> str:
    """sha256 over task ids, kinds, sizes, floors, checkers, tiers and data identity.

    ``v1`` hashes exactly its Phase 8 payload (generator versions, tasks, tiers).
    Later suites add each task's floor origin and, per public task, the settings
    hash, the dataset identity and every split's ``cases_sha256`` (Section 10.1), so
    a change to the settings file or to one case changes the digest.
    """
    tasks = SUITES.get(version)
    if tasks is None:
        raise BenchError("bench_suite_unknown")
    tiers = tiers_for(version)
    tier_payloads: dict[str, JsonValue] = {tier: list(tiers[tier]) for tier in sorted(tiers)}
    payload: dict[str, JsonValue]
    if version == "v1":
        payload = {
            "version": version,
            "generators": dict(GENERATOR_VERSIONS),
            "tasks": [_digest_payload(task) for task in tasks],
            "tiers": tier_payloads,
        }
    else:
        public: dict[str, JsonValue] = {
            task.id: public_tasks.load_public(task.id).digest_entry()
            for task in tasks
            if task.is_public
        }
        payload = {
            "version": version,
            "tasks": [
                {**_digest_payload(task), "floor_origin": task.floor_origin} for task in tasks
            ],
            "tiers": tier_payloads,
            "public": public,
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _register_floor(cases: Sequence[EvaluationCase], state: OracleState, limit: float) -> None:
    """Hand the oracle case ids and lookup keys (from ``task`` only) for the noisy quotas."""
    state.add_floor([(case.case_id, lookup_key(case.task)) for case in cases], limit)


def _delivery_id(case: EvaluationCase) -> EvaluationCase:
    return case.model_copy(update={"case_id": f"delivery-{case.case_id}"})


def _without_platform(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in payload.items() if key != "platform"}


def example_manifests(version: str = "v1", seed: int = 7) -> dict[str, dict[str, JsonValue]]:
    """The committed ``examples/bench/suite-<version>/*-manifest.json`` payloads.

    Text manifests hold seed, ``n`` and the canonical cases sha256 at every tier
    size; the pages manifest holds the ``clean`` renderer's page sha256s at the
    largest size of each ``clean`` task. ``platform`` is omitted because the
    ``clean`` bytes are identical on every platform; nothing here is content.
    """
    generated = (
        "tickets-40", "invoice-40", "exam-clean-40", "digitizer-24", "digitizer-multipage-12"
    )
    tasks: dict[str, BenchTask] = (
        {task.id: task for task in SUITES.get(version) or ()} if version in SUITES else {}
    )
    if not all(task_id in tasks for task_id in generated):
        raise BenchError("bench_suite_unknown")  # only a generated suite has these manifests
    out: dict[str, dict[str, JsonValue]] = {}
    for task_id, generate, name in (
        ("tickets-40", text_tasks.generate_tickets, "tickets"),
        ("invoice-40", text_tasks.generate_invoices, "invoice"),
    ):
        sizes = sorted(set(tasks[task_id].sizes.values()))
        entries: list[JsonValue] = [
            {"seed": seed, "n": n, "cases_sha256": text_tasks.cases_sha256(generate(seed, n)[0])}
            for n in sizes
        ]
        out[f"{name}-manifest.json"] = {
            "suite_version": version,
            "task_id": task_id,
            "generator_version": text_tasks.GENERATOR_VERSION,
            "entries": entries,
            "note": (
                "seed, n and sha256 of the canonical cases JSON only; "
                f"regenerate with generate_{'tickets' if name == 'tickets' else 'invoices'}"
            ),
        }
    page_tasks: dict[str, JsonValue] = {}
    for task_id in ("exam-clean-40", "digitizer-24", "digitizer-multipage-12"):
        bundle = tasks[task_id].bundle(seed, tasks[task_id].default_size())
        parts: list[JsonValue] = [_without_platform(part) for part in bundle.manifests]
        page_tasks[task_id] = parts[0] if len(parts) == 1 else {"parts": parts}
    out["pages-manifest.json"] = {
        "suite_version": version,
        "seed": seed,
        "style": "clean",
        "dpi": 96,
        "generator_version": pages.GENERATOR_VERSION,
        "tasks": page_tasks,
        "note": (
            "clean-renderer sha256s at the full-tier sizes; PNGs are never committed and "
            "scan/dense pages are not byte-portable across Pillow builds"
        ),
    }
    return out
