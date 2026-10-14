"""Refusals by the judges' relay (``architect.models.relay``): one code and one plain
sentence each, from the adapter to the CLI, build mode, the sign-in and the desktop bridge.
The relay's ledger out of reach is retried first, and stops a build once it is raised.

Offline. Every exchange goes through ``httpx.MockTransport``, with error bodies exactly as
deploy/relay/src/handler.ts writes them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from session_support import fake_profile
from test_agent_conductor import Scripted
from test_desktop_bridge import Wire, call, started
from test_desktop_bridge_more import answering, install
from test_nebius_adversarial import (
    Script,
    chunk,
    collect,
    nebius,
    reply,
    shown,
    sse,
    streamed,
    usage_chunk,
    waits,
)
from test_phase14_build import Build, write_spec

from architect.agent.app import AppOptions, boot
from architect.agent.build import _until_outage, make, set_override
from architect.agent.events import Notice
from architect.agent.repl import LineFrontend, Repl
from architect.agent.tools import Tool
from architect.compiler.forge_audit import AUDIT_ENV
from architect.desktop.bridge import Bridge
from architect.models import ModelRequest, relay
from architect.models.fake import FakeAdapter
from architect.models.relay import RelayRefusal, refusal
from architect.providers import judge
from architect.providers.catalog import find
from architect.providers.host import Host
from architect.providers.signin import Prompter, sign_in
from architect.session import ScriptedConsole
from architect.session.codes import CODES, code_for, explain

__all__ = ["waits"]  # the retry-wait recorder is a fixture of test_nebius_adversarial

RELAY = "https://relay.example/v1"
CODE = "rc_Ab3-dE6_gH9jK2mN5pQ8sT1vW4yZ7aB2"
# The relay's own words, which must never reach a person: only the code and its sentence do.
DAILY = {"error": {
    "message": "This access code has $0.0012 left today (daily cap $3.00); this call needs up "
               "to $0.0123 (Nemotron 3 Super, max_tokens 8192). Lower max_tokens, or wait for "
               "the reset at 2026-12-02T00:00:00.000Z.",
    "type": "relay_cap_exceeded", "code": "daily_cap", "remaining_usd": 0.0012,
    "needed_usd": 0.0123, "cap_usd": 3, "resets_at": "2026-12-02T00:00:00.000Z"}}
BODIES: dict[str, list[tuple[int, dict[str, Any]]]] = {
    "relay_daily_limit": [(402, DAILY)],
    "relay_total_limit": [(402, {"error": {
        "message": "This access code has $0.0001 left of its $10.00 total; this call needs ...",
        "type": "relay_cap_exceeded", "code": "total_cap", "remaining_usd": 0.0001,
        "needed_usd": 0.01, "cap_usd": 10}})],
    "relay_shared_limit": [
        (402, {"error": {"message": "This access code has $2.00 left, but the relay's shared "
                                    "budget cannot cover this call ...",
                         "type": "relay_cap_exceeded", "code": "global_cap",
                         "remaining_usd": 2, "needed_usd": 0.01}}),
        (402, {"error": {"message": "... keeping the rest of its shared budget ...",
                         "type": "relay_cap_exceeded", "code": "global_freeze",
                         "remaining_usd": 2, "needed_usd": 0.01}}),
        (503, {"error": {"message": "Token Factory reports the relay's account is out of "
                                    "funds. Your code was not charged.",
                         "type": "upstream_budget", "upstream_status": 402}}),
    ],
    "relay_unknown_code": [
        (401, {"error": {"message": "That access code is not valid.",
                         "type": "relay_unknown_code", "code": "unknown_code"}}),
        (401, {"error": {"message": 'Send your access code as "Authorization: Bearer rc_...".',
                         "type": "relay_unknown_code", "code": "missing_code"}}),
    ],
    "relay_code_revoked": [(401, {"error": {"message": "That access code was revoked.",
                                            "type": "relay_unknown_code",
                                            "code": "code_revoked"}})],
    "relay_code_paused": [(403, {"error": {"message": "This access code is paused: ...",
                                           "type": "relay_code_frozen", "code": "code_frozen"}})],
    "relay_off": [(503, {"error": {"message": "The relay is switched off by its operator.",
                                   "type": "relay_disabled", "code": "disabled"}})],
    # Sent with "Retry-After: 5" (ledgerDown in handler.ts); the only body with no code.
    "relay_ledger_down": [(503, {"error": {
        "message": "The relay's ledger cannot be reached right now; nothing was charged. "
                   "Retry shortly.",
        "type": "relay_ledger_unavailable"}})],
}
LEDGER_DOWN = BODIES["relay_ledger_down"][0][1]


def every_refusal() -> list[tuple[str, int, dict[str, Any]]]:
    return [(code, status, body) for code, rows in BODIES.items() for status, body in rows]


def ledger_down() -> httpx.Response:
    return httpx.Response(503, json=LEDGER_DOWN, headers={"Retry-After": "5"})


class Refusing(Scripted):
    """A conductor model behind a judge code that has used its day."""

    async def complete(self, request: ModelRequest) -> Any:
        raise RelayRefusal("relay_daily_limit")


def refuse_fake_calls(
    monkeypatch: pytest.MonkeyPatch, code: str = "relay_daily_limit",
) -> list[str]:
    """Every fake model call from here on is refused by the relay; the list it returns
    gets one entry (the code) for each such call."""
    calls: list[str] = []

    async def refused(self: FakeAdapter, request: ModelRequest) -> Any:
        calls.append(code)
        raise RelayRefusal(code)

    monkeypatch.setattr(FakeAdapter, "complete", refused)
    return calls


# -- one code and one sentence each ------------------------------------------------------


def test_each_refusal_is_one_code_with_one_plain_sentence() -> None:
    for code, status, body in every_refusal():
        assert refusal(status, json.dumps(body).encode()) == code
    assert set(BODIES) == relay.CODES
    daily, total = relay.TEXT["relay_daily_limit"], relay.TEXT["relay_total_limit"]
    assert "daily limit" in daily and "resets at 00:00 UTC" in daily
    assert "total limit" in total and "UTC" not in total
    assert "revoked" in relay.TEXT["relay_code_revoked"]
    assert "does not know this code" in relay.TEXT["relay_unknown_code"]
    assert "switched off" in relay.TEXT["relay_off"]
    down = relay.TEXT["relay_ledger_down"]
    assert "cannot reach its ledger" in down and "charged nothing" in down
    assert "Try again later" in down and "spare" in down
    assert relay.OUTAGES == {"relay_ledger_down"}  # the only refusal an adapter retries
    for code, sentence in relay.TEXT.items():
        assert code in CODES and explain(code) == sentence == judge.explain(code)
        assert sentence.isascii() and "\n" not in sentence and sentence.endswith(".")
        assert "provider" not in sentence.lower() and "signed out" not in sentence.lower()


def test_anything_else_is_not_a_relay_refusal() -> None:
    raw = json.dumps(DAILY).encode()
    assert refusal(200, raw) is None and refusal(429, raw) is None
    assert refusal(402, b"not json") is None and refusal(402, b"[]") is None
    assert refusal(402, b'{"error": "daily_cap"}') is None
    assert refusal(401, b'{"error": {"type": "invalid_request_error", "code": "x"}}') is None
    assert refusal(401, b'{"detail": "Invalid API key"}') is None  # Token Factory's own 401
    assert refusal(503, b'{"error": {"type": "overloaded_error"}}') is None
    assert refusal(402, b'{"error": {"type": "relay_cap_exceeded", "code": "new_cap"}}') is None
    assert refusal(402, raw + b" " * 70_000) is None  # no relay error body is this large
    limited = {"error": {"type": "relay_rate_limited", "code": "code_rpm"}}
    assert refusal(429, json.dumps(limited).encode()) is None  # retried, never a refusal


def test_since_says_whether_a_refusal_came_after_a_mark() -> None:
    start = relay.mark()
    assert relay.since(start) is None
    RelayRefusal("relay_total_limit")
    assert relay.since(start) == "relay_total_limit" and relay.mark() == start + 1
    assert relay.since(relay.mark()) is None
    with pytest.raises(ValueError):
        RelayRefusal("no_such_refusal")


# -- the adapter -------------------------------------------------------------------------


async def test_the_adapter_raises_each_refusal_at_once_and_never_retries_it(
    waits: list[float],
) -> None:
    for code, status, body in every_refusal():
        if code in relay.OUTAGES:
            continue  # retried first: the next test
        once = Script(httpx.Response(status, json=body), httpx.Response(200, json=reply("ok")))
        with pytest.raises(RelayRefusal) as caught:
            await nebius(once).complete(ModelRequest(prompt="hi"))
        assert caught.value.code == code and code_for(caught.value) == code
        assert str(caught.value) == relay.TEXT[code]
        stream = Script(httpx.Response(status, json=body), httpx.Response(200, json=reply("ok")))
        with pytest.raises(RelayRefusal) as streamed:
            await collect(nebius(stream), cap=None)
        assert streamed.value.code == code
        assert len(once.bodies) == len(stream.bodies) == 1
    assert waits == []  # the kill switch's 503 is not the busy 503 that is retried
    busy = Script(httpx.Response(503, json={"error": {"type": "overloaded_error"}}),
                  httpx.Response(200, json=reply("ok")))
    assert (await nebius(busy).complete(ModelRequest(prompt="hi"))).text == "ok"
    assert waits == [1.0]


async def test_the_ledger_outage_is_retried_and_named_once_the_retries_are_spent(
    waits: list[float],
) -> None:
    """The relay's ledger out of reach may pass within seconds, so the adapter waits out its
    Retry-After as it does for a busy server. Still out after the two retries, the call is
    refused with the outage's code and sentence, never a bare "HTTP 503"."""
    text = sse(chunk(finish="stop", content="ok"), usage_chunk(2))
    blip = Script(ledger_down(), httpx.Response(200, json=reply("ok")))
    assert (await nebius(blip).complete(ModelRequest(prompt="hi"))).text == "ok"
    streamed_blip = Script(ledger_down(), lambda: streamed(text))
    assert shown(await collect(nebius(streamed_blip), cap=None)) == "ok"
    assert waits == [5.0, 5.0]
    waits.clear()
    down = Script(*(ledger_down() for _ in range(3)), httpx.Response(200, json=reply("ok")))
    with pytest.raises(RelayRefusal) as caught:
        await nebius(down).complete(ModelRequest(prompt="hi"))
    assert caught.value.code == code_for(caught.value) == "relay_ledger_down"
    assert str(caught.value) == relay.TEXT["relay_ledger_down"]
    stream = Script(*(ledger_down() for _ in range(3)), lambda: streamed(text))
    with pytest.raises(RelayRefusal) as refused:
        await collect(nebius(stream), cap=None)
    assert refused.value.code == "relay_ledger_down"
    assert len(down.bodies) == len(stream.bodies) == 3 and len(set(down.bodies)) == 1
    assert waits == [5.0, 5.0, 5.0, 5.0]


