"""W7 attack review: the Ed25519 trust path against forged, re-signed and replayed files.

Each test is an attack that the trust path refuses today (docs/trust.md); it stays here
so the refusal cannot quietly regress. Attacks that still succeed are kept apart, as
expected failures, in ``test_w7_trust_findings.py``.

Covered: key substitution in a manifest or a report, fingerprints that share a prefix,
seals lifted from one signed file into another, signatures over another canonical form
(key order, whitespace, escapes, duplicate keys), file names that leave their folder,
links and junctions, a signed licence replayed for changed code, every seal stripped
(a downgrade to the HMAC), keys that are not Ed25519 public keys, a trust list that
arrives inside a package, and every spelling of the trust files a coding model might
use. Offline: fake provider, sockets blocked, throwaway keys made at run time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, rsa, x25519
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from forge_modes_support import block_sockets, make_profile, services_for, spec
from pydantic import JsonValue, ValidationError
from trust_support import compile_install, host_key, trust_on, use_host

from architect import trust
from architect._authority import _canonical_payload
from architect.backends.pi import _NODE, PiBackend, PiConfig, _granted_path
from architect.cli import main
from architect.forge_modes import ExportedHarness, ExportError, export_package, place_package
from architect.forge_modes.export import changed_files, check_package, read_manifest, read_pin
from architect.models import ModelRequest, ModelResponse, UsageQuote
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec, TestReport
from architect.telemetry import TelemetryStore
from architect.verdict import verify_path

PACKAGE = "receipt_reader"
WINDOWS = os.name == "nt"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


# ------------------------------------------------------------------ helpers


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bench_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A package exported on the bench host (Ed25519 key) and placed in a repository.
    Returns (package, bench host)."""
    bench = use_host(monkeypatch, tmp_path / "a")
    host_key(bench)
    entry, _, workspace = compile_install(bench)
    manifest, _ = export_package(entry, workspace / "exports", PACKAGE)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE, repo, "lib")
    return repo / "lib" / PACKAGE, bench


def _bench_key(bench: Path) -> Path:
    return bench / trust.ED25519_KEY_NAME


def _clean_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bench: Path) -> Path:
    """No HMAC key and no private key; the bench public key is its only trusted key."""
    private = trust.load_private_key(_bench_key(bench))
    assert private is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    trust_on(clean, private.public_key())
    return clean


def _mallory(tmp_path: Path) -> tuple[Path, str]:
    """An attacker's own key: (private key file, fingerprint). No host trusts it."""
    path = tmp_path / "mallory.key"
    return path, trust.fingerprint(trust.generate_key(path))


def _accepted(package: Path) -> None:
    pin = read_pin(package)
    assert check_package(package, pin, registry=None).codes == ()
    assert verify_path(package).verified


def _refused(package: Path, code: str) -> None:
    """``code`` leads every check: ``check_package``, a load and ``architect verify``."""
    pin = read_pin(package)
    check = check_package(package, pin, registry=None)
    assert check.codes[:1] == (code,), check.codes
    with pytest.raises(ExportError) as caught:
        ExportedHarness(package, pin or {}).load()
    assert caught.value.code == code
    verdict = verify_path(package)
    assert not verdict.verified and verdict.code == code, verdict


def _listing(package: Path) -> list[list[str]]:
    """Every file of the package as it is now, as an attacker regenerates the list."""
    return sorted(
        [path.relative_to(package).as_posix(), _sha256(path)]
        for path in package.rglob("*")
        if path.is_file() and path.relative_to(package).as_posix() != "MANIFEST.json"
    )


def _manifest_body(root: Path, **changes: Any) -> dict[str, Any]:
    """The manifest without its seal, re-listing the files on disk."""
    data = read_manifest(root)[0].model_dump(mode="json")
    data.pop("ed25519", None)
    data["files"] = _listing(root)
    data.update(changes)
    return data


def _write_manifest(package: Path, body: dict[str, Any], seal: object = None) -> None:
    data = dict(body) if seal is None else {**body, "ed25519": seal}
    (package / "MANIFEST.json").write_bytes(trust.canonical_json(data))


def _manifest_seal(body: dict[str, Any], key: Path, fingerprint: str | None = None) -> object:
    sealed = trust.seal("manifest", trust.canonical_json(body), key=key)
    assert sealed is not None
    value = sealed.model_dump(mode="json")
    if fingerprint is not None:
        value["fingerprint"] = fingerprint
    return value


def _report_path(package: Path) -> Path:
    return package / "artifact" / "report.json"


def _write_report(path: Path, report: TestReport) -> None:
    text = json.dumps(
        report.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
    )
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def _reseal_report(
    path: Path, key: Path, *, fingerprint: str | None = None, **changes: Any
) -> TestReport:
    """Change report values and seal them with ``key`` (the HMAC is left as it was)."""
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    report = report.model_copy(update={**changes, "ed25519": None})
    body = _canonical_payload({**report.signed_payload(), "signature": report.signature})
    sealed = trust._seal("report", body, key=key)
    assert sealed is not None
    if fingerprint is not None:
        sealed = sealed.model_copy(update={"fingerprint": fingerprint})
    report = report.model_copy(update={"ed25519": sealed})
    _write_report(path, report)
    return report


