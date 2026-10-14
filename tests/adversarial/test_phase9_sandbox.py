"""Phase 9 adversarial round for sandbox language v2: escapes, DoS and forged protocol.

Language 2 widens the grammar (loops, helpers, mutation, builtins), so every
attack here must still end in a fixed code: rejected before execution, or
stopped by fuel, construction bounds, the OS memory cap or the host deadline.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue
from sandbox_v2_support import (
    TurnContext,
    binding,
    body,
    game_artifact,
    interpret,
    make_artifact,
    wall_limit,
)

from architect.models import FakeAdapter
from architect.runtime import BudgetExceeded, RunContext
from architect.runtime import _proc_limits as proc_limits
from architect.runtime import _sandbox_builtins as builtins
from architect.runtime.sandbox import SandboxError, _encode, sandbox_harness, validate_source
from architect.spec import Budget, HarnessArtifact

ESCAPES = [
    "import os\nasync def run(context, task):\n    return os.environ\n",
    "from pathlib import Path\nasync def run(context, task):\n    return 1\n",
    "from architect.floors._access import _VERIFIER_CAPABILITY\n" + body("return 1"),
    "from architect.runtime import RunContext as __builtins__\n" + body("return task"),
    body("return task.__class__.__mro__"),
    body("return ().__class__.__bases__[0].__subclasses__()"),
    body("return getattr(task, '__class__')"),
    body("return type(task)"),
    body("return vars()"),
    body("return dir()"),
    body("return globals()"),
    body("return locals()"),
    body("return __builtins__"),
    body("return __import__('os').environ"),
    body("return eval(task['payload'])"),
    body("exec(task['payload'])"),
    body("return compile('1', 'x', 'eval')"),
    body("return open('signing.key')"),
    body("return context.adapter"),
    body("return context._attached_image('0')"),
    body("return await context._call('x', None)"),
    body("return await context.call.__self__.adapter.complete(task)"),
    body("method = context.call", "return await method('x')"),
    body("return await context.call(**task)"),
    body("return [x.__class__ for x in task]"),
    body("return (lambda: task)()"),
    body("return execute_preset.__globals__"),
    body("def hidden():", "    return task", "return hidden()"),
    body("async def hidden():", "    return task", "return await hidden()"),
    "class Escape:\n    pass\n" + body("return 1"),
    "@__import__('os').system\ndef f(x):\n    return x\n" + body("return 1"),
    "def __init__(x):\n    return x\n" + body("return 1"),
    "def f(__class__):\n    return 1\n" + body("return 1"),
    "async def run(context, task=__import__('os')):\n    return task\n",
    body("return f'{task.__class__}'"),
    body("return f'{task!r}'"),
    body("return f'{task:{task}}'"),
    body("return '{0.__class__}'.format(task)"),
    body("return join('', [c.__class__ for c in task])"),
    body("x = task", "x.__class__ = 1"),
    body("return task[0].__class__"),
    body("return [*task]"),
    body("return (x := task)"),
    body("with open('x') as f:", "    return 1"),
    body("async with context as c:", "    return 1"),
    body("async for x in context:", "    return 1"),
    body("yield task"),
    body("global task"),
    body("raise SystemExit"),
    body("return await context.harness(task['name'], task)"),
    body("return await execute_preset('coder', {}, (), {}, context, task)"),
    body("return str.__dict__"),
    body("return len.__self__"),
    # Forge pack v2: method spellings, annotations, defaults, isinstance and constants.
    body("return task.__class__()"),
    body("return task.get.__self__"),
    body("return ''.join.__globals__"),
    body("return task.format(task)"),
    body("return task.mro()"),
    body("return context.call.__self__"),
    body("x = context.call('x')", "return x"),
    body("return context.adapter.complete(task)"),
    body("return (lambda: task).__code__"),
    "def f(x: __import__('os')) -> eval('1'):\n    return x\n" + body("return f(1)"),
    "async def run(context, task: __import__('os').environ):\n    return task\n",
    "async def run(context, task) -> exec(task['payload']):\n    return task\n",
    "async def f(context, x: await context.call('leak')):\n    return x\n"
    + body("return await f(context, 1)"),
    body("x: open('signing.key') = 1", "return x"),
    "C: __import__('os').system('id') = 1\n" + body("return C"),
    "def f(x=__import__('os')):\n    return x\n" + body("return f()"),
    "def f(x=open('signing.key')):\n    return x\n" + body("return f()"),
    "def f(x=task):\n    return x\n" + body("return f()"),
    "async def f(context=None, x=1):\n    return x\n" + body("return await f(context)"),
    body("return isinstance(task, __import__('os'))"),
    body("return isinstance(task, task.__class__)"),
    body("return isinstance(task, type)"),
    "NAME = 'coder'\nPARAMS = {}\nCOMPONENTS = ()\nSPEC = {}\n"
    + body("return await execute_preset(NAME, PARAMS, COMPONENTS, SPEC, context, task)"),
    "def isinstance(x):\n    return x\n" + body("return isinstance(1)"),
    "def lower(s):\n    return s\n" + body("return lower('A')"),
    # Pack v3: comprehensions are accepted, so each of their parts is checked like any
    # other expression, and their names follow the binding rules.
    body("return [__import__('os') for _ in [1]]"),
    body("return [x for x in task if x.__class__]"),
    body("return {k: v for k, v in task.__dict__}"),
    body("return [(lambda: x)() for x in task]"),
    body("return [(y := x) for x in task]"),
    body("return [x for x in task for task in x]"),
    body("return [context for _ in [1]]"),
    body("return [await context.adapter() for _ in [1]]"),
    body("return [await context.call.__self__ for _ in [1]]"),
    body("return any(x.__globals__ for x in task)"),
    body("return sum(type(x) for x in task)"),
    body("return [x for x in eval(task)]"),
    body("return {x.__class__ for x in task}"),
    body("return [x for x in task if open('signing.key')]"),
    body("g = (x for x in task)", "return g"),
    body("return next(x for x in task)"),
    body("return [x async for x in context]"),
    body("return [execute_preset for _ in [1]]"),
    "def f(x):\n    return [f(y) for y in x]\n" + body("return f(task)"),
    "def f(x):\n    return [await g(y) for y in x]\n" + body("return f(task)"),
    "import os\n" + body("return [os.system('x') for _ in [1]]"),
    'if __name__ == "__main__":\n    pass\nelse:\n    import os\n' + body("return 1"),
    body("return 1") + 'if __name__ != "__main__":\n    import os\n',
    body("return 1") + 'if __name__ == "__main__" or True:\n    import os\n',
    # Pack v4: typed and named excepts, three loop names and the string builtins add no
    # way to reach an object: types are names only, never evaluated, and e is a str.
    body("return task.isdigit.__self__"),
    body("return ''.isdigit.__globals__"),
    body("return chr.__self__"),
    body("return [].copy.__class__"),
    body("return task.copy(task)"),
    body("return zip(*task)"),
    body("return max(*task)"),
    body("try:", "    x = 1", "except Exception as e:", "    return e.__traceback__"),
    body("try:", "    x = 1", "except __import__('os').error:", "    pass"),
    body("try:", "    x = 1", "except (ValueError, eval('X')):", "    pass"),
    body("try:", "    x = 1", "except type(task):", "    pass"),
    body("try:", "    x = 1", "except ValueError as e.x:", "    pass"),
    body("try:", "    x = 1", "except ValueError as __builtins__:", "    pass"),
    body("for a, b, c in zip(task, task, task.__class__):", "    pass"),
    body("for a, b, c.x in task:", "    pass"),
    "def chr(x):\n    return x\n" + body("return chr(1)"),
]


@pytest.mark.parametrize("source", ESCAPES)
def test_v2_rejects_capability_escapes_before_execution(tmp_path: Path, source: str) -> None:
    artifact = make_artifact(tmp_path / "a", forge=binding(children=("child",)))
    with pytest.raises(SandboxError):
        validate_source(source, artifact)


def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, task: JsonValue = None,
        *, fuel: int = 10_000_000) -> dict[str, Any]:
    artifact = make_artifact(tmp_path / "a", forge=binding(fuel=fuel))
    validate_source(source, artifact)
    return interpret(monkeypatch, artifact, source, task)[0]


def test_dictionary_lookups_never_reach_python_attributes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = body(
        "return [get(task, '__class__', 'none'), at([1], 99, 'none'), kind(task),"
        " get({'__dict__': 1}, '__dict__', 0)]"
    )
    frame = run(monkeypatch, tmp_path, source, {"a": 1})
    assert frame["value"] == ["none", "none", "dict", 1]


def test_deep_helper_chains_run_and_cycles_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chain = "".join(f"def h{i}(x):\n    return h{i + 1}(x + 1)\n" for i in range(31))
    chain += "def h31(x):\n    return x\n"
    frame = run(monkeypatch, tmp_path, chain + body("return h0(0)"))
    assert frame["value"] == 31
    cycle = chain.replace("def h31(x):\n    return x\n", "def h31(x):\n    return h0(x)\n")
    with pytest.raises(SandboxError, match="^sandbox_recursion$"):
        validate_source(cycle + body("return h0(0)"), make_artifact(tmp_path / "b"))
    nested = body("return " + "-(" * 30 + "1" + ")" * 30)
    assert run(monkeypatch, tmp_path, nested)["value"] == 1


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        # Unbounded loops end on fuel, never on wall clock alone.
        (("while True:", "    pass"), "sandbox_fuel"),
        (("n = 0", "while True:", "    n += 1"), "sandbox_fuel"),
        (("for i in range(100000):", "    for j in range(100000):", "        pass"),
         "sandbox_fuel"),
        # Aliasing cannot build a shared DAG: every store copies, so doubling costs fuel.
        (("a = [0]", "for i in range(40):", "    a = [a, a]"), "sandbox_fuel"),
        (("a = {'k': 0}", "for i in range(40):", "    a = {'l': a, 'r': a}"), "sandbox_fuel"),
        # Construction bounds.
        (("d = {}", "i = 0", "while True:", "    set_key(d, str(i), i)", "    i += 1"),
         "sandbox_value_limit"),
        (("l = [0]", "while True:", "    l = l + l"), "sandbox_value_limit"),
        (("l = []", "while True:", "    append(l, 0)"), "sandbox_value_limit"),
        (("s = 'x'", "while True:", "    s = s + s"), "sandbox_value_limit"),
        (("s = 'x'", "while len(s) < 100000:", "    s = s + s", "return f'{s}{s}'"),
         "sandbox_value_limit"),
        (("s = 'a'", "while len(s) < 10000:", "    s = s + s", "return replace(s, 'a', s)"),
         "sandbox_value_limit"),
        (("s = ','", "while len(s) < 100000:", "    s = s + s", "return split(s, ',')"),
         "sandbox_value_limit"),
        (("s = 'x'", "while len(s) < 1000:", "    s = s + s", "l = []",
          "for i in range(200):", "    append(l, s)", "return join(s, l)"),
         "sandbox_value_limit"),
        (("return range(1000000000)",), "sandbox_value_limit"),
        (("return list(range(100000))",), "sandbox_value_limit"),
        (("s = 'x'", "while len(s) < 100000:", "    s = s + s", "return list(s)"),
         "sandbox_value_limit"),
        # Deep nesting built through aliases is refused when copied, rendered or returned.
        (("root = []", "cur = root", "for i in range(100):", "    append(cur, [])",
          "    cur = cur[0]", "return root"), "sandbox_value_limit"),
        (("root = []", "cur = root", "for i in range(100):", "    append(cur, [])",
          "    cur = cur[0]", "return str(root)"), "sandbox_value_limit"),
        (("root = []", "cur = root", "for i in range(100):", "    append(cur, [])",
          "    cur = cur[0]", "return [root]"), "sandbox_value_limit"),
        (("root = {}", "cur = root", "for i in range(100):", "    cur['k'] = {}",
          "    cur = cur['k']", "return digest(root)"), "sandbox_value_limit"),
        # An oversized value cannot cross the RPC boundary.
        (("s = 'x'", "while len(s) < 100000:", "    s = s + s",
          "return await context.call(s + s)"), "sandbox_value_limit"),
        (("s = 'x'", "while len(s) < 70000:", "    s = s + s",
          "return [s, s]"), "sandbox_value_limit"),
    ],
)
def test_resource_attacks_end_in_fixed_fatal_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: tuple[str, ...], code: str
) -> None:
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, body(*lines))
    assert frame["op"] == "error" and frame["code"] == code, frame
    assert time.perf_counter() - started < wall_limit(30)


def test_fatal_codes_are_never_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for inner in (
        ("while True:", "    pass"),
        ("l = []", "while True:", "    append(l, 0)"),
        ("x = await context.call(1)",),
        ("x = y",),
    ):
        source = body("try:", *["    " + line for line in inner], "except:", "    return 'caught'",
                      "if task:", "    y = 1")
        frame = run(monkeypatch, tmp_path, source, None, fuel=200_000)
        assert frame["op"] == "error" and frame["code"] != "caught"


async def test_a_100m_fuel_run_is_killed_by_the_host_deadline(tmp_path: Path) -> None:
    artifact = make_artifact(
        tmp_path / "a", body("while True:", "    pass"), forge=binding(fuel=100_000_000)
    )
    started = time.perf_counter()
    with pytest.raises(BudgetExceeded):
        await sandbox_harness(artifact)(RunContext(FakeAdapter(), Budget(compute_s=2)), None)
    assert time.perf_counter() - started < wall_limit(15)


async def test_a_10m_fuel_run_finishes_in_under_60_seconds(tmp_path: Path) -> None:
    source = body("n = 0", "d = {}", "while True:", "    n += 1", "    set_key(d, 'k', n % 7)")
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=10_000_000))
    context = RunContext(FakeAdapter(), Budget(compute_s=120))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$"):
        await sandbox_harness(artifact)(context, None)
    assert time.perf_counter() - started < wall_limit(60)
    assert context.fuel_used == 10_000_000


# ---------------------------------------------------------------- forged worker protocol


class _Process:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.written: list[bytes] = []
        self.returncode: int | None = None
        self.killed = False
        outer = self

        class Input:
            def write(self, data: bytes) -> None:
                outer.written.append(data)

            async def drain(self) -> None:
                return None

        class Output:
            async def readline(self) -> bytes:
                return outer.messages.pop(0) if outer.messages else b""

        self.stdin = Input()
        self.stdout = Output()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = 0
        return 0


async def forged(
    monkeypatch: pytest.MonkeyPatch, artifact: HarnessArtifact, messages: list[JsonValue],
    context: RunContext | None = None,
) -> tuple[JsonValue, _Process]:
    process = _Process([_encode(message) for message in messages])

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    value = await sandbox_harness(artifact)(context or TurnContext(), None)
    return value, process


@pytest.mark.parametrize(
    ("messages", "code"),
    [
        ([{"op": "error", "code": "sandbox_key", "line": 2, "text": "leak"}],
         "sandbox_worker_failed"),
        ([{"op": "error", "code": "not_a_code", "line": 2}], "sandbox_worker_failed"),
        ([{"op": "error", "code": "sandbox_recursion", "line": 2}], "sandbox_worker_failed"),
        ([{"op": "error", "code": "sandbox_key", "line": 0}], "sandbox_worker_failed"),
        ([{"op": "error", "code": "sandbox_key", "line": True}], "sandbox_worker_failed"),
        ([{"op": "error"}], "sandbox_worker_failed"),
        ([{"op": "result", "value": 1, "fuel": 10_000_001}], "sandbox_invalid_message"),
        ([{"op": "result", "value": 1, "fuel": -1}], "sandbox_invalid_message"),
        ([{"op": "observe"}], "sandbox_invalid_call"),
        ([{"op": "step", "action": {"name": "ACTION1"}}], "sandbox_invalid_call"),
        ([{"op": "harness", "name": "other", "input": 1}], "sandbox_invalid_call"),
        ([{"op": "call", "prompt": "x", "tokens": None, "system": 1}], "sandbox_invalid_call"),
        ([{"op": "call", "prompt": "x", "tokens": None, "system": "s" * 65537}],
         "sandbox_invalid_call"),
        ([{"op": "call", "prompt": "x", "tokens": None, "extra": 1}], "sandbox_worker_failed"),
        ([{"op": "shell", "command": "x"}], "sandbox_worker_failed"),
    ],
)
async def test_host_rejects_forged_v2_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, messages: list[JsonValue], code: str
) -> None:
    artifact = make_artifact(tmp_path / "a", body("return 1"), forge=binding(children=("child",)))
    with pytest.raises(SandboxError, match=f"^{code}$"):
        await forged(monkeypatch, artifact, messages)


@pytest.mark.parametrize(
    "message",
    [
        {"op": "observe"},
        {"op": "harness", "name": "child", "input": 1},
        {"op": "call", "prompt": "x", "tokens": None, "system": "s"},
        {"op": "error", "code": "sandbox_key", "line": 2},
        {"op": "result", "value": 1, "fuel": 1},
    ],
)
async def test_a_v1_host_never_honours_v2_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, message: JsonValue
) -> None:
    artifact = make_artifact(tmp_path / "a", body("return 1"), v1=True)
    context = TurnContext()
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        await forged(monkeypatch, artifact, [message], context)
    assert context.observes == 0 and context.children == []


async def test_valid_v2_frames_are_honoured(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    artifact = make_artifact(tmp_path / "a", body("return 1"), forge=binding(children=("child",)))
    context = TurnContext()
    value, process = await forged(
        monkeypatch, artifact,
        [{"op": "harness", "name": "child", "input": 1}, {"op": "result", "value": 7, "fuel": 9}],
        context,
    )
    assert value == 7 and context.fuel_used == 9 and context.children == [("child", 1)]
    assert process.killed or process.returncode == 0


async def test_a_worker_that_dies_without_a_frame_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = make_artifact(tmp_path / "a", body("return 1"))
    with pytest.raises(SandboxError, match="^sandbox_invalid_message$"):
        await forged(monkeypatch, artifact, [])


# ---------------------------------------------------------------- OS resource caps


_ALLOCATE = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "blocks = []\n"
    "try:\n"
    "    for _ in range(64):\n"
    "        blocks.append(bytearray(16 * 2**20))\n"
    "except MemoryError:\n"
    "    print('memory-error', flush=True)\n"
    "    sys.exit(3)\n"
    "print('allocated', flush=True)\n"
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
async def test_job_object_caps_process_memory_and_kills_on_close() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _ALLOCATE, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    job = proc_limits.contain(process, memory_bytes=256 * 2**20)
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(b"go\n")
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
        assert line.strip() == b"memory-error"
        assert await asyncio.wait_for(process.wait(), timeout=60) == 3
        assert job.memory_exceeded()
    finally:
        job.close()
    sleeper = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    job = proc_limits.contain(sleeper, memory_bytes=None)
    started = time.perf_counter()
    job.close()
    await asyncio.wait_for(sleeper.wait(), timeout=30)  # killed long before its 60 s sleep
    assert time.perf_counter() - started < wall_limit(30)
    job.close()  # idempotent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX RLIMIT_AS")
def test_limit_self_caps_the_address_space() -> None:
    script = (
        "from architect.runtime._proc_limits import limit_self\n"
        "limit_self(512 * 2**20)\n" + _ALLOCATE
    )
    result = subprocess.run(
        [sys.executable, "-c", script], input=b"go\n", capture_output=True, timeout=120
    )
    assert result.returncode == 3 and result.stdout.strip() == b"memory-error"


def test_limit_self_is_a_no_op_on_windows_and_contain_on_posix() -> None:
    if sys.platform == "win32":
        proc_limits.limit_self(2**30)
        with pytest.raises(ValueError):
            proc_limits.contain(cast(asyncio.subprocess.Process, object()), memory_bytes=5)
    else:
        handle = proc_limits.contain(cast(asyncio.subprocess.Process, object()),
                                     memory_bytes=2**30)
        handle.close()


async def test_forged_game_and_preset_frames(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    from sandbox_v2_support import game_artifact

    game = game_artifact(tmp_path / "g", body("return 1"))
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await forged(monkeypatch, game, [{"op": "step", "action": "ACTION1"}])
    text = make_artifact(tmp_path / "t", body("return 1"))
    with pytest.raises(SandboxError, match="^sandbox_invalid_preset$"):
        await forged(monkeypatch, text, [{"op": "preset", "task": 1}])
    capped = game_artifact(tmp_path / "c", body("return 1"), max_turns=1, calls_per_turn=1)

    async def fake_preset(*args: object) -> JsonValue:
        return {"score": 1}

    monkeypatch.setattr("architect.presets.execute_preset", fake_preset)
    with pytest.raises(SandboxError, match="^sandbox_invalid_call$"):
        await forged(monkeypatch, capped, [{"op": "preset", "task": 1}] * 2)


async def test_memory_kill_detection_and_containment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from architect.runtime import sandbox as sandbox_module

    class Job:
        def __init__(self, exceeded: bool) -> None:
            self.exceeded = exceeded

        def memory_exceeded(self) -> bool:
            return self.exceeded

        def close(self) -> None:
            return None

    class Exited:
        def __init__(self, code: int) -> None:
            self.code = code

        async def wait(self) -> int:
            return self.code

    class Hung:
        async def wait(self) -> int:
            raise OSError

    process = cast(asyncio.subprocess.Process, Exited(-9))
    assert await sandbox_module._memory_killed(process, Job(True))
    assert await sandbox_module._memory_killed(process, Job(False)) == (sys.platform != "win32")
    assert not await sandbox_module._memory_killed(
        cast(asyncio.subprocess.Process, Exited(1)), Job(False)
    )
    assert not await sandbox_module._memory_killed(
        cast(asyncio.subprocess.Process, Hung()), Job(False)
    )

    def refuse(*args: object, **kwargs: object) -> object:
        raise OSError("no job")

    class Real:
        pid = 1

    class Double:
        pid = None

    monkeypatch.setattr(sandbox_module, "contain", refuse)
    # Language 1 keeps its Phase 4 fallback; a double without a pid has nothing to contain.
    sandbox_module._contain(cast(asyncio.subprocess.Process, Real()), required=False).close()
    sandbox_module._contain(cast(asyncio.subprocess.Process, Double()), required=True).close()
    # Language 2 never runs uncapped: a refused Job Object is a fixed code.
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        sandbox_module._contain(cast(asyncio.subprocess.Process, Real()), required=True)


async def test_a_v2_worker_that_cannot_be_contained_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: a failed memory cap used to run the v2 worker uncapped and unreported."""
    from architect.runtime import sandbox as sandbox_module

    spawned: list[asyncio.subprocess.Process] = []

    def refuse(process: asyncio.subprocess.Process, **kwargs: object) -> object:
        spawned.append(process)
        raise OSError("no job")

    monkeypatch.setattr(sandbox_module, "contain", refuse)
    adapter = FakeAdapter()
    artifact = make_artifact(tmp_path / "a", body("return await context.call('x')"))
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        await sandbox_harness(artifact)(RunContext(adapter, Budget(cost_usd=10)), None)
    assert adapter.calls == 0
    assert len(spawned) == 1 and spawned[0].returncode is not None  # reaped, not leaked
    v1 = make_artifact(tmp_path / "b", body("return await context.call('x')"), v1=True)
    assert await sandbox_harness(v1)(RunContext(FakeAdapter(), Budget(cost_usd=10)), None)


