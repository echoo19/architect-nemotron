"""The receipt reader for judges (examples/expense-app): its spec is the pre-registered one,
its profile is the evidence build's setup with Forge off by default, and ``/make
@receipt-reader.json`` in the app's folder shows the plan without a question. Also the two
profile rules it relies on: ``~`` is the home folder, and an entry without a ``base_url``
goes where its credential's stored URL says (a judge code's relay).

Offline: no plan here is approved, the stored relay address refuses connections, and the
home folder is a temporary one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_desktop_bridge import Wire, call

from architect.agent import RecordingUI, ToolContext
from architect.agent.app import SIGN_IN_FIRST, SIGN_IN_FIRST_TEXT, AppOptions, BootError, boot
from architect.agent.build import frozen_name, make
from architect.agent.events import PlanCard
from architect.agent.repl import LineFrontend, _interactive
from architect.compiler.forge_pack import pack_hash
from architect.compiler.types import ForgeSettings
from architect.desktop.bridge import Bridge
from architect.forge_modes.export import flag_name
from architect.models import OpenAICompatibleAdapter
from architect.profiles import (
    ProfileError,
    _adapter,
    build_services,
    forge_settings_of,
    in_workspace,
    load_profile,
)
from architect.providers import credentials
from architect.providers.onboarding import profile_workspace
from architect.session import ScriptedConsole
from architect.spec import Spec

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples" / "expense-app"
LADDER = ROOT / "examples" / "bench" / "eval-2026-10-05" / "ladder"
# The second ladder build's spec and profile: the first one's plus two rule checks.
CHECKED = ROOT / "examples" / "bench" / "eval-2026-10-06" / "ladder"
CHECKED_HASH = "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb"
FLOOR = "bench-receipts-v2-evidence"
MODELS = ("nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b",
          "nvidia/Nemotron-3-Ultra-550b-a55b")
RELAY = "http://127.0.0.1:9/v1"  # a judge code's relay; nothing listens there
TOKEN_FACTORY = "https://api.tokenfactory.nebius.com/v1"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home folder of its own, so ``~/.architect`` is never the real one."""
    folder = tmp_path / "home"
    folder.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(folder))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    return folder


def judge_code(home: Path) -> None:
    """What the first run stores for a judge code: the code and its relay's address."""
    workspace = home / ".architect"
    credentials.set_key("nebius", "rc_" + "a" * 32, workspace)
    credentials.set_base_url("nebius", RELAY, workspace)


def test_the_spec_is_the_pre_registered_one_with_its_checks_under_a_plain_name() -> None:
    """The second ladder build's spec: the 2026-10-05 build's spec, the one its outcome
    names, plus the two rule checks of the plan's 2026-10-06 change."""
    text = (APP / "receipt-reader.json").read_text(encoding="utf-8")
    assert text == (CHECKED / "spec.json").read_text(encoding="utf-8")
    spec = Spec.model_validate_json(text)
    assert spec.spec_hash == CHECKED_HASH
    outcome = json.loads((LADDER / "outcome.json").read_text(encoding="utf-8"))
    assert set(spec.execution_config) == {"checks"}
    plain = spec.model_copy(update={"execution_config": {}})
    assert plain.spec_hash == outcome["spec_hash"]
    assert spec.floor.limit == outcome["bar"] == 0.4 and spec.floor.dataset == FLOOR
    assert spec.model_target == "cheapest that passes"
    assert frozen_name("receipt-reader.json") == "receipt-reader"


def test_the_docs_name_the_specs_own_feature_flag_and_hash() -> None:
    spec = Spec.model_validate_json((APP / "receipt-reader.json").read_text(encoding="utf-8"))
    flag = flag_name(spec.spec_hash)
    readme = (APP / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"ARCHITECT_FEATURE_[0-9A-F]{8}", readme) == [flag]
    assert f"`{flag}=1`" in readme
    judges = (ROOT / "docs" / "judges.md").read_text(encoding="utf-8")
    assert "examples/bench/eval-2026-10-06/ladder/spec.json" in judges
    for text in (readme, judges):
        assert "ff985dd9" not in text.lower()


