"""The boundary is a restricted JSON capability language, NOT arbitrary Python isolation."""

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.runtime import BudgetExceeded, RunContext
from architect.runtime import _sandbox_worker as worker
from architect.runtime.sandbox import (
    SandboxError,
    _decode,
    _encode,
    _json,
    _reap,
    _validated_tree,
    rebind_source,
    sandbox_harness,
    validate_source,
)
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Spec

DIRECT = "async def run(context, task):\n    return await context.call(str(task), 64)\n"


def artifact(tmp_path: Path, source: str | None = DIRECT) -> HarnessArtifact:
    spec = Spec(
        task="summarize",
        input_schema={},
        output_schema={"type": "string"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="private-set"),
        budget=Budget(),
    )
    preset = get_preset("synthesizer")
    params = preset.validate_params({})
    preset.emit(tmp_path, spec, params, preset.components)
    result = HarnessArtifact(
        spec=spec,
        directory=tmp_path,
        preset=preset.name,
        params=params,
        components=preset.components,
        model="fake",
        execution_mode="sandbox",
    )
    if source is not None:
        (tmp_path / "harness.py").write_text(source, encoding="utf-8")
    return result


@pytest.mark.parametrize(
    "source",
    [
        "import os\nasync def run(context, task):\n    return None",
        "from pathlib import Path\nasync def run(context, task):\n    return None",
        "from pydantic import JsonValue as x\nasync def run(context, task):\n    return None",
        "from architect.runtime import Runtime\nasync def run(context, task):\n    return None",
        "from .pydantic import JsonValue\nasync def run(context, task):\n    return None",
        "class C: pass\nasync def run(context, task):\n    return None",
        "def f(): pass\nasync def run(context, task):\n    return None",
        "@task\nasync def run(context, task):\n    return None",
        "async def run(context, task=open('private')):\n    return None",
        "async def run(context: open('private'), task):\n    return None",
        "async def run(context, task) -> open('private'):\n    return None",
        "async def run(context, task, *args):\n    return None",
        "async def run(context, task):\n    return context.adapter",
        "async def run(context, task):\n    return context.__class__",
        "async def run(context, task):\n    return task.__class__.__mro__",
        "async def run(context, task):\n    return getattr(task, '__class__')",
        "async def run(context, task):\n    return __import__('os').environ",
        "async def run(context, task):\n    return open('private').read()",
        "async def run(context, task):\n    return eval(task)",
        "async def run(context, task):\n    return exec(task)",
        "async def run(context, task):\n    return task['callable']()",
        "async def run(context, task):\n    return [x for x in task]",
        "async def run(context, task):\n    while True:\n        pass",
        "async def run(context, task):\n    for x in task:\n        return x",
        "async def run(context, task):\n    async def inner():\n        return task",
        "async def run(context, task):\n    return (lambda: task)()",
        "async def run(context, task):\n    context = task\n    return context",
        "async def run(context, task):\n    x = context.call\n    return await x(task)",
        "async def run(context, task):\n    return context.call(task)",
        "async def run(context, task):\n    return await context.call(*task)",
        "async def run(context, task):\n    return await context.call(task, **task)",
        "async def run(context, task):\n    return await context.call(task, unknown=1)",
        "async def run(context, task):\n    return await context.call(task, 1, 2)",
        "async def run(context, task):\n    return {**task}",
        "async def run(context, task):\n    return {'a': 1e999}",
        "async def run(context, task):\n    return 'x' * 1000000000",
        "async def run(context, task):\n    return f'{task}'",
        "async def run(context, task):\n    task['x'] = 1\n    return task",
        "async def run(context, task):\n    return task.get('private')",
        "async def run(context, task):\n    import socket\n    return socket.socket()",
        "async def run(context, task):\n    try:\n        return task\n    except: pass",
    ],
)
def test_closed_grammar_rejects_python_capabilities(tmp_path: Path, source: str) -> None:
    value = artifact(tmp_path)
    with pytest.raises(SandboxError):
        validate_source(source, value)


def test_source_node_depth_and_call_caps(tmp_path: Path) -> None:
    value = artifact(tmp_path)
    samples = [
        "#" * 65537,
        "async def run(context, task):\n    return [" + ",".join(["0"] * 2001) + "]",
        "async def run(context, task):\n    return " + "[" * 45 + "0" + "]" * 45,
        "async def run(context, task):\n" + "    await context.call('x')\n" * 65,
    ]
    for source in samples:
        with pytest.raises(SandboxError):
            validate_source(source, value)


async def test_emitted_wrapper_runs_through_parent_context(tmp_path: Path) -> None:
    value = artifact(tmp_path, None)
    context = RunContext(FakeAdapter(), value.spec.budget)
    assert await sandbox_harness(value)(context, {"input": "not code"}) == "ok"
    assert context.tokens_in + context.tokens_out > 0


