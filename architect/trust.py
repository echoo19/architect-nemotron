"""Ed25519 signatures anyone can check, and the local list of keys this host trusts.

Stage 4 keeps signing every report with the host's HMAC key (``_authority``), which only
that host can check. A host that also holds an Ed25519 private key adds an Ed25519
signature, a ``Seal``, to its reports, its export manifests and the report folders it
publishes. Anyone can check a seal with the public key alone.

The trust model:

1. A seal names its key by fingerprint (the sha256 of the raw 32-byte public key) and
   never carries the key itself, so a seal verifies only under a key this host already
   trusts. A key that arrives inside a signed file is never trusted. A seal made by a
   trusted key whose fingerprint was edited afterwards counts as changed bytes.
2. The trust list is local. It is ``trusted-keys.json`` next to the HMAC key (the
   workspace), or the file ``ARCHITECT_TRUST_FILE`` names. Without that file this host
   trusts exactly the key shipped in the package at ``architect/keys/architect-ed25519.pub``;
   with it, the keys it lists, plus the shipped key unless it says ``"shipped": false``.
   A trust file that cannot be read trusts nothing, and so does a file that
   ``ARCHITECT_TRUST_FILE`` names but that does not exist.
3. The private key lives only on the signing host: ``ed25519.key`` in the workspace, or
   the file ``ARCHITECT_ED25519_KEY_FILE`` names. It is never created implicitly and
   never shipped, and Pi file tools are denied both it and the trust file.
4. Every seal covers ``architect-signature-v1``, a zero byte, the kind (``report``,
   ``manifest`` or ``published``), a zero byte, then the signed bytes. A seal made for
   one kind never verifies as another.
5. A seal proves the bytes are the ones the key holder signed. It does not prove that the
   exam behind a report ran honestly; that rests on the signing host.
6. Checks of files that may come from elsewhere (``architect verify``, exported
   packages) run inside ``configured_only``: there the HMAC key and the trust list count
   only when ``--workspace`` or an ``ARCHITECT_*`` variable names them, never
   ``./.architect`` on its own, so a key or a list that arrives with a downloaded folder
   cannot vouch for it. Without either, only the shipped key counts. A configured key or
   list that lies inside the folder being checked never vouches for it either
   (``vouchers_inside``).

Only ``_authority`` seals reports (the ``report`` kind goes through the private
``_seal``); host code seals manifests and published folders through ``seal``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Annotated, Literal, Self

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

__all__ = [
    "ED25519_KEY_ENV",
    "ED25519_KEY_NAME",
    "PUBLISHED_FILE",
    "PUBLISHED_ROOT",
    "SHIPPED_KEY",
    "SIGNING_KEY_ENV",
    "SIGNING_KEY_NAME",
    "TRUST_FILE_ENV",
    "TRUST_FILE_NAME",
    "Kind",
    "Policy",
    "PublishedCheck",
    "PublishedManifest",
    "Seal",
    "SealCheck",
    "TrustError",
    "TrustList",
    "TrustedKey",
    "active_policy",
    "add_trusted_key",
    "canonical_json",
    "check_published",
    "check_seal",
    "configured_only",
    "fingerprint",
    "generate_key",
    "hmac_key_path",
    "load_private_key",
    "load_trust",
    "os_metadata",
    "parse_public_key",
    "private_key_path",
    "protected_paths",
    "public_key_pem",
    "published_folder",
    "raw_public_key",
    "safe_name",
    "seal",
    "sign_published",
    "signing_key_path",
    "trust_file_path",
    "trust_source",
    "vouchers_inside",
    "within",
    "workspace_scope",
]

SIGNING_KEY_ENV = "ARCHITECT_SIGNING_KEY_FILE"
ED25519_KEY_ENV = "ARCHITECT_ED25519_KEY_FILE"
TRUST_FILE_ENV = "ARCHITECT_TRUST_FILE"
ED25519_KEY_NAME = "ed25519.key"
TRUST_FILE_NAME = "trusted-keys.json"
_PACKAGE_DIR = Path(__file__).resolve().parent
SHIPPED_KEY = _PACKAGE_DIR / "keys" / "architect-ed25519.pub"
PUBLISHED_ROOT = _PACKAGE_DIR / "published"
PUBLISHED_FILE = "SIGNED.json"
PUBLISHED_FORMAT: Literal["architect-published-v1"] = "architect-published-v1"

Kind = Literal["report", "manifest", "published"]
SealStatus = Literal["valid", "untrusted", "invalid"]
_CONTEXT = b"architect-signature-v1\x00"
_FILE_LIMIT = 1_048_576
_PUBLISHED_LIMIT = 64 * 1_048_576
_PEM_BEGIN = "-----BEGIN PUBLIC KEY-----"
_PEM_END = "-----END PUBLIC KEY-----"
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
# Files an operating system writes into a folder a person opens: the Finder's .DS_Store and
# AppleDouble "._" files, and Windows Explorer's Thumbs.db and desktop.ini. A published
# folder's checks skip them by name when signing and when verifying, so the manifest never
# lists one and none can stand in for a signed file.
_OS_METADATA = re.compile(r"(?i)\.ds_store|\._.+|thumbs\.db|desktop\.ini")
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Signature = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
SIGNING_KEY_NAME = "signing.key"
_IMPLICIT_WORKSPACE = Path(".architect")
# Ed25519 points of small order (the eight-point torsion subgroup), by y coordinate: the
# identity (y = 1), the point of order 2 (y = p - 1), the two of order 4 (y = 0) and the
# four of order 8 (two values of y, each with both signs of x). Such a public key
# verifies a constant forged signature (R of small order, S = 0) for every message or for
# one message in 2 to 8, so no key whose y reduces to one of these is ever accepted:
# that covers the non-canonical encodings too (y >= p, or a sign bit set with x = 0).
_P = 2**255 - 19
_SMALL_ORDER_Y = frozenset(
    {
        0,
        1,
        _P - 1,
        int.from_bytes(
            bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
            "little",
        ),
        int.from_bytes(
            bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
            "little",
        ),
    }
)


class TrustError(ValueError):
    """A refused key or signing operation; ``str()`` is one fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Seal(BaseModel):
    """An Ed25519 signature and the fingerprint of the key that made it (never the key)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: Digest
    signature: Signature


@dataclass(frozen=True)
class TrustedKey:
    fingerprint: str
    name: str
    source: Literal["shipped", "workspace"]
    key: Ed25519PublicKey
    path: Path | None = None  # the trust file that lists a workspace key

    @property
    def label(self) -> str:
        """Who vouches for the key, in a few plain words."""
        if self.source == "shipped":
            return "shipped with Architect"
        return f"trusted in this workspace as {self.name}"


@dataclass(frozen=True)
class TrustList:
    keys: Mapping[str, TrustedKey]
    # The trust file, as an absolute path; None when no workspace list applies (inside
    # ``configured_only`` with nothing configured: the shipped key alone).
    path: Path | None
    # "trust_list_invalid" when the file could not be read; "trust_list_missing" when
    # ARCHITECT_TRUST_FILE names a file that does not exist. Either way nothing is trusted.
    error: str | None = None


@dataclass(frozen=True)
class Policy:
    """How the checks inside ``configured_only`` find the keys that may vouch.

    ``hmac`` False: no HMAC key vouches, so only an Ed25519 seal counts. ``only``: the
    one fingerprint trusted (it must be on the trust list already), as for
    ``architect verify --require-key``.
    """

    hmac: bool = True
    only: str | None = None


_POLICY: ContextVar[Policy | None] = ContextVar("architect_trust_policy", default=None)


@dataclass(frozen=True)
class SealCheck:
    status: SealStatus
    fingerprint: str
    signer: TrustedKey | None = None  # set only when status is "valid"


# ------------------------------------------------------------------ keys


def canonical_json(value: object) -> bytes:
    """Sorted keys, no spaces, ASCII only: the bytes manifests are written and signed as."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def raw_public_key(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(key: Ed25519PublicKey) -> str:
    """The sha256 of the raw 32-byte public key, as 64 hex digits."""
    return hashlib.sha256(raw_public_key(key)).hexdigest()