@pytest.mark.parametrize("error", [ValueError, RuntimeError, TypeError, AttributeError])
async def test_any_containment_error_fails_a_v2_worker_closed_and_reaps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Review: only OSError became sandbox_worker_failed; any other contain() error
    skipped the caller's reap (guarded by ``except SandboxError``) and leaked the worker."""
    from architect.runtime import sandbox as sandbox_module

    spawned: list[asyncio.subprocess.Process] = []

    def broken(process: asyncio.subprocess.Process, **kwargs: object) -> object:
        spawned.append(process)
        raise error("containment exploded")

    class Real:
        pid = 1

    monkeypatch.setattr(sandbox_module, "contain", broken)
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        sandbox_module._contain(cast(asyncio.subprocess.Process, Real()), required=True)
    spawned.clear()
    adapter = FakeAdapter()
    artifact = make_artifact(tmp_path / "a", body("return await context.call('x')"))
    with pytest.raises(SandboxError, match="^sandbox_worker_failed$"):
        await sandbox_harness(artifact)(RunContext(adapter, Budget(cost_usd=10)), None)
    assert adapter.calls == 0
    assert len(spawned) == 1 and spawned[0].returncode is not None  # reaped, not leaked
    # Language 1 is not failed closed, but its worker is still reaped before the error.
    v1 = make_artifact(tmp_path / "b", body("return await context.call('x')"), v1=True)
    with pytest.raises(error):
        await sandbox_harness(v1)(RunContext(FakeAdapter(), Budget(cost_usd=10)), None)
    assert len(spawned) == 2 and spawned[1].returncode is not None



