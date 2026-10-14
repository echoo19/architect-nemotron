"""Self-contained export packages of installed licensed harnesses (Phase 10 Section 12.3).

Trusted host code, never Pi. ``export_package`` stages one package under
``workspace/exports/<artifact_digest[:16]>/<package>/`` (``exports`` is Pi-denied):
the byte-identical artifact, its schemas, content-free evidence, generated tests, a
thin shim ``__init__.py`` with pins, and a ``MANIFEST.json`` hashing every file.
``place_package`` is the only writer into a repository and creates exactly one new
directory ``<into>/<package>``; ``verify_export`` re-hashes a package against its
manifest and pins and re-checks the license where it can be checked.

No held-out, tuning, authored or floor-dataset case content is ever exported, and no
signing key or HMAC is ever shipped. Without an Ed25519 key the license is
host-attested: only a host holding the HMAC key that signed it can check it.

W7: on a host holding an Ed25519 private key (``architect.trust``), the report inside
carries an Ed25519 seal and the manifest is sealed too (``ed25519``, which names the
key's fingerprint, never the key). Any host whose trust list holds that public key then
checks the license and every file without the HMAC key. When a license verifies by its
seal, the manifest must be sealed by a trusted key too: an unsealed manifest is
``unsigned`` and one sealed by a key this host does not trust is ``key_untrusted``. In a
sealed package any changed byte is ``artifact_changed``.

A package may come from anywhere, so ``check_package`` (and with it ``ExportedHarness``,
``export verify`` and the package's own tests) checks with configured keys only
(``trust.configured_only``): never ``./.architect`` on its own, and never an HMAC key or a
trust list that lies inside the package (``trust_in_target``). Every package carries a
``.gitattributes`` (``* -text``), so a checkout that converts line endings leaves its
signed bytes alone.

Retake: ``stop_error`` says in plain words why a package refuses to load (which files
differ from the ones that passed), ``stage_artifact`` stages a package from a licensed
artifact that is not in the registry and keeps the flag the app already sets, and
``replace_package`` puts a staged package where an existing one is.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import keyword
import marshal
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import CodeType
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

import architect
from architect import trust
from architect._authority import hmac_key_present
from architect.registry import Registry, RegistryEntry, RegistryError, _entry_fits
from architect.spec import HarnessArtifact, Lifecycle, SubharnessRef
from architect.spec.binding import check_subharness_tree, copy_subharness_tree
from architect.spec.license import check_license

from ._routing import EvidenceClass, FloorOrigin
from .errors import ExportError, StopReason
from .run import forge_summary

__all__ = [
    "ATTESTATION_NOTE",
    "EDITABLE_FILES",
    "EVIDENCE_LABEL",
    "SIGNED_LABEL",
    "ExportCheck",
    "ExportManifest",
    "cache_file_ok",
    "changed_files",
    "check_package",
    "default_package",
    "evidence_label_of",
    "export_package",
    "flag_name",
    "manifest_seal",
    "package_pin",
    "place_package",
    "read_flag",
    "read_pin",
    "replace_package",
    "stage_artifact",
    "stop_error",
    "verify_export",
]

ATTESTATION_NOTE = (
    "licence verifiable only on a host holding the Architect signing key; "
    "re-verify with `architect export verify`"
)
EVIDENCE_LABEL = "host_attested"
# W7: the report inside carries an Ed25519 seal any host trusting the key can check.
SIGNED_LABEL = "ed25519_signed"
PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ARTIFACT_FILES = ("harness.py", "spec.json", "config.json", "report.json")
# The agent's own editable files: a change confined to them may be retaken
# (``architect.session.retake``). A change to any other file can only be undone.
EDITABLE_FILES = ("artifact/spec.json", "artifact/config.json")
_FLAG = re.compile(r"^ARCHITECT_FEATURE_[0-9A-F]{8}$")
# Refusals that can hold while every file still matches the manifest.
_UNCHECKABLE = frozenset(
    {
        "license_unverifiable", "key_untrusted", "trust_list_invalid", "trust_list_missing",
        "trust_in_target", "export_unverified", "unsigned",
    }
)
# Interpreter and pytest caches are not package content, but they are never trusted
# blindly: a bytecode cache must prove it is the compiled form of a listed source.
_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache"})
_PYCACHE = "__pycache__"
_PYC_LIMIT = 16 * 1_048_576
# Nothing importable may sit in pytest's data cache.
_CODE_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyo", ".pyw", ".pyd", ".pth", ".so", ".dll", ".dylib", ".zip", ".egg"}
)
_MANIFEST = "MANIFEST.json"
_MANIFEST_LIMIT = 4 * 1_048_576
_INIT_LIMIT = 1_048_576
_TEMPLATES = Path(__file__).resolve().with_name("templates")
_INTO_LIMIT = 512
# Git must keep every file byte for byte as listed and signed: a checkout with
# ``core.autocrlf=true`` would otherwise rewrite line endings and refuse the package.
_GITATTRIBUTES = ".gitattributes"
_GITATTRIBUTES_TEXT = (
    b"# Architect checks every file here byte for byte against MANIFEST.json,\n"
    b"# so git must never convert their line endings.\n"
    b"* -text\n"
)

CaseOrigin = Literal["projection", "authored"]


def _listable(name: str) -> bool:
    """A file name a manifest may list: ``trust.safe_name`` (plain relative segments, no
    ``..``, no absolute or drive paths), or the package's ``.gitattributes``."""
    return name == _GITATTRIBUTES or trust.safe_name(name)


class ExportManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    package_format: Literal[1]
    architect_version: str
    package: str
    spec_hash: str
    artifact_digest: str
    verification_id: str
    execution_mode: Literal["native", "sandbox"]
    # name -> (artifact_digest, spec_hash, case_origin)
    children: dict[str, tuple[str, str, CaseOrigin]]
    # (package-relative POSIX path, sha256), sorted; excludes MANIFEST.json
    files: tuple[tuple[str, trust.Digest], ...]
    # W7: the Ed25519 seal over the canonical manifest without this field, on a host
    # holding an Ed25519 key. It names the key's fingerprint, never the key; omitted
    # when None, so unsealed manifests are byte-identical to earlier ones.
    ed25519: trust.Seal | None = None

    @model_validator(mode="after")
    def _plain_names(self) -> ExportManifest:
        """Every listed name stays inside the package: no ``..``, no absolute or drive
        path, no empty or doubled segment, listed once, and never the manifest itself."""
        names = [name for name, _ in self.files]
        if len(set(names)) != len(names) or not all(_listable(name) for name in names):
            raise ValueError("Manifest file names must be unique and plain")
        if _MANIFEST in names:
            raise ValueError("The manifest does not list itself")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_seal(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and self.ed25519 is None:
            data.pop("ed25519", None)
        return data


class ExportCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    integrity: bool
    pins: bool
    children: bool
    licensed: bool | None
    registry_match: bool | None
    codes: tuple[str, ...]


# ------------------------------------------------------------------ helpers


def default_package(spec_hash: str) -> str:
    return "architect_" + spec_hash[:8]


def flag_name(spec_hash: str) -> str:
    """The feature flag of a package as first exported: ``ARCHITECT_FEATURE_`` and the
    first eight characters of that spec's hash. The feature is off unless it is exactly
    ``"1"``. A retake that passes keeps this name although the spec's hash has changed,
    so the app's environment needs no edit; the package's own ``FLAG`` is the name to
    read (``read_flag``)."""
    return "ARCHITECT_FEATURE_" + spec_hash[:8].upper()


def _check_name(package: str) -> None:
    if (
        not isinstance(package, str)
        or PACKAGE_NAME.fullmatch(package) is None
        or keyword.iskeyword(package)
    ):
        raise ExportError("export_package_invalid")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _pretty(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("ascii")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _walk(root: Path) -> list[Path]:
    """Every file under ``root`` but the operating system's own (``trust.os_metadata``);
    a link or special file anywhere is ``export_failed``."""
    if _linked(root) or not root.is_dir():
        raise ExportError("export_failed")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if _linked(path):
            raise ExportError("export_failed")
        if path.is_file():
            if trust.os_metadata(path.name):
                continue
            files.append(path)
        elif not path.is_dir():
            raise ExportError("export_failed")
    return files


def _fits(paths: Iterable[Path]) -> None:
    """The registry's 246-character directory cap (``_entry_fits``) for every path."""
    try:
        for path in paths:
            _entry_fits(path)
    except RegistryError:
        raise ExportError("export_failed") from None


def _template(name: str, values: Mapping[str, str]) -> bytes:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    for token, value in values.items():
        text = text.replace(token, value)
    if "@@" in text:
        raise ExportError("export_failed")
    return text.encode("ascii")


def _children(artifact: HarnessArtifact) -> dict[str, tuple[str, str, CaseOrigin]]:
    if artifact.forge is None:
        return {}
    return {
        name: (ref.artifact_digest, ref.spec_hash, ref.case_origin)
        for name, ref in sorted(artifact.forge.subharnesses.items())
    }


def package_pin(manifest: ExportManifest) -> dict[str, JsonValue]:
    """The ``PIN`` constant written into the generated ``__init__.py``."""
    return {
        "package_format": manifest.package_format,
        "spec_hash": manifest.spec_hash,
        "artifact_digest": manifest.artifact_digest,
        "execution_mode": manifest.execution_mode,
        "children": {name: list(value) for name, value in manifest.children.items()},
    }


def _authentic(directory: Path) -> HarnessArtifact:
    """An installed, licensed, child-checked artifact on a fresh load."""
    try:
        artifact = HarnessArtifact.load(directory)
        if artifact.spec.lifecycle != Lifecycle.INSTALL or not artifact.licensed:
            raise ExportError("export_unverified")
        check_subharness_tree(artifact)
        return artifact
    except ExportError:
        raise
    except (OSError, ValueError, TypeError):
        raise ExportError("export_unverified") from None


def _sealed_by(artifact: HarnessArtifact) -> str | None:
    """The fingerprint of the trusted key whose seal licenses ``artifact`` here, or None:
    a seal that no longer verifies on this host (a rotated or dropped key) is no
    signature to advertise."""
    check = check_license(artifact)
    if check.via == "ed25519" and check.signer is not None:
        return check.signer.fingerprint
    return None


def evidence_label_of(artifact: HarnessArtifact) -> str:
    """``ed25519_signed`` when the report's Ed25519 seal verifies under this host's trust
    list (``check_license().via``), else ``host_attested``."""
    return SIGNED_LABEL if _sealed_by(artifact) is not None else EVIDENCE_LABEL


def _signed_note(artifact: HarnessArtifact) -> str:
    sealed = _sealed_by(artifact)
    assert sealed is not None
    return (
        f"licence signed with Ed25519 key {sealed[:16]}; any host that trusts that public "
        "key can check it with `architect verify`"
    )


def _evidence(
    artifact: HarnessArtifact,
    *,
    model_class: EvidenceClass | None,
    floor_origin: FloorOrigin,
) -> dict[str, JsonValue]:
    """Content-free evidence: identities, numbers and closed labels only."""
    report = artifact.report
    assert report is not None  # licensed
    label = evidence_label_of(artifact)
    summary = forge_summary(artifact.directory)
    forge: JsonValue = None
    if artifact.forge is not None:
        forge = {
            key: summary.get(key, 0)
            for key in ("checks", "dryruns", "measures", "submissions", "spawns", "agent_cost_usd")
        }
    children: list[JsonValue] = [
        {"name": name, "artifact_digest": digest, "case_origin": origin}
        for name, (digest, _spec_hash, origin) in _children(artifact).items()
    ]
    if model_class == "synthetic_fake":
        report_evidence = "synthetic_fake"
    elif floor_origin == "authored_cases":
        report_evidence = "authored_cases"
    else:
        report_evidence = "stage4_signed"
    return {
        "spec_hash": artifact.spec.spec_hash,
        "artifact_digest": artifact.artifact_digest,
        "verification_id": report.verification_id,
        "floor_kind": report.floor_kind.value,
        "floor_score": report.floor_score,
        "floor_limit": report.floor_limit,
        "held_out_count": report.held_out_count,
        "secondary_floor_kind": (
            report.secondary_floor_kind.value if report.secondary_floor_kind else None
        ),
        "secondary_floor_score": report.secondary_floor_score,
        "secondary_floor_limit": report.secondary_floor_limit,
        "mean_cost_usd": report.mean_cost_usd,
        "max_cost_usd": report.cost_usd,
        "max_compute_s": report.compute_s,
        "model_class": model_class,
        "floor_origin": floor_origin,
        "preset": artifact.preset,
        "forge": forge,
        "children": children,
        "evidence_label": label,
        "report_evidence": report_evidence,
        "exported_at": datetime.now(UTC).isoformat(),
        "architect_version": architect.__version__,
        "attestation_note": (
            ATTESTATION_NOTE if label == EVIDENCE_LABEL else _signed_note(artifact)
        ),
    }


# ------------------------------------------------------------------ export


def export_package(
    entry: RegistryEntry,
    staging_root: Path,
    package: str,
    *,
    model_class: EvidenceClass | None = None,
    floor_origin: FloorOrigin = "user_dataset",
) -> tuple[ExportManifest, str]:
    """Stage one package; returns ``(manifest, sha256 of MANIFEST.json)``.

    ``staging_root`` is ``workspace/exports``. An existing staged copy of the same
    package is replaced: staging is host-owned and never part of a repository.
    """
    _check_name(package)
    artifact = _authentic(entry.directory)
    if artifact.spec.spec_hash != entry.spec_hash or artifact.version != entry.artifact_version:
        raise ExportError("export_unverified")
    return _stage_directory(
        artifact, staging_root, package, flag=None, model_class=model_class,
        floor_origin=floor_origin,
    )


def stage_artifact(
    directory: Path,
    staging_root: Path,
    package: str,
    *,
    flag: str | None = None,
    model_class: EvidenceClass | None = None,
    floor_origin: FloorOrigin = "user_dataset",
) -> tuple[ExportManifest, str]:
    """Stage a package from the licensed artifact in ``directory``, registry or not.

    For a retake that passed: the artifact is the one Stage 4 just licensed, and ``flag``
    is the name the package it replaces already carries, so the app that sets it keeps
    working. Everything else is ``export_package``.
    """
    _check_name(package)
    if flag is not None and _FLAG.fullmatch(flag) is None:
        raise ExportError("export_failed")
    return _stage_directory(
        _authentic(Path(directory)), staging_root, package, flag=flag,
        model_class=model_class, floor_origin=floor_origin,
    )


def _stage_directory(
    artifact: HarnessArtifact,
    staging_root: Path,
    package: str,
    *,
    flag: str | None,
    model_class: EvidenceClass | None,
    floor_origin: FloorOrigin,
) -> tuple[ExportManifest, str]:
    directory = artifact.directory
    sources = _walk(directory)
    digest = artifact.artifact_digest
    parent = Path(staging_root) / digest[:16]
    destination = parent / package
    relative_dirs = sorted(
        {path.parent.relative_to(directory) for path in sources} | {Path(".")}
    )
    _fits(
        [destination / "artifact" / item for item in relative_dirs]
        + [destination / "schemas", destination / "tests"]
    )
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if _linked(parent):
            raise ExportError("export_failed")
        temporary = Path(tempfile.mkdtemp(prefix=".x-", dir=parent))
    except OSError:
        raise ExportError("export_failed") from None
    try:
        _fits([temporary / "artifact" / item for item in relative_dirs])
        manifest, manifest_sha = _stage(
            artifact, temporary, package, model_class=model_class, floor_origin=floor_origin,
            flag=flag,
        )
        if destination.exists() or _linked(destination):
            if _linked(destination):
                raise ExportError("export_failed")
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        return manifest, manifest_sha
    except ExportError:
        raise
    except (OSError, ValueError, TypeError, ValidationError):
        raise ExportError("export_failed") from None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _stage(
    artifact: HarnessArtifact,
    root: Path,
    package: str,
    *,
    model_class: EvidenceClass | None,
    floor_origin: FloorOrigin,
    flag: str | None = None,
) -> tuple[ExportManifest, str]:
    target = root / "artifact"
    target.mkdir()
    for name in _ARTIFACT_FILES:
        shutil.copyfile(artifact.directory / name, target / name)
    record = artifact.directory / "forge.json"
    if record.is_file():
        shutil.copyfile(record, target / "forge.json")
    copy_subharness_tree(artifact.directory, target)
    for path in _walk(target):
        _fsync(path)
    copied = HarnessArtifact.load(target)
    if copied.artifact_digest != artifact.artifact_digest or not copied.licensed:
        raise ExportError("export_failed")
    check_subharness_tree(copied)
    _write(root / "schemas" / "input.schema.json", _pretty(artifact.spec.input_schema))
    _write(root / "schemas" / "output.schema.json", _pretty(artifact.spec.output_schema))
    _write(
        root / "EVIDENCE.json",
        _pretty(_evidence(artifact, model_class=model_class, floor_origin=floor_origin)),
    )
    spec_hash = artifact.spec.spec_hash
    # A retake keeps the flag of the package it replaces. Nothing checks the name against
    # the spec's hash (``_pins``): it only has to be the one the app sets.
    flag = flag or flag_name(spec_hash)
    values = {
        "@@VERSION@@": architect.__version__,
        "@@PACKAGE@@": package,
        "@@FLAG@@": flag,
    }
    signed = evidence_label_of(artifact) == SIGNED_LABEL
    keyed = {**values, **_key_values(artifact)}
    readme_note = "license-signed.md.tmpl" if signed else "license-host.md.tmpl"
    evidence_note = "evidence-signed.md.tmpl" if signed else "evidence-host.md.tmpl"
    notes = {
        # A note ends without its newline: the parent template supplies it.
        "@@LICENSE_README@@": _template(readme_note, keyed).decode("ascii").rstrip(),
        "@@LICENSE_EVIDENCE@@": _template(evidence_note, keyed).decode("ascii").rstrip(),
        "@@WHERE@@": "where the license can be checked" if signed else "on the signing host",
    }
    _write(root / "EVIDENCE.md", _template("EVIDENCE.md.tmpl", {**values, **notes}))
    _write(root / "README.md", _template("README.md.tmpl", {**values, **notes}))
    _write(root / "tests" / f"test_{package}.py", _template("test.py.tmpl", {}))
    _write(root / _GITATTRIBUTES, _GITATTRIBUTES_TEXT)
    report = artifact.report
    assert report is not None
    draft = ExportManifest(
        package_format=1,
        architect_version=architect.__version__,
        package=package,
        spec_hash=spec_hash,
        artifact_digest=artifact.artifact_digest,
        verification_id=report.verification_id,
        execution_mode=artifact.execution_mode,
        children=_children(artifact),
        files=(),
    )
    pin = json.dumps(package_pin(draft), sort_keys=True, ensure_ascii=True)
    _write(
        root / "__init__.py",
        _template("init.py.tmpl", {"@@PIN@@": pin, "@@FLAG@@": json.dumps(flag)}),
    )
    files = tuple(
        (path.relative_to(root).as_posix(), _sha256(path))
        for path in _walk(root)
        if path.relative_to(root).as_posix() != _MANIFEST
    )
    manifest = draft.model_copy(update={"files": tuple(sorted(files))})
    # W7: seal the canonical manifest when this host holds an Ed25519 key.
    try:
        sealed = trust.seal("manifest", _canonical(manifest.model_dump(mode="json")))
    except trust.TrustError as exc:
        raise ExportError(exc.code) from None
    if sealed is not None:
        manifest = manifest.model_copy(update={"ed25519": sealed})
    data = _canonical(manifest.model_dump(mode="json"))
    _write(root / _MANIFEST, data)
    return manifest, hashlib.sha256(data).hexdigest()


def _key_values(artifact: HarnessArtifact) -> dict[str, str]:
    return {"@@KEY@@": _sealed_by(artifact) or "-"}


# ------------------------------------------------------------------ verification


def read_manifest(package_dir: Path) -> tuple[ExportManifest, str]:
    path = Path(package_dir) / _MANIFEST
    try:
        if _linked(path) or not path.is_file() or path.stat().st_size > _MANIFEST_LIMIT:
            raise ExportError("export_integrity_mismatch")
        data = path.read_bytes()
        return ExportManifest.model_validate_json(data), hashlib.sha256(data).hexdigest()
    except ExportError:
        raise
    except (OSError, ValueError):
        raise ExportError("export_integrity_mismatch") from None


def _unplaced(code: CodeType, depth: int = 0) -> CodeType:
    """``code`` with ``co_filename`` cleared, recursively through nested code constants.

    The import system rewrites ``co_filename`` to the real path at load time, so a moved
    package's genuine cache differs only there; every other field is kept.
    """
    if depth > 200:
        raise RecursionError
    consts = tuple(
        _unplaced(item, depth + 1) if isinstance(item, CodeType) else item
        for item in code.co_consts
    )
    return code.replace(co_filename="", co_consts=consts)


def _same_code(left: CodeType, right: CodeType) -> bool:
    """Code objects identical in every marshalled field except ``co_filename``.

    ``CodeType.__eq__`` is not enough: it ignores frame-layout fields such as
    ``co_stacksize`` (a forged one corrupts memory when the code runs) as well as
    ``co_qualname``. The comparison is therefore of the marshalled form, which holds
    every field the interpreter reads back, recursively through nested code. Marshal
    version 2 writes no back-references, so the bytes depend on the values only and
    not on how often an object happens to be referenced in this process.
    """
    return marshal.dumps(_unplaced(left), 2) == marshal.dumps(_unplaced(right), 2)


_INTERPRETER_FLAVOURS = ("", ".opt-1", ".opt-2")
_PYTEST_FLAVOUR = "-pytest-"
CacheRole = Literal["check", "ignore", "refuse"]


def _pytest_version() -> str | None:
    """The version of the pytest this interpreter can import; None when it has none."""
    try:
        import pytest
    except ImportError:
        return None
    version = getattr(pytest, "__version__", None)
    return version if isinstance(version, str) else None


def _cache_role(name: str) -> tuple[CacheRole, str, str]:
    """``(role, stem, flavour)`` of a file directly under ``__pycache__``.

    This interpreter's import system only ever opens ``<stem>.<cache_tag><opt>.pyc``
    (``opt`` is ``""``, ``".opt-1"`` or ``".opt-2"``), and pytest's assertion rewriter
    only ``<stem>.<cache_tag>-pytest-<its version>.pyc``: those are ``check``. Another
    interpreter's cache, and a rewrite cache of a pytest this interpreter does not have
    (none installed, or another version, as after an upgrade), are never loaded here:
    ``ignore``; the interpreter that would load one checks it itself. Anything else is
    ``refuse``, including a case variant of a loadable name (a case-insensitive file
    system opens it) and a bare ``<stem>.pyc``.
    """
    tag = sys.implementation.cache_tag
    stem, _, rest = name.partition(".")
    folded = rest.casefold()
    if not tag or not stem or not folded.endswith(".pyc") or len(folded) <= len(".pyc"):
        return "refuse", stem, ""
    own = tag.casefold()
    if not folded.startswith((own + ".", own + "-")):
        return "ignore", stem, ""
    if not rest.startswith(tag) or not rest.endswith(".pyc"):
        return "refuse", stem, ""
    flavour = rest[len(tag) : -len(".pyc")]
    if flavour in _INTERPRETER_FLAVOURS:
        return "check", stem, flavour
    if not flavour.casefold().startswith(_PYTEST_FLAVOUR):
        return "refuse", stem, flavour
    version = _pytest_version()
    if version is None:
        return "ignore", stem, flavour
    wanted = _PYTEST_FLAVOUR + version
    if flavour == wanted:
        return "check", stem, flavour
    return ("refuse" if flavour.casefold() == wanted.casefold() else "ignore"), stem, flavour


class _PassHookIni:
    """The one ini value pytest's ``AssertionRewriter`` reads from its ``Config``.

    A host repository may set ``enable_assertion_pass_hook = true``; pytest then writes
    its rewrite cache under the same name with extra (guarded) pass-hook calls. Any
    other ini lookup is unknown here and refuses the cache (``ValueError``).
    """

    def getini(self, name: str) -> bool:
        if name != "enable_assertion_pass_hook":
            raise ValueError(name)
        return True


def _expected_codes(source: bytes, path: Path, flavour: str) -> Iterator[CodeType]:
    """What the interpreter (``""``, ``".opt-1"``, ``".opt-2"``) or pytest's assertion
    rewriter (``"-pytest-<version>"``) compiles from ``source``; nothing when unknown.

    pytest's rewrite depends on the host's ini through ``enable_assertion_pass_hook``
    only, so both settings are candidates (the default one first). Each candidate is
    produced lazily: the second is built only when the first does not match.
    """
    if flavour in _INTERPRETER_FLAVOURS:
        optimize = int(flavour[-1]) if flavour else 0
        yield compile(source, str(path), "exec", dont_inherit=True, optimize=optimize)
        return
    if not flavour.startswith(_PYTEST_FLAVOUR):
        return
    try:
        import pytest
        from _pytest.assertion.rewrite import rewrite_asserts
    except ImportError:
        return
    if flavour != _PYTEST_FLAVOUR + pytest.__version__:
        return
    # pytest's own ``_rewrite_test``: without a Config (pass hook off), then with one
    # that enables the pass hook.
    hook_configs: tuple[Any, ...] = (None, _PassHookIni())
    for config in hook_configs:
        tree = ast.parse(source, filename=str(path))
        rewrite_asserts(tree, source, str(path), config)
        code: CodeType = compile(tree, str(path), "exec", dont_inherit=True)
        yield code


def cache_file_ok(root: Path, relative: str, listed: Mapping[str, str]) -> bool:
    """Whether one file under a cache directory is harmless (Section 12.3 integrity).

    A file under ``.pytest_cache`` is data and must not be importable. A file under
    ``__pycache__`` is judged by what the running interpreter would do with it
    (``_cache_role``). A cache it or its pytest would load must be the bytecode cache
    of a listed ``.py`` file in the parent directory, and its code must equal, field
    for field, what compiling the listed source (re-hashed here) produces. The
    invalidation mode is irrelevant: an unchecked hash, a forged source hash or a
    forged timestamp all run the embedded body, so the body itself is compared. A cache
    this interpreter can never load (another interpreter's, or another or an absent
    pytest's) cannot run here and is not a changed exported file; anything else (an
    unlisted source, an unknown optimisation level, garbage) is refused.
    """
    try:
        parts = PurePosixPath(relative).parts
        path = Path(root).joinpath(*parts)
        if not parts or ".." in parts or _linked(path) or not path.is_file():
            return False
        if _PYCACHE not in parts:
            return ".pytest_cache" in parts and path.suffix.lower() not in _CODE_SUFFIXES
        if parts.count(_PYCACHE) != 1 or len(parts) < 2 or parts[-2] != _PYCACHE:
            return False
        if ".pytest_cache" in parts:
            return False
        role, stem, flavour = _cache_role(parts[-1])
        if role != "check":
            return role == "ignore"
        source_name = PurePosixPath(*parts[:-2], stem + ".py").as_posix()
        digest = listed.get(source_name)
        source_path = Path(root).joinpath(*PurePosixPath(source_name).parts)
        if digest is None or _linked(source_path) or not source_path.is_file():
            return False
        source = source_path.read_bytes()
        if hashlib.sha256(source).hexdigest() != digest:
            return False
        if path.stat().st_size > _PYC_LIMIT:
            return False
        data = path.read_bytes()
        flags = int.from_bytes(data[4:8], "little")
        if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER or flags not in (0, 1, 3):
            return False
        cached = marshal.loads(data[16:])
        if not isinstance(cached, CodeType):
            return False
        return any(
            _same_code(cached, expected)
            for expected in _expected_codes(source, source_path, flavour)
        )
    except (OSError, ValueError, TypeError, EOFError, SyntaxError, RecursionError):
        return False


def _integrity(root: Path, manifest: ExportManifest) -> bool:
    try:
        listed = dict(manifest.files)
        if len(listed) != len(manifest.files) or _linked(root):
            return False
        present: set[str] = set()
        caches: list[str] = []
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if _linked(path):
                return False
            if path.is_file():
                if trust.os_metadata(path.name):
                    continue  # a Finder or Explorer visit, never part of the package
                if _CACHE_DIRS & set(relative.parts):
                    caches.append(relative.as_posix())
                else:
                    present.add(relative.as_posix())
            elif not path.is_dir():
                return False
        present.discard(_MANIFEST)
        if present != set(listed):
            return False
        if not all(_sha256(root / name) == digest for name, digest in listed.items()):
            return False
        return all(cache_file_ok(root, name, listed) for name in caches)
    except (OSError, ValueError):
        return False


def changed_files(root: Path, manifest: ExportManifest) -> tuple[str, ...]:
    """Listed files that are missing or differ, then files the manifest does not list
    (a genuine interpreter cache aside), by package-relative name. For display only:
    names a manifest may not list (``_listable``) are counted, never printed."""
    root = Path(root)
    listed = dict(manifest.files)
    changed: list[str] = []
    for name, digest in sorted(listed.items()):
        path = root.joinpath(*PurePosixPath(name).parts)
        try:
            same = not _linked(path) and path.is_file() and _sha256(path) == digest
        except OSError:
            same = False
        if not same:
            changed.append(name)
    extra: list[str] = []
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_dir() and not _linked(path):
                continue
            name = relative.as_posix()
            if name == _MANIFEST or (
                not _linked(path) and path.is_file() and trust.os_metadata(path.name)
            ):
                continue
            if _CACHE_DIRS & set(relative.parts):
                if not cache_file_ok(root, name, listed):
                    extra.append(name)
            elif name not in listed:
                extra.append(name)
    except OSError:
        pass
    shown = [name for name in changed + sorted(extra) if _listable(name)]
    hidden = len(changed) + len(extra) - len(shown)
    if hidden:
        shown.append(f"{hidden} more with unusual names")
    return tuple(shown)


def read_pin(package_dir: Path) -> dict[str, JsonValue] | None:
    """``PIN`` from the package's ``__init__.py``, read by AST (never executed)."""
    path = Path(package_dir) / "__init__.py"
    try:
        if _linked(path) or not path.is_file() or path.stat().st_size > _INIT_LIMIT:
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"), filename="__init__.py")
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "PIN"
            ):
                value = ast.literal_eval(node.value)
                return value if isinstance(value, dict) else None
    except (OSError, ValueError, SyntaxError, RecursionError):
        return None
    return None


def read_flag(package_dir: Path) -> str | None:
    """``FLAG`` from the package's ``__init__.py``, read by AST (never executed)."""
    path = Path(package_dir) / "__init__.py"
    try:
        if _linked(path) or not path.is_file() or path.stat().st_size > _INIT_LIMIT:
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"), filename="__init__.py")
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "FLAG"
            ):
                value = ast.literal_eval(node.value)
                if isinstance(value, str) and _FLAG.fullmatch(value) is not None:
                    return value
                return None
    except (OSError, ValueError, SyntaxError, RecursionError):
        return None
    return None


