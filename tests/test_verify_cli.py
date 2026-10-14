"""W7: ``architect verify`` and ``architect keys`` (``architect.verdict``, ``architect.cli``).

The verdict is ``verified`` or ``refused: <code>`` and a few plain ASCII lines; the exit
code is 0 only for ``verified``. Offline: fake provider, sockets blocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from forge_modes_support import block_sockets
from trust_support import (
    RELEASE_KEY,
    compile_install,
    flip,
    host_key,
    trust_on,
    use_host,
    write_pub,
)

from architect import trust
from architect.cli import main
from architect.forge_modes import export_package, place_package

PACKAGE = "receipt_reader"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


def _verify(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[str], str]:
    code = main(["verify", *argv])
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert text.isascii() and "—" not in text and "–" not in text
    return code, captured.out.splitlines(), captured.err


def _built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, signed: bool = True
) -> tuple[Path, Path, Path, str | None]:
    """Host ``a`` compiles, installs and exports; returns (artifact, package, host, fp)."""
    host = use_host(monkeypatch, tmp_path / "a")
    public = host_key(host) if signed else None
    entry, _, workspace = compile_install(host)
    manifest, _ = export_package(entry, workspace / "exports", PACKAGE)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE, repo, "lib")
    if public is not None:
        write_pub(tmp_path / "bench.pub", public)
    fingerprint = trust.fingerprint(public) if public is not None else None
    return entry.directory, repo / "lib" / PACKAGE, host, fingerprint


# ------------------------------------------------------------------ verified


def test_verify_a_signed_report_artifact_and_package_on_a_clean_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact, package, _, fingerprint = _built(tmp_path, monkeypatch)
    assert fingerprint is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    assert main(["keys", "trust", str(tmp_path / "bench.pub"), "--name", "bench"]) == 0
    capsys.readouterr()
    signer = f"signed by key {fingerprint[:16]} (trusted in this workspace as bench)"
    # Which list vouched for the key: a list planted in the current folder shows itself.
    source = f"trust list: {clean / 'trusted-keys.json'}"

    code, lines, err = _verify(capsys, str(artifact / "report.json"))
    assert code == 0 and err == "" and lines[0] == "verified", lines
    assert lines[1].startswith("report for spec ")
    assert lines[2].startswith("exam passed: score 1.000 against a bar of 1.000 on ")
    assert signer in lines and lines[-1] == source

    code, lines, _ = _verify(capsys, str(artifact))
    assert code == 0 and lines[0] == "verified" and signer in lines
    assert lines[1].startswith("licensed artifact: spec ") and lines[-1] == source

    code, lines, _ = _verify(capsys, str(package))
    assert code == 0 and lines[0] == "verified", lines
    # In plain words first: the model, the score, the pass mark and the attempt.
    assert lines[1] == "fake. 4 of 4. Pass mark 4. Passed. Attempt 1."
    assert lines[2] == f"package {PACKAGE}" and signer in lines
    assert lines[-2:] == [
        f"files: all 13 match the manifest signed by key {fingerprint[:16]} "
        "(trusted in this workspace as bench)",
        source,
    ]

    code, lines, _ = _verify(capsys, str(package), "--json")
    facts = json.loads(lines[0])
    assert facts["verified"] is True and facts["kind"] == "package"
    assert facts["signer"] == fingerprint and facts["via"] == "ed25519"
    assert not (clean / "signing.key").exists()


def test_hmac_only_results_verify_on_their_host_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact, package, host, _ = _built(tmp_path, monkeypatch, signed=False)
    # The key that vouched, by absolute path: never just "this host's own key".
    host_only = (
        f"signed by the HMAC key at {host / 'signing.key'} (only a host holding that key "
        "can check it)"
    )
    for target in (artifact / "report.json", artifact, package):
        code, lines, _ = _verify(capsys, str(target))
        assert code == 0 and lines[0] == "verified" and host_only in lines, lines
    code, lines, _ = _verify(capsys, str(package))
    assert lines[-1] == (
        "files: all 13 match the manifest; no trusted key signed the manifest, so only "
        "a host holding that HMAC key can vouch for them"
    )
    use_host(monkeypatch, tmp_path / "b")
    for target in (artifact / "report.json", artifact, package):
        code, lines, err = _verify(capsys, str(target))
        assert code == 1 and lines[0] == "refused: license_unverifiable", lines
        assert json.loads(err) == {"error": "license_unverifiable"}
    # This host's own key exists but did not sign it: a different refusal.
    other = use_host(monkeypatch, tmp_path / "c")
    (other / "signing.key").write_bytes(b"k" * 32)
    code, lines, _ = _verify(capsys, str(artifact / "report.json"))
    assert lines[0] == "refused: invalid_license"
    assert host.is_dir()


# ------------------------------------------------------------------ refused


def test_untrusted_and_changed_results_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact, package, _, fingerprint = _built(tmp_path, monkeypatch)
    assert fingerprint is not None
    clean = use_host(monkeypatch, tmp_path / "b")
    for target in (artifact / "report.json", artifact, package):
        code, lines, err = _verify(capsys, str(target))
        assert code == 1 and lines[0] == "refused: key_untrusted", lines
        assert any(f"signed by key {fingerprint[:16]}, which this workspace does not trust"
                   in line for line in lines), lines
        assert json.loads(err) == {"error": "key_untrusted"}
    trust_on(clean, trust.parse_public_key((tmp_path / "bench.pub").read_text("ascii")))
    harness = package / "artifact" / "harness.py"
    flip(harness, 3)
    code, lines, _ = _verify(capsys, str(package))
    assert code == 1 and lines[0] == "refused: artifact_changed", lines
    assert lines[1] == "Stopped: changed after its exam. 0 model calls."
    assert lines[2] == (
        "Changed: artifact/harness.py. A change to this file can only be undone."
    )
    assert lines[3] == f"package {PACKAGE}"
    assert lines[4] == "changed: artifact/harness.py"
    assert lines[5] == f"what key {fingerprint[:16]} signed no longer matches"
    flip(harness, 3)
    report = artifact / "report.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    data["floor_score"] = 0.25
    report.write_text(json.dumps(data), encoding="utf-8")
    for target in (report, artifact):
        code, lines, _ = _verify(capsys, str(target))
        assert lines[0] == "refused: artifact_changed", lines
        assert f"the report differs from what key {fingerprint[:16]} signed" in lines


def test_what_cannot_be_checked_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = use_host(monkeypatch, tmp_path / "a")
    text = tmp_path / "notes.txt"
    text.write_text("hello\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    for target in (text, empty, tmp_path / "absent"):
        code, lines, err = _verify(capsys, str(target))
        assert code == 1 and lines[0] == "refused: verify_target_unknown", lines
    (host / "trusted-keys.json").write_text("{not json", encoding="utf-8")
    code, lines, _ = _verify(capsys, str(text))
    assert lines[0] == "refused: trust_list_invalid"
    for argv in ([], [str(text), "--published", "receipts"]):
        assert main(["verify", *argv]) == 1
        assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}


# ------------------------------------------------------------------ published


def test_verify_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_host(monkeypatch, tmp_path / "a")
    # What ships: the receipt results, signed with the release key.
    signed = json.loads(
        (trust.PUBLISHED_ROOT / "receipts" / trust.PUBLISHED_FILE).read_text(encoding="ascii")
    )
    code, lines, err = _verify(capsys, "--published", "receipts")
    assert code == 0 and lines[:3] == [
        "verified",
        "published report receipts",
        f"files: all {len(signed['files'])} match what key {RELEASE_KEY[:16]} (shipped with "
        "Architect) signed",
    ]
    code, lines, _ = _verify(capsys, "--published", "nope")
    assert lines[0] == "refused: published_unknown" and "available: receipts" in lines
    # A folder under a stand-in package root and shipped key: unsigned, then signed.
    root = tmp_path / "published"
    folder = root / "receipts"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text("# Receipts\n", encoding="utf-8")
    (folder / "REPORT.md").write_text("exam 93 of 100\n", encoding="utf-8")
    monkeypatch.setattr(trust, "PUBLISHED_ROOT", root)
    code, lines, _ = _verify(capsys, "--published", "receipts")
    assert code == 1 and lines == [
        "refused: unsigned",
        "published report receipts",
        "there is no SIGNED.json here, so nothing in this folder is signed yet",
    ]
    key = tmp_path / "release.key"
    public = trust.generate_key(key)
    trust.sign_published(folder, key=key)
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", public))
    code, lines, _ = _verify(capsys, "--published", "receipts")
    assert code == 0 and lines == [
        "verified",
        "published report receipts",
        f"files: all 2 match what key {trust.fingerprint(public)[:16]} "
        "(shipped with Architect) signed",
        "listed: README.md, REPORT.md",
    ]
    # The Finder's own file in the folder is skipped and named, never a change.
    (folder / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    code, lines, _ = _verify(capsys, "--published", "receipts")
    assert code == 0 and lines[-1] == (
        "ignored: .DS_Store (files the operating system writes; no signature covers them)"
    )
    flip(folder / "REPORT.md", 5)
    code, lines, _ = _verify(capsys, "--published", "receipts")
    assert code == 1 and lines[:3] == [
        "refused: artifact_changed", "published report receipts", "changed: REPORT.md",
    ]
    flip(folder / "REPORT.md", 5)
    (folder / ".DS_Store").unlink()
    # Files signed under another name do not stand in for receipts.
    trust.sign_published(folder, "other", key=key)
    code, lines, _ = _verify(capsys, "--published", "receipts")
    assert code == 1 and lines == [
        "refused: artifact_changed",
        "published report other",
        "these files were signed as other, not receipts",
    ]
    code, lines, _ = _verify(capsys, str(folder))  # a path names no expected name
    assert code == 0 and lines[1] == "published report other", lines


def test_published_folders_that_ship_verify_under_the_shipped_key() -> None:
    from architect.verdict import verify_published

    names = sorted(path.name for path in trust.PUBLISHED_ROOT.iterdir() if path.is_dir())
    assert "receipts" in names
    for name in names:
        folder = trust.PUBLISHED_ROOT / name
        assert (folder / "README.md").is_file()
        verdict = verify_published(name)
        if (folder / trust.PUBLISHED_FILE).exists():
            assert verdict.verified, verdict.text()
            assert verdict.via == "ed25519" and verdict.signer == RELEASE_KEY
        else:
            assert verdict.code == "unsigned"
    assert (trust.PUBLISHED_ROOT / "receipts" / trust.PUBLISHED_FILE).is_file()


def test_the_published_receipts_are_the_committed_evidence_byte_for_byte() -> None:
    """The signed folder holds the evaluation plan, the plain models' bench rows, every
    ladder build's files and the frozen check functions exactly as the repository keeps
    them, so a change to either side shows here (and needs a new signature from the release
    key's host)."""
    from architect.verdict import verify_path

    root = Path(__file__).resolve().parents[1]
    folder = trust.PUBLISHED_ROOT / "receipts"
    evidence = root / "examples" / "bench" / "eval-2026-10-05"
    checked = root / "examples" / "bench" / "eval-2026-10-06"
    copies = {"plan.md": root / "docs" / "bench" / "prereg-nebius.md"}
    for arm in ("A1", "A2", "A3"):
        for name in ("rows.jsonl", "run.json", "summary.json"):
            copies[f"{arm}/{name}"] = evidence / arm / name
    for name in ("outcome.json", "spec.json", "profile.json"):
        copies[f"ladder/{name}"] = evidence / "ladder" / name
    # The checked ladder's reports are committed too, so they are compared byte for byte.
    reports = ("exam-lightning.json", "exam-super.json", "exam-ultra.json")
    for name in ("outcome.json", "spec.json", "profile.json", *reports):
        copies[f"ladder-2026-10-06/{name}"] = checked / "ladder" / name
    # Text, not .py: the folder ships inside the package, where mypy would check a module.
    copies["ladder-2026-10-06/frozen_checks.py.txt"] = (
        root / "docs" / "bench" / "eval-2026-10-06" / "frozen_checks.py"
    )
    # The same spec's three later sits, the builds for the demo video.
    videos = (
        "ladder-2026-10-06-video-1",
        "ladder-2026-10-06-video-2",
        "ladder-2026-10-06-video-3",
    )
    for video in videos:
        for name in ("outcome.json", *reports):
            copies[f"{video}/{name}"] = checked / video.removeprefix("ladder-2026-10-06-") / name
    for published, source in copies.items():
        assert (folder / published).read_bytes() == source.read_bytes(), published
    digests = {}
    for ladder in ("ladder", "ladder-2026-10-06", *videos):
        outcome = json.loads((folder / ladder / "outcome.json").read_text(encoding="utf-8"))
        for rung, label in zip(outcome["rungs"], ("lightning", "super", "ultra"), strict=True):
            path = folder / ladder / f"exam-{label}.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            assert report["artifact_digest"] == rung["artifact_digest"], (ladder, label)
            assert report["held_out_counts"] == rung["exam"]
            assert report["passed"] == rung["licensed"]
            assert report["spec_hash"] == outcome["spec_hash"]
            if ladder != "ladder":
                # Each 2026-10-06 report is sealed by the release key and checks alone.
                verdict = verify_path(path, require_key=RELEASE_KEY)
                assert verdict.verified and verdict.signer == RELEASE_KEY, (ladder, label)
        # The cheapest licensed rung ships, and it is the agent the build installed.
        shipped = [rung for rung in outcome["rungs"] if rung["ships"]]
        assert shipped == [next(rung for rung in outcome["rungs"] if rung["licensed"])], ladder
        assert outcome["agent"]["artifact_digest"] == shipped[0]["artifact_digest"], ladder
        digests[ladder] = [rung["artifact_digest"] for rung in outcome["rungs"]]
    # All four sits of the checked spec examined the same three agents.
    assert all(digests[video] == digests["ladder-2026-10-06"] for video in videos)


# ------------------------------------------------------------------ keys


def test_keys_generate_list_trust_and_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_host(monkeypatch, tmp_path / "elsewhere")
    workspace = tmp_path / "ws"
    flags = ["--workspace", str(workspace)]
    # A new workspace trusts the release key that ships with Architect, and signs nothing.
    release = {"fingerprint": RELEASE_KEY, "name": "architect", "source": "shipped"}
    assert main(["keys", "list", "--json", *flags]) == 0
    assert json.loads(capsys.readouterr().out) == {"signing_key": None, "trusted": [release]}
    pub = tmp_path / "out" / "release.pub"
    pub.parent.mkdir()
    pub.write_text("placeholder: no key yet\n", encoding="ascii")
    assert main(["keys", "generate", "--name", "bench host", "--pub", str(pub), *flags]) == 0
    out = capsys.readouterr().out
    public = trust.parse_public_key(pub.read_text(encoding="ascii"))
    fingerprint = trust.fingerprint(public)
    assert out.splitlines()[0] == f"created Ed25519 key {fingerprint}"
    assert (workspace / "ed25519.key").is_file()
    assert not (tmp_path / "elsewhere" / "ed25519.key").exists()
    assert main(["keys", "list", "--json", *flags]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "signing_key": fingerprint,
        "trusted": [
            release, {"fingerprint": fingerprint, "name": "bench host", "source": "workspace"}
        ],
    }
    # Never replaced: not the private key, not a public key file that holds a key.
    for argv in (["keys", "generate", *flags], ["keys", "generate", "--pub", str(pub), *flags]):
        assert main(argv) == 1
        assert json.loads(capsys.readouterr().err) == {"error": "key_exists"}
    assert trust.fingerprint(trust.parse_public_key(pub.read_text(encoding="ascii"))) == (
        fingerprint
    )
    # Trust another key, twice; refuse a file that is not a key.
    other = write_pub(tmp_path / "other.pub", trust.generate_key(tmp_path / "other.key"))
    assert main(["keys", "trust", str(other), "--name", "colleague", *flags]) == 0
    assert capsys.readouterr().out.startswith("this workspace now trusts key ")
    assert main(["keys", "trust", str(other), "--name", "colleague", *flags]) == 0
    assert capsys.readouterr().out.startswith("this workspace already trusted key ")
    bad = tmp_path / "bad.pub"
    bad.write_text("not a key\n", encoding="ascii")
    for argv in (["keys", "trust", str(bad), "--name", "x", *flags],
                 ["keys", "trust", str(tmp_path / "absent.pub"), "--name", "x", *flags]):
        assert main(argv) == 1
        assert json.loads(capsys.readouterr().err) == {"error": "key_invalid"}
    assert main(["keys", "trust", str(other), "--name", "../x", *flags]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}
    # Sign a folder, then verify it with this workspace's list.
    folder = tmp_path / "receipts"
    folder.mkdir()
    (folder / "REPORT.md").write_text("exam 93 of 100\n", encoding="utf-8")
    assert main(["keys", "sign", str(folder), *flags]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        f"signed receipts: 1 file with key {fingerprint[:16]}"
    )
    code, lines, _ = _verify(capsys, str(folder), *flags)
    assert code == 0 and lines[0] == "verified"
    assert lines[2] == (
        f"files: the one file matches what key {fingerprint[:16]} (trusted in this "
        "workspace as bench host) signed"
    )
    # A workspace without a private key signs nothing.
    assert main(["keys", "sign", str(folder), "--workspace", str(tmp_path / "keyless")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "key_missing"}