# ---------------------------------------------------------------- review regressions


def _deadline_context() -> RunContext:
    return RunContext(FakeAdapter(), Budget(compute_s=60))


@pytest.mark.parametrize(
    "copy",
    [
        "y = big + []",
        "y = [] + big",
        "y = big[:]",
        "y = list(big)",
        "extend(y, big)",
        "y = values(d)",
        "y = enumerate(big)",
        "y = items(d)",
        "y = zip(big, big)",
    ],
)
def test_every_copy_charges_one_fuel_per_copied_element(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, copy: str
) -> None:
    # 60,000 elements per copy at 1 fuel each: 2M fuel allows a few dozen, not 4,000.
    source = body(
        "big = list(range(60000))",
        "d = {}",
        "for i in range(60000):",
        "    set_key(d, str(i), i)",
        "for i in range(4000):",
        "    y = []",
        "    " + copy,
        "return 1",
    )
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, source, fuel=2_000_000)
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", frame
    assert time.perf_counter() - started < wall_limit(5)


async def test_list_concatenation_is_bounded_by_fuel_through_the_worker(tmp_path: Path) -> None:
    # The review case: 240M scalar copies used to cost about 20,000 fuel and 9.5 s.
    source = body(
        "big = list(range(60000))", "for i in range(4000):", "    y = big + []", "return len(y)"
    )
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=100_000))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$") as caught:
        await sandbox_harness(artifact)(_deadline_context(), None)
    assert caught.value.line == 4
    assert time.perf_counter() - started < wall_limit(5)


