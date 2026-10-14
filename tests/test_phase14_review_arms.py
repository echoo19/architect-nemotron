"""Phase 14 review: no arm may be handicapped against another.

The pre-registration (docs/bench/prereg-nebius.md, sections 4.2, 5 and 6) gives every
arm the same input, parsing, output cap, json_schema rule and per-run budget, and PLAN
section 2 (W5) sizes calls from the largest case so a budget never zeroes an arm. The
tests that record a defect found in review assert the pre-registered behaviour; each
was a strict xfail until its fix landed. Everything runs offline: providers are
``httpx.MockTransport`` stand-ins or scripted fakes, and the sandbox worker's pipe is
stubbed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import JsonValue
from sandbox_v2_support import body, interpret, make_artifact

import architect.compiler.tuning as tuning
from architect.bench import BenchError, public_tasks, runner
from architect.bench.providers import CallMeter, instrument
from architect.bench.runner import BenchConfig, run_bench
from architect.bench.suite import SUITES, BenchTask
from architect.bench.text_tasks import canonical_key
from architect.models import AdapterError, FakeAdapter, ModelAdapter, ModelRequest, ModelResponse
from architect.models.http import REASONING_CUT, OpenAICompatibleAdapter
from architect.models.pricing import load_prices, price_for
from architect.spec import Budget

ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
RECEIPT = {"total": 1200, "subtotal": 1100, "tax": 100, "item_count": 2}
VERDICT = {"passed": True, "reason": "matches the receipt"}


def _receipts() -> BenchTask:
    return next(item for item in SUITES["v2"] if item.id == "receipts")


def _answers() -> dict[str, JsonValue]:
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    return {
        canonical_key(case.task): case.expected
        for split in data.splits.values()
        for case in split
    }


def _reply(
    model: str, message: dict[str, JsonValue], finish: str, completion: int
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": completion},
        },
    )


def _factory(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    **options: object,
) -> Callable[[str], ModelAdapter]:
    """Token Factory adapters (``nebius`` dialect) over an offline transport."""

    def factory(model: str) -> ModelAdapter:
        return OpenAICompatibleAdapter(
            model,
            api_key="offline",
            base_url="https://relay.invalid/v1",
            transport=httpx.MockTransport(handler),
            dialect="nebius",
            **options,  # type: ignore[arg-type]
        )

    return factory


# ------------------------------------------------------------ the json_schema rule


def test_a_sandboxed_preset_answers_under_the_schema_reply_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = _receipts().spec("fake", "default", suite="v2", reply_json="schema")
    artifact = make_artifact(tmp_path / "harness", spec=spec, preset_name="extractor")
    emitted = (tmp_path / "harness" / "harness.py").read_text(encoding="utf-8")
    bound = emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip()
    # The exact binding a Forge harness or the extract_verify template reuses.
    frame, sent = interpret(
        monkeypatch,
        artifact,
        body(f"return {bound}"),
        {"receipt": "TOTAL 1,200"},
        replies=(json.dumps(RECEIPT), json.dumps(VERDICT)),
    )
    assert frame["op"] == "result" and frame["value"] == RECEIPT
    assert [message["op"] for message in sent] == ["call", "call"]
    # Each call carries its reply schema to the host: the answer's, then the check's.
    assert sent[0]["schema"] == spec.output_schema
    assert sent[1]["schema"]["required"] == ["passed", "reason"]


def test_the_same_sandboxed_preset_answers_under_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control for the test above: only the reply mode differs."""
    spec = _receipts().spec("fake", "default", suite="v2", reply_json="first")
    artifact = make_artifact(tmp_path / "harness", spec=spec, preset_name="extractor")
    emitted = (tmp_path / "harness" / "harness.py").read_text(encoding="utf-8")
    bound = emitted[emitted.index("await execute_preset(") :].split("\n")[0].strip()
    frame, sent = interpret(
        monkeypatch,
        artifact,
        body(f"return {bound}"),
        {"receipt": "TOTAL 1,200"},
        replies=(json.dumps(RECEIPT), json.dumps(VERDICT)),
    )
    assert frame["op"] == "result" and frame["value"] == RECEIPT
    assert [sorted(message) for message in sent] == [["op", "prompt", "tokens"]] * 2


# ------------------------------------------------------------ the per-run budget


async def test_a_pinned_run_budget_never_zeroes_an_arm(tmp_path: Path) -> None:
    answers = _answers()
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        prompt = json.loads(json.loads(request.content)["messages"][0]["content"])
        answer = answers[canonical_key(prompt["input"])]
        return _reply(ULTRA, {"role": "assistant", "content": json.dumps(answer)}, "stop", 30)

    config = BenchConfig(
        suite="v2", tier="public", provider="fake", models=(ULTRA,), plain=True,
        floor_split="rehearsal", delivery_n=4, tuning_replay=False, out=tmp_path / "out",
    )
    try:
        # The plain Ultra arm, as profile.json's Nemotron entries define it (no max_tokens).
        result = await run_bench(
            config, factory=_factory(handler), workspace_root=tmp_path / "ws"
        )
    except BenchError:
        return  # refused before any spend: no arm is zeroed
    for row in result.rows:
        if row.delivery_cases:
            assert row.delivery_over_budget != row.delivery_cases, (
                f"{row.delivery_over_budget} of {row.delivery_cases} delivery runs over "
                f"budget, {len(sent)} requests sent"
            )


