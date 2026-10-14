"""Build mode's "Put it into software" destination (Phase 13, Section 7).

``export_agent(name, ctx, path)`` takes an installed, licensed agent and puts it into the
user's repository as a tested package, through the existing guarded export path and
nothing else:

1. ``forge_modes.export_package`` stages the package in the host-owned
   ``<workspace>/exports`` (the byte-identical licensed artifact, schemas, content-free
   evidence, generated tests, a pinned shim ``__init__.py`` and a manifest hashing every file).
2. The destination is checked by the same rules ``place_package`` applies
   (``forge_modes.export._target``: inside the working directory, no ``..``, no links,
   no control-plane path) and must not exist: nothing is ever overwritten.
3. The files and the destination are shown, and ``ctx.ui.approve`` asks before anything
   is written into the repository. A refusal writes nothing there (the staged copy stays
   in the workspace).
4. ``forge_modes.place_package`` creates exactly one new directory, and the placed copy is
   re-verified (``placement_codes`` and ``verify_export``: file hashes against the
   manifest, pins, sub-harnesses and the license, where this host's signing key or a
   trusted Ed25519 key can check it). A placed package that passes them is also kept in
   ``<workspace>/passed/<package>`` with a note of where it went (``placements.json``),
   so Undo can put that version back after a change (``architect.session.retake``).
5. The result says how to call it: a Python snippet (the package's ``run``, which is
   ``ExportedHarness.load`` plus one licensed run), an HTTP snippet for other languages
   (the local ``architect.server`` and curl on its real routes) and the test command.

The forge_modes semantics are kept and explained: the feature flag
(``ARCHITECT_FEATURE_<hash8>``) is off unless it is exactly ``"1"``, a human reviews the
package before it is set, and the license is host-attested (it verifies only where the
signing key that signed it is present; no key is ever shipped). W7: on a host holding an
Ed25519 key the license and manifest are Ed25519-signed too, and the package verifies
wherever that public key is trusted.
"""

from __future__ import annotations

import asyncio
import json
import keyword
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from pydantic import JsonValue

from architect.forge_modes import ExportError, export_package, place_package, verify_export
from architect.forge_modes._routing import FloorOrigin, evidence_class_of, provider_of
from architect.forge_modes.export import _MANIFEST as MANIFEST
from architect.forge_modes.export import (
    PACKAGE_NAME,
    SIGNED_LABEL,
    _target,
    evidence_label_of,
    flag_name,
)
from architect.forge_modes.integrate import _denied, placement_codes
from architect.registry import RegistryEntry
from architect.spec import HarnessArtifact

from .events import Notice, PlanCard, Progress
from .tools import ToolContext, ToolError

if TYPE_CHECKING:
    from architect.forge_modes import ExportCheck, ExportManifest

__all__ = [
    "DEFAULT_INTO",
    "DEFAULT_PORT",
    "Destination",
    "destination",
    "example_input",
    "export_agent",
    "http_snippet",
    "package_name",
    "python_snippet",
    "tests_command",
]

DEFAULT_INTO = "agents"
DEFAULT_PORT = 8765
KEY_ENV = "ARCHITECT_SIGNING_KEY_FILE"
TOKEN_ENV = "ARCHITECT_API_TOKEN"
_FILES_SHOWN = 30
_PACKAGE_CHARS = 40


# -- where the package goes ------------------------------------------------------------


def package_name(agent: str) -> str:
    """A Python package name for an agent: ``extracts-invoice-fields`` ->
    ``extracts_invoice_fields`` (``forge_modes``' rule: ``[a-z][a-z0-9_]{0,39}``)."""
    name = "_".join(re.findall(r"[a-z0-9]+", agent.lower())) or "agent"
    if not name[0].isalpha():
        name = f"agent_{name}"
    name = name[:_PACKAGE_CHARS].rstrip("_")
    if keyword.iskeyword(name):
        name = f"{name}_agent"
    if PACKAGE_NAME.fullmatch(name) is None:  # unreachable by construction; stay safe
        name = "agent"
    return name


@dataclass(frozen=True)
class Destination:
    """``<cwd>/<into>/<package>``; ``into`` is a POSIX path relative to the working
    directory (``"."`` for the root)."""

    into: str
    package: str

    @property
    def relative(self) -> str:
        return self.package if self.into == "." else f"{self.into}/{self.package}"

    @property
    def module(self) -> str | None:
        """The import path from the repository root, when every part is importable."""
        parts = [] if self.into == "." else self.into.split("/")
        if all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
            return ".".join([*parts, self.package])
        return None


