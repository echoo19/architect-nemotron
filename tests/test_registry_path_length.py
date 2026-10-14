"""Registry roots too deep for stock Windows fail before any Stage 4 dollars are spent.

Entry directories nest about 120 characters below the root. Without the
LongPathsEnabled policy, Windows refuses directories above 247 characters, so a
long workspace made every approved install fail as an opaque storage_error after
compilation. Construction probes a two-digit revision and version; install checks
the exact directory and names the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from architect import registry as registry_module
from architect.registry import Registry, RegistryError

PROBE_BELOW_ROOT = len(str(Path("r") / registry_module._PROBE_ENTRY)) - 1
LIMIT = registry_module._MAX_ENTRY_DIRECTORY - PROBE_BELOW_ROOT


def _root(tmp_path: Path, length: int) -> Path:
    padding = length - len(str(tmp_path)) - 1
    assert padding > 0, "temporary directory already exceeds the tested length"
    return tmp_path / ("w" * padding)


def test_probe_entry_matches_layout_with_two_digit_revision_and_version() -> None:
    parts = registry_module._PROBE_ENTRY.parts
    assert parts == ("versions", "0" * 32, "99", "artifacts", "0" * 64, "99")
    assert LIMIT == 123


def test_long_root_without_long_path_policy_fails_before_storage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_long_paths_enabled", lambda: False)
    root = _root(tmp_path, LIMIT + 1)
    with pytest.raises(RegistryError, match="workspace_path_too_long"):
        Registry(root)
    assert not root.exists()


def test_root_at_limit_constructs_without_policy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_long_paths_enabled", lambda: False)
    root = _root(tmp_path, LIMIT)
    assert Registry(root).root == root.resolve()


def test_long_root_with_long_path_policy_constructs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_long_paths_enabled", lambda: True)
    root = _root(tmp_path, LIMIT + 1)
    assert Registry(root).root == root.resolve()


def test_short_root_never_consults_policy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> bool:
        raise AssertionError("policy consulted for a short root")

    monkeypatch.setattr(registry_module, "_long_paths_enabled", forbidden)
    # Pin the length: POSIX temp roots (e.g. macOS /private/var/folders/...) are
    # long enough that an arbitrary child of tmp_path can exceed the limit.
    assert Registry(_root(tmp_path, LIMIT)).root.exists()


def test_install_directory_is_checked_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_long_paths_enabled", lambda: False)
    limit = registry_module._MAX_ENTRY_DIRECTORY
    registry_module._entry_fits(Path("C:/" + "x" * (limit - 3)))
    with pytest.raises(RegistryError, match="workspace_path_too_long"):
        registry_module._entry_fits(Path("C:/" + "x" * (limit - 2)))
    monkeypatch.setattr(registry_module, "_long_paths_enabled", lambda: True)
    registry_module._entry_fits(Path("C:/" + "x" * 400))


def test_policy_is_irrelevant_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "_PLATFORM", "linux")
    assert registry_module._long_paths_enabled() is True


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False), (2, False)])
def test_windows_policy_value_is_read_from_the_machine_key(
        monkeypatch: pytest.MonkeyPatch, value: int, expected: bool) -> None:
    winreg = pytest.importorskip("winreg")
    monkeypatch.setattr(registry_module, "_PLATFORM", "win32")
    seen: list[str] = []

    def query(key: object, name: str) -> tuple[int, int]:
        seen.append(name)
        return value, winreg.REG_DWORD

    monkeypatch.setattr(winreg, "QueryValueEx", query)
    assert registry_module._long_paths_enabled() is expected
    assert seen == ["LongPathsEnabled"]


def test_unreadable_windows_policy_counts_as_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    winreg = pytest.importorskip("winreg")
    monkeypatch.setattr(registry_module, "_PLATFORM", "win32")

    def broken(key: object, name: str) -> tuple[int, int]:
        raise OSError("policy unreadable")

    monkeypatch.setattr(winreg, "QueryValueEx", broken)
    assert registry_module._long_paths_enabled() is False


def test_real_policy_detection_is_boolean() -> None:
    assert isinstance(registry_module._long_paths_enabled(), bool)