async def test_guarded_control_flow_and_json_operations(tmp_path: Path) -> None:
    source = """async def run(context, task):
    prompt = 'compressed: ' + task['text']
    if task['skip'] and len(task['text']) != 0:
        return {'skipped': True, 'items': [1, -2, None] + [3]}
    answer = await context.call(prompt, max_output_tokens=64)
    if answer in ['ok', 'yes']:
        return answer
    return 'no'
"""
    value = artifact(tmp_path, source)
    context = RunContext(FakeAdapter(), value.spec.budget)
    harness = sandbox_harness(value)
    assert await harness(context, {"text": "hello", "skip": True}) == {
        "skipped": True,
        "items": [1, -2, None, 3],
    }
    assert context.tokens_in == 0
    assert await harness(context, {"text": "hello", "skip": False}) == "ok"
    assert context.tokens_in > 0


async def test_task_and_model_injection_stay_data(tmp_path: Path) -> None:
    value = artifact(tmp_path, "async def run(context, task):\n    return task\n")
    marker = tmp_path / "must-not-exist"
    payload = f"__import__('pathlib').Path({str(marker)!r}).write_text('oops')"
    context = RunContext(FakeAdapter(), value.spec.budget)
    assert await sandbox_harness(value)(context, payload) == payload
    assert not marker.exists()
    value = artifact(tmp_path)
    adapter = FakeAdapter(
        responses=[
            ModelResponse(
                text=payload,
                tokens_in=1,
                tokens_out=1,
                cost_usd=0,
                model="fake",
            )
        ]
    )
    assert await sandbox_harness(value)(RunContext(adapter, Budget()), payload) == payload
    assert not marker.exists()


def test_metadata_injection_is_a_literal_and_binding_is_exact(tmp_path: Path) -> None:
    value = artifact(tmp_path, None)
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    validate_source(source, value)
    updates: list[dict[str, object]] = [
        {"params": {**value.params, "terseness": 1}},
        {"components": ("synthesis",)},
        {"preset": "verifier", "params": {}, "components": ("verdict",)},
        {"spec": value.spec.model_copy(update={"task": "new task"})},
    ]
    for update in updates:
        with pytest.raises(SandboxError):
            validate_source(source, value.model_copy(update=update))
    marker = tmp_path / "no-metadata-execution"
    malicious = value.spec.model_copy(
        update={
            "task": f"'; __import__('pathlib').Path({str(marker)!r}).touch(); #",
        }
    )
    rebound = rebind_source(source, value, malicious)
    validate_source(rebound, value.model_copy(update={"spec": malicious}))
    assert not marker.exists()


async def test_rebind_keeps_control_flow_and_replaces_only_spec(tmp_path: Path) -> None:
    value = artifact(tmp_path, None)
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    source = source.replace(
        "    return await", "    if task == 'skip':\n        return 'cached'\n    return await"
    )
    new_spec = value.spec.model_copy(update={"task": "different task"})
    rebound = rebind_source(source, value, new_spec)
    assert "different task" in rebound
    assert any(isinstance(node, ast.If) for node in ast.walk(ast.parse(rebound)))
    new = value.model_copy(update={"spec": new_spec, "report": None})
    (tmp_path / "harness.py").write_text(rebound, encoding="utf-8")
    assert await sandbox_harness(new)(RunContext(FakeAdapter(), Budget()), "skip") == "cached"
    assert rebind_source(DIRECT, value, new_spec) == DIRECT


async def test_parent_run_and_compile_budgets_remain_authoritative(tmp_path: Path) -> None:
    value = artifact(
        tmp_path,
        """async def run(context, task):
    await context.call('first')
    return await context.call('second')
""",
    )
    responses = [
        ModelResponse(
            text="ok",
            tokens_in=3,
            tokens_out=2,
            cost_usd=0.01,
            model="fake",
        )
    ] * 2
    adapter = FakeAdapter(responses=responses)
    context = RunContext(adapter, Budget(cost_usd=0.015))
    with pytest.raises(BudgetExceeded):
        await sandbox_harness(value)(context, None)
    assert adapter.calls == 1
    assert context.tokens_in + context.tokens_out == 5
    adapter = FakeAdapter(responses=responses)
    context = RunContext(adapter, Budget())
    with compilation_wallet(Budget(cost_usd=0.015)) as wallet:
        with pytest.raises(BudgetExceeded):
            await sandbox_harness(value)(context, None)
    assert wallet.tokens == 5
    assert adapter.calls == 1