def stop_error(
    package_dir: Path, code: str, pin: Mapping[str, JsonValue] | None = None
) -> ExportError:
    """The refusal of a package that failed ``check_package``, with why in plain words.

    The files that differ from the ones the manifest lists are named. When they are all
    the agent's own editable files the reason is ``changed``: the change is in the files
    a retake reads, and ``architect.session.retake.status`` says whether it may be
    retaken. Any other changed file is ``changed_undo_only``. When no file differs, a key
    that is missing or not trusted here is ``cannot_check``: saying "changed" would be
    false. ``pin`` is the pin the caller checked with: when no file differs and the folder
    passes with the pin it carries now, the folder was written again after the caller
    imported it (a retake that passed, or an undo) and the reason is ``reloaded``.
    """
    root = Path(package_dir)
    try:
        manifest = read_manifest(root)[0]
    except ExportError:
        return ExportError(code, changed_files=(_MANIFEST,), reason="changed_undo_only")
    changed = changed_files(root, manifest)
    if not changed:
        if code in _UNCHECKABLE:
            return ExportError(code, reason="cannot_check")
        if pin is not None:
            current = read_pin(root)
            if (
                current is not None
                and dict(current) != dict(pin)
                and not check_package(root, current, registry=None).codes
            ):
                return ExportError(code, reason="reloaded")
        # Every listed file matches, so the list itself or the pins were rewritten.
        return ExportError(code, changed_files=(_MANIFEST,), reason="changed_undo_only")
    reason: StopReason = (
        "changed" if set(changed) <= set(EDITABLE_FILES) else "changed_undo_only"
    )
    return ExportError(code, changed_files=changed, reason=reason)


