"""The example expense app with a real Architect agent in agents/receipt_reader.

examples/expense-app's own tests use a fake package, since that project does not install
Architect. Here the app's code runs against a package Architect really built, exported
and placed (on the fake provider, offline): the app reads a receipt through it, its
terminal names the exam, and after a change to the agent's files the app's reply and
terminal carry the stop sentence, with no model call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from forge_modes_support import block_sockets

from architect.forge_modes import export_package, place_package
from architect.forge_modes.export import read_flag
from architect.profiles import build_services, load_profile
from architect.session.retake import remember_passed, retake, undo
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples" / "expense-app"
ANSWER = {"total": 33000, "subtotal": 30000, "tax": 3000, "item_count": 2}
RECEIPT = "KEDAI PAGI\n1 Kopi Susu 18,000\n1 Roti Bakar 12,000\nSUBTOTAL 30,000\nTOTAL 33,000"
STOPPED = "Stopped: changed after its exam. 0 model calls."
OTHER = "gpt-4o-mini"  # a second model of the profile; the fake provider answers for it


def _reply(model: str) -> dict[str, Any]:
    return {"text": json.dumps(ANSWER), "model": model, "tokens_in": 10, "tokens_out": 10,
            "cost_usd": 0.0001}


def _profile(root: Path) -> Path:
    """The app's architect.json for this test: two fake models that read every receipt
    as ``ANSWER``, and an exam of 8 cases with 6 more for retakes."""
    def cases(prefix: str, count: int) -> list[dict[str, Any]]:
        return [
            {"case_id": f"{prefix}{index:02d}", "task": {"receipt": f"receipt {prefix}{index}"},
             "expected": ANSWER}
            for index in range(count)
        ]

    (root / "cases.json").write_text(json.dumps(cases("c", 8)), encoding="utf-8")
    (root / "retake.json").write_text(json.dumps(cases("r", 6)), encoding="utf-8")
    for name in ("fake", OTHER):
        (root / f"{name}.json").write_text(json.dumps([_reply(name)] * 512), encoding="utf-8")
    payload = {
        "workspace": "work",
        "models": {name: {"provider": "fake", "fixture": f"{name}.json"}
                   for name in ("fake", OTHER)},
        "floors": {"receipts": {"kind": "held_out_tasks", "path": "cases.json",
                                "retake_cases": "retake.json", "retakes": True}},
        "compile": {"max_candidates": 1, "enable_rewriting": False, "model_ladder": ["fake"]},
    }
    path = root / "architect.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _spec() -> Spec:
    amount = {"type": ["integer", "null"]}
    return Spec(
        task="Return the receipt's total, subtotal, tax and item count as JSON.",
        input_schema={"type": "object", "properties": {"receipt": {"type": "string"}},
                      "required": ["receipt"]},
        output_schema={
            "type": "object",
            "properties": {"total": amount, "subtotal": amount, "tax": amount,
                           "item_count": {"type": "integer"}},
            "required": ["total", "subtotal", "tax", "item_count"],
        },
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.5, dataset="receipts"),
        budget=Budget(),
        model_target="cheapest that passes",
        lifecycle=Lifecycle.INSTALL,
        preset_hint="extractor",
    )


class Project:
    """A copy of the example app with the receipt agent added to it."""

    def __init__(self, root: Path, workspace: Path, folder: Path) -> None:
        self.root, self.workspace, self.folder = root, workspace, folder
        self.profile = root / "architect.json"

    def set_model(self, model: str) -> None:
        path = self.folder / "artifact" / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["model"] = model
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


async def _add_agent(root: Path) -> Project:
    shutil.copytree(APP / "app", root / "app")
    profile = _profile(root)
    services = build_services(load_profile(profile), Path("work"))
    try:
        compiled = await services.compiler.compile(_spec(), approved=True)
        assert compiled.artifact is not None and compiled.artifact.licensed
        services.registry.install("receipt-reader", compiled.artifact)
        entry = services.registry.status("receipt-reader")
        workspace = services.workspace
        manifest, _ = export_package(
            entry, workspace / "exports", "receipt_reader", model_class="synthetic_fake"
        )
    finally:
        services.close()
    staged = workspace / "exports" / manifest.artifact_digest[:16] / "receipt_reader"
    place_package(staged, root, "agents")
    folder = root / "agents" / "receipt_reader"
    remember_passed(workspace, folder, staged)
    return Project(root, workspace, folder)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Project]:
    block_sockets(monkeypatch)
    added = asyncio.run(_add_agent(tmp_path / "expense-app"))
    flag = read_flag(added.folder)
    assert flag is not None
    monkeypatch.setenv(flag, "1")  # the app's environment switches the agent on
    monkeypatch.syspath_prepend(str(added.root))
    before = set(sys.modules)
    yield added
    for name in set(sys.modules) - before:
        if name.split(".")[0] in ("app", "agents"):
            del sys.modules[name]


@pytest.fixture
def terminal() -> Iterator[list[str]]:
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = Keep()
    logger = logging.getLogger("expenses")
    logger.addHandler(handler)
    yield lines
    logger.removeHandler(handler)


def _client(project: Project) -> TestClient:
    from app.main import app, get_store  # the copy's, found on the path the fixture set
    from app.store import Store

    store = Store(":memory:")
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app)


def test_the_app_reads_with_the_agent_stops_when_it_changes_and_works_again(
    project: Project, terminal: list[str]
) -> None:
    import app.extract as extract_module

    assert extract_module.EXTRACTOR == "receipt-agent" and extract_module.agent_added()
    with _client(project) as client:
        # Working: the terminal names the exam once, and a receipt is read by the agent.
        assert terminal == ["Receipt agent: passed its exam. 4 of 4, pass mark 2, attempt 1."]
        response = client.post("/receipts", json={"text": RECEIPT, "date": "2026-10-12"})
        assert response.status_code == 201
        assert {key: response.json()[key] for key in ANSWER} == ANSWER

        # Someone swaps the model in the agent's folder. The next call is refused before
        # any model is called, and the reply and the terminal say why.
        del terminal[:]
        project.set_model(OTHER)
        response = client.post("/receipts", json={"text": RECEIPT})
        assert response.status_code == 502
        changed = f"Changed: artifact/config.json (model: fake to {OTHER})."
        step = "Retake its exam or undo the change in Architect."
        assert response.json()["detail"] == (
            f"Could not read the receipt. {STOPPED} {changed} {step}"
        )
        assert terminal == [STOPPED, f"{changed} {step}"]
        assert len(client.get("/receipts").json()) == 1
        assert extract_module.agent_line() == f"Receipt agent: {STOPPED} {changed} {step}"

        # Undo: the version that passed is back and the app works at once, no restart.
        undo(project.folder, project.workspace)
        assert client.post("/receipts", json={"text": RECEIPT}).status_code == 201

        # Change it again and retake: it passes, the folder is written again, and the
        # same flag still switches it on.
        project.set_model(OTHER)
        assert client.post("/receipts", json={"text": RECEIPT}).status_code == 502
        services = build_services(load_profile(project.profile), Path("work"))
        try:
            outcome = asyncio.run(retake(project.folder, services, lambda _text: True))
        finally:
            services.close()
        assert outcome.passed and outcome.attempt == 2
        assert client.post("/receipts", json={"text": RECEIPT}).status_code == 201
        assert extract_module.agent_line() == (
            "Receipt agent: passed its exam. 4 of 4, pass mark 2, attempt 2."
        )


def test_a_change_to_a_fixed_field_tells_the_app_to_undo_and_says_why(
    project: Project, terminal: list[str]
) -> None:
    """The trial: a changed budget sits in a file a retake reads, so the app said "Retake
    its exam or undo", and the retake then refused. The app now asks Architect first."""
    path = project.folder / "artifact" / "spec.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["budget"] = {"cost_usd": 0.005, "compute_s": 10.0}
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    with _client(project) as client:
        response = client.post("/receipts", json={"text": RECEIPT})
    assert response.status_code == 502
    assert response.json()["detail"] == (
        f"Could not read the receipt. {STOPPED} Changed: artifact/spec.json. The change "
        "touches the budget. Undo the change in Architect."
    )
    assert "Retake" not in " ".join(terminal)