@pytest.mark.parametrize(
    ("setup", "compare"),
    [
        (("a = list(range(1000, 61000))", "b = list(range(1000, 61000))"), "a == b"),
        (("a = list(range(1000, 61000))", "b = list(range(1000, 61000))"), "a != b"),
        (("a = []", "for i in range(20000):", "    append(a, [i + 1000])", "b = a[:]"),
         "a == b"),
        (("a = {}", "for i in range(30000):", "    set_key(a, str(i), i + 1000)",
          "b = {}", "for k in a:", "    set_key(b, k, a[k])"), "a == b"),
        (("a = list(range(1000, 61000))", "l = [a[:], a[:], a[:]]"), "a in l"),
        (("s = 'ab'", "for i in range(16):", "    s = s + s", "t = s[0:1] + s[1:]"),
         "s == t"),
        (("s = 'ab'", "for i in range(16):", "    s = s + s", "t = s[0:1] + s[1:]"),
         "s <= t"),
        (("s = 'ab'", "for i in range(15):", "    s = s + s", "l = []", "for i in range(64):",
          "    append(l, s + str(100 + i))"), "sorted(l)"),
        (("s = 'ab'", "for i in range(15):", "    s = s + s", "l = []", "for i in range(64):",
          "    append(l, s[0:1] + s[1:])"), "s in l"),
    ],
)
def test_comparisons_of_large_values_are_charged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: tuple[str, ...], compare: str
) -> None:
    source = body(*setup, "while True:", "    y = " + compare)
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, source, fuel=1_000_000)
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", frame
    assert time.perf_counter() - started < wall_limit(3)


def test_charged_equality_keeps_python_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = body(
        "g = [[0, 1], [2, 3]]",
        "h = [[0, 1], [2, 3]]",
        "return [g == h, g != h, [1, [2]] == [1, [3]], [1] == [True], {'a': [1]} == {'a': [1.0]},"
        " {'a': 1} == {'b': 1}, [[1]] == [1], [1] == {}, 'x' == 'x', [2, 3] in g, [2] in g,"
        " {'k': [1]} in [0, {'k': [1]}], 1 in [[1]], None == []]",
    )
    frame = run(monkeypatch, tmp_path, source)
    assert frame["value"] == [
        True, False, False, True, True, False, False, False, True, True, False, True, False,
        False,
    ]


def test_digest_is_total_over_lone_surrogates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frame = run(monkeypatch, tmp_path, body("return [digest(task), digest([task, 'e'])]"),
                "\ud800")
    expected = hashlib.sha256('"\ud800"'.encode("utf-8", "surrogatepass")).hexdigest()
    assert frame["value"][0] == expected and len(frame["value"][1]) == 64
    # Valid text hashes exactly as before: plain UTF-8 of canonical JSON.
    frame = run(monkeypatch, tmp_path, body("return digest(task)"), "é")
    assert frame["value"] == hashlib.sha256('"é"'.encode()).hexdigest()


async def test_digest_of_a_parsed_lone_surrogate_through_the_worker(tmp_path: Path) -> None:
    source = body("x = parse_json(task)", "return digest(x)")
    artifact = make_artifact(tmp_path / "a", source)
    value = await sandbox_harness(artifact)(_deadline_context(), '"\\ud800"')
    expected = hashlib.sha256('"\ud800"'.encode("utf-8", "surrogatepass")).hexdigest()
    assert value == expected