def destination(cwd: Path, path: str | None, agent: str) -> Destination:
    """Where an export goes. No path: ``agents/<package>``. A path ending in ``/`` or
    naming an existing directory is the parent (the package keeps its name); any other
    path names the package directory itself, whose last part must be a Python package
    name. Paths outside the working directory are ``path_outside_cwd``.
    """
    if path is None or not path.strip():
        return Destination(DEFAULT_INTO, package_name(agent))
    text = path.strip().strip("\"'")
    trailing = text.endswith(("/", "\\"))
    posix = text.replace("\\", "/")
    candidate = Path(posix)
    if candidate.is_absolute() or candidate.drive or posix.startswith("/"):
        try:
            posix = candidate.resolve().relative_to(cwd.resolve()).as_posix()
        except (ValueError, OSError):
            raise ToolError("path_outside_cwd", text) from None
    parts = [part for part in PurePosixPath(posix).parts if part not in ("", ".")]
    if ".." in parts or any(part.startswith("~") for part in parts):
        raise ToolError("path_outside_cwd", text)
    if not parts:
        return Destination(".", package_name(agent))
    joined = "/".join(parts)
    if trailing or (cwd / joined).is_dir():
        return Destination(joined, package_name(agent))
    last = parts[-1]
    if PACKAGE_NAME.fullmatch(last) is None or keyword.iskeyword(last):
        raise ToolError(
            "export_package_invalid",
            f"{last}: the package directory must be a Python package name (lowercase "
            "letters, digits and _, starting with a letter)",
        )
    return Destination("/".join(parts[:-1]) or ".", last)


# -- what a call looks like --------------------------------------------------------------


def _placeholder(schema: object, depth: int = 0) -> JsonValue:
    if not isinstance(schema, Mapping) or depth > 3:
        return "..."
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]  # type: ignore[no-any-return]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), None)
    properties = schema.get("properties")
    if kind == "object" or (kind is None and isinstance(properties, Mapping)):
        if not isinstance(properties, Mapping) or not properties:
            return {}
        required = schema.get("required")
        names = [n for n in required if n in properties] if isinstance(required, list) else []
        chosen = names or list(properties)
        return {str(name): _placeholder(properties[name], depth + 1) for name in chosen[:8]}
    simple: dict[str, JsonValue] = {
        "array": [], "integer": 0, "number": 0, "boolean": False, "null": None,
    }
    return simple.get(kind, "...") if isinstance(kind, str) else "..."


def example_input(schema: Mapping[str, Any], example: JsonValue | None = None) -> JsonValue:
    """``example`` when it fits the input schema, else a placeholder shaped by it."""
    if example is not None:
        try:
            Draft202012Validator(dict(schema)).validate(example)
        except Exception:
            pass
        else:
            return example
    return _placeholder(schema)


