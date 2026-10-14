"""Phase 9 Pi trusted tools: the ``TrustedTool`` model, grants and the capability RPC.

The integration tests drive the real pinned Pi 0.73.1 SDK from ``FakeAdapter``
envelopes; the forged-peer tests probe the Python protocol boundary directly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from architect.backends.pi import FORGE_TOOL_NAMES, PiBackend, PiConfig, PiError, TrustedTool
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.runtime import BudgetExceeded, RunContext
from architect.spec import Budget

SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"source": {"type": "string"}},
    "required": ["source"],
    "additionalProperties": False,
}


async def callback(arguments: dict[str, JsonValue]) -> JsonValue:
    return {"ok": True}


def tool(**changes: Any) -> TrustedTool:
    values: dict[str, Any] = {
        "name": "forge_check",
        "description": "Validate the draft harness.",
        "parameters": SCHEMA,
        "callback": callback,
        "max_calls": 200,
    }
    values.update(changes)
    return TrustedTool(**values)


def test_forge_tool_names_are_the_closed_five() -> None:
    assert FORGE_TOOL_NAMES == frozenset(
        {"forge_check", "forge_dryrun", "forge_measure", "forge_submit", "forge_spawn"}
    )
    assert isinstance(FORGE_TOOL_NAMES, frozenset)


def test_trusted_tool_defaults_and_frozen() -> None:
    value = tool()
    assert value.max_result_chars == 16384
    assert value.requires_build is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.max_calls = 1  # type: ignore[misc]


def test_trusted_tool_accepts_boundaries() -> None:
    assert tool(name="a").name == "a"
    assert tool(name="a" + "b" * 63).name == "a" + "b" * 63
    assert tool(name="compile_agent", requires_build=False).requires_build is False
    assert tool(description="x" * 1024).description == "x" * 1024
    assert tool(description="").description == ""
    assert tool(max_calls=0).max_calls == 0
    assert tool(max_result_chars=1).max_result_chars == 1
    for name in FORGE_TOOL_NAMES:
        assert tool(name=name).name == name


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "Forge_check"},
        {"name": "1forge"},
        {"name": "forge-check"},
        {"name": "forge check"},
        {"name": "a" + "b" * 64},
        {"name": "forge_check\n"},
        {"name": 7},
        {"description": "x" * 1025},
        {"description": None},
        {"parameters": {"type": "object"}},
        {"parameters": {"type": "object", "additionalProperties": True}},
        {"parameters": {"type": "string", "additionalProperties": False}},
        {"parameters": []},
        {"callback": None},
        {"max_calls": -1},
        {"max_calls": 1.0},
        {"max_calls": True},
        {"max_result_chars": 0},
        {"max_result_chars": "16384"},
        {"requires_build": 1},
    ],
)
def test_trusted_tool_rejects_out_of_contract_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="^Invalid trusted tool$"):
        tool(**changes)


# ---------------------------------------------------------------------------
# WP2: grants, the real Pi extension tool and the ``capability`` RPC.
# ---------------------------------------------------------------------------


def response(text: str, cost: float = 0) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=cost)


def calls(name: str, **args: object) -> str:
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})


class Recording(FakeAdapter):
    """FakeAdapter that keeps prompts so a test can see what Pi returned to the model."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(responses=[response(text) for text in texts])
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return await super().complete(request)


def build_context(repo: Path, texts: list[str], *, build: bool = True) -> RunContext:
    ctx = RunContext(
        Recording(texts), Budget(cost_usd=1, compute_s=60), repository=repo, is_evaluation=False
    )
    # ``RunContext.is_build`` is the WP3 constructor kwarg; the backend reads it by getattr.
    ctx.is_build = build  # type: ignore[attr-defined]
    return ctx


def pack(*tools: str) -> dict[str, Any]:
    return {
        "tools": list(tools),
        "prompt_fragment": "Write harness.py.",
        "context_policy": {"max_characters": 60000},
        "subagent_rules": [],
    }