def _published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A folder signed by the release key, which this host trusts. (folder, key)."""
    use_host(monkeypatch, tmp_path / "h")
    key = tmp_path / "release.key"
    trust_on(tmp_path / "h", trust.generate_key(key), "release")
    folder = tmp_path / "receipts"
    (folder / "cases").mkdir(parents=True)
    (folder / "README.md").write_text("# Receipts\n", encoding="ascii")
    (folder / "cases" / "c01.json").write_text('{"score": 0.93}\n', encoding="ascii")
    trust.sign_published(folder, key=key)
    assert trust.check_published(folder).code is None
    return folder, key


def _junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


# ------------------------------------------------------------------ seals and fingerprints


def test_a_fingerprint_that_shares_a_prefix_names_another_key(tmp_path: Path) -> None:
    """Keys are found by the whole fingerprint; the 16 digits shown are display only."""
    key = tmp_path / "bench.key"
    bench = trust.generate_key(key)
    listed = trust.TrustList({trust.fingerprint(bench): trust.TrustedKey(
        trust.fingerprint(bench), "bench", "workspace", bench)}, tmp_path / "t.json")
    mallory = tmp_path / "mallory.key"
    other = trust.generate_key(mallory)
    payload = b'{"files":[]}'
    known = trust.fingerprint(bench)
    twin = known[:16] + trust.fingerprint(other)[16:]
    last = known[:63] + ("1" if known[63] == "0" else "0")
    for named in (twin, last, trust.fingerprint(other)):
        sealed = trust.seal("published", payload, key=mallory)
        assert sealed is not None
        forged = sealed.model_copy(update={"fingerprint": named})
        checked = trust.check_seal("published", payload, forged, listed)
        assert checked.status == "untrusted" and checked.signer is None, named
    # The bench key's own signature under an edited fingerprint is a changed file.
    genuine = trust.seal("published", payload, key=key)
    assert genuine is not None
    for named in (twin, last):
        edited = genuine.model_copy(update={"fingerprint": named})
        assert trust.check_seal("published", payload, edited, listed).status == "invalid"
    assert trust.check_seal("published", payload, genuine, listed).status == "valid"


def test_a_seal_names_its_key_in_exactly_one_spelling(tmp_path: Path) -> None:
    key = tmp_path / "bench.key"
    trust.generate_key(key)
    sealed = trust.seal("manifest", b"{}", key=key)
    assert sealed is not None
    good = sealed.model_dump(mode="json")
    fingerprint, signature = good["fingerprint"], good["signature"]
    for value in (
        {**good, "fingerprint": fingerprint.upper()},
        {**good, "fingerprint": fingerprint[:-1]},
        {**good, "fingerprint": fingerprint + "0"},
        {**good, "fingerprint": fingerprint + "\n"},
        {**good, "fingerprint": " " + fingerprint},
        {**good, "signature": signature.upper()},
        {**good, "signature": signature + "00"},
        {**good, "signature": base64.b64encode(bytes.fromhex(signature)).decode()},
        # A key never travels with its seal, in any field.
        {**good, "public_key": base64.b64encode(b"k" * 32).decode()},
        {"fingerprint": fingerprint},
    ):
        with pytest.raises(ValidationError):
            trust.Seal.model_validate(value)
        with pytest.raises(ValidationError):
            trust.Seal.model_validate_json(json.dumps(value))


def test_a_seal_made_for_one_kind_never_checks_as_another(tmp_path: Path) -> None:
    key = tmp_path / "bench.key"
    public = trust.generate_key(key)
    listed = trust.TrustList({trust.fingerprint(public): trust.TrustedKey(
        trust.fingerprint(public), "bench", "workspace", public)}, tmp_path / "t.json")
    payload = b'{"name":"receipts"}'
    kinds: tuple[trust.Kind, ...] = ("report", "manifest", "published")
    for made in kinds:
        sealed = trust._seal(made, payload, key=key)
        assert sealed is not None
        for checked in kinds:
            status = trust.check_seal(checked, payload, sealed, listed).status
            assert status == ("valid" if checked == made else "invalid"), (made, checked)
    # A signature over the bare bytes, with no context, is no seal of any kind.
    private = trust.load_private_key(key)
    assert private is not None
    bare = trust.Seal(
        fingerprint=trust.fingerprint(public), signature=private.sign(payload).hex()
    )
    for kind in kinds:
        assert trust.check_seal(kind, payload, bare, listed).status == "invalid"


def test_the_signed_bytes_of_every_kind_are_pinned() -> None:
    """Every seal already made (published results, exported packages) covers exactly these
    bytes. A change to the canonical form (escaping, float text, key order, a new field
    in the dump) silently breaks all of them, so it must come with a new context string."""
    from architect.forge_modes.export import ExportManifest, _canonical
    from architect.spec import CaseCounts

    report = TestReport(
        spec_hash="1" * 64, artifact_digest="2" * 64, passed=True, floor_score=0.95,
        floor_limit=0.9, floor_kind=FloorKind.HELD_OUT_TASKS, tokens=1234, cost_usd=1e-05,
        compute_s=12.0, component_count=2, failures=("budget",),
        verification_id="v-\u00fc-1", signature="3" * 64, held_out_count=100,
        mean_cost_usd=0.0001,
        held_out_counts=CaseCounts(cases=100, correct=95, invalid=1, over_budget=0),
    )
    body = _canonical_payload(report.sealed_payload())
    assert body == _canonical_payload({**report.signed_payload(), "signature": "3" * 64})
    assert body == (
        b'{"artifact_digest":"' + b"2" * 64 + b'","component_count":2,"compute_s":12.0,'
        b'"cost_usd":1e-05,"failures":["budget"],"floor_kind":"held_out_tasks",'
        b'"floor_limit":0.9,"floor_score":0.95,"held_out_count":100,"held_out_counts":'
        b'{"cases":100,"correct":95,"invalid":1,"over_budget":0},"mean_cost_usd":0.0001,'
        b'"passed":true,"secondary_floor_kind":null,"secondary_floor_limit":null,'
        b'"secondary_floor_score":null,"signature":"' + b"3" * 64 + b'","spec_hash":"'
        + b"1" * 64 + b'","tokens":1234,"verification_id":"v-\xc3\xbc-1"}'
    )
    message = trust._message("report", body)
    assert message.startswith(b"architect-signature-v1\x00report\x00{")
    assert hashlib.sha256(message).hexdigest() == (
        "2da2895f046863ca0c124deb25a042a3b4ef90c0e681e58588e8ac57d74f71a0"
    )
    # Optional fields stay out of the signed bytes while they are unset.
    bare = report.model_copy(update={"held_out_counts": None, "mean_cost_usd": None})
    assert b"held_out_counts" not in _canonical_payload(bare.sealed_payload())
    assert b'"ed25519"' not in _canonical_payload(
        report.model_copy(update={"ed25519": None}).sealed_payload()
    )
    manifest = ExportManifest(
        package_format=1, architect_version="0.1.0", package="receipt_reader",
        spec_hash="1" * 64, artifact_digest="2" * 64, verification_id="v-1",
        execution_mode="native", children={},
        files=(("EVIDENCE.json", "4" * 64), ("artifact/harness.py", "5" * 64)),
    )
    assert _canonical(manifest.model_dump(mode="json")) == (
        b'{"architect_version":"0.1.0","artifact_digest":"' + b"2" * 64 + b'","children":{},'
        b'"execution_mode":"native","files":[["EVIDENCE.json","' + b"4" * 64 + b'"],'
        b'["artifact/harness.py","' + b"5" * 64 + b'"]],"package":"receipt_reader",'
        b'"package_format":1,"spec_hash":"' + b"1" * 64 + b'","verification_id":"v-1"}'
    )
    folder = trust.canonical_json(
        {"files": [["README.md", "6" * 64]], "format": "architect-published-v1",
         "name": "receipts"}
    )
    assert trust._message("published", folder) == (
        b'architect-signature-v1\x00published\x00{"files":[["README.md","' + b"6" * 64
        + b'"]],"format":"architect-published-v1","name":"receipts"}'
    )


def _foreign_keys() -> dict[str, str]:
    """Key files that are not an Ed25519 public key, as an operator might be handed."""
    pem = serialization.Encoding.PEM
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    ed25519 = Ed25519PrivateKey.generate()
    raw = trust.raw_public_key(ed25519.public_key())
    return {
        "rsa": rsa.generate_private_key(65537, 2048).public_key().public_bytes(pem, spki).decode(),
        "p256": ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            pem, spki).decode(),
        "ed448": ed448.Ed448PrivateKey.generate().public_key().public_bytes(pem, spki).decode(),
        "x25519": x25519.X25519PrivateKey.generate().public_key().public_bytes(
            pem, spki).decode(),
        "ed25519-private": ed25519.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        "short": base64.b64encode(raw[:31]).decode(),
        "long": base64.b64encode(raw + b"\x00").decode(),
        "urlsafe": base64.urlsafe_b64encode(b"\xfb\xff" + raw[2:]).decode(),
        "split": base64.b64encode(raw).decode()[:20] + "\n" + base64.b64encode(raw).decode()[20:],
        "hex": raw.hex(),
        "empty": "",
    }


def test_only_ed25519_public_keys_enter_a_trust_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = use_host(monkeypatch, tmp_path / "h")
    for name, text in _foreign_keys().items():
        with pytest.raises(trust.TrustError, match="^key_invalid$"):
            trust.parse_public_key(text)
        path = tmp_path / f"{name}.pub"
        path.write_text(text, encoding="ascii")
        assert main(["keys", "trust", str(path), "--name", "partner"]) == 1, name
        assert json.loads(capsys.readouterr().err) == {"error": "key_invalid"}, name
        assert not (host / trust.TRUST_FILE_NAME).exists(), name
        # Written by hand, one such entry makes the whole list unreadable: nothing is
        # trusted, not even the keys listed next to it.
        good = base64.b64encode(trust.raw_public_key(trust.generate_key(
            tmp_path / f"{name}.good.key"))).decode()
        listing = {"keys": [{"name": "good", "public_key": good},
                            {"name": "partner", "public_key": text}]}
        (host / trust.TRUST_FILE_NAME).write_text(json.dumps(listing), encoding="ascii")
        loaded = trust.load_trust()
        assert loaded.error == "trust_list_invalid" and dict(loaded.keys) == {}, name
        (host / trust.TRUST_FILE_NAME).unlink()


def test_a_trust_file_that_is_not_plain_json_trusts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = use_host(monkeypatch, tmp_path / "h")
    key = tmp_path / "bench.key"
    good = base64.b64encode(trust.raw_public_key(trust.generate_key(key))).decode()
    entry = {"name": "bench", "public_key": good}
    path = host / trust.TRUST_FILE_NAME
    for raw in (
        b"\xef\xbb\xbf" + json.dumps({"keys": [entry]}).encode(),
        b'{"keys": [' + json.dumps(entry).encode() + b",]}",
        b'// trusted\n{"keys": []}',
        b'{"keys": [], "trust_all": true}',
        b'{"keys": "*"}',
        b'{"keys": [{"name": "bench", "public_key": "' + good.encode() + b'", "fp": "x"}]}',
        b'{"keys": [{"name": "../../x", "public_key": "' + good.encode() + b'"}]}',
        b'{"shipped": "maybe", "keys": []}',
        b"[]",
        b"",
    ):
        path.write_bytes(raw)
        loaded = trust.load_trust()
        assert loaded.error == "trust_list_invalid" and dict(loaded.keys) == {}, raw
    path.unlink()
    path.mkdir()  # a folder in its place
    loaded = trust.load_trust()
    assert loaded.error == "trust_list_invalid" and dict(loaded.keys) == {}


# ------------------------------------------------------------------ published folders


def test_signed_json_counts_only_in_its_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, _ = _published(tmp_path, monkeypatch)
    path = folder / trust.PUBLISHED_FILE
    signed = path.read_bytes()
    data = json.loads(signed)
    forged = json.dumps([[name, "0" * 64] for name, _ in data["files"]], separators=(",", ":"))
    variants = {
        "pretty": json.dumps(data, indent=2, sort_keys=True).encode(),
        "reordered": json.dumps(dict(reversed(list(data.items()))),
                                separators=(",", ":")).encode(),
        "spaced": json.dumps(data, sort_keys=True).encode(),
        "escaped": signed.replace(b'"receipts"', b'"\\u0072eceipts"'),
        "newline": signed + b"\n",
        "bom": b"\xef\xbb\xbf" + signed,
        # Two lists: a parser that keeps the first would read the forged one.
        "duplicate": signed.replace(
            b'{"ed25519"', b'{"files":' + forged.encode() + b',"ed25519"', 1
        ),
    }
    for name, raw in variants.items():
        assert raw != signed, name
        path.write_bytes(raw)
        result = trust.check_published(folder, name="receipts")
        assert result.code == "artifact_changed", name
        assert not verify_path(folder).verified, name
    path.write_bytes(signed)
    assert trust.check_published(folder, name="receipts").code is None


@pytest.mark.parametrize(
    "name",
    ["../outside.txt", "/etc/passwd", "C:/x.txt", "C:x.txt", "a/../README.md", "./README.md",
     "cases//c01.json", "cases\\c01.json", "cases/", ".hidden", "caf\u00e9.txt", " a.txt",
     "a.txt\n", "", "x" * 101],
)
def test_signed_file_names_never_leave_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Even a list the trusted key itself signed is refused when a name is not plain."""
    folder, key = _published(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("not published\n", encoding="ascii")
    files = sorted([*json.loads((folder / trust.PUBLISHED_FILE).read_bytes())["files"],
                    [name, _sha256(outside)]])
    body = trust.canonical_json(
        {"files": files, "format": "architect-published-v1", "name": "receipts"}
    )
    sealed = trust.seal("published", body, key=key)
    assert sealed is not None
    signed = json.loads(body)
    signed["ed25519"] = sealed.model_dump(mode="json")
    (folder / trust.PUBLISHED_FILE).write_bytes(trust.canonical_json(signed))
    result = trust.check_published(folder, name="receipts")
    assert result.code == "artifact_changed" and result.changed == (trust.PUBLISHED_FILE,)
    verdict = verify_path(folder)
    assert not verdict.verified and verdict.code == "artifact_changed"
    assert all(line.isascii() and ".." not in line for line in verdict.lines)
    with pytest.raises(ValidationError):
        trust.PublishedManifest.model_validate_json(trust.canonical_json(signed))


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions")
def test_junctions_in_or_as_a_published_folder_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, key = _published(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "extra.json").write_text("{}\n", encoding="ascii")
    _junction(outside, folder / "more")
    result = trust.check_published(folder, name="receipts")
    assert result.code == "artifact_changed" and "1 unexpected entries" in result.changed
    with pytest.raises(trust.TrustError, match="^published_invalid$"):
        trust.sign_published(folder, key=key)
    os.rmdir(folder / "more")
    assert trust.check_published(folder, name="receipts").code is None
    # The folder itself behind a junction: never followed.
    alias = tmp_path / "alias"
    _junction(folder, alias)
    assert trust.check_published(alias).code == "published_invalid"
    assert not verify_path(alias).verified
    with pytest.raises(trust.TrustError, match="^published_invalid$"):
        trust.sign_published(alias, key=key)


def test_symlinks_in_a_published_folder_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, key = _published(tmp_path, monkeypatch)
    target = folder / "cases" / "c01.json"
    try:
        os.symlink(target, folder / "cases" / "c02.json")
    except OSError:
        pytest.skip("this host cannot create symbolic links")
    result = trust.check_published(folder, name="receipts")
    assert result.code == "artifact_changed" and "1 unexpected entries" in result.changed
    with pytest.raises(trust.TrustError, match="^published_invalid$"):
        trust.sign_published(folder, key=key)


# ------------------------------------------------------------------ manifests


def test_a_manifest_re_signed_by_another_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code added to ``__init__.py`` (it runs at import) with a fresh file list."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    _clean_host(tmp_path, monkeypatch, bench)
    _accepted(package)
    mallory, mallory_fp = _mallory(tmp_path)
    init = package / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8") + "_PLANTED = True\n", encoding="utf-8")
    body = _manifest_body(package)
    # The attacker's key under its own name: this host does not trust it.
    _write_manifest(package, body, _manifest_seal(body, mallory))
    _refused(package, "key_untrusted")
    # The attacker's signature under the bench key's name: a changed file.
    bench_private = trust.load_private_key(_bench_key(bench))
    assert bench_private is not None
    bench_fp = trust.fingerprint(bench_private.public_key())
    _write_manifest(package, body, _manifest_seal(body, mallory, bench_fp))
    _refused(package, "artifact_changed")
    assert mallory_fp != bench_fp
    # No seal at all: the licence is sealed, so the other files must be too.
    _write_manifest(package, body)
    _refused(package, "unsigned")


def test_a_seal_lifted_from_another_signed_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    key = _bench_key(bench)
    # Genuine seals by the trusted key, each made for something else.
    report = json.loads(_report_path(package).read_text(encoding="utf-8"))
    folder = tmp_path / "pub"
    folder.mkdir()
    (folder / "notes.md").write_text("notes\n", encoding="ascii")
    published = trust.sign_published(folder, key=key).ed25519.model_dump(mode="json")
    second = package.parent.parent / "lib2"
    second.mkdir()
    shutil.copytree(package, second / "other_reader")
    other = second / "other_reader"
    other_body = _manifest_body(other, package="other_reader")
    other_seal = _manifest_seal(other_body, key)
    _clean_host(tmp_path, monkeypatch, bench)
    manifest = (package / "MANIFEST.json").read_bytes()
    body = _manifest_body(package)
    for seal in (report["ed25519"], published, other_seal):
        _write_manifest(package, body, seal)
        _refused(package, "artifact_changed")
    (package / "MANIFEST.json").write_bytes(manifest)
    _accepted(package)
    # The bench key did sign the copy under its own name; under this name it is refused.
    _write_manifest(other, other_body, other_seal)
    assert check_package(other, read_pin(other), registry=None).codes == ()
    shutil.rmtree(package)
    shutil.copytree(other, package)
    _refused(package, "export_pin_mismatch")


def test_a_signed_manifest_counts_only_in_its_canonical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    _clean_host(tmp_path, monkeypatch, bench)
    path = package / "MANIFEST.json"
    signed = path.read_bytes()
    data = json.loads(signed)
    forged = [[name, digest] for name, digest in data["files"]]
    forged[0][1] = "0" * 64
    variants = {
        "pretty": json.dumps(data, indent=2, sort_keys=True).encode(),
        "reordered": json.dumps(dict(reversed(list(data.items()))),
                                separators=(",", ":")).encode(),
        "escaped": signed.replace(b'"receipt_reader"', b'"\\u0072eceipt_reader"'),
        "newline": signed + b"\n",
        # The forged list first, the signed one last: one parser in each camp.
        "duplicate": signed.replace(
            b'{"architect_version"',
            b'{"files":' + json.dumps(forged, separators=(",", ":")).encode()
            + b',"architect_version"', 1,
        ),
    }
    for name, raw in variants.items():
        assert raw != signed, name
        path.write_bytes(raw)
        _refused(package, "artifact_changed")
    path.write_bytes(signed)
    _accepted(package)


# ------------------------------------------------------------------ reports and replays


def test_a_report_re_signed_by_another_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    bench_private = trust.load_private_key(_bench_key(bench))
    assert bench_private is not None
    bench_fp = trust.fingerprint(bench_private.public_key())
    _clean_host(tmp_path, monkeypatch, bench)
    mallory, _ = _mallory(tmp_path)
    path = _report_path(package)
    original = path.read_bytes()
    manifest = (package / "MANIFEST.json").read_bytes()
    for fingerprint, code in ((None, "key_untrusted"), (bench_fp, "artifact_changed")):
        _reseal_report(path, mallory, fingerprint=fingerprint, held_out_count=400)
        # With the bench manifest seal kept (now stale), then with a fresh unsealed list.
        _refused(package, "artifact_changed")
        _write_manifest(package, _manifest_body(package))
        _refused(package, code)
        path.write_bytes(original)
        (package / "MANIFEST.json").write_bytes(manifest)
        _accepted(package)


def test_another_unicode_normal_form_is_a_changed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same text to a reader, other code points to the seal: NFD for NFC is refused."""
    import unicodedata

    package, bench = _bench_package(tmp_path, monkeypatch)
    key = _bench_key(bench)
    path = _report_path(package)
    word = "caf\u00e9-r\u00e9sum\u00e9"
    _reseal_report(path, key, verification_id=word)  # the bench key signs non-ASCII text
    body = _manifest_body(package)
    _write_manifest(package, body, _manifest_seal(body, key))
    _clean_host(tmp_path, monkeypatch, bench)
    _accepted(package)
    decomposed = unicodedata.normalize("NFD", word)
    assert decomposed != word and unicodedata.normalize("NFC", decomposed) == word
    text = path.read_text(encoding="utf-8")
    assert word in text
    for changed in (decomposed, word.replace("\u00e9", "\uff45\u0301")):
        path.write_text(text.replace(word, changed), encoding="utf-8", newline="\n")
        # Re-sealed by the bench key, the manifest leaves only the report's seal to judge.
        body = _manifest_body(package)
        _write_manifest(package, body, _manifest_seal(body, key))
        _refused(package, "artifact_changed")


def test_the_signed_licence_never_covers_changed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    _clean_host(tmp_path, monkeypatch, bench)
    mallory, mallory_fp = _mallory(tmp_path)
    harness = package / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    # 1. Only the code changed.
    _refused(package, "artifact_changed")
    # 2. The file list regenerated, unsealed: the sealed report still names the old code.
    _write_manifest(package, _manifest_body(package))
    _refused(package, "artifact_changed")
    # 3. ... or sealed by the attacker's key.
    body = _manifest_body(package)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    _refused(package, "artifact_changed")
    # 4. The report's digest moved to the new code: its bench seal no longer verifies.
    from architect.spec import HarnessArtifact

    digest = HarnessArtifact.load(package / "artifact").artifact_digest
    report = TestReport.model_validate_json(_report_path(package).read_text(encoding="utf-8"))
    _write_report(_report_path(package), report.model_copy(update={"artifact_digest": digest}))
    _write_manifest(package, _manifest_body(package))
    _refused(package, "artifact_changed")
    # 5. Everything re-pointed at the new code and re-sealed by the attacker: the pins,
    # the manifest and the licence agree, and it is an untrusted licence, never a valid one.
    old = read_manifest(package)[0].artifact_digest
    init = package / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(old, digest), encoding="utf-8")
    sealed = _reseal_report(_report_path(package), mallory)
    assert sealed.ed25519 is not None and sealed.ed25519.fingerprint == mallory_fp
    body = _manifest_body(package, artifact_digest=digest)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    _refused(package, "key_untrusted")


async def _second_artifact(root: Path) -> Path:
    """Another licensed artifact on the same host, for a different spec."""
    services = services_for(make_profile(root))
    try:
        compiled = await services.compiler.compile(
            spec(task="Return ok, a second time", lifecycle=Lifecycle.INSTALL), approved=True
        )
        assert compiled.artifact is not None and compiled.artifact.licensed
        return compiled.artifact.directory
    finally:
        services.close()


def test_a_sealed_licence_of_another_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    use_host(monkeypatch, bench)
    (bench / "second").mkdir()
    other = asyncio.run(_second_artifact(bench / "second"))
    lifted = (other / "report.json").read_bytes()
    assert b'"ed25519"' in lifted
    _clean_host(tmp_path, monkeypatch, bench)
    _report_path(package).write_bytes(lifted)
    _refused(package, "artifact_changed")
    _write_manifest(package, _manifest_body(package))
    _refused(package, "artifact_changed")


def test_stripping_every_seal_never_downgrades_to_a_licence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the exporting host's HMAC key a seal-less licence is never a licence."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    path = _report_path(package)
    report = TestReport.model_validate_json(path.read_text(encoding="utf-8"))
    _write_report(path, report.model_copy(update={"ed25519": None}))
    _write_manifest(package, _manifest_body(package))
    # A clean host that trusts the bench key and holds no HMAC key.
    clean = _clean_host(tmp_path, monkeypatch, bench)
    _refused(package, "license_unverifiable")
    # A host with an HMAC key of its own: that key did not sign the report.
    (clean / "signing.key").write_bytes(os.urandom(32))
    _refused(package, "export_unverified")
    assert main(["verify", str(path)]) == 1
    assert capsys.readouterr().out.startswith("refused: invalid_license")
    # A changed value with the seal gone and a forged HMAC is refused the same way.
    forged = report.model_copy(update={"ed25519": None, "signature": "0" * 64})
    _write_report(path, forged)
    _write_manifest(package, _manifest_body(package))
    _refused(package, "export_unverified")


def test_a_trust_list_shipped_inside_a_package_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that arrives with the files is not trusted, wherever in the files it sits."""
    from architect.forge_modes.export import ExportManifest

    package, bench = _bench_package(tmp_path, monkeypatch)
    mallory, forged = _mallory(tmp_path)
    public = trust.load_private_key(mallory)
    assert public is not None
    planted = {"shipped": False, "keys": [{"name": "bench", "public_key": base64.b64encode(
        trust.raw_public_key(public.public_key())).decode()}]}
    (package / trust.TRUST_FILE_NAME).write_text(json.dumps(planted), encoding="ascii")
    _reseal_report(_report_path(package), mallory)
    body = _manifest_body(package)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    clean = _clean_host(tmp_path, monkeypatch, bench)
    _refused(package, "key_untrusted")
    # In a .architect folder of the package no manifest can even list it (W7-6: names
    # are plain), so the files stay unlisted and the package is changed.
    for folder in (package / ".architect", package / "artifact" / ".architect"):
        folder.mkdir()
        (folder / trust.TRUST_FILE_NAME).write_text(json.dumps(planted), encoding="ascii")
    listed = {**body, "files": _listing(package)}
    with pytest.raises(ValidationError):
        ExportManifest.model_validate(listed)
    _write_manifest(package, listed, _manifest_seal(listed, mallory))
    _refused(package, "export_integrity_mismatch")
    # The clean host's own list is unchanged: its bench key beside the shipped one, and
    # never the planted key.
    trusted = trust.load_trust().keys
    assert [key.name for key in trusted.values() if key.source == "workspace"] == ["bench"]
    assert forged not in trusted
    assert trust.trust_file_path() == clean / trust.TRUST_FILE_NAME


def test_verify_with_a_workspace_ignores_a_list_in_the_current_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--workspace`` names whose keys count, whatever folder the shell is in."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    mallory, _ = _mallory(tmp_path)
    _reseal_report(_report_path(package), mallory)
    body = _manifest_body(package)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    download = package.parent.parent
    planted = download / ".architect"
    planted.mkdir()
    private = trust.load_private_key(mallory)
    assert private is not None
    trust.add_trusted_key(private.public_key(), "bench", planted / trust.TRUST_FILE_NAME)
    clean = _clean_host(tmp_path, monkeypatch, bench)
    for name in ("ARCHITECT_SIGNING_KEY_FILE", "ARCHITECT_TRUST_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(download)
    assert main(["verify", "--workspace", str(clean), str(package)]) == 1
    assert capsys.readouterr().out.startswith("refused: key_untrusted")


# ------------------------------------------------------------------ placement and links


def test_placement_never_writes_outside_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _ = _bench_package(tmp_path, monkeypatch)
    staged = tmp_path / "staged" / PACKAGE
    shutil.copytree(package, staged)
    escape = tmp_path / "staged" / "escape.py"
    escape.write_text("PLANTED = True\n", encoding="ascii")
    genuine = _manifest_body(staged)
    for name in ("../escape.py", "artifact/../../escape.py", str(escape)):
        body = {**genuine, "files": sorted([*genuine["files"], [name, _sha256(escape)]])}
        _write_manifest(staged, body)
        target = tmp_path / f"repo{len(name)}"
        target.mkdir()
        with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
            place_package(staged, target, "vendor/lib")
        assert [path for path in target.rglob("*")] == [], name
        # Such a manifest is not even read (W7-6), so no file outside is ever hashed.
        with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
            read_manifest(staged)
    _write_manifest(staged, genuine)
    assert changed_files(staged, read_manifest(staged)[0]) == ()


@pytest.mark.skipif(not WINDOWS, reason="NTFS junctions")
def test_junctions_in_or_as_a_package_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, bench = _bench_package(tmp_path, monkeypatch)
    _clean_host(tmp_path, monkeypatch, bench)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "helpers.py").write_text("PLANTED = True\n", encoding="ascii")
    _junction(outside, package / "helpers")
    _refused(package, "artifact_changed")
    os.rmdir(package / "helpers")
    _accepted(package)
    # The artifact folder swapped for a junction to an identical copy elsewhere.
    copy = tmp_path / "copy"
    shutil.copytree(package / "artifact", copy)
    shutil.rmtree(package / "artifact")
    _junction(copy, package / "artifact")
    assert check_package(package, read_pin(package), registry=None).codes[:1] == (
        "artifact_changed",
    )
    assert not verify_path(package).verified


# ------------------------------------------------------------------ model file tools


def _short_name(path: Path) -> str | None:
    """The 8.3 alias Windows keeps for the last part of ``path``, when it keeps one."""
    if not WINDOWS:
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024):
        return None
    alias = Path(buffer.value).name
    return alias if alias.casefold() != path.name.casefold() else None


