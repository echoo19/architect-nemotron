"""Phase 10 Sections 12.3-12.7 (WP3): export packages, the shim, placement, verification.

Offline only (fake adapters, sockets blocked). The generated tests run in a temp
repository through a ``pytest`` subprocess, with and without the host signing key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from forge_modes_support import (
    FINGERPRINT,
    block_sockets,
    make_profile,
    services_for,
    spec,
    tree_digest,
)

from architect.forge_modes import (
    ExportedHarness,
    ExportError,
    FeatureDisabled,
    export_package,
    place_package,
    verify_export,
)
from architect.forge_modes.export import (
    ATTESTATION_NOTE,
    changed_files,
    flag_name,
    read_manifest,
    read_pin,
)
from architect.models import FakeAdapter
from architect.registry import Registry, RegistryEntry
from architect.runtime import Runtime, UnlicensedArtifactError
from architect.spec import HarnessArtifact, Lifecycle
from architect.telemetry import TelemetryStore

PACKAGE = "ok_feature"
EVIDENCE_KEYS = {
    "spec_hash", "artifact_digest", "verification_id", "floor_kind", "floor_score",
    "floor_limit", "held_out_count", "secondary_floor_kind", "secondary_floor_score",
    "secondary_floor_limit", "mean_cost_usd", "max_cost_usd", "max_compute_s", "model_class",
    "floor_origin", "preset", "forge", "children", "evidence_label", "report_evidence",
    "exported_at", "architect_version", "attestation_note",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_sockets(monkeypatch)


async def _installed_entry(tmp_path: Path) -> tuple[RegistryEntry, Registry, Path]:
    services = services_for(make_profile(tmp_path))
    try:
        compiled = await services.compiler.compile(
            spec(lifecycle=Lifecycle.INSTALL), approved=True
        )
        assert compiled.artifact is not None and compiled.artifact.licensed
        services.registry.install("feature-ok", compiled.artifact)
        return services.registry.status("feature-ok"), services.registry, services.workspace
    finally:
        services.close()


@pytest.fixture
def installed(tmp_path: Path) -> tuple[RegistryEntry, Registry, Path]:
    return asyncio.run(_installed_entry(tmp_path))


def _export(
    installed: tuple[RegistryEntry, Registry, Path], package: str = PACKAGE
) -> tuple[Path, Any, str]:
    entry, _, workspace = installed
    manifest, digest = export_package(
        entry, workspace / "exports", package, model_class="synthetic_fake"
    )
    staged = workspace / "exports" / manifest.artifact_digest[:16] / package
    return staged, manifest, digest


def _forge_parent(root: Path) -> HarnessArtifact:
    """A licensed INSTALL Forge parent with one bound licensed child (test_subharness)."""
    from test_subharness import parent_artifact, sign

    parent, _ = parent_artifact(root)
    persistent = parent.spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
    licensed = sign(parent.model_copy(update={"spec": persistent, "report": None}))
    record = {"checks": 3, "dryruns": 0, "measures": 2, "submissions": 1, "spawns": 1,
              "agent_cost_usd": 0.25, "notes": [{"chars": 4, "sha256": "ab" * 32}]}
    (licensed.directory / "forge.json").write_text(json.dumps(record), encoding="utf-8")
    return licensed


# ------------------------------------------------------------------ export layout


def test_layout_manifest_and_content_free_evidence(
    installed: tuple[RegistryEntry, Registry, Path],
) -> None:
    entry, _, _ = installed
    staged, manifest, digest = _export(installed)
    files = {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()}
    assert files == {
        "__init__.py", "MANIFEST.json", "EVIDENCE.json", "EVIDENCE.md", "README.md",
        "schemas/input.schema.json", "schemas/output.schema.json",
        "artifact/harness.py", "artifact/spec.json", "artifact/config.json",
        "artifact/report.json", f"tests/test_{PACKAGE}.py", ".gitattributes",
    }
    # Git keeps every listed byte: no line-ending conversion in a consumer's checkout.
    assert (staged / ".gitattributes").read_bytes().splitlines()[-1] == b"* -text"
    # Byte-identical artifact; every file hashed; MANIFEST.json is canonical.
    for name in ("harness.py", "spec.json", "config.json", "report.json"):
        assert (staged / "artifact" / name).read_bytes() == (entry.directory / name).read_bytes()
    listed = dict(manifest.files)
    assert set(listed) == files - {"MANIFEST.json"}
    for name, value in listed.items():
        assert hashlib.sha256((staged / name).read_bytes()).hexdigest() == value
    raw = (staged / "MANIFEST.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    assert raw == json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    assert manifest.package == PACKAGE and manifest.children == {}
    evidence = json.loads((staged / "EVIDENCE.json").read_text("utf-8"))
    assert set(evidence) == EVIDENCE_KEYS
    assert evidence["evidence_label"] == "host_attested"
    assert evidence["attestation_note"] == ATTESTATION_NOTE == (
        "licence verifiable only on a host holding the Architect signing key; "
        "re-verify with `architect export verify`"
    )
    assert evidence["report_evidence"] == "synthetic_fake"
    assert evidence["model_class"] == "synthetic_fake" and evidence["forge"] is None
    # No floor case content anywhere in the package, and everything is ASCII text.
    for path in staged.rglob("*"):
        if path.is_file():
            text = path.read_bytes()
            assert FINGERPRINT.encode() not in text, path
            assert text.isascii(), path
    init = (staged / "__init__.py").read_text("utf-8")
    assert f'FLAG = "{flag_name(manifest.spec_hash)}"' in init
    assert read_pin(staged) == {
        "package_format": 1, "spec_hash": manifest.spec_hash,
        "artifact_digest": manifest.artifact_digest, "execution_mode": "native",
        "children": {},
    }
    readme = (staged / "README.md").read_text("utf-8")
    assert "architect>=" in readme and "-I -m architect.runtime._sandbox_worker" in readme
    assert "--approved-spec" in readme


def test_forge_parent_export_pins_children_and_forge_counts(tmp_path: Path) -> None:
    parent = _forge_parent(tmp_path / "source")
    registry = Registry(tmp_path / "registry")
    entry = registry.install("forge-task", parent)
    manifest, _ = export_package(entry, tmp_path / "exports", "forged")
    staged = tmp_path / "exports" / manifest.artifact_digest[:16] / "forged"
    assert parent.forge is not None
    ref = parent.forge.subharnesses["helper"]
    assert manifest.children == {"helper": (ref.artifact_digest, ref.spec_hash, "authored")}
    assert manifest.execution_mode == "sandbox"
    evidence = json.loads((staged / "EVIDENCE.json").read_text("utf-8"))
    assert evidence["forge"] == {"checks": 3, "dryruns": 0, "measures": 2, "submissions": 1,
                                 "spawns": 1, "agent_cost_usd": 0.25}
    assert evidence["children"] == [
        {"name": "helper", "artifact_digest": ref.artifact_digest, "case_origin": "authored"}
    ]
    assert (staged / "artifact" / "forge.json").is_file()
    assert (staged / "artifact" / "subharnesses" / "helper" / "harness.py").is_file()
    assert verify_export(staged, registry=registry).codes == ()
    # A tampered child is export_subharness_missing (and breaks integrity).
    child = staged / "artifact" / "subharnesses" / "helper" / "harness.py"
    child.write_bytes(child.read_bytes() + b"\n")
    check = verify_export(staged, registry=registry)
    assert not check.children and "export_subharness_missing" in check.codes


def test_expire_entries_and_bad_names_are_refused(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    from test_subharness import sign

    entry, _, workspace = installed
    for name in ("Bad", "1abc", "class", "a" * 41, "a-b", ""):
        with pytest.raises(ExportError, match="^export_package_invalid$"):
            export_package(entry, workspace / "exports", name)
    directory = tmp_path / "expire"
    directory.mkdir()
    (directory / "harness.py").write_text("async def run(c, t):\n    return 1\n", "utf-8")
    expiring = sign(HarnessArtifact(spec=spec(), preset="extractor", model="fake",
                                    components=(), directory=directory))
    fake_entry = RegistryEntry(name="x", revision=1, spec_hash=expiring.spec.spec_hash,
                               artifact_version=1, directory=directory)
    with pytest.raises(ExportError, match="^export_unverified$"):
        export_package(fake_entry, workspace / "exports", "expired")
    assert not (workspace / "exports" / expiring.artifact_digest[:16]).exists()


def _link(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return
    except OSError:
        pass
    if os.name == "nt" and target.is_dir():
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("this host cannot create links")


def test_a_link_anywhere_is_export_failed(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    entry, _, workspace = installed
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(outside, entry.directory / "subharnesses")
    with pytest.raises(ExportError, match="^export_failed$"):
        export_package(entry, workspace / "exports", PACKAGE)
    staging = workspace / "exports"
    assert not any(p.is_file() for p in staging.rglob("*")) if staging.exists() else True


# ------------------------------------------------------------------ verification and shim


def test_verify_export_with_and_without_the_host_key(
    installed: tuple[RegistryEntry, Registry, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, registry, _ = installed
    staged, manifest, _ = _export(installed)
    clean = verify_export(staged, registry=registry)
    assert clean.model_dump() == {"integrity": True, "pins": True, "children": True,
                                  "licensed": True, "registry_match": True, "codes": ()}
    assert verify_export(staged, registry=None).registry_match is None
    assert verify_export(staged, registry=Registry(tmp_path / "empty")).registry_match is False
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(tmp_path / "no-key"))
    keyless = verify_export(staged, registry=None)
    assert keyless.licensed is None and keyless.codes == ("license_unverifiable",)
    with pytest.raises(ExportError, match="^license_unverifiable$"):
        ExportedHarness(staged, read_pin(staged) or {}).load()


def test_tampering_is_detected_even_when_the_manifest_is_rewritten(
    installed: tuple[RegistryEntry, Registry, Path],
) -> None:
    staged, manifest, _ = _export(installed)
    harness = staged / "artifact" / "harness.py"
    harness.write_bytes(harness.read_bytes() + b"\n# tampered\n")
    check = verify_export(staged, registry=None)
    assert check.codes[0] == "export_integrity_mismatch" and not check.integrity
    # Rewrite the manifest consistently: the pins and the license still catch it.
    files = tuple(
        (name, hashlib.sha256((staged / name).read_bytes()).hexdigest())
        for name, _ in manifest.files
    )
    forged = manifest.model_copy(update={"files": files})
    (staged / "MANIFEST.json").write_bytes(
        json.dumps(forged.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    )
    check = verify_export(staged, registry=None)
    assert check.integrity and not check.pins and check.licensed is False
    assert check.codes == ("export_pin_mismatch", "export_unverified")
    # An extra file is an integrity failure too.
    (staged / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes


def test_the_operating_systems_own_files_never_break_a_package(
    installed: tuple[RegistryEntry, Registry, Path],
) -> None:
    """A Finder or Explorer visit (.DS_Store, ._ files, Thumbs.db, desktop.ini) leaves the
    package verifiable and loadable; a changed listed file is still refused."""
    staged, _, _ = _export(installed)
    for name in (".DS_Store", "._README.md", "Thumbs.db", "desktop.ini", "artifact/.DS_Store"):
        (staged / name).write_bytes(b"\x00\x00\x00\x01Bud1")
    clean = verify_export(staged, registry=None)
    assert clean.integrity and clean.codes == ()
    ExportedHarness(staged, read_pin(staged) or {}).load()
    assert changed_files(staged, read_manifest(staged)[0]) == ()
    harness = staged / "artifact" / "harness.py"
    harness.write_bytes(harness.read_bytes() + b"\n# tampered\n")
    check = verify_export(staged, registry=None)
    assert check.codes[0] == "export_integrity_mismatch" and not check.integrity
    assert changed_files(staged, read_manifest(staged)[0]) == ("artifact/harness.py",)


@pytest.mark.parametrize("name", ["match", "case", "type"])
def test_soft_keywords_are_valid_package_names(
    installed: tuple[RegistryEntry, Registry, Path], name: str
) -> None:
    # 12.3 fixes the rule: the regex and "not a Python keyword". Soft keywords are
    # ordinary, importable identifiers.
    staged, manifest, _ = _export(installed, name)
    assert manifest.package == name and staged.name == name
    assert verify_export(staged, registry=None).codes == ()


# ------------------------------------------------------------------ interpreter caches

_TAG = sys.implementation.cache_tag
_EVIL = "PWNED = True\ndef enabled():\n    return True\n"


def _pyc(source: str, target: Path, work: Path, mode: str, like: Path | None = None) -> None:
    """Compile ``source`` into ``target``; ``like`` forges a timestamp header for it."""
    import py_compile

    work.mkdir(parents=True, exist_ok=True)
    origin = work / f"src{len(list(work.iterdir()))}.py"
    origin.write_text(source, encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(
        str(origin), cfile=str(target), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode[mode],
    )
    if like is not None:
        stat = like.stat()
        data = bytearray(target.read_bytes())
        data[8:12] = (int(stat.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
        data[12:16] = (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little")
        target.write_bytes(bytes(data))


def _import_flag(package_dir: Path) -> str:
    """Import the package in a fresh interpreter (bytecode on) and report the flag."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    env.pop("ARCHITECT_FEATURE_" + (read_pin(package_dir) or {})["spec_hash"][:8].upper(), None)
    code = f"import {package_dir.name} as p; print(getattr(p, 'PWNED', False), p.enabled())"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=package_dir.parent, env=env, capture_output=True,
        text=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("mode", ["UNCHECKED_HASH", "CHECKED_HASH", "TIMESTAMP"])