# -- the sign-in -------------------------------------------------------------------------


def relay_answers(tmp_path: Path, status: int, body: dict[str, Any]) -> Host:
    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(f"{RELAY}/models"):
            return httpx.Response(status, json=body)
        raise httpx.ConnectError("refused", request=request)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Host(which=lambda name: None, env={"ARCHITECT_RELAY_URL": RELAY}, home=home,
                transport=httpx.MockTransport(handle), runner=lambda argv: 1)


@pytest.mark.parametrize(
    "code", ["relay_off", "relay_code_revoked", "relay_unknown_code", "relay_ledger_down"],
)
async def test_a_code_the_relay_refuses_at_sign_in_is_said_in_one_sentence(
    tmp_path: Path, code: str,
) -> None:
    status, body = BODIES[code][0]
    host = relay_answers(tmp_path, status, body)
    result = await judge.use_code(CODE, tmp_path / "ws", host=host)
    assert not result.ok and result.code == code
    said: list[str] = []

    async def ask(question: str, **_: Any) -> str | None:
        return CODE

    def say(text: str, level: str = "info") -> None:
        said.append(text)

    flow = Prompter(say, ask)
    signed = await sign_in(find("nebius"), tmp_path / "ws2", flow, host=host)  # type: ignore[arg-type]
    assert not signed.ok and signed.code == code and signed.message == relay.TEXT[code]
    assert relay.TEXT[code] in said and CODE not in " ".join(said)


