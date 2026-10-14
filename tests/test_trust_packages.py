"""W7: Ed25519-signed export packages.

A package exported on a host with an Ed25519 key runs on a clean host whose trust list
holds only the public key, refuses with ``artifact_changed`` after a one-byte edit, and
refuses a manifest that no trusted key signed. HMAC-only packages keep their old rules.
Offline: fake provider, sockets blocked; the generated tests run in a subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from forge_modes_support import block_sockets
from trust_support import compile_install, flip, host_key, trust_on, use_host, write_pub

from architect import trust
from architect.cli import main
from architect.forge_modes import ExportedHarness, ExportError, export_package, place_package
from architect.forge_modes.export import check_package, read_manifest, read_pin
from architect.forge_modes.integrate import placement_codes
from architect.models import FakeAdapter
from architect.runtime import Runtime
from architect.telemetry import TelemetryStore

PACKAGE = "receipt_reader"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, signed: bool = True
) -> tuple[Path, Ed25519PublicKey | None, Path]:
    """Compile, install and export on host ``a``; place the package in a repository."""
    source = use_host(monkeypatch, tmp_path / "a")
    public = host_key(source) if signed else None
    entry, _, workspace = compile_install(source)
    manifest, _ = export_package(
        entry, workspace / "exports", PACKAGE, model_class="synthetic_fake"
    )
    staged = workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(staged, repo, "vendor")
    return repo / "vendor" / PACKAGE, public, source


def _pin(package: Path) -> dict[str, object]:
    pin = read_pin(package)
    assert pin is not None
    return pin


def _run(package: Path, root: Path) -> object:
    async def go() -> object:
        runtime = Runtime(TelemetryStore(root / "telemetry"))
        try:
            harness = ExportedHarness(package, _pin(package))
            return await harness.run_with(runtime, FakeAdapter(model="fake"), {})
        finally:
            runtime.store.close()

    return asyncio.run(go())


def _generated_tests(package: Path, tmp_path: Path, host: Path) -> subprocess.CompletedProcess[str]:
    """The package's own tests in a fresh interpreter whose workspace is ``host``."""
    ini = tmp_path / "isolated.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    env = {**os.environ, "ARCHITECT_SIGNING_KEY_FILE": str(host / "signing.key"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("ARCHITECT_ED25519_KEY_FILE", "ARCHITECT_TRUST_FILE", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(package / "tests"), "-q", "-p", "no:cacheprovider",
         "-c", str(ini), "--basetemp", str(tmp_path / "gen-tmp"), "--color=no", "-rfs"],
        cwd=package.parent, env=env, capture_output=True, text=True, timeout=300, check=False,
    )


def _rewrite_manifest(package: Path, **changes: object) -> None:
    manifest = read_manifest(package)[0]
    data = manifest.model_dump(mode="json")
    data.update(changes)
    data = {key: value for key, value in data.items() if value is not None}
    (package / "MANIFEST.json").write_bytes(trust.canonical_json(data))


# ------------------------------------------------------------------ the clean host


def test_a_package_exported_on_one_workspace_runs_on_a_clean_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, public, _ = _exported(tmp_path, monkeypatch)
    assert public is not None
    manifest = json.loads((package / "MANIFEST.json").read_text(encoding="ascii"))
    assert manifest["ed25519"]["fingerprint"] == trust.fingerprint(public)
    evidence = json.loads((package / "EVIDENCE.json").read_text(encoding="ascii"))
    assert evidence["evidence_label"] == "ed25519_signed"
    assert trust.fingerprint(public)[:16] in evidence["attestation_note"]
    readme = (package / "README.md").read_text(encoding="ascii")
    assert trust.fingerprint(public) in readme and "architect verify" in readme
    # Manifests name the key's fingerprint, never the key, and no private key ships.
    raw = trust.raw_public_key(public)
    for path in package.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for form in (raw, base64.b64encode(raw), raw.hex().encode()):
                assert form not in data, path
            assert b"PRIVATE KEY" not in data and b"PUBLIC KEY" not in data, path

    # The clean host: no HMAC key, no private key; it trusts the public key, nothing else.
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    check = ExportedHarness(package, _pin(package)).verify()
    assert check.codes == () and check.licensed is True and check.integrity
    assert _run(package, clean) == "ok"
    assert not (clean / "signing.key").exists()
    assert not (clean / trust.ED25519_KEY_NAME).exists()
    done = _generated_tests(package, tmp_path, clean)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "7 passed" in done.stdout


def test_the_shipped_key_alone_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, public, _ = _exported(tmp_path, monkeypatch)
    assert public is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    assert ExportedHarness(package, _pin(package)).verify().codes == ("key_untrusted",)
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", public))
    assert ExportedHarness(package, _pin(package)).verify().codes == ()
    assert _run(package, clean) == "ok"


def test_an_untrusted_key_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package, _, _ = _exported(tmp_path, monkeypatch)
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, trust.generate_key(tmp_path / "stranger.key"), "someone else")
    check = ExportedHarness(package, _pin(package)).verify()
    assert check.licensed is None and check.codes == ("key_untrusted",)
    with pytest.raises(ExportError, match="^key_untrusted$"):
        ExportedHarness(package, _pin(package)).load()
    # W7-7: the package's own license tests fail on key_untrusted; they skip only on
    # license_unverifiable, where nothing here can check the license at all.
    done = _generated_tests(package, tmp_path, clean)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "3 failed, 4 passed" in done.stdout, done.stdout
    summary = done.stdout.split("short test summary")[-1]
    for name in ("test_license_on_signing_host", "test_refuses_wrong_model", "test_fake_smoke"):
        assert f"::{name}" in summary, done.stdout


