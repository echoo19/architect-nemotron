"""W7: Ed25519 keys, the local trust list, seals and published folders (``architect.trust``).

Keys are generated per test in temp directories; nothing here writes the shipped key file
or makes a network call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from trust_support import RELEASE_KEY, flip, write_pub

from architect import trust

# ------------------------------------------------------------------ keys


def test_generate_load_and_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "ed25519.key"
    public = trust.generate_key(path)
    private = trust.load_private_key(path)
    assert private is not None
    assert trust.raw_public_key(private.public_key()) == trust.raw_public_key(public)
    raw = trust.raw_public_key(public)
    assert len(raw) == 32
    assert trust.fingerprint(public) == hashlib.sha256(raw).hexdigest()
    assert b"PRIVATE KEY" in path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    # Never replaced, never created on a read.
    before = path.read_bytes()
    with pytest.raises(trust.TrustError, match="^key_exists$"):
        trust.generate_key(path)
    assert path.read_bytes() == before
    assert trust.load_private_key(tmp_path / "absent.key") is None
    assert not (tmp_path / "absent.key").exists()


def test_a_key_file_that_is_not_an_ed25519_private_key_is_key_invalid(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.key"
    garbage.write_bytes(b"not a key\n")
    other = tmp_path / "x25519.key"
    other.write_bytes(
        X25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    folder = tmp_path / "folder.key"
    folder.mkdir()
    for path in (garbage, other, folder):
        with pytest.raises(trust.TrustError, match="^key_invalid$"):
            trust.load_private_key(path)


def test_public_keys_parse_from_pem_or_base64_only(tmp_path: Path) -> None:
    public = Ed25519PrivateKey.generate().public_key()
    raw = trust.raw_public_key(public)
    pem = trust.public_key_pem(public)
    assert pem.startswith("-----BEGIN PUBLIC KEY-----")
    for text in (pem, "note above\n" + pem + "note below\n", base64.b64encode(raw).decode()):
        assert trust.raw_public_key(trust.parse_public_key(text)) == raw
    x25519 = X25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    for bad in ("", "AAAA", base64.b64encode(raw[:31]).decode(), "not base64!", x25519,
                "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"):
        with pytest.raises(trust.TrustError, match="^key_invalid$"):
            trust.parse_public_key(bad)


# ------------------------------------------------------------------ trust list


def test_the_default_trust_list_holds_exactly_the_shipped_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", shipped))
    listed = trust.load_trust(tmp_path / "absent.json")
    assert listed.error is None
    assert list(listed.keys) == [trust.fingerprint(shipped)]
    only = listed.keys[trust.fingerprint(shipped)]
    assert only.source == "shipped" and only.label == "shipped with Architect"
    # A shipped file that holds a note and no key, as before the release key existed,
    # trusts nothing.
    placeholder = tmp_path / "placeholder.pub"
    placeholder.write_text("Architect release key: not generated yet.\n", encoding="ascii")
    assert trust.load_trust(tmp_path / "absent.json", shipped=placeholder).keys == {}


def test_workspace_keys_add_to_the_shipped_one_and_can_drop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", shipped))
    workspace = Ed25519PrivateKey.generate().public_key()
    path = tmp_path / "trusted-keys.json"
    assert trust.add_trusted_key(workspace, "bench host", path) is True
    assert trust.add_trusted_key(workspace, "again", path) is False  # already listed
    listed = trust.load_trust(path)
    assert set(listed.keys) == {trust.fingerprint(shipped), trust.fingerprint(workspace)}
    added = listed.keys[trust.fingerprint(workspace)]
    assert added.source == "workspace" and added.name == "bench host"
    assert added.label == "trusted in this workspace as bench host"
    config = json.loads(path.read_text(encoding="ascii"))
    config["shipped"] = False
    path.write_text(json.dumps(config), encoding="ascii")
    assert set(trust.load_trust(path).keys) == {trust.fingerprint(workspace)}


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"keys": [{"name": "x", "public_key": "AAAA"}]}',
        '{"keys": [], "extra": 1}',
        '{"keys": [{"name": "../bad", "public_key": "AAAA"}]}',
    ],
)
def test_a_trust_file_that_cannot_be_read_trusts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    shipped = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr(trust, "SHIPPED_KEY", write_pub(tmp_path / "shipped.pub", shipped))
    path = tmp_path / "trusted-keys.json"
    path.write_text(content, encoding="ascii")
    listed = trust.load_trust(path)
    assert listed.keys == {} and listed.error == "trust_list_invalid"
    with pytest.raises(trust.TrustError, match="^trust_list_invalid$"):
        trust.add_trusted_key(shipped, "x", path)
    assert path.read_text(encoding="ascii") == content


def test_paths_follow_the_workspace_and_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "ws" / "signing.key"))
    assert trust.private_key_path() == tmp_path / "ws" / "ed25519.key"
    assert trust.trust_file_path() == tmp_path / "ws" / "trusted-keys.json"
    monkeypatch.setenv("ARCHITECT_ED25519_KEY_FILE", str(tmp_path / "elsewhere.key"))
    monkeypatch.setenv("ARCHITECT_TRUST_FILE", str(tmp_path / "list.json"))
    assert trust.protected_paths() == (tmp_path / "elsewhere.key", tmp_path / "list.json")
    with trust.workspace_scope(tmp_path / "other"):
        assert trust.private_key_path() == tmp_path / "other" / "ed25519.key"
        assert trust.trust_file_path() == tmp_path / "other" / "trusted-keys.json"
    assert trust.trust_file_path() == tmp_path / "list.json"


# ------------------------------------------------------------------ seals


def _trusting(*keys: Ed25519PublicKey) -> trust.TrustList:
    return trust.TrustList(
        {
            trust.fingerprint(key): trust.TrustedKey(trust.fingerprint(key), "k", "workspace", key)
            for key in keys
        },
        Path("unused.json"),
    )


def test_seal_round_trip_names_the_fingerprint_never_the_key(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519.key"
    public = trust.generate_key(key_path)
    payload = trust.canonical_json({"files": [["a.txt", "0" * 64]], "package": "x"})
    sealed = trust.seal("manifest", payload, key=key_path)
    assert sealed is not None
    assert sealed.fingerprint == trust.fingerprint(public)
    dumped = sealed.model_dump_json()
    raw = trust.raw_public_key(public)
    assert base64.b64encode(raw).decode() not in dumped and raw.hex() not in dumped
    assert set(sealed.model_dump()) == {"fingerprint", "signature"}
    listed = _trusting(public)
    good = trust.check_seal("manifest", payload, sealed, listed)
    assert good.status == "valid" and good.signer is not None
    # Another kind, other bytes, a stranger's list: never valid.
    assert trust.check_seal("report", payload, sealed, listed).status == "invalid"
    assert trust.check_seal("published", payload, sealed, listed).status == "invalid"
    assert trust.check_seal("manifest", payload + b" ", sealed, listed).status == "invalid"
    stranger = _trusting(Ed25519PrivateKey.generate().public_key())
    assert trust.check_seal("manifest", payload, sealed, stranger).status == "untrusted"
    assert trust.seal("manifest", payload, key=tmp_path / "absent.key") is None


def test_every_one_byte_edit_of_a_seal_is_refused(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519.key"
    public = trust.generate_key(key_path)
    payload = b'{"package":"x"}'
    sealed = trust.seal("manifest", payload, key=key_path)
    assert sealed is not None
    listed = _trusting(public)
    for field in ("fingerprint", "signature"):
        value = getattr(sealed, field)
        for index in range(len(value)):
            for replacement in {"0", "f"} - {value[index]}:
                edited = sealed.model_copy(
                    update={field: value[:index] + replacement + value[index + 1 :]}
                )
                # An edited fingerprint still names nobody trusted, but the signature is
                # a trusted key's: that is changed bytes, not an unknown signer.
                assert trust.check_seal("manifest", payload, edited, listed).status == "invalid"


# ------------------------------------------------------------------ published folders


def _published(root: Path) -> Path:
    folder = root / "receipts"
    (folder / "rows").mkdir(parents=True)
    (folder / "README.md").write_text("# Results\n", encoding="utf-8")
    (folder / "REPORT.md").write_text("exam 93/100\n", encoding="utf-8")
    (folder / "rows" / "a1.jsonl").write_text('{"case": 1}\n', encoding="utf-8")
    return folder


def test_a_published_folder_signs_and_verifies(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519.key"
    public = trust.generate_key(key_path)
    folder = _published(tmp_path)
    listed = _trusting(public)
    assert trust.check_published(folder, listed).code == "unsigned"
    manifest = trust.sign_published(folder, key=key_path)
    assert manifest.name == "receipts"
    assert [name for name, _ in manifest.files] == ["README.md", "REPORT.md", "rows/a1.jsonl"]
    data = (folder / trust.PUBLISHED_FILE).read_bytes()
    assert data == trust.canonical_json(json.loads(data))
    checked = trust.check_published(folder, listed)
    assert checked.code is None and checked.changed == ()
    assert checked.seal is not None and checked.seal.status == "valid"
    stranger = _trusting(Ed25519PrivateKey.generate().public_key())
    assert trust.check_published(folder, stranger).code == "key_untrusted"
    with pytest.raises(trust.TrustError, match="^key_missing$"):
        trust.sign_published(folder, key=tmp_path / "absent.key")


def test_any_change_to_a_published_folder_is_artifact_changed(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519.key"
    listed = _trusting(trust.generate_key(key_path))
    folder = _published(tmp_path)
    trust.sign_published(folder, key=key_path)
    signed = folder / trust.PUBLISHED_FILE
    original = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}

    def restore() -> None:
        for path in folder.rglob("*"):
            if path.is_file() and path not in original:
                path.unlink()
        for path, data in original.items():
            path.write_bytes(data)
        assert trust.check_published(folder, listed).code is None

    report = folder / "REPORT.md"
    for offset in range(len(report.read_bytes())):
        flip(report, offset)
        result = trust.check_published(folder, listed)
        assert result.code == "artifact_changed" and result.changed == ("REPORT.md",)
        restore()
    for offset in range(len(signed.read_bytes())):
        flip(signed, offset)
        assert trust.check_published(folder, listed).code == "artifact_changed", offset
        restore()
    (folder / "rows" / "extra.jsonl").write_text("{}\n", encoding="utf-8")
    assert trust.check_published(folder, listed).changed == ("rows/extra.jsonl",)
    restore()
    (folder / "README.md").unlink()
    assert trust.check_published(folder, listed).changed == ("README.md",)
    restore()


OS_FILES = (".DS_Store", "._README.md", "Thumbs.db", "desktop.ini", "rows/.DS_Store")


def test_the_operating_systems_own_files_are_never_signed_or_checked(tmp_path: Path) -> None:
    """A Finder or Explorer visit leaves .DS_Store, ._ files, Thumbs.db or desktop.ini:
    signing skips them, verification ignores them and names them, and a changed listed
    file is still refused next to them."""
    key_path = tmp_path / "ed25519.key"
    listed = _trusting(trust.generate_key(key_path))
    folder = _published(tmp_path)
    for name in OS_FILES:
        (folder / name).write_bytes(b"\x00\x05\x16\x07 metadata")
    manifest = trust.sign_published(folder, key=key_path)
    assert [name for name, _ in manifest.files] == ["README.md", "REPORT.md", "rows/a1.jsonl"]
    for name in OS_FILES:
        (folder / name).unlink()
    assert trust.check_published(folder, listed).ignored == ()
    for name in OS_FILES:
        (folder / name).write_bytes(b"other bytes")
    checked = trust.check_published(folder, listed)
    assert checked.code is None and checked.changed == ()
    assert sorted(checked.ignored) == sorted(OS_FILES)
    flip(folder / "REPORT.md", 0)
    changed = trust.check_published(folder, listed)
    assert changed.code == "artifact_changed" and changed.changed == ("REPORT.md",)
    assert trust.os_metadata(".DS_Store") and trust.os_metadata("._x")
    for name in ("DS_Store", "._", "Thumbs.db.txt", "my-desktop.ini", "README.md"):
        assert not trust.os_metadata(name)


def test_only_plain_names_are_published(tmp_path: Path) -> None:
    key_path = tmp_path / "ed25519.key"
    trust.generate_key(key_path)
    folder = _published(tmp_path)
    (folder / "bad name.txt").write_text("x", encoding="utf-8")
    with pytest.raises(trust.TrustError, match="^published_invalid$"):
        trust.sign_published(folder, key=key_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(trust.TrustError, match="^published_invalid$"):
        trust.sign_published(empty, key=key_path)
    for name in ("receipts", "a-b_1"):
        assert trust.safe_name(name)
    for name in ("", "../x", "a/../b", "a\\b", "/abs", ".hidden", "a b", "x" * 600):
        assert not trust.safe_name(name)
    for name in ("Receipts", "../receipts", "", "a" * 41):
        with pytest.raises(trust.TrustError, match="^published_unknown$"):
            trust.published_folder(name, root=tmp_path)


# ------------------------------------------------------------------ what ships


def test_the_package_ships_the_release_public_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release key's public half, which a fresh install trusts and nothing else; its
    fingerprint is the one docs/trust.md and the published receipts' README give."""
    folder = trust.SHIPPED_KEY.parent
    for path in folder.iterdir():
        assert path.suffix == ".pub", path.name
        assert "PRIVATE" not in path.read_text(encoding="ascii"), path.name
    shipped = trust.parse_public_key(trust.SHIPPED_KEY.read_text(encoding="ascii"))
    assert trust.fingerprint(shipped) == RELEASE_KEY
    # A judge's checkout: no workspace and no variable names any key or trust list.
    monkeypatch.delenv(trust.SIGNING_KEY_ENV, raising=False)
    with trust.configured_only():
        listed = trust.load_trust()
    assert listed.error is None and listed.path is None and list(listed.keys) == [RELEASE_KEY]
    assert listed.keys[RELEASE_KEY].source == "shipped"
    root = Path(__file__).resolve().parents[1]
    for page in (root / "docs" / "trust.md", trust.PUBLISHED_ROOT / "receipts" / "README.md"):
        assert RELEASE_KEY in page.read_text(encoding="utf-8"), page


def test_the_wheel_lists_the_key_and_the_published_folders() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    data = config["tool"]["setuptools"]["package-data"]["architect"]
    assert {"keys/*.pub", "published/**/*", "forge_modes/templates/*.tmpl"} <= set(data)
    assert any(dep.startswith("cryptography") for dep in config["project"]["dependencies"])
