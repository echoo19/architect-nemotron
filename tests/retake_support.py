"""Offline fixtures for the retake tests: one built agent, added to a project.

Everything runs on the fake provider. The profile holds two models: ``fake`` answers
``ok`` and so gets every case right, the second (``MISS``) answers ``nope`` and gets none. The
floor has 8 cases (4 practice, 4 exam) and names 6 extra exam cases for retakes, so one
retake has unused cases and the next is the same exam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_modes_support import case_rows, spec

from architect.forge_modes import export_package, place_package
from architect.profiles import Services, build_services, load_profile
from architect.session.retake import remember_passed
from architect.spec import Lifecycle

PACKAGE = "ok_feature"
MISS = "gpt-4o-mini"  # a priced name; the fake provider answers for it


def write_profile(
    root: Path, *, retake_cases: int = 6, floor: dict[str, Any] | None = None
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases.json").write_text(json.dumps(case_rows(8)), encoding="utf-8")
    entry: dict[str, Any] = {"kind": "held_out_tasks", "path": "cases.json", "retakes": True}
    if retake_cases:
        rows = case_rows(retake_cases, prefix="r")
        (root / "retake.json").write_text(json.dumps(rows), encoding="utf-8")
        entry["retake_cases"] = "retake.json"
    entry.update(floor or {})
    if entry["retakes"] is None:  # a test of an exam whose entry does not say
        del entry["retakes"]
    miss = {"text": "nope", "model": MISS, "tokens_in": 10, "tokens_out": 10,
            "cost_usd": 0.0001}
    (root / "miss.json").write_text(json.dumps([miss] * 512), encoding="utf-8")
    payload = {
        "workspace": "work",
        "models": {"fake": {"provider": "fake"},
                   MISS: {"provider": "fake", "fixture": "miss.json"}},
        "floors": {"tasks": entry},
        "compile": {"max_candidates": 1, "enable_rewriting": False,
                    "model_ladder": ["fake"]},
    }
    path = root / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def record_rows(workspace: Path) -> list[dict[str, Any]]:
    """The workspace's one record of attempts, a row per attempt, oldest first. The file
    is append-only: a later line for the same attempt adds to the earlier one."""
    files = sorted((workspace / "retakes").glob("*.jsonl"))
    assert len(files) <= 1, files
    merged: dict[int, dict[str, Any]] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            merged.setdefault(row["attempt"], {}).update(row)
    return [merged[number] for number in sorted(merged)]


@dataclass
class Placed:
    profile: Path
    workspace: Path
    repo: Path
    folder: Path  # <repo>/agents/ok_feature
    staged: Path

    def services(self) -> Services:
        return build_services(load_profile(self.profile), Path("work"))

    def read(self, name: str) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(
            (self.folder / "artifact" / name).read_text(encoding="utf-8")
        )
        return loaded

    def edit(self, name: str, **changes: Any) -> None:
        """Change keys of ``artifact/<name>`` in the placed folder, as an editor would."""
        path = self.folder / "artifact" / name
        value = self.read(name)
        value.update(changes)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def use_model(self, model: str) -> None:
        self.edit("config.json", model=model)

    def reword(self, text: str = "Return ok, and nothing else") -> None:
        self.edit("spec.json", task=text)


async def place(
    root: Path, *, model_target: str = "cheapest that passes", **profile: Any
) -> Placed:
    """Build the agent, install it, export it and add it to ``<root>/repo/agents``."""
    path = write_profile(root, **profile)
    services = build_services(load_profile(path), Path("work"))
    try:
        compiled = await services.compiler.compile(
            spec(lifecycle=Lifecycle.INSTALL, model_target=model_target), approved=True
        )
        assert compiled.artifact is not None and compiled.artifact.licensed
        services.registry.install("feature-ok", compiled.artifact)
        entry = services.registry.status("feature-ok")
        workspace = services.workspace
        manifest, _ = export_package(
            entry, workspace / "exports", PACKAGE, model_class="synthetic_fake"
        )
    finally:
        services.close()
    staged = workspace / "exports" / manifest.artifact_digest[:16] / PACKAGE
    repo = root / "repo"
    repo.mkdir()
    place_package(staged, repo, "agents")
    folder = repo / "agents" / PACKAGE
    remember_passed(workspace, folder, staged)
    return Placed(path, workspace, repo, folder, staged)