def public_key_pem(key: Ed25519PublicKey) -> str:
    """The key as a PEM ``PUBLIC KEY`` block (what ``openssl pkey -pubout`` writes)."""
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def _small_order(raw: bytes) -> bool:
    """Whether a 32-byte Ed25519 public key encodes a point of small order."""
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    return y % _P in _SMALL_ORDER_Y


def parse_public_key(text: str) -> Ed25519PublicKey:
    """A PEM ``PUBLIC KEY`` block or the base64 of the raw key; ``key_invalid`` otherwise,
    and for every encoding of a point of small order (a key anyone can sign as)."""
    try:
        if _PEM_BEGIN in text:
            start = text.index(_PEM_BEGIN)
            end = text.index(_PEM_END, start) + len(_PEM_END)
            key: object = serialization.load_pem_public_key(text[start:end].encode("ascii"))
        else:
            raw = base64.b64decode(text.strip().encode("ascii"), validate=True)
            if len(raw) != 32:
                raise TrustError("key_invalid")
            key = Ed25519PublicKey.from_public_bytes(raw)
    except TrustError:
        raise
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm, binascii.Error):
        raise TrustError("key_invalid") from None
    if not isinstance(key, Ed25519PublicKey) or _small_order(raw_public_key(key)):
        raise TrustError("key_invalid")
    return key