@pytest.mark.parametrize(
    ("lines", "line"),
    [
        # Mutation through an alias: the level of ``cur`` is tracked, not assumed 1.
        (("root = []", "cur = root", "for i in range(100000):", "    append(cur, [])",
          "    cur = cur[0]", "return 1"), 5),
        (("root = {}", "cur = root", "for i in range(100):", "    cur['k'] = {}",
          "    cur = cur['k']", "return 1"), 5),
        (("root = {}", "cur = root", "for i in range(100):", "    set_key(cur, 'k', {})",
          "    cur = get(cur, 'k', None)", "return 1"), 5),
        (("root = [[]]", "cur = root", "for i in range(100):", "    insert(cur, 0, [[]])",
          "    cur = at(cur, 0, None)", "return 1"), 5),
        (("root = []", "cur = root", "for i in range(100):", "    extend(cur, [[]])",
          "    for c in cur:", "        cur = c", "return 1"), 5),
        (("root = [[]]", "cur = root", "for i in range(100):", "    append(cur, [[]])",
          "    a, b = cur", "    cur = b", "return 1"), 5),
        (("root = [[]]", "cur = root", "for i in range(100):", "    cur[0] = [[]]",
          "    cur = cur[0] if i >= 0 else root", "return 1"), 5),
        (("root = [[]]", "cur = root", "for i in range(100):", "    cur[0] = [[]]",
          "    cur = None or cur[0]", "return 1"), 5),
    ],
)
def test_nesting_through_aliases_is_bounded_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: tuple[str, ...], line: int
) -> None:
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, body(*lines))
    assert frame == {"op": "error", "code": "sandbox_value_limit", "line": line}
    assert time.perf_counter() - started < wall_limit(5)


def test_levels_follow_helpers_and_their_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helpers = (
        "def grow(l):\n    append(l, [])\n    return l[0]\n"
        "def first(l):\n    return l[0]\n"
    )
    deep = body("root = []", "cur = root", "for i in range(100):", "    cur = grow(cur)",
                "return 1")
    frame = run(monkeypatch, tmp_path, helpers + deep)
    assert frame == {"op": "error", "code": "sandbox_value_limit", "line": 2}
    via_return = body("root = [[]]", "cur = root", "for i in range(100):",
                      "    nxt = first(cur)", "    append(nxt, [[]])", "    cur = nxt",
                      "return 1")
    frame = run(monkeypatch, tmp_path, "def first(l):\n    return l[0]\n" + via_return)
    assert frame == {"op": "error", "code": "sandbox_value_limit", "line": 8}


def test_the_nesting_bound_is_exact_through_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def chain(count: int) -> str:
        return body("root = []", "cur = root", f"for i in range({count}):",
                    "    append(cur, [])", "    cur = cur[0]", "return root")

    deepest = run(monkeypatch, tmp_path, chain(39))
    assert deepest["op"] == "result"
    value, depth = deepest["value"], 0
    while isinstance(value, list):
        value, depth = (value[0] if value else None), depth + 1
    assert depth == 40
    assert run(monkeypatch, tmp_path, chain(40)) == {
        "op": "error", "code": "sandbox_value_limit", "line": 5
    }
    # Popping a subtree out makes it a root again; a stale level is only conservative.
    popped = body("root = [[[]]]", "x = pop(root)", "append(x, [1])", "return [root, x]")
    assert run(monkeypatch, tmp_path, popped)["value"] == [[], [[], [1]]]


async def test_alias_nesting_fails_with_a_line_through_the_worker(tmp_path: Path) -> None:
    # The review case: a 100,001-deep list used to be built, then compared without a line.
    source = body("root = []", "cur = root", "for i in range(100000):", "    append(cur, [])",
                  "    cur = cur[0]", "r2 = []", "return root == r2")
    artifact = make_artifact(tmp_path / "a", source)
    with pytest.raises(SandboxError, match="^sandbox_value_limit$") as caught:
        await sandbox_harness(artifact)(_deadline_context(), None)
    assert caught.value.line == 5


# ---------------------------------------------------------------- review 2: fuel bounds time

_LONG = "x" * 4090
_UCS4 = "\U0001f600" * 4000
_SHUFFLED = [(i * 7919) % 4096 for i in range(4096)]
_GRID = [[1] * 64 for _ in range(64)]
_OTHER = [[(x + y) % 16 for x in range(64)] for y in range(64)]
# Review 4: substring searches whose CPython worst case is window-times-needle compares.
_MID = "a" * 624 + "b" + "a" * 625  # a middle mismatch in a run of "a"s
_MID99 = "a" * 49 + "b" + "a" * 49
_ADAPTIVE = "a" * 58981 + "b" + "a" * 58982  # over a third of the haystack
_ADAPTIVE_HAY = "c" * 11108 + "a" * 119_964  # cheap skips, then 2,000 quadratic windows