def _pin_children(pin: Mapping[str, JsonValue]) -> dict[str, tuple[str, ...]] | None:
    raw = pin.get("children")
    if not isinstance(raw, dict):
        return None
    children: dict[str, tuple[str, ...]] = {}
    for name, value in raw.items():
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            return None
        children[name] = tuple(str(v) for v in value)
    return children


def _pins(root: Path, manifest: ExportManifest, pin: Mapping[str, JsonValue]) -> bool:
    try:
        artifact = HarnessArtifact.load(root / "artifact")
        pinned_children = _pin_children(pin)
        expected = {
            "package_format": 1,
            "spec_hash": manifest.spec_hash,
            "artifact_digest": manifest.artifact_digest,
            "execution_mode": manifest.execution_mode,
        }
        return (
            all(pin.get(key) == value for key, value in expected.items())
            and manifest.package == root.name
            and artifact.artifact_digest == manifest.artifact_digest
            and artifact.spec.spec_hash == manifest.spec_hash
            and artifact.execution_mode == manifest.execution_mode
            and pinned_children is not None
            and pinned_children == {k: tuple(v) for k, v in manifest.children.items()}
            and {k: tuple(v) for k, v in _children(artifact).items()} == pinned_children
        )
    except (OSError, ValueError, TypeError):
        return False