class WaitingAdapter:
    model = "fake"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = False

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.01)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancellation_and_deadline_reap_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    value = artifact(tmp_path)
    spawned: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec
    observed: list[dict[str, Any]] = []

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        observed.append(kwargs)
        process = await original(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setenv("SECRET_TEST_TOKEN", "do-not-share")
    adapter = WaitingAdapter()
    context = RunContext(adapter, Budget(compute_s=10 if cancel else 1.5))
    pending = asyncio.create_task(sandbox_harness(value)(context, None))
    if cancel:
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(BudgetExceeded):
            await pending
    assert spawned and all(process.returncode is not None for process in spawned)
    assert adapter.cancelled
    assert context.tokens_in + context.tokens_out == 2
    assert "SECRET_TEST_TOKEN" not in observed[0]["env"]
    assert "ARCHITECT_SIGNING_KEY_FILE" not in observed[0]["env"]
    assert "HOME" not in observed[0]["env"]
    assert not await asyncio.to_thread(Path(observed[0]["cwd"]).exists)


async def test_oversize_inputs_and_results_fail_closed(tmp_path: Path) -> None:
    value = artifact(tmp_path, "async def run(context, task):\n    return task\n")
    with pytest.raises(SandboxError):
        await sandbox_harness(value)(RunContext(FakeAdapter(), Budget()), "x" * (128 * 1024))
    source = "async def run(context, task):\n    x = task + task\n    return x + x\n"
    value = artifact(tmp_path, source)
    with pytest.raises(SandboxError):
        await sandbox_harness(value)(RunContext(FakeAdapter(), Budget()), "x" * 40000)


async def test_worker_failures_do_not_pass_stdout_or_tracebacks(tmp_path: Path) -> None:
    value = artifact(tmp_path, "async def run(context, task):\n    return task['private-key']\n")
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        await sandbox_harness(value)(RunContext(FakeAdapter(), Budget()), {})


async def test_cancellation_during_spawn_still_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = artifact(tmp_path)
    created = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    pending = asyncio.create_task(sandbox_harness(value)(RunContext(FakeAdapter(), Budget()), None))
    await asyncio.wait_for(created.wait(), timeout=5)
    pending.cancel()
    asyncio.get_running_loop().call_soon(pending.cancel)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert processes and all(process.returncode is not None for process in processes)


# Trusted test fixtures exercise the interpreter itself for line/branch evidence.
# No authored source is compiled or executed, and subprocess tests above stay intact.
@pytest.mark.parametrize(
    ("expression", "task", "expected"),
    [
        ("None", None, None),
        ("task", {"a": "data"}, {"a": "data"}),
        ("[1, -2, not False]", None, [1, -2, True]),
        ("{'x': task['key']}", {"key": "data"}, {"x": "data"}),
        ("task[1]", [1, 2], 2),
        ("task[-1]", "abc", "c"),
        ("'a' + 'b'", None, "ab"),
        ("[1] + [2]", None, [1, 2]),
        ("True and 'yes'", None, "yes"),
        ("False and task['missing']", {}, False),
        ("False or 'fallback'", None, "fallback"),
        ("'first' or task['missing']", {}, "first"),
        ("1 == 1 != 2", None, True),
        ("1 == 2", None, False),
        ("'a' in 'cat'", None, True),
        ("'a' in {'a': 1}", None, True),
        ("1 in [1, 2]", None, True),
        ("1 not in [2]", None, True),
        ("1 not in [1]", None, False),
        ("len('abc')", None, 3),
        ("len([])", None, 0),
        ("len({'a': 1})", None, 1),
        ("str('data')", None, "data"),
        ("str({'b': 2, 'a': 1})", None, '{"a": 1, "b": 2}'),
        ("-1.5", None, -1.5),
    ],
)
async def test_pure_worker_json_expressions(
    tmp_path: Path,
    expression: str,
    task: JsonValue,
    expected: JsonValue,
) -> None:
    value = artifact(tmp_path)
    source = f"async def run(context, task):\n    return {expression}\n"
    tree = _validated_tree(source, value)
    interpreter = worker._Interpreter(task)
    with pytest.raises(worker._Return) as returned:
        await interpreter.block(tree.body)
    assert returned.value.value == expected


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        ("task[0]", "sandbox_index"),
        ("-'bad'", "sandbox_unary"),
        ("1 + 2", "sandbox_add"),
        ("1 in 2", "sandbox_comparison"),
        ("len(1)", "sandbox_length"),
        ("await context.call(1)", "sandbox_invalid_call"),
        ("await context.call('x', True)", "sandbox_invalid_call"),
        ("await context.call('x', 0)", "sandbox_invalid_call"),
    ],
)
async def test_pure_worker_rejects_invalid_runtime_json_operations(
    tmp_path: Path,
    expression: str,
    error: str,
) -> None:
    value = artifact(tmp_path)
    tree = _validated_tree(f"async def run(context, task):\n    return {expression}", value)
    with pytest.raises(SandboxError, match=f"^{error}$"):
        await worker._Interpreter(None).block(tree.body)


async def test_pure_worker_defensive_unknown_syntax_fails() -> None:
    interpreter = worker._Interpreter(None)
    with pytest.raises(SandboxError, match="^sandbox_statement$"):
        await interpreter.block([ast.Pass()])
    with pytest.raises(SandboxError, match="^sandbox_expression$"):
        await interpreter.expr(ast.Set(elts=[]))


