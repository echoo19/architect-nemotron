"""Deterministic fake-model tests through the real pinned Pi SDK and tools."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from architect.backends.pi import PiBackend, PiConfig, PiError
from architect.models import FakeAdapter, ModelResponse
from architect.runtime import BudgetExceeded, RunContext
from architect.spec import Budget


def response(text: str) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=0)


def calls(name: str, **args: object) -> str:
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})


def context(repo: Path, texts: list[str], **kwargs: object) -> RunContext:
    return RunContext(
        FakeAdapter(responses=[response(text) for text in texts]),
        Budget(cost_usd=1, compute_s=60),
        repository=repo,
        **kwargs,
    )


def pack(*tools: str) -> dict:
    return {
        "tools": list(tools),
        "prompt_fragment": "Implement and verify the task.",
        "context_policy": {"max_characters": 2048},
        "subagent_rules": ["Bounded compiler only"],
    }


async def test_actual_sdk_write_edit_read_and_native_test(tmp_path: Path) -> None:
    backend = PiBackend()
    ctx = context(
        tmp_path,
        [
            calls("write", path="answer.py", content="answer = 40\n"),
            calls("edit", path="answer.py", edits=[{"oldText": "40", "newText": "42"}]),
            calls("read", path="answer.py"),
            '{"ok":true}',
        ],
    )
    assert await backend.run(ctx, "implement", pack("write", "edit", "read")) == '{"ok":true}'
    assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
    # A separate native Python process verifies the output, not a model claim.
    import sys

    checked = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from answer import answer; assert answer == 42",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await checked.communicate()
    assert checked.returncode == 0
    evidence = backend.last_evidence
    assert evidence and evidence.extension_loaded and evidence.exit_code == 0
    assert evidence.model_calls == 4 and evidence.tool_calls == 3
    assert evidence.pid > 0
    assert evidence.versions == {
        name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")
    }
    assert ctx.tokens_in == 40 and ctx.tokens_out == 40


@pytest.mark.parametrize("target", ["../escape.txt", "@../escape.txt", "~/escape.txt"])
async def test_traversal_denied(tmp_path: Path, target: str) -> None:
    backend = PiBackend()
    ctx = context(tmp_path, [calls("write", path=target, content="no"), "denied"])
    assert await backend.run(ctx, {}, pack("write")) == "denied"
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


async def test_budget_refusal_precedes_tool(tmp_path: Path) -> None:
    backend = PiBackend()
    ctx = RunContext(
        FakeAdapter(
            responses=[
                response(calls("write", path="bad", content="no")).model_copy(
                    update={"cost_usd": 0.2}
                )
            ]
        ),
        Budget(cost_usd=0.1, compute_s=60),
        repository=tmp_path,
    )
    with pytest.raises(BudgetExceeded):
        await backend.run(ctx, {}, pack("write"))
    assert not (tmp_path / "bad").exists()
    assert backend.last_evidence and backend.last_evidence.tool_calls == 0


async def test_evaluation_cannot_compile(tmp_path: Path) -> None:
    seen = []

    async def compile_tool(args: dict) -> str:
        seen.append(args)
        return "compiled"

    backend = PiBackend(PiConfig(allowed_tools=("compile_agent",)), compiler_tool=compile_tool)
    ctx = context(tmp_path, [calls("compile_agent", request="feature"), "done"], is_evaluation=True)
    assert await backend.run(ctx, {}, pack("compile_agent")) == "done"
    assert seen == []


async def test_licensed_compile_is_actual_extension_tool(tmp_path: Path) -> None:
    seen = []

    async def compile_tool(args: dict) -> str:
        seen.append(args)
        return "proposed"

    backend = PiBackend(PiConfig(allowed_tools=("compile_agent",)), compiler_tool=compile_tool)
    ctx = context(
        tmp_path, [calls("compile_agent", request="feature"), "done"], is_evaluation=False
    )
    assert await backend.run(ctx, {}, pack("compile_agent")) == "done"
    assert seen == [{"request": "feature"}]
    assert backend.last_evidence.tool_calls == 1


async def test_shell_requires_operator_grant(tmp_path: Path) -> None:
    backend = PiBackend(PiConfig(allowed_tools=("bash",)))
    ctx = context(tmp_path, [calls("bash", command="echo bad > bad.txt"), "denied"])
    assert await backend.run(ctx, {"allow_shell": True}, pack("bash")) == "denied"
    assert not (tmp_path / "bad.txt").exists()


async def test_real_shell_opt_in(tmp_path: Path) -> None:
    backend = PiBackend(PiConfig(allowed_tools=("bash",), allow_shell=True))
    ctx = context(tmp_path, [calls("bash", command="printf 'yes' > shell.txt"), "done"])
    assert await backend.run(ctx, {}, pack("bash")) == "done"
    assert (tmp_path / "shell.txt").read_text() == "yes"


async def test_ordinary_json_is_final_not_control(tmp_path: Path) -> None:
    backend = PiBackend()
    value = '{"text":"ordinary","ok":true}'
    assert await backend.run(context(tmp_path, [value]), {}, pack()) == value


async def test_turn_limit(tmp_path: Path) -> None:
    backend = PiBackend(PiConfig(max_turns=1))
    ctx = context(tmp_path, [calls("write", path="one", content="first"), "done"])
    with pytest.raises(PiError, match="turn limit"):
        await backend.run(ctx, {}, pack("write"))
    assert (tmp_path / "one").read_text() == "first"


def test_lock_pins_all_three() -> None:
    directory = Path(__file__).parents[1] / "architect/backends/pi-node"
    lock = json.loads((directory / "package-lock.json").read_text())
    for name in ("pi-ai", "pi-agent-core", "pi-coding-agent"):
        assert lock["packages"][f"node_modules/@mariozechner/{name}"]["version"] == "0.73.1"
        assert lock["packages"][f"node_modules/@mariozechner/{name}"]["integrity"].startswith(
            "sha512-"
        )


async def test_underquoted_response_cannot_execute_tools(tmp_path: Path) -> None:
    from architect.models import UsageQuote

    class Underquote(FakeAdapter):
        def quote(self, request):
            return UsageQuote(tokens_in=10, tokens_out=10, cost_usd=0.01)

    backend = PiBackend()
    ctx = RunContext(
        Underquote(
            responses=[
                response(calls("write", path="bad", content="no")).model_copy(
                    update={"cost_usd": 0.02}
                )
            ]
        ),
        Budget(cost_usd=1, compute_s=60),
        repository=tmp_path,
    )
    with pytest.raises(BudgetExceeded, match="reserved usage"):
        await backend.run(ctx, {}, pack("write"))
    assert not (tmp_path / "bad").exists()
    assert backend.last_evidence.tool_calls == 0


async def test_ambient_extension_and_context_are_not_loaded(tmp_path: Path) -> None:
    (tmp_path / ".pi/extensions").mkdir(parents=True)
    (tmp_path / ".pi/extensions/evil.ts").write_text(
        'throw new Error("ambient extension executed")', encoding="utf8"
    )
    (tmp_path / "AGENTS.md").write_text("UNTRUSTED_AMBIENT_MARKER", encoding="utf8")

    class Inspect(FakeAdapter):
        async def complete(self, request):
            assert "UNTRUSTED_AMBIENT_MARKER" not in request.prompt
            assert "bound-fragment-marker" in request.prompt
            assert request.max_output_tokens == 256
            return await super().complete(request)

    backend = PiBackend()
    ctx = RunContext(Inspect(), Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    bound = pack()
    bound.update(prompt_fragment="bound-fragment-marker", max_output_tokens=256)
    assert await backend.run(ctx, {}, bound) == "ok"


async def test_empty_subagent_rules_disables_compiler(tmp_path: Path) -> None:
    async def forbidden(args):
        pytest.fail("Unbound compiler called")

    backend = PiBackend(PiConfig(allowed_tools=("compile_agent",)), compiler_tool=forbidden)
    bound = pack("compile_agent")
    bound["subagent_rules"] = []
    assert (
        await backend.run(
            context(
                tmp_path, [calls("compile_agent", request="feature"), "no"], is_evaluation=False
            ),
            {},
            bound,
        )
        == "no"
    )


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32")
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not stat.exists() or stat.read_text().split()[2] != "Z"
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("mode", ["success", "cancel", "deadline"])
async def test_whole_tree_cleanup_including_background_child(tmp_path: Path, mode: str) -> None:
    import shlex

    program = (
        'const cp = require("node:child_process"), fs = require("node:fs");'
        'const child = cp.spawn(process.execPath,["-e","setInterval(()=>{},1000)"],'
        '{stdio:"ignore",detached:process.platform==="win32"});child.unref();'
        'fs.writeFileSync("child.pid",String(child.pid));'
    )
    entered = asyncio.Event()
    never = asyncio.Event()

    class WaitingAdapter(FakeAdapter):
        async def complete(self, request):
            if self.calls == 1 and mode != "success":
                entered.set()
                await never.wait()
            return await super().complete(request)

    adapter = WaitingAdapter(
        responses=[
            response(calls("bash", command="node -e " + shlex.quote(program))),
            response("done"),
        ]
    )
    ctx = RunContext(
        adapter,
        Budget(cost_usd=1, compute_s=4 if mode == "deadline" else 60),
        repository=tmp_path,
    )
    backend = PiBackend(PiConfig(allowed_tools=("bash",), allow_shell=True))
    evidence = []

    async def execute():
        try:
            return await backend.run(ctx, {}, pack("bash"))
        finally:
            evidence.append(backend.last_evidence)

    task = asyncio.create_task(execute())
    if mode != "success":
        await asyncio.wait_for(entered.wait(), 30)
        assert process_alive(int((tmp_path / "child.pid").read_text()))
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(BudgetExceeded, match="compute budget"):
                await task
    else:
        assert await task == "done"
    assert evidence[0] and not process_alive(evidence[0].pid)
    assert not process_alive(int((tmp_path / "child.pid").read_text()))


async def test_write_to_exited_bridge_is_fixed_pi_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from architect.backends import pi

    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await real_spawn(sys.executable, "-c", "pass", **kwargs)
        await process.wait()  # nothing is left to read the init message
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PiError):
        await PiBackend().run(context(tmp_path, []), {}, pack())


@pytest.mark.skipif(os.name == "nt", reason="POSIX session cleanup")
async def test_exited_bridge_cleanup_reaps_regrouped_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import sys

    from architect.backends import pi

    marker = tmp_path / "child.pid"
    # The stand-in bridge forks a worker that leaves its process group (keeping
    # the bridge's session id), then exits before speaking the protocol.
    script = (
        "import os, time\n"
        "r, w = os.pipe()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    null = os.open(os.devnull, os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(null, fd)\n"
        "    os.setpgid(0, 0)\n"
        "    os.write(w, b'x')\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "os.read(r, 1)\n"
        f"open({str(marker)!r}, 'w').write(str(pid))\n"
    )
    real_spawn = asyncio.create_subprocess_exec
    bridges = []

    class DeadStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        process = await real_spawn(sys.executable, "-c", script, **kwargs)
        await process.wait()  # the bridge has exited and been reaped before cleanup
        process.stdin.close()
        process.stdin = DeadStdin()  # let init "send" so the run fails on protocol EOF
        bridges.append(process)
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", spawn)
    try:
        with pytest.raises(PiError, match="Invalid Pi protocol"):
            await PiBackend().run(context(tmp_path, []), {}, pack())
        assert bridges and bridges[0].returncode == 0
        child = int(marker.read_text())
        for _ in range(100):
            if not process_alive(child):
                break
            await asyncio.sleep(0.05)
        assert not process_alive(child)
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _live_non_zombie(pid: int) -> bool:
    import subprocess

    listing = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(listing) and not listing.startswith("Z")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_zombie_only_group_after_reap_does_not_mask_pi_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from architect.backends import pi

    pids = tmp_path / "children.pid"
    # The stand-in bridge leaves two background workers in its group and exits.
    quiet = "</dev/null >/dev/null 2>&1"
    script = f"sleep 30 {quiet} & echo $! > {pids}; sleep 30 {quiet} & echo $! >> {pids}; exit 0"
    real_spawn = asyncio.create_subprocess_exec
    bridges = []

    class DeadStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        process = await real_spawn("/bin/sh", "-c", script, **kwargs)
        await process.wait()
        process.stdin.close()
        process.stdin = DeadStdin()
        bridges.append(process)
        return process

    monkeypatch.setattr(pi.asyncio, "create_subprocess_exec", spawn)
    try:
        # The reaped workers leave a zombie-only group (EPERM from killpg on
        # macOS); the protocol failure must still be the error that escapes.
        with pytest.raises(PiError, match="Invalid Pi protocol"):
            await PiBackend().run(context(tmp_path, []), {}, pack())
        children = [int(line) for line in pids.read_text().split()]
        assert len(children) == 2
        for _ in range(100):
            if not any(_live_non_zombie(child) for child in children):
                break
            await asyncio.sleep(0.05)
        assert not any(_live_non_zombie(child) for child in children)
        # The group is gone or zombie-only: the final group kill is benign.
        assert not pi._group_has_live_member(bridges[0].pid)
        pi._kill_posix_group(bridges[0].pid)
    finally:
        if pids.exists():
            for line in pids.read_text().split():
                try:
                    os.kill(int(line), signal.SIGKILL)
                except OSError:
                    pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_group_kill_with_live_member_is_fixed_pi_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from architect.backends import pi

    def denied(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(pi, "_group_has_live_member", lambda pgid: True)
    with pytest.raises(PiError, match="could not be terminated"):
        pi._kill_posix_group(12345)
    monkeypatch.setattr(pi, "_group_has_live_member", lambda pgid: False)
    pi._kill_posix_group(12345)


def test_exited_root_pid_is_not_walked_by_parentage(monkeypatch: pytest.MonkeyPatch) -> None:
    from architect.backends.pi import _tree_members

    if os.name == "nt":
        pytest.skip("POSIX process table")
    root = 4000
    table = {
        4001: (1, 4001, root),  # regrouped but still in the exited root's session
        4002: (4001, 4002, 4002),  # re-sessioned child of a member
        4003: (root, 4003, 4003),  # child of an unrelated process that reused the PID
        os.getpid(): (1, root, root),  # the host itself is never a member
    }
    assert _tree_members(table, root, root_alive=False) == {4001, 4002}
    assert _tree_members(table, root) == {4001, 4002, 4003}


async def test_hardlink_escape_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-private")
    outside.write_text("private", encoding="utf8")
    try:
        (tmp_path / "linked").hardlink_to(outside)
        backend = PiBackend()
        assert (
            await backend.run(
                context(tmp_path, [calls("write", path="linked", content="bad"), "no"]),
                {},
                pack("write"),
            )
            == "no"
        )
        assert outside.read_text() == "private"
    finally:
        outside.unlink()


async def test_swallowed_compiler_money_denial_stops_same_batch_write(tmp_path: Path) -> None:
    from architect.runtime.wallet import compilation_wallet

    batch = json.dumps(
        {
            "tool_calls": [
                {"id": "compile", "name": "compile_agent", "arguments": {"request": "feature"}},
                {"id": "write", "name": "write", "arguments": {"path": "denied", "content": "bad"}},
            ]
        }
    )
    seen = []

    async def compiler(args):
        seen.append(args)
        child = context(tmp_path, ["child"])
        child.adapter = FakeAdapter(
            responses=[response("child").model_copy(update={"cost_usd": 0.4})]
        )
        try:
            await child.call("nested")
        except BudgetExceeded:
            return {"accepted": False, "failures": ["budget_exhausted"]}
        raise AssertionError("The shared money wallet must refuse the child")

    backend = PiBackend(PiConfig(allowed_tools=("compile_agent", "write")), compiler_tool=compiler)
    ctx = context(tmp_path, [], is_evaluation=False)
    ctx.adapter = FakeAdapter(responses=[response(batch).model_copy(update={"cost_usd": 0.4})])
    with compilation_wallet(Budget(cost_usd=0.7, compute_s=60)) as wallet:
        with pytest.raises(BudgetExceeded, match="[Cc]ompilation budget"):
            await backend.run(ctx, {}, pack("compile_agent", "write"))
        assert wallet.exhausted
    assert seen == [{"request": "feature"}]
    assert not (tmp_path / "denied").exists()
    assert backend.last_evidence.tool_calls == 1


async def test_normal_pi_has_no_token_quota_and_reconciles_usage(tmp_path: Path) -> None:
    from architect.models import UsageQuote

    class Metered(FakeAdapter):
        def quote(self, request):
            assert request.max_output_tokens is None
            return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.01)

    adapter = Metered(
        responses=[
            ModelResponse(
                text="done", model="fake", tokens_in=2000000, tokens_out=1000000, cost_usd=0.01
            )
        ]
    )
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    backend = PiBackend()
    assert await backend.run(ctx, {}, pack()) == "done"
    assert ctx.tokens_in == 2000000 and ctx.tokens_out == 1000000
    assert ctx.cost_usd == pytest.approx(0.01)


@pytest.mark.parametrize("tool", ["read", "write", "edit"])
@pytest.mark.parametrize(
    "target", [".architect/signing.key", "state/key.bin", "private/labels.json", "fixture.json"]
)
async def test_control_plane_paths_are_not_file_tool_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, target: str
) -> None:
    protected = tmp_path / target
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("OPERATOR_PRIVATE_SENTINEL", encoding="utf8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", "state/key.bin")
    config = PiConfig(denied_paths=(tmp_path / "private", tmp_path / "fixture.json"))
    arguments = {"path": target}
    if tool == "write":
        arguments["content"] = "attacker controlled"
    elif tool == "edit":
        arguments["edits"] = [{"oldText": "OPERATOR", "newText": "attacker"}]

    class Inspect(FakeAdapter):
        async def complete(self, request):
            if self.calls == 1:
                assert "Tool denied by Architect policy" in request.prompt
                assert "OPERATOR_PRIVATE_SENTINEL" not in request.prompt
            return await super().complete(request)

    adapter = Inspect(responses=[response(calls(tool, **arguments)), response("denied")])
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    assert await PiBackend(config).run(ctx, {}, pack(tool)) == "denied"
    assert protected.read_text() == "OPERATOR_PRIVATE_SENTINEL"


async def test_node_extension_independently_denies_private_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / ".architect" / "signing.key"
    private.parent.mkdir()
    private.write_text("operator-owned", encoding="utf8")
    # Prove the shipped TS extension enforces its own bound private roots, even if
    # the parent-side file permission callback accidentally grants this attempt.
    monkeypatch.setattr("architect.backends.pi._granted_path", lambda *args: True)
    ctx = context(
        tmp_path, [calls("write", path=".architect/signing.key", content="attacker"), "no"]
    )
    assert await PiBackend().run(ctx, {}, pack("write")) == "no"
    assert private.read_text() == "operator-owned"


async def test_private_root_ancestor_missing_file_is_denied(tmp_path: Path) -> None:
    config = PiConfig(denied_paths=(tmp_path / "future-control-state",))
    ctx = context(
        tmp_path, [calls("write", path="future-control-state/new/key", content="attacker"), "no"]
    )
    assert await PiBackend(config).run(ctx, {}, pack("write")) == "no"
    assert not (tmp_path / "future-control-state").exists()


async def test_windows_private_state_alias_cannot_create_signer(tmp_path: Path) -> None:
    # On POSIX this spelling is an ordinary unrelated directory, so use an exact
    # custom protected root there; the actual Windows run probes Win32 aliasing.
    target = ".architect./signing.key" if os.name == "nt" else "private/signing.key"
    config = PiConfig(denied_paths=(tmp_path / "private",))
    assert (
        await PiBackend(config).run(
            context(
                tmp_path, [calls("write", path=target, content="known signing material"), "no"]
            ),
            {},
            pack("write"),
        )
        == "no"
    )
    assert not (tmp_path / ".architect").exists()
    assert not (tmp_path / "private").exists()


async def test_explicit_fixture_repo_inside_host_state_is_not_blanket_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / ".architect" / "evaluation-fixtures" / "case1"
    repo.mkdir(parents=True)
    ctx = context(repo, [calls("write", path="answer.txt", content="fixture result"), "done"])
    assert await PiBackend().run(ctx, {}, pack("write")) == "done"
    assert (repo / "answer.txt").read_text() == "fixture result"


async def test_actual_active_package_source_cannot_be_read(tmp_path: Path) -> None:
    from architect.backends.pi import _PACKAGE

    class Inspect(FakeAdapter):
        async def complete(self, request):
            if self.calls == 1:
                assert "Tool denied by Architect policy" in request.prompt
                assert "import hmac" not in request.prompt
            return await super().complete(request)

    adapter = Inspect(responses=[response(calls("read", path="_authority.py")), response("denied")])
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=60), repository=_PACKAGE)
    assert await PiBackend().run(ctx, {}, pack("read")) == "denied"


@pytest.mark.parametrize("tool", ["read", "write", "edit"])
@pytest.mark.parametrize("protected_kind", ["package", "prefix", "base_prefix"])
async def test_active_host_sources_and_interpreters_are_not_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, protected_kind: str
) -> None:
    import sys

    live_root = tmp_path / "active-host"
    live_root.mkdir()
    private = live_root / "enforcement.py"
    private.write_text("HOST_ENFORCEMENT_SENTINEL", encoding="utf8")
    if protected_kind == "package":
        monkeypatch.setattr("architect.backends.pi._PACKAGE", live_root)
    else:
        monkeypatch.setattr(sys, protected_kind, str(live_root))
    arguments = {"path": "active-host/enforcement.py"}
    if tool == "write":
        arguments["content"] = "attacker"
    elif tool == "edit":
        arguments["edits"] = [{"oldText": "HOST", "newText": "ATTACKER"}]
    ctx = context(tmp_path, [calls(tool, **arguments), "denied"])
    assert await PiBackend().run(ctx, {}, pack(tool)) == "denied"
    assert private.read_text() == "HOST_ENFORCEMENT_SENTINEL"


async def test_node_resolution_ignores_repository_path_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.backends.pi import _resolve_node

    shadow = tmp_path / ("node.exe" if os.name == "nt" else "node")
    shadow.write_text("not an executable; must never launch", encoding="utf8")
    shadow.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    resolved = _resolve_node("node", tmp_path)
    assert resolved.is_absolute() and not resolved.is_relative_to(tmp_path)
    assert await PiBackend().run(context(tmp_path, ["done"]), {}, pack()) == "done"
    with pytest.raises(PiError, match="host Node executable"):
        await PiBackend(PiConfig(node_executable=str(shadow))).run(
            context(tmp_path, ["must not call"]), {}, pack()
        )


async def test_sibling_money_denial_while_waiting_for_tool_rpc_blocks_write(tmp_path: Path) -> None:
    import shlex

    from architect.models import UsageQuote
    from architect.runtime.wallet import compilation_wallet

    program = 'require("node:fs").writeFileSync("ready", "ready");setTimeout(()=>{},1500);'
    batch = json.dumps(
        {
            "tool_calls": [
                {
                    "id": "slow",
                    "name": "bash",
                    "arguments": {"command": "node -e " + shlex.quote(program)},
                },
                {
                    "id": "late",
                    "name": "write",
                    "arguments": {"path": "must-not-exist", "content": "bad"},
                },
            ]
        }
    )
    backend = PiBackend(PiConfig(allowed_tools=("bash", "write"), allow_shell=True))
    ctx = context(tmp_path, [batch, "must not reach a second model call"])
    evidence = []

    async def execute():
        try:
            return await backend.run(ctx, {}, pack("bash", "write"))
        finally:
            evidence.append(backend.last_evidence)

    with compilation_wallet(Budget(cost_usd=1, compute_s=60)) as wallet:
        running = asyncio.create_task(execute())
        try:
            async with asyncio.timeout(30):
                while not (tmp_path / "ready").exists():
                    if running.done():
                        await running
                        raise AssertionError("Pi stopped before the first tool executed")
                    await asyncio.sleep(0.01)
            # Real bash is running. Python has already passed the top-of-loop check
            # and is awaiting the next RPC. A separate guarded consumer is denied.
            with pytest.raises(BudgetExceeded):
                wallet.reserve(UsageQuote(tokens_in=0, tokens_out=0, cost_usd=2))
            with pytest.raises(BudgetExceeded):
                await running
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    assert not (tmp_path / "must-not-exist").exists()
    assert evidence[0].tool_calls == 1
    assert ctx.adapter.calls == 1


# These negatives probe the Python permission/protocol boundary directly. The
# earlier integration tests still run the actual pinned SDK and real tools.
@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "~",
        "@file",
        "bad\nname",
        "bad\u00a0name",
        "file:stream",
        "sub/../escape",
        ".architect/private",
    ],
)
def test_permission_rejects_ambiguous_private_or_invalid_paths(tmp_path: Path, raw: object) -> None:
    from architect.backends.pi import _granted_path

    assert not _granted_path(tmp_path, raw, "write", 32, ())


def test_permission_rejects_missing_large_directory_and_resolution_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.backends.pi import _granted_path

    (tmp_path / "directory").mkdir()
    (tmp_path / "large").write_text("x" * 33, encoding="utf8")
    assert not _granted_path(tmp_path, "directory", "read", 32, ())
    assert not _granted_path(tmp_path, "missing", "read", 32, ())
    assert not _granted_path(tmp_path, "large", "edit", 32, ())
    assert not _granted_path(tmp_path, str(tmp_path), "write", 32, ())
    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_symlink", lambda path: path.name == "large")
        assert not _granted_path(tmp_path, "large", "write", 32, ())
    with monkeypatch.context() as patch:

        def inaccessible(path, *args, **kwargs):
            raise OSError("synthetic inaccessible path")

        patch.setattr(Path, "resolve", inaccessible)
        assert not _granted_path(tmp_path, "unreadable", "write", 32, ())


@pytest.mark.parametrize("exhausted,remaining", [(True, 10), (False, 0)])
def test_backend_budget_defense_checks_wallet_even_if_context_gate_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exhausted: bool, remaining: int
) -> None:
    from types import SimpleNamespace

    from architect.backends.pi import _check_parent

    ctx = context(tmp_path, [])
    checked = []
    monkeypatch.setattr(ctx, "_check", lambda: checked.append(True))
    monkeypatch.setattr(
        "architect.backends.pi.active_wallets",
        lambda: (SimpleNamespace(exhausted=exhausted, remaining_s=remaining),),
    )
    with pytest.raises(BudgetExceeded, match="Parent compilation budget"):
        _check_parent(ctx)
    assert checked == [True]


@pytest.mark.parametrize("tools", [("read", "read"), ("unapproved",)])
def test_invalid_tool_configuration_is_rejected(tools: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="Unsupported or duplicate"):
        PiConfig(allowed_tools=tools)


@pytest.mark.parametrize("requested", ["write", ["write", 5], None])
async def test_invalid_bound_tool_pack_fails_before_spawn(
    tmp_path: Path, requested: object
) -> None:
    bound = pack()
    bound["tools"] = requested
    ctx = context(tmp_path, [])
    with pytest.raises(PiError, match="Invalid bound Pi tools"):
        await PiBackend().run(ctx, {}, bound)
    assert ctx.adapter.calls == 0


@pytest.mark.parametrize("option", [0, -1, "4096", True])
async def test_invalid_provider_option_fails_before_spawn(tmp_path: Path, option: object) -> None:
    bound = pack()
    bound["max_output_tokens"] = option
    with pytest.raises(PiError, match="provider output option"):
        await PiBackend().run(context(tmp_path, []), {}, bound)


async def test_missing_dependency_and_file_repository_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr("architect.backends.pi._NODE", tmp_path / "uninstalled")
        with pytest.raises(PiError, match="dependencies missing"):
            await PiBackend().run(context(tmp_path, []), {}, pack())
    not_repo = tmp_path / "file"
    not_repo.write_text("ordinary file", encoding="utf8")
    with pytest.raises(PiError, match="must be a directory"):
        await PiBackend().run(context(not_repo, []), {}, pack())


class ProtocolProcess:
    """An inert IPC peer for malformed/forged-message tests, never a coding loop."""

    def __init__(self, messages, *, stdout_eof=True, stderr=b"", stderr_eof=False):
        self.pid = 987654321
        self.returncode = None
        self.sent = []
        self.killed = False
        self.stdin = self
        self.stdout = asyncio.StreamReader(limit=2 * 1048576)
        self.stderr = asyncio.StreamReader(limit=2 * 1048576)
        for message in messages:
            self.stdout.feed_data(
                message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode()
            )
        if stdout_eof:
            self.stdout.feed_eof()
        if stderr:
            self.stderr.feed_data(stderr)
        if stderr_eof:
            self.stderr.feed_eof()

    def write(self, data):
        self.sent.append(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        pass

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def install_protocol_peer(monkeypatch: pytest.MonkeyPatch, peer: ProtocolProcess) -> None:
    async def spawn(*args, **kwargs):
        assert Path(args[0]).is_absolute()
        return peer

    monkeypatch.setattr("architect.backends.pi.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("architect.backends.pi._windows_job", lambda pid: None)
    # This mock peer must never send OS signals on POSIX test hosts either.
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pid, sig: peer.kill())


_READY = {
    "kind": "ready",
    "extension_loaded": True,
    "versions": {name: "0.73.1" for name in ("pi-ai", "pi-agent-core", "pi-coding-agent")},
}


@pytest.mark.parametrize(
    "messages,error",
    [
        (
            [{"kind": "ready", "extension_loaded": False, "versions": _READY["versions"]}],
            "version or extension",
        ),
        ([{**_READY, "versions": {"pi-ai": "untrusted"}}], "version or extension"),
        ([{"kind": "done", "text": "forged"}], "Invalid Pi completion"),
        ([_READY, {"kind": "done", "text": 42}], "Invalid Pi completion"),
        ([{"kind": "failed"}], "execution failed"),
        ([b"not json\n"], "Invalid Pi protocol"),
        ([[]], "Invalid Pi message"),
        ([], "Invalid Pi protocol"),
        ([{"kind": "tool", "id": 0}], "request identity"),
        ([{"kind": "tool", "id": True}], "request identity"),
        ([{"kind": "untrusted-capability", "id": 1}], "Unknown Pi request"),
        (
            [
                {"kind": "tool", "id": 1, "name": "unknown", "arguments": {}},
                {"kind": "tool", "id": 1},
            ],
            "request identity",
        ),
    ],
)
async def test_malformed_or_forged_protocol_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, messages: list, error: str
) -> None:
    peer = ProtocolProcess(messages)
    install_protocol_peer(monkeypatch, peer)
    ctx = context(tmp_path, [])
    with pytest.raises(PiError, match=error):
        await PiBackend().run(ctx, {}, pack())
    assert ctx.adapter.calls == 0
    assert peer.returncode is not None
    assert not any(item.get("value") is True for item in peer.sent)


@pytest.mark.parametrize("args", [None, {"request": 12}, {"request": "feature", "approved": True}])
async def test_forged_compiler_arguments_never_reach_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: object
) -> None:
    peer = ProtocolProcess([_READY, {"kind": "compile", "id": 1, "arguments": args}])
    install_protocol_peer(monkeypatch, peer)
    seen = []

    async def callback(value):
        seen.append(value)
        return "must not execute"

    backend = PiBackend(PiConfig(allowed_tools=("compile_agent",)), compiler_tool=callback)
    with pytest.raises(PiError, match="arguments denied"):
        await backend.run(context(tmp_path, [], is_evaluation=False), {}, pack("compile_agent"))
    assert seen == []
    assert len(peer.sent) == 1  # init only; no capability response.


async def test_forged_disabled_compiler_rpc_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolProcess([_READY, {"kind": "compile", "id": 1, "arguments": {"request": "x"}}])
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="Compiler capability denied"):
        await PiBackend().run(context(tmp_path, []), {}, pack("compile_agent"))
    assert len(peer.sent) == 1


async def test_parent_tool_limit_cannot_be_bypassed_by_forged_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolProcess(
        [
            _READY,
            {"kind": "tool", "id": 1, "name": "write", "arguments": {"path": "first"}},
            {"kind": "tool", "id": 2, "name": "write", "arguments": {"path": "second"}},
        ]
    )
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="tool limit"):
        await PiBackend(PiConfig(max_turns=1)).run(context(tmp_path, []), {}, pack("write"))
    assert [item for item in peer.sent if item.get("value") is True] == [{"id": 1, "value": True}]
    assert not (tmp_path / "first").exists()  # IPC peer cannot execute tools.
    assert not (tmp_path / "second").exists()


@pytest.mark.parametrize("mode", ["stdout", "stderr", "init", "early-exit"])
async def test_protocol_output_limits_and_early_exit_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    limit = 8192
    peer = ProtocolProcess(
        [b"x" * (limit + 1) + b"\n"] if mode == "stdout" else [],
        stdout_eof=mode != "stderr" and mode != "early-exit",
        stderr=b"x" * (limit + 1) if mode == "stderr" else b"",
        stderr_eof=mode == "early-exit",
    )
    install_protocol_peer(monkeypatch, peer)
    task = "x" * (limit + 1) if mode == "init" else {}
    with pytest.raises(PiError):
        await PiBackend(PiConfig(max_protocol_bytes=limit)).run(context(tmp_path, []), task, pack())
    assert peer.returncode is not None


async def test_node_spawn_oserror_is_fixed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_spawn(*args, **kwargs):
        raise OSError("synthetic unavailable executable")

    monkeypatch.setattr("architect.backends.pi.asyncio.create_subprocess_exec", fail_spawn)
    with pytest.raises(PiError, match="Cannot start Pi Node process"):
        await PiBackend().run(context(tmp_path, []), {}, pack())
