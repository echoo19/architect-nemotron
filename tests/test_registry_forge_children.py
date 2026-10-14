"""Phase 10 Section 4: an installed Forge artifact keeps, and re-authenticates, its children.

Before this fix ``Registry.install`` copied only the four license files, so an
installed Forge parent passed authentication but failed its first child call
with ``subharness_digest_mismatch``. Every run here uses a fake adapter; sockets
to anything but loopback are refused.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
from test_subharness import Priced, parent_artifact, sign

from architect import registry as registry_module
from architect.registry import Registry, RegistryError
from architect.runtime import Runtime
from architect.runtime.subharness import _load as load_child
from architect.spec import HarnessArtifact, Lifecycle
from architect.telemetry import TelemetryStore

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every non-loopback connection (the event loop's self-pipe stays usable)."""

    def guarded(original: Any) -> Any:
        def connect(self: socket.socket, address: Any, *args: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise AssertionError("network access in a hermetic test")
            return original(self, address, *args)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))


RECORD = {"schema_version": 1, "note": "content-free fixture"}


def forge_parent(root: Path) -> HarnessArtifact:
    """A licensed INSTALL Forge parent whose sandbox code calls one licensed child."""
    parent, _ = parent_artifact(root)
    (parent.directory / "harness.py").write_text(
        "from pydantic import JsonValue\n"
        "\n"
        "from architect.runtime import RunContext\n"
        "\n"
        "\n"
        "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
        "    first = await context.harness('helper', {'text': task})\n"
        "    return first['answer'] + '!'\n",
        encoding="utf-8",
    )
    persistent = parent.spec.model_copy(update={"lifecycle": Lifecycle.INSTALL})
    licensed = sign(
        parent.model_copy(
            update={"spec": persistent, "components": ("extraction",), "report": None}
        )
    )
    (licensed.directory / "forge.json").write_text(json.dumps(RECORD), encoding="utf-8")
    return licensed


def published(registry: Registry) -> list[Path]:
    versions = registry.root / "versions"
    return sorted(versions.rglob("harness.py")) if versions.exists() else []


async def test_installed_forge_parent_keeps_children_and_runs(tmp_path: Path) -> None:
    parent = forge_parent(tmp_path / "source")
    registry = Registry(tmp_path / "registry")
    entry = registry.install("forge-task", parent)

    installed = registry.get("forge-task")
    assert installed.directory == entry.directory != parent.directory
    assert installed.licensed and installed.artifact_digest == parent.artifact_digest
    assert installed.forge == parent.forge
    # The child is present at the path play time reloads it from, and still binds.
    assert parent.forge is not None
    child = load_child(installed, "helper")
    assert child.directory == entry.directory / "subharnesses" / "helper"
    assert child.licensed
    assert child.artifact_digest == parent.forge.subharnesses["helper"].artifact_digest
    # forge.json travels with the artifact, byte for byte.
    assert (entry.directory / "forge.json").read_bytes() == (
        parent.directory / "forge.json"
    ).read_bytes()

    # The installed copy is isolated: breaking the source child changes nothing.
    source_child = parent.directory / "subharnesses" / "helper" / "harness.py"
    source_child.write_text("broken", encoding="utf-8")

    runtime = Runtime(TelemetryStore(tmp_path / "runs"))
    adapter = Priced(0.01)
    result = await runtime.run(installed, "hi", adapter)
    assert result.output == "done!"
    assert adapter.prompts == ["child: hi"]
    row = runtime.store.rows()[-1]
    assert row.model_calls == 1 and row.cost_usd == pytest.approx(0.01)


def test_tampered_child_is_refused_before_publication(tmp_path: Path) -> None:
    parent = forge_parent(tmp_path / "source")
    child_code = parent.directory / "subharnesses" / "helper" / "harness.py"
    child_code.write_bytes(child_code.read_bytes() + b"\n")
    # The parent's own license is untouched; only the bound child changed.
    assert HarnessArtifact.load(parent.directory).licensed
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="^registry_subharness_missing$"):
        registry.install("forge-task", parent)
    assert registry.list() == ()
    assert published(registry) == []
    assert not any(path.name.startswith(".install-") for path in registry.root.iterdir())


def test_missing_child_is_refused(tmp_path: Path) -> None:
    parent = forge_parent(tmp_path / "source")
    for path in sorted((parent.directory / "subharnesses").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="^registry_subharness_missing$"):
        registry.install("forge-task", parent)
    assert registry.list() == () and published(registry) == []


def test_linked_child_tree_is_refused(tmp_path: Path) -> None:
    parent = forge_parent(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, parent.directory / "subharnesses" / "extra",
                   target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable to this user")
    registry = Registry(tmp_path / "registry")
    with pytest.raises(RegistryError, match="^registry_subharness_missing$"):
        registry.install("forge-task", parent)
    assert registry.list() == () and published(registry) == []


def test_deepest_child_directory_is_path_checked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = forge_parent(tmp_path / "source")
    registry = Registry(tmp_path / "registry")
    checked: list[Path] = []
    original = registry_module._entry_fits

    def spy(directory: Path) -> None:
        checked.append(directory)
        if directory.name == "helper":
            raise RegistryError("workspace_path_too_long")
        original(directory)

    monkeypatch.setattr(registry_module, "_entry_fits", spy)
    with pytest.raises(RegistryError, match="^workspace_path_too_long$"):
        registry.install("forge-task", parent)
    assert any(path.parts[-2:] == ("subharnesses", "helper") for path in checked)
    assert registry.list() == () and published(registry) == []


def test_plain_artifact_install_is_unchanged(tmp_path: Path) -> None:
    parent = forge_parent(tmp_path / "source")
    child = HarnessArtifact.load(parent.directory / "subharnesses" / "helper")
    assert child.forge is None
    registry = Registry(tmp_path / "registry")
    entry = registry.install("plain-task", child)
    assert sorted(path.name for path in entry.directory.iterdir()) == [
        "config.json", "harness.py", "report.json", "spec.json",
    ]
    assert registry.get("plain-task").licensed