# ------------------------------------------------------------------ where the keys live


def _named(variable: str) -> Path | None:
    value = os.environ.get(variable)
    return Path(value) if value else None


def signing_key_path() -> Path:
    """This host's HMAC key file, where Stage 4 signs (and creates it on first use):
    ``ARCHITECT_SIGNING_KEY_FILE``, else ``.architect/signing.key`` under the working
    directory. Checks inside ``configured_only`` use ``hmac_key_path`` instead."""
    return _named(SIGNING_KEY_ENV) or _IMPLICIT_WORKSPACE / SIGNING_KEY_NAME


def _workspace_dir() -> Path:
    """The directory of the HMAC key, which every session and CLI run sets to the workspace."""
    return signing_key_path().parent


def private_key_path() -> Path:
    configured = os.environ.get(ED25519_KEY_ENV)
    return Path(configured) if configured else _workspace_dir() / ED25519_KEY_NAME


def trust_file_path() -> Path:
    """Where ``architect keys`` writes this workspace's trust list."""
    configured = os.environ.get(TRUST_FILE_ENV)
    return Path(configured) if configured else _workspace_dir() / TRUST_FILE_NAME


def protected_paths() -> tuple[Path, Path]:
    """The Ed25519 private key and the trust file: no model tool may read or write them."""
    return private_key_path(), trust_file_path()


def active_policy() -> Policy | None:
    """The ``configured_only`` policy in force here, or None outside one."""
    return _POLICY.get()


@contextmanager
def configured_only(*, hmac: bool = True, only: str | None = None) -> Iterator[None]:
    """Check with configured keys only, for files that may come from elsewhere.

    Inside, the HMAC key is the file ``ARCHITECT_SIGNING_KEY_FILE`` names (none without
    it) and the trust list is the file ``ARCHITECT_TRUST_FILE`` names, else
    ``trusted-keys.json`` next to that HMAC key, else none: then the shipped key alone is
    trusted. ``./.architect`` never counts on its own, so a key or a list that arrives
    with a downloaded folder cannot vouch for it. ``hmac=False`` lets no HMAC key vouch
    (Ed25519 only) and ``only`` trusts exactly that fingerprint. A nested scope can add
    restrictions but never lift them. Signing is unaffected (``signing_key_path``).
    """
    outer = _POLICY.get()
    if outer is not None:
        hmac = hmac and outer.hmac
        only = outer.only if outer.only is not None else only
    token = _POLICY.set(Policy(hmac=hmac, only=only))
    try:
        yield
    finally:
        _POLICY.reset(token)


def hmac_key_path() -> Path | None:
    """The HMAC key that may vouch for a report here, or None.

    Outside ``configured_only`` it is ``signing_key_path``. Inside, only a key that
    ``ARCHITECT_SIGNING_KEY_FILE`` names counts, and none when the scope turns the HMAC off.
    """
    policy = _POLICY.get()
    if policy is None:
        return signing_key_path()
    if not policy.hmac:
        return None
    return _named(SIGNING_KEY_ENV)


