"""Package boundaries, checked on the source tree with ``ast``.

Phase 9 Section 3 adds the Forge boundaries: the Forge modules and the play-time
sub-harness path never reach the verifier capability, Stage 4 or a
``FloorEvaluator`` private; only the Forge driver opens a build context; only the
Forge tool module makes ``forge_*`` trusted tools; the bench stays a leaf.
"""

import ast
import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "architect"
FORGE_TOOLS = ("forge_check", "forge_dryrun", "forge_measure", "forge_submit", "forge_spawn")


def test_business_examples_do_not_leak_into_core() -> None:
    package = Path(__file__).resolve().parents[1] / "architect"
    forbidden = ("lectern", "tutoring", "academy", "report card")
    for path in package.rglob("*"):
        if path.suffix in {".py", ".json", ".ts", ".md"}:
            text = path.read_text(encoding="utf-8").lower()
            assert all(word not in text for word in forbidden), path


# ------------------------------------------------------------------ helpers


def _sources() -> Iterator[Path]:
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" not in path.parts and "node_modules" not in path.parts:
            yield path


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Every absolute module name the file imports, with relative imports resolved.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c`` (``c`` may be a module).
    """
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _under(name: str, module: str) -> bool:
    return name == module or name.startswith(module + ".")


def _forge_files() -> list[Path]:
    compiler = PACKAGE / "compiler"
    files = sorted(compiler.glob("forge*.py"))
    files += sorted((compiler / "forge_templates").rglob("*.py"))
    files.append(PACKAGE / "runtime" / "subharness.py")
    return [path for path in files if "__pycache__" not in path.parts]


def _floor_private_names() -> set[str]:
    """Single-underscore members of ``FloorEvaluator``, its subclasses and its module."""
    import architect.floors as floors
    from architect.floors.base import FloorEvaluator

    for info in pkgutil.iter_modules(floors.__path__):
        importlib.import_module(f"architect.floors.{info.name}")
    names: set[str] = set()
    pending: list[type] = [FloorEvaluator]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        names.update(
            name
            for name in vars(cls)
            if name.startswith("_") and not name.startswith("__") and name != "_abc_impl"
        )
    base = inspect.getmodule(FloorEvaluator)
    assert base is not None
    names.update(name for name in vars(base) if name.startswith("_") and not name.startswith("__"))
    return names


# ------------------------------------------------------------------ Phase 9 Section 3


def test_forge_boundary_covers_the_forge_modules() -> None:
    names = {_rel(path) for path in _forge_files()}
    for expected in (
        "architect/compiler/forge.py",
        "architect/compiler/forge_lint.py",
        "architect/compiler/forge_settings.py",
        "architect/compiler/forge_tools.py",
        "architect/compiler/forge_workspace.py",
        "architect/compiler/forge_templates/__init__.py",
        "architect/runtime/subharness.py",
    ):
        assert expected in names


def test_forge_and_subharness_never_import_the_verifier_or_stage4() -> None:
    for path in _forge_files():
        for name in _imports(path):
            assert not _under(name, "architect.floors._access"), (_rel(path), name)
            assert not _under(name, "architect.compiler.verification"), (_rel(path), name)
            # No private name of any floors module (``_PARTITIONS``, ``_fingerprint``...).
            if _under(name, "architect.floors"):
                assert not name.rsplit(".", 1)[-1].startswith("_"), (_rel(path), name)


def test_forge_and_subharness_never_touch_floor_evaluator_privates() -> None:
    private = _floor_private_names()
    assert "_verification_cases" in private  # the check is not vacuous
    for path in _forge_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute):
                assert node.attr not in private, (_rel(path), node.lineno, node.attr)
                # ``held_out`` stays reachable only from compiler/verification.py.
                assert node.attr != "held_out", (_rel(path), node.lineno)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # getattr(floor, "_verification_cases") and friends.
                assert node.value not in private, (_rel(path), node.lineno, node.value)
                assert node.value != "held_out", (_rel(path), node.lineno)


def _runtime_build_sites(path: Path) -> list[int]:
    """Lines reaching ``Runtime.build``: ``<..runtime..>.build`` or ``getattr(x, "build")``.

    ``preset.build(...)`` (spec validation of a preset) is a different method and is
    not counted; any getattr of ``"build"`` is counted, whatever the receiver.
    """
    lines: list[int] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Attribute) and node.attr == "build":
            if "runtime" in ast.unparse(node.value).lower():
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "build"
        ):
            lines.append(node.lineno)
    return lines


def test_only_the_forge_driver_calls_runtime_build() -> None:
    sites = {_rel(path): _runtime_build_sites(path) for path in _sources()}
    callers = {name for name, lines in sites.items() if lines}
    assert callers == {"architect/compiler/forge.py"}, sites


def _trusted_tool_names(path: Path) -> list[tuple[int, object]]:
    """``(line, name)`` for each ``TrustedTool(...)`` call; name is None unless a literal."""
    found: list[tuple[int, object]] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called != "TrustedTool":
            continue
        name = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"),
            node.args[0] if node.args else None,
        )
        found.append((node.lineno, name.value if isinstance(name, ast.Constant) else None))
    return found


def test_only_forge_tools_constructs_forge_trusted_tools() -> None:
    owner = "architect/compiler/forge_tools.py"
    owned: set[object] = set()
    for path in _sources():
        for line, name in _trusted_tool_names(path):
            if _rel(path) == owner:
                owned.add(name)
                continue
            # Elsewhere every trusted tool has a literal, non-Forge name.
            assert isinstance(name, str), (_rel(path), line)
            assert not name.startswith("forge_"), (_rel(path), line, name)
    assert owned == set(FORGE_TOOLS)


def test_bench_is_imported_only_by_bench_cli_and_session_bench() -> None:
    allowed = {"architect/cli.py", "architect/session/bench.py"}
    importers: set[str] = set()
    for path in _sources():
        name = _rel(path)
        if name.startswith("architect/bench/"):
            continue
        if any(_under(module, "architect.bench") for module in _imports(path)):
            importers.add(name)
    assert importers == allowed, importers


# ------------------------------------------------------------------ Phase 10 Section 17.6

ROUTING = PACKAGE / "routing"
FORGE_MODES = PACKAGE / "forge_modes"
# Section 3: architect/routing/** imports only these architect packages (plus the
# standard library and pydantic); RoutedLibrary satisfies LibraryLookup structurally.
ROUTING_ALLOWED = ("spec", "registry", "library", "telemetry", "models", "presets", "routing")
ROUTING_FORBIDDEN = (
    "bench", "compiler", "profiles", "orchestrator", "session", "server", "cli", "backends",
    "forge_modes", "games",
)


def _package_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts]


def test_routing_boundary_covers_the_routing_package() -> None:
    names = {_rel(path) for path in _package_files(ROUTING)}
    for expected in ("labels", "signature", "log", "evidence", "router", "proposals", "roles"):
        assert f"architect/routing/{expected}.py" in names


def test_routing_imports_only_its_contract_packages() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _package_files(ROUTING):
        for name in _imports(path):
            if not _under(name, "architect"):
                if name.split(".")[0] not in {"pydantic", "__future__"} and not _stdlib(name):
                    offenders.append((_rel(path), name))
                continue
            if name == "architect":
                continue
            top = name.split(".")[1]
            if top in ROUTING_FORBIDDEN or top not in ROUTING_ALLOWED:
                offenders.append((_rel(path), name))
    assert offenders == []


def _stdlib(name: str) -> bool:
    import sys

    return name.split(".")[0] in sys.stdlib_module_names


def test_routing_is_never_imported_by_the_bench_in_reverse_and_bench_stays_a_leaf() -> None:
    # Nothing in routing reaches the bench, and the only bench importers stay the CLI
    # and the session's bench screen (the Phase 8 rule, re-checked with routing present).
    for path in _package_files(ROUTING):
        assert not any(_under(name, "architect.bench") for name in _imports(path)), path
    test_bench_is_imported_only_by_bench_cli_and_session_bench()


def _calls(path: Path, function: str) -> bool:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == function:
                return True
    return False


def _defines(path: Path, function: str) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        for node in ast.walk(_tree(path))
    )


def test_only_the_export_modules_write_export_trees() -> None:
    """Section 17.6: export trees are written only by forge_modes/export.py (staging and
    host placement) and forge_modes/place_tool.py (the single-use Pi placement)."""
    writers = ("export_package", "place_package", "_stage")
    for function in writers:
        definers = {_rel(path) for path in _sources() if _defines(path, function)}
        assert definers <= {"architect/forge_modes/export.py", "architect/runtime/pipeline.py"}
        assert "architect/forge_modes/export.py" in definers
    placers = {_rel(path) for path in _sources() if _calls(path, "place_package")}
    # integrate.py's --package-only host path calls export.py's place_package (12.2);
    # the Phase 13 CLI's "into software" export (agent/export.py) places after approval.
    assert placers <= {
        "architect/agent/export.py",
        "architect/forge_modes/export.py",
        "architect/forge_modes/place_tool.py",
        "architect/forge_modes/integrate.py",
    }, placers
    stagers = {_rel(path) for path in _sources() if _calls(path, "export_package")}
    assert stagers <= {
        "architect/agent/export.py",
        "architect/forge_modes/export.py",
        "architect/forge_modes/integrate.py",
        "architect/session/delivery.py",
    }, stagers
    manifests = {
        _rel(path) for path in _sources() if "MANIFEST.json" in path.read_text(encoding="utf-8")
    }
    # integrate.py only reads the staged manifest's size; export.py alone writes it.
    assert manifests <= {"architect/forge_modes/export.py", "architect/forge_modes/integrate.py"}


def test_forge_modes_never_reach_the_verifier_or_bench() -> None:
    for path in _package_files(FORGE_MODES):
        for name in _imports(path):
            assert not _under(name, "architect.floors._access"), (_rel(path), name)
            assert not _under(name, "architect.compiler.verification"), (_rel(path), name)
            assert not _under(name, "architect.bench"), (_rel(path), name)