# ------------------------------------------------------------------ one-byte edits


def test_one_byte_edits_refuse_with_artifact_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, public, _ = _exported(tmp_path, monkeypatch)
    assert public is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    pin = _pin(package)
    assert check_package(package, pin, registry=None).codes == ()
    files = sorted(path for path in package.rglob("*") if path.is_file())
    assert len(files) == 13  # .gitattributes included
    for path in files:
        original = path.read_bytes()
        for offset in sorted({0, len(original) // 2, len(original) - 1}):
            flip(path, offset)
            try:
                check = check_package(package, pin, registry=None)
                where = (path.relative_to(package).as_posix(), offset, check.codes)
                assert check.codes[:1] == ("artifact_changed",), where
                assert not check.integrity or check.licensed is not True, where
                with pytest.raises(ExportError, match="^artifact_changed$"):
                    ExportedHarness(package, pin).load()
            finally:
                path.write_bytes(original)
    # Every byte of the manifest's seal, fingerprint included.
    path = package / "MANIFEST.json"
    original = path.read_bytes()
    start = original.index(b'"ed25519"')
    for offset in range(start, original.index(b"}", start) + 1):
        flip(path, offset)
        try:
            codes = check_package(package, pin, registry=None).codes
            assert codes[:1] == ("artifact_changed",), (offset, codes)
        finally:
            path.write_bytes(original)
    assert check_package(package, pin, registry=None).codes == ()


def test_cli_and_placement_report_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from architect.session.exports import evidence_table

    package, public, _ = _exported(tmp_path, monkeypatch)
    assert public is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    manifest_sha = hashlib.sha256((package / "MANIFEST.json").read_bytes()).hexdigest()
    assert placement_codes(package, manifest_sha, None) == []
    assert main(["export", "verify", str(package)]) == 0
    assert "export evidence (Ed25519-signed;" in capsys.readouterr().out
    flip(package / "artifact" / "harness.py", 10)
    assert main(["export", "verify", str(package)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": "artifact_changed"}
    check = check_package(package, _pin(package), registry=None)
    assert "codes artifact_changed," in evidence_table(package, check)
    assert placement_codes(package, manifest_sha, None)[0] == "artifact_changed"


# ------------------------------------------------------------------ the manifest seal


def test_a_licence_signed_by_ed25519_needs_a_signed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, public, source = _exported(tmp_path, monkeypatch)
    assert public is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    pin = _pin(package)
    signed_manifest = (package / "MANIFEST.json").read_bytes()
    # Strip the manifest's seal: the licence still verifies, the other files do not.
    _rewrite_manifest(package, ed25519=None)
    assert check_package(package, pin, registry=None).codes == ("unsigned",)
    # ... even after a consistent rewrite of a file the licence does not bind.
    readme = package / "README.md"
    readme.write_text("# changed\n", encoding="ascii")
    files = [
        (name, hashlib.sha256((package / name).read_bytes()).hexdigest())
        for name, _ in read_manifest(package)[0].files
    ]
    _rewrite_manifest(package, files=files)
    assert check_package(package, pin, registry=None).codes == ("unsigned",)
    with pytest.raises(ExportError, match="^unsigned$"):
        ExportedHarness(package, pin).load()
    # The signing host itself applies the same rule to its sealed licences.
    use_host(monkeypatch, source)
    assert check_package(package, pin, registry=None).codes == ("unsigned",)
    # A manifest re-signed by a key this host does not trust.
    use_host(monkeypatch, clean)
    other = tmp_path / "other.key"
    other_public = trust.generate_key(other)
    body = trust.canonical_json(read_manifest(package)[0].model_dump(mode="json"))
    sealed = trust.seal("manifest", body, key=other)
    assert sealed is not None
    _rewrite_manifest(package, ed25519=sealed.model_dump(mode="json"))
    assert check_package(package, pin, registry=None).codes == ("key_untrusted",)
    # Trusting that key too makes it a signed manifest again (a deliberate choice here).
    trust_on(clean, other_public, "packager")
    assert check_package(package, pin, registry=None).codes == ()
    assert signed_manifest != (package / "MANIFEST.json").read_bytes()


# ------------------------------------------------------------------ HMAC-only packages


def test_hmac_only_packages_keep_their_old_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _, source = _exported(tmp_path, monkeypatch, signed=False)
    manifest = json.loads((package / "MANIFEST.json").read_text(encoding="ascii"))
    assert "ed25519" not in manifest
    evidence = json.loads((package / "EVIDENCE.json").read_text(encoding="ascii"))
    assert evidence["evidence_label"] == "host_attested"
    pin = _pin(package)
    assert check_package(package, pin, registry=None).codes == ()
    assert _run(package, source) == "ok"
    # The host makes an Ed25519 key later: the old package still loads there.
    public = host_key(source)
    assert check_package(package, pin, registry=None).codes == ()
    # Elsewhere the licence carries no seal, so even a host trusting that key refuses it.
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, public)
    check = check_package(package, pin, registry=None)
    assert check.licensed is None and check.codes == ("license_unverifiable",)