@pytest.mark.parametrize("task", [True, False])
async def test_pure_worker_straight_line_branching_and_bare_return(
    tmp_path: Path,
    task: bool,
) -> None:
    value = artifact(tmp_path)
    source = """async def run(context, task):
    x = 'initial'
    if task:
        x = 'true'
    else:
        x = 'false'
    return
"""
    interpreter = worker._Interpreter(task)
    with pytest.raises(worker._Return) as returned:
        await interpreter.block(_validated_tree(source, value).body)
    assert returned.value.value is None
    assert interpreter.locals["x"] == ("true" if task else "false")


@pytest.mark.parametrize(
    "source",
    [
        "async def run(context, task):\n    return await context.call('x')",
        "async def run(context, task):\n    return await context.call('x', 64)",
        "async def run(context, task):\n    return await context.call('x', max_output_tokens=64)",
        "async def run(context, task):\n    await context.call('x')\n    return 'ok'",
        None,
    ],
)
async def test_pure_worker_model_capabilities_use_only_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str | None,
) -> None:
    value = artifact(tmp_path, source)
    sent: list[object] = []
    monkeypatch.setattr(worker, "_send", sent.append)
    monkeypatch.setattr(worker, "_receive", lambda: {"value": "ok"})
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    with pytest.raises(worker._Return) as returned:
        await worker._Interpreter({}).block(_validated_tree(source, value).body)
    assert returned.value.value == "ok"
    assert len(sent) == 1
    request = cast(dict[str, JsonValue], sent[0])
    assert request["op"] == "call"
    assert request["tokens"] in (64, None)
    assert isinstance(request["prompt"], str)


@pytest.mark.parametrize("response", [None, [], {}, {"value": 1}, {"value": "ok", "extra": 1}])
async def test_pure_worker_rpc_rejects_invalid_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: JsonValue,
) -> None:
    monkeypatch.setattr(worker, "_send", lambda message: None)
    monkeypatch.setattr(worker, "_receive", lambda: response)
    with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
        await worker._RPC().call("x")


@pytest.mark.parametrize(("prompt", "tokens"), [(1, 64), ("x", True), ("x", 0)])
async def test_pure_worker_rpc_rejects_invalid_requests(prompt: object, tokens: object) -> None:
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await worker._RPC().call(cast(str, prompt), cast(int, tokens))


def test_pure_worker_protocol_streams_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    incoming = io.BytesIO(b'{"value": "answer"}\n')
    outgoing = io.BytesIO()
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(buffer=incoming),
            stdout=SimpleNamespace(buffer=outgoing),
        ),
    )
    worker._send({"op": "call", "prompt": "x", "tokens": 64})
    assert _decode(outgoing.getvalue()) == {"op": "call", "prompt": "x", "tokens": 64}
    assert worker._receive() == {"value": "answer"}
    with pytest.raises(SandboxError):
        worker._receive()


@pytest.mark.parametrize(
    "source",
    [
        "async def run(context, task):\n    return task",
        "async def run(context, task):\n    x = task",
    ],
)
async def test_pure_worker_main_returns_json_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    value = artifact(tmp_path, source)
    binding = value.model_dump(
        mode="json",
        include={"spec", "preset", "params", "components", "model"},
    )
    sent: list[object] = []
    monkeypatch.setattr(
        worker,
        "_receive",
        lambda: {
            "source": source,
            "binding": binding,
            "task": "only-input",
        },
    )
    monkeypatch.setattr(worker, "_send", sent.append)
    await worker._main()
    assert sent == [{"op": "result", "value": "only-input" if "return" in source else None}]


@pytest.mark.parametrize(
    "initial",
    [
        None,
        [],
        {},
        {"source": 1, "binding": {}, "task": None},
        {"source": "code", "binding": [], "task": None},
        {"source": "code", "binding": {}, "task": None, "authority": "forged"},
    ],
)
async def test_pure_worker_main_rejects_invalid_protocol(
    monkeypatch: pytest.MonkeyPatch,
    initial: JsonValue,
) -> None:
    monkeypatch.setattr(worker, "_receive", lambda: initial)
    with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
        await worker._main()


@pytest.mark.parametrize("data", [b"", b"{}", b"NaN\n", b"not-json\n", b"\xff\n"])
def test_parent_protocol_decode_rejects_malformed_json(data: bytes) -> None:
    with pytest.raises(SandboxError):
        _decode(data)


def test_parent_protocol_rejects_non_json_and_deep_values() -> None:
    for value in [{1: "non-string-key"}, float("inf"), object()]:
        with pytest.raises(SandboxError):
            _encode(value)
    with pytest.raises(SandboxError, match="^sandbox_value_limit$"):
        _json(None, depth=41)
    with pytest.raises(SandboxError):
        _decode(b"x" * (128 * 1024 + 1) + b"\n")