def backend(*tools: TrustedTool, **config: Any) -> PiBackend:
    names = tuple(item.name for item in tools)
    return PiBackend(
        PiConfig(allowed_tools=("read", "write", *names), **config),
        capabilities={item.name: item for item in tools},
    )


def prompts(ctx: RunContext) -> list[str]:
    assert isinstance(ctx.adapter, Recording)
    return ctx.adapter.prompts


def tool_replies(ctx: RunContext) -> list[tuple[str, Any]]:
    """The tool results Pi fed back to the model (JSON decoded when possible), in order."""
    found: list[tuple[str, Any]] = []
    for prompt in prompts(ctx):
        for message in json.loads(prompt)["pi_context"]["messages"]:
            if message.get("role") == "toolResult":
                text = "".join(
                    item.get("text", "") for item in message["content"] if item["type"] == "text"
                )
                try:
                    value: Any = json.loads(text)
                except ValueError:
                    value = text
                if (message["toolName"], value) not in found:
                    found.append((message["toolName"], value))
    return found


def listed_tools(ctx: RunContext) -> dict[str, Any]:
    # The granted tools as the model sees them (the Pi context's copy is not resent).
    first = json.loads(prompts(ctx)[0])
    return {item["name"]: item for item in first["tools"]}


# -- construction ------------------------------------------------------------


def test_config_accepts_forge_tool_names() -> None:
    names = ("read", "write", "edit", *sorted(FORGE_TOOL_NAMES))
    assert PiConfig(allowed_tools=names).allowed_tools == names
    with pytest.raises(ValueError, match="Unsupported or duplicate"):
        PiConfig(allowed_tools=("forge_check", "forge_check"))
    with pytest.raises(ValueError, match="Unsupported or duplicate"):
        PiConfig(allowed_tools=("forge_unknown",))


@pytest.mark.parametrize(
    "allowed,capabilities",
    [
        (("forge_check",), {}),  # granted name without a trusted tool
        (("read",), {"forge_check": "forge_check"}),  # capability not in allowed_tools
        (("forge_check", "forge_submit"), {"forge_check": "forge_check"}),
        (("forge_check",), {"forge_check": "forge_measure"}),  # key/name mismatch
        (("forge_check", "forge_measure"), {"forge_check": "forge_measure"}),
    ],
)
def test_grant_must_match_capabilities_exactly(
    allowed: tuple[str, ...], capabilities: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="^Unsupported or duplicate Pi tools$"):
        PiBackend(
            PiConfig(allowed_tools=allowed),
            capabilities={key: tool(name=name) for key, name in capabilities.items()},
        )


def test_capabilities_cannot_shadow_builtin_tools() -> None:
    with pytest.raises(ValueError, match="Unsupported or duplicate"):
        PiBackend(PiConfig(allowed_tools=("read",)), capabilities={"read": tool(name="read")})
    with pytest.raises(ValueError, match="Unsupported or duplicate"):
        PiBackend(
            PiConfig(allowed_tools=("forge_check",)),
            capabilities={"forge_check": object()},  # type: ignore[dict-item]
        )


def test_invalid_capability_schema_is_rejected_at_construction() -> None:
    bad = tool(parameters={"type": "object", "additionalProperties": False, "required": 3})
    with pytest.raises(ValueError, match="^Invalid trusted tool$"):
        PiBackend(PiConfig(allowed_tools=("forge_check",)), capabilities={"forge_check": bad})


def test_default_backend_has_no_capabilities_and_compile_agent_is_a_trusted_tool() -> None:
    assert dict(PiBackend().capabilities) == {}
    assert PiBackend().compile_agent is None

    async def compiler(args: dict[str, JsonValue]) -> JsonValue:
        return "ok"

    compile_agent = PiBackend(
        PiConfig(allowed_tools=("compile_agent",)), compiler_tool=compiler
    ).compile_agent
    assert isinstance(compile_agent, TrustedTool)
    assert compile_agent.name == "compile_agent"
    assert compile_agent.requires_build is False
    assert compile_agent.callback is compiler
    assert compile_agent.parameters["additionalProperties"] is False


