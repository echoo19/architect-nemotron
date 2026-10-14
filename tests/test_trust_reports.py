"""W7: Ed25519-signed Stage 4 reports and licences (``_authority``, ``spec.license``).

A host with an Ed25519 key seals every report next to its HMAC. Registry loads and
``Runtime.run`` then accept the licence on a clean host whose trust list holds only the
public key, and refuse it everywhere else. Artifacts signed before W7 (HMAC only) keep
loading on the host that signed them. Offline: fake provider, sockets blocked.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from forge_modes_support import block_sockets
from trust_support import compile_install, host_key, trust_on, use_host, write_pub

from architect import trust
from architect._authority import _canonical_payload, verify_report
from architect.models import FakeAdapter
from architect.registry import Registry, RegistryError
from architect.runtime import Runtime, UnlicensedArtifactError
from architect.spec import HarnessArtifact
from architect.spec.license import check_license
from architect.telemetry import TelemetryStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _run(artifact: HarnessArtifact, root: Path) -> object:
    async def go() -> object:
        runtime = Runtime(TelemetryStore(root / "telemetry"))
        try:
            return (await runtime.run(artifact, {}, FakeAdapter(model="fake"))).output
        finally:
            runtime.store.close()

    return asyncio.run(go())


def _report(directory: Path) -> dict[str, object]:
    data = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# ------------------------------------------------------------------ signing


def test_stage4_seals_reports_on_a_host_with_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = use_host(monkeypatch, tmp_path / "a")
    public = host_key(host)
    entry, _, _ = compile_install(host)
    data = _report(entry.directory)
    seal = data["ed25519"]
    assert isinstance(seal, dict) and set(seal) == {"fingerprint", "signature"}
    assert seal["fingerprint"] == trust.fingerprint(public)
    assert isinstance(data["signature"], str) and len(data["signature"]) == 64  # HMAC kept
    artifact = HarnessArtifact.load(entry.directory)
    check = check_license(artifact)
    assert check.reason == "licensed" and check.via == "ed25519"
    assert check.signer is not None and check.signer.fingerprint == trust.fingerprint(public)
    # The seal covers every other field, the HMAC included; the HMAC still covers the rest.
    report = artifact.report
    assert report is not None and report.ed25519 is not None
    sealed = _canonical_payload(report.sealed_payload())
    assert trust.check_seal("report", sealed, report.ed25519).status == "valid"
    assert verify_report(report.signed_payload(), report.signature)
    # The public key itself is nowhere in the report.
    text = (entry.directory / "report.json").read_text(encoding="utf-8")
    assert trust.raw_public_key(public).hex() not in text
    assert "PUBLIC KEY" not in text


def test_without_a_key_reports_are_hmac_only_and_byte_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = use_host(monkeypatch, tmp_path / "a")
    entry, _, _ = compile_install(host)
    data = _report(entry.directory)
    assert "ed25519" not in data
    # The HMAC covers exactly what it covered before W7: every field but itself.
    signature = data.pop("signature")
    assert isinstance(signature, str) and verify_report(data, signature)
    artifact = HarnessArtifact.load(entry.directory)
    assert artifact.report is not None
    assert artifact.report.model_dump(mode="json") == {**data, "signature": signature}
    check = check_license(artifact)
    assert check.reason == "licensed" and check.via == "hmac" and check.seal is None


def test_an_unusable_key_fails_the_build_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge_modes_support import make_profile, services_for, spec

    from architect.compiler.verification import verify
    from architect.spec import Lifecycle

    host = use_host(monkeypatch, tmp_path / "a")
    (host / trust.ED25519_KEY_NAME).write_bytes(b"-----BEGIN PRIVATE KEY-----\nAAAA\n")
    services = services_for(make_profile(host))
    try:
        result = asyncio.run(
            services.compiler.compile(spec(lifecycle=Lifecycle.INSTALL), approved=True)
        )
        assert result.artifact is None and result.failures == ("key_invalid",)
        assert services.runtime.store.rows() == []  # nothing ran, so nothing was spent
    finally:
        services.close()
    unused = tmp_path / "unused"
    unused.mkdir()
    artifact = HarnessArtifact(spec=spec(), preset="extractor", model="fake", components=(),
                               directory=unused)
    with pytest.raises(trust.TrustError, match="^key_invalid$"):
        asyncio.run(verify(artifact, None, None, None))  # type: ignore[arg-type]


# ------------------------------------------------------------------ a clean host


def test_a_sealed_licence_loads_on_a_clean_host_through_registry_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = use_host(monkeypatch, tmp_path / "a")
    public = host_key(source)
    entry, _, workspace = compile_install(source)
    registry_copy = tmp_path / "b" / "registry"
    shutil.copytree(workspace / "registry", registry_copy)
    incoming = tmp_path / "b" / "incoming"
    shutil.copytree(entry.directory, incoming)

    # The clean host holds no HMAC key and no private key; its default trust list is
    # the shipped public key.
    clean = use_host(monkeypatch, tmp_path / "b")
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", public))
    artifact = HarnessArtifact.load(incoming)
    assert artifact.licensed
    assert check_license(artifact).via == "ed25519"
    # Registry load: the copied registry, and a fresh install into this host's own.
    loaded = Registry(registry_copy).get("feature-ok")
    assert loaded.licensed
    installed = Registry(clean / "own-registry").install("feature-ok", artifact)
    assert HarnessArtifact.load(installed.directory).licensed
    # Runtime.run
    assert _run(HarnessArtifact.load(installed.directory), clean) == "ok"
    assert not (clean / "signing.key").exists()
    assert not (clean / trust.ED25519_KEY_NAME).exists()

    # The same files, but the key only on a workspace trust list: accepted too.
    monkeypatch.setattr(trust, "SHIPPED_KEY", tmp_path / "absent.pub")
    assert not HarnessArtifact.load(incoming).licensed
    trust_on(clean, public)
    assert HarnessArtifact.load(incoming).licensed


def test_an_untrusted_key_is_refused_by_registry_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = use_host(monkeypatch, tmp_path / "a")
    host_key(source)
    entry, _, workspace = compile_install(source)
    registry_copy = tmp_path / "b" / "registry"
    shutil.copytree(workspace / "registry", registry_copy)
    licensed = HarnessArtifact.load(entry.directory)

    clean = use_host(monkeypatch, tmp_path / "b")
    stranger = trust.generate_key(tmp_path / "stranger.key")
    trust_on(clean, stranger, "someone else")
    artifact = HarnessArtifact.load(entry.directory)
    assert not artifact.licensed
    check = check_license(artifact)
    assert check.reason == "key_untrusted" and check.licensed is None
    with pytest.raises(RegistryError, match="^invalid_license$"):
        Registry(registry_copy).get("feature-ok")
    with pytest.raises(RegistryError, match="^invalid_license$"):
        Registry(clean / "own-registry").install("feature-ok", artifact)
    with pytest.raises(UnlicensedArtifactError):
        _run(licensed, clean)  # loaded on the signing host; re-checked here at run time


def test_a_changed_value_anywhere_in_a_sealed_report_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = use_host(monkeypatch, tmp_path / "a")
    public = host_key(source)
    entry, _, _ = compile_install(source)
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    path = entry.directory / "report.json"
    original = path.read_bytes()
    assert HarnessArtifact.load(entry.directory).licensed
    edits = {
        "floor_score": 0.5,
        "held_out_count": 999,
        "verification_id": "f" * 32,
        "signature": "0" * 64,  # the HMAC is covered by the seal too
    }
    for field, value in edits.items():
        data = json.loads(original)
        data[field] = value
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifact = HarnessArtifact.load(entry.directory)
        check = check_license(artifact)
        assert check.reason == "changed" and check.seal is not None, field
        assert check.seal.status == "invalid" and not artifact.licensed, field
    # A seal moved to another fingerprint is still the trusted key's signature: changed.
    data = json.loads(original)
    data["ed25519"]["fingerprint"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    assert check_license(HarnessArtifact.load(entry.directory)).reason == "changed"
    # Formatting is not content: the same values in other whitespace still verify.
    path.write_text(json.dumps(json.loads(original)), encoding="utf-8")
    assert HarnessArtifact.load(entry.directory).licensed


# ------------------------------------------------------------------ legacy HMAC artifacts


def test_hmac_only_legacy_artifacts_still_load_on_their_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = use_host(monkeypatch, tmp_path / "a")
    entry, registry, _ = compile_install(source)  # no Ed25519 key: HMAC only
    before = {path.name: path.read_bytes() for path in entry.directory.iterdir()
              if path.is_file()}
    assert "ed25519" not in before["report.json"].decode("utf-8")
    loaded = registry.get("feature-ok")
    assert loaded.licensed and _run(loaded, source) == "ok"
    # The host makes an Ed25519 key later: its old licences keep loading, unchanged.
    host_key(source)
    again = Registry(registry.root).get("feature-ok")
    assert again.licensed and check_license(again).via == "hmac"
    assert _run(again, source) == "ok"
    after = {path.name: path.read_bytes() for path in entry.directory.iterdir()
             if path.is_file()}
    assert after == before
    # Elsewhere, an HMAC-only licence stays unverifiable, whatever keys are trusted.
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, trust.generate_key(tmp_path / "other.key"))
    artifact = HarnessArtifact.load(entry.directory)
    assert not artifact.licensed
    assert check_license(artifact).reason == "unverifiable"


# ------------------------------------------------------------------ model-facing views


def test_model_tools_see_neither_the_key_nor_the_trust_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.agent.tools import WORKSPACE_SECRETS
    from architect.profiles import pi_denied_paths

    host = use_host(monkeypatch, tmp_path / "a")
    assert "ed25519.key" in WORKSPACE_SECRETS
    denied = pi_denied_paths(None, host)
    resolved = host.resolve()
    assert resolved / "ed25519.key" in denied
    assert resolved / "trusted-keys.json" in denied