@pytest.mark.parametrize(
    "source",
    [
        "async def run(context, task):\n    return 1 < 2",
        "async def run(context, task):\n    return await execute_preset()",
        "async def run(context, task):\n    return await task()",
        "async def run(context, task):\n    return await execute_preset("
        "str(task), {}, (), {}, context, task)",
        "async def run(context, task):\n    return await execute_preset("
        "'synthesizer', {}, (), {}, task, task)",
        "async def run(context, task):\n    return await context.call('x', 1, max_output_tokens=2)",
        "async def run(context, task):\n    return [",
        "\ud800",
    ],
)
def test_additional_source_gate_rejections(tmp_path: Path, source: str) -> None:
    value = artifact(tmp_path)
    with pytest.raises(SandboxError):
        validate_source(source, value)


@pytest.mark.parametrize("raw", [None, b"\xff", b"#" * 65537], ids=["absent", "utf8", "limit"])
def test_sandbox_loader_rejects_missing_invalid_or_oversize_source(
    tmp_path: Path,
    raw: bytes | None,
) -> None:
    value = artifact(tmp_path)
    path = tmp_path / "harness.py"
    if raw is None:
        path.unlink()
    else:
        path.write_bytes(raw)
    with pytest.raises(SandboxError):
        sandbox_harness(value)


class _ProcessStub:
    def __init__(self, *, exited: bool = False, missing: bool = False) -> None:
        self.returncode: int | None = 0 if exited else None
        self.missing = missing
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        if self.missing:
            raise ProcessLookupError

    async def wait(self) -> int:
        self.waited = True
        self.returncode = 0
        return 0


@pytest.mark.parametrize(("exited", "missing"), [(True, False), (False, True)])
async def test_reap_handles_already_exited_or_disappearing_worker(
    exited: bool,
    missing: bool,
) -> None:
    process = _ProcessStub(exited=exited, missing=missing)
    await _reap(cast(asyncio.subprocess.Process, process))
    assert process.waited
    assert process.killed != exited


async def test_reap_defers_cancellation_until_child_is_reaped() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowExit(_ProcessStub):
        async def wait(self) -> int:
            entered.set()
            await release.wait()
            return await super().wait()

    process = SlowExit()
    pending = asyncio.create_task(_reap(cast(asyncio.subprocess.Process, process)))
    await entered.wait()
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.killed and process.waited


@pytest.mark.parametrize(
    "message",
    [
        [],
        {"op": "call", "prompt": 1, "tokens": 64},
        {"op": "call", "prompt": "x", "tokens": True},
        {"op": "call", "prompt": "x", "tokens": 0},
        {"op": "call", "prompt": "x", "tokens": 64},
        {"op": "result", "value": "no-output-on-protocol-error"},
    ],
)
async def test_parent_rejects_untrusted_protocol_calls_and_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: JsonValue,
) -> None:
    value = artifact(tmp_path)
    adapter = FakeAdapter()
    context = RunContext(adapter, Budget(tokens=100000, cost_usd=100))

    class Input:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    class Output:
        async def readline(self) -> bytes:
            if isinstance(message, dict) and message.get("op") == "result":
                raise OSError("private-protocol-detail")
            return _encode(message)

    class Process(_ProcessStub):
        stdin = Input()
        stdout = Output()

    process = Process()

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.delenv("SystemRoot", raising=False)
    with pytest.raises(SandboxError) as raised:
        await sandbox_harness(value)(context, None)
    assert "private-protocol-detail" not in str(raised.value)
    assert adapter.calls <= 64
    if message == {"op": "call", "prompt": "x", "tokens": 64}:
        assert adapter.calls == 64
    assert process.killed and process.waited


def test_parser_resource_exhaustion_is_a_closed_source_rejection(tmp_path: Path) -> None:
    value = artifact(tmp_path)
    # CPython 3.12 can raise MemoryError before an AST exists; source-byte limits
    # alone cannot prevent this small, deeply recursive unary-expression attack.
    source = "async def run(context, task):\n    return " + "-" * 7000 + "1"
    with pytest.raises(SandboxError):
        validate_source(source, value)
    with pytest.raises(SandboxError):
        rebind_source(source, value, value.spec)
    (tmp_path / "harness.py").write_text(source, encoding="utf-8")
    with pytest.raises(SandboxError):
        sandbox_harness(value)


