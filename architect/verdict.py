"""``architect verify``: a plain verdict on a report, an artifact, an exported package or a
published report folder (W7).

Everything is checked offline, inside ``trust.configured_only``: the keys that count are
the ones ``--workspace`` or the ``ARCHITECT_*`` variables name, never ``./.architect`` on
its own. With neither, only the key shipped with Architect is trusted and no HMAC key
vouches. A seal counts only when the key that made it is on the trust list, found by the
fingerprint the seal names. A configured HMAC key still vouches for what it signed, so
legacy reports and packages verify on the host that made them, and the verdict names
that key's absolute path. ``require_key`` (``--require-key``) accepts only a seal by that
one key, so a result stripped back to the HMAC is refused.

The verdict is ``verified`` or ``refused: <code>``, then a few plain ASCII lines. For a
package the first of them say it as a person would: the model, the score, the pass mark
and which attempt at the exam that was (and for a retake, which cases it ran on), or the
stop sentence and the files that changed. Codes:

- ``artifact_changed``: bytes differ from what a trusted key signed (a file, a manifest,
  a report, or code bound to a signed report);
- ``key_untrusted``: signed by a key that is not on this host's trust list (or not the
  required key);
- ``unsigned``: a package or folder whose files no signature covers;
- ``license_unverifiable``: no seal, and no HMAC key here may check it;
- ``invalid_license``: no seal, and the HMAC key here did not sign it;
- ``unlicensed_artifact``: an authentic report that is not a license;
- ``trust_in_target``: the HMAC key or the trust list lies inside the folder being
  checked, so it cannot vouch for it;
- ``trust_list_invalid``, ``trust_list_missing``, ``published_unknown``,
  ``published_invalid`` and ``verify_target_unknown`` for what could not be checked at all;
- any other code of ``check_package`` for a package (pins, children, integrity).

Nothing here signs, creates a key or executes package code.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, ValidationError

from architect import trust
from architect._authority import hmac_key_present, verify_hmac, verify_seal
from architect.forge_modes.errors import ExportError
from architect.forge_modes.export import _MANIFEST as PACKAGE_MANIFEST
from architect.forge_modes.export import (
    changed_files,
    check_package,
    manifest_seal,
    read_manifest,
    read_pin,
    stop_error,
)
from architect.forge_modes.words import source_line, verdict_line
from architect.spec import HarnessArtifact, TestReport
from architect.spec.binding import check_subharness_tree
from architect.spec.license import LicenseCheck, check_license

__all__ = ["Verdict", "render_json", "verify_path", "verify_published"]

Kind = Literal["report", "artifact", "package", "published"]
_SHOWN = 12
_REPORT_LIMIT = 4 * 1_048_576
_TEXT_LIMIT = 80
_NO_SEAL = (
    "it carries no Ed25519 signature, and only the host that made it can check its HMAC"
)


@dataclass(frozen=True)
class Verdict:
    verified: bool
    code: str | None
    kind: Kind | None
    lines: tuple[str, ...] = ()
    signer: str | None = None  # the full fingerprint of the trusted key that vouched
    via: Literal["ed25519", "hmac"] | None = None
    changed: tuple[str, ...] = ()

    def text(self) -> str:
        head = "verified" if self.verified else f"refused: {self.code}"
        return "\n".join((head, *self.lines)) + "\n"

    def facts(self) -> dict[str, JsonValue]:
        return {
            "verified": self.verified,
            "code": self.code,
            "kind": self.kind,
            "signer": self.signer,
            "via": self.via,
            "changed": list(self.changed),
            "lines": list(self.lines),
        }


def _refused(
    code: str,
    kind: Kind | None,
    *lines: str,
    signer: str | None = None,
    changed: tuple[str, ...] = (),
) -> Verdict:
    return Verdict(False, code, kind, tuple(lines), signer=signer, changed=changed)


def _plain(text: str, limit: int = _TEXT_LIMIT) -> str:
    """Printable ASCII only: a signed report or an artifact's config may still carry
    terminal escapes, control characters or look-alike text, and none of it reaches the
    terminal. Anything else becomes ``?``."""
    shown = "".join(char if " " <= char <= "~" else "?" for char in text[: limit * 2])
    return shown[:limit] + ("..." if len(text) > limit else "")


def _path(path: Path) -> str:
    """An absolute path, printable ASCII only."""
    return _plain(os.path.abspath(path), 400)


def _key(value: trust.TrustedKey) -> str:
    return f"key {value.fingerprint[:16]} ({_plain(value.label, 200)})"


def _trust_lines(*signers: trust.TrustedKey | None) -> tuple[str, ...]:
    """Where a workspace key's trust comes from, as an absolute path, so a list planted
    in a downloaded folder shows itself; the shipped key needs no line."""
    paths = dict.fromkeys(
        _path(signer.path) for signer in signers
        if signer is not None and signer.source == "workspace" and signer.path is not None
    )
    return tuple(f"trust list: {path}" for path in paths)


def _required() -> str | None:
    policy = trust.active_policy()
    return policy.only if policy is not None else None


def _untrusted(fingerprint: str) -> str:
    required = _required()
    if required is not None:
        return f"signed by key {fingerprint[:16]}, not the required key {_plain(required[:16])}"
    if trust.trust_source()[0] is None:
        return (
            f"signed by key {fingerprint[:16]}; only the key shipped with Architect is "
            "trusted here (pass --workspace or set ARCHITECT_TRUST_FILE to use a trust list)"
        )
    return (
        f"signed by key {fingerprint[:16]}, which this workspace does not trust "
        "(architect keys list shows the keys it trusts)"
    )


def _no_seal(subject: str) -> str:
    required = _required()
    if required is not None:
        return (
            f"{subject} carries no Ed25519 signature, and --require-key accepts only a "
            f"signature by key {_plain(required[:16])}"
        )
    return f"{subject} {_NO_SEAL.removeprefix('it ')}"


def _hmac_where() -> str:
    path = trust.hmac_key_path()
    return _path(path) if path is not None else "?"


def _hmac_signed() -> str:
    """Which HMAC key vouched, by absolute path: never just "this host's own key"."""
    return (
        f"signed by the HMAC key at {_hmac_where()} (only a host holding that key can "
        "check it)"
    )


def _hmac_refused(subject: str) -> str:
    return (
        f"the HMAC key at {_hmac_where()} did not sign {subject}, and it carries no "
        "Ed25519 signature"
    )


def _all(count: int) -> str:
    return "the one file matches" if count == 1 else f"all {count} match"


def _listing(names: tuple[str, ...]) -> str:
    shown = list(names[:_SHOWN])
    if len(names) > _SHOWN:
        shown.append(f"and {len(names) - _SHOWN} more")
    return ", ".join(shown)


def _exam(report: TestReport) -> tuple[str, ...]:
    outcome = "passed" if report.passed else "failed"
    lines = [
        f"exam {outcome}: score {report.floor_score:.3f} against a bar of "
        f"{report.floor_limit:.3f} on {report.held_out_count} held-out cases"
    ]
    if report.held_out_counts is not None:
        lines.append(report.held_out_counts.readout(", "))
    if report.failures:
        lines.append("failures: " + ", ".join(_plain(item, 40) for item in report.failures[:6]))
    return tuple(lines)


def _identity(report: TestReport) -> str:
    # Both are validated as 64 hex digits (``spec.Digest``).
    return f"spec {report.spec_hash[:8]}, artifact {report.artifact_digest[:16]}"


def _signed_by(check: LicenseCheck) -> str:
    if check.via == "ed25519" and check.signer is not None:
        return "signed by " + _key(check.signer)
    return _hmac_signed()


# ------------------------------------------------------------------ reports


def _verify_report_file(path: Path) -> Verdict:
    try:
        if path.stat().st_size > _REPORT_LIMIT:
            raise ValueError
        data = path.read_bytes()
    except (OSError, ValueError):
        return _refused("verify_target_unknown", None, "this file cannot be read as a report")
    try:
        report = TestReport.model_validate_json(data)
    except (ValueError, ValidationError):
        if b'"verification_id"' in data:
            return _refused("artifact_changed", "report", "the report no longer parses")
        return _refused("verify_target_unknown", None, "this file is not an Architect report")
    payload = report.signed_payload()
    seal = None
    if report.ed25519 is not None:
        seal = verify_seal(payload, report.signature, report.ed25519)
        if seal is None or seal.status == "invalid":
            fingerprint = report.ed25519.fingerprint
            return _refused(
                "artifact_changed", "report",
                f"the report differs from what key {fingerprint[:16]} signed",
                signer=fingerprint,
            )
    if seal is not None and seal.status == "valid" and seal.signer is not None:
        signer_line = "signed by " + _key(seal.signer)
        via: Literal["ed25519", "hmac"] = "ed25519"
    elif verify_hmac(payload, report.signature):
        signer_line = _hmac_signed()
        via = "hmac"
    elif seal is not None:
        return _refused("key_untrusted", "report", _untrusted(seal.fingerprint))
    elif hmac_key_present():
        return _refused("invalid_license", "report", _hmac_refused("this report"))
    else:
        return _refused("license_unverifiable", "report", _no_seal("the report"))
    return Verdict(
        True, None, "report",
        (
            f"report for {_identity(report)}",
            *_exam(report),
            signer_line,
            "this checks the report alone; verify its folder or package to check the code too",
            *_trust_lines(seal.signer if via == "ed25519" and seal is not None else None),
        ),
        signer=seal.fingerprint if via == "ed25519" and seal is not None else None,
        via=via,
    )


# ------------------------------------------------------------------ artifacts


def _license_refusal(check: LicenseCheck, kind: Kind, *head: str) -> Verdict:
    seal = check.seal
    if check.reason == "changed":
        if seal is not None and seal.status == "invalid":
            line = f"the report differs from what key {seal.fingerprint[:16]} signed"
        else:
            line = "the code, configuration or spec differs from what the report signed"
        return _refused("artifact_changed", kind, *head, line,
                        signer=seal.fingerprint if seal is not None else None)
    if check.reason == "key_untrusted" and seal is not None:
        return _refused("key_untrusted", kind, *head, _untrusted(seal.fingerprint))
    if check.reason == "unverifiable":
        return _refused("license_unverifiable", kind, *head, _no_seal("the license"))
    if check.reason == "unverified":
        return _refused("invalid_license", kind, *head, _hmac_refused("the report"))
    if check.reason == "no_report":
        return _refused(
            "unlicensed_artifact", kind, *head, "there is no report, so nothing licenses it"
        )
    return _refused(
        "unlicensed_artifact", kind, *head,
        "the report is authentic, but it does not license this artifact (the exam or a "
        "gate failed)",
    )


def _verify_artifact(directory: Path) -> Verdict:
    try:
        artifact = HarnessArtifact.load(directory)
    except (OSError, ValueError, TypeError, RecursionError):
        return _refused("artifact_changed", "artifact", "the artifact files no longer load")
    check = check_license(artifact)
    if check.reason != "licensed":
        return _license_refusal(check, "artifact")
    try:
        check_subharness_tree(artifact)
    except (OSError, ValueError, TypeError, RecursionError):
        return _refused(
            "artifact_changed", "artifact",
            "a bound sub-harness is missing, unlicensed or differs from its binding",
        )
    report = artifact.report
    assert report is not None
    return Verdict(
        True, None, "artifact",
        (
            f"licensed artifact: {_identity(report)}, {_plain(artifact.preset)} on "
            f"{_plain(artifact.model)}",
            *_exam(report),
            _signed_by(check),
            *_trust_lines(check.signer if check.via == "ed25519" else None),
        ),
        signer=check.signer.fingerprint if check.signer is not None else None,
        via=check.via,
    )


# ------------------------------------------------------------------ packages


def _package_refusal(root: Path, label: str, code: str, codes: tuple[str, ...]) -> Verdict:
    try:
        manifest = read_manifest(root)[0]
    except ExportError:
        manifest = None
    changed = changed_files(root, manifest) if manifest is not None else ()
    # In plain words first: the stop sentence and the files that changed.
    stop = stop_error(root, code)
    lines = [line for line in (stop.sentence, stop.reason_line) if line]
    lines.append(label)
    if changed:
        lines.append("changed: " + _listing(changed))
    if code == "artifact_changed":
        sealed = manifest_seal(root, manifest) if manifest is not None else None
        if sealed is not None and sealed.status != "untrusted":
            lines.append(f"what key {sealed.fingerprint[:16]} signed no longer matches")
        else:
            lines.append("the files no longer match what was signed")
    elif code in ("key_untrusted", "license_unverifiable", "unsigned"):
        try:
            artifact = HarnessArtifact.load(root / "artifact")
            license_check: LicenseCheck | None = check_license(artifact)
        except (OSError, ValueError, TypeError, RecursionError):
            license_check = None
        sealed = manifest_seal(root, manifest) if manifest is not None else None
        if code == "unsigned":
            lines.append(
                "the license is signed, but the package manifest is not, so the other "
                "files cannot be checked"
            )
        elif code == "license_unverifiable":
            lines.append(_no_seal("the license"))
        elif license_check is not None and license_check.reason == "key_untrusted":
            assert license_check.seal is not None
            lines.append(_untrusted(license_check.seal.fingerprint))
        elif sealed is not None:
            lines.append("manifest " + _untrusted(sealed.fingerprint))
    elif code == "trust_in_target":
        lines.append("the HMAC key or the trust list lies inside this package")
    elif code in ("trust_list_invalid", "trust_list_missing"):
        lines.append("the trust list cannot be read, so no key is trusted")
    rest = tuple(item for item in codes if item != code)
    if rest:
        lines.append("other checks: " + ", ".join(rest))
    return _refused(code, "package", *lines, changed=changed)


def _verify_package(root: Path) -> Verdict:
    check = check_package(root, read_pin(root), registry=None)
    try:
        manifest = read_manifest(root)[0]
    except ExportError:
        manifest = None
    name = manifest.package if manifest is not None else root.name
    label = f"package {name}" if trust.safe_name(name) else "package"
    if check.codes or manifest is None:
        codes = check.codes or ("export_integrity_mismatch",)
        return _package_refusal(root, label, codes[0], codes)
    try:
        artifact = HarnessArtifact.load(root / "artifact")
    except (OSError, ValueError, TypeError, RecursionError):  # changed since the check
        return _package_refusal(root, label, "artifact_changed", ("artifact_changed",))
    license_check = check_license(artifact)
    sealed = manifest_seal(root, manifest)
    files = len(manifest.files) + 1
    report = artifact.report
    assert report is not None
    plain = tuple(
        line for line in (verdict_line(artifact.model, report), source_line(report)) if line
    )
    head = (
        *plain, label, f"license: {_identity(report)}", *_exam(report),
        _signed_by(license_check),
    )
    licensor = license_check.signer if license_check.via == "ed25519" else None
    if sealed is not None and sealed.status == "valid" and sealed.signer is not None:
        return Verdict(
            True, None, "package",
            (
                *head,
                f"files: {_all(files)} the manifest signed by {_key(sealed.signer)}",
                *_trust_lines(licensor, sealed.signer),
            ),
            signer=sealed.signer.fingerprint,
            via=license_check.via,
        )
    if license_check.via == "hmac":
        return Verdict(
            True, None, "package",
            (
                *head,
                f"files: {_all(files)} the manifest; no trusted key signed the "
                "manifest, so only a host holding that HMAC key can vouch for them",
            ),
            via="hmac",
        )
    # check_package refuses a seal-verified license beside an unsealed manifest.
    return _refused("unsigned", "package", label, "the package manifest is not signed")


# ------------------------------------------------------------------ published folders


def _ignored(names: tuple[str, ...]) -> tuple[str, ...]:
    """The line naming the operating system's own files that the check skipped."""
    if not names:
        return ()
    return ("ignored: " + _listing(tuple(_plain(name) for name in names))
            + " (files the operating system writes; no signature covers them)",)


