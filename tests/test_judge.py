"""Judge codes (``architect.providers.judge``): a capped relay code stands in for a Nebius key.

Every request goes to an ``httpx.MockTransport``; nothing reaches the network. The code
under test must never appear in a message.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from architect import cli
from architect.providers import credentials, judge
from architect.providers.catalog import find
from architect.providers.host import Host
from architect.providers.signin import Prompter, onboard, sign_in

RELAY = "https://relay.example/v1"
CODE = "rc_Ab3-dE6_gH9jK2mN5pQ8sT1vW4yZ7aB2"
OTHER_CODE = "rc_Zy9-xW8_vU7tS6rQ5pO4nM3lK2jI1hG0"
REAL_KEY = "v1.real-token-factory-key-0123456789"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def _row(model: str, prompt: str, completion: str, clamp: int, context: int) -> dict[str, Any]:
    """One row of the relay's GET /v1/models (deploy/relay/src/handler.ts, models())."""
    return {
        "id": model, "object": "model", "created": 1791072000, "owned_by": "nvidia",
        "name": model, "context_length": context, "architecture": {"modality": "text->text"},
        "pricing": {"prompt": prompt, "completion": completion},
        "top_provider": {"max_completion_tokens": clamp, "context_length": context},
        "relay": {"price_date": "2026-10-03", "max_tokens_clamp": clamp},
    }


RELAY_MODELS = {"object": "list", "data": [
    _row(NANO, "0.00000006", "0.00000024", 8192, 262144),
    _row(LIGHTNING, "0.00000006", "0.00000024", 8192, 1048576),
    _row(SUPER, "0.0000003", "0.0000009", 8192, 262144),
    _row(ULTRA, "0.000001", "0.000003", 32768, 1048576),
]}
BALANCE = {
    "code": "0a1b2c3d4e5f", "label": "judge-primary", "remaining_usd": 2.75,
    "limited_by": "daily_cap", "spent_usd": 0.25, "in_flight_usd": 0, "cap_total_usd": 10,
    "requests": 4, "calls_running": 0, "max_calls_running": 8, "frozen": False,
    "today": {"day": "2026-12-01", "spent_usd": 0.25, "in_flight_usd": 0, "cap_daily_usd": 3,
              "remaining_usd": 2.75, "resets_at": "2026-12-02T00:00:00.000Z"},
    "relay_disabled": False, "price_date": "2026-10-03",
}


def relay_host(tmp_path: Path, *, env: dict[str, str] | None = None,
               models: Any = RELAY_MODELS, balance: Any = BALANCE,
               seen: list[httpx.Request] | None = None) -> Host:
    """A relay at RELAY that knows CODE and OTHER_CODE; every other URL refuses to connect."""

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        known = request.headers.get("authorization") in (f"Bearer {CODE}", f"Bearer {OTHER_CODE}")
        if url.startswith(f"{RELAY}/models"):
            if isinstance(models, int):
                return httpx.Response(models, json={"error": {"type": "x"}})
            if not known:
                return httpx.Response(401, json={"error": {"code": "unknown_code"}})
            return httpx.Response(200, json=models)
        if url == f"{RELAY}/relay/balance":
            if isinstance(balance, int):
                return httpx.Response(balance, json={"error": {"type": "x"}})
            return httpx.Response(200, json=balance)
        raise httpx.ConnectError("refused", request=request)

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Host(which=lambda name: None, env=env or {}, home=home,
                transport=httpx.MockTransport(handle),
                runner=lambda argv: pytest.fail(f"unexpected run {argv}"))


class Recorder:
    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.said: list[tuple[str, str]] = []
        self.asked: list[str] = []

    def say(self, text: str, level: str = "info") -> None:
        self.said.append((text, level))

    async def ask(self, question: str, *, default: str | None = None,
                  choices: Sequence[str] = ()) -> str | None:
        self.asked.append(question)
        if not self.answers:
            return None
        answer = self.answers.pop(0)
        if answer is None:
            return None
        return answer.strip() or (default if default is not None else "")

    def prompter(self) -> Prompter:
        return Prompter(self.say, self.ask)

    def text(self) -> str:
        return "\n".join(text for text, _ in self.said) + "\n" + "\n".join(self.asked)


@pytest.fixture(autouse=True)
def no_built_in_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test names its relay; the build's own address never leaks in."""
    monkeypatch.setattr(judge, "RELAY_URL", None)


# -- the code and the address -----------------------------------------------------------


def test_a_code_is_rc_and_32_base64url_characters() -> None:
    assert judge.is_code(CODE) and judge.is_code(f"  {CODE}\n")
    for wrong in ("", "rc_", "rc_short", CODE + "x", CODE[:-1], "RC_" + CODE[3:],
                  "rc_" + "a" * 31 + "!", REAL_KEY, None, 42):
        assert not judge.is_code(wrong), wrong


