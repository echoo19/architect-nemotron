"""Why an artifact is or is not licensed on this host (W7).

``HarnessArtifact.licensed`` answers yes or no. ``check_license`` also says which
signature vouched for the report (this host's HMAC, or an Ed25519 seal from a key on this
host's trust list) and, when the answer is no, why:

- ``unverifiable``: no HMAC key here and no seal, so nothing here can check the report;
- ``key_untrusted``: the report is sealed by a key this host does not trust;
- ``unverified``: this host's HMAC key did not sign the report and nothing else did;
- ``changed``: the signed bytes and the artifact differ (a seal that no longer verifies,
  or an authentic report bound to other code, configuration or spec);
- ``not_a_license``: an authentic, bound report that does not pass every gate;
- ``no_report``: there is no report at all.

It reads only; it never signs, and it never creates a key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from architect._authority import hmac_key_present, verify_hmac, verify_seal
from architect.spec import HarnessArtifact
from architect.trust import SealCheck, TrustedKey

__all__ = ["LicenseCheck", "Reason", "check_license"]

Reason = Literal[
    "licensed", "no_report", "unverifiable", "key_untrusted", "unverified", "changed",
    "not_a_license",
]


@dataclass(frozen=True)
class LicenseCheck:
    reason: Reason
    via: Literal["hmac", "ed25519"] | None = None  # what authenticated the report
    seal: SealCheck | None = None  # the report's seal on this host's trust list, if sealed

    @property
    def licensed(self) -> bool | None:
        """True, False, or None when nothing on this host can decide."""
        if self.reason == "licensed":
            return True
        if self.reason in ("unverifiable", "key_untrusted"):
            return None
        return False

    @property
    def signer(self) -> TrustedKey | None:
        return self.seal.signer if self.seal is not None else None

    @property
    def trusted_seal(self) -> bool:
        """The report carries a seal from a key this host trusts (valid or not)."""
        return self.seal is not None and self.seal.status in ("valid", "invalid")


def check_license(artifact: HarnessArtifact) -> LicenseCheck:
    report = artifact.report
    if report is None:
        return LicenseCheck("no_report")
    payload = report.signed_payload()
    seal: SealCheck | None = None
    if report.ed25519 is not None:
        seal = verify_seal(payload, report.signature, report.ed25519)
        if seal is None:  # the payload cannot be serialized: not what was signed
            seal = SealCheck("invalid", report.ed25519.fingerprint)
    via: Literal["hmac", "ed25519"] | None = None
    if seal is not None and seal.status == "valid":
        via = "ed25519"
    elif verify_hmac(payload, report.signature):
        via = "hmac"
    if via is None:
        if seal is not None and seal.status == "invalid":
            return LicenseCheck("changed", seal=seal)
        if seal is not None:
            return LicenseCheck("key_untrusted", seal=seal)
        return LicenseCheck("unverified" if hmac_key_present() else "unverifiable")
    try:
        bound = (
            report.spec_hash == artifact.spec.spec_hash
            and report.artifact_digest == artifact.artifact_digest
        )
    except (OSError, ValueError, TypeError):
        bound = False
    if not bound:
        return LicenseCheck("changed", via, seal)
    if not artifact.licensed:
        return LicenseCheck("not_a_license", via, seal)
    return LicenseCheck("licensed", via, seal)
