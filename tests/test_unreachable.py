"""An address that does not answer (the judges' relay or Token Factory, with no network or
a wrong address): one code, ``provider_unreachable``, and one sentence that names the host,
from the adapter to the CLI, build mode and the desktop's Try it. Never "not logged in".

Offline: every exchange goes through ``httpx.MockTransport``, which raises the connection
errors httpx raises for a refused connection, a failed DNS lookup or a connect timeout.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from session_support import fake_profile
from test_agent_conductor import Scripted
from test_desktop_bridge import call, started
from test_desktop_bridge_more import install
from test_nebius_adversarial import HI, SUPER
from test_phase14_build import Build, write_spec

from architect.agent.app import AppOptions, boot
from architect.agent.build import make
from architect.agent.events import Notice
from architect.agent.repl import LineFrontend, Repl
from architect.models import ModelRequest, OpenAICompatibleAdapter, relay
from architect.models.fake import FakeAdapter
from architect.models.relay import Unreachable
from architect.session import ScriptedConsole
from architect.session.codes import CODES, code_for, explain

RELAY = "https://architect-relay.vercel.app/v1"
SAID = "Architect could not reach architect-relay.vercel.app; check the connection and try again."


def no_answer(error: type[httpx.TransportError] = httpx.ConnectError) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)

    return httpx.MockTransport(handle)


def adapter(base_url: str = RELAY, **options: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(SUPER, api_key="k", base_url=base_url, dialect="nebius",
                                   **options)


class Unanswered(Scripted):
    """A conductor model whose address does not answer."""

    async def complete(self, request: ModelRequest) -> Any:
        raise Unreachable(RELAY)


def unanswered_fake_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unanswered(self: FakeAdapter, request: ModelRequest) -> Any:
        raise Unreachable(RELAY)

    monkeypatch.setattr(FakeAdapter, "complete", unanswered)


def test_one_code_and_one_sentence_that_names_the_host() -> None:
    error = Unreachable(RELAY)
    assert str(error) == SAID and error.code == "provider_unreachable" == relay.UNREACHABLE
    assert code_for(error) == "provider_unreachable" and "provider_unreachable" in CODES
    assert "provider_unreachable" in relay.PLAIN and relay.sentence("provider_unreachable") == SAID
    assert explain("provider_unreachable").startswith("Architect could not reach")
    # The host and a port it names, never a path, a query or credentials.
    assert relay.address("https://user:secret@example.com:8443/v1?key=x") == "example.com:8443"
    assert relay.address("http://localhost:11434") == "localhost:11434"
    assert relay.address("not a url") == "the model's address"
    for text in (SAID, explain("provider_unreachable")):
        assert text.isascii() and ". " not in text and text.endswith("again.")  # one sentence
        assert "logged in" not in text and "signed out" not in text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_the_adapter_says_unreachable_for_a_call_and_a_stream(
    error: type[httpx.TransportError],
) -> None:
    with pytest.raises(Unreachable) as called:
        await adapter(transport=no_answer(error)).complete(ModelRequest(prompt="hi"))
    assert str(called.value) == SAID
    with pytest.raises(Unreachable) as streamed:
        async for _ in adapter(transport=no_answer(error)).stream_chat("", HI, (), None):
            pass
    assert str(streamed.value) == SAID
    direct = adapter("https://api.tokenfactory.nebius.com/v1", transport=no_answer(error))
    with pytest.raises(Unreachable, match="api.tokenfactory.nebius.com; check the connection"):
        await direct.complete(ModelRequest(prompt="hi"))


async def test_a_reply_that_breaks_off_is_not_called_unreachable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset", request=request)

    with pytest.raises(Exception) as caught:
        await adapter(transport=httpx.MockTransport(handle)).complete(ModelRequest(prompt="hi"))
    assert not isinstance(caught.value, Unreachable)


async def test_a_turn_with_no_answer_says_so_in_one_sentence(tmp_path: Any) -> None:
    fake_profile(tmp_path)
    console = ScriptedConsole(["hello"])
    frontend = LineFrontend(console)
    app = await boot(AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend,
                     adapter=Unanswered())
    repl = Repl(app, frontend)
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    out = console.transcript()
    assert SAID in out
    assert "The turn failed" not in out and "provider_not_logged_in" not in out


async def test_a_build_with_no_answer_stops_before_any_question(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Build(tmp_path, ["{}"], [])
    unanswered_fake_calls(monkeypatch)
    try:
        result = await make("make an agent that extracts invoice fields from text", h.ctx)
    finally:
        h.close()
    assert isinstance(result, dict) and result["failures"] == ["provider_unreachable"]
    assert result["next_step"] == SAID and h.ui.questions == []


async def test_a_frozen_build_with_no_answer_says_so_first(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_spec(tmp_path)
    h = Build(tmp_path, [], ["yes"])
    unanswered_fake_calls(monkeypatch)
    try:
        result = await make("", h.ctx, spec_file="receipts.spec.json")
    finally:
        h.close()
    assert isinstance(result, dict) and result["licensed"] is False, result
    failures = result["failures"]
    assert isinstance(failures, list) and failures[0] == "provider_unreachable"
    assert result["next_step"] == SAID
    said = [e.text for e in h.ui.events if isinstance(e, Notice)]
    assert any(SAID in text for text in said), said


async def test_try_it_carries_the_sentence_when_nothing_answers(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, wire = await started(tmp_path)
    try:
        await install(bridge, "alpha")
        unanswered_fake_calls(monkeypatch)
        await call(bridge, 2, "run_agent", name="alpha", input={"text": "hi"})
        assert wire.result(2)["error"] == {"code": "provider_unreachable", "message": SAID}
        assert SAID in [e["text"] for e in wire.events("notice")]
    finally:
        await bridge.close()