def test_the_relay_address_is_the_variable_else_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    assert judge.relay_url({}) is None
    assert judge.relay_url({"ARCHITECT_RELAY_URL": " https://a.example/v1/ "}) == "https://a.example/v1"
    monkeypatch.setattr(judge, "RELAY_URL", "https://built.example/v1/")
    assert judge.relay_url({}) == "https://built.example/v1"
    assert judge.relay_url({"ARCHITECT_RELAY_URL": "https://a.example/v1"}) == "https://a.example/v1"


# -- use_code ---------------------------------------------------------------------------


async def test_a_code_is_checked_at_the_relay_then_stored_with_its_address(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    seen: list[httpx.Request] = []
    result = await judge.use_code(CODE, ws, url=RELAY + "/", host=relay_host(tmp_path, seen=seen))
    assert result.ok and result.base_url == RELAY
    assert {model.id for model in result.models} == {NANO, LIGHTNING, SUPER, ULTRA}
    by_id = {model.id: model for model in result.models}
    price = by_id[SUPER].price
    assert price is not None and price.input_per_million == pytest.approx(0.3)
    assert (result.left_today, result.left_total) == (2.75, 9.75)
    assert credentials.get("nebius", ws, env={}) == CODE
    assert credentials.get_base_url("nebius", ws) == RELAY
    # One model list and one balance, both at the relay, both with the code.
    assert [str(r.url) for r in seen] == [f"{RELAY}/models?verbose=true", f"{RELAY}/relay/balance"]
    assert all(r.headers["authorization"] == f"Bearer {CODE}" for r in seen)
    text = judge.summary(result, ws)
    assert RELAY in text and "4 models" in text and "$2.75 left today" in text
    assert "$9.75 in all" in text and CODE not in text


async def test_the_address_comes_from_the_environment_when_none_is_given(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    h = relay_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY})
    result = await judge.use_code(CODE, ws, host=h)
    assert result.ok and credentials.get_base_url("nebius", ws) == RELAY