def python_snippet(
    target: Destination,
    flag: str,
    value: JsonValue,
    profile: Path | None,
    workspace: Path,
    key: str | None,
    *,
    signed: bool = False,
) -> str:
    """Python: the package's ``verify`` and ``run`` (``ExportedHarness`` underneath).
    ``signed``: the license carries an Ed25519 seal (W7)."""
    lines = [
        "# Call the exported agent from Python (run from the repository root).",
        f"# {flag} must be 1, set only after a human has reviewed the package.",
        "import asyncio",
    ]
    if key:
        lines += ["import os"]
    module = target.module
    if module is None:
        lines += ["import sys", "", f'sys.path.insert(0, "{target.into}")']
        module = target.package
    if key:
        note = (
            "# This host's signing key; the Ed25519-signed license also verifies wherever "
            "its key is trusted."
            if signed
            else "# The license is host-attested: it verifies with this host's signing key only."
        )
        lines += ["", note, f'os.environ.setdefault("{KEY_ENV}", r"{key}")']
    profile_text = str(profile) if profile is not None else "<profile.json>"
    lines += [
        "",
        f"from {module} import FLAG, enabled, run, verify",
        "",
        "check = verify()  # ExportedHarness: file hashes, pins, sub-harnesses, license",
        "if check.codes:",
        '    raise SystemExit(f"package check failed: {check.codes}")',
        "if not enabled():",
        '    raise SystemExit(f"{FLAG} is off; set it to 1 after review")',
        "# run(): ExportedHarness.load re-verifies, then one licensed run within its budget",
        "output = asyncio.run(",
        "    run(",
        f"        {value!r},",
        f'        profile=r"{profile_text}",',
        f'        workspace=r"{workspace}",',
        "    )",
        ")",
        "print(output)",
    ]
    return "\n".join(lines)


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def http_snippet(
    target: Destination, value: JsonValue, profile: Path | None, port: int, compile_usd: float
) -> str:
    """Other languages: the local ``architect.server`` and curl on its real routes."""
    base = f"http://127.0.0.1:{port}"
    auth = f'-H "Authorization: Bearer ${TOKEN_ENV}" -H "Content-Type: application/json"'
    spec_path = f"{target.relative}/artifact/spec.json"
    body = json.dumps(
        {"artifact_id": "<artifact_id from step 2>", "input": value, "enabled": True,
         "human_reviewed": True},
        ensure_ascii=False,
    )
    to_request = (
        "import json,sys; print(json.dumps({'spec': json.load(open(sys.argv[1], "
        "encoding='utf-8')), 'approved': True}))"
    )
    profile_text = str(profile) if profile is not None else "<profile.json>"
    return "\n".join(
        [
            "# Other languages: the local Architect HTTP service (loopback, bearer token; POSIX",
            "# shell shown). 1. Start it, or type /serve in Architect, which prints its token:",
            f"export {TOKEN_ENV}=\"$(python -c "
            "'import secrets; print(secrets.token_urlsafe(32))')\"",
            f'uv run python -m architect.server --profile "{profile_text}" --port {port} &',
            "# 2. Once: the service runs only agents it licensed itself, so it licenses this",
            "#    agent's approved spec (a fresh Stage 4 on this host, up to the compile budget,",
            f"#    {_money(compile_usd)}); the answer carries the artifact_id.",
            f'uv run python -c "{to_request}" {spec_path} \\',
            f"  | curl -s -X POST {base}/compile {auth} --data-binary @-",
            "# 3. Call it (enabled and human_reviewed must both be true):",
            f"curl -s -X POST {base}/run {auth} \\",
            f"  -d {_shell_quote(body)}",
            f"# Status: curl -s {base}/status/<artifact_id> -H \"Authorization: Bearer "
            f"${TOKEN_ENV}\"",
        ]
    )


def tests_command(cwd: Path, target: Destination) -> str:
    runner = "uv run pytest" if (cwd / "uv.lock").is_file() else "python -m pytest"
    return f"{runner} {target.relative}/tests -p no:cacheprovider"


def _money(value: float) -> str:
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"


# -- export --------------------------------------------------------------------------------


def _floor_origin(ctx: ToolContext, artifact: HarnessArtifact) -> FloorOrigin:
    """``model_draft`` when the agent's floor holds SYNTHETIC cases, else ``user_dataset``.

    Phase 14: a floor that names a manifest is ``public_dataset`` when its cases match it
    and ``model_draft`` (SYNTHETIC) for every other outcome, as build mode labels it. A
    floor this folder's profile does not list or cannot read is judged by the cases saved
    in the workspace (``_saved_floor_origin``).
    """
    from architect.session.floors import summarize

    from .build import floor_provenance

    profile = ctx.services.profile
    dataset = artifact.spec.floor.dataset
    try:
        provenance = floor_provenance(profile, dataset)
        if provenance is not None:
            return "public_dataset" if provenance.origin == "public_dataset" else "model_draft"
        summary = summarize(profile, dataset, artifact.spec.floor.limit)
    except Exception:
        return _saved_floor_origin(ctx.services.workspace, dataset)
    return "model_draft" if summary.synthetic else "user_dataset"


def _saved_floor_origin(workspace: Path, dataset: str) -> FloorOrigin:
    """The origin of a floor the current profile does not list or cannot read, as when the
    agent was built in another folder: the cases build mode saved as
    ``<workspace>/floors/<dataset>.json`` say it. A floor that cannot be read there counts
    as SYNTHETIC (``model_draft``), never as the user's own data."""
    from architect.session.floors import _check_dataset, load_cases

    try:
        _check_dataset(dataset)  # a plain name, never a path
        cases = load_cases(workspace / "floors" / f"{dataset}.json")
    except Exception:
        return "model_draft"
    if not cases or any(case.metadata.get("synthetic") is True for case in cases):
        return "model_draft"
    return "user_dataset"