def _bound(ref: SubharnessRef, directory: Path, depth: int = 0) -> bool:
    """A child is present and matches its binding; no key is needed for digests."""
    if depth > 4 or _linked(directory):
        return False
    child = HarnessArtifact.load(directory)
    if child.artifact_digest != ref.artifact_digest or child.spec.spec_hash != ref.spec_hash:
        return False
    if child.forge is None:
        return True
    return all(
        _bound(item, directory / "subharnesses" / name, depth + 1)
        for name, item in child.forge.subharnesses.items()
    )


def _children_ok(root: Path) -> bool:
    try:
        artifact = HarnessArtifact.load(root / "artifact")
        if artifact.forge is None:
            return True
        tree = root / "artifact" / "subharnesses"
        return all(
            _bound(ref, tree / name) for name, ref in artifact.forge.subharnesses.items()
        )
    except (OSError, ValueError, TypeError):
        return False


def _key_present() -> bool:
    """The local authority's rule (``hmac_key_present``): inside ``configured_only`` only a
    configured HMAC key counts. The key is never read here."""
    return hmac_key_present()


@dataclass(frozen=True)
class _License:
    """The packaged license as this host sees it.

    ``licensed`` is None when nothing here can check it; ``code`` is then
    ``license_unverifiable``, or ``key_untrusted`` for a seal from an unlisted key
    (``trust_list_invalid`` or ``trust_list_missing`` when no key is trusted at all).
    ``code`` is ``artifact_changed`` when a report sealed by a trusted key no longer
    matches its seal or the artifact. ``via`` names the signature that authenticated the
    report.
    """

    licensed: bool | None
    code: str | None = None
    via: Literal["hmac", "ed25519"] | None = None