def _verify_folder(folder: Path, name: str | None = None) -> Verdict:
    result = trust.check_published(folder, name=name)
    title = f"published report {result.name}" if trust.safe_name(result.name) else "published"
    if result.code is None and result.seal is not None and result.seal.signer is not None:
        signer = result.seal.signer
        return Verdict(
            True, None, "published",
            (
                title,
                f"files: {_all(len(result.files))} what {_key(signer)} signed",
                "listed: " + _listing(result.files),
                *_ignored(result.ignored),
                *_trust_lines(signer),
            ),
            signer=signer.fingerprint,
            via="ed25519",
        )
    code = result.code or "artifact_changed"
    lines = [title]
    if code == "unsigned":
        lines.append("there is no SIGNED.json here, so nothing in this folder is signed yet")
    elif code == "key_untrusted" and result.seal is not None:
        lines.append(_untrusted(result.seal.fingerprint))
    elif code == "artifact_changed" and name is not None and result.name != name:
        lines.append(f"these files were signed as {result.name}, not {name}")
    elif code == "artifact_changed":
        lines.append("changed: " + _listing(result.changed or (trust.PUBLISHED_FILE,)))
        lines.extend(_ignored(result.ignored))
    elif code in ("trust_list_invalid", "trust_list_missing"):
        lines.append("the trust list cannot be read, so no key is trusted")
    elif code == "published_invalid":
        lines.append("this is not a folder of published files")
    return _refused(code, "published", *lines, changed=result.changed)