def _spellings(repo: Path, relative: str) -> list[str]:
    """Other names for ``repo/relative``: separators, dots and the absolute path; on
    Windows (case-insensitive) also case, trailing dots and spaces, streams, 8.3 aliases."""
    path = repo / relative
    parent = path.parent.relative_to(repo).as_posix()
    names = [relative.replace("/", "\\"), f"./{relative}", f"{parent}/../{relative}", str(path)]
    if WINDOWS:
        names += [relative.upper(), str(path).upper(), relative + ".", relative + " ",
                  relative + "::$DATA", relative + ":x"]
        alias = _short_name(path)
        if alias is not None:
            names.append(f"{parent}/{alias}")
        folder = _short_name(path.parent)
        if folder is not None:
            above = path.parent.parent.relative_to(repo).as_posix()
            names.append(f"{above}/{folder}/{path.name}".removeprefix("./"))
    return names


def _trust_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> tuple[Path, Path, Path]:
    """A repository holding this host's trust list and Ed25519 key: in its ``.architect``
    workspace, or in an operator folder outside it. (repo, trust list, key)."""
    repo = (tmp_path / "repo").resolve()
    folder = repo / (".architect" if layout == "workspace" else "operator")
    folder.mkdir(parents=True)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(folder / "signing.key"))
    return repo, folder / trust.TRUST_FILE_NAME, folder / trust.ED25519_KEY_NAME