# Each case is the worst input found for one builtin path: setup lines, a
# looped expression, the task. Before the fix these ran at 7 to 3,000 us per
# fuel; the contract allows at most 6 (10M fuel in under 60 s).
WALKS: dict[str, tuple[tuple[str, ...], str, JsonValue]] = {
    "sum": (("l = list(range(65536))",), "sum(l)", None),
    "min_max": (("l = list(range(65536))",), "[min(l), max(l)]", None),
    "sorted_long_prefix": (
        ("l = task",), "sorted(l)", [_LONG + str(i).zfill(5) for i in _SHUFFLED]
    ),
    "sorted_ucs4_prefix": (
        ("l = task",), "sorted(l)", [_UCS4 + chr(0x1F600 + i % 50) for i in _SHUFFLED]
    ),
    "max_ucs4_prefix": (
        ("l = task",), "max(l)", [_UCS4 + chr(0x1F600 + i % 50) for i in _SHUFFLED]
    ),
    "ordering_ucs4": (("a = task[0]", "b = task[1]"), "a < b", [_UCS4 + "a", _UCS4 + "b"]),
    "sorted_floats": (("l = task",), "sorted(l)", [((i * 7919) % 65536) / 7.0 for i in
                                                  range(65536)]),
    "in_long_strs": (
        ("l = task['l']", "n = task['n']"), "n in l",
        {"l": ["y" * 4095] * 65536, "n": "y" * 4094 + "z"},
    ),
    "in_short_strs": (("l = task",), "'zz' in l", [str(i) for i in range(65536)]),
    "join_empties": (("l = task",), "join('', l)", [""] * 65536),
    "split": (("s = task",), "split(s, ',')", "," * 65535),
    "keys": (("d = task",), "keys(d)", {str(i): i for i in range(65536)}),
    "equal_dicts": (
        ("d = task[0]", "e = task[1]"), "d == e",
        [{str(i): i for i in range(65536)}, {str(i): i for i in range(65536)}],
    ),
    "equal_mixed_list": (
        ("a = task[0]", "b = task[1]"), "a == b",
        [[[]] + [0] * 65535, [[]] + [0] * 65535],
    ),
    "to_json": (("l = task",), "to_json(l)", [[]] * 40000),
    "digest": (("l = task",), "digest(l)", [[]] * 40000),
    "fstring": (("l = task",), "f'{l}'", [[]] * 40000),
    "parse_json": (("s = task",), "parse_json(s)", "[" + ",".join(["{}"] * 40000) + "]"),
    "extract_json_brackets": (("s = task",), "extract_json(s)", "[" * 131072),
    "extract_json_fences": (("s = task",), "extract_json(s)", "```\n[[[[[[[[[[\n```\n" * 6000),
    "extract_json_empty_fences": (("s = task",), "extract_json(s)", "```" * 43690),
    "extract_json_unclosed_tag": (("s = task",), "extract_json(s)", "```" + "a" * 131_069),
    "extract_json_unclosed_blanks": (("s = task",), "extract_json(s)", "```json" + " " * 131_065),
    "find_middle_mismatch": (("h = task[0]", "n = task[1]"), "find(h, n)", ["a" * 2499, _MID]),
    "count_middle_mismatch": (
        ("h = task[0]", "n = task[1]"), "count(h, n)", ["a" * 29_999, _MID99]
    ),
    "in_middle_mismatch": (("h = task[0]", "n = task[1]"), "n in h", ["a" * 2499, _MID]),
    "split_long_separator": (("h = task[0]", "n = task[1]"), "split(h, n)", ["a" * 2499, _MID]),
    "replace_long_old": (
        ("h = task[0]", "n = task[1]"), "replace(h, n, '')", ["a" * 2499, _MID]
    ),
    "find_adaptive_tail": (
        ("h = task[0]", "n = task[1]"), "find(h, n)", [_ADAPTIVE_HAY, _ADAPTIVE]
    ),
    "count_adaptive_tail": (
        ("h = task[0]", "n = task[1]"), "count(h, n)", [_ADAPTIVE_HAY, _ADAPTIVE]
    ),
    "lower_non_ascii": (("s = task",), "lower(s)", "\u03a3A" * 65536),
    "list_range": ((), "list(range(65536))", None),
    "objects": (("g = task",), "objects(g)", _GRID),
    "grid_diff": (("g = task[0]", "h = task[1]"), "grid_diff(g, h)", [_GRID, _OTHER]),
    "find_cells": (("g = task",), "find_cells(g, 1)", _GRID),
    "count_colors": (("g = task",), "count_colors(g)", _OTHER),
    "grid_text": (("g = task",), "grid_text(g)", _OTHER),
    "grid_equal": (("g = task[0]", "h = task[1]"), "grid_equal(g, h)", [_OTHER, _OTHER]),
    "crop": (("g = task",), "crop(g, 0, 0, 63, 63)", _OTHER),
    # Pack v4: the worst inputs for each new builtin path.
    "isalnum_ucs4": (("s = task",), "isalnum(s)", "\U0001d7d8" * 65536),
    "isupper_digits": (("s = task",), "s.isupper()", "1" * 131072),
    "split_whitespace": (("s = task",), "s.split()", "a " * 65536),
    "split_whitespace_ucs4": (("s = task",), "split(s)", "\U0001f600 " * 65536),
    "strip_chars_ucs4": (  # about 264,000 fuel a call, so 1M fuel runs several
        ("s = task[0]", "c = task[1]"), "s.strip(c)",
        ["\U0001f600" * 16384, "\U0001f601" * 16383 + "\U0001f600"],
    ),
    "lstrip_chars_ascii": (
        ("s = task[0]", "c = task[1]"), "lstrip(s, c)", ["a" * 131072, "bcdefghijklmnopa"]
    ),
    "rstrip_whitespace_ucs2": (("s = task",), "s.rstrip()", "　" * 131072),
    "rfind_worst": (
        ("h = task[0]", "n = task[1]"), "rfind(h, n)", ["a" * 131072, "ab" + "a" * 998]
    ),
    "rfind_ucs4": (
        ("h = task[0]", "n = task[1]"), "h.rfind(n)",
        ["\U0001f600" * 131072, "\U0001f600\U0001f601" + "\U0001f600" * 98],
    ),
    "zip_three": (("a = list(range(65536))",), "zip(a, a, a)", None),
    "copy_dict": (("d = task",), "d.copy()", {str(i): [i] for i in range(65536)}),
    "max_two_strs": (
        ("a = task[0]", "b = task[1]"), "max(a, b)", ["x" * 131071 + "a", "x" * 131071 + "b"]
    ),
}


@pytest.mark.parametrize("name", sorted(WALKS))
def test_fuel_bounds_the_wall_time_of_every_builtin_walk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    setup, expression, task = WALKS[name]
    source = body(*setup, "while True:", "    y = " + expression)
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, source, task, fuel=1_000_000)
    elapsed = time.perf_counter() - started
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", frame
    # 1M fuel in under 6 s is the contracted 10M in under 60 s; measured <= 0.6 s.
    assert elapsed < 6, f"{name}: {elapsed:.2f} s for 1M fuel"


async def test_a_10m_fuel_sum_loop_finishes_in_under_60_seconds(tmp_path: Path) -> None:
    # The review case: this ran 274.94 s before sum paid for its Python-level fold.
    source = body("l = list(range(65536))", "while True:", "    x = sum(l)")
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=10_000_000))
    context = RunContext(FakeAdapter(), Budget(compute_s=120))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$") as caught:
        await sandbox_harness(artifact)(context, None)
    assert time.perf_counter() - started < wall_limit(60)
    assert caught.value.line == 4 and context.fuel_used == 10_000_000


@pytest.mark.parametrize(
    ("setup", "loop"),
    [
        # Review 4: this ran 70.3 s for 2,758 fuel (the fence regex backtracked).
        (("s = 'a'", "while len(s) < 65536:", "    s = s + s", "s = '```' + s + s[0:65533]"),
         "x = extract_json(s)"),
        (("s = ' '", "while len(s) < 65536:", "    s = s + s", "s = '```json' + s + s[0:65529]"),
         "x = extract_json(s)"),
        # Review 4: this ran 108.9 s (find was charged for the haystack only).
        (("h = 'a'", "while len(h) < 2499:", "    h = h + 'a'", "n = h[0:600] + 'b' + h[0:600]"),
         "x = find(h, n)"),
        # Pack v4: the reverse search and strip with characters, each a quadratic worst case.
        (("h = 'a'", "while len(h) < 131072:", "    h = h + h", "n = 'ab' + h[0:998]"),
         "x = rfind(h, n)"),
        (("s = '\\U0001f600'", "while len(s) < 32768:", "    s = s + s",
          "c = replace(s, '\\U0001f600', '\\U0001f601')[0:32767] + s[0:1]"),
         "x = s.strip(c)"),
    ],
    ids=["unclosed_fence_tag", "unclosed_fence_blanks", "find_middle_mismatch", "rfind_worst",
         "strip_chars_ucs4"],
)
async def test_a_10m_fuel_search_loop_finishes_in_under_60_seconds(
    tmp_path: Path, setup: tuple[str, ...], loop: str
) -> None:
    source = body(*setup, "while True:", "    " + loop)
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=10_000_000))
    context = RunContext(FakeAdapter(), Budget(compute_s=120))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$") as caught:
        await sandbox_harness(artifact)(context, None)
    assert time.perf_counter() - started < wall_limit(60)
    assert caught.value.line == 7 and context.fuel_used == 10_000_000


# ---------------------------------------------------------------- review 2: unbounded task ints


async def test_a_range_span_beyond_sys_maxsize_is_the_value_limit_with_a_line(
    tmp_path: Path,
) -> None:
    # The review case: this used to kill the worker (sandbox_worker_failed, no line).
    source = body("r = range(-9223372036854775807, 9223372036854775807)", "return 1")
    artifact = make_artifact(tmp_path / "a", source)
    with pytest.raises(SandboxError, match="^sandbox_value_limit$") as caught:
        await sandbox_harness(artifact)(_deadline_context(), None)
    assert caught.value.line == 2


