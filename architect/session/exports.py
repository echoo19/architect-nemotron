"""``export verify`` screens and the paid ``--reverify`` (Phase 10 Section 12.7).

``evidence_table`` draws only the content-free ``EVIDENCE.json`` fields (identities,
numbers, closed labels) and the check codes. ``reverify`` re-runs Stage 4 of the
package's approved Spec on this host's profile floor. It never executes code shipped in
the package: the package must first pass its own integrity, pin and children checks,
and Stage 4 then runs a private copy (under ``workspace/artifacts/reverify``) of this
host's own installed entry with the same artifact digest and spec hash. A package this
host never installed is ``export_unverified``; another host mints its own license only
through ``architect compile --approved-spec <package>/artifact/spec.json``. The package
itself is never written, and a failure deletes nothing. It is bounded by
``max_verifications_per_spec``.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from architect.profiles import forge_settings_of
from architect.runtime.wallet import compilation_wallet
from architect.spec import HarnessArtifact, Lifecycle

if TYPE_CHECKING:
    from architect.forge_modes import ExportCheck
    from architect.profiles import Services

__all__ = ["EVIDENCE_FIELDS", "ReverifyError", "evidence_table", "reverify", "reverify_line"]

WIDTH = 100
EVIDENCE_FIELDS = (
    "spec_hash", "artifact_digest", "verification_id", "floor_kind", "floor_score",
    "floor_limit", "held_out_count", "secondary_floor_kind", "secondary_floor_score",
    "secondary_floor_limit", "mean_cost_usd", "max_cost_usd", "max_compute_s", "model_class",
    "floor_origin", "preset", "evidence_label", "report_evidence", "architect_version",
)
_LIMIT = 1_048_576


class ReverifyError(ValueError):
    """A refused re-verification; ``str()`` is one fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:.4f}"
    if isinstance(value, str) and value and all(
        ch.isascii() and (ch.isalnum() or ch in "._:-+") for ch in value
    ):
        return value[:72]
    return "-"


def _evidence(package: Path) -> Mapping[str, object]:
    path = package / "EVIDENCE.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def evidence_table(package: Path, check: ExportCheck) -> str:
    """The ``EVIDENCE.json`` table and the check line; at most 100 ASCII columns."""
    evidence = _evidence(package)
    # W7: the label EVIDENCE.json states; the check line below says what verified here.
    signed = evidence.get("evidence_label") == "ed25519_signed"
    attested = "Ed25519-signed" if signed else "host-attested"
    lines = [f"export evidence ({attested}; tuning numbers are not a license)"]
    for name in EVIDENCE_FIELDS:
        lines.append(f"  {name:<22} {_safe(evidence.get(name))}")
    children = evidence.get("children")
    if isinstance(children, list):
        lines.append(f"  children {len(children)} (child licenses rest on their own weaker floors)")
    lines.append(
        f"check integrity {_safe(check.integrity)}  pins {_safe(check.pins)}  children "
        f"{_safe(check.children)}  licensed {_safe(check.licensed)}  registry_match "
        f"{_safe(check.registry_match)}"
    )
    lines.append("codes " + (",".join(check.codes) or "-"))
    return "\n".join(line[:WIDTH] for line in lines) + "\n"


def reverify_line(floor_kind: str, cap_usd: float) -> str:
    """The Section 12.7 consent line."""
    return f"reverify: Stage 4 on {floor_kind}; cap ${cap_usd:.2f} (API-equivalent, not a bill)"


def _host_copy(services: Services, artifact_digest: str, spec_hash: str) -> Path | None:
    """The directory of this host's installed entry with exactly these pins, or None."""
    from architect.registry import RegistryError

    try:
        entries = services.registry.list()
    except RegistryError:
        return None
    for entry in entries:
        try:
            artifact = HarnessArtifact.load(entry.directory)
        except (OSError, ValueError, TypeError):
            continue
        if artifact.artifact_digest == artifact_digest and artifact.spec.spec_hash == spec_hash:
            return Path(entry.directory)
    return None


async def reverify(
    package: Path, services: Services, consent: Callable[[str], bool]
) -> dict[str, JsonValue]:
    """Re-run Stage 4 of the exported approved Spec against this profile's floor."""
    from architect.compiler.verification import verify
    from architect.floors import FloorError
    from architect.forge_modes import ExportError, verify_export
    from architect.forge_modes.export import read_manifest

    # The package's own checks first; loading it to read pins executes nothing.
    try:
        check = await asyncio.to_thread(verify_export, package, registry=None)
    except ExportError as exc:
        raise ReverifyError(exc.code) from None
    if not (check.integrity and check.pins and check.children):
        shipped = ("export_integrity_mismatch", "export_pin_mismatch", "export_subharness_missing")
        raise ReverifyError(next((c for c in check.codes if c in shipped), "export_unverified"))
    try:
        manifest = (await asyncio.to_thread(read_manifest, package))[0]
    except ExportError as exc:
        raise ReverifyError(exc.code) from None
    # Stage 4 runs this host's own installed copy, never the package's shipped code.
    source = await asyncio.to_thread(
        _host_copy, services, manifest.artifact_digest, manifest.spec_hash
    )
    if source is None:
        raise ReverifyError("export_unverified")
    artifact = await asyncio.to_thread(HarnessArtifact.load, source)
    spec = artifact.spec
    if spec.lifecycle != Lifecycle.INSTALL:
        raise ReverifyError("export_unverified")
    compiler = services.compiler
    settings = forge_settings_of(services.profile.compile)
    library = compiler.library
    counter = getattr(library, "verification_count", None)
    count = counter(spec.spec_hash) if callable(counter) else 0
    if type(count) is int and count >= settings.max_verifications_per_spec:
        raise ReverifyError("forge_verification_cap")
    try:
        floor = compiler.floor_factory(spec.floor)
        secondary = (
            compiler.floor_factory(spec.secondary_floor) if spec.secondary_floor else None
        )
    except (FloorError, OSError, ValueError):
        raise ReverifyError("floor_unavailable") from None
    cap = compiler.config.compile_budget
    if not consent(reverify_line(spec.floor.kind.value, cap.cost_usd)):
        raise ReverifyError("export_cancelled")
    name = f"{artifact.artifact_digest[:16]}-{uuid.uuid4().hex[:8]}"
    copy = compiler.artifact_root / "reverify" / name

    def stage() -> HarnessArtifact:
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, copy, symlinks=False)
        return HarnessArtifact.load(copy)

    staged = await asyncio.to_thread(stage)
    with compilation_wallet(cap):
        verified = await verify(
            staged, floor, compiler.adapter_factory, compiler.runtime, secondary_floor=secondary
        )
    record = getattr(library, "record_forge_verification", None)
    if callable(record):
        record(spec.spec_hash)
    report = verified.report
    return {
        "licensed": verified.licensed,
        "verification_id": report.verification_id if report is not None else None,
        "floor_kind": spec.floor.kind.value,
        "floor_score": report.floor_score if report is not None else None,
        "floor_limit": report.floor_limit if report is not None else None,
        "held_out_count": report.held_out_count if report is not None else None,
        "note": "a new host-local Stage 4 report; the exported package was not changed",
    }
