"""Phase 13 Package E: Forge mode's two destinations, /export, /serve and /forge (offline).

The fake provider answers the interview proposal and Stage 0 from the profile's inference
fixture; the play model always answers "ok". The repository is a temp directory next to
(never inside) the workspace. /serve runs a stand-in listener script, never the real
service on a live provider, and nothing here talks to a model server or a CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from session_support import fake_profile, make_spec
from test_agent_build import INTERVIEW, Harness, spec_json
from test_agent_conductor import Scripted

from architect import server
from architect.agent import RecordingUI, commands_forge
from architect.agent.app import AppOptions, boot
from architect.agent.build import DESTINATION_QUESTION, PATH_QUESTION, is_arc, make, set_override
from architect.agent.commands import COMMANDS
from architect.agent.events import Notice, PlanCard
from architect.agent.export import (
    Destination,
    destination,
    example_input,
    export_agent,
    package_name,
)
from architect.agent.repl import LineFrontend, Repl
from architect.agent.tools import ToolError
from architect.forge_modes import verify_export
from architect.session import ScriptedConsole
from architect.session.bootstrap import SESSION_PROFILE

DESCRIPTION = "make an agent that extracts invoice fields from text"
PACKAGE = "extracts_invoice_fields"


class RepoHarness(Harness):
    """``test_agent_build.Harness`` with the working directory a separate repository."""

    def __init__(self, tmp_path: Path, *args: Any, **kwargs: Any) -> None:
        super().__init__(tmp_path, *args, **kwargs)
        self.repo = tmp_path / "repo"
        self.repo.mkdir(exist_ok=True)
        self.ctx.cwd = self.repo


@pytest.fixture
def repo_harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    made: list[RepoHarness] = []

    def build(*args: Any, **kwargs: Any) -> RepoHarness:
        item = RepoHarness(tmp_path, *args, **kwargs)
        made.append(item)
        return item

    yield build
    for item in made:
        item.close()


def cards(ui: RecordingUI, prefix: str) -> list[PlanCard]:
    return [e for e in ui.events if isinstance(e, PlanCard) and e.title.startswith(prefix)]


def tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def software_interview() -> str:
    proposal = json.loads(INTERVIEW)
    proposal["location"] = {"value": "into software", "certain": True}
    return json.dumps(proposal)


# -- the interview: the destination comes first and lands on the plan card ---------------


async def test_destination_is_always_the_first_question_with_run_here_as_default(
    repo_harness,  # type: ignore[no-untyped-def]
) -> None:
    h = repo_harness([INTERVIEW, spec_json()], ["", "", "", "no"])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["cancelled"] is True
    assert h.ui.questions[0] == DESTINATION_QUESTION
    assert "[1] Run it here" in DESTINATION_QUESTION and "[2] Put it into software" in (
        DESTINATION_QUESTION
    )
    rows = dict(cards(h.ui, "Plan")[-1].rows)
    assert rows["destination"] == (
        "run it here: /run extracts-invoice-fields, `architect run`, HTTP through /serve"
    )
    assert rows["install as"] == "extracts-invoice-fields (installed agent)"


async def test_choosing_software_asks_for_the_package_directory(repo_harness) -> None:  # type: ignore[no-untyped-def]
    h = repo_harness([INTERVIEW, spec_json()], ["2", "src/ai/invoices", "", "", "no"])
    await make(DESCRIPTION, h.ctx)
    assert h.ui.questions[:2] == [DESTINATION_QUESTION, PATH_QUESTION]
    rows = dict(cards(h.ui, "Plan")[-1].rows)
    assert rows["destination"].startswith("into software: src/ai/invoices/ in this repository")
    assert "asked before anything is written" in rows["destination"]
    # A path outside the repository is refused with a warning; the default is kept.
    h2 = repo_harness([INTERVIEW, spec_json()], ["2", "../elsewhere", "", "", "no"])
    await make(DESCRIPTION, h2.ctx)
    assert any(isinstance(e, Notice) and "path_outside_cwd" in e.text for e in h2.ui.events)
    rows = dict(cards(h2.ui, "Plan")[-1].rows)
    assert rows["destination"].startswith(f"into software: agents/{PACKAGE}/")


async def test_a_request_that_says_software_defaults_to_software(repo_harness) -> None:  # type: ignore[no-untyped-def]
    h = repo_harness([software_interview(), spec_json()], ["", "", "", "", "no"])
    await make(DESCRIPTION, h.ctx)
    assert h.ui.questions[:2] == [DESTINATION_QUESTION, PATH_QUESTION]
    rows = dict(cards(h.ui, "Plan")[-1].rows)
    assert rows["destination"].startswith(f"into software: agents/{PACKAGE}/")


async def test_skip_at_the_destination_accepts_every_default(repo_harness) -> None:  # type: ignore[no-untyped-def]
    h = repo_harness([INTERVIEW, spec_json()], ["/skip", "no"])
    await make(DESCRIPTION, h.ctx)
    assert h.ui.questions == [DESTINATION_QUESTION, "Compile this agent?"]


async def test_local_models_show_zero_dollars_and_time_as_the_binding_budget(
    repo_harness, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        raise AssertionError("no model server may be contacted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    h = repo_harness([INTERVIEW, spec_json()], ["", "", "", "no"])
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    payload["models"]["llama3.2"] = {"provider": "ollama", "timeout_s": 90}
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()
    set_override(h.ctx, "model", "llama3.2")
    await make(DESCRIPTION, h.ctx)
    rows = dict(cards(h.ui, "Plan")[-1].rows)
    assert rows["budget"].startswith("$0 per run (local model); time is the budget: ")
    assert rows["model"] == "llama3.2 (local, $0)"


# -- into software: stage, ask, write, verify, call ---------------------------------------


async def test_software_build_exports_verifies_and_the_python_snippet_runs(
    repo_harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    h = repo_harness([software_interview(), spec_json()], ["", "", "", "", ""])
    h.ui.approvals = ["yes"]
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["destination"] == "into software"
    export = result["export"]
    assert isinstance(export, dict) and export["exported"] is True, export
    placed = h.repo / "agents" / PACKAGE
    assert export["path"] == f"agents/{PACKAGE}" and export["absolute"] == str(placed)
    assert export["codes"] == [] and export["human_review_required"] is True
    # The files were shown before the approval, and only then written.
    plan = cards(h.ui, "Export plan")[-1]
    shown = dict(plan.rows)
    assert "__init__.py" in shown["files"] and "MANIFEST.json" in shown["files"]
    assert "Nothing has been written" in plan.footer
    events = h.ui.events
    assert events.index(plan) < events.index(cards(h.ui, "Exported")[-1])
    check = verify_export(placed, registry=h.ctx.services.registry)
    assert check.integrity and check.pins and check.children and check.licensed is True
    assert check.registry_match is True
    assert tree(h.repo)[0] == "agents" and all(
        name.startswith(f"agents/{PACKAGE}") or name == "agents" for name in tree(h.repo)
    )
    # Snippets: Python (ExportedHarness through the package), HTTP (the real routes), tests.
    python = str(export["python"])
    assert f"from agents.{PACKAGE} import FLAG, enabled, run, verify" in python
    http = str(export["http"])
    assert "python -m architect.server" in http and "http://127.0.0.1:8765/compile" in http
    assert "http://127.0.0.1:8765/run" in http and '"human_reviewed": true' in http
    assert f"agents/{PACKAGE}/artifact/spec.json" in http
    assert export["test"] == f"python -m pytest agents/{PACKAGE}/tests -p no:cacheprovider"
    # The result card: design, floor, cost, destination and the next step.
    result_rows = dict(cards(h.ui, "Result")[-1].rows)
    assert {"floor", "cost", "destination", "next"} <= set(result_rows)
    assert result_rows["destination"] == f"into software: agents/{PACKAGE}/"
    assert str(export["feature_flag"]) in result_rows["next"]

    # The Python snippet, as printed, imports and calls the exported harness offline.
    monkeypatch.syspath_prepend(str(h.repo))
    flag = str(export["feature_flag"])
    monkeypatch.delenv(flag, raising=False)
    with pytest.raises(SystemExit, match="is off"):
        await asyncio.to_thread(exec, compile(python, "snippet", "exec"), {})
    monkeypatch.setenv(flag, "1")
    printed: list[object] = []
    namespace: dict[str, Any] = {"print": printed.append}
    try:
        await asyncio.to_thread(exec, compile(python, "snippet", "exec"), namespace)
    finally:
        for module in [name for name in sys.modules if name.split(".")[0] == "agents"]:
            del sys.modules[module]
    assert printed == ["ok"]

    # The generated tests pass with the host key (the printed test command's suite).
    ini = tmp_path / "isolated.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "pytest", str(placed / "tests"), "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "gen"), "--color=no", "-c", str(ini)],
        cwd=h.repo, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" in done.stdout


async def test_the_http_snippet_routes_serve_the_exported_agent(repo_harness) -> None:  # type: ignore[no-untyped-def]
    """Steps 2 and 3 of the HTTP snippet, against the real service app (in-process)."""
    h = repo_harness([software_interview(), spec_json()], ["", "", "", "", ""])
    h.ui.approvals = ["yes"]
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict)
    export = result["export"]
    assert isinstance(export, dict) and export["exported"] is True
    spec = json.loads((h.repo / str(export["path"]) / "artifact" / "spec.json").read_text("utf-8"))
    token = "t" * 40
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(server.create_app(h.ctx.services, api_token=token)) as client:
        compiled = client.post("/compile", json={"spec": spec, "approved": True}, headers=auth)
        assert compiled.status_code == 200, compiled.text
        body = compiled.json()
        assert body["accepted"] is True and body["artifact_id"], body
        ran = client.post(
            "/run",
            json={"artifact_id": body["artifact_id"], "input": {"text": "..."},
                  "enabled": True, "human_reviewed": True},
            headers=auth,
        )
        assert ran.status_code == 200, ran.text
        assert ran.json()["output"] == "ok"


async def install_agent(services: Any, name: str = "invoice") -> None:
    compiled = await services.compiler.compile(make_spec(lifecycle="install"), approved=True)
    assert compiled.artifact is not None and compiled.artifact.licensed
    services.registry.install(name, compiled.artifact)


async def test_a_refused_export_leaves_the_repository_untouched(repo_harness) -> None:  # type: ignore[no-untyped-def]
    h = repo_harness([INTERVIEW])
    await install_agent(h.ctx.services)
    (h.repo / "README.md").write_text("mine\n", encoding="utf-8")
    before = tree(h.repo)
    h.ui.approvals = ["no"]
    result = await export_agent("invoice", h.ctx)
    assert result["exported"] is False and result["cancelled"] is True
    assert result["codes"] == ["export_cancelled"]
    assert tree(h.repo) == before
    assert (h.repo / "README.md").read_text(encoding="utf-8") == "mine\n"
    # A destination that exists is never overwritten, and nothing is asked.
    (h.repo / "agents" / "invoice").mkdir(parents=True)
    h.ui.approvals = ["yes"]
    with pytest.raises(ToolError) as caught:
        await export_agent("invoice", h.ctx)
    assert caught.value.code == "export_target_exists"
    assert tree(h.repo) == sorted([*before, "agents", "agents/invoice"])
    # Outside the working directory, or a control-plane path: refused before staging.
    for path, code in (("../outside/pkg", "path_outside_cwd"),
                       (".architect/pkg", "export_path_invalid")):
        with pytest.raises(ToolError) as caught:
            await export_agent("invoice", h.ctx, path)
        assert caught.value.code == code
    with pytest.raises(ToolError) as caught:
        await export_agent("missing", h.ctx)
    assert caught.value.code == "unknown_agent"


async def test_an_export_keeps_the_version_undo_puts_back(repo_harness) -> None:  # type: ignore[no-untyped-def]
    """Add to project keeps a copy of what it placed and notes where, so a later change
    can be undone to the version that passed (``architect.session.retake``)."""
    from architect.session.retake import status, undo

    h = repo_harness([INTERVIEW])
    await install_agent(h.ctx.services)
    h.ui.approvals = ["yes"]
    result = await export_agent("invoice", h.ctx)
    assert result["exported"] is True and result["codes"] == []
    workspace = h.ctx.services.workspace
    placed = h.repo / "agents" / "invoice"
    kept = workspace / "passed" / "invoice"

    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }

    assert files(kept) == files(placed)
    placements = json.loads((workspace / "placements.json").read_text(encoding="utf-8"))
    assert Path(placements["invoice"]["path"]) == placed.resolve()
    assert status(placed, workspace).state == "passed"
    before = files(placed)
    (placed / "artifact" / "config.json").write_text("{}", encoding="utf-8")
    state = status(placed, workspace)
    assert state.state == "stopped" and state.can_undo
    assert state.lines[0] == "Stopped: changed after its exam. 0 model calls."
    undo(placed, workspace)
    assert files(placed) == before


def test_destinations_and_package_names(tmp_path: Path) -> None:
    assert package_name("extracts-invoice-fields") == "extracts_invoice_fields"
    assert package_name("3d-render") == "agent_3d_render"
    assert package_name("class") == "class_agent"
    assert package_name("!!!") == "agent"
    assert destination(tmp_path, None, "a-b") == Destination("agents", "a_b")
    assert destination(tmp_path, "src/ai/invoices", "x") == Destination("src/ai", "invoices")
    assert destination(tmp_path, "lib/", "a-b") == Destination("lib", "a_b")
    (tmp_path / "existing").mkdir()
    assert destination(tmp_path, "existing", "a-b") == Destination("existing", "a_b")
    assert destination(tmp_path, str(tmp_path / "pkg_dir"), "x") == Destination(".", "pkg_dir")
    assert Destination(".", "pkg").relative == "pkg"
    assert Destination("my-lib", "pkg").module is None
    assert Destination("src/ai", "pkg").module == "src.ai.pkg"
    for path, code in (("../x", "path_outside_cwd"), ("bad-name", "export_package_invalid")):
        with pytest.raises(ToolError) as caught:
            destination(tmp_path, path, "x")
        assert caught.value.code == code
    schema = {"type": "object", "properties": {"text": {"type": "string"},
                                               "n": {"type": "integer"}}, "required": ["text"]}
    assert example_input(schema) == {"text": "..."}
    assert example_input(schema, {"text": "Invoice 7"}) == {"text": "Invoice 7"}
    assert example_input(schema, {"nope": 1}) == {"text": "..."}


# -- the slash commands -------------------------------------------------------------------


async def start_repl(
    tmp_path: Path, lines: list[str], *replies: str
) -> tuple[Repl, ScriptedConsole]:
    if not (tmp_path / "w" / SESSION_PROFILE).exists():
        fake_profile(tmp_path)
    console = ScriptedConsole(lines)
    frontend = LineFrontend(console)
    app = await boot(
        AppOptions(cwd=tmp_path, workspace=tmp_path / "w"), frontend, adapter=Scripted(*replies)
    )
    return Repl(app, frontend), console


async def drive(repl: Repl, console: ScriptedConsole) -> str:
    try:
        assert await repl.run() == 0
    finally:
        repl.app.close()
    return console.transcript()


async def test_export_command_exports_an_installed_agent(tmp_path: Path) -> None:
    repl, console = await start_repl(
        tmp_path,
        ["/export invoice", "y", "/export invoice", "/export invoice ../x", "/export nope",
         "/export", "/help export"],
    )
    await install_agent(repl.app.services)
    out = await drive(repl, console)
    assert "== Export plan: agents/invoice/ ==" in out
    assert "Allow export invoice (" in out and "into agents/invoice/" in out
    assert (
        "Exported invoice to agents/invoice/ (files, pins, sub-harnesses and the Stage 4 "
        "license: ok)" in out
    )
    assert "== Exported: agents/invoice/ ==" in out and "from agents.invoice import" in out
    assert "Not exported (export_target_exists: agents/invoice)." in out
    assert "Not exported (path_outside_cwd: ../x)." in out
    assert "No installed agent nope" in out
    if commands_forge._PREVIOUS_EXPORT is None:
        assert "Usage: /export <agent> [path]" in out
    assert (tmp_path / "agents" / "invoice" / "MANIFEST.json").is_file()


async def test_forge_forms(tmp_path: Path) -> None:
    repl, console = await start_repl(
        tmp_path,
        ["/forge", "/forge arc --model", "/forge an agent that tags emails",
         "/forge the arc of a story, summarized", "/forge build a harness for ARC-AGI-3",
         "/forge arc --run", "n"],
    )
    seen: list[tuple[str, object, object]] = []

    async def turn(text: str, *, mode: object = None) -> None:
        seen.append((text, mode, repl.conductor.extras.get("force_forge")))

    repl.turn = turn  # type: ignore[method-assign]
    out = await drive(repl, console)
    assert "Usage: /forge <what the agent should do>, or /forge arc [--run] [--model M]" in out
    assert "Usage: /forge arc --run --model <profile model>" in out
    assert seen == [
        ("an agent that tags emails", "build", True),
        ("the arc of a story, summarized", "build", True),
    ]
    assert "force_forge" not in repl.conductor.extras
    assert '"reason": "arc_run_not_allowed"' in out  # the ARC form explains, runs nothing
    assert "Build and play an ARC harness" in out and "Not run." in out
    assert "allow_arc_run" not in repl.conductor.extras
    assert COMMANDS["/forge"].handler is commands_forge._forge
    assert is_arc("ARC-AGI-3") and not commands_forge._arc_form("the arc of a story", False)


async def test_forced_forge_overrides_the_preset_engineer(repo_harness) -> None:  # type: ignore[no-untyped-def]
    h = repo_harness([INTERVIEW, spec_json()], ["", "", "", "no"], engineer="preset")
    h.ctx.extras["force_forge"] = True
    await make(DESCRIPTION, h.ctx)
    assert dict(cards(h.ui, "Plan")[-1].rows)["engineer"].startswith("Forge (")


# -- /serve -------------------------------------------------------------------------------

LISTENER = """
import os, socket, sys, time
port = int(sys.argv[1])
open(sys.argv[2], "w").write(str(len(os.environ.get("ARCHITECT_API_TOKEN", ""))))
server = socket.socket()
server.bind(("127.0.0.1", port))
server.listen(5)
server.settimeout(0.2)
while True:
    try:
        connection, _ = server.accept()
        connection.close()
    except socket.timeout:
        pass