def test_a_setting_out_of_range_tells_the_app_what_to_fix(project: Project) -> None:
    path = project.folder / "artifact" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["params"] = {**config["params"], "retries": 2}
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    with _client(project) as client:
        response = client.post("/receipts", json={"text": RECEIPT})
    assert response.json()["detail"] == (
        f"Could not read the receipt. {STOPPED} Changed: artifact/config.json. "
        "artifact/config.json: retries is 2. It can be 0 or 1. Fix it or undo."
    )


def test_an_agent_folder_that_lost_a_file_stops_and_is_not_swapped_for_the_general_model(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.extract as extract_module

    def general(_text: str) -> None:
        raise AssertionError("the general model was asked")

    monkeypatch.setattr(extract_module, "ask_general_model", general)
    (project.folder / "__init__.py").unlink()
    with _client(project) as client:
        response = client.post("/receipts", json={"text": RECEIPT})
    assert response.status_code == 502
    assert response.json()["detail"] == (
        f"Could not read the receipt. {STOPPED} Changed: __init__.py. A change to this "
        "file can only be undone. Undo the change in Architect."
    )
    undo(project.folder, project.workspace)
    with _client(project) as client:
        assert client.post("/receipts", json={"text": RECEIPT}).status_code == 201


def test_a_change_that_cannot_be_retaken_tells_the_app_to_undo(
    project: Project, terminal: list[str]
) -> None:
    harness = project.folder / "artifact" / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    with _client(project) as client:
        response = client.post("/receipts", json={"text": RECEIPT})
    assert response.status_code == 502
    assert response.json()["detail"] == (
        f"Could not read the receipt. {STOPPED} Changed: artifact/harness.py. A change to "
        "this file can only be undone. Undo the change in Architect."
    )
    assert terminal[0].startswith(f"Receipt agent: {STOPPED}")