@pytest.mark.parametrize("mode", ["coder", "pipeline"])
async def test_sandbox_forwards_only_bound_trusted_composed_preset(
    tmp_path: Path, mode: str,
) -> None:
    import json

    from architect.runtime import Runtime
    from architect.spec import PipelineStep
    from architect.telemetry import TelemetryStore

    directory, repo = tmp_path / "artifact", tmp_path / "repo"
    await asyncio.to_thread(directory.mkdir)
    await asyncio.to_thread(repo.mkdir)
    spec = Spec(
        task="Exercise a bound trusted helper", input_schema={"type": "object"},
        output_schema={"type": "string"}, budget=Budget(),
        floor=FloorDefinition(kind="held_out_tasks", limit=1, dataset="fixture"),
        tools=("write",) if mode == "coder" else (),
        pipeline=() if mode == "coder" else (
            PipelineStep(name="draft", preset="synthesizer", model="fake", budget=Budget(),
                         output_schema={"type": "string"}),
            PipelineStep(name="check", preset="verifier", model="llama3.2", budget=Budget()),
        ),
    )
    preset = get_preset("coder" if mode == "coder" else "synthesizer")
    params = preset.validate_params({})
    await asyncio.to_thread(preset.emit, directory, spec, params, preset.components)
    candidate = HarnessArtifact(
        spec=spec, directory=directory, preset=preset.name, params=params,
        components=preset.components, model="fake", execution_mode="sandbox",
    )
    texts = ["done"]
    if mode == "coder":
        texts.insert(0, json.dumps({"tool_calls": [{"id": "write", "name": "write",
            "arguments": {"path": "result.txt", "content": "actual Pi tool"}}]}))
    adapters = {
        "fake": FakeAdapter(responses=[ModelResponse(text=text, tokens_in=5, tokens_out=5,
                                                     cost_usd=0, model="fake") for text in texts]),
        "llama3.2": FakeAdapter(model="llama3.2", responses=[ModelResponse(
            text='{"passed":true,"reason":"checked"}', tokens_in=5, tokens_out=5,
            cost_usd=0, model="llama3.2",
        )]),
    }
    with TelemetryStore(tmp_path / "telemetry") as store:
        runtime = Runtime(store, adapter_factory=adapters.__getitem__)
        result = await runtime.evaluate(spec, sandbox_harness(candidate), {"repo": str(repo)},
                                        adapters["fake"])
        assert result.output == "done" and result.tokens_in == 10
    if mode == "coder":
        assert await asyncio.to_thread((repo / "result.txt").read_text) == "actual Pi tool"
    else:
        assert adapters["llama3.2"].calls == 1


# --- Phase 8: pages stay in the host; only sha256 names cross the pipe -----------------

import base64  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402

from architect._authority import _sign_report  # noqa: E402
from architect.models.images import ImageInput, encode_png_gray  # noqa: E402
from architect.runtime import sandbox as sandbox_module  # noqa: E402
from architect.runtime.sandbox import MESSAGE_LIMIT  # noqa: E402
from architect.spec import TestReport  # noqa: E402

PAGE_INPUT: dict[str, JsonValue] = {
    "type": "object",
    "required": ["images"],
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["$image"],
                "properties": {"$image": {"type": "object", "required": ["media_type"]}},
            },
        }
    },
}


def noisy_page(seed: int = 7, width: int = 400, height: int = 400) -> bytes:
    """Incompressible greyscale noise: about 160 KB of PNG, over 200 KB of base64."""
    rng = random.Random(seed)
    return encode_png_gray([rng.randbytes(width) for _ in range(height)], width, height)


def page_task(data: bytes) -> dict[str, JsonValue]:
    encoded = base64.b64encode(data).decode("ascii")
    return {"images": [{"$image": {"media_type": "image/png", "base64": encoded}}]}


class PageAdapter(FakeAdapter):
    def __init__(self, texts: list[str]) -> None:
        super().__init__(responses=[
            ModelResponse(text=text, tokens_in=10, tokens_out=5, cost_usd=0.0, model="fake")
            for text in texts
        ])
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def licensed_extractor(directory: Path, mode: str) -> HarnessArtifact:
    spec = Spec(
        task="Extract the printed page number",
        input_schema=PAGE_INPUT,
        output_schema={
            "type": "object",
            "required": ["page"],
            "properties": {"page": {"type": "integer"}},
        },
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="pages"),
        budget=Budget(),
    )
    preset = get_preset("extractor")
    params = preset.validate_params({"verifier_depth": 1})
    preset.emit(directory, spec, params, preset.components)
    value = HarnessArtifact(
        spec=spec, directory=directory, preset=preset.name, params=params,
        components=preset.components, model="fake",
        execution_mode=cast(Any, mode),
    )
    value.save()
    report = TestReport(
        spec_hash=spec.spec_hash, artifact_digest=value.artifact_digest, passed=True,
        floor_score=1.0, floor_limit=1.0, floor_kind=spec.floor.kind, tokens=1,
        cost_usd=0.0, compute_s=0.01, component_count=value.component_count,
        verification_id="fixture-verifier",
    )
    signed = report.model_copy(update={
        "signature": _sign_report(report.model_dump(mode="json", exclude={"signature"}))
    })
    value = value.model_copy(update={"report": signed})
    value.save()
    assert value.licensed
    return value


