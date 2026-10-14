"""W7 attack review: the attacks on the Ed25519 trust path that succeeded at review time.

Each test states the property the trust path should have. They were expected failures
until the W7 fixes; every finding is fixed now, so each test is a plain regression test
(the macOS ones run only there). Findings are numbered as in the W7 review: W7-1 a key
that came with a folder vouched for it, W7-2 so did a trust list, W7-3 small-order keys,
W7-4 8.3 names in the Pi extension, W7-5 case variants on macOS, W7-6 manifest names.
The rest of each fix (and W7-7, the package's own tests) is covered at the end.

Offline: fake provider, sockets blocked, throwaway keys made at run time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from forge_modes_support import block_sockets
from test_w7_trust_attacks import (
    PACKAGE,
    WINDOWS,
    _bench_key,
    _bench_package,
    _clean_host,
    _conductor,
    _mallory,
    _manifest_body,
    _manifest_seal,
    _read,
    _report_path,
    _reseal_report,
    _Script,
    _short_name,
    _tool,
    _trust_files,
    _write_manifest,
    _write_report,
)
from trust_support import compile_install, host_key, trust_on, use_host, write_pub

from architect import trust
from architect._authority import _canonical_payload, _sign_report
from architect.backends import pi as pi_module
from architect.backends.pi import _NODE, PiBackend, PiConfig, _granted_path
from architect.cli import main
from architect.forge_modes import ExportedHarness, ExportError, export_package
from architect.forge_modes.export import (
    ExportManifest,
    _target,
    check_package,
    read_manifest,
    read_pin,
)
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import (
    Budget,
    FloorDefinition,
    FloorKind,
    HarnessArtifact,
    Lifecycle,
    Spec,
    TestReport,
)
from architect.telemetry import TelemetryStore

_ENV = ("ARCHITECT_SIGNING_KEY_FILE", "ARCHITECT_ED25519_KEY_FILE", "ARCHITECT_TRUST_FILE")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _tampered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    """A bench-signed package whose code then changed, re-pointed at the new code
    everywhere (pins, manifest, report) with every seal stripped, in a downloaded folder.
    Returns (download folder, package, new artifact digest)."""
    package, _ = _bench_package(tmp_path, monkeypatch)
    download = package.parent.parent
    harness = package / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    digest = HarnessArtifact.load(package / "artifact").artifact_digest
    old = read_manifest(package)[0].artifact_digest
    init = package / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(old, digest), encoding="utf-8")
    path = package / "artifact" / "report.json"
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    _write_report(path, report.model_copy(update={"artifact_digest": digest, "ed25519": None}))
    _write_manifest(package, _manifest_body(package, artifact_digest=digest))
    return download, package, digest


def _victim(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """A shell in ``folder`` with no Architect variables: the documented defaults apply."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(folder)


# ------------------------------------------------------------------ W7-1, W7-2


def test_a_signing_key_that_came_with_the_folder_does_not_vouch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Downgrade to the HMAC on a foreign host: strip the Ed25519 seals, re-license the
    changed code with an HMAC key of the attacker's own and ship that key beside it."""
    download, package, _ = _tampered(tmp_path, monkeypatch)
    planted = os.urandom(32)
    (download / ".architect").mkdir()
    (download / ".architect" / "signing.key").write_bytes(planted)
    path = package / "artifact" / "report.json"
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    forged = hmac.new(planted, _canonical_payload(report.signed_payload()), hashlib.sha256)
    _write_report(path, report.model_copy(update={"signature": forged.hexdigest()}))
    _write_manifest(package, _manifest_body(package))
    _victim(monkeypatch, download)
    code = main(["verify", f"lib/{PACKAGE}"])
    out = capsys.readouterr().out
    # Before W7-1: "verified" and "signed by this host's own key (only this host can
    # check it)". Now ./.architect never counts on its own.
    assert code == 1 and out.startswith("refused: license_unverifiable"), out
    with pytest.raises(ExportError):
        ExportedHarness(package, read_pin(package) or {}).load()