async def test_the_desktop_sign_in_carries_the_sentence(tmp_path: Path) -> None:
    status, body = BODIES["relay_off"][0]
    wire = Wire()
    bridge = Bridge(wire.write)
    bridge.host = relay_answers(tmp_path, status, body)
    where = {"workspace": str(tmp_path / "w"), "cwd": str(tmp_path)}
    helper = asyncio.create_task(answering(bridge, [CODE]))
    await call(bridge, 50, "sign_in", provider="nebius", **where)  # 50: not an ask's id
    await helper
    result = wire.result(50)["result"]
    assert result["ok"] is False and result["code"] == "relay_off"
    assert result["message"] == relay.TEXT["relay_off"]
    assert CODE not in json.dumps([line for line in wire.lines if line.get("type") != "ask"])


# -- the CLI -----------------------------------------------------------------------------


async def test_a_refused_turn_says_why_in_one_sentence(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    console = ScriptedConsole(["hello"])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Refusing())
    repl = Repl(app, frontend)
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    out = console.transcript()
    assert relay.TEXT["relay_daily_limit"] in out
    assert "The turn failed" not in out and "signed out" not in out


async def test_a_refused_command_says_why_in_one_sentence(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    console = ScriptedConsole([])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Scripted())
    repl = Repl(app, frontend)

    async def refused(args: dict[str, Any], ctx: Any) -> Any:
        raise RelayRefusal("relay_total_limit")

    try:
        conductor = repl.conductor
        conductor.set_tools([*conductor.tools, Tool("refused", "x", {}, refused)])
        assert await repl.call_tool("refused", {}) is None
    finally:
        repl.app.close()
    out = console.transcript()
    assert relay.TEXT["relay_total_limit"] in out and "refused failed" not in out