def verify_published(name: str, *, require_key: str | None = None) -> Verdict:
    """``architect verify --published <name>``: the folder shipped in this install."""
    try:
        folder = trust.published_folder(name)
    except trust.TrustError as exc:
        available = sorted(
            path.name for path in trust.PUBLISHED_ROOT.glob("*")
            if path.is_dir() and trust.safe_name(path.name)
        ) if trust.PUBLISHED_ROOT.is_dir() else []
        lines = ["no published report by that name ships with this install"]
        if available:
            lines.append("available: " + ", ".join(available))
        return _refused(exc.code, "published", *lines)
    return _checked(folder, require_key, lambda: _verify_folder(folder, name))


# ------------------------------------------------------------------ dispatch


def _checked(
    root: Path | None, require_key: str | None, check: Callable[[], Verdict]
) -> Verdict:
    """``check`` with configured keys only (``trust.configured_only``; no HMAC key with
    ``require_key``), after the refusals that leave nothing to check: an HMAC key or a
    trust list inside ``root`` (the folder being checked; None for a report file), a
    trust list that cannot be read or is missing, and a required key that is not on the
    trust list."""
    with trust.configured_only(hmac=require_key is None):
        inside = trust.vouchers_inside(root) if root is not None else ()
        if inside:
            what, where = inside[0]
            return _refused(
                "trust_in_target", None,
                f"the {what} at {_path(where)} lies inside the folder being checked, so it "
                "cannot vouch for it",
            )
        listed = trust.load_trust()
        shown = _path(listed.path) if listed.path is not None else "?"
        if listed.error == "trust_list_missing":
            return _refused(
                "trust_list_missing", None,
                f"ARCHITECT_TRUST_FILE names {shown}, which does not exist, so no key is "
                "trusted",
            )
        if listed.error is not None:
            return _refused(
                "trust_list_invalid", None,
                f"the trust list at {shown} cannot be read, so no key is trusted; fix or "
                "remove it",
            )
        if require_key is not None and require_key not in listed.keys:
            return _refused(
                "key_untrusted", None,
                f"the required key {_plain(require_key[:16])} is not on the trust list here, "
                "so nothing can be checked against it; add it with architect keys trust or "
                "pass --workspace",
            )
        with trust.configured_only(only=require_key):
            return check()


def verify_path(path: Path, *, require_key: str | None = None) -> Verdict:
    """The verdict for a report file, an artifact folder, a package or a published folder.

    ``require_key``: the full fingerprint of the one key whose Ed25519 signature counts;
    no HMAC key vouches then, so a result stripped back to the HMAC is refused.
    """
    path = Path(path)
    try:
        if path.is_file():
            return _checked(None, require_key, lambda: _verify_report_file(path))
        if path.is_dir():
            if (path / trust.PUBLISHED_FILE).exists():
                return _checked(path, require_key, lambda: _verify_folder(path))
            if (path / PACKAGE_MANIFEST).exists() and (path / "__init__.py").exists():
                return _checked(path, require_key, lambda: _verify_package(path))
            if (path / "report.json").exists() and (path / "harness.py").exists():
                return _checked(path, require_key, lambda: _verify_artifact(path))
    except OSError:
        pass
    return _refused(
        "verify_target_unknown", None,
        "give a report.json, an artifact folder, an exported package or a published folder",
    )


def render_json(verdict: Verdict) -> str:
    return json.dumps(verdict.facts(), ensure_ascii=True, sort_keys=True)