@pytest.mark.parametrize(
    ("lines", "task", "expected"),
    [
        (("r = range(task['a'], task['b'])", "return 1"), {"a": -10**30, "b": 10**30},
         ("sandbox_value_limit", 2)),
        (("r = range(task['b'], task['a'], -1)", "return 1"), {"a": -10**30, "b": 10**30},
         ("sandbox_value_limit", 2)),
        (("return len(range(task))",), 10**40, ("sandbox_value_limit", 2)),
        (("return list(range(task['a'], task['b']))",), {"a": 10**40, "b": 10**40 + 3},
         [10**40, 10**40 + 1, 10**40 + 2]),
        (("return at(range(3), task, 'none')",), 10**40, "none"),
        (("l = [1]", "insert(l, task['a'], 2)", "insert(l, task['b'], 0)", "return l"),
         {"a": 10**40, "b": -10**40}, [0, 1, 2]),
        (("return float(task)",), 10**309, ("sandbox_value", 2)),
        (("try:", "    x = float(task)", "except:", "    x = 'caught'", "return x"), 10**309,
         "caught"),
        (("return int(task)",), 10**40, ("sandbox_value", 2)),
        (("return sum([1.5, task])",), 10**400, ("sandbox_value", 2)),
        (("try:", "    x = sum([1.5, task])", "except:", "    x = 'caught'", "return x"),
         10**400, "caught"),
        (("return round(task)",), 10**40, ("sandbox_value", 2)),
        (("return abs(-task)",), 10**40, ("sandbox_value", 2)),
        (("l = [1]", "return l[task]"), 10**40, ("sandbox_index", 3)),
        (("return 1.5 + task",), 10**400, ("sandbox_value", 2)),
        (("return [1.5 < task, task == 1.5, task in [1.5], max([1.5, task])]",), 10**400,
         [True, False, False, 10**400]),
    ],
)
def test_builtins_are_total_over_unbounded_task_ints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: tuple[str, ...], task: JsonValue,
    expected: object,
) -> None:
    frame = run(monkeypatch, tmp_path, body(*lines), task)
    if isinstance(expected, tuple):
        assert frame == {"op": "error", "code": expected[0], "line": expected[1]}
    else:
        assert frame["op"] == "result" and frame["value"] == expected, frame


# ---------------------------------------------------------------- review 3: big ints pay by digits

# Python caps int <-> decimal conversion at 4,300 digits, so this is the largest int a
# task, a capability reply or (before the fix) parse_json could hand the interpreter.
_HUGE = int("9" * 4300)
_HALF = int("7" * 2150)
_TWIN = int("9" * 4300)  # equal to _HUGE but a distinct object: == walks every digit
_NINES = ("s = '9'", "while len(s) < 4300:", "    s = s + s")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9223372036854775807", 2**63 - 1),
        ("-9223372036854775807", -(2**63) + 1),
        ("9223372036854775808", None),
        ("-9223372036854775808", None),
        ('{"a": [1, 99999999999999999999]}', None),
        ("9" * 4300, None),
        ("1e300", 1e300),
    ],
)
def test_parse_json_refuses_ints_beyond_the_arithmetic_bound(
    text: str, expected: object
) -> None:
    assert builtins.parse_json_text(text) == expected
    wrapped = builtins.extract_json_text(f"prose ```json\n[{text}]\n``` more")
    assert wrapped == (None if expected is None else [expected])


def test_harness_code_cannot_mint_a_big_int_from_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The review program: a 4,300-digit int parsed from a str the harness built.
    source = body(
        *_NINES,
        "return [parse_json(s[0:4300]), extract_json('[' + s[0:4300] + ']'),"
        " parse_json(s[0:18]), kind(int(s[0:18]))]",
    )
    frame = run(monkeypatch, tmp_path, source)
    assert frame["op"] == "result" and frame["value"] == [None, None, int("9" * 18), "int"]


# Each case loops one operation on a 4,300-digit task int. Before the fix these ran
# at 6 to 9 us per fuel (str 84 s, f-string 86 s, b * b 64 s, b % c 77 s per 10M):
# the decimal conversion and the arithmetic were charged as if the int were small.
BIG_INT_LOOPS: dict[str, tuple[str, ...]] = {
    "str": ("x = str(b)",),
    "fstring": ("x = f'{b}'",),
    "fstring_in_list": ("x = f'{[b, 1]}'",),
    "to_json": ("x = to_json({'k': b})",),
    "digest": ("x = digest([b])",),
    "mul": ("try:", "    x = b * b", "except:", "    pass"),
    "mod": ("try:", "    x = b % c", "except:", "    pass"),
    "floordiv": ("try:", "    x = b // c", "except:", "    pass"),
    "div": ("try:", "    x = b / c", "except:", "    pass"),
    "sub": ("x = b - t",),
    "add_float": ("try:", "    x = b + 1.5", "except:", "    pass"),
    "augmented": ("try:", "    x = b", "    x *= c", "except:", "    pass"),
    "neg": ("try:", "    x = -b", "except:", "    pass"),
    "ordering": ("x = b < t",),
    "equal": ("x = b == t",),
    "equal_lists": ("x = l == m",),
    "in_list": ("x = n in l",),
    "sorted": ("x = sorted(l)",),
    "max": ("x = max(l)",),
}


@pytest.mark.parametrize("name", sorted(BIG_INT_LOOPS))
def test_fuel_bounds_the_wall_time_of_big_task_int_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    setup = (
        "b = task['b']", "c = task['c']", "t = task['t']", "n = task['n']",
        "l = [b, t]", "m = [t, b]",
        "while len(l) < 8192:", "    l = l + l", "    m = m + m",
    )
    loop = tuple("    " + line for line in BIG_INT_LOOPS[name])
    source = body(*setup, "while True:", *loop)
    task: JsonValue = {"b": _HUGE, "c": _HALF, "t": _TWIN, "n": _HUGE - 1}
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, source, task, fuel=1_000_000)
    elapsed = time.perf_counter() - started
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", frame
    # 1M fuel in under 6 s is the contracted 10M in under 60 s.
    assert elapsed < 6, f"{name}: {elapsed:.2f} s for 1M fuel"


@pytest.mark.parametrize(
    "operation",
    ["x = str(task)", "x = f'{task}'", "x = digest(task)", "x = to_json([task])"],
)
def test_rendering_a_big_int_is_charged_for_its_digits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    source = body(operation, "return 1")
    small = run(monkeypatch, tmp_path / "small", source, 7)
    big = run(monkeypatch, tmp_path / "big", source, _HUGE)
    # 4,300 digits are 223 64-bit words: 1 + 223 * 223 // 64 = 778 fuel of digit work.
    assert big["fuel"] - small["fuel"] >= 778, (small, big)
    bounded = run(monkeypatch, tmp_path / "bounded", source, 2**63 - 1)
    assert bounded["fuel"] - small["fuel"] < 16, (small, bounded)


def test_arithmetic_on_a_big_int_is_charged_for_its_digits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = body("b = task", "try:", "    x = b * b", "except:", "    x = 'caught'", "return x")
    small = run(monkeypatch, tmp_path / "small", source, 3)
    assert small == {"op": "result", "value": 9, "fuel": 9}  # bounded ints: 1 per node
    big = run(monkeypatch, tmp_path / "big", source, _HUGE)
    # 2 * 223 words: 1 + 446 * 446 // 256 = 778 fuel, charged before b * b is attempted.
    assert big["value"] == "caught" and big["fuel"] - small["fuel"] >= 778, big
    assert run(monkeypatch, tmp_path / "raw", body("return task * 2"), _HUGE) == {
        "op": "error", "code": "sandbox_value", "line": 2,
    }


def test_a_big_int_crossing_an_rpc_boundary_is_charged_for_its_digits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = body(
        "l = [task]", "while len(l) < 16:", "    l = l + l",
        "while True:", "    x = await context.harness('kid', l)",
    )
    artifact = make_artifact(tmp_path / "a", forge=binding(fuel=1_000_000, children=("kid",)))
    validate_source(source, artifact)
    started = time.perf_counter()
    frame, sent = interpret(monkeypatch, artifact, source, _HUGE)
    assert time.perf_counter() - started < wall_limit(6)
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", frame
    # 16 ints of 778 fuel each: at most 1M / 12,448 = 80 messages are ever encoded.
    assert 0 < len(sent) <= 81


