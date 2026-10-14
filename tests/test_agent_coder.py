"""Phase 12 coder subagent: Pi approvals and live events (offline, no live model).

The protocol tests drive ``PiBackend`` against the inert IPC peer from ``test_pi``; the
end-to-end tests run the real pinned Pi SDK under Node with a scripted parent model.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from session_support import fake_profile
from test_pi import _READY, ProtocolProcess, install_protocol_peer

from architect.agent.coder import approval_detail, coder_config, run_coder
from architect.agent.events import Approval, RecordingUI, SubagentActivity
from architect.agent.tools import ToolContext, default_tools
from architect.backends import pi as pi_module
from architect.backends.pi import PiBackend, PiError, _event
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.models.types import UsageQuote
from architect.profiles import build_services, load_profile
from architect.runtime import RunContext
from architect.spec import Budget


def response(text: str) -> ModelResponse:
    return ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=10, cost_usd=0)


def calls(name: str, **args: object) -> str:
    return json.dumps({"tool_calls": [{"id": "call1", "name": name, "arguments": args}]})


def context(repo: Path, texts: list[str]) -> RunContext:
    return RunContext(
        FakeAdapter(responses=[response(text) for text in texts]),
        Budget(cost_usd=1, compute_s=60),
        repository=repo,
    )


def pack(*tools: str) -> dict[str, Any]:
    return {"tools": list(tools), "prompt_fragment": "Do it.", "subagent_rules": []}


class Approvals:
    """A recording approver with a fixed answer (or an exception)."""

    def __init__(self, answer: object = True) -> None:
        self.answer = answer
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool: str, arguments: dict[str, Any]) -> bool:
        self.seen.append((tool, arguments))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer  # type: ignore[return-value]


def tool_request(request_id: int, name: str, **args: object) -> dict[str, Any]:
    return {"kind": "tool", "id": request_id, "name": name, "arguments": args}


def event(**fields: object) -> dict[str, Any]:
    return {"kind": "event", **fields}


async def run_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[Any],
    backend: PiBackend,
    tools: tuple[str, ...] = ("write",),
) -> ProtocolProcess:
    """Run the backend against scripted bridge messages; the stream then ends."""
    peer = ProtocolProcess(messages)
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="Invalid Pi protocol"):  # end of stream
        await backend.run(context(tmp_path, []), {}, pack(*tools))
    return peer


def replies(peer: ProtocolProcess) -> list[dict[str, Any]]:
    return [item for item in peer.sent if item.get("kind") != "init"]


# -- the approver hook (inert IPC peer) -------------------------------------------


@pytest.mark.parametrize("answer", [True, False, "yes", 1, None, RuntimeError("ui broke")])
async def test_approver_decides_after_guards_and_only_true_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    approver = Approvals(answer)
    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [_READY, tool_request(1, "write", path="a.txt", content="x")],
        PiBackend(approver=approver),
    )
    assert approver.seen == [("write", {"path": "a.txt", "content": "x"})]
    assert replies(peer) == [{"id": 1, "value": answer is True}]


async def test_approver_cannot_widen_a_guard_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approver = Approvals(True)
    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [
            _READY,
            tool_request(1, "write", path="../escape.txt", content="x"),
            tool_request(2, "write", path=".architect/signing.key", content="x"),
            tool_request(3, "edit", path="missing.txt", edits=[]),
            tool_request(4, "bash", command="echo hi"),  # shell is not granted
            tool_request(5, "unknown", path="a.txt"),
        ],
        PiBackend(approver=approver),
        tools=("write", "edit", "bash"),
    )
    assert approver.seen == []
    assert [item["value"] for item in replies(peer)] == [False] * 5


async def test_approver_sees_bash_and_edit_but_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("old", encoding="utf-8")
    approver = Approvals(False)
    config = pi_module.PiConfig(allowed_tools=("read", "edit", "bash"), allow_shell=True)
    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [
            _READY,
            tool_request(1, "read", path="a.txt"),
            tool_request(2, "edit", path="a.txt", edits=[{"oldText": "old", "newText": "new"}]),
            tool_request(3, "bash", command="echo hi"),
        ],
        PiBackend(config, approver=approver),
        tools=("read", "edit", "bash"),
    )
    assert [tool for tool, _ in approver.seen] == ["edit", "bash"]
    assert [item["value"] for item in replies(peer)] == [True, False, False]


async def test_approver_gets_a_private_copy_of_the_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def mutate(tool: str, arguments: dict[str, Any]) -> bool:
        arguments["path"] = "../escape.txt"
        return True

    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [_READY, tool_request(1, "write", path="a.txt", content="x")],
        PiBackend(approver=mutate),
    )
    assert replies(peer) == [{"id": 1, "value": True}]


# -- the event stream (inert IPC peer) ---------------------------------------------


async def test_events_are_forwarded_sanitized_and_never_replied_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [
            event(event="tool_start", tool="write", summary="early.txt"),  # before ready
            _READY,
            event(event="tool_start", tool="write", summary="a.txt", id=99, extra="dropped"),
            tool_request(1, "write", path="a.txt", content="x"),
            event(event="tool_end", tool="write", ok=True, summary="1 bytes"),
            event(event="text", text="Now\x1b[31m the ‮test\n"),
            event(event="tool_end", tool="write", ok="yes", summary="bad ok"),
            event(event="tool_start", tool="Bad Name", summary="x"),
            event(event="tool_start", tool="bash", summary=["not", "text"]),
            event(event="text", text="   "),
            event(event="telemetry", data={"secret": 1}),
            event(),
            event(event="tool_start", tool="bash", summary="x" * 5000),
            tool_request(2, "write", path="b.txt", content="y"),
        ],
        PiBackend(on_event=seen.append),
    )
    assert peer.sent[0]["kind"] == "init" and peer.sent[0]["events"] is True
    # Only the two requests are answered, in order; events carry no identity.
    assert replies(peer) == [{"id": 1, "value": True}, {"id": 2, "value": True}]
    assert seen == [
        {"event": "tool_start", "tool": "write", "summary": "a.txt"},
        {"event": "tool_end", "tool": "write", "summary": "1 bytes", "ok": True},
        {"event": "text", "text": "Now [31m the  test"},
        {"event": "tool_start", "tool": "bash", "summary": "x" * 200},
    ]


async def test_events_are_capped_and_a_failing_sink_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_module, "_MAX_EVENTS", 2)
    seen: list[dict[str, Any]] = []

    def sink(item: dict[str, Any]) -> None:
        seen.append(item)
        raise RuntimeError("renderer failed")

    start = event(event="tool_start", tool="read", summary="a")
    peer = await run_peer(
        tmp_path,
        monkeypatch,
        [_READY, start, start, start, tool_request(1, "write", path="a.txt", content="x")],
        PiBackend(on_event=sink),
    )
    assert len(seen) == 2
    assert replies(peer) == [{"id": 1, "value": True}]


async def test_without_hooks_the_protocol_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolProcess([_READY, event(event="tool_start", tool="write", summary="a")])
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError, match="request identity"):
        await PiBackend().run(context(tmp_path, []), {}, pack("write"))
    assert "events" not in peer.sent[0]
    assert len(peer.sent) == 1


def test_event_shapes_are_bounded() -> None:
    assert _event({"event": "tool_start", "tool": "write"}) == {
        "event": "tool_start", "tool": "write", "summary": ""
    }
    assert _event({"event": "tool_start", "tool": "x" * 65, "summary": ""}) is None
    assert _event({"event": "text", "text": "y" * 10_000}) == {"event": "text", "text": "y" * 400}
    assert _event({"event": "tool_end", "tool": "edit", "summary": "s"}) is None


# -- the real pinned Pi SDK: events and approvals end to end ------------------------


async def test_real_bridge_streams_tool_events(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    approver = Approvals(True)
    backend = PiBackend(approver=approver, on_event=seen.append)
    ctx = context(
        tmp_path,
        [
            calls("write", path="answer.py", content="answer = 40\n"),
            calls("edit", path="answer.py", edits=[{"oldText": "40", "newText": "42"}]),
            calls("read", path="answer.py"),
            calls("write", path="../escape.py", content="no"),
            "done",
        ],
    )
    assert await backend.run(ctx, "go", pack("write", "edit", "read")) == "done"
    assert (tmp_path / "answer.py").read_text(encoding="utf-8") == "answer = 42\n"
    assert [tool for tool, _ in approver.seen] == ["write", "edit"]
    tools = [(item["event"], item.get("tool"), item.get("ok")) for item in seen]
    assert tools == [
        ("tool_start", "write", None), ("tool_end", "write", True),
        ("tool_start", "edit", None), ("tool_end", "edit", True),
        ("tool_start", "read", None), ("tool_end", "read", True),
        ("tool_start", "write", None), ("tool_end", "write", False),
    ]
    summaries = [item["summary"] for item in seen]
    assert summaries[:4] == ["answer.py", "12 bytes", "answer.py", "+1 -1 lines"]
    assert summaries[-1] == "Tool denied by Architect policy"


async def test_real_bridge_approver_denial_blocks_the_write(tmp_path: Path) -> None:
    approver = Approvals(False)
    backend = PiBackend(approver=approver)
    ctx = context(tmp_path, [calls("write", path="no.txt", content="x"), "denied"])
    assert await backend.run(ctx, {}, pack("write")) == "denied"
    assert not (tmp_path / "no.txt").exists()
    assert approver.seen == [("write", {"path": "no.txt", "content": "x"})]


# -- the coder subagent through the runtime ------------------------------------------


class Scripted:
    """The parent model: replies in order at a tiny fixed price."""

    def __init__(self, *replies: str, model: str = "fake") -> None:
        self.model = model
        self.replies = list(replies)
        self.prompts: list[str] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=1, tokens_out=1, cost_usd=0.001)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        text = self.replies.pop(0) if self.replies else '{"text": "done"}'
        return ModelResponse(text=text, model=self.model, tokens_in=1, tokens_out=1,
                             cost_usd=0.001)


class CountingUI(RecordingUI):
    def __init__(self, approvals: list[Approval]) -> None:
        super().__init__(approvals=approvals)
        self.asked: list[tuple[str, str]] = []

    async def approve(self, action: str, detail: str) -> Approval:
        self.asked.append((action, detail))
        return await super().approve(action, detail)


@pytest.fixture
def services(tmp_path: Path):  # type: ignore[no-untyped-def]
    profile = load_profile(fake_profile(tmp_path))
    built = build_services(profile, profile.workspace)
    yield built
    built.close()


def coder_context(  # type: ignore[no-untyped-def]
    services, tmp_path: Path, replies: list[str], approvals: list[Approval],
    permission: str = "ask",
) -> tuple[ToolContext, CountingUI, Scripted]:
    adapter = Scripted(*replies)
    wired = dataclasses.replace(services, adapter_factory=lambda model: adapter)
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    ui = CountingUI(approvals)
    ctx = ToolContext(services=wired, cwd=cwd, ui=ui, model="fake",
                      permission=permission)  # type: ignore[arg-type]
    ctx.extras["coder"] = run_coder
    return ctx, ui, adapter


def activity(ui: RecordingUI) -> list[str]:
    return [item.text for item in ui.events if isinstance(item, SubagentActivity)]


WRITE = calls("write", path="hello.txt", content="hello\n")


async def test_ask_mode_no_leaves_the_file_unwritten(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(services, tmp_path, [WRITE, '{"text": "was denied"}'], ["no"])
    result = await run_coder("write hello.txt", ctx)
    assert not (ctx.cwd / "hello.txt").exists()
    assert isinstance(result, dict)
    assert result["ok"] is True and result["summary"] == "was denied"
    assert result["changed_files"] == [] and result["denied"] == 1 and result["tool_calls"] == 1
    assert ui.asked == [("write", "hello.txt (6 bytes, 1 lines)\n+ hello")]
    assert activity(ui) == ["write hello.txt", "write hello.txt", "write hello.txt: denied"]


async def test_ask_mode_yes_writes_through_the_code_tool(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(services, tmp_path, [WRITE, '{"text": "wrote it"}'], ["yes"])
    code = next(tool for tool in default_tools() if tool.name == "code")
    result = await code.run({"task": "write hello.txt"}, ctx)
    assert (ctx.cwd / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert isinstance(result, dict)
    assert result["changed_files"] == ["hello.txt"] and result["denied"] == 0
    assert result["summary"] == "wrote it" and result["tool_calls"] == 1
    assert isinstance(result["cost_usd"], float) and result["cost_usd"] > 0
    assert "write hello.txt: 6 bytes" in activity(ui)
    assert ctx.allowed == set()


async def test_session_answer_suppresses_the_next_prompt(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(
        services, tmp_path,
        [WRITE, calls("write", path="two.txt", content="2"), '{"text": "both"}',
         calls("write", path="three.txt", content="3"), '{"text": "third"}'],
        ["session"],
    )
    result = await run_coder("write two files", ctx)
    assert isinstance(result, dict) and result["changed_files"] == ["hello.txt", "two.txt"]
    assert len(ui.asked) == 1 and ctx.allowed == {"write"}
    # The session grant carries over to the next coder run in the same session.
    await run_coder("one more", ctx)
    assert (ctx.cwd / "three.txt").read_text(encoding="utf-8") == "3"
    assert len(ui.asked) == 1


async def test_auto_edit_writes_without_asking_but_shell_asks(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(
        services, tmp_path,
        [WRITE, calls("bash", command="printf 'x' > shell.txt"), '{"text": "ok"}'],
        ["no"], permission="auto-edit",
    )
    result = await run_coder("write and run", ctx)
    assert (ctx.cwd / "hello.txt").exists() and not (ctx.cwd / "shell.txt").exists()
    assert ui.asked == [("bash", "printf 'x' > shell.txt")]
    assert isinstance(result, dict) and result["denied"] == 1 and result["tool_calls"] == 2


async def test_full_auto_without_operator_shell_asks_for_shell(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(
        services, tmp_path,
        [WRITE, calls("bash", command="printf 'x' > shell.txt"), '{"text": "ok"}'],
        ["yes"], permission="full-auto",
    )
    result = await run_coder("write and run", ctx)
    assert (ctx.cwd / "hello.txt").exists()
    assert (ctx.cwd / "shell.txt").read_text(encoding="utf-8") == "x"
    assert [action for action, _ in ui.asked] == ["bash"]
    assert isinstance(result, dict)
    assert result["changed_files"] == ["hello.txt", "shell.txt"]  # bash seen by the snapshot


async def test_control_plane_paths_are_denied_before_any_prompt(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, ui, _ = coder_context(
        services, tmp_path,
        [calls("write", path=".architect/signing.key", content="forged"), '{"text": "no"}'],
        ["yes"], permission="full-auto",
    )
    result = await run_coder("forge a key", ctx)
    assert not (ctx.cwd / ".architect").exists()
    assert ui.asked == []
    assert isinstance(result, dict) and result["denied"] == 1


async def test_shell_can_be_withheld_and_denials_kept(services, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ctx, _, _ = coder_context(services, tmp_path, [], [])
    config = coder_config(ctx)
    assert config.allowed_tools == ("read", "write", "edit", "bash") and config.allow_shell
    assert set(services.profile.pi.denied_paths) <= set(config.denied_paths)
    ctx.extras["coder_shell"] = False
    config = coder_config(ctx)
    assert config.allowed_tools == ("read", "write", "edit") and not config.allow_shell


def test_approval_detail_is_bounded_and_printable() -> None:
    detail = approval_detail("edit", {"path": "a.py", "edits": [
        {"oldText": "x = 1", "newText": "x = 2\x1b[2J"}]})
    assert detail == "a.py (1 edit)\n- x = 1\n+ x = 2 [2J"
    long = approval_detail("write", {"path": "b.txt", "content": "line\n" * 5000})
    assert len(long) <= 1500 and "4988 more lines" in long
    assert approval_detail("bash", {"command": "echo hi"}) == "echo hi"