# -- the real pinned SDK -------------------------------------------------------


async def test_trusted_tool_is_actual_extension_tool(tmp_path: Path) -> None:
    seen: list[dict[str, JsonValue]] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True, "code": None, "nodes": 3, "truncated": False}

    pi = backend(tool(callback=check))
    ctx = build_context(tmp_path, [calls("forge_check", source="harness.py"), "done"])
    assert await pi.run(ctx, {}, pack("read", "write", "forge_check")) == "done"
    assert seen == [{"source": "harness.py"}]
    assert tool_replies(ctx) == [
        ("forge_check", {"ok": True, "code": None, "nodes": 3, "truncated": False})
    ]
    # The model saw the tool with its fixed description and exact parameters schema.
    listed = listed_tools(ctx)
    assert listed["forge_check"]["description"] == "Validate the draft harness."
    assert listed["forge_check"]["parameters"]["additionalProperties"] is False
    evidence = pi.last_evidence
    assert evidence is not None and evidence.extension_loaded and evidence.tool_calls == 1


async def test_typebox_rejects_extra_keys_before_any_python_rpc(tmp_path: Path) -> None:
    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    pi = backend(tool(callback=check))
    ctx = build_context(tmp_path, [calls("forge_check", source="harness.py", extra=True), "done"])
    assert await pi.run(ctx, {}, pack("forge_check")) == "done"
    assert seen == []
    # Pi validation failed the call before the approval RPC reached Python.
    assert pi.last_evidence is not None and pi.last_evidence.tool_calls == 0
    [(name, text)] = tool_replies(ctx)
    assert name == "forge_check" and "Validation failed" in text