# ------------------------------------------------------------ cost per receipt


async def test_a_billed_reply_cut_in_its_reasoning_is_charged_to_its_run(
    tmp_path: Path,
) -> None:
    answers = _answers()
    public_tasks.clear_cache()
    delivery = public_tasks.load_public("receipts").splits["delivery"][:4]
    cut = {canonical_key(case.task) for case in delivery[:2]}
    price = price_for(load_prices(), LIGHTNING)
    billed = price.cost(900, 4096)

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(json.loads(request.content)["messages"][0]["content"])
        key = canonical_key(prompt["input"])
        if key in cut:  # reasoning ran to the 4,096-token cap: billed, no answer
            message = {"role": "assistant", "content": None, "reasoning_content": "Step. " * 400}
            return _reply(LIGHTNING, message, "length", 4096)
        message = {
            "role": "assistant",
            "content": json.dumps(answers[key]),
            "reasoning_content": "Short.",
        }
        return _reply(LIGHTNING, message, "stop", 300)

    # The A4 arm: Lightning thinking, max_tokens 4,096, last-JSON parsing, plain.
    config = BenchConfig(
        suite="v2", tier="public", provider="fake", models=(LIGHTNING,), plain=True,
        reply_json="last", tuning_replay=False, floor_split="rehearsal", delivery_n=4,
        out=tmp_path / "out",
    )
    factory = _factory(handler, think=True, max_tokens=4096)
    [row] = (await run_bench(config, factory=factory, workspace_root=tmp_path / "ws")).rows
    assert row.delivery_invalid == 2
    costs = row.delivery_run_costs_usd
    assert costs is not None and len(costs) == 4
    assert all(cost >= billed * (1 - 1e-9) for cost in costs[:2]), (costs, billed)


