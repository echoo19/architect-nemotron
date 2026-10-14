"""W7 re-attack: a second pass over the Ed25519 trust path after the W7-1..W7-7 fixes.

Each test states a property the trust path must keep, written as a passing regression
test. The new ground this pass covers:

- a key or a trust list that lies inside the folder being checked is refused as
  ``trust_in_target`` even when it is named by a Windows spelling that ``Path.resolve``
  leaves looking like another place: a UNC share, a loopback address, an extended-length
  prefix (``\\\\?\\C:\\...``) or a device path. Before the fix, naming the in-package key
  that way slipped past ``trust_in_target`` and the package verified under its own planted
  key;
- the Pi extension's own path guard refuses those same spellings of the trust files
  (defense in depth, real Pi);
- a small-order key refused by W7-3 in base64 raw form is refused in PEM form too;
- manifest names carrying an alternate-data-stream suffix, a drive or UNC spelling or a
  control byte stay unreadable (W7-6);
- the no-regression side: a UNC spelling of an external workspace still vouches, and an
  ordinary key wrapped in PEM with surrounding text still loads.

Offline: fake provider, sockets blocked, throwaway keys made at run time.
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from forge_modes_support import block_sockets
from test_w7_trust_attacks import (
    PACKAGE,
    WINDOWS,
    _bench_key,
    _bench_package,
    _clean_host,
    _manifest_body,
    _manifest_seal,
    _report_path,
    _reseal_report,
    _Script,
    _tool,
    _trust_files,
    _write_manifest,
)
from test_w7_trust_findings import _plant_hmac, _tampered, _victim
from trust_support import trust_on, use_host

from architect import trust
from architect.backends import pi as pi_module
from architect.backends.pi import _NODE, PiBackend, PiConfig
from architect.cli import main
from architect.forge_modes import ExportedHarness, ExportError
from architect.forge_modes.export import check_package, read_pin
from architect.presets import get_preset
from architect.runtime import Runtime
from architect.spec import Budget, FloorDefinition, FloorKind, Spec
from architect.telemetry import TelemetryStore

_BS = chr(92)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _unc_forms(path: Path) -> dict[str, str]:
    """Spellings of an existing Windows ``path`` that name the same file but that
    ``Path.resolve`` keeps written differently from the drive form."""
    text = str(path.resolve())
    drive, rest = text[0], text[2:]
    return {
        "extended": _BS * 2 + "?" + _BS + text,
        "unc-host": _BS * 2 + "localhost" + _BS + drive + "$" + rest,
        "unc-ip": _BS * 2 + "127.0.0.1" + _BS + drive + "$" + rest,
        "unc-extended": _BS * 2 + "?" + _BS + "UNC" + _BS + "localhost" + _BS + drive + "$" + rest,
    }


# ------------------------------------------------------------------ trust_in_target aliases


@pytest.mark.skipif(not WINDOWS, reason="Windows UNC and extended-length spellings")
@pytest.mark.parametrize("form", ["extended", "unc-host", "unc-ip", "unc-extended"])
def test_a_windows_alias_of_an_in_package_key_never_vouches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    """W7-1, the "inside the target" rule, re-attacked: the package ships the key its
    README tells the victim to point ARCHITECT_SIGNING_KEY_FILE at, but spelled as a UNC
    share, a loopback address or an extended-length path. Before the fix vouchers_inside
    compared the text of the resolved paths, which stays UNC for a UNC spelling, so the
    planted key was not seen as inside and the tampered package verified under it."""
    download, package, _ = _tampered(tmp_path, monkeypatch)
    key = package / "keys" / "signing.key"
    _plant_hmac(package, key)
    _victim(monkeypatch, download)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", _unc_forms(key)[form])
    pin = read_pin(package) or {}
    check = check_package(package, pin, registry=None)
    assert check.codes[:1] == ("trust_in_target",) and check.licensed is None, check
    with pytest.raises(ExportError, match="^trust_in_target$"):
        ExportedHarness(package, pin).load()
    assert main(["verify", f"lib/{PACKAGE}"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: trust_in_target"), out
    assert "lies inside the folder being checked" in out, out
    assert main(["export", "verify", f"lib/{PACKAGE}"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "trust_in_target"}


@pytest.mark.skipif(not WINDOWS, reason="Windows UNC and extended-length spellings")
@pytest.mark.parametrize("form", ["extended", "unc-host", "unc-extended"])
def test_a_windows_alias_workspace_of_in_package_keys_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    """The same evasion through ``--workspace``: a spelling of the package's own keys
    directory that is not textually inside it is still refused as trust_in_target."""
    download, package, _ = _tampered(tmp_path, monkeypatch)
    _plant_hmac(package, package / "keys" / "signing.key")
    _victim(monkeypatch, download)
    workspace = _unc_forms(package / "keys")[form]
    assert main(["verify", "--workspace", workspace, f"lib/{PACKAGE}"]) == 1
    assert capsys.readouterr().out.startswith("refused: trust_in_target")
    assert main(["export", "verify", "--workspace", workspace, f"lib/{PACKAGE}"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "trust_in_target"}


@pytest.mark.skipif(not WINDOWS, reason="Windows UNC and extended-length spellings")
@pytest.mark.parametrize("form", ["extended", "unc-host", "unc-extended"])
def test_a_windows_alias_of_an_in_package_trust_list_never_vouches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    """The trust-list half of the same rule, re-attacked with the same spellings. The
    list shipped inside the package, named through ARCHITECT_TRUST_FILE by a UNC or
    extended-length path, is still refused as trust_in_target."""
    download, package, digest = _tampered(tmp_path, monkeypatch)
    _victim(monkeypatch, download)
    mallory = tmp_path / "mallory.key"
    public = trust.generate_key(mallory)
    _reseal_report(_report_path(package), mallory)
    listed = package / "keys" / trust.TRUST_FILE_NAME
    trust.add_trusted_key(public, "architect", listed)
    body = _manifest_body(package, artifact_digest=digest)
    _write_manifest(package, body, _manifest_seal(body, mallory))
    monkeypatch.setenv("ARCHITECT_TRUST_FILE", _unc_forms(listed)[form])
    pin = read_pin(package) or {}
    assert check_package(package, pin, registry=None).codes[:1] == ("trust_in_target",)
    with pytest.raises(ExportError, match="^trust_in_target$"):
        ExportedHarness(package, pin).load()
    assert main(["verify", f"lib/{PACKAGE}"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("refused: trust_in_target") and "the trust list at" in out, out


# ------------------------------------------------------------------ no regression


@pytest.mark.skipif(not WINDOWS, reason="Windows UNC and extended-length spellings")
@pytest.mark.parametrize("form", ["extended", "unc-host", "unc-extended"])
def test_a_windows_alias_of_an_external_workspace_still_vouches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    """The fix must not over-refuse: a workspace outside the package, named by the same
    kind of spelling, still vouches. Identity only fires for a key inside the target."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    clean = _clean_host(tmp_path, monkeypatch, bench)
    _victim(monkeypatch, tmp_path)
    workspace = _unc_forms(clean)[form]
    assert main(["verify", "--workspace", workspace, str(package)]) == 0, capsys.readouterr().out
    assert capsys.readouterr().out.startswith("verified")