"""
FAILING = """
import json, sys
print(json.dumps({"error": "missing_api_token"}), file=sys.stderr)
sys.exit(1)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_default_server_command_is_the_real_service_on_loopback() -> None:
    assert commands_forge.server_command(Path("p.json"), 9000) == [
        sys.executable, "-m", "architect.server", "--profile", "p.json", "--host",
        "127.0.0.1", "--port", "9000",
    ]


async def test_serve_starts_shows_and_stops_a_managed_background_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port()
    marker = tmp_path / "token-length.txt"
    monkeypatch.delenv("ARCHITECT_API_TOKEN", raising=False)
    monkeypatch.setattr(
        commands_forge, "server_command",
        lambda profile, chosen: [sys.executable, "-c", LISTENER, str(chosen), str(marker)],
    )
    repl, console = await start_repl(
        tmp_path, [f"/serve {port}", "/serve", f"/serve {port + 1}", "/serve stop", "/serve stop"]
    )
    started: list[commands_forge.Served] = []
    original = commands_forge._start

    async def spy(repl_: Repl, chosen: int) -> None:
        await original(repl_, chosen)
        served = commands_forge.running_server(repl_)
        if served is not None:
            started.append(served)

    monkeypatch.setattr(commands_forge, "_start", spy)
    try:
        out = await drive(repl, console)
    finally:
        commands_forge.stop_servers()
    assert f"Starting the HTTP service on http://127.0.0.1:{port} ..." in out
    assert out.count("HTTP service\n") >= 2  # the start and the status
    assert f"http://127.0.0.1:{port}  (running, pid" in out
    assert "POST /compile, POST /run, GET /status/{artifact_id}" in out
    assert f"Already serving on port {port}; /serve stop first." in out
    assert f"Stopped the HTTP service on port {port}." in out
    assert "The HTTP service is not running." in out
    assert len(started) == 1 and started[0].process.poll() is not None
    assert int(marker.read_text(encoding="utf-8")) >= 32  # a fresh token reached the service
    assert started[0].token in out and "serve" not in repl.conductor.extras


