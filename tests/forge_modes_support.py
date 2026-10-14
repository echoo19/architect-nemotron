"""Offline fixtures for the Phase 10 Forge delivery modes (WP3).

Everything runs on the fake provider: no live provider, no ``claude``/``codex`` CLI,
no Ollama and no GPU. Forge runs in ``template`` mode (no Pi, no agent model), where a
compile must still forge. Floor cases carry fingerprint strings so tests can prove
that no case content ever reaches an export or a record.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from architect.compiler.types import ForgeSettings
from architect.profiles import Services, build_services, load_profile
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

FINGERPRINT = "HELDOUT_FINGERPRINT_"
TEMPLATE = ForgeSettings(mode="template")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
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


def case_rows(count: int = 8, expected: Any = "ok", prefix: str = "c") -> list[dict[str, Any]]:
    return [
        {
            "case_id": f"{prefix}{index:02d}",
            "task": {"text": f"{FINGERPRINT}{prefix}{index:03d}"},
            "expected": expected,
        }
        for index in range(count)
    ]


def make_profile(
    tmp_path: Path,
    *,
    expected: Any = "ok",
    smoke: bool = True,
    reply: str | None = None,
    **extra: Any,
) -> Path:
    """A fake-provider profile; ``reply`` scripts every play call (default: ``ok``)."""
    rows = case_rows(8, expected)
    (tmp_path / "cases.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "smoke.json").write_text(json.dumps(rows[:4]), encoding="utf-8")
    floor: dict[str, Any] = {"kind": "held_out_tasks", "path": "cases.json"}
    if smoke:
        floor["smoke"] = "smoke.json"
    model: dict[str, Any] = {"provider": "fake"}
    if reply is not None:
        response = {"text": reply, "model": "fake", "tokens_in": 10, "tokens_out": 10,
                    "cost_usd": 0.0001}
        (tmp_path / "replies.json").write_text(json.dumps([response] * 4096), encoding="utf-8")
        model["fixture"] = "replies.json"
    payload: dict[str, Any] = {
        "workspace": "work",
        "models": {"fake": model},
        "floors": {"tasks": floor},
        "compile": {"max_candidates": 1, "enable_rewriting": False},
    }
    payload.update(extra)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def forge_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Template mode submits the Phase 9 ``REPAIR`` harness (three calls, then ``ok``).

    With play replies of ``nope`` Stage 2 fails, so the Forge submission is accepted
    and promoted (``forge.json`` written), exactly as ``tests/test_forge.py`` does it.
    """
    from forge_support import REPAIR

    import architect.compiler.forge as forge_module

    monkeypatch.setattr(forge_module, "instantiate", lambda name, candidate: REPAIR)


def services_for(profile: Path) -> Services:
    return build_services(load_profile(profile), Path("work"))


def spec(**changes: Any) -> Spec:
    values: dict[str, Any] = {
        "task": "Return ok",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "output_schema": {"type": "string"},
        "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        "budget": Budget(),
        "lifecycle": Lifecycle.EXPIRE,
    }
    values.update(changes)
    return Spec(**values)


class Consent:
    """Records every consent text and answers from a script (default: yes)."""

    def __init__(self, *answers: bool, default: bool = True) -> None:
        self.answers = list(answers)
        self.default = default
        self.texts: list[str] = []

    def __call__(self, text: str) -> bool:
        assert text.isascii()
        self.texts.append(text)
        return self.answers.pop(0) if self.answers else self.default


def logical_lines(text: str) -> list[str]:
    """Stderr lines with the 100-column wrap undone (a continuation starts with two
    spaces); every physical line must fit 100 ASCII columns."""
    lines: list[str] = []
    for line in text.strip().splitlines():
        assert len(line) <= 100 and line.isascii(), line
        if line.startswith("  ") and lines:
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    return lines


def tree_digest(root: Path) -> str:
    """A content hash of every file and directory under ``root`` (order independent)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(b"D" + relative.encode() + b"\0")
        else:
            digest.update(b"F" + relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def artifact_dirs(root: Path) -> list[Path]:
    return sorted(path.parent for path in root.rglob("report.json")) if root.exists() else []