def test_a_trust_list_that_came_with_the_folder_does_not_vouch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    download, package, digest = _tampered(tmp_path, monkeypatch)
    mallory = tmp_path / "mallory.key"
    public = trust.generate_key(mallory)
    _reseal_report(package / "artifact" / "report.json", mallory)
    body = _manifest_body(package, artifact_digest=digest)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    (download / ".architect").mkdir()
    trust.add_trusted_key(public, "architect", download / ".architect" / trust.TRUST_FILE_NAME)
    _victim(monkeypatch, download)
    code = main(["verify", f"lib/{PACKAGE}"])
    out = capsys.readouterr().out
    # Before W7-2: "verified", "signed by key ... (trusted in this workspace as
    # architect)" and a relative "trust list: .architect\\trusted-keys.json" line.
    assert code == 1 and out.startswith("refused: key_untrusted"), out
    assert "only the key shipped with Architect is trusted here" in out, out
    with pytest.raises(ExportError):
        ExportedHarness(package, read_pin(package) or {}).load()


# ------------------------------------------------------------------ W7-3


SMALL_ORDER = {
    # The eight points of small order, then the non-canonical encodings of some of them
    # (y >= p, or x = 0 with the sign bit set). Each verifies a constant forged signature
    # (R a small-order point, S = 0) for every message or for one message in 2 to 8.
    "identity": "01" + "00" * 31,
    "order-2": "ec" + "ff" * 30 + "7f",
    "order-4": "00" * 32,
    "order-4-neg": "00" * 31 + "80",
    "order-8-a": "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "order-8-b": "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
    "order-8-c": "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "order-8-d": "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
    "identity-neg": "01" + "00" * 30 + "80",
    "order-2-neg": "ec" + "ff" * 31,
    "y-p+1": "ee" + "ff" * 30 + "7f",
    "y-p+1-neg": "ee" + "ff" * 31,
    "y-p": "ed" + "ff" * 30 + "7f",
    "y-p-neg": "ed" + "ff" * 31,
}


@pytest.mark.parametrize("name", sorted(SMALL_ORDER))
def test_small_order_points_are_not_public_keys(name: str) -> None:
    raw = bytes.fromhex(SMALL_ORDER[name])
    with pytest.raises(trust.TrustError, match="^key_invalid$"):
        trust.parse_public_key(base64.b64encode(raw).decode())


def test_a_constant_signature_never_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One bad key in the list (from `keys trust` of a file someone handed over) and any
    folder, report or manifest could be signed by anyone, with no key and no hashing.
    `keys trust` refuses such a key, and written into the list by hand it makes the
    whole list unreadable, so nothing is trusted."""
    host = use_host(monkeypatch, tmp_path / "h")
    identity = bytes.fromhex(SMALL_ORDER["identity"])
    handed = tmp_path / "partner.pub"
    handed.write_text(base64.b64encode(identity).decode() + "\n", encoding="ascii")
    assert main(["keys", "trust", str(handed), "--name", "partner"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "key_invalid"}
    assert not (host / trust.TRUST_FILE_NAME).exists()
    folder = tmp_path / "results"
    folder.mkdir()
    (folder / "scores.csv").write_text("model,score\nmine,1.0\n", encoding="ascii")
    files = [["scores.csv", hashlib.sha256((folder / "scores.csv").read_bytes()).hexdigest()]]
    forged = {
        "ed25519": {
            "fingerprint": hashlib.sha256(identity).hexdigest(),
            "signature": (identity + bytes(32)).hex(),
        },
        "files": files,
        "format": "architect-published-v1",
        "name": "results",
    }
    (folder / trust.PUBLISHED_FILE).write_bytes(trust.canonical_json(forged))
    assert trust.check_published(folder).code == "key_untrusted"
    assert main(["verify", str(folder)]) == 1
    capsys.readouterr()
    planted = base64.b64encode(identity).decode()
    listing = {"keys": [{"name": "partner", "public_key": planted}]}
    (host / trust.TRUST_FILE_NAME).write_text(json.dumps(listing), encoding="ascii")
    assert trust.load_trust().error == "trust_list_invalid"
    assert trust.check_published(folder).code == "trust_list_invalid"
    assert main(["verify", str(folder)]) == 1
    assert capsys.readouterr().out.startswith("refused: trust_list_invalid")


# ------------------------------------------------------------------ W7-4, W7-5


@pytest.mark.skipif(
    not WINDOWS
    or not (_NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file()
    or shutil.which("node") is None,
    reason="Windows 8.3 aliases and real Pi",
)
async def test_the_pi_extension_alone_refuses_short_names_of_the_trust_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: with the parent's path check switched off, the extension's own
    check must still deny every alias of the trust list."""
    repo, trust_file, _ = _trust_files(tmp_path, monkeypatch, "operator")
    listed = '{"keys": []}'
    trust_file.write_text(listed, encoding="ascii")
    alias = _short_name(trust_file)
    if alias is None:
        pytest.skip("this volume keeps no 8.3 aliases")
    monkeypatch.setattr(pi_module, "_granted_path", lambda *args: True)
    planted = '{"keys": [{"name": "model", "public_key": "' + "A" * 43 + '="}]}'
    adapter = _Script([_tool("write", {"path": f"operator/{alias}", "content": planted}), "ok"])
    task = Spec(
        task="Perform the supplied task without granting new authority",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="trusted-floor", minimum_samples=2
        ),
        budget=Budget(cost_usd=1, compute_s=60),
        tools=("read", "write", "edit"),
    )
    with TelemetryStore(tmp_path / "telemetry") as store:
        await Runtime(store, pi_backend=PiBackend(PiConfig())).evaluate(
            task, get_preset("coder").build(task), {"repo": str(repo)}, adapter
        )
    assert trust_file.read_text(encoding="ascii") == listed, "Pi rewrote the trust list"


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive APFS")
def test_a_case_variant_of_a_trust_file_outside_architect_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, trust_file, key = _trust_files(tmp_path, monkeypatch, "operator")
    trust_file.write_text('{"keys": []}', encoding="ascii")
    key.write_text("PRIVATE", encoding="ascii")
    protected = (Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"]), *trust.protected_paths())
    denied = tuple(path.resolve() for path in protected)
    for raw in ("OPERATOR/TRUSTED-KEYS.JSON", "operator/Trusted-Keys.json",
                "OPERATOR/ED25519.KEY"):
        for tool_name in ("read", "write", "edit"):
            assert _granted_path(repo, raw, tool_name, 1_048_576, denied) is False, raw


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive APFS")
def test_the_conductor_never_reads_a_case_variant_of_the_ed25519_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from architect.agent.tools import ToolError

    cwd = tmp_path.resolve()
    workspace = cwd / ".architect"
    workspace.mkdir()
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(workspace / "signing.key"))
    (workspace / trust.ED25519_KEY_NAME).write_text("SYNTHETIC-KEY", encoding="ascii")
    context = _conductor(cwd, workspace)
    for raw in (".architect/ED25519.KEY", ".ARCHITECT/Ed25519.key"):
        try:
            _read(context, raw)
        except ToolError as failure:
            assert failure.code == "denied_path", raw
        else:
            raise AssertionError(f"read_file returned the private key as {raw}")