async def test_serve_reports_the_services_own_failure_code_and_a_busy_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands_forge, "server_command",
                        lambda profile, port: [sys.executable, "-c", FAILING])
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        repl, console = await start_repl(tmp_path, [f"/serve {free_port()}", f"/serve {taken}",
                                                    "/serve 80", "/serve x"])
        out = await drive(repl, console)
    finally:
        busy.close()
    assert "The HTTP service did not start (missing_api_token)" in out
    assert f"Port {taken} on 127.0.0.1 is in use" in out
    assert out.count("Usage: /serve [port|stop]") == 2
    assert commands_forge._LIVE == []


async def test_stop_servers_ends_every_live_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port()
    monkeypatch.setattr(
        commands_forge, "server_command",
        lambda profile, chosen: [sys.executable, "-c", LISTENER, str(chosen),
                                 str(tmp_path / "m.txt")],
    )
    repl, console = await start_repl(tmp_path, [f"/serve {port}"])
    try:
        assert await repl.run() == 0
        served = commands_forge.running_server(repl)
        assert served is not None and served.alive(), console.transcript()
    finally:
        repl.app.close()  # App.close stops the services this CLI started
    assert served.process.poll() is not None and commands_forge._LIVE == []
    assert commands_forge.stop_servers() == 0  # nothing left to stop
    with contextlib.suppress(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        pytest.fail("the service still listens")



# -- unpriced models (Package A's model_unpriced): build mode will not compile on them ----


def add_models(h: RepoHarness, models: dict[str, dict[str, Any]]) -> None:
    from architect.models.pricing import unpriced_price, write_price_entries

    write_price_entries(h.path.parent / "prices.json", {name: unpriced_price() for name in models})
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    for name, config in models.items():
        payload["models"][name] = {**config, "prices": "prices.json"}
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()


async def test_an_unpriced_model_blocks_the_compile_with_a_next_step(
    repo_harness, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    from architect.agent.build import UNPRICED_STEP

    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        raise AssertionError("no model server may be contacted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    h = repo_harness([INTERVIEW, spec_json()], ["", "", "", ""])
    add_models(h, {"mystery-model": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1"}})
    set_override(h.ctx, "model", "mystery-model")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False
    assert "model_unpriced" in result["failures"]  # type: ignore[operator]
    assert result["next_step"] == UNPRICED_STEP
    assert UNPRICED_STEP == (
        "declare a price: /model add <provider:model> --price <in>/<out> per million tokens, "
        "or pick a priced model with /model"
    )
    card = cards(h.ui, "Plan")[-1]
    assert card.title == "Plan (blocked)" and "model_unpriced" in card.footer
    assert any(label == "note" and "no price for mystery-model" in value
               for label, value in card.rows)
    assert "Compile this agent?" not in h.ui.questions and h.installed() == []


async def test_an_unpriced_forge_engineer_blocks_the_compile_with_a_next_step(
    repo_harness, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    from architect.agent.build import ENGINEER_UNPRICED_STEP
    from architect.models.pricing import unpriced_price, write_price_entries

    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        raise AssertionError("no model server may be contacted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    h = repo_harness([INTERVIEW, spec_json()] * 2, ["", "", "", ""], engineer="forge")
    write_price_entries(h.path.parent / "prices.json", {"mystery-engineer": unpriced_price()})
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    payload["forge_agent"] = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1",
                              "model": "mystery-engineer", "prices": "prices.json"}
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False
    failures = result["failures"]
    assert "forge_model_unpriced" in failures and "model_unpriced" not in failures  # type: ignore[operator]
    assert result["next_step"] == ENGINEER_UNPRICED_STEP
    card = cards(h.ui, "Plan")[-1]
    assert card.title == "Plan (blocked)" and "forge_model_unpriced" in card.footer
    assert dict(card.rows)["engineer"].startswith("Forge (mystery-engineer)")
    assert any(label == "note" and "no price for mystery-engineer (Forge engineer" in value
               for label, value in card.rows)
    assert "Compile this agent?" not in h.ui.questions and h.installed() == []
    # The preset engineer runs no Forge session, so its price does not matter.
    set_override(h.ctx, "engineer", "preset")
    h.ui.answers = ["", "", "", "no"]
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["cancelled"] is True, result


def test_unpriced_engineers_judge_the_entry_each_role_was_built_from(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from typing import cast

    from architect.agent.build import unpriced_engineers
    from architect.models import ModelAdapter
    from architect.models.pricing import Price, unpriced_price, write_price_entries
    from architect.profiles import build_services, load_profile, model_unpriced

    declared = Price(input_per_million=1, output_per_million=2, output_capacity_tokens=4096,
                     input_overhead_tokens=0)
    write_price_entries(tmp_path / "unpriced.json",
                        {"mystery": unpriced_price(), "qwen3:4b": unpriced_price()})
    write_price_entries(tmp_path / "declared.json", {"mystery": declared})
    remote = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1"}
    (tmp_path / "p.json").write_text(json.dumps({
        "workspace": "w",
        "models": {"fake": {"provider": "fake"}, "mystery": {**remote, "prices": "declared.json"}},
        # The Stage 0 entry is its own adapter, with its own (unpriced) table.
        "inference": {**remote, "model": "mystery", "prices": "unpriced.json"},
        "forge_agent": {"provider": "ollama", "model": "qwen3:4b", "prices": "unpriced.json"},
    }), encoding="utf-8")
    profile = load_profile(tmp_path / "p.json")
    services = build_services(profile, profile.workspace)
    try:
        stage0, engineer = services.compiler.inference_adapter, services.forge_adapter
        assert stage0 is not None and engineer is not None
        assert not model_unpriced(profile, "mystery")  # the models entry is priced
        assert unpriced_engineers(services, engineer, stage0) == ["mystery"]  # local is $0
        # Built from the models entry (an engineer named by agent_model), it is priced.
        assert unpriced_engineers(services, services.adapter_factory("mystery"), None) == []
        injected = cast(ModelAdapter, SimpleNamespace(model="injected"))  # a host seam
        assert unpriced_engineers(services, injected, None) == []
        assert unpriced_engineers(services, None, None) == []
        # The agent meters the role adapters in place: the wrapper is judged by its entry.
        from architect.session.observe import Meter, ObservedAdapter, observe_services

        metered = observe_services(services, Meter())
        wrapped = metered.compiler.inference_adapter
        assert isinstance(wrapped, ObservedAdapter)
        assert unpriced_engineers(metered, wrapped, None) == ["mystery"]
        assert unpriced_engineers(metered, metered.adapter_factory("mystery"), None) == []
    finally:
        services.close()


async def test_a_local_model_without_a_price_entry_is_zero_dollars_not_unpriced(
    repo_harness, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    def refuse(self: socket.socket, address: Any, *args: Any) -> Any:
        raise AssertionError("no model server may be contacted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    h = repo_harness([INTERVIEW, spec_json()] * 2, ["", "", "", "no"])
    add_models(h, {
        "qwen3:4b": {"provider": "ollama"},
        "local-gguf": {"provider": "openai", "base_url": "http://127.0.0.1:1234/v1",
                       "api_key_ref": "lmstudio"},
    })
    for model in ("qwen3:4b", "local-gguf"):
        set_override(h.ctx, "model", model)
        result = await make(DESCRIPTION, h.ctx)
        assert isinstance(result, dict) and result["cancelled"] is True, result  # not blocked
        rows = dict(cards(h.ui, "Plan")[-1].rows)
        assert rows["budget"].startswith("$0 per run (local model)")
        assert rows["model"] == f"{model} (local, $0)"
        h.ui.answers = ["", "", "", "no"]
