"""The general-model path, tested without calling any model."""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from app.extract import RULES, ExtractionError, Receipt, ask_general_model, parse_reply

# The sha256 that examples/receipts/RULES.md gives for its rules block (receipts-v2).
RULES_SHA256 = "b552ab6033626d0e5f1792a560932b764b749a7dc17f7f2cbe928653a8f52480"
RECEIPT = "1 TEH MANIS 6.000\nTOTAL 6.000"
ANSWER = '{"total": 6000, "subtotal": null, "tax": null, "item_count": 1}'


def reply(content: str | None) -> dict[str, Any]:
    """A chat completion with one choice."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def refused(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://test", code, "refused", email.message.Message(), None)


class Server:
    """Stands in for urlopen: gives its answers in turn (an exception is raised) and keeps
    every request it was sent."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    def body(self) -> Any:
        data = self.requests[-1].data
        assert isinstance(data, bytes)
        return json.loads(data)


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A key, the default address and model, and no real waiting between tries."""
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.delenv("NEBIUS_BASE_URL", raising=False)
    monkeypatch.delenv("NEBIUS_MODEL", raising=False)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


def serve(monkeypatch: pytest.MonkeyPatch, *answers: object) -> Server:
    server = Server(*answers)
    monkeypatch.setattr(urllib.request, "urlopen", server)
    return server


def test_the_prompt_quotes_the_receipt_rules_word_for_word() -> None:
    assert hashlib.sha256(RULES.encode()).hexdigest() == RULES_SHA256


def test_sends_the_rules_and_the_receipt_to_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    server = serve(monkeypatch, reply(ANSWER))
    assert ask_general_model(RECEIPT) == Receipt(total=6000, subtotal=None, tax=None, item_count=1)
    [request] = server.requests
    assert request.full_url == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert server.body() == {
        "model": "nvidia/Nemotron-3-Ultra-550b-a55b",
        "messages": [
            {"role": "system", "content": RULES},
            {"role": "user", "content": RECEIPT},
        ],
        "temperature": 0,
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert server.timeouts == [60]


def test_the_address_and_the_model_can_be_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_BASE_URL", "http://127.0.0.1:8787/v1/")
    monkeypatch.setenv("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b")
    server = serve(monkeypatch, reply(ANSWER))
    ask_general_model(RECEIPT)
    assert server.requests[0].full_url == "http://127.0.0.1:8787/v1/chat/completions"
    assert server.body()["model"] == "nvidia/nemotron-3-super-120b-a12b"


@pytest.mark.parametrize(
    "name, value, problem",
    [
        ("NEBIUS_API_KEY", "", "NEBIUS_API_KEY is not set"),
        ("NEBIUS_BASE_URL", "api.tokenfactory.nebius.com/v1", "NEBIUS_BASE_URL is not"),
    ],
)
def test_needs_a_key_and_an_http_address(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, problem: str
) -> None:
    monkeypatch.setenv(name, value)
    server = serve(monkeypatch)
    with pytest.raises(ExtractionError, match=problem):
        ask_general_model(RECEIPT)
    assert server.requests == []


def test_a_busy_server_is_tried_again(monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    server = serve(monkeypatch, refused(429), urllib.error.URLError("reset"), reply(ANSWER))
    assert ask_general_model(RECEIPT).total == 6000
    assert len(server.requests) == 3
    assert waits == [1, 2]


@pytest.mark.parametrize(
    "answers, failure",
    [
        ((refused(401),), "HTTP 401"),
        ((refused(503),) * 3, "HTTP 503"),
        ((TimeoutError(),) * 3, "TimeoutError"),
    ],
)
def test_a_failed_call_is_an_extraction_error(
    monkeypatch: pytest.MonkeyPatch, answers: tuple[object, ...], failure: str
) -> None:
    server = serve(monkeypatch, *answers)
    with pytest.raises(ExtractionError, match=rf"the model call failed \({failure}\)"):
        ask_general_model(RECEIPT)
    assert len(server.requests) == len(answers)


@pytest.mark.parametrize("answer", [{"choices": []}, {"error": "overloaded"}, []])
def test_a_reply_without_an_answer_is_an_extraction_error(
    monkeypatch: pytest.MonkeyPatch, answer: object
) -> None:
    serve(monkeypatch, answer)
    with pytest.raises(ExtractionError, match="the model sent no answer"):
        ask_general_model(RECEIPT)


def test_reads_json_inside_a_code_fence() -> None:
    answer = '```json\n{"total": 45100, "subtotal": 41000, "tax": 4100, "item_count": 2}\n```'
    assert parse_reply(answer) == Receipt(total=45100, subtotal=41000, tax=4100, item_count=2)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "I cannot read this receipt.",
        '{"total": "45,100", "subtotal": null, "tax": null, "item_count": 2}',
        '{"total": 45100, "subtotal": null, "tax": null}',
    ],
)
def test_refuses_an_answer_that_is_not_a_receipt(answer: str | None) -> None:
    with pytest.raises(ExtractionError):
        parse_reply(answer)