# -- build mode --------------------------------------------------------------------------


async def test_a_refused_interview_stops_the_build_before_any_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Build(tmp_path, ["{}"], [])  # one scripted reply, so the call is quoted, then refused
    refuse_fake_calls(monkeypatch, "relay_off")
    try:
        result = await make("make an agent that extracts invoice fields from text", h.ctx)
    finally:
        h.close()
    assert isinstance(result, dict) and result["failures"] == ["relay_off"]
    assert result["next_step"] == relay.TEXT["relay_off"] and h.ui.questions == []


async def test_a_build_the_relay_refused_says_so_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_spec(tmp_path)
    h = Build(tmp_path, [], ["yes"])
    refuse_fake_calls(monkeypatch)
    try:
        result = await make("", h.ctx, spec_file="receipts.spec.json")
    finally:
        h.close()
    assert isinstance(result, dict) and result["licensed"] is False, result
    failures = result["failures"]
    assert isinstance(failures, list) and failures[0] == "relay_daily_limit"
    assert result["next_step"] == relay.TEXT["relay_daily_limit"]
    said = [e.text for e in h.ui.events if isinstance(e, Notice)]
    assert any(relay.TEXT["relay_daily_limit"] in text for text in said), said


async def test_a_relay_outage_stops_the_build_and_its_result_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call refused for the outage already waited out its retries, and every later call
    would too, so the build stops at the first one instead of going on through practice
    and the exam, as it does for a daily limit (the test above)."""
    write_spec(tmp_path)
    h = Build(tmp_path, [], ["yes"])
    calls = refuse_fake_calls(monkeypatch, "relay_ledger_down")
    try:
        result = await make("", h.ctx, spec_file="receipts.spec.json")
    finally:
        h.close()
    assert isinstance(result, dict) and result["licensed"] is False, result
    assert result["compiled"] is False and result["failures"] == ["relay_ledger_down"]
    assert result["next_step"] == relay.TEXT["relay_ledger_down"]
    assert result["spec_file"] == "receipts.spec.json" and calls == ["relay_ledger_down"]


async def test_the_cli_says_why_an_outage_stopped_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AUDIT_ENV, raising=False)
    fake_profile(tmp_path)
    write_spec(tmp_path, "spec.json")
    console = ScriptedConsole(["yes"])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Scripted())
    try:
        repl = Repl(app, frontend)
        set_override(repl.conductor.context, "engineer", "preset")
        calls = refuse_fake_calls(monkeypatch, "relay_ledger_down")
        await repl.handle("/make @spec.json")
    finally:
        app.close()
    out = console.transcript()
    assert f"Not compiled (relay_ledger_down). Next: {relay.TEXT['relay_ledger_down']}" in out
    assert calls == ["relay_ledger_down"]


async def test_a_build_the_user_stops_is_still_stopped_by_the_user() -> None:
    """Only the outage turns the compile's end into its refusal; any other refusal goes on,
    and the user's own stop stays a stop."""
    begun = asyncio.Event()

    async def compiling() -> Any:
        with contextlib.suppress(RelayRefusal):
            raise RelayRefusal("relay_daily_limit")  # one failed case; the compile goes on
        begun.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_until_outage(compiling()))
    await begun.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- the desktop's Try it ----------------------------------------------------------------


@pytest.mark.parametrize("code", ["relay_daily_limit", "relay_ledger_down"])
async def test_try_it_carries_the_sentence_when_the_relay_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await install(bridge, "alpha")
        refuse_fake_calls(monkeypatch, code)
        await call(bridge, 2, "run_agent", name="alpha", input={"text": "hi"})
        line = wire.result(2)
        assert line["error"] == {"code": code, "message": relay.TEXT[code]}
        notices = [e["text"] for e in wire.events("notice")]
        assert relay.TEXT[code] in notices
    finally:
        await bridge.close()