async def test_a_call_cancelled_at_the_run_deadline_is_charged_its_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usage comes back from a call the run's deadline cancels, and the provider may
    have billed it in full, so the run is charged the call's reservation, as RunContext
    keeps it. Here the deadline is 0.5 s and one delivery reply takes 30 s."""
    answers = _answers()
    public_tasks.clear_cache()
    data = public_tasks.load_public("receipts")
    slow = canonical_key(data.splits["delivery"][0].task)
    pinned = data.settings.run_budget
    assert pinned is not None
    monkeypatch.setattr(
        runner, "_run_budget", lambda task: Budget(cost_usd=pinned.cost_usd, compute_s=0.5)
    )
    stalled: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = json.loads(payload["messages"][0]["content"])
        key = canonical_key(prompt["input"])
        if key == slow:
            stalled.append(payload)
            await asyncio.sleep(30)
        return _reply(ULTRA, {"role": "assistant", "content": json.dumps(answers[key])}, "stop", 30)

    # A plain Ultra arm with the evidence profile's 512-token cap.
    config = BenchConfig(
        suite="v2", tier="public", provider="fake", models=(ULTRA,), plain=True,
        floor_split="rehearsal", delivery_n=4, tuning_replay=False, out=tmp_path / "out",
    )
    factory = _factory(handler, max_tokens=512)
    [row] = (await run_bench(config, factory=factory, workspace_root=tmp_path / "ws")).rows
    assert row.delivery_over_budget == 1 and row.delivery_invalid == 0
    [payload] = stalled
    request = ModelRequest(
        prompt=payload["messages"][0]["content"], max_output_tokens=payload["max_tokens"]
    )
    reservation = factory(ULTRA).quote(request).cost_usd
    costs = row.delivery_run_costs_usd
    assert costs is not None and costs[0] == pytest.approx(reservation, rel=1e-9)
    assert all(0 < cost < reservation for cost in costs[1:])  # the others are billed usage


async def test_a_cancelled_call_counts_its_reservation_dollars_and_no_tokens() -> None:
    """No usage came back, so the meter adds the reservation's dollars and claims no
    tokens the provider never reported."""
    sent = asyncio.Event()

    async def stall(_: httpx.Request) -> httpx.Response:
        sent.set()
        await asyncio.sleep(30)
        raise AssertionError("the call is cancelled before any reply")

    meter = CallMeter()
    adapter = instrument(_factory(stall, max_tokens=512)(ULTRA), meter)
    request = ModelRequest(prompt="Read this receipt: TOTAL 1,200")
    call = asyncio.ensure_future(adapter.complete(request))
    await sent.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert (meter.calls, meter.failed) == (1, 1)
    assert meter.cost_usd == adapter.quote(request).cost_usd > 0
    assert (meter.tokens_in, meter.tokens_out) == (0, 0)


async def test_only_billed_or_cancelled_failures_are_charged() -> None:
    """A refusal before any reply (an HTTP error after the retries) costs nothing; a reply
    cut in its reasoning costs the usage the provider reported for it."""
    request = ModelRequest(prompt="Read this receipt: TOTAL 1,200")

    def refused(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    def cut(_: httpx.Request) -> httpx.Response:
        message = {"role": "assistant", "content": None, "reasoning_content": "Step. " * 40}
        return _reply(LIGHTNING, message, "length", 4096)

    price = price_for(load_prices(), LIGHTNING)
    for handler, billed in ((refused, None), (cut, price.cost(900, 4096))):
        meter = CallMeter()
        adapter = instrument(_factory(handler, think=True, max_tokens=4096)(LIGHTNING), meter)
        with pytest.raises(AdapterError) as caught:
            await adapter.complete(request)
        assert (meter.calls, meter.failed) == (1, 1)
        if billed is None:
            assert caught.value.usage is None and meter.cost_usd == 0.0
        else:
            assert str(caught.value) == REASONING_CUT
            usage = caught.value.usage
            assert usage is not None and (usage.tokens_in, usage.tokens_out) == (900, 4096)
            assert meter.cost_usd == pytest.approx(billed, rel=1e-12) == usage.cost_usd
            assert (meter.tokens_in, meter.tokens_out) == (900, 4096)


# ------------------------------------------------------------ A5's four configurations


def _flag(key: str, offset: int, modulus: int, below: int) -> bool:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[offset : offset + 4], 16) % modulus < below


def _wrong_first(key: str) -> bool:
    return _flag(key, 0, 100, 12)  # about 12% of first answers are wrong


def _fixed_on_repair(key: str) -> bool:
    return not _flag(key, 4, 3, 1)  # the repaired answer is right about 2 times in 3


class _CheckingModel(FakeAdapter):
    """A model whose first answers are sometimes wrong, whose self-check verdict is exact,
    and whose repaired answer is usually right. Usage stays within the fake quote."""

    def __init__(self, answers: dict[str, JsonValue]) -> None:
        super().__init__("fake")
        self.answers = answers

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        prompt, _ = json.JSONDecoder().raw_decode(request.prompt)
        key = canonical_key(prompt["input"])
        right = self.answers[key]
        if prompt.get("operation") == "verify_candidate":
            text = json.dumps({"passed": prompt["candidate"] == right, "reason": "checked"})
        elif not _wrong_first(key) or (
            "Repair the candidate" in request.prompt and _fixed_on_repair(key)
        ):
            text = json.dumps(right)
        else:
            assert isinstance(right, dict) and isinstance(right["item_count"], int)
            text = json.dumps({**right, "item_count": right["item_count"] + 1})
        return ModelResponse(
            text=text, tokens_in=8, tokens_out=2, cost_usd=self._price.cost(8, 2),
            model=self.model,
        )


async def test_a5_can_keep_a_check_and_repair_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {
        canonical_key(case.task): case.expected
        for split in public_tasks.load_public("receipts").splits.values()
        for case in split
    }
    measured: list[tuple[dict[str, Any], Any, Sequence[Any]]] = []
    original = tuning.evaluate_cases

    async def spy(artifact: Any, floor: Any, cases: Any, *args: Any, **kwargs: Any) -> Any:
        measurement = await original(artifact, floor, cases, *args, **kwargs)
        measured.append((dict(artifact.params), measurement, list(cases)))
        return measurement

    monkeypatch.setattr(tuning, "evaluate_cases", spy)
    model = _CheckingModel(answers)
    config = BenchConfig(
        suite="v2", tier="public", provider="fake", max_candidates=4,
        floor_split="rehearsal", out=tmp_path / "out",
    )
    # The A5 arm: Lightning, best of 4 preset configurations, count_as_miss at 2%.
    await run_bench(config, factory=lambda name: model, workspace_root=tmp_path / "ws")
    by_params = {(p["verifier_depth"], p["retries"]): (m, c) for p, m, c in measured}
    plain, cases = by_params[(0, 0)]
    keys = [canonical_key(case.task) for case in cases]
    repaired_right = sum(1 for key in keys if not _wrong_first(key) or _fixed_on_repair(key))
    assert plain.counts.correct == sum(1 for key in keys if not _wrong_first(key))
    assert len(keys) - repaired_right >= 2  # more than the 2% allowance of 75 stays caught
    check_and_repair, _ = by_params[(1, 1)]
    # Every run returned a schema-valid answer: the caught ones are misses, not invalid.
    assert check_and_repair.counts.invalid == 0, check_and_repair.counts
    assert check_and_repair.counts.correct == repaired_right > plain.counts.correct