_SMALL_ORDER_RAW = (
    "01" + "00" * 31,  # identity
    "ec" + "ff" * 30 + "7f",  # order 2
    "00" * 32,  # order 4
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",  # order 8
    "ed" + "ff" * 30 + "7f",  # y = p (non-canonical identity)
)
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


@pytest.mark.parametrize("hexed", _SMALL_ORDER_RAW)
def test_a_small_order_key_in_pem_form_is_refused(hexed: str) -> None:
    """W7-3 reached the base64 raw path; the PEM path must refuse the same points. A PEM
    block is accepted with surrounding text (parse_public_key slices the first block), so
    a small-order key wrapped in PEM, even padded with junk, is still key_invalid."""
    der = _SPKI_PREFIX + bytes.fromhex(hexed)
    body = base64.b64encode(der).decode()
    pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----\n"
    for text in (pem, "handed to the operator\n" + pem + "\n" + pem):
        with pytest.raises(trust.TrustError, match="^key_invalid$"):
            trust.parse_public_key(text)


def test_a_valid_key_in_pem_form_with_surrounding_text_still_loads(tmp_path: Path) -> None:
    """The no-regression side: an ordinary key in a PEM block padded with junk and a
    second block still parses (to the first block's key), so the slicing is not the
    reason the small-order ones above are refused."""
    public = trust.generate_key(tmp_path / "bench.key")
    pem = trust.public_key_pem(public)
    reparsed = trust.parse_public_key("notes before\n" + pem + "notes after\n" + pem)
    assert trust.fingerprint(reparsed) == trust.fingerprint(public)