async def test_a_relay_that_will_not_give_a_balance_still_takes_the_code(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    for balance in (503, {"remaining_usd": "lots"}, ["not", "an", "object"]):
        result = await judge.use_code(CODE, ws, url=RELAY,
                                      host=relay_host(tmp_path, balance=balance))
        assert result.ok and result.left_total is None
        assert "left today" not in judge.summary(result, ws)


async def test_refusals_store_nothing_and_never_echo_the_code(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    h = relay_host(tmp_path)
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("rc_short", {"url": RELAY}, "invalid_code"),
        (REAL_KEY, {"url": RELAY}, "invalid_code"),
        (CODE, {}, "no_relay_url"),
        (CODE, {"url": "ftp://relay.example/v1"}, "invalid_url"),
        (CODE, {"url": "https://relay example/v1"}, "invalid_url"),
        (CODE, {"url": "http://[::1/v1"}, "invalid_url"),
        (CODE, {"url": "http://relay:b:c/v1"}, "invalid_url"),
    ]
    for code, options, expected in cases:
        result = await judge.use_code(code, ws, host=h, **options)
        assert (result.ok, result.code) == (False, expected), (code, options)
    unknown = await judge.use_code("rc_" + "q" * 32, ws, url=RELAY, host=h)
    assert unknown.code == "unauthorized"
    gone = await judge.use_code(CODE, ws, url="https://elsewhere.example/v1", host=h)
    assert gone.code == "unreachable"
    broken = await judge.use_code(CODE, ws, url=RELAY, host=relay_host(tmp_path, models=503))
    assert broken.code == "http_error"
    assert not credentials.path_for(ws).exists()
    for failure in ("invalid_code", "no_relay_url", "invalid_url", "key_in_env", "key_stored",
                    "unauthorized", "unreachable", "timeout", "write_failed", "http_error", None):
        assert CODE not in judge.explain(failure) and judge.explain(failure).endswith(".")


async def test_a_nebius_key_in_the_environment_wins_so_the_code_is_refused(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    seen: list[httpx.Request] = []
    h = relay_host(tmp_path, env={"NEBIUS_API_KEY": REAL_KEY}, seen=seen)
    result = await judge.use_code(CODE, ws, url=RELAY, host=h)
    assert result.code == "key_in_env" and not seen
    assert "NEBIUS_API_KEY" in judge.explain(result.code)
    assert credentials.get("nebius", ws, env={}) is None


async def test_a_stored_key_is_replaced_only_when_asked(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    h = relay_host(tmp_path)
    credentials.set_key("nebius", REAL_KEY, ws)
    kept = await judge.use_code(CODE, ws, url=RELAY, host=h)
    assert kept.code == "key_stored" and credentials.get("nebius", ws, env={}) == REAL_KEY
    assert credentials.get_base_url("nebius", ws) is None
    replaced = await judge.use_code(CODE, ws, url=RELAY, host=h, replace=True)
    assert replaced.ok and credentials.get("nebius", ws, env={}) == CODE
    # A stored code gives way to another code (a spare) without asking.
    spare = await judge.use_code(OTHER_CODE, ws, url=RELAY, host=h)
    assert spare.ok and credentials.get("nebius", ws, env={}) == OTHER_CODE


async def test_a_stored_code_makes_super_the_conversation_model_unless_one_is_set(
    tmp_path: Path,
) -> None:
    """A folder with its own profile (examples/expense-app) asks no first-run question, so
    without this its conversation would run on the ladder's first model, Lightning."""
    from architect.agent.config import load_config, set_value, workspace_config_path

    h = relay_host(tmp_path)
    ws = tmp_path / "ws"
    assert (await judge.use_code(CODE, ws, url=RELAY, host=h)).ok
    assert load_config(ws).text("model") == SUPER
    set_value(workspace_config_path(ws), "model", ULTRA)
    assert (await judge.use_code(OTHER_CODE, ws, url=RELAY, host=h)).ok
    assert load_config(ws).text("model") == ULTRA  # a model set before is kept
    # A workspace whose own profile does not list Super keeps its default.
    other = tmp_path / "other"
    other.mkdir()
    (other / "session-profile.json").write_text(
        json.dumps({"workspace": ".", "models": {"claude-haiku-4-5-20251001": {}}}),
        encoding="utf-8",
    )
    assert (await judge.use_code(CODE, other, url=RELAY, host=h)).ok
    assert load_config(other).text("model") is None
    # A refused code writes nothing.
    refused = tmp_path / "refused"
    assert not (await judge.use_code("rc_" + "q" * 32, refused, url=RELAY, host=h)).ok
    assert not workspace_config_path(refused).exists()


# -- the sign-in flow and the first run -------------------------------------------------


async def test_a_code_pasted_at_the_nebius_sign_in_goes_to_the_relay(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    seen: list[httpx.Request] = []
    h = relay_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY}, seen=seen)
    flow = Recorder(CODE)
    result = await sign_in(find("nebius"), ws, flow.prompter(), host=h)  # type: ignore[arg-type]
    assert result.ok and result.base_url == RELAY and len(result.models) == 4
    assert "API key or judge code" in flow.asked[0] and "or paste a judge code" in flow.text()
    assert "The judge code works. It is stored" in flow.text() and CODE not in flow.text()
    assert all(str(r.url).startswith(RELAY) for r in seen)
    assert credentials.get("nebius", ws, env={}) == CODE
    assert credentials.get_base_url("nebius", ws) == RELAY
    refused = Recorder("rc_" + "q" * 32)
    result = await sign_in(find("nebius"), tmp_path / "other", refused.prompter(), host=h)  # type: ignore[arg-type]
    assert not result.ok and result.code == "unauthorized"
    assert "unknown or has been revoked" in refused.text()


async def test_a_stored_code_is_called_a_code_not_a_key(tmp_path: Path) -> None:
    from architect.providers.status import provider_status

    ws = tmp_path / "ws"
    h = relay_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY})
    nebius = find("nebius")
    assert nebius is not None
    credentials.set_key("nebius", CODE, ws)
    credentials.set_base_url("nebius", RELAY, ws)
    status = await provider_status(nebius, ws, host=h)
    assert (status.detail, status.credential) == ("judge code stored", "judge_code")
    flow = Recorder("n")
    assert (await sign_in(nebius, ws, flow.prompter(), host=h)).ok
    assert flow.asked == ["Nebius Token Factory already has a stored judge code. Replace it?"]
    # A key of one's own keeps the old words.
    credentials.set_key("nebius", REAL_KEY, ws)
    status = await provider_status(nebius, ws, host=h)
    assert (status.detail, status.credential) == ("key stored", "key")
    keyed = Recorder("n")
    await sign_in(nebius, ws, keyed.prompter(), host=h)
    assert keyed.asked == ["Nebius Token Factory already has a stored key. Replace it?"]


async def test_without_a_relay_the_nebius_prompt_never_mentions_codes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    flow = Recorder(CODE)
    result = await sign_in(find("nebius"), ws, flow.prompter(), host=relay_host(tmp_path))  # type: ignore[arg-type]
    assert not result.ok and result.code == "no_relay_url"
    assert "judge code" not in flow.asked[0] and "or paste a judge code" not in flow.text()
    assert credentials.get("nebius", ws, env={}) is None