def test_a_planted_bytecode_cache_is_an_integrity_mismatch(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path, mode: str
) -> None:
    staged, _, _ = _export(installed)
    init = staged / "__init__.py"
    planted = staged / "__pycache__" / f"__init__.{_TAG}.pyc"
    if mode == "CHECKED_HASH":
        # A checked-hash pyc that names the genuine source's hash but carries another body.
        import importlib.util

        _pyc(_EVIL, planted, tmp_path / "evil", mode)
        data = bytearray(planted.read_bytes())
        data[8:16] = importlib.util.source_hash(init.read_bytes())
        planted.write_bytes(bytes(data))
    else:
        _pyc(_EVIL, planted, tmp_path / "evil", mode, like=init if mode == "TIMESTAMP" else None)
    # The planted bytecode really is what the interpreter runs: the flag is on.
    assert _import_flag(staged) == "True True"
    check = verify_export(staged, registry=None)
    assert not check.integrity and check.codes[0] == "export_integrity_mismatch"
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        ExportedHarness(staged, read_pin(staged) or {}).load()


def _forged_pyc(source: bytes, target: Path, **fields: int) -> None:
    """An unchecked-hash pyc of the genuine body with frame-layout fields forged."""
    import importlib.util
    import marshal
    from types import CodeType

    def forge(code: CodeType) -> CodeType:
        consts = tuple(forge(c) if isinstance(c, CodeType) else c for c in code.co_consts)
        changed = {name: value(code) if callable(value) else value
                   for name, value in fields.items()}
        return code.replace(co_consts=consts, **changed)

    code = compile(source, str(target.parent.parent / "__init__.py"), "exec", dont_inherit=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = importlib.util.MAGIC_NUMBER + (1).to_bytes(4, "little")
    target.write_bytes(header + importlib.util.source_hash(source) + marshal.dumps(forge(code)))


@pytest.mark.parametrize(
    "fields",
    [
        {"co_stacksize": 0},
        {"co_stacksize": 1},
        {"co_flags": lambda code: code.co_flags ^ 0x20},  # CO_GENERATOR
    ],
    ids=["stacksize-0", "stacksize-1", "flags"],
)
def test_a_cache_with_the_genuine_body_but_a_forged_frame_layout_is_refused(
    installed: tuple[RegistryEntry, Registry, Path], fields: dict[str, Any]
) -> None:
    from architect.forge_modes.export import cache_file_ok

    staged, manifest, _ = _export(installed)
    planted = staged / "__pycache__" / f"__init__.{_TAG}.pyc"
    _forged_pyc((staged / "__init__.py").read_bytes(), planted, **fields)
    # Never imported here: a too-small frame corrupts memory in the interpreter.
    assert not cache_file_ok(staged, planted.relative_to(staged).as_posix(),
                             dict(manifest.files))
    check = verify_export(staged, registry=None)
    assert not check.integrity and check.codes[0] == "export_integrity_mismatch"
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        ExportedHarness(staged, read_pin(staged) or {}).load()
    # The unforged body in the same container is accepted: only the forgery is refused.
    _forged_pyc((staged / "__init__.py").read_bytes(), planted)
    assert verify_export(staged, registry=None).codes == ()


def test_genuine_bytecode_caches_keep_the_package_verifiable(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    import py_compile

    staged, _, _ = _export(installed)
    genuine = staged / "__pycache__" / f"__init__.{_TAG}.pyc"
    # A real import writes the interpreter's own cache next to __init__.py.
    assert _import_flag(staged) == "False False"
    assert genuine.is_file()
    for level in (1, 2):
        py_compile.compile(str(staged / "__init__.py"), doraise=True, optimize=level)
    py_compile.compile(
        str(staged / "tests" / f"test_{PACKAGE}.py"), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    assert verify_export(staged, registry=None).codes == ()
    # A cache this interpreter could load but that is not the compiled form of a listed
    # source (an unlisted source, an unknown optimisation level, a case variant that a
    # case-insensitive file system would open), or anything that is not a cache, is refused.
    original = genuine.read_bytes()
    for name in (f"ghost.{_TAG}.pyc", f"__init__.{_TAG}.opt-3.pyc", "__init__.pyc",
                 f"__init__.{_TAG}.pyc.1234", f"__init__.{_TAG}-PYTEST-{pytest.__version__}.pyc"):
        stray = staged / "__pycache__" / name
        stray.write_bytes(original)
        assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes, name
        stray.unlink()
    # A case variant of the interpreter's own cache name. It cannot sit next to the
    # genuine one on a case-insensitive file system, so the genuine one is moved away.
    genuine.unlink()
    for name in (f"__init__.{_TAG.upper()}.pyc", f"__init__.{_TAG}.PYC"):
        stray = staged / "__pycache__" / name
        stray.write_bytes(original)
        assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes, name
        stray.unlink()
    genuine.write_bytes(original)
    assert verify_export(staged, registry=None).codes == ()
    genuine.write_bytes(original[:16] + b"\x00garbage")
    assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes
    genuine.write_bytes(original)
    # pytest's cache holds data only; code there is refused.
    (staged / ".pytest_cache" / "v").mkdir(parents=True)
    (staged / ".pytest_cache" / "v" / "lastfailed").write_text("{}", encoding="utf-8")
    assert verify_export(staged, registry=None).codes == ()
    (staged / ".pytest_cache" / "sitecustomize.py").write_text("x = 1\n", encoding="utf-8")
    assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes


def test_caches_this_interpreter_and_its_pytest_never_load_are_not_tampering(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged, _, _ = _export(installed)
    harness = ExportedHarness(staged, read_pin(staged) or {})
    junk = b"not a code object"
    # Another interpreter's cache and a stale pytest's rewrite cache (left behind after a
    # pytest upgrade) are never loaded by this interpreter: the package stays usable.
    for name in ("__pycache__/__init__.cpython-00.pyc", "__pycache__/__init__.pypy39.pyc",
                 f"__pycache__/__init__.{_TAG}-pytest-0.0.0.pyc",
                 f"tests/__pycache__/test_{PACKAGE}.{_TAG}-pytest-0.0.0.pyc",
                 f"tests/__pycache__/test_{PACKAGE}.cpython-313-pytest-{pytest.__version__}.pyc"):
        stray = staged.joinpath(*name.split("/"))
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(junk)
        assert verify_export(staged, registry=None).codes == (), name
        assert harness.load().artifact_digest == read_pin(staged)["artifact_digest"]  # type: ignore[index]
        stray.unlink()
    # This pytest's rewrite cache would be loaded, so a bogus one is refused ...
    current = staged / "tests" / "__pycache__" / (
        f"test_{PACKAGE}.{_TAG}-pytest-{pytest.__version__}.pyc"
    )
    current.write_bytes(junk)
    assert "export_integrity_mismatch" in verify_export(staged, registry=None).codes
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        harness.load()
    # ... unless pytest is not installed in the interpreter that loads the package (an
    # application interpreter): then nothing there can ever load it.
    monkeypatch.setitem(sys.modules, "pytest", None)
    assert verify_export(staged, registry=None).codes == ()
    assert harness.load().artifact_digest == read_pin(staged)["artifact_digest"]  # type: ignore[index]
    monkeypatch.undo()
    # The interpreter's own caches are always checked.
    current.unlink()
    _pyc(_EVIL, staged / "__pycache__" / f"__init__.{_TAG}.pyc", tmp_path / "evil",
         "UNCHECKED_HASH")
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        harness.load()


async def test_shim_loads_runs_and_refuses(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    staged, manifest, _ = _export(installed)
    pin = read_pin(staged)
    assert pin is not None
    harness = ExportedHarness(staged, pin)
    artifact = harness.load()
    assert artifact.artifact_digest == manifest.artifact_digest
    runtime = Runtime(TelemetryStore(tmp_path / "telemetry"))
    try:
        assert await harness.run_with(runtime, FakeAdapter(model="fake"), {}) == "ok"
        # INSTALL: a run consumes nothing, so a second run works too.
        assert await harness.run_with(runtime, FakeAdapter(model="fake"), {}) == "ok"
        impostor = FakeAdapter(model="fake")
        impostor.model = "not-fake"
        with pytest.raises(UnlicensedArtifactError):
            await harness.run_with(runtime, impostor, {})
    finally:
        runtime.store.close()
    assert not (staged / "artifact" / ".consumed").exists()
    wrong = ExportedHarness(staged, {**pin, "artifact_digest": "0" * 64})
    with pytest.raises(ExportError, match="^export_pin_mismatch$"):
        wrong.load()
    # The pin is a private copy.
    pin["artifact_digest"] = "1" * 64
    assert harness.load().artifact_digest == manifest.artifact_digest


async def test_generated_init_is_off_by_default_and_runs_with_a_profile(
    installed: tuple[RegistryEntry, Registry, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    staged, manifest, _ = _export(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(staged, repo, "vendor")
    root = repo / "vendor" / PACKAGE
    loader = importlib.util.spec_from_file_location(
        PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert loader is not None and loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    monkeypatch.delenv(module.FLAG, raising=False)
    assert module.enabled() is False
    with pytest.raises(FeatureDisabled):
        await module.run({}, profile="p.json", workspace="w")
    monkeypatch.setenv(module.FLAG, "true")
    assert module.enabled() is False  # only exactly "1" enables it
    monkeypatch.setenv(module.FLAG, "1")
    profile = tmp_path / "profile.json"
    output = await module.run({}, profile=profile, workspace=tmp_path / "delivery")
    assert output == "ok"
    assert module.verify().codes == ()
    rows = TelemetryStore(tmp_path / "delivery" / "telemetry")
    try:
        assert len(rows.rows()) == 1
    finally:
        rows.close()


RELAY = "http://127.0.0.1:9/v1"  # a judge code's relay; nothing is called here
SUPER = "nvidia/nemotron-3-super-120b-a12b"


@pytest.mark.parametrize("chosen_by", ["ARCHITECT_WORKSPACE", "the argument", "the profile"])
async def test_a_profile_naming_the_shared_workspace_runs_where_architect_works(
    installed: tuple[RegistryEntry, Registry, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chosen_by: str,
) -> None:
    """The expense app's profile names ``~/.architect``. Its package run uses the workspace
    the CLI and the app use: the ``workspace`` argument, else ``ARCHITECT_WORKSPACE``, else
    the profile's own, and reads the judge code and relay address stored there."""
    import importlib.util

    from architect import profiles
    from architect.models import OpenAICompatibleAdapter
    from architect.providers import credentials

    staged, _, _ = _export(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(staged, repo, "vendor")
    root = repo / "vendor" / PACKAGE
    loader = importlib.util.spec_from_file_location(
        PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert loader is not None and loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    monkeypatch.setenv(module.FLAG, "1")

    home = tmp_path / "home"
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("ARCHITECT_WORKSPACE", raising=False)
    app = tmp_path / "app"
    app.mkdir()
    profile = app / "architect.json"
    entry = {"provider": "openai", "api_key_ref": "nebius", "temperature": 0.0,
             "max_tokens": 512}
    profile.write_text(json.dumps({"workspace": "~/.architect",
                                   "models": {"fake": {"provider": "fake"}, SUPER: entry}}),
                       encoding="utf-8")
    workspace = {"ARCHITECT_WORKSPACE": tmp_path / "ws", "the argument": tmp_path / "arg",
                 "the profile": home / ".architect"}[chosen_by]
    code = "rc_" + "c" * 32
    credentials.set_key("nebius", code, workspace)
    credentials.set_base_url("nebius", RELAY, workspace)
    if chosen_by != "the profile":
        # Another code in the workspace that does not count: the run must not read it.
        decoy = tmp_path / "ws" if chosen_by == "the argument" else home / ".architect"
        credentials.set_key("nebius", "rc_" + "d" * 32, decoy)
        credentials.set_base_url("nebius", "http://127.0.0.1:8/v1", decoy)
    if chosen_by == "ARCHITECT_WORKSPACE":
        monkeypatch.setenv("ARCHITECT_WORKSPACE", str(workspace))
    elif chosen_by == "the argument":
        monkeypatch.setenv("ARCHITECT_WORKSPACE", str(tmp_path / "ws"))
    built: list[profiles.Services] = []
    real = profiles.build_services

    def spy(profile: profiles.Profile, where: Path) -> profiles.Services:
        services = real(profile, where)
        built.append(services)
        return services

    monkeypatch.setattr(profiles, "build_services", spy)
    extra = {"workspace": workspace} if chosen_by == "the argument" else {}
    assert await module.run({}, profile=profile, **extra) == "ok"
    services = built[0]
    assert services.workspace == workspace.resolve()
    adapter = services.adapter_factory(SUPER)
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter._base_url == RELAY and adapter._api_key == code
    rows = TelemetryStore(workspace / "telemetry")
    try:
        assert len(rows.rows()) == 1
    finally:
        rows.close()


# ------------------------------------------------------------------ generated tests


def _pytest(
    package_dir: Path, tmp_path: Path, key: Path, *, bytecode: bool = False,
    ini_text: str = "[pytest]\n",
) -> subprocess.CompletedProcess[str]:
    ini = tmp_path / "isolated.ini"
    ini.write_text(ini_text, encoding="utf-8")
    env = {**os.environ, "ARCHITECT_SIGNING_KEY_FILE": str(key), "PYTHONDONTWRITEBYTECODE": "1"}
    if bytecode:
        env.pop("PYTHONDONTWRITEBYTECODE")
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(package_dir / "tests"), "-q", "-p",
         "no:cacheprovider", "-c", str(ini), "--basetemp", str(tmp_path / "gen-tmp"),
         "--color=no", "-rfs"],
        cwd=package_dir.parent, env=env, capture_output=True, text=True, timeout=300,
        check=False,
    )


def test_generated_tests_pass_with_the_key_skip_without_and_fail_when_tampered(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    staged, _, _ = _export(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(staged, repo, "vendor")
    package_dir = repo / "vendor" / PACKAGE
    key = Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"])
    assert key.is_file()
    with_key = _pytest(package_dir, tmp_path, key)
    assert with_key.returncode == 0, with_key.stdout + with_key.stderr
    assert "7 passed" in with_key.stdout
    keyless = _pytest(package_dir, tmp_path, tmp_path / "absent.key")
    assert keyless.returncode == 0, keyless.stdout + keyless.stderr
    assert "4 passed, 3 skipped" in keyless.stdout
    [skipped] = [line for line in keyless.stdout.splitlines() if line.startswith("SKIPPED")]
    assert "[3]" in skipped and skipped.endswith("license_unverifiable_without_host_key")
    # Running the generated tests leaves the package verifiable, also when the
    # interpreter and pytest write their bytecode caches into it.
    assert verify_export(package_dir, registry=None).codes == ()
    cached = _pytest(package_dir, tmp_path, key, bytecode=True)
    assert cached.returncode == 0, cached.stdout + cached.stderr
    assert "7 passed" in cached.stdout
    caches = {p.name for p in package_dir.rglob("*.pyc")}
    assert f"__init__.{_TAG}.pyc" in caches
    assert any("-pytest-" in name for name in caches), caches
    assert verify_export(package_dir, registry=None).codes == ()
    # A planted bytecode cache fails the package's own tests (and turns the flag on).
    planted = package_dir / "__pycache__" / f"__init__.{_TAG}.pyc"
    _pyc(_EVIL, planted, tmp_path / "evil", "UNCHECKED_HASH")
    poisoned = _pytest(package_dir, tmp_path, key, bytecode=True)
    assert poisoned.returncode != 0
    summary = poisoned.stdout.split("short test summary")[-1]
    assert "::test_manifest_integrity" in summary, poisoned.stdout
    assert "export_integrity_mismatch" in verify_export(package_dir, registry=None).codes
    planted.unlink()
    assert verify_export(package_dir, registry=None).codes == ()
    # A tampered exported package fails its own verification tests.
    harness = package_dir / "artifact" / "harness.py"
    harness.write_bytes(harness.read_bytes() + b"\n# tampered\n")
    tampered = _pytest(package_dir, tmp_path, key)
    assert tampered.returncode != 0
    summary = tampered.stdout.split("short test summary")[-1]
    for name in ("test_manifest_integrity", "test_pins", "test_license_on_signing_host"):
        assert f"::{name}" in summary, tampered.stdout
    assert "::test_flag_default_off" not in summary


# ------------------------------------------------------------------ placement


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_package_only_placement_adds_exactly_one_new_directory(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    staged, manifest, _ = _export(installed)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
    before = _files(repo)
    placed = place_package(staged, repo, "src")
    assert placed == manifest
    assert tree_digest(repo / "src" / PACKAGE) == tree_digest(staged)
    after = _files(repo)
    prefix = f"src/{PACKAGE}/"
    # Every pre-existing file is byte-identical; every new file is in the new directory.
    assert {k: v for k, v in after.items() if not k.startswith(prefix)} == before
    assert all(k.startswith(prefix) for k in set(after) - set(before))
    assert {p.name for p in (repo / "src").iterdir()} == {"app.py", PACKAGE}
    with pytest.raises(ExportError, match="^export_target_exists$"):
        place_package(staged, repo, "src")
    assert read_manifest(repo / "src" / PACKAGE)[0] == manifest


@pytest.mark.parametrize(
    "into", ["../escape", "a/../../escape", ".architect", "x/.ARCHITECT/y", "", "/abs", "C:/abs",
             "a:b", "bad\nname"]
)
def test_invalid_destinations_are_export_path_invalid(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path, into: str
) -> None:
    staged, _, _ = _export(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    before = tree_digest(tmp_path / "repo")
    with pytest.raises(ExportError, match="^export_path_invalid$"):
        place_package(staged, repo, into)
    assert tree_digest(tmp_path / "repo") == before
    assert not (tmp_path / "escape").exists()


def test_denied_and_linked_destinations_are_refused(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    staged, _, _ = _export(installed)
    repo = tmp_path / "repo"
    (repo / "private").mkdir(parents=True)
    with pytest.raises(ExportError, match="^export_path_invalid$"):
        place_package(staged, repo, "private", denied=(repo / "private",))
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(outside, repo / "linked")
    with pytest.raises(ExportError, match="^export_path_invalid$"):
        place_package(staged, repo, "linked")
    assert list(outside.iterdir()) == []


def test_a_tampered_staging_copy_is_never_placed(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    staged, _, _ = _export(installed)
    (staged / "README.md").write_text("changed\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        place_package(staged, repo, "vendor")
    assert not (repo / "vendor").exists()


class _HookOn:
    """A stand-in pytest Config with the assertion pass hook enabled."""

    def getini(self, name: str) -> bool:
        assert name == "enable_assertion_pass_hook"
        return True


def _rewritten(source: bytes, path: Path, config: object) -> Any:
    """pytest's ``_rewrite_test`` of ``source`` under ``config``."""
    import ast

    from _pytest.assertion.rewrite import rewrite_asserts

    tree = ast.parse(source, filename=str(path))
    rewrite_asserts(tree, source, str(path), config)  # type: ignore[arg-type]
    return compile(tree, str(path), "exec", dont_inherit=True)


def test_a_pytest_rewrite_cache_written_with_the_pass_hook_is_not_tampering(
    installed: tuple[RegistryEntry, Registry, Path], tmp_path: Path
) -> None:
    """A host ini with ``enable_assertion_pass_hook = true`` changes the code pytest
    writes into its genuine rewrite cache; that cache must not read as tampering."""
    import importlib.util
    import marshal
    from types import CodeType

    from architect.forge_modes.export import _same_code, cache_file_ok

    staged, manifest, _ = _export(installed)
    repo = tmp_path / "repo"
    repo.mkdir()
    place_package(staged, repo, "vendor")
    package_dir = repo / "vendor" / PACKAGE
    key = Path(os.environ["ARCHITECT_SIGNING_KEY_FILE"])
    assert verify_export(package_dir, registry=None).codes == ()
    hooked = "[pytest]\nenable_assertion_pass_hook = true\n"
    first = _pytest(package_dir, tmp_path, key, bytecode=True, ini_text=hooked)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "7 passed" in first.stdout
    source_path = package_dir / "tests" / f"test_{PACKAGE}.py"
    relative = f"tests/__pycache__/test_{PACKAGE}.{_TAG}-pytest-{pytest.__version__}.pyc"
    cache = package_dir.joinpath(*relative.split("/"))
    assert cache.is_file()
    # The genuine cache really differs from the default (no pass hook) rewrite ...
    source = source_path.read_bytes()
    written = marshal.loads(cache.read_bytes()[16:])
    assert isinstance(written, CodeType)
    assert not _same_code(written, _rewritten(source, source_path, None))
    assert _same_code(written, _rewritten(source, source_path, _HookOn()))
    # ... and is still accepted: by the check, verify_export, the shim and the tests.
    listed = dict(manifest.files)
    assert cache_file_ok(package_dir, relative, listed)
    assert verify_export(package_dir, registry=None).codes == ()
    pin = read_pin(package_dir)
    assert pin is not None
    assert ExportedHarness(package_dir, pin).load().artifact_digest == manifest.artifact_digest
    again = _pytest(package_dir, tmp_path, key, bytecode=True, ini_text=hooked)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "7 passed" in again.stdout
    plain = _pytest(package_dir, tmp_path, key, bytecode=True)
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "7 passed" in plain.stdout
    assert verify_export(package_dir, registry=None).codes == ()
    # A pass-hook rewrite of another body under the genuine name is still refused.
    evil = b"PWNED = True\n\n\ndef test_evil():\n    assert PWNED\n"
    body = _rewritten(evil, source_path, _HookOn())
    header = importlib.util.MAGIC_NUMBER + (1).to_bytes(4, "little")
    cache.write_bytes(header + importlib.util.source_hash(source) + marshal.dumps(body))
    assert not cache_file_ok(package_dir, relative, listed)
    assert "export_integrity_mismatch" in verify_export(package_dir, registry=None).codes
    with pytest.raises(ExportError, match="^export_integrity_mismatch$"):
        ExportedHarness(package_dir, pin).load()
    poisoned = _pytest(package_dir, tmp_path, key, ini_text=hooked)
    assert poisoned.returncode != 0
    assert "::test_manifest_integrity" in poisoned.stdout.split("short test summary")[-1]