@pytest.mark.parametrize("mode", ["native", "sandbox"])
async def test_licensed_extractor_reads_a_page_with_no_base64_in_prompts_or_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from architect.runtime import Runtime
    from architect.telemetry import TelemetryStore

    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    directory = tmp_path / "artifact"
    await asyncio.to_thread(directory.mkdir)
    value = licensed_extractor(directory, mode)
    data = noisy_page()
    encoded = base64.b64encode(data).decode("ascii")
    task = page_task(data)
    assert len(encoded) > 200_000
    with pytest.raises(SandboxError, match="^sandbox_message_limit$"):
        _encode({"task": task})  # the literal page could never cross the pipe itself

    pipe: list[bytes] = []
    encode, decode = sandbox_module._encode, sandbox_module._decode

    def spy_encode(message: object) -> bytes:
        data = encode(message)
        pipe.append(data)
        return data

    def spy_decode(data: bytes) -> JsonValue:
        pipe.append(data)
        return decode(data)

    monkeypatch.setattr(sandbox_module, "_encode", spy_encode)
    monkeypatch.setattr(sandbox_module, "_decode", spy_decode)
    adapter = PageAdapter(['{"page": 4}', '{"passed": true, "reason": "matches"}'])
    with TelemetryStore(tmp_path / "telemetry") as store:
        result = await Runtime(store).run(value, task, adapter)
        row = store.rows()[0]
    assert result.output == {"page": 4}
    sha = ImageInput.from_bytes(data).sha256
    assert [shape.sha256s for shape in adapter.seen] == [(sha,), (sha,)]
    assert [shape.image_bytes for shape in adapter.seen] == [len(data), len(data)]
    for request in adapter.requests:
        assert encoded[:64] not in request.prompt and "base64" not in request.prompt
        assert len(request.prompt) < 4096
    assert row.image_inputs == 2 and row.accepted
    if mode == "sandbox":
        calls = [json.loads(message) for message in pipe if b'"op": "call"' in message]
        assert [message["images"] for message in calls] == [[sha], [sha]]
        assert all(len(message) < MESSAGE_LIMIT // 4 for message in pipe)
    else:
        assert pipe == []
    for message in pipe:
        assert encoded[:64].encode() not in message and b"base64" not in message


def recorded_process(messages: list[JsonValue]) -> tuple[Any, list[JsonValue]]:
    written: list[JsonValue] = []

    class Input:
        def write(self, data: bytes) -> None:
            written.append(_decode(data))

        async def drain(self) -> None:
            pass

    class Output:
        async def readline(self) -> bytes:
            return _encode(messages.pop(0))

    class Process(_ProcessStub):
        stdin = Input()
        stdout = Output()

    return Process(), written


PAGE = ImageInput.from_bytes(noisy_page(width=16, height=16))


async def test_parent_resolves_sha_names_only_from_the_runs_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = artifact(tmp_path)
    adapter = PageAdapter(["seen"])
    context = RunContext(adapter, Budget(cost_usd=100))
    process, written = recorded_process([
        {"op": "call", "prompt": "x", "tokens": None, "images": [PAGE.sha256]},
        {"op": "result", "value": "done"},
    ])

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    # The host lifts the task literal and attaches it before the initial message.
    assert await sandbox_harness(value)(context, page_task(PAGE.data)) == "done"
    initial = written[0]
    assert isinstance(initial, dict)
    assert initial["task"] == {"images": [{"$image": PAGE.stub(1).model_dump(mode="json")}]}
    assert written[1] == {"value": "seen"}
    assert [image.sha256 for image in adapter.requests[0].images] == [PAGE.sha256]
    assert context.images_sent == 1


@pytest.mark.parametrize(
    "images",
    [
        [],
        "0" * 64,
        [PAGE.sha256.upper()],
        ["0" * 64],
        [PAGE.sha256[:63]],
        [7],
        [PAGE.sha256] * 101,
    ],
)
async def test_parent_rejects_invalid_or_unattached_image_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, images: JsonValue,
) -> None:
    value = artifact(tmp_path)
    adapter = PageAdapter(["seen"])
    context = RunContext(adapter, Budget(cost_usd=100))
    # The page is attached to the run, so only the malformed names are at fault.
    context.attach_images([PAGE])
    process, _ = recorded_process([
        {"op": "call", "prompt": "x", "tokens": None, "images": images},
    ])

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await sandbox_harness(value)(context, None)
    assert adapter.calls == 0
    assert process.killed and process.waited


async def test_parent_does_not_resolve_images_attached_to_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = artifact(tmp_path)
    other = RunContext(FakeAdapter(), Budget())
    other.attach_images([PAGE])
    adapter = PageAdapter(["seen"])
    process, _ = recorded_process([
        {"op": "call", "prompt": "x", "tokens": None, "images": [PAGE.sha256]},
    ])

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await sandbox_harness(value)(RunContext(adapter, Budget(cost_usd=100)), None)
    assert adapter.calls == 0


async def test_worker_rpc_sends_sha_names_and_keeps_text_calls_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []
    monkeypatch.setattr(worker, "_send", sent.append)
    monkeypatch.setattr(worker, "_receive", lambda: {"value": "ok"})
    rpc = worker._RPC()
    assert await rpc.call("x") == "ok"
    assert await rpc.call("x", 64, images=[PAGE.stub(1), PAGE.stub(2)]) == "ok"
    assert sent == [
        {"op": "call", "prompt": "x", "tokens": None},
        {"op": "call", "prompt": "x", "tokens": 64, "images": [PAGE.sha256, PAGE.sha256]},
    ]
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await rpc.call("x", images=[PAGE])  # bytes inside the worker are a violation
    assert len(sent) == 2