async def test_a_first_run_with_a_code_points_every_nemotron_entry_at_the_relay(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    h = relay_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY})
    flow = Recorder("nebius", CODE, "")  # Enter takes the suggested model
    key = await onboard(flow.prompter(), ws, ws / "session-profile.json", host=h,
                        probe_local=False)
    assert key == SUPER and CODE not in flow.text()
    payload = json.loads((ws / "session-profile.json").read_text(encoding="utf-8"))
    entries = [*payload["models"].values(), payload["inference"], payload["forge_agent"]]
    assert set(payload["models"]) >= {NANO, LIGHTNING, SUPER, ULTRA}
    assert all(e["base_url"] == RELAY and e["api_key_ref"] == "nebius" for e in entries)
    assert CODE not in (ws / "session-profile.json").read_text(encoding="utf-8")


async def test_the_first_run_suggests_nemotron_3_super_although_nano_sorts_first(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    suggested: list[str | None] = []

    class Suggestions(Recorder):
        async def ask(self, question: str, *, default: str | None = None,
                      choices: Sequence[str] = ()) -> str | None:
            suggested.append(default)
            return await super().ask(question, default=default, choices=choices)

    flow = Suggestions("nebius", CODE, "")
    h = relay_host(tmp_path, env={"ARCHITECT_RELAY_URL": RELAY})
    key = await onboard(flow.prompter(), ws, ws / "session-profile.json", host=h,
                        probe_local=False)
    listing = next(text for text, _ in flow.said if "models:" in text)
    assert listing.index(NANO) < listing.index(SUPER)  # the relay lists Nano first
    assert suggested[-1] == SUPER and key == SUPER  # Enter keeps Nemotron 3 Super


# -- architect judge <code> -------------------------------------------------------------


def test_the_cli_stores_the_code_and_never_prints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, Path, str | None]] = []

    async def fake_use_code(code: str, workspace: Path, *, url: str | None = None,
                            **_: object) -> judge.JudgeResult:
        calls.append((code, workspace, url))
        if code == CODE:
            return judge.JudgeResult(True, models=(), base_url=RELAY, left_today=3.0,
                                     left_total=10.0)
        return judge.JudgeResult(False, "unauthorized", base_url=RELAY)

    monkeypatch.setattr(judge, "use_code", fake_use_code)
    ws = tmp_path / "ws"
    assert cli.main(["judge", CODE, "--url", RELAY, "--workspace", str(ws)]) == 0
    out = capsys.readouterr()
    assert calls == [(CODE, ws.resolve(), RELAY)]
    assert "$3.00 left today" in out.out and "Nebius Token Factory" in out.out
    assert CODE not in out.out + out.err
    wrong = "rc_" + "q" * 32
    assert cli.main(["judge", wrong, "--workspace", str(ws)]) == 1
    out = capsys.readouterr()
    assert "unknown or has been revoked" in out.err and '{"error": "unauthorized"}' in out.err
    assert wrong not in out.out + out.err


def test_the_cli_says_how_to_start_architect_from_where_it_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_use_code(code: str, workspace: Path, **_: object) -> judge.JudgeResult:
        return judge.JudgeResult(True, models=(), base_url=RELAY)

    monkeypatch.setattr(judge, "use_code", fake_use_code)
    ws = tmp_path / "ws"
    root = Path(cli.__file__).resolve().parent.parent
    monkeypatch.chdir(root)
    assert cli.main(["judge", CODE, "--workspace", str(ws)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(
        "Next: run 'uv run architect' here and press Enter at each first-run question"
    )
    # examples/expense-app is a uv project of its own with a profile: --project, and no
    # first-run questions.
    monkeypatch.chdir(root / "examples" / "expense-app")
    assert cli.main(["judge", CODE, "--workspace", str(ws)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "Next: run 'uv run --project ../.. architect' here, or start a new conversation in "
        "the desktop app."
    )


def test_the_cli_uses_the_shared_workspace_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Path] = []

    async def fake_use_code(code: str, workspace: Path, **_: object) -> judge.JudgeResult:
        seen.append(workspace)
        return judge.JudgeResult(False, "unreachable")

    monkeypatch.setattr(judge, "use_code", fake_use_code)
    monkeypatch.setenv("ARCHITECT_WORKSPACE", str(tmp_path / "shared"))
    assert cli.main(["judge", CODE]) == 1
    assert seen == [(tmp_path / "shared").resolve()]
    assert "did not answer" in capsys.readouterr().err