def _size(root: Path, manifest: ExportManifest) -> int:
    names = [name for name, _ in manifest.files] + [MANIFEST]
    return sum((root / name).stat().st_size for name in names)


def _files_text(manifest: ExportManifest) -> str:
    names = sorted([name for name, _ in manifest.files] + [MANIFEST])
    shown = names[:_FILES_SHOWN]
    if len(names) > len(shown):
        shown.append(f"... and {len(names) - len(shown)} more")
    return "\n".join(shown)


def _verified_text(check: ExportCheck, codes: list[str]) -> str:
    if (
        check.integrity and check.pins and check.children and check.licensed is True
        and not codes and not check.codes
    ):
        return "files, pins, sub-harnesses and the Stage 4 license: ok"
    found = list(dict.fromkeys([*codes, *check.codes]))
    if found == ["license_unverifiable"]:
        return (
            "files, pins and sub-harnesses: ok; license_unverifiable (no signing key on this "
            f"host: set {KEY_ENV})"
        )
    return "FAILED: " + ", ".join(found)


def _tests_note(tests: str, fingerprint: str | None) -> str:
    """How the package's own license tests behave (W7-7), and the CI check for a signed one."""
    if fingerprint is None:
        return f"{tests}  (license tests skip without {KEY_ENV})"
    return (
        f"{tests}  (license tests fail where key {fingerprint[:16]} is not trusted; in CI also "
        f"run architect verify --require-key {fingerprint} on the package)"
    )


def _entry(ctx: ToolContext, name: str) -> tuple[RegistryEntry, HarnessArtifact]:
    try:
        entry = ctx.services.registry.status(name)
        artifact = HarnessArtifact.load(entry.directory)  # the license, re-read from disk
    except Exception:
        raise ToolError("unknown_agent", name) from None
    if not artifact.licensed:
        raise ToolError("unlicensed_agent", name)
    return entry, artifact