def trust_source() -> tuple[Path | None, bool]:
    """The trust file the checks read, and whether it must exist.

    A file ``ARCHITECT_TRUST_FILE`` names must exist: a named list that is missing trusts
    nothing. Otherwise the workspace's ``trusted-keys.json``, which may be absent (the
    shipped key alone). Inside ``configured_only`` the workspace counts only when
    ``ARCHITECT_SIGNING_KEY_FILE`` names it; with neither variable there is no list (None).
    """
    named = _named(TRUST_FILE_ENV)
    if named is not None:
        return named, True
    key = _named(SIGNING_KEY_ENV)
    if key is not None:
        return key.parent / TRUST_FILE_NAME, False
    if _POLICY.get() is not None:
        return None, False
    return _IMPLICIT_WORKSPACE / TRUST_FILE_NAME, False


def within(path: Path | str, root: Path | str) -> bool:
    """Whether ``path`` is ``root`` or lies under it, compared without case.

    APFS and NTFS, the macOS and Windows defaults, match names without case, so a case
    variant of a protected file is that file. Pass resolved paths. Folding only widens a
    match (``Operator`` and ``operator`` both count as the protected ``operator``), so
    a check that refuses on a match stays safe on case-sensitive systems too.
    """
    folded = PurePath(os.fspath(path).casefold())
    base = PurePath(os.fspath(root).casefold())
    return folded == base or folded.is_relative_to(base)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _inside_by_identity(path: Path, root: Path) -> bool:
    """Whether ``path`` or one of its parents is the same directory on disk as ``root``.

    ``within`` compares path text, which ``Path.resolve`` leaves spelled differently for a
    UNC share (``\\\\localhost\\C$\\...``), a loopback address, an extended-length prefix
    (``\\\\?\\C:\\...``) or a device path: each names the same tree as a drive path but
    does not share its leading text, so the text check alone would miss a key reached by
    one of those spellings. ``os.path.samefile`` matches them by file identity, which does
    not depend on how a path is written. Both must exist to be compared this way; a key or
    a list that is not there vouches for nothing, so a miss here is safe."""
    for probe in (path, *path.parents):
        try:
            if os.path.samefile(probe, root):
                return True
        except OSError:
            continue
    return False


def vouchers_inside(folder: Path) -> tuple[tuple[str, Path], ...]:
    """The HMAC key and the trust list the checks here would use, when either lies
    inside ``folder``, as ``(what, absolute path)`` pairs (``what`` is ``"HMAC key"`` or
    ``"trust list"``). What arrives with the files being checked never vouches for them.
    A path that cannot be resolved counts as inside. A path whose spelling hides that it
    lies inside (a UNC, loopback, extended-length or device alias) is matched by file
    identity, so naming the in-folder key that way does not slip past the check."""
    try:
        root = Path(folder).resolve()
    except (OSError, RuntimeError):
        root = _absolute(Path(folder))
    found: list[tuple[str, Path]] = []
    candidates = (("HMAC key", hmac_key_path()), ("trust list", trust_source()[0]))
    for what, path in candidates:
        if path is None:
            continue
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            found.append((what, _absolute(path)))
            continue
        if within(resolved, root) or _inside_by_identity(resolved, root):
            found.append((what, resolved))
    return tuple(found)


@contextmanager
def workspace_scope(workspace: Path | None) -> Iterator[None]:
    """Point the HMAC key, the Ed25519 key and the trust list at ``workspace`` for a while.

    For one CLI command (``--workspace``); None changes nothing. The trust list is the
    workspace's own ``trusted-keys.json`` (it may be absent: the shipped key alone), so
    ``ARCHITECT_TRUST_FILE`` is cleared for the scope.
    """
    if workspace is None:
        yield
        return
    values: dict[str, str | None] = {
        SIGNING_KEY_ENV: str(workspace / SIGNING_KEY_NAME),
        ED25519_KEY_ENV: str(workspace / ED25519_KEY_NAME),
        TRUST_FILE_ENV: None,
    }
    previous = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, before in previous.items():
            if before is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = before


def load_private_key(path: Path | None = None) -> Ed25519PrivateKey | None:
    """This host's Ed25519 private key, or None when it holds none.

    A file that exists but is not an unencrypted Ed25519 PEM key is ``key_invalid``:
    an operator who opted in must not silently get unsigned reports.
    """
    path = private_key_path() if path is None else path
    if not path.exists():
        return None
    try:
        if not path.is_file() or path.stat().st_size > _FILE_LIMIT:
            raise TrustError("key_invalid")
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except TrustError:
        raise
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        raise TrustError("key_invalid") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise TrustError("key_invalid")
    return key