def _license(root: Path) -> _License:
    try:
        artifact = HarnessArtifact.load(root / "artifact")
    except (OSError, ValueError, TypeError):
        return _License(False) if _key_present() else _License(None, "license_unverifiable")
    check = check_license(artifact)
    if check.licensed is None:
        if check.reason != "key_untrusted":
            return _License(None, "license_unverifiable")
        # A trust list that cannot be read, or a named one that is missing, trusts
        # nothing: say so rather than blame the key.
        return _License(None, trust.load_trust().error or "key_untrusted")
    if not check.licensed:
        changed = check.reason == "changed" and check.trusted_seal
        return _License(False, "artifact_changed" if changed else None, check.via)
    if artifact.spec.lifecycle != Lifecycle.INSTALL:
        return _License(False, None, check.via)
    try:
        check_subharness_tree(artifact)
    except (OSError, ValueError, TypeError):
        return _License(False, None, check.via)
    return _License(True, None, check.via)


def manifest_seal(root: Path, manifest: ExportManifest) -> trust.SealCheck | None:
    """The manifest's seal on this host's trust list; None when it carries none.

    A sealed manifest must be byte-for-byte the canonical form that was signed: any other
    bytes, even ones that parse to the same content, are ``invalid``.
    """
    if manifest.ed25519 is None:
        return None
    body = _canonical(manifest.model_dump(mode="json", exclude={"ed25519"}))
    checked = trust.check_seal("manifest", body, manifest.ed25519)
    if checked.status != "valid":
        return checked
    try:
        raw = (Path(root) / _MANIFEST).read_bytes()
    except OSError:
        raw = b""
    if raw != _canonical(manifest.model_dump(mode="json")):
        return trust.SealCheck("invalid", manifest.ed25519.fingerprint)
    return checked