async def test_worker_preset_execution_sends_only_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    value = licensed_extractor(tmp_path, "sandbox")
    source = (tmp_path / "harness.py").read_text(encoding="utf-8")
    sent: list[object] = []
    replies = iter([{"value": '{"page": 2}'}, {"value": '{"passed": true, "reason": "ok"}'}])
    monkeypatch.setattr(worker, "_send", sent.append)
    monkeypatch.setattr(worker, "_receive", lambda: next(replies))
    stubbed = {"images": [{"$image": PAGE.stub(1).model_dump(mode="json")}]}
    with pytest.raises(worker._Return) as returned:
        await worker._Interpreter(stubbed).block(_validated_tree(source, value).body)
    assert returned.value.value == {"page": 2}
    assert [cast(dict[str, JsonValue], message)["images"] for message in sent] == [
        [PAGE.sha256], [PAGE.sha256],
    ]
    # A literal page smuggled into the worker cannot be sent as bytes.
    sent.clear()
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await worker._Interpreter(page_task(PAGE.data)).block(_validated_tree(source, value).body)
    assert sent == []


@pytest.mark.parametrize(
    "source",
    [
        "async def run(context, task):\n    return await context.call(task, images=[])\n",
        "async def run(context, task):\n    return await context.call(task, 64, images=task)\n",
        "async def run(context, task):\n"
        "    return await context.call(task, max_output_tokens=64, images=task)\n",
    ],
)
def test_ast_guard_still_rejects_images_in_harness_source(tmp_path: Path, source: str) -> None:
    with pytest.raises(SandboxError, match="^sandbox_call$"):
        validate_source(source, artifact(tmp_path))


def test_validate_source_returns_frozen_language_1_statistics(tmp_path: Path) -> None:
    from architect.runtime.sandbox import SourceStats, call_limit

    value = artifact(tmp_path)
    stats = validate_source(DIRECT, value)
    assert isinstance(stats, SourceStats)
    assert (stats.language, stats.helpers, stats.constants, stats.loops) == (1, 0, 0, 0)
    assert stats.await_sites == 1 and stats.bytes == len(DIRECT.encode("utf-8"))
    assert call_limit(value) == 64


async def test_language_1_initial_message_names_its_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = artifact(tmp_path, "async def run(context, task):\n    return task\n")
    process, written = recorded_process([{"op": "result", "value": "done"}])

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await sandbox_harness(value)(RunContext(FakeAdapter(), Budget()), "x") == "done"
    initial = written[0]
    assert isinstance(initial, dict) and initial["language"] == 1
    assert isinstance(initial["binding"], dict) and "forge" not in initial["binding"]
    assert "execution_mode" not in initial["binding"]


def _replying_artifact(tmp_path: Path, mode: str) -> HarnessArtifact:
    """``artifact`` with the spec's ``reply_json`` set to ``mode``."""
    value = artifact(tmp_path)
    spec = Spec.model_validate({**value.spec.model_dump(mode="json"), "reply_json": mode})
    return value.model_copy(update={"spec": spec})


@pytest.mark.parametrize(
    ("mode", "schema", "code"),
    [
        ("strict", {"type": "object"}, "sandbox_worker_failed"),
        ("first", {"type": "object"}, "sandbox_worker_failed"),
        ("schema", "object", "sandbox_invalid_call"),
        ("schema", ["object"], "sandbox_invalid_call"),
    ],
)
async def test_a_call_carries_a_reply_schema_only_under_the_schema_reply_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, schema: JsonValue, code: str,
) -> None:
    """Phase 14 review: a reply schema crosses the pipe only for a spec whose
    ``reply_json`` is "schema", and only as a JSON object; anything else is refused
    before any model call."""
    value = _replying_artifact(tmp_path, mode)
    adapter = FakeAdapter()
    process, _ = recorded_process(
        [{"op": "call", "prompt": "x", "tokens": None, "schema": schema}]
    )

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SandboxError, match=f"^{code}$"):
        await sandbox_harness(value)(RunContext(adapter, Budget()), "x")
    assert adapter.calls == 0 and process.killed and process.waited


async def test_a_schema_mode_call_reaches_the_adapter_as_its_response_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _replying_artifact(tmp_path, "schema")
    adapter = FakeAdapter()
    schema: dict[str, JsonValue] = {"type": "object", "required": ["total"]}
    process, written = recorded_process([
        {"op": "call", "prompt": "x", "tokens": None, "schema": schema},
        {"op": "call", "prompt": "y", "tokens": None},
        {"op": "result", "value": "done"},
    ])

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await sandbox_harness(value)(RunContext(adapter, Budget()), "x") == "done"
    constrained = ModelRequest(prompt="x", response_schema=schema).response_schema_sha256()
    assert [shape.response_schema_sha256 for shape in adapter.seen] == [constrained, None]
    assert written[1:] == [{"value": "ok"}, {"value": "ok"}]