def test_the_checked_ladder_profile_is_the_first_ones_with_forge_off() -> None:
    """Only two things differ from 2026-10-05: every model goes through the deployed
    relay, which all Token Factory calls now use, instead of the local one, and Forge is
    off, so each rung is the tuned preset. The ladder, candidates per rung, floor and
    wallet are the same."""
    first = json.loads((LADDER / "profile.json").read_text(encoding="utf-8"))
    second = json.loads((CHECKED / "profile.json").read_text(encoding="utf-8"))
    relay = "https://architect-relay.vercel.app/v1"
    for entry in [*second["models"].values(), second["inference"], second["forge_agent"]]:
        assert entry.pop("base_url") == relay
    for entry in [*first["models"].values(), first["inference"], first["forge_agent"]]:
        entry.pop("base_url")
    assert second["compile"]["forge"].pop("enabled") is False
    assert first["compile"]["forge"].pop("enabled") is True
    assert second == first
    profile = load_profile(CHECKED / "profile.json")
    assert profile.compile.model_ladder == MODELS and profile.floors[FLOOR].checker == "receipt_v1"
    assert forge_settings_of(profile.compile).enabled is False


def test_the_profile_is_the_evidence_builds_with_forge_off_by_default(home: Path) -> None:
    judge, evidence = load_profile(APP / "architect.json"), load_profile(LADDER / "profile.json")
    ours, theirs = judge.compile.model_dump(), evidence.compile.model_dump()
    forge, evidence_forge = ours.pop("forge"), theirs.pop("forge")
    assert ours == theirs  # the ladder, candidates per rung, rewriting and the $0.60 wallet
    assert ours["model_ladder"] == MODELS
    assert {key for key in forge if forge[key] != evidence_forge[key]} == {"enabled", "trigger"}
    assert forge["enabled"] is False
    # Forge, once chosen, runs exactly as in the evidence build: the same pack hash.
    chosen = ForgeSettings.model_validate(
        {**forge, "enabled": True, "trigger": "always", "mode": "agent"})
    assert pack_hash(chosen) == pack_hash(forge_settings_of(evidence.compile))
    for name in MODELS:  # temperature 0, 512 tokens, thinking off; the address comes later
        assert judge.models[name].base_url is None
        assert judge.models[name].model_copy(update={"base_url": None}) == (
            evidence.models[name].model_copy(update={"base_url": None}))
    for role in ("inference", "forge_agent"):
        mine, its = getattr(judge, role), getattr(evidence, role)
        assert mine.model_copy(update={"base_url": None}) == its.model_copy(
            update={"base_url": None})
    floor = judge.floors[FLOOR]
    assert (floor.kind.value, floor.checker) == ("held_out_tasks", "receipt_v1")
    assert judge.resolve(floor.path) == (ROOT / "examples/receipts/floors/evidence.json")
    assert floor.manifest is not None
    assert judge.resolve(floor.manifest) == ROOT / "examples/receipts/manifests/evidence.json"
    assert judge.resolve(judge.workspace) == (home / ".architect").resolve()
    payload = json.loads((APP / "architect.json").read_text(encoding="utf-8"))
    assert profile_workspace(APP / "architect.json", payload) == (home / ".architect").resolve()


def test_an_entry_without_an_address_goes_where_its_credential_says(home: Path) -> None:
    profile = load_profile(APP / "architect.json")
    with pytest.raises(ProfileError, match="missing_api_key"):
        _adapter(profile, MODELS[1], profile.models[MODELS[1]])  # no key stored yet
    credentials.set_key("nebius", "v1.own-token-factory-key", home / ".architect")
    own = _adapter(profile, MODELS[1], profile.models[MODELS[1]])
    assert isinstance(own, OpenAICompatibleAdapter) and own._base_url == TOKEN_FACTORY
    assert own.dialect == "nebius" and own.temperature == 0.0
    judge_code(home)
    relayed = _adapter(profile, MODELS[1], profile.models[MODELS[1]])
    assert isinstance(relayed, OpenAICompatibleAdapter) and relayed._base_url == RELAY