def generate_key(path: Path | None = None) -> Ed25519PublicKey:
    """Create a new private key at ``path`` (mode 0600, never overwritten) and return
    its public key. ``key_exists`` when a file is already there."""
    path = private_key_path() if path is None else path
    private = Ed25519PrivateKey.generate()
    data = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
    except FileExistsError:
        raise TrustError("key_exists") from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return private.public_key()


# ------------------------------------------------------------------ trust list


class _TrustEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")]
    public_key: str  # base64 of the raw 32-byte key


class _TrustFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shipped: bool = True
    keys: tuple[_TrustEntry, ...] = ()


def _shipped_key(path: Path) -> TrustedKey | None:
    """The key shipped in the package; None while the file holds no key yet."""
    try:
        if not path.is_file() or path.stat().st_size > _FILE_LIMIT:
            return None
        text = path.read_text(encoding="ascii")
        if _PEM_BEGIN not in text:
            return None
        key = parse_public_key(text)
    except (OSError, UnicodeError, TrustError):
        return None
    return TrustedKey(fingerprint(key), "architect", "shipped", key)


def _read_trust_file(path: Path) -> _TrustFile:
    if not path.is_file() or path.stat().st_size > _FILE_LIMIT:
        raise TrustError("trust_list_invalid")
    return _TrustFile.model_validate_json(path.read_bytes())


def load_trust(path: Path | None = None, *, shipped: Path | None = None) -> TrustList:
    """The keys this host trusts, by fingerprint (``trust_source`` without ``path``).

    Fails closed: an unreadable file trusts nothing, and so does a file that
    ``ARCHITECT_TRUST_FILE`` names but that does not exist (``error`` says which). Inside
    ``configured_only(only=...)`` the list holds that one key at most.
    """
    required = False
    if path is None:
        path, required = trust_source()
    shipped_key = _shipped_key(SHIPPED_KEY if shipped is None else shipped)
    keys: dict[str, TrustedKey] = {}
    shown = None if path is None else _absolute(path)
    try:
        if shown is None:
            config = _TrustFile()
        elif shown.exists():
            config = _read_trust_file(shown)
        elif required:
            return TrustList({}, shown, "trust_list_missing")
        else:
            config = _TrustFile()
        if config.shipped and shipped_key is not None:
            keys[shipped_key.fingerprint] = shipped_key
        for entry in config.keys:
            key = parse_public_key(entry.public_key)
            known = fingerprint(key)
            keys.setdefault(known, TrustedKey(known, entry.name, "workspace", key, shown))
    except (OSError, ValueError, TrustError):
        return TrustList({}, shown, "trust_list_invalid")
    policy = _POLICY.get()
    if policy is not None and policy.only is not None:
        keys = {known: key for known, key in keys.items() if known == policy.only}
    return TrustList(keys, shown)


def add_trusted_key(key: Ed25519PublicKey, name: str, path: Path | None = None) -> bool:
    """Add ``key`` to the workspace trust file (created if absent); False when it was
    already listed there. ``trust_list_invalid`` when the existing file cannot be read."""
    path = trust_file_path() if path is None else path
    try:
        config = _read_trust_file(path) if path.exists() else _TrustFile()
        entry = _TrustEntry(
            name=name, public_key=base64.b64encode(raw_public_key(key)).decode("ascii")
        )
    except (OSError, ValueError):
        raise TrustError("trust_list_invalid") from None
    wanted = fingerprint(key)
    try:
        listed = {fingerprint(parse_public_key(item.public_key)) for item in config.keys}
    except TrustError:
        raise TrustError("trust_list_invalid") from None
    if wanted in listed:
        return False
    updated = config.model_copy(update={"keys": (*config.keys, entry)})
    text = json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".trusted-keys.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


# ------------------------------------------------------------------ seals


def _message(kind: Kind, payload: bytes) -> bytes:
    return _CONTEXT + kind.encode("ascii") + b"\x00" + payload