def _trusted_report_seal(root: Path) -> bool:
    """The packaged report is sealed by a trusted key (read without loading the artifact)."""
    path = root / "artifact" / "report.json"
    try:
        if _linked(path) or not path.is_file() or path.stat().st_size > _MANIFEST_LIMIT:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("ed25519") if isinstance(payload, dict) else None
        if value is None:
            return False
        sealed = trust.Seal.model_validate(value)
    except (OSError, ValueError, RecursionError):
        return False
    return sealed.fingerprint in trust.load_trust().keys


def _registry_match(registry: Registry, manifest: ExportManifest) -> bool:
    try:
        entries = registry.list()
    except RegistryError:
        return False
    for entry in entries:
        try:
            artifact = HarnessArtifact.load(entry.directory)
        except (OSError, ValueError, TypeError):
            continue
        if (
            artifact.artifact_digest == manifest.artifact_digest
            and artifact.spec.spec_hash == manifest.spec_hash
        ):
            return True
    return False


def check_package(
    package_dir: Path, pin: Mapping[str, JsonValue] | None, *, registry: Registry | None
) -> ExportCheck:
    """Integrity, pins, children, license and registry match.

    The license is checked with this host's HMAC key or, for a sealed report, with a key
    on this host's trust list (W7). Any code refuses the package; the W7 codes lead:

    - ``artifact_changed``: bytes a trusted key signed changed (a sealed manifest that no
      longer verifies, a file that differs from a verified sealed manifest, a sealed
      report that no longer matches, or an unreadable manifest beside a report sealed by
      a trusted key). A manifest seal that no longer verifies also clears ``integrity``.
    - ``unsigned`` / ``key_untrusted``: the license verifies by its Ed25519 seal, so every
      file must be covered by a manifest seal from a trusted key too, and that seal is
      missing or from an unlisted key. (A license this host vouches for by its own HMAC
      alone keeps the old rule: the manifest's file list is checked, unsigned.)

    Only configured keys count (``trust.configured_only``): the HMAC key that
    ``ARCHITECT_SIGNING_KEY_FILE`` names and the trust list that ``ARCHITECT_TRUST_FILE``
    (or that key's workspace) names, else the shipped key alone; never ``./.architect`` on
    its own. When the HMAC key or the trust list lies inside the package, nothing here
    may vouch for it: ``trust_in_target`` leads and the license is left unchecked.
    """
    root = Path(package_dir)
    with trust.configured_only():
        return _check_package(root, pin, registry)


def _check_package(
    root: Path, pin: Mapping[str, JsonValue] | None, registry: Registry | None
) -> ExportCheck:
    try:
        manifest: ExportManifest | None = read_manifest(root)[0]
    except ExportError:
        manifest = None
    integrity = manifest is not None and _integrity(root, manifest)
    pins = manifest is not None and pin is not None and _pins(root, manifest, pin)
    children = manifest is not None and _children_ok(root)
    signed: list[str] = []
    licensed: bool | None
    license_code: str | None
    inside = bool(trust.vouchers_inside(root))
    if inside:
        # What arrived with the package never vouches for it: no license check at all.
        licensed, license_code = None, None
        signed.append("trust_in_target")
    elif manifest is not None:
        state = _license(root)
        licensed, license_code = state.licensed, state.code
        sealed = manifest_seal(root, manifest)
        status = sealed.status if sealed is not None else None
        if status == "invalid":
            integrity = False
        if (
            license_code == "artifact_changed"
            or status == "invalid"
            or (status == "valid" and not integrity)
        ):
            signed.append("artifact_changed")
        elif state.via == "ed25519" and status != "valid":
            signed.append("unsigned" if status is None else "key_untrusted")
    else:
        licensed = None if not _key_present() else False
        license_code = None
        if _trusted_report_seal(root):
            signed.append("artifact_changed")
            if licensed is None:
                licensed = False
    match = None
    if registry is not None:
        match = manifest is not None and _registry_match(registry, manifest)
    codes: list[str] = list(signed)
    if not integrity:
        codes.append("export_integrity_mismatch")
    if not pins:
        codes.append("export_pin_mismatch")
    if not children:
        codes.append("export_subharness_missing")
    if licensed is None and not inside:
        codes.append(license_code or "license_unverifiable")
    elif licensed is False:
        codes.append("export_unverified")
    return ExportCheck(
        integrity=integrity,
        pins=pins,
        children=children,
        licensed=licensed,
        registry_match=match,
        codes=tuple(codes),
    )


def verify_export(package_dir: Path, *, registry: Registry | None) -> ExportCheck:
    """``architect export verify``: the package's own pins are read without executing it."""
    root = Path(package_dir)
    return check_package(root, read_pin(root), registry=registry)


# ------------------------------------------------------------------ placement