@pytest.mark.parametrize("layout", ["workspace", "operator"])
def test_pi_path_guard_refuses_every_spelling_of_the_trust_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    """Pi file tools never reach the trust list or the Ed25519 key, however named."""
    repo, trust_file, key = _trust_files(tmp_path, monkeypatch, layout)
    # The paths PiBackend.run denies for these files, resolved as it resolves them.
    protected = (
        Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"]), *trust.protected_paths(),
        repo / ".architect",
    )
    denied = tuple(path.resolve() for path in protected)
    trust_file.write_text('{"keys": []}', encoding="ascii")
    key.write_text("PRIVATE", encoding="ascii")
    for target in (trust_file, key):
        relative = target.relative_to(repo).as_posix()
        for raw in [relative, *_spellings(repo, relative)]:
            for tool_name in ("read", "write", "edit"):
                granted = _granted_path(repo, raw, tool_name, 1_048_576, denied)
                assert granted is False, (raw, tool_name)
    # Creating either file where it does not exist yet is refused the same way.
    trust_file.unlink()
    key.unlink()
    for target in (trust_file, key):
        relative = target.relative_to(repo).as_posix()
        for raw in [relative, *_spellings(repo, relative)]:
            assert _granted_path(repo, raw, "write", 1_048_576, denied) is False, raw


