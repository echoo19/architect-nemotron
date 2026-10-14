"""Local report integrity, for a trusted Python host.

Only the future stage-4 verifier may call ``_sign_report`` or ``_seal_report`` in
production. Model outputs cannot issue credentials. This is not a Python sandbox.

A Stage 4 report carries two signatures. The HMAC (``_sign_report``) covers the
canonical dump of every other field; it uses this host's secret key and only this host
can check it. The Ed25519 seal (``_seal_report``, W7) covers that dump plus the HMAC; it
is added only on a host holding an Ed25519 private key, and any host that trusts the
matching public key can check it (``architect.trust``). Either one alone authenticates
the report's values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

from architect import trust


def _canonical_payload(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _key_path() -> Path | None:
    """The HMAC key a check may use (``trust.hmac_key_path``): inside
    ``trust.configured_only`` only a configured one, never ``./.architect`` on its own."""
    return trust.hmac_key_path()


def _read_key(*, create: bool) -> bytes:
    # Signing uses this host's own key file; a check uses only the key it may trust.
    path = trust.signing_key_path() if create else _key_path()
    if path is None:
        raise ValueError("No local signing key here")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid local signing key")
    return key


def _sign_report(payload: Mapping[str, object]) -> str:
    """Issue a test/verifier credential; never expose this to model tools."""
    return hmac.new(_read_key(create=True), _canonical_payload(payload), hashlib.sha256).hexdigest()


def _sealed(payload: Mapping[str, object], signature: str) -> dict[str, object]:
    """The seal's payload: the HMAC payload plus the HMAC itself."""
    return {**dict(payload), "signature": signature}


def _seal_report(payload: Mapping[str, object], signature: str) -> trust.Seal | None:
    """The Ed25519 seal of a Stage 4 report (``payload`` as for ``_sign_report``, and the
    HMAC it produced), or None on a host without an Ed25519 key. ``key_invalid``
    (``trust.TrustError``) when the key file exists but is not a usable key.

    Like ``_sign_report`` it is a credential: never expose it to model tools.
    """
    return trust._seal("report", _canonical_payload(_sealed(payload, signature)))


def seal_ready() -> None:
    """Fail now, not at signing time, when this host's Ed25519 key file exists but is not a
    usable key (``trust.TrustError("key_invalid")``); a host without one passes."""
    trust.load_private_key()


def hmac_key_present() -> bool:
    """Whether a check here has an HMAC key file to use; the key itself is never read."""
    path = _key_path()
    try:
        return path is not None and path.is_file()
    except OSError:
        return False


def verify_hmac(payload: Mapping[str, object], signature: str) -> bool:
    """This host's HMAC only; never creates a key."""
    if len(signature) != 64:
        return False
    try:
        expected = hmac.new(
            _read_key(create=False), _canonical_payload(payload), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (OSError, ValueError, TypeError):
        return False


def verify_seal(
    payload: Mapping[str, object], signature: str, seal: trust.Seal
) -> trust.SealCheck | None:
    """The report seal against this host's trust list (``payload`` and ``signature`` as
    for ``verify_hmac``); None when the payload cannot be serialized."""
    try:
        return trust.check_seal("report", _canonical_payload(_sealed(payload, signature)), seal)
    except (OSError, ValueError, TypeError):
        return None


def verify_report(
    payload: Mapping[str, object], signature: str, *, seal: trust.Seal | None = None
) -> bool:
    """Authenticate every payload value without creating a key on reads.

    True when this host's HMAC matches, or when ``seal`` verifies under a key this host
    trusts. A seal from an unlisted key never counts.
    """
    if verify_hmac(payload, signature):
        return True
    if seal is None:
        return False
    checked = verify_seal(payload, signature, seal)
    return checked is not None and checked.status == "valid"