def _protected(repo: Path, denied: Iterable[Path]) -> tuple[Path, ...]:
    """Host-owned paths a package may never land under (as ``PiBackend`` protects)."""
    values = (
        *denied,
        trust.signing_key_path(),
        *trust.protected_paths(),
        repo / ".architect",
        Path(architect.__file__).resolve().parent,
        Path(sys.prefix),
        Path(sys.base_prefix),
    )
    return tuple(Path(path).resolve() for path in values)


def _target(repo: Path, into: str, package: str, denied: Iterable[Path]) -> Path:
    """``repo / into / package`` under ``PiBackend._granted_path``'s rules."""
    if (
        not isinstance(into, str)
        or not into
        or len(into) > _INTO_LIMIT
        or into.startswith(("~", "@"))
        or any(ord(char) < 32 or (char.isspace() and char != " ") for char in into)
    ):
        raise ExportError("export_path_invalid")
    candidate = Path(into)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise ExportError("export_path_invalid")
    if ".." in candidate.parts or ":" in into:
        raise ExportError("export_path_invalid")
    if os.name == "nt" and any(part.endswith((".", " ")) for part in candidate.parts):
        raise ExportError("export_path_invalid")
    base = repo.resolve(strict=True)
    path = base / candidate / package
    resolved = path.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ExportError("export_path_invalid")
    if any(part.casefold() == ".architect" for part in resolved.relative_to(base).parts):
        raise ExportError("export_path_invalid")
    for protected in _protected(base, denied):
        # Without case: on APFS and NTFS a case variant names the same folder.
        if trust.within(resolved, protected):
            raise ExportError("export_path_invalid")
    current = path
    while current != base:
        if _linked(current):
            raise ExportError("export_path_invalid")
        if current.exists() and current.is_file():
            if current.stat().st_nlink > 1:
                raise ExportError("export_path_invalid")
        if current == current.parent:
            raise ExportError("export_path_invalid")
        current = current.parent
    return path


def place_package(
    staged: Path, repo: Path, into: str, *, denied: Iterable[Path] = ()
) -> ExportManifest:
    """Copy a staged package to ``repo / into / <package>``: one new directory only.

    Shared by the single-use Pi tool and ``--package-only``. Existing files are never
    edited; an existing target is ``export_target_exists``. The staged copy is
    re-hashed before and the placed copy after the copy.
    """
    staged = Path(staged)
    manifest, _ = read_manifest(staged)
    if manifest.package != staged.name or not _integrity(staged, manifest):
        raise ExportError("export_integrity_mismatch")
    try:
        target = _target(Path(repo), into, manifest.package, denied)
    except ExportError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise ExportError("export_path_invalid") from None
    if target.exists() or _linked(target):
        raise ExportError("export_target_exists")
    names = [name for name, _ in manifest.files]
    _fits({(target / name).parent for name in names} | {target})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _linked(target.parent):
            raise ExportError("export_path_invalid")
        target.mkdir()
    except FileExistsError:
        raise ExportError("export_target_exists") from None
    except OSError:
        raise ExportError("export_failed") from None
    try:
        for name in [*names, _MANIFEST]:
            source = staged / name
            if _linked(source) or not source.is_file():
                raise ExportError("export_integrity_mismatch")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
    except (ExportError, OSError) as exc:
        # Only the directory this call just created is removed; nothing else is touched.
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, ExportError):
            raise
        raise ExportError("export_failed") from None
    placed, _ = read_manifest(target)
    if placed != manifest or not _integrity(target, manifest):
        raise ExportError("export_integrity_mismatch")
    return manifest


def _put_back(backup: Path, target: Path) -> None:
    """Move the old package back to where it was. If even that fails the old package is
    still whole in ``backup``, and the error names it (``ExportError.aside``)."""
    try:
        os.replace(backup, target)
    except OSError:
        raise ExportError("export_failed", aside=backup) from None


def replace_package(staged: Path, target: Path) -> ExportManifest:
    """Put a staged package where the existing package ``target`` is.

    For a retake that passed and for Undo. The staged copy is re-hashed, copied beside the
    target and re-hashed again; only then is the old folder moved aside and the new one
    moved in. The placed package must pass ``check_package``: if it does not, or anything
    fails on the way, the old folder is put back as it was. Nothing outside ``target`` and
    two short-lived dot folders beside it is touched. If the old folder cannot be moved
    back either, it stays whole in the dot folder the error names (``ExportError.aside``).
    """
    staged, target = Path(staged), Path(target)
    manifest, _ = read_manifest(staged)
    if (
        manifest.package != staged.name
        or manifest.package != target.name
        or not _integrity(staged, manifest)
    ):
        raise ExportError("export_integrity_mismatch")
    if _linked(target) or not target.is_dir() or _linked(target.parent):
        raise ExportError("export_path_invalid")
    names = [name for name, _ in manifest.files]
    token = uuid4().hex[:8]
    fresh = target.parent / f".{target.name}.new-{token}"
    backup = target.parent / f".{target.name}.old-{token}"
    _fits({(fresh / name).parent for name in names} | {fresh, backup})
    try:
        fresh.mkdir()
        for name in [*names, _MANIFEST]:
            source = staged / name
            if _linked(source) or not source.is_file():
                raise ExportError("export_integrity_mismatch")
            destination = fresh / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
        if read_manifest(fresh)[0] != manifest or not _integrity(fresh, manifest):
            raise ExportError("export_integrity_mismatch")
        os.replace(target, backup)
        try:
            os.replace(fresh, target)
        except OSError:
            _put_back(backup, target)
            raise
    except (ExportError, OSError) as exc:
        shutil.rmtree(fresh, ignore_errors=True)
        if isinstance(exc, ExportError):
            raise
        raise ExportError("export_failed") from None
    check = check_package(target, read_pin(target), registry=None)
    if check.codes:
        # Put the old folder back exactly as it was; the refused copy is removed.
        refused = target.parent / f".{target.name}.bad-{token}"
        try:
            os.replace(target, refused)
        except OSError:
            raise ExportError("export_failed", aside=backup) from None
        _put_back(backup, target)
        shutil.rmtree(refused, ignore_errors=True)
        raise ExportError(check.codes[0])
    shutil.rmtree(backup, ignore_errors=True)
    return manifest