async def test_make_at_the_spec_shows_the_bar_and_full_hash_without_a_question(
    home: Path,
) -> None:
    judge_code(home)
    profile = load_profile(APP / "architect.json")
    services = build_services(profile, profile.workspace)
    spec = Spec.model_validate_json((APP / "receipt-reader.json").read_text(encoding="utf-8"))
    ui = RecordingUI(answers=["no", "no", "no"])
    ctx = ToolContext(services=services, cwd=APP, ui=ui, model=MODELS[1])
    ctx.extras.update(profile_path=APP / "architect.json", build_overrides={})
    try:
        result = await make("", ctx, spec_file="receipt-reader.json")
        assert isinstance(result, dict) and result["cancelled"] is True
        assert ui.questions == ["Compile this agent?"]  # no interview: the spec as written
        [card] = [e for e in ui.events if isinstance(e, PlanCard)]
        rows = dict(card.rows)
        assert card.title == "Plan: receipt-reader"
        assert rows["spec"] == f"{spec.spec_hash} (frozen, receipt-reader.json)"
        assert rows["floor"] == f"held_out_tasks >= 0.40 on '{FLOOR}'"
        assert rows["floor data"].startswith("CORD v2 · CC BY 4.0 · 200 receipts")
        assert rows["engineer"] == "Tuned preset only: extractor (Forge off)"
        # The spec's two rule checks, digits first, right after the engineer.
        keys = [key for key, _ in card.rows]
        assert keys[keys.index("engineer") + 1] == "checks"
        assert rows["checks"] == (
            'Amounts keep every printed digit, so "226,500.00" is 22650000. A subtotal equal '
            "to the total is null unless the receipt prints that amount as a subtotal."
        )
        assert "forge budget" not in rows
        assert rows["model"] == "cheapest that passes over " + ", ".join(MODELS)
        assert rows["budget"] == "$0.02 and 60 s per run; compile up to $0.60 and 60 min"
        assert rows["install as"] == "receipt-reader (installed agent)"
        data: dict[str, Any] = dict(card.data or {})
        assert data["spec_hash"] == spec.spec_hash and data["bar"] == 0.4
        assert (data["practice"], data["exam"], data["ladder"]) == (100, 100, list(MODELS))
        assert (data["engineer"], data["build_usd"], data["build_s"]) == ("preset", 0.6, 3600)
        # /forge @receipt-reader.json keeps Forge off: the checks run inside the tuned preset.
        ctx.extras["force_forge"] = True
        await make("", ctx, spec_file="receipt-reader.json")
        plan = [e for e in ui.events if isinstance(e, PlanCard)][-1]
        rows = dict(plan.rows)
        assert rows["engineer"] == "Tuned preset only: extractor (Forge off)"
        assert ("note", "Forge stays off: this spec's checks run inside the tuned preset.") in (
            plan.rows)
        assert "forge budget" not in rows and "forge pack" not in rows
        assert dict(plan.data or {})["engineer"] == "preset"
        # On the 2026-10-05 spec, which has no checks, Forge runs as pre-registered.
        await make("", ctx, spec_file=str(LADDER / "spec.json"))
        rows = dict([e for e in ui.events if isinstance(e, PlanCard)][-1].rows)
        assert rows["engineer"].startswith(f"Forge ({MODELS[1]}) writes a custom harness")
        assert rows["forge budget"].startswith("up to $1.20 and 30 min")
        assert rows["forge pack"].startswith(
            "9a2d437bfd9376c9940e2fc758cc935cf23fc670b106a6ee16b4a1dfc150fbdf")
        assert not services.registry.list()
    finally:
        services.close()