async def export_agent(
    name: str,
    ctx: ToolContext,
    path: str | None = None,
    *,
    floor_origin: FloorOrigin | None = None,
    example: JsonValue | None = None,
    port: int = DEFAULT_PORT,
) -> dict[str, JsonValue]:
    """Export installed agent ``name`` into ``ctx.cwd`` (see the module docstring).

    Returns ``{"exported": true, "path", "package", "files", "verify", "python", "http",
    "test", "feature_flag", ...}``, or ``{"exported": false, "cancelled": true, ...}`` when
    the user declines (nothing is written into the repository). Refusals raise
    ``ToolError`` with a fixed code (``unknown_agent``, ``unlicensed_agent``,
    ``path_outside_cwd``, ``export_path_invalid``, ``export_target_exists``,
    ``export_package_invalid``, ``export_unverified``, ``export_failed``, ...).
    """
    services = ctx.services
    entry, artifact = _entry(ctx, name)
    target = destination(ctx.cwd, path, name)
    repo = ctx.cwd.resolve()
    denied = _denied(services)
    try:
        planned = _target(repo, target.into, target.package, denied)
    except ExportError as exc:
        raise ToolError(exc.code, target.relative) from None
    except (OSError, ValueError, RuntimeError):
        raise ToolError("export_path_invalid", target.relative) from None
    if planned.exists() or planned.is_symlink():
        raise ToolError("export_target_exists", target.relative)

    # Stage in the host-owned workspace (never the repository).
    ctx.ui.emit(Progress(f"export: staging {target.package} from {name} v{entry.revision} ..."))
    origin = floor_origin or _floor_origin(ctx, artifact)
    model_class = evidence_class_of(provider_of(services, artifact.model))
    try:
        manifest, manifest_sha = await asyncio.to_thread(
            export_package,
            entry,
            services.workspace / "exports",
            target.package,
            model_class=model_class,
            floor_origin=origin,
        )
    except ExportError as exc:
        raise ToolError(exc.code, name) from None
    staged = services.workspace / "exports" / manifest.artifact_digest[:16] / manifest.package
    files = len(manifest.files) + 1
    size = await asyncio.to_thread(_size, staged, manifest)
    flag = flag_name(manifest.spec_hash)
    kb = max(1, math.ceil(size / 1024))
    report = artifact.report
    floor = "-" if report is None else f"{report.floor_score:.2f}/{report.floor_limit:.2f}"
    signed = evidence_label_of(artifact) == SIGNED_LABEL
    seal = report.ed25519 if report is not None else None
    license_text = (
        f"Ed25519-signed with key {seal.fingerprint[:16]}: it verifies wherever that key "
        "is trusted"
        if signed and seal is not None
        else "checked only on this computer, with its signing key (host-attested)"
    )
    ctx.ui.emit(
        PlanCard(
            f"Export plan: {target.relative}/",
            (
                ("agent", f"{name} v{entry.revision}: licensed, floor {floor}, "
                 f"{artifact.preset} on {artifact.model}"),
                ("writes", f"{planned} (one new directory; nothing existing is changed)"),
                ("files", _files_text(manifest)),
                ("size", f"{files} files, {kb} KB"),
                ("feature flag", f"{flag}: off unless it is exactly 1"),
                ("review", "a human reviews the package before the flag is set; the license "
                 f"is {license_text}"),
            ),
            "Nothing has been written into the repository yet.",
        )
    )
    detail = (
        f"{target.package} ({files} files, {kb} KB) into {target.relative}/ (one new "
        "directory; feature flag OFF; human review required)"
    )
    answer = await ctx.ui.approve("export", detail)
    if answer not in ("yes", "session"):
        step = (
            f"/export {name} {target.relative} when you are ready (nothing was written into "
            "the repository)"
        )
        ctx.ui.emit(Notice(f"Not exported: {step}."))
        return {
            "exported": False,
            "cancelled": True,
            "agent": name,
            "path": target.relative,
            "staged": str(staged),
            "codes": ["export_cancelled"],
            "next_step": step,
        }

    # Place exactly one new directory, then re-verify the placed copy.
    try:
        await asyncio.to_thread(place_package, staged, repo, target.into, denied=denied)
    except ExportError as exc:
        raise ToolError(exc.code, target.relative) from None
    placed = repo / target.relative
    codes = await asyncio.to_thread(placement_codes, placed, manifest_sha, services.registry)
    check = await asyncio.to_thread(verify_export, placed, registry=services.registry)
    if not codes and not check.codes:
        # The version Undo puts back. Failing to keep it never fails the export: Undo
        # then falls back to the staged copy above, or says there is none.
        from architect.session.retake import remember_passed

        try:
            await asyncio.to_thread(
                remember_passed, services.workspace, placed, staged, agent=name
            )
        except (ExportError, OSError, ValueError):
            pass

    value = example_input(artifact.spec.input_schema, example)
    profile = ctx.extras.get("profile_path")
    profile_path = Path(profile) if isinstance(profile, str | Path) and str(profile) else None
    key = os.environ.get(KEY_ENV) or None
    python = python_snippet(
        target, flag, value, profile_path, services.workspace, key, signed=signed
    )
    wallet = services.profile.compile.compile_budget.cost_usd
    http = http_snippet(target, value, profile_path, port, wallet)
    tests = tests_command(ctx.cwd, target)
    verified = _verified_text(check, codes)
    step = (
        f"review {target.relative}/ (README.md, EVIDENCE.md), run {tests}, then set {flag}=1 "
        "where the software runs"
    )
    ctx.ui.emit(
        PlanCard(
            f"Exported: {target.relative}/",
            (
                ("verified", verified),
                ("python", python),
                ("http", http),
                ("tests", _tests_note(tests, seal.fingerprint if signed and seal else None)),
                ("enable", f"after review: {flag}=1"),
            ),
            "Human review required; the feature flag is OFF until set.",
        )
    )
    return {
        "exported": True,
        "agent": name,
        "revision": entry.revision,
        "package": target.package,
        "path": target.relative,
        "absolute": str(placed),
        "module": target.module,
        "files": files,
        "bytes": size,
        "manifest_sha256": manifest_sha,
        "verify": check.model_dump(mode="json"),
        "codes": list(dict.fromkeys([*codes, *check.codes])),
        "verified": verified,
        "feature_flag": flag,
        "human_review_required": True,
        "feature_flag_required": True,
        "license": license_text,
        "python": python,
        "http": http,
        "test": tests,
        "next_step": step,
    }