def _seal(kind: Kind, payload: bytes, *, key: Path | None = None) -> Seal | None:
    """Sign ``payload`` as ``kind`` with this host's private key; None without one."""
    private = load_private_key(key)
    if private is None:
        return None
    signature = private.sign(_message(kind, payload))
    return Seal(fingerprint=fingerprint(private.public_key()), signature=signature.hex())


def seal(
    kind: Literal["manifest", "published"], payload: bytes, *, key: Path | None = None
) -> Seal | None:
    """A host seal for a manifest or a published folder. Reports are sealed by Stage 4
    alone, through ``_authority``."""
    return _seal(kind, payload, key=key)


def _verifies(key: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    try:
        key.verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def check_seal(
    kind: Kind, payload: bytes, value: Seal, trust: TrustList | None = None
) -> SealCheck:
    """``valid`` (a trusted key signed exactly these bytes as ``kind``), ``untrusted``
    (no key on this host's list made the signature) or ``invalid`` (the bytes changed).

    A seal whose fingerprint names no trusted key but whose signature a trusted key
    made is ``invalid`` too: its fingerprint was edited after signing.
    """
    trust = load_trust() if trust is None else trust
    try:
        signature = bytes.fromhex(value.signature)
    except ValueError:
        return SealCheck("invalid", value.fingerprint)
    message = _message(kind, payload)
    signer = trust.keys.get(value.fingerprint)
    if signer is None:
        if any(_verifies(known.key, signature, message) for known in trust.keys.values()):
            return SealCheck("invalid", value.fingerprint)
        return SealCheck("untrusted", value.fingerprint)
    if not _verifies(signer.key, signature, message):
        return SealCheck("invalid", value.fingerprint)
    return SealCheck("valid", value.fingerprint, signer)


# ------------------------------------------------------------------ published folders


class PublishedManifest(BaseModel):
    """``SIGNED.json``: every file of a published report folder and their seal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["architect-published-v1"]
    name: Annotated[str, StringConstraints(pattern=_NAME.pattern)]
    files: tuple[tuple[str, Digest], ...]
    ed25519: Seal

    @model_validator(mode="after")
    def plain_names(self) -> Self:
        names = [name for name, _ in self.files]
        if len(set(names)) != len(names) or not all(safe_name(name) for name in names):
            raise ValueError("Published file names must be unique and plain")
        if PUBLISHED_FILE in names:
            raise ValueError("The manifest does not list itself")
        return self

    def body(self) -> bytes:
        """The signed bytes: the canonical manifest without its seal."""
        return canonical_json(self.model_dump(mode="json", exclude={"ed25519"}))


@dataclass(frozen=True)
class PublishedCheck:
    """A published folder: ``code`` is None when it is verified. ``ignored`` lists the
    operating system's own files in it (``os_metadata``), which no check covers."""

    code: str | None
    name: str
    files: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    seal: SealCheck | None = None
    ignored: tuple[str, ...] = ()


def safe_name(name: str) -> bool:
    """A relative POSIX path of plain segments, safe to show on a terminal."""
    parts = PurePosixPath(name).parts
    return (
        bool(parts)
        and len(name) <= 512
        and "\\" not in name
        and all(_SEGMENT.fullmatch(part) is not None for part in parts)
        and PurePosixPath(*parts).as_posix() == name
    )


def os_metadata(name: str) -> bool:
    """Whether a file name is one the operating system writes on its own: ``.DS_Store``,
    an AppleDouble ``._`` file, ``Thumbs.db`` or ``desktop.ini``."""
    return _OS_METADATA.fullmatch(name) is not None


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _folder_files(folder: Path) -> tuple[dict[str, Path], int, tuple[str, ...]]:
    """Every regular file under ``folder`` except ``SIGNED.json``, by relative name; the
    number of other entries: links, special files and names that are not ``safe_name``
    (signing refuses those, verification counts them as changes); and the regular files
    the operating system wrote (``os_metadata``), which both skip.
    """
    if _linked(folder) or not folder.is_dir():
        raise TrustError("published_invalid")
    files: dict[str, Path] = {}
    odd = 0
    ignored: list[str] = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if _linked(path):
            odd += 1
        elif path.is_dir():
            continue
        elif path.is_file() and os_metadata(path.name):
            ignored.append(relative)
        elif not path.is_file() or not safe_name(relative):
            odd += 1
        elif relative != PUBLISHED_FILE:
            files[relative] = path
    return files, odd, tuple(ignored)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def published_folder(name: str, root: Path | None = None) -> Path:
    """``architect/published/<name>``; ``published_unknown`` for any other name."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise TrustError("published_unknown")
    folder = (PUBLISHED_ROOT if root is None else root) / name
    if not folder.is_dir():
        raise TrustError("published_unknown")
    return folder


def sign_published(
    folder: Path, name: str | None = None, *, key: Path | None = None
) -> PublishedManifest:
    """Write ``folder/SIGNED.json`` over every other file in the folder.

    ``key_missing`` without a private key; ``published_invalid`` for an empty folder, a
    link, a special file or a file name outside ``safe_name``. An existing
    ``SIGNED.json`` is replaced.
    """
    folder = Path(folder)
    name = folder.name if name is None else name
    if _NAME.fullmatch(name) is None:
        raise TrustError("published_invalid")
    files, odd, _ = _folder_files(folder)
    if odd or not files:
        raise TrustError("published_invalid")
    listed = tuple(sorted((relative, _sha256(path)) for relative, path in files.items()))
    body = canonical_json(
        {"files": [list(item) for item in listed], "format": PUBLISHED_FORMAT, "name": name}
    )
    value = seal("published", body, key=key)
    if value is None:
        raise TrustError("key_missing")
    manifest = PublishedManifest(format=PUBLISHED_FORMAT, name=name, files=listed, ed25519=value)
    if manifest.body() != body:  # the model must dump exactly what was signed
        raise TrustError("published_invalid")
    data = canonical_json(manifest.model_dump(mode="json"))
    descriptor, temporary = tempfile.mkstemp(prefix=".signed.", dir=folder)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / PUBLISHED_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def check_published(
    folder: Path, trust: TrustList | None = None, *, name: str | None = None
) -> PublishedCheck:
    """Check a published folder against its ``SIGNED.json`` and this host's trust list.

    Codes: ``unsigned`` (no ``SIGNED.json``), ``trust_list_invalid``, ``key_untrusted``,
    ``artifact_changed`` (the manifest or any file differs from what was signed, a file
    was added or removed, or the signed name is not ``name`` when one is required) and
    ``published_invalid`` (links or unsafe names).
    """
    folder = Path(folder)
    label = folder.name  # shown until the signed name is known
    trust = load_trust() if trust is None else trust
    path = folder / PUBLISHED_FILE
    try:
        files, odd, ignored = _folder_files(folder)
    except TrustError as exc:
        return PublishedCheck(exc.code, label)
    if not path.is_file() or _linked(path):
        return PublishedCheck("unsigned", label, tuple(files), ignored=ignored)
    try:
        if path.stat().st_size > _PUBLISHED_LIMIT:
            raise ValueError
        data = path.read_bytes()
        manifest = PublishedManifest.model_validate_json(data)
        if canonical_json(manifest.model_dump(mode="json")) != data:
            raise ValueError
    except (OSError, ValueError, ValidationError):
        return PublishedCheck("artifact_changed", label, tuple(files), (PUBLISHED_FILE,))
    if trust.error is not None:
        return PublishedCheck(trust.error, manifest.name, tuple(files))
    checked = check_seal("published", manifest.body(), manifest.ed25519, trust)
    listed = dict(manifest.files)
    if checked.status == "untrusted":
        return PublishedCheck("key_untrusted", manifest.name, tuple(listed), seal=checked)
    if checked.status == "invalid" or (name is not None and manifest.name != name):
        # A folder signed under another name stands in for this one: changed too.
        return PublishedCheck(
            "artifact_changed", manifest.name, tuple(listed), (PUBLISHED_FILE,), checked
        )
    changed = sorted(
        relative
        for relative in set(files) | set(listed)
        if relative not in files
        or relative not in listed
        or _sha256(files[relative]) != listed[relative]
    )
    if odd:
        changed.append(f"{odd} unexpected entries")
    code = "artifact_changed" if changed else None
    return PublishedCheck(
        code, manifest.name, tuple(listed), tuple(changed), checked, ignored=ignored
    )