def _conductor(cwd: Path, workspace: Path) -> Any:
    """The agent CLI's tool context, as far as its read-only file tools look at it."""
    from types import SimpleNamespace

    from architect.agent.tools import ToolContext

    return ToolContext(
        services=SimpleNamespace(workspace=workspace),  # type: ignore[arg-type]
        cwd=cwd, ui=SimpleNamespace(), model="fake",
        extras={"workspace": workspace},
    )


def _read(context: Any, raw: str) -> JsonValue:
    """The agent CLI's ``read_file`` tool, run once."""
    from architect.agent.tools import _read_file

    return asyncio.run(_read_file({"path": raw}, context))


def test_the_conductor_never_reads_the_ed25519_key_by_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent CLI's read_file hides the private key wherever it is configured; the
    trust list holds public keys only and stays readable."""
    from architect.agent.tools import ToolError

    cwd = tmp_path.resolve()
    workspace = cwd / ".architect"
    workspace.mkdir()
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(workspace / "signing.key"))
    key = workspace / trust.ED25519_KEY_NAME
    key.write_text("SYNTHETIC-ED25519-PRIVATE-KEY", encoding="ascii")
    (workspace / trust.TRUST_FILE_NAME).write_text('{"keys": []}', encoding="ascii")
    context = _conductor(cwd, workspace)
    relative = key.relative_to(cwd).as_posix()
    for raw in [relative, *_spellings(cwd, relative)]:
        if ":" in raw[2:] or raw.endswith((".", " ")):
            continue  # no such file to read: refused for another reason
        with pytest.raises(ToolError) as failure:
            _read(context, raw)
        assert failure.value.code == "denied_path", raw
    shown = _read(context, ".architect/trusted-keys.json")
    assert "keys" in json.dumps(shown)
    # Moved by ARCHITECT_ED25519_KEY_FILE, it is still hidden.
    moved = cwd / "keys" / "release.key"
    moved.parent.mkdir()
    moved.write_text("SYNTHETIC-ED25519-PRIVATE-KEY", encoding="ascii")
    monkeypatch.setenv("ARCHITECT_ED25519_KEY_FILE", str(moved))
    with pytest.raises(ToolError, match="^denied_path$"):
        _read(context, "keys/release.key")


class _Script:
    """A scripted provider; Pi, its tools and the policy hooks are all real."""

    def __init__(self, texts: list[str]) -> None:
        self.model = "fake"
        self.texts = texts
        self.calls = 0
        self.requests: list[ModelRequest] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=4, tokens_out=0, cost_usd=0.01)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text = self.texts[self.calls]
        self.calls += 1
        self.requests.append(request)
        return ModelResponse(text=text, tokens_in=4, tokens_out=0, cost_usd=0.01, model="fake")


def _tool(name: str, arguments: dict[str, JsonValue]) -> str:
    return json.dumps({"tool_calls": [{"id": "call-1", "name": name, "arguments": arguments}]})


@pytest.mark.skipif(
    not (_NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file()
    or shutil.which("node") is None,
    reason="real Pi needs npm ci in architect/backends/pi-node",
)
@pytest.mark.parametrize("layout", ["workspace", "operator"])
async def test_real_pi_cannot_reach_the_trust_files_by_another_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    repo, trust_file, key = _trust_files(tmp_path, monkeypatch, layout)
    listed = '{"keys": []}'
    trust_file.write_text(listed, encoding="ascii")
    secret = "SYNTHETIC-ED25519-PRIVATE-KEY-32X"
    key.write_text(secret, encoding="ascii")
    planted = '{"keys": [{"name": "model", "public_key": "' + "A" * 43 + '="}]}'
    relative = trust_file.relative_to(repo).as_posix()
    calls = [_tool("write", {"path": name, "content": planted})
             for name in [relative, *_spellings(repo, relative)]]
    relative = key.relative_to(repo).as_posix()
    calls += [_tool("read", {"path": name}) for name in [relative, *_spellings(repo, relative)]]
    adapter = _Script([*calls, "ok"])
    task = Spec(
        task="Perform the supplied task without granting new authority",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        floor=FloorDefinition(
            kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="trusted-floor", minimum_samples=2
        ),
        budget=Budget(cost_usd=10, compute_s=120),
        tools=("read", "write", "edit"),
    )
    with TelemetryStore(tmp_path / "telemetry") as store:
        result = await Runtime(store, pi_backend=PiBackend(PiConfig())).evaluate(
            task, get_preset("coder").build(task), {"repo": str(repo)}, adapter
        )
    assert result.output == "ok" and adapter.calls == len(calls) + 1
    assert trust_file.read_text(encoding="ascii") == listed, "Pi rewrote the trust list"
    assert sorted(path.name for path in trust_file.parent.iterdir()) == [
        trust.ED25519_KEY_NAME, trust.TRUST_FILE_NAME,
    ], "Pi created a file beside the trust files"
    assert all(secret not in request.prompt for request in adapter.requests), "key exposed"
