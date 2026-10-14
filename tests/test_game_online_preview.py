"""Operator opt-in for a cloud game model on OFFLINE previews only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from architect.games.competition import (
    CompetitionDescription,
    CompetitionFactory,
    CompetitionPolicy,
)
from architect.games.evaluation import OfficialPreviewManifest
from architect.games.execution import GameExecutionPolicy
from architect.games.factory import (
    GameFactoryConfig,
    resolve_model_binding,
    validate_offline_adapter,
)
from architect.games.local import LocalFixtureManifest, LocalGameProvider
from architect.games.types import GameProtocolError, GameRef
from architect.models import OpenAICompatibleAdapter
from architect.models.types import ModelRequest, ModelResponse, UsageQuote
from architect.spec import Spec

_spec = importlib.util.spec_from_file_location(
    "arc_competition_example_online", Path(__file__).parents[1] / "examples/arc/competition.py"
)
assert _spec is not None and _spec.loader is not None
_example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_example)


class CloudModel:
    """Stand-in for a cloud adapter: not a FakeAdapter and not loopback."""

    def __init__(self, model: str = "fake") -> None:
        self.inner = _example.OfflineFixtureModel()
        self.model = model
        self._base_url = "https://api.example.com/v1"

    def quote(self, request: ModelRequest) -> UsageQuote:
        quote: UsageQuote = self.inner.quote(request)
        return quote

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response: ModelResponse = await self.inner.complete(request)
        return response


def description() -> CompetitionDescription:
    return CompetitionDescription.read(
        Path(__file__).parents[1] / "examples/arc/competition.local.json"
    )


def cloud_factory(tmp_path: Path, *, allow: bool, model: str = "fake") -> CompetitionFactory:
    factory: CompetitionFactory = _example.build_factory(tmp_path, description())
    cloud = CloudModel(model)
    for compiler in (factory.compiler, factory.game_factory.compiler):
        compiler.adapter_factory = lambda name: cloud
        compiler.inference_adapter = cloud
    payload = factory.game_factory.config.model_dump(mode="json")
    payload["allow_online_model"] = allow
    factory.game_factory.config = GameFactoryConfig.model_validate(payload)
    return factory


class GatewayConfig:
    mode = "gateway"


class GatewayLocalProvider(LocalGameProvider):
    config = GatewayConfig()


def test_opt_in_is_off_by_default_and_strict(tmp_path: Path) -> None:
    assert GameFactoryConfig.model_fields["allow_online_model"].default is False
    assert CompetitionPolicy.model_fields["model_binding"].default == "offline"
    assert GameExecutionPolicy.model_fields["model_binding"].default == "offline"
    config = _example.build_factory(tmp_path, description()).game_factory.config
    assert config.allow_online_model is False
    raw = config.model_dump(mode="json")
    raw["allow_online_model"] = "true"
    with pytest.raises(ValidationError):
        GameFactoryConfig.model_validate(raw)
    # The competition description (operator/model-authored input) has no such field.
    desc = description().model_dump(mode="json")
    desc["allow_online_model"] = True
    with pytest.raises(ValidationError):
        CompetitionDescription.model_validate(desc)


def test_online_binding_keeps_model_name_check_and_offline_rules() -> None:
    cloud = CloudModel()
    with pytest.raises(GameProtocolError, match="game_model_offline_required"):
        validate_offline_adapter(cloud, "fake")
    validate_offline_adapter(cloud, "fake", binding="online_preview")
    with pytest.raises(GameProtocolError, match="game_model_binding"):
        validate_offline_adapter(cloud, "other", binding="online_preview")
    remote = OpenAICompatibleAdapter(model="fake", base_url="https://api.example.com/v1")
    with pytest.raises(GameProtocolError, match="game_model_offline_required"):
        validate_offline_adapter(remote, "fake")


def test_resolve_binding_requires_offline_provider_and_preview_manifest() -> None:
    local = LocalGameProvider()
    assert resolve_model_binding(local, local.manifest, allow_online=False) == "offline"
    assert resolve_model_binding(local, local.manifest, allow_online=True) == "online_preview"
    with pytest.raises(GameProtocolError, match="game_online_model_gateway_forbidden"):
        resolve_model_binding(GatewayLocalProvider(), LocalFixtureManifest(), allow_online=True)
    refs = tuple(
        GameRef(backend="official_offline", game_id=f"aa{n:02}-r1", revision="r1") for n in range(5)
    )
    official = OfficialPreviewManifest(
        dataset_id="fake",
        dataset_revision="v1",
        parent_tuning=(refs[0],),
        parent_held_out=(refs[1],),
        support_tuning=(refs[2],),
        support_held_out=(refs[3],),
        delivery=(refs[4],),
    )

    class OfflineConfig:
        mode = "offline"

    class Offline:
        config = OfflineConfig()

    class Gateway:
        config = GatewayConfig()

    assert resolve_model_binding(Offline(), official, allow_online=True) == "online_preview"  # type: ignore[arg-type]
    with pytest.raises(GameProtocolError, match="game_online_model_gateway_forbidden"):
        resolve_model_binding(Gateway(), official, allow_online=True)  # type: ignore[arg-type]
    # An official manifest without a declared OFFLINE provider mode is refused.
    with pytest.raises(GameProtocolError, match="game_online_model_preview_required"):
        resolve_model_binding(local, official, allow_online=True)


async def test_cloud_model_refused_without_opt_in(tmp_path: Path) -> None:
    factory = cloud_factory(tmp_path, allow=False)
    with pytest.raises(GameProtocolError, match="game_model_offline_required"):
        await factory.compile(description())


async def test_opt_in_runs_and_labels_results(tmp_path: Path) -> None:
    factory = cloud_factory(tmp_path, allow=True)
    desc = description()
    proposed = await factory.compile(desc)
    assert proposed.spec.execution_config["model_binding"] == "online_preview"
    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    assert result.artifact is not None and result.artifact.licensed, result
    await factory.install(result, name=desc.name)
    run = await factory.run(desc.name)
    assert run.accepted and run.children, run
    assert run.model_binding == "online_preview" and run.compliance == "unverified"
    for child in run.children:
        assert child.model_binding == "online_preview" and child.compliance == "unverified"
        assert child.compile_result is not None
        policy = child.compile_result.spec.execution_config
        assert policy["model_binding"] == "online_preview"
    assert "online_preview" in run.model_dump_json()


async def test_default_offline_results_are_labeled_offline(tmp_path: Path) -> None:
    factory: CompetitionFactory = _example.build_factory(tmp_path, description())
    desc = description()
    proposed = await factory.compile(desc)
    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    await factory.install(result, name=desc.name)
    run = await factory.run(desc.name)
    assert run.accepted and run.model_binding == "offline"
    assert all(child.model_binding == "offline" for child in run.children)


async def test_gateway_provider_refused_even_with_opt_in(tmp_path: Path) -> None:
    factory = cloud_factory(tmp_path, allow=True)
    provider = GatewayLocalProvider()
    factory.provider = provider
    factory.game_factory.provider = provider
    with pytest.raises(GameProtocolError, match="game_online_model_gateway_forbidden"):
        await factory.compile(description())
    assert not factory.compiler.runtime.store.rows()


@pytest.mark.parametrize(
    ("allow", "forged_binding"), [(False, "online_preview"), (True, "offline")]
)
async def test_spec_cannot_change_model_binding(
    tmp_path: Path, allow: bool, forged_binding: str
) -> None:
    # Only the host config decides; a reviewed/proposed Spec cannot flip it either way.
    factory = cloud_factory(tmp_path, allow=allow)
    if not allow:
        factory = _example.build_factory(tmp_path / "offline", description())
    proposed = await factory.compile(description())
    payload = proposed.spec.model_dump(mode="json")
    payload["execution_config"]["model_binding"] = forged_binding
    forged = Spec.model_validate(payload)
    rows = len(factory.compiler.runtime.store.rows())
    with pytest.raises(GameProtocolError, match="competition_policy_mismatch"):
        await factory.compile(description(), approved=True, approved_spec=forged)
    assert len(factory.compiler.runtime.store.rows()) == rows


async def test_host_revoking_opt_in_blocks_installed_run(tmp_path: Path) -> None:
    factory = cloud_factory(tmp_path, allow=True)
    desc = description()
    proposed = await factory.compile(desc)
    result = await factory.compile(desc, approved=True, approved_spec=proposed.spec)
    await factory.install(result, name=desc.name)
    before = len(factory.compiler.runtime.store.rows())
    payload = factory.game_factory.config.model_dump(mode="json")
    payload["allow_online_model"] = False
    factory.game_factory.config = GameFactoryConfig.model_validate(payload)
    run = await factory.run(desc.name)
    assert not run.accepted and run.tokens_in == 0
    assert run.model_binding == "online_preview"  # The installed policy was online.
    assert len(factory.compiler.runtime.store.rows()) == before


async def test_online_binding_still_checks_model_name(tmp_path: Path) -> None:
    factory = cloud_factory(tmp_path, allow=True, model="not-fake")
    with pytest.raises(GameProtocolError, match="game_model_binding"):
        await factory.compile(description())