# ------------------------------------------------------------------ W7-6


def test_manifest_file_names_stay_inside_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _ = _bench_package(tmp_path, monkeypatch)
    data = json.loads((package / "MANIFEST.json").read_bytes())
    data.pop("ed25519")
    for name in ("../escape.py", "/etc/passwd", "C:/Windows/win.ini", "a//b", ""):
        listed = {**data, "files": sorted([*data["files"], [name, "0" * 64]])}
        with pytest.raises(ValueError):
            ExportManifest.model_validate(listed)


# ------------------------------------------------------------------ the rest of each fix


def _plant_hmac(package: Path, key: Path) -> None:
    """Re-sign the package's (seal-less) report with an HMAC key of the attacker's own,
    written to ``key``, and re-list the manifest unsealed."""
    planted = os.urandom(32)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(planted)
    path = _report_path(package)
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    forged = hmac.new(planted, _canonical_payload(report.signed_payload()), hashlib.sha256)
    _write_report(path, report.model_copy(update={"signature": forged.hexdigest()}))
    _write_manifest(package, _manifest_body(package))


def _own_tests(package: Path, cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """The package's own tests in a fresh interpreter, run from ``cwd`` with no
    ``ARCHITECT_*`` variable: the documented defaults."""
    ini = tmp_path / "isolated.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in (*_ENV, "PYTEST_ADDOPTS")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(package / "tests"), "-q", "-p", "no:cacheprovider",
         "-c", str(ini), "--basetemp", str(tmp_path / "own-tmp"), "--color=no", "-rfs"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False,
    )


