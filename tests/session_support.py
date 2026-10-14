"""Shared fixtures for the session tests: a fake profile, scripted runs and fake adapters.

Everything here is offline: the fake provider answers from fixture files, the clock is a
counter, and the console is a script. Nothing is POSIX-only and every path is short
enough for the registry's Windows path rule.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from architect.models import AdapterError, FakeAdapter, ModelAdapter, ModelRequest, ModelResponse
from architect.profiles import Profile, Services, build_services
from architect.session import ScriptedConsole, Session
from architect.session.state import State
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

OUTPUT = "ok"


def make_spec(*, lifecycle: str = "expire", dataset: str = "tasks") -> Spec:
    """The Spec the fake inference fixture proposes; deterministic, so its hash is known."""
    # input_schema names a property but requires none, so the fixture cases' ``{}`` inputs
    # still validate while the schema is not one the session calls missing.
    return Spec(
        task="Return ok",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset=dataset),
        budget=Budget(),
        lifecycle=Lifecycle(lifecycle),
    )


def spec_text(lifecycle: str = "expire") -> str:
    return make_spec(lifecycle=lifecycle).model_dump_json()


def response(text: str, *, cost: float = 0.00002) -> dict[str, object]:
    return {"text": text, "tokens_in": 8, "tokens_out": 12, "cost_usd": cost, "model": "fake"}


def fake_profile(
    tmp_path: Path,
    *,
    inference_text: str | None = None,
    responses: int = 40,
    cases: int = 6,
    lifecycle: str = "expire",
    output: str = OUTPUT,
    held_out_expected: str | None = None,
    case_metadata: dict[str, object] | None = None,
    inference_calls: int = 4,
) -> Path:
    """Write ``<tmp_path>/w/session-profile.json`` (workspace ".") with fake fixtures.

    ``held_out_expected`` changes the expected value of the held-out partition (sorted ids,
    odd positions) so Stage 2 passes and Stage 4 fails; ``case_metadata`` is merged into
    every case (a SYNTHETIC floor is ``{"synthetic": True, ...}``).
    """
    workspace = tmp_path / "w"
    workspace.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for index in range(cases):
        expected = output
        if held_out_expected is not None and index % 2 == 1:
            expected = held_out_expected
        row: dict[str, object] = {"case_id": str(index), "task": {}, "expected": expected}
        if case_metadata is not None:
            row["metadata"] = dict(case_metadata)
        rows.append(row)
    (workspace / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
    (workspace / "responses.json").write_text(
        json.dumps([response(output) for _ in range(responses)]), encoding="utf-8"
    )
    text = spec_text(lifecycle) if inference_text is None else inference_text
    (workspace / "inference.json").write_text(
        json.dumps([response(text) for _ in range(inference_calls)]), encoding="utf-8"
    )
    profile = workspace / "session-profile.json"
    profile.write_text(
        json.dumps(
            {
                "workspace": ".",
                "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
                "inference": {"provider": "fake", "model": "fake", "fixture": "inference.json"},
                "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
                "compile": {"max_candidates": 1, "enable_rewriting": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return profile


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BlockingFakeAdapter(FakeAdapter):
    """Completes only after ``release`` is set; ``started`` is set on the first call."""

    def __init__(self, model: str = "fake") -> None:
        super().__init__(model=model)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await self.release.wait()
        return await super().complete(request)


class SecretFakeAdapter(FakeAdapter):
    """Every call fails with provider text that must never reach the terminal."""

    def __init__(self, model: str = "fake") -> None:
        super().__init__(model=model)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        raise AdapterError("sk-secret-4242 leaked in a provider message")


class SentinelRequestAdapter(FakeAdapter):
    """Records the exact request objects it receives; the session must pass them through."""

    def __init__(self, model: str = "fake") -> None:
        super().__init__(model=model)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert type(request) is ModelRequest
        self.requests.append(request)
        return await super().complete(request)


class QuotingFakeAdapter(FakeAdapter):
    """A fake whose quote reserves ``cost`` per call, like a real CLI model."""

    def __init__(self, cost: float, model: str = "fake") -> None:
        super().__init__(
            model=model,
            responses=[
                ModelResponse(text=OUTPUT, tokens_in=8, tokens_out=12, cost_usd=cost, model=model)
                for _ in range(40)
            ],
        )


def swapping_factory(
    adapter: ModelAdapter, *, inference: bool = False
) -> Callable[[Profile, Path], Services]:
    """A services factory whose compile/run adapters (and optionally Stage 0) are ``adapter``."""

    def factory(profile: Profile, workspace: Path) -> Services:
        services = build_services(profile, workspace)

        def adapter_factory(model: str) -> ModelAdapter:
            return adapter

        services.compiler.adapter_factory = adapter_factory
        services.runtime.adapter_factory = adapter_factory
        if inference:
            services.compiler.inference_adapter = adapter
        return dataclasses.replace(services, adapter_factory=adapter_factory)

    return factory


def make_session(
    lines: list[str],
    tmp_path: Path,
    *,
    interactive: bool = False,
    **kw: object,
) -> tuple[Session, ScriptedConsole]:
    console = ScriptedConsole(lines, interactive=interactive)
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        **kw,  # type: ignore[arg-type]
    )
    return session, console


def run_session(
    lines: list[str], tmp_path: Path, **kw: Any
) -> tuple[int, str, Session]:
    session, console = make_session(lines, tmp_path, **kw)
    code = asyncio.run(session.run())
    return code, console.transcript(), session


def run_script(lines: list[str], tmp_path: Path, **kw: object) -> tuple[int, str]:
    code, transcript, _ = run_session(lines, tmp_path, **kw)
    return code, transcript


Origin = Literal["operator", "file", "model_draft"]


def unwrap(text: str) -> str:
    """Join the continuation lines ``render.wrap`` produces so path-bearing lines can be matched."""
    return re.sub(r"\n {2,}", " ", text)


def dense(text: str) -> str:
    """Drop every whitespace so a path that ``wrap`` broke mid-token can still be matched."""
    return re.sub(r"\s+", "", text)


class GatedConsole(ScriptedConsole):
    """An interactive console whose lines are typed only once the session reaches a state.

    ``ScriptedConsole`` answers instantly, so with ``interactive=True`` every line would be
    consumed during the first busy state. Gating each line on ``session.state`` makes
    "type this while the compile runs" deterministic. ``eof_at`` gates the EOF the same
    way: an operator who closes the terminal only once the session is back in that state
    (without it, EOF arrives as soon as the lines run out, cancelling running work).
    """

    def __init__(self, lines: list[tuple[str, State]], *, eof_at: State | None = None) -> None:
        super().__init__([line for line, _ in lines], interactive=True)
        self._states = [state for _, state in lines]
        self._eof_at = eof_at
        self.session: Session | None = None

    async def _wait_for(self, wanted: State) -> None:
        for _ in range(60_000):  # at most a minute; a wrong gate fails instead of hanging
            if self.session is None or self.session.state is wanted:
                break
            await asyncio.sleep(0.001)

    async def read(self, prompt: str) -> str | None:
        if self._index < len(self._states):
            await self._wait_for(self._states[self._index])
        elif self._eof_at is not None:
            await self._wait_for(self._eof_at)
        return await super().read(prompt)


def run_gated(
    lines: list[tuple[str, State]],
    tmp_path: Path,
    *,
    eof_at: State | None = None,
    **kw: object,
) -> tuple[int, str, Session]:
    console = GatedConsole(lines, eof_at=eof_at)
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        **kw,  # type: ignore[arg-type]
    )
    console.session = session
    code = asyncio.run(session.run())
    return code, console.transcript(), session


class StubbornFakeAdapter(BlockingFakeAdapter):
    """Blocks like ``BlockingFakeAdapter``; once cancelled it unwinds only after ``cancel_release``.

    Models a provider call whose cancellation takes time (a subprocess being killed), so a
    test can land a second event (Ctrl-C, EOF, a typed quit) while the work is still
    cancelling. ``cancel_seen`` is set when the cancellation reaches the call.
    """

    def __init__(self, model: str = "fake") -> None:
        super().__init__(model)
        self.cancel_seen = asyncio.Event()
        self.cancel_release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.cancel_release.wait()
            raise
        return await FakeAdapter.complete(self, request)


# Nemotron 3 Super's reply to the drafting call for "Sort support emails into billing, bug
# report or question" (2026-10-13), byte for byte: the fifth case is never closed, the array
# is closed and reopened, and the sixth case never opens. json.loads refuses it.
MALFORMED_DRAFT = (
    "[\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"I was charged twice for my subscription th"
    "is month.\"},\n"
    "    \"expected\": {\"category\": \"billing\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"How do I reset my password?\"},\n"
    "    \"expected\": {\"category\": \"question\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"The app crashes when I try to upload a fil"
    "e larger than 5MB.\"},\n"
    "    \"expected\": {\"category\": \"bug report\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"Can you tell me if your service supports A"
    "PI access?\"},\n"
    "    \"expected\": {\"category\": \"question\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"My invoice shows a fee I didn't authorize "
    "\u2014 please refund it.\"},\n"
    "    \"expected\": {\"category\": \"billing\"\n"
    "  }\n"
    "  ],\n"
    "  [\n"
    "    \"task\": {\"email_text\": \"After the latest update, the login button "
    "does nothing when clicked.\"},\n"
    "    \"expected\": {\"category\": \"bug report\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"What are your business hours on weekends?\""
    "},\n"
    "    \"expected\": {\"category\": \"question\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"I need to update my payment method but can"
    "'t find where to do it in the settings.\"},\n"
    "    \"expected\": {\"category\": \"question\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"The system keeps logging me out every 5 mi"
    "nutes even though I'm active.\"},\n"
    "    \"expected\": {\"category\": \"bug report\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"Why was I charged $19.99 when my plan is o"
    "nly $9.99?\"},\n"
    "    \"expected\": {\"category\": \"billing\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"Is there a way to export my data in CSV fo"
    "rmat?\"},\n"
    "    \"expected\": {\"category\": \"question\"}\n"
    "  },\n"
    "  {\n"
    "    \"task\": {\"email_text\": \"The search function returns no results eve"
    "n when I know the item exists.\"},\n"
    "    \"expected\": {\"category\": \"bug report\"}\n"
    "  }\n"
    "]"
)