async def test_oversized_result_is_result_too_large(tmp_path: Path) -> None:
    async def huge(arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": True, "blob": "x" * 20000}

    pi = backend(tool(callback=huge), max_protocol_bytes=16384)
    ctx = build_context(tmp_path, [calls("forge_check", source="a"), "done"])
    assert await pi.run(ctx, {}, pack("forge_check")) == "done"
    assert tool_replies(ctx) == [("forge_check", {"ok": False, "code": "result_too_large"})]


async def test_callback_truncation_is_trusted(tmp_path: Path) -> None:
    async def truncated(arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": True, "blob": "y" * 17000, "truncated": True}

    pi = backend(tool(callback=truncated))
    ctx = build_context(tmp_path, [calls("forge_check", source="a"), "done"])
    assert await pi.run(ctx, {}, pack("forge_check")) == "done"
    [(name, value)] = tool_replies(ctx)
    assert name == "forge_check" and value["truncated"] is True and len(value["blob"]) == 17000


async def test_per_tool_cap_is_soft_forge_tool_cap(tmp_path: Path) -> None:
    count = 0

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        nonlocal count
        count += 1
        return {"ok": True, "call": count}

    pi = backend(tool(callback=check, max_calls=1))
    first = calls("forge_check", source="a")
    second = first.replace("call1", "call2")
    ctx = build_context(tmp_path, [first, second, "done"])
    assert await pi.run(ctx, {}, pack("forge_check")) == "done"
    assert count == 1
    assert tool_replies(ctx) == [
        ("forge_check", {"ok": True, "call": 1}),
        ("forge_check", {"ok": False, "code": "forge_tool_cap"}),
    ]


async def test_callback_failure_is_soft_and_content_free(tmp_path: Path) -> None:
    async def broken(arguments: dict[str, JsonValue]) -> JsonValue:
        raise RuntimeError("SECRET_EXCEPTION_TEXT")

    pi = backend(tool(callback=broken))
    ctx = build_context(tmp_path, [calls("forge_check", source="a"), "done"])
    assert await pi.run(ctx, {}, pack("forge_check")) == "done"
    assert tool_replies(ctx) == [("forge_check", {"ok": False, "code": "tool_failed"})]
    assert not any("SECRET" in prompt for prompt in prompts(ctx))


async def test_capabilities_are_dropped_outside_a_build_context(tmp_path: Path) -> None:
    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    pi = backend(tool(callback=check))
    ctx = build_context(tmp_path, [calls("forge_check", source="a"), "done"], build=False)
    assert await pi.run(ctx, {}, pack("forge_check", "read")) == "done"
    assert seen == []
    assert "forge_check" not in listed_tools(ctx)
    [(name, text)] = tool_replies(ctx)
    assert name == "forge_check" and "not found" in text


async def test_swallowed_budget_denial_in_callback_stops_same_batch_write(
    tmp_path: Path,
) -> None:
    from architect.runtime.wallet import compilation_wallet

    batch = json.dumps(
        {
            "tool_calls": [
                {"id": "measure", "name": "forge_measure", "arguments": {"source": "a"}},
                {"id": "write", "name": "write", "arguments": {"path": "denied", "content": "x"}},
            ]
        }
    )
    seen: list[object] = []

    async def measure(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        child = RunContext(
            FakeAdapter(responses=[response("child", cost=0.4)]),
            Budget(cost_usd=1, compute_s=60),
        )
        try:
            await child.call("nested")
        except BudgetExceeded:
            return {"ok": False, "code": "budget_exceeded"}
        raise AssertionError("The shared money wallet must refuse the measurement")

    pi = backend(tool(name="forge_measure", callback=measure))
    ctx = build_context(tmp_path, [])
    ctx.adapter = FakeAdapter(responses=[response(batch, cost=0.4)])
    with compilation_wallet(Budget(cost_usd=0.7, compute_s=60)) as wallet:
        with pytest.raises(BudgetExceeded, match="[Cc]ompilation budget"):
            await pi.run(ctx, {}, pack("forge_measure", "write"))
        assert wallet.exhausted
    assert seen == [{"source": "a"}]
    assert not (tmp_path / "denied").exists()
    assert pi.last_evidence is not None and pi.last_evidence.tool_calls == 1


async def test_budget_exceeded_from_callback_propagates(tmp_path: Path) -> None:
    async def measure(arguments: dict[str, JsonValue]) -> JsonValue:
        raise BudgetExceeded("Forge wallet exhausted")

    pi = backend(tool(name="forge_measure", callback=measure))
    ctx = build_context(tmp_path, [calls("forge_measure", source="a"), "never"])
    with pytest.raises(BudgetExceeded, match="Forge wallet"):
        await pi.run(ctx, {}, pack("forge_measure"))
    assert len(prompts(ctx)) == 1


# -- forged peers (Python protocol boundary) -----------------------------------


class ProtocolPeer:
    """An inert IPC peer that replays messages; never a coding loop."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.pid = 987654321
        self.returncode: int | None = None
        self.sent: list[dict[str, Any]] = []
        self.stdin = self
        self.stdout = asyncio.StreamReader(limit=2 * 1048576)
        self.stderr = asyncio.StreamReader(limit=2 * 1048576)
        for message in messages:
            self.stdout.feed_data((json.dumps(message) + "\n").encode())
        self.stdout.feed_eof()

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


READY = {
    "kind": "ready",
    "extension_loaded": True,
    "versions": {name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")},
}


def install(monkeypatch: pytest.MonkeyPatch, peer: ProtocolPeer) -> None:
    async def spawn(*args: object, **kwargs: object) -> ProtocolPeer:
        return peer

    monkeypatch.setattr("architect.backends.pi.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("architect.backends.pi._windows_job", lambda pid: None)
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pid, sig: peer.kill())


def capability(identity: int, name: str, arguments: object) -> dict[str, Any]:
    return {"kind": "capability", "id": identity, "name": name, "arguments": arguments}


def replies(peer: ProtocolPeer) -> list[Any]:
    return [item["value"] for item in peer.sent if item.get("kind") != "init"]


def init_message(peer: ProtocolPeer) -> dict[str, Any]:
    [init] = [item for item in peer.sent if item.get("kind") == "init"]
    return init


async def test_forged_ungranted_capability_rpc_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    peer = ProtocolPeer([READY, capability(1, "forge_submit", {"source": "a"})])
    install(monkeypatch, peer)
    with pytest.raises(PiError, match="^Capability denied$"):
        await backend(tool(callback=check)).run(
            build_context(tmp_path, []), {}, pack("forge_check")
        )
    assert seen == [] and replies(peer) == []


async def test_capability_rpc_is_denied_when_not_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolPeer([READY, capability(1, "forge_check", {"source": "a"})])
    install(monkeypatch, peer)
    with pytest.raises(PiError, match="^Capability denied$"):
        await backend(tool()).run(build_context(tmp_path, [], build=False), {}, pack("forge_check"))
    init = init_message(peer)
    assert init["capabilities"] == [] and "forge_check" not in init["tools"]
    assert replies(peer) == []


async def test_python_schema_mismatch_is_soft_invalid_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    peer = ProtocolPeer(
        [
            READY,
            capability(1, "forge_check", {"source": 5}),
            capability(2, "forge_check", {"source": "a", "extra": 1}),
            capability(3, "forge_check", ["not", "an", "object"]),
            capability(4, "forge_check", {"source": "a"}),
        ]
    )
    install(monkeypatch, peer)
    with pytest.raises(PiError, match="Pi protocol"):
        await backend(tool(callback=check, max_calls=1)).run(
            build_context(tmp_path, []), {}, pack("forge_check")
        )
    invalid = {"ok": False, "code": "invalid_arguments"}
    # Rejected calls never consume the per-tool cap.
    assert replies(peer) == [invalid, invalid, invalid, {"ok": True}]
    assert seen == [{"source": "a"}]
    assert init_message(peer)["capabilities"] == [
        {"name": "forge_check", "description": "Validate the draft harness.", "parameters": SCHEMA}
    ]


async def test_oversized_truncated_result_never_trips_protocol_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def huge(arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": True, "blob": "z" * 9000, "truncated": True}

    async def unserializable(arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": float("nan")}

    peer = ProtocolPeer(
        [
            READY,
            capability(1, "forge_check", {"source": "a"}),
            capability(2, "forge_dryrun", {"source": "a"}),
        ]
    )
    install(monkeypatch, peer)
    pi = backend(
        tool(callback=huge, max_result_chars=1024),
        tool(name="forge_dryrun", callback=unserializable),
        max_protocol_bytes=8192,
    )
    with pytest.raises(PiError, match="Pi protocol"):
        await pi.run(build_context(tmp_path, []), {}, pack("forge_check", "forge_dryrun"))
    assert replies(peer) == [
        {"ok": False, "code": "result_too_large"},
        {"ok": False, "code": "tool_failed"},
    ]


async def test_untruncated_result_over_wire_room_is_soft_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under max_result_chars (default 16384) yet larger than the 4096-byte wire:
    # the raw result must never be written, so the protocol limit cannot trip.
    # The wire leaves room for init, which carries temp paths of any length.
    sizes = iter([9000, 10])

    async def blob(arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": True, "blob": "x" * next(sizes)}

    peer = ProtocolPeer(
        [
            READY,
            capability(1, "forge_check", {"source": "a"}),
            capability(2, "forge_check", {"source": "a"}),
        ]
    )
    install(monkeypatch, peer)
    pi = backend(tool(callback=blob), max_protocol_bytes=4096)
    with pytest.raises(PiError, match="Pi protocol"):
        await pi.run(build_context(tmp_path, []), {}, pack("forge_check"))
    assert replies(peer) == [
        {"ok": False, "code": "result_too_large"},
        {"ok": True, "blob": "x" * 10},
    ]
    assert all(len(json.dumps(item)) + 1 <= 4096 for item in peer.sent)


async def test_capability_calls_count_toward_max_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolPeer(
        [
            READY,
            capability(1, "forge_check", {"source": "a"}),
            capability(2, "forge_check", {"source": "a"}),
        ]
    )
    install(monkeypatch, peer)
    with pytest.raises(PiError, match="tool limit"):
        await backend(tool(), max_turns=1).run(build_context(tmp_path, []), {}, pack("forge_check"))
    assert replies(peer) == [{"ok": True}]


async def test_non_build_capability_follows_the_compile_agent_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolPeer([READY, capability(1, "forge_check", {"source": "a"})])
    install(monkeypatch, peer)
    with pytest.raises(PiError, match="Pi protocol"):
        await backend(tool(requires_build=False)).run(
            build_context(tmp_path, [], build=False), {}, pack("forge_check")
        )
    assert replies(peer) == [{"ok": True}]
    peer = ProtocolPeer([READY, capability(1, "forge_check", {"source": "a"})])
    install(monkeypatch, peer)
    evaluation = build_context(tmp_path, [], build=False)
    evaluation.is_evaluation = True
    with pytest.raises(PiError, match="^Capability denied$"):
        await backend(tool(requires_build=False)).run(evaluation, {}, pack("forge_check"))


CODE_PACK: dict[str, JsonValue] = {
    "tools": ["forge_check"],
    "prompt_fragment": "x",
    "subagent_rules": [],
}
HARNESS_SOURCE = (
    "async def run(context, task):\n"
    "    return await context.code({}, {'tools': ['forge_check'], 'prompt_fragment': 'x', "
    "'subagent_rules': []})\n"
)


async def _code_harness(context: RunContext, task: JsonValue) -> JsonValue:
    return await context.code({}, CODE_PACK)


def _spec(**changes: Any) -> Any:
    from architect.spec import FloorDefinition, FloorKind, Spec

    return Spec(
        budget=Budget(),
        task="t",
        input_schema={},
        output_schema={},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="d"),
        **changes,
    )


async def test_runtime_evaluate_never_grants_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.runtime import Runtime
    from architect.telemetry import TelemetryStore

    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    peer = ProtocolPeer([READY, capability(1, "forge_check", {"source": "a"})])
    install(monkeypatch, peer)
    runtime = Runtime(
        TelemetryStore(tmp_path / "telemetry"), pi_backend=backend(tool(callback=check))
    )
    with pytest.raises(PiError, match="^Capability denied$"):
        await runtime.evaluate(_spec(), _code_harness, {}, FakeAdapter())
    assert seen == []
    init = init_message(peer)
    assert init["capabilities"] == [] and "forge_check" not in init["tools"]


async def test_runtime_run_never_grants_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect._authority import _sign_report
    from architect.runtime import Runtime
    from architect.spec import HarnessArtifact, Lifecycle, TestReport
    from architect.telemetry import TelemetryStore

    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text(HARNESS_SOURCE, encoding="utf-8")
    value = HarnessArtifact(
        spec=_spec(lifecycle=Lifecycle.INSTALL),
        preset="toy",
        model="fake",
        components=("model-call",),
        directory=directory,
    )
    value.save()
    report = TestReport(
        spec_hash=value.spec.spec_hash,
        artifact_digest=value.artifact_digest,
        passed=True,
        floor_score=0.9,
        floor_limit=0.8,
        floor_kind=value.spec.floor.kind,
        tokens=5,
        cost_usd=0.01,
        compute_s=0.01,
        component_count=value.component_count,
        verification_id="fixture-verifier",
    )
    signed = report.model_copy(
        update={"signature": _sign_report(report.model_dump(mode="json", exclude={"signature"}))}
    )
    value = value.model_copy(update={"report": signed})
    value.save()
    assert value.licensed

    seen: list[object] = []

    async def check(arguments: dict[str, JsonValue]) -> JsonValue:
        seen.append(arguments)
        return {"ok": True}

    peer = ProtocolPeer([READY, capability(1, "forge_check", {"source": "a"})])
    install(monkeypatch, peer)
    runtime = Runtime(
        TelemetryStore(tmp_path / "telemetry"), pi_backend=backend(tool(callback=check))
    )
    with pytest.raises(PiError, match="^Capability denied$"):
        await runtime.run(value, {}, FakeAdapter())
    assert seen == []
    init = init_message(peer)
    assert init["capabilities"] == [] and "forge_check" not in init["tools"]