@pytest.mark.parametrize(
    ("source", "task"),
    [
        (body(*_NINES, "b = parse_json(s[0:4300])", "while True:", "    x = f'{b}'"), None),
        (body("b = task", "while True:", "    x = f'{b}'"), _HUGE),
        (body("b = task", "while True:", "    try:", "        x = b * b",
              "    except:", "        pass"), _HUGE),
    ],
    ids=["review_program", "task_fstring", "task_mul"],
)
async def test_a_10m_fuel_big_int_loop_finishes_in_under_60_seconds(
    tmp_path: Path, source: str, task: JsonValue
) -> None:
    # The review case: the first program ran 84.6 s through the real worker.
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=10_000_000))
    context = RunContext(FakeAdapter(), Budget(compute_s=120))
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$"):
        await sandbox_harness(artifact)(context, task)
    assert time.perf_counter() - started < wall_limit(60)
    assert context.fuel_used == 10_000_000


# ------------------------------------------------- review: fuel bounds capability traffic


class _GridTurns(TurnContext):
    """A turn context whose frame is an ``n`` x ``n`` grid (64 is ARC-sized)."""

    def __init__(self, n: int, *, actions_left: int = 8) -> None:
        super().__init__(actions_left=actions_left)
        self.n = n

    def observation(self, *, refused: bool = False) -> dict[str, JsonValue]:
        observation = super().observation(refused=refused)
        observation["frame"] = [[(i + j) % 10 for j in range(self.n)] for i in range(self.n)]
        return observation


_OBSERVE_FLOOD = body("while True:", "    x = await context.observe()")
# About 84 KB of action per message, sent to a host that refuses every step.
_REFUSED_STEP_FLOOD = body(
    "a = {}", "i = 0", "while i < 6000:", "    set_key(a, 'k' + str(i), i)", "    i += 1",
    "while True:", "    x = await context.step(a)",
)


@pytest.mark.parametrize(
    ("source", "size", "actions_left"),
    [(_OBSERVE_FLOOD, 2, 8), (_OBSERVE_FLOOD, 64, 8), (_REFUSED_STEP_FLOOD, 2, 0)],
    ids=["observe_2x2", "observe_64x64", "refused_step_84kb"],
)
async def test_a_10m_fuel_capability_flood_ends_in_sandbox_fuel_under_60_seconds(
    tmp_path: Path, source: str, size: int, actions_left: int
) -> None:
    # The review case: observe and refused steps escape the op cap and used to
    # cost 3 fuel each, so these ran to the host deadline (projected 3 min to
    # 54 min for 10M fuel) instead of failing fast on fuel. Measured about 3-6 s.
    artifact = game_artifact(tmp_path / "g", source)
    artifact = artifact.model_copy(
        update={"forge": binding(fuel=10_000_000, call_limit=4)}
    )
    context = _GridTurns(size, actions_left=actions_left)
    started = time.perf_counter()
    with pytest.raises(SandboxError, match="^sandbox_fuel$"):
        await sandbox_harness(artifact)(context, None)
    assert time.perf_counter() - started < wall_limit(60)
    assert context.fuel_used == 10_000_000
    assert not context.steps


def test_each_capability_rpc_is_charged_its_round_trip_and_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from architect.runtime import sandbox_language as language

    grid = [[(i + j) % 10 for j in range(64)] for i in range(64)]
    artifact = game_artifact(tmp_path / "g", _OBSERVE_FLOOD)
    one = body("x = await context.observe()", "return 0")
    frame, sent = interpret(monkeypatch, artifact, one, replies=({"frame": grid},))
    assert sent == [{"op": "observe"}] and frame["op"] == "result"
    sent_bytes = len(_encode({"op": "observe"}))
    reply_bytes = len(_encode({"value": {"frame": grid}}))
    rpc = language.RPC_FUEL + (
        sent_bytes // language.RPC_BYTES_PER_FUEL + reply_bytes // language.RPC_BYTES_PER_FUEL
    )
    assert rpc > 1_500  # an ARC frame is not a 3-fuel poll
    baseline, _ = interpret(monkeypatch, artifact, body("x = 0", "return 0"))
    assert frame["fuel"] - baseline["fuel"] == rpc
    # A flood stops on fuel. Only the last message may go out without its reply
    # being paid for; that reply then fails on fuel before it is decoded.
    small = artifact.model_copy(update={"forge": binding(fuel=100_000, call_limit=4)})
    frame, sent = interpret(monkeypatch, small, _OBSERVE_FLOOD, replies=({"frame": grid},) * 99)
    assert frame["code"] == "sandbox_fuel" and frame["line"] == 3
    assert 0 < len(sent) <= 100_000 // rpc + 1


def test_copied_defaults_are_charged_and_end_in_fuel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A default is copied for each call, one fuel per element, so a loop calling a
    helper with a big list default stops on fuel like any other copy."""
    big = "BIG = [" + ", ".join("0" for _ in range(2000)) + "]\n"
    source = big + "def f(x=BIG):\n    return 1\n" + body(
        "n = 0", "while True:", "    n += f()"
    )
    started = time.perf_counter()
    frame = run(monkeypatch, tmp_path, source, fuel=1_000_000)
    assert frame["op"] == "error" and frame["code"] == "sandbox_fuel"
    assert time.perf_counter() - started < wall_limit(30)


def test_a_main_guard_is_dropped_unread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only ``if __name__ == "__main__":`` with no else is dropped: nothing in it is checked
    or run, so it can hold anything, and nothing outside it changes."""
    source = body("return 'ran'") + (
        'if __name__ == "__main__":\n'
        "    import os\n"
        "    os.system('x')\n"
        "    while True:\n"
        "        pass\n"
    )
    assert run(monkeypatch, tmp_path, source)["value"] == "ran"


def test_comprehension_copies_and_set_checks_end_in_fuel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A comprehension's work is the loop's work: every copy and every membership test of
    a set comprehension is charged, so 10M fuel ends in seconds whatever it builds."""
    for lines in (
        ("big = list(range(65000))", "return len([big for _ in range(60000)])"),
        ("return len({x for x in range(100000)})",),
        ("big = list(range(65000))", "return len([[x for x in big] for _ in range(60000)])"),
        ("return any(any(x < 0 for x in range(100000)) for _ in range(100000))",),
    ):
        started = time.perf_counter()
        frame = run(monkeypatch, tmp_path, body(*lines))
        assert frame["op"] == "error" and frame["code"] == "sandbox_fuel", (lines, frame)
        assert time.perf_counter() - started < wall_limit(20), lines


async def test_a_comprehension_run_is_killed_by_the_host_deadline(tmp_path: Path) -> None:
    source = body("return [[y for y in range(60000)] for x in range(100000)]")
    artifact = make_artifact(tmp_path / "a", source, forge=binding(fuel=100_000_000))
    started = time.perf_counter()
    with pytest.raises(BudgetExceeded):
        await sandbox_harness(artifact)(RunContext(FakeAdapter(), Budget(compute_s=2)), None)
    assert time.perf_counter() - started < wall_limit(15)


def test_method_spellings_cost_what_the_builtins_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for lines in (
        ("s = 'ab'", "l = []", "while True:", "    l.append(s)"),
        ("s = 'a,b'", "while True:", "    x = s.split(',')"),
    ):
        frame = run(monkeypatch, tmp_path, body(*lines), fuel=200_000)
        assert frame["op"] == "error"
        assert frame["code"] in {"sandbox_fuel", "sandbox_value_limit"}