def test_export_verify_and_the_packages_own_tests_ignore_a_planted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W7-1 for ``architect export verify`` and the generated tests: the downgraded package
    of the W7-1 attack, with its key in the downloaded folder's .architect."""
    download, package, _ = _tampered(tmp_path, monkeypatch)
    _plant_hmac(package, download / ".architect" / "signing.key")
    _victim(monkeypatch, download)
    assert main(["export", "verify", f"lib/{PACKAGE}"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "license_unverifiable"}
    # Its own tests skip the license tests (nothing here can check it) instead of passing.
    done = _own_tests(package, download, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "4 passed, 3 skipped" in done.stdout, done.stdout


def test_export_verify_takes_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no variables, ``export verify`` names the HMAC key's workspace explicitly."""
    host = use_host(monkeypatch, tmp_path / "a")
    entry, _, workspace = compile_install(host)
    manifest, _ = export_package(entry, workspace / "exports", PACKAGE)
    staged = workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE
    _victim(monkeypatch, tmp_path)
    assert main(["export", "verify", str(staged)]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "license_unverifiable"}
    assert main(["export", "verify", "--workspace", str(host), str(staged)]) == 0
    assert "codes -" in capsys.readouterr().out


def test_a_key_or_a_trust_list_inside_the_package_never_vouches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W7-1, last rule: the package ships the key (or the list) and its README says to point
    ARCHITECT_SIGNING_KEY_FILE (or ARCHITECT_TRUST_FILE, or --workspace) at it."""
    download, package, digest = _tampered(tmp_path, monkeypatch)
    key = package / "keys" / "signing.key"
    _plant_hmac(package, key)
    _victim(monkeypatch, download)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(key))
    pin = read_pin(package) or {}
    check = check_package(package, pin, registry=None)
    assert check.codes[:1] == ("trust_in_target",) and check.licensed is None, check
    with pytest.raises(ExportError, match="^trust_in_target$"):
        ExportedHarness(package, pin).load()
    assert main(["verify", f"lib/{PACKAGE}"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: trust_in_target"), out
    assert f"the HMAC key at {key.resolve()} lies inside the folder being checked" in out, out
    assert main(["export", "verify", f"lib/{PACKAGE}"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "trust_in_target"}
    monkeypatch.delenv("ARCHITECT_SIGNING_KEY_FILE")
    assert main(["verify", "--workspace", f"lib/{PACKAGE}/keys", f"lib/{PACKAGE}"]) == 1
    assert capsys.readouterr().out.startswith("refused: trust_in_target")
    # The list the W7-2 attack plants, shipped inside the package and named by the variable.
    mallory = tmp_path / "mallory.key"
    public = trust.generate_key(mallory)
    _reseal_report(_report_path(package), mallory)
    listed = package / "keys" / trust.TRUST_FILE_NAME
    trust.add_trusted_key(public, "architect", listed)
    body = _manifest_body(package, artifact_digest=digest)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    monkeypatch.setenv("ARCHITECT_TRUST_FILE", str(listed))
    assert check_package(package, pin, registry=None).codes[:1] == ("trust_in_target",)
    with pytest.raises(ExportError, match="^trust_in_target$"):
        ExportedHarness(package, pin).load()
    assert main(["verify", f"lib/{PACKAGE}"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: trust_in_target") and "the trust list at" in out, out


def test_require_key_accepts_only_that_keys_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W7-1: ``--require-key`` lets a judge or CI refuse a downgrade to the HMAC, even on
    the host whose HMAC key signed it, and any other key, even a trusted one."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    private = trust.load_private_key(_bench_key(bench))
    assert private is not None
    bench_fp = trust.fingerprint(private.public_key())
    clean = _clean_host(tmp_path, monkeypatch, bench)
    for spelled in (bench_fp, bench_fp.upper()):
        assert main(["verify", "--require-key", spelled, str(package)]) == 0
        assert capsys.readouterr().out.startswith("verified")
    for spelled in (bench_fp[:16], bench_fp + "0", "z" * 64, ""):
        assert main(["verify", "--require-key", spelled, str(package)]) == 1
        assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}
    # A key not on the trust list cannot be required; a trusted key that did not sign
    # is not the required one.
    mallory, mallory_fp = _mallory(tmp_path)
    assert main(["verify", "--require-key", mallory_fp, str(package)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: key_untrusted") and "is not on the trust list here" in out
    other = trust.load_private_key(mallory)
    assert other is not None
    trust_on(clean, other.public_key(), "partner")
    assert main(["verify", "--require-key", mallory_fp, str(package)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: key_untrusted"), out
    assert f"signed by key {bench_fp[:16]}, not the required key {mallory_fp[:16]}" in out
    # Stripped back to the HMAC on the host that holds it: verified by that key alone,
    # refused with --require-key.
    use_host(monkeypatch, bench)
    path = _report_path(package)
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    _write_report(path, report.model_copy(update={"ed25519": None}))
    _write_manifest(package, _manifest_body(package))
    assert main(["verify", str(package)]) == 0
    assert f"signed by the HMAC key at {bench / 'signing.key'}" in capsys.readouterr().out
    for target in (package, path):
        assert main(["verify", "--require-key", bench_fp, str(target)]) == 1
        out = capsys.readouterr().out
        assert out.startswith("refused: license_unverifiable"), out
        assert f"--require-key accepts only a signature by key {bench_fp[:16]}" in out, out


def test_the_trust_list_line_is_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W7-2: the list that vouched is named by its absolute path, so a list in an
    unexpected place shows where it is."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    _clean_host(tmp_path, monkeypatch, bench)
    _victim(monkeypatch, tmp_path)
    assert main(["verify", "--workspace", "b", str(package)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == f"trust list: {tmp_path / 'b' / trust.TRUST_FILE_NAME}", lines
    assert main(["keys", "list", "--workspace", "b"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"trust list: {tmp_path / 'b' / trust.TRUST_FILE_NAME}"
    )


def test_a_named_trust_file_that_is_missing_trusts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ARCHITECT_TRUST_FILE set to a file that does not exist trusts no key at all, not
    even the shipped one, and says so; it never falls back quietly."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    private = trust.load_private_key(_bench_key(bench))
    assert private is not None
    shipped = write_pub(tmp_path / "shipped.pub", private.public_key())
    monkeypatch.setattr(trust, "SHIPPED_KEY", shipped)
    use_host(monkeypatch, tmp_path / "b")
    assert check_package(package, read_pin(package), registry=None).codes == ()
    named = tmp_path / "typo" / trust.TRUST_FILE_NAME
    monkeypatch.setenv("ARCHITECT_TRUST_FILE", str(named))
    listed = trust.load_trust()
    assert listed.error == "trust_list_missing" and dict(listed.keys) == {}
    assert check_package(package, read_pin(package), registry=None).codes == (
        "trust_list_missing",
    )
    with pytest.raises(ExportError, match="^trust_list_missing$"):
        ExportedHarness(package, read_pin(package) or {}).load()
    assert main(["verify", str(package)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "refused: trust_list_missing",
        f"ARCHITECT_TRUST_FILE names {named}, which does not exist, so no key is trusted",
    ]
    assert main(["keys", "list"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "trust_list_missing"}
    # Named on purpose: keys trust creates it.
    assert main(["keys", "trust", str(shipped), "--name", "bench"]) == 0
    capsys.readouterr()
    assert named.is_file() and trust.load_trust().error is None


def test_the_evidence_label_follows_the_signature_that_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a key rotation the old seal no longer verifies here, so the export is
    host-attested; it never advertises the old key."""
    host = use_host(monkeypatch, tmp_path / "a")
    old = host_key(host)
    entry, _, workspace = compile_install(host)
    (host / trust.ED25519_KEY_NAME).unlink()
    (host / trust.TRUST_FILE_NAME).unlink()
    host_key(host, "bench 2")
    manifest, _ = export_package(entry, workspace / "exports", PACKAGE)
    staged = workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE
    evidence = json.loads((staged / "EVIDENCE.json").read_text(encoding="ascii"))
    assert evidence["evidence_label"] == "host_attested"
    for name in ("EVIDENCE.json", "EVIDENCE.md", "README.md"):
        assert trust.fingerprint(old)[:16] not in (staged / name).read_text(encoding="ascii")


def test_verify_prints_no_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A signer's text (failures, the model, the preset) never reaches the terminal raw."""
    use_host(monkeypatch, tmp_path / "h")
    report = TestReport(
        spec_hash="1" * 64, artifact_digest="2" * 64, passed=False, floor_score=0.5,
        floor_limit=0.9, floor_kind=FloorKind.HELD_OUT_TASKS, tokens=1, cost_usd=0.0,
        compute_s=0.1, component_count=1, verification_id="v-1",
        failures=("\x1b[2J\x1b[Hverified", "floor‮", "bell\x07"),
    )
    signed = report.model_copy(update={"signature": _sign_report(report.signed_payload())})
    path = tmp_path / "report.json"
    path.write_text(signed.model_dump_json(), encoding="utf-8")
    assert main(["verify", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and "\x1b" not in out and "\x07" not in out, out
    assert "failures: ?[2J?[Hverified, floor?, bell?" in out.splitlines()
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "harness.py").write_text(
        "async def run(context, task):\n    return 'ok'\n", encoding="utf-8"
    )
    artifact = HarnessArtifact(
        spec=Spec(
            task="Answer", input_schema={}, output_schema={},
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.8, dataset="d.jsonl"),
            budget=Budget(cost_usd=1, compute_s=30), lifecycle=Lifecycle.INSTALL,
        ),
        preset="extractor\x1b[31m", model="fake\x1b]0;owned\x07", components=(),
        directory=directory,
    )
    body = TestReport(
        spec_hash=artifact.spec.spec_hash, artifact_digest=artifact.artifact_digest,
        passed=True, floor_score=1.0, floor_limit=0.8, floor_kind=FloorKind.HELD_OUT_TASKS,
        tokens=1, cost_usd=0.0, compute_s=0.1, component_count=artifact.component_count,
        verification_id="v-2",
    )
    licensed = artifact.model_copy(update={"report": body.model_copy(
        update={"signature": _sign_report(body.signed_payload())})})
    licensed.save()
    assert licensed.licensed
    assert main(["verify", str(directory)]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and "\x1b" not in out and "\x07" not in out, out
    assert out.splitlines()[1].endswith(", extractor?[31m on fake?]0;owned?"), out


def test_case_variants_of_protected_files_are_refused_on_every_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W7-5 where it can run anywhere: a case variant names the protected file on APFS and
    NTFS, so Pi may not write one on any system (folding only widens what is refused),
    and the conductor never reads one. A ~digit segment (an 8.3 alias) is refused too."""
    from architect.agent.tools import _is_secret

    repo, _, key = _trust_files(tmp_path, monkeypatch, "operator")
    protected = (Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"]), *trust.protected_paths())
    denied = tuple(path.resolve() for path in protected)
    for raw in ("OPERATOR/TRUSTED-KEYS.JSON", "operator/Trusted-Keys.json",
                "OPERATOR/ED25519.KEY", "Operator/Signing.Key", "operator/TRUSTE~1.JSO",
                "notes~1.md"):
        assert _granted_path(repo, raw, "write", 1_048_576, denied) is False, raw
    assert _granted_path(repo, "operator/notes.md", "write", 1_048_576, denied) is True
    assert trust.within(repo / "OPERATOR" / "x.json", repo / "operator")
    assert not trust.within(repo / "operators" / "x.json", repo / "operator")
    key.write_text("PRIVATE", encoding="ascii")
    hidden = frozenset({key.resolve()})
    assert _is_secret(repo / "OPERATOR" / "ED25519.KEY", hidden)
    assert not _is_secret(repo / "operator" / "notes.md", hidden)
    alias = repo / "copy.txt"
    try:
        os.link(key, alias)
    except OSError:
        pass
    else:
        assert _is_secret(alias.resolve(), hidden)  # the same file under another name
    # Placement refuses a case variant of a protected folder the same way.
    with pytest.raises(ExportError, match="^export_path_invalid$"):
        _target(repo, "OPERATOR/lib", PACKAGE, (repo / "operator",))


def test_a_cli_integrate_signed_with_the_working_folders_keys_checks_with_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The host side of W7-1: a CLI command with no variable signs with ./.architect, and
    the re-check of the package it just placed (configured keys only) uses those keys too,
    as a session does. ``architect verify`` and ``export verify`` stay explicit."""
    from forge_modes_support import forge_repairs, make_profile, spec

    forge_repairs(monkeypatch)
    root = tmp_path / "host"
    root.mkdir()
    profile = make_profile(root, reply="nope")
    repo = tmp_path / "repo"
    repo.mkdir()
    approved = tmp_path / "approved.json"
    approved.write_text(spec(lifecycle=Lifecycle.INSTALL).model_dump_json(), encoding="utf-8")
    _victim(monkeypatch, root)
    public = trust.generate_key(Path(".architect") / trust.ED25519_KEY_NAME)
    trust.add_trusted_key(public, "this host", Path(".architect") / trust.TRUST_FILE_NAME)
    code = main(["forge", "integrate", "Return ok", "--profile", str(profile), "--repo",
                 str(repo), "--into", "lib", "--forge-mode", "template", "--approved-spec",
                 str(approved), "--package-only", "--yes"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    result = json.loads(captured.out)
    assert result["codes"] == [] and result["export"]["evidence_label"] == "ed25519_signed"
    placed = Path(result["export"]["package_path"])
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    # A verification command stays explicit: the shipped key alone, until named.
    assert main(["verify", str(placed)]) == 1
    assert capsys.readouterr().out.startswith("refused: key_untrusted")
    assert main(["verify", "--workspace", ".architect", str(placed)]) == 0
    assert capsys.readouterr().out.startswith("verified")