@pytest.mark.parametrize("moved_by", ["ARCHITECT_WORKSPACE", "the app"])
async def test_the_profile_works_in_the_workspace_the_cli_and_the_app_use(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moved_by: str
) -> None:
    """The film records with ARCHITECT_WORKSPACE set, and the desktop app passes its own
    workspace setting: the receipt profile names ``~/.architect``, so it follows either,
    as a session profile kept in that workspace does, and finds the judge code there."""
    shared = tmp_path / "ws"
    credentials.set_key("nebius", "rc_" + "b" * 32, shared)
    credentials.set_base_url("nebius", RELAY, shared)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    if moved_by == "ARCHITECT_WORKSPACE":
        monkeypatch.setenv("ARCHITECT_WORKSPACE", str(shared))
        options = AppOptions(cwd=APP, probe=False)
    else:
        monkeypatch.delenv("ARCHITECT_WORKSPACE", raising=False)
        options = AppOptions(cwd=APP, workspace=shared, probe=False)
    app = await boot(options, RecordingUI())
    try:
        assert app.profile_path == APP / "architect.json"
        assert app.workspace == shared.resolve()
        assert app.services.workspace == shared.resolve()
        profile = app.services.profile
        adapter = _adapter(profile, MODELS[1], profile.models[MODELS[1]])
        assert isinstance(adapter, OpenAICompatibleAdapter) and adapter._base_url == RELAY
        reload = app.conductor.extras["reload"]
        assert callable(reload)
        assert reload().workspace == shared.resolve()  # /model, a drafted floor, Settings
    finally:
        app.close()
    assert not (home / ".architect").exists()


def test_only_a_profile_that_names_the_shared_workspace_moves(tmp_path: Path) -> None:
    receipt, ladder = load_profile(APP / "architect.json"), load_profile(LADDER / "profile.json")
    assert in_workspace(receipt, tmp_path).resolve(in_workspace(receipt, tmp_path).workspace) == (
        tmp_path.resolve())
    assert in_workspace(ladder, tmp_path) is ladder  # "." stays where the profile is


async def test_opening_the_folder_before_signing_in_says_to_sign_in_first(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before `architect judge` or a Nebius key, the receipt folder's profile has no key:
    the terminal says to sign in first, in words, and the app gets a code that opens its
    sign-in, never missing_api_key."""
    monkeypatch.delenv("ARCHITECT_WORKSPACE", raising=False)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    with pytest.raises(BootError) as failure:
        await boot(AppOptions(cwd=APP, probe=False), RecordingUI())
    assert failure.value.code == SIGN_IN_FIRST == "sign_in_first"
    console = ScriptedConsole([])
    code = await _interactive(AppOptions(cwd=APP, probe=False), LineFrontend(console),
                              resume=None, continue_latest=False)
    out = console.transcript()
    assert code == 1 and SIGN_IN_FIRST_TEXT in out
    assert "architect judge <code>" in out and "NEBIUS_API_KEY" in out
    assert "missing_api_key" not in out and "could not start" not in out
    monkeypatch.chdir(tmp_path)  # the bridge works in the folder it boots in
    wire = Wire()
    bridge = Bridge(wire.write)
    await call(bridge, 1, "boot", cwd=str(APP), probe=False)
    assert wire.result(1)["error"]["code"] == "sign_in_first"
    # Once the judge code is stored, the same folder starts.
    judge_code(home)
    app = await boot(AppOptions(cwd=APP, probe=False), RecordingUI())
    app.close()


async def test_another_missing_key_keeps_its_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "signing.key"))
    (tmp_path / "architect.json").write_text(json.dumps({
        "workspace": ".",
        "models": {"gpt-4o-mini": {"provider": "openai", "api_key_env": "NO_SUCH_KEY"}},
    }), encoding="utf-8")
    with pytest.raises(BootError) as failure:
        await boot(AppOptions(cwd=tmp_path, probe=False), RecordingUI())
    assert failure.value.code == "missing_api_key"