def test_require_key_normalizes_then_pins_the_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--require-key strips surrounding whitespace and lowercases, then demands exactly 64
    hex digits. A pasted value with a trailing newline or a leading space still names the
    key (it is re-validated after stripping, so there is no bypass); an interior space, a
    non-hex byte or the wrong length is invalid_arguments, never a near-match compared
    loosely against the seal."""
    package, bench = _bench_package(tmp_path, monkeypatch)
    private = trust.load_private_key(_bench_key(bench))
    assert private is not None
    good = trust.fingerprint(private.public_key())
    _clean_host(tmp_path, monkeypatch, bench)
    for spelled in (good + "\n", " " + good + "  ", "\t" + good.upper() + "\n"):
        assert main(["verify", "--require-key", spelled, str(package)]) == 0, repr(spelled)
        assert capsys.readouterr().out.startswith("verified")
    for spelled in (good[:32] + " " + good[33:], good[:-1] + "g", good + "0", good[:63], ""):
        assert main(["verify", "--require-key", spelled, str(package)]) == 1, repr(spelled)
        assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}, repr(spelled)


def test_a_published_folder_named_with_control_bytes_prints_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name a published manifest claims reaches the terminal as printable ASCII only.
    A name is validated as a plain slug, so an escape sequence there makes the manifest
    unreadable rather than printing raw; verify still exits clean and ASCII."""
    use_host(monkeypatch, tmp_path / "h")
    key = tmp_path / "release.key"
    trust_on(tmp_path / "h", trust.generate_key(key), "release")
    folder = tmp_path / "receipts"
    folder.mkdir()
    (folder / "scores.csv").write_text("model,score\nmine,0.9\n", encoding="ascii")
    trust.sign_published(folder, key=key)
    signed = (folder / trust.PUBLISHED_FILE).read_bytes()
    poisoned = signed.replace(b'"name":"receipts"', b'"name":"recei\\u001bpts"')
    (folder / trust.PUBLISHED_FILE).write_bytes(poisoned)
    assert main(["verify", str(folder)]) == 1
    out = capsys.readouterr().out
    assert out.isascii() and "\x1b" not in out, out


def test_a_manifest_name_with_a_stream_or_control_byte_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W7-6, re-attacked with the names the task named: an NTFS alternate-data-stream
    suffix, a drive or UNC spelling and a control byte in a listed name all make the
    manifest refuse to validate, so no such file is ever hashed or placed."""
    from pydantic import ValidationError

    from architect.forge_modes.export import ExportManifest

    package, _ = _bench_package(tmp_path, monkeypatch)
    data = json.loads((package / "MANIFEST.json").read_bytes())
    data.pop("ed25519")
    for name in ("report.json:evil", "a.txt::$DATA", "file:stream", "C:/win.ini",
                 _BS * 2 + "host" + _BS + "s", "note\x1bname", "tab\tfile", "x\x00y"):
        listed = {**data, "files": sorted([*data["files"], [name, "0" * 64]])}
        with pytest.raises(ValidationError):
            ExportManifest.model_validate(listed)


# ------------------------------------------------------------------ Pi extension (real Pi)


@pytest.mark.skipif(
    not WINDOWS
    or not (_NODE / "node_modules/@mariozechner/pi-coding-agent/package.json").is_file()
    or shutil.which("node") is None,
    reason="Windows UNC aliases and real Pi",
)
@pytest.mark.parametrize("form", ["unc-host", "unc-ip", "unc-extended"])
async def test_the_pi_extension_alone_refuses_unc_spellings_of_the_trust_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """Defense in depth: with the parent's path check switched off, the extension's own
    check must deny a UNC or loopback spelling of the trust list, as it denies short
    names. The Python guard already refuses these (the path is not under the repo); this
    pins the extension's own refusal, which the earlier fix left to short names alone."""
    repo, trust_file, _ = _trust_files(tmp_path, monkeypatch, "operator")
    listed = '{"keys": []}'
    trust_file.write_text(listed, encoding="ascii")
    monkeypatch.setattr(pi_module, "_granted_path", lambda *args: True)
    planted = '{"keys": [{"name": "model", "public_key": "' + "A" * 43 + '="}]}'
    target = _unc_forms(trust_file)[form]
    adapter = _Script([_tool("write", {"path": target, "content": planted}), "ok"])
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
