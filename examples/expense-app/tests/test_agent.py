"""The receipt agent's place in the app, tested without Architect and without a model.

A fake package stands in for agents/receipt_reader. What is tested is the app's side:
extract() uses the agent once it is in the project, and when the agent refuses to run
because its files changed after its exam, the app's reply and its terminal say so in
Architect's words.
"""

from __future__ import annotations

import pytest
from conftest import Refused
from fastapi.testclient import TestClient

import app.extract as extract_module
import app.main as main_module
from app.extract import PROFILE, AgentStopped, ExtractionError, Receipt, extract
from app.main import app, get_store

STOPPED = "Stopped: changed after its exam. 0 model calls."
ANSWER = {"total": 33000, "subtotal": 30000, "tax": 3000, "item_count": 2}


@pytest.fixture
def api(store):
    """The API with an empty store and the app's own extract()."""
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_without_the_agent_the_general_model_reads(monkeypatch):
    monkeypatch.setattr(extract_module, "agent_added", lambda: False)
    monkeypatch.setattr(extract_module, "ask_general_model", lambda text: Receipt(**ANSWER))
    assert extract("a receipt") == Receipt(**ANSWER)


def test_the_agent_reads_once_it_is_in_the_project(agent):
    fake = agent(ANSWER)
    assert extract("KEDAI PAGI") == Receipt(**ANSWER)
    assert fake.calls == [({"receipt": "KEDAI PAGI"}, PROFILE)]


def test_a_stopped_agent_says_so_in_the_reply_and_in_the_terminal(api, agent, terminal):
    agent(Refused())
    response = api.post("/receipts", json={"text": "KEDAI PAGI"})
    assert response.status_code == 502
    # The fake has no folder for Architect to look into, so the app can only say what the
    # refusal itself says: the change is in the agent's own files.
    step = "In Architect, retake its exam if the change allows it, or undo the change."
    assert response.json()["detail"] == (
        f"Could not read the receipt. {STOPPED} Changed: artifact/config.json. {step}"
    )
    assert terminal == [STOPPED, f"Changed: artifact/config.json. {step}"]
    assert api.get("/receipts").json() == []


def test_a_change_that_cannot_be_retaken_offers_undo_only(agent):
    agent(Refused("changed_undo_only", "artifact/harness.py"))
    with pytest.raises(AgentStopped) as stopped:
        extract("KEDAI PAGI")
    assert str(stopped.value) == (
        f"{STOPPED} Changed: artifact/harness.py. A change to this file can only be "
        "undone. Undo the change in Architect."
    )


def test_another_failure_of_the_agent_is_still_a_502(api, agent, terminal):
    agent(RuntimeError("no key"))
    response = api.post("/receipts", json={"text": "KEDAI PAGI"})
    assert response.status_code == 502
    # The reply names the kind of failure. Its text, which may hold a local path, stays
    # in the app's terminal.
    assert response.json()["detail"] == (
        "Could not read the receipt: the receipt agent failed (RuntimeError)"
    )
    assert terminal == ["Receipt agent: RuntimeError: no key"]


def test_an_app_that_runs_an_older_copy_is_told_to_restart(agent):
    refused = Refused("reloaded")
    refused.sentence = "Stopped: this app loaded an earlier version of its agent. 0 model calls."
    refused.reason_line = "The version in its folder passed its exam. Restart the app to load it."
    agent(refused)
    with pytest.raises(AgentStopped) as stopped:
        extract("KEDAI PAGI")
    assert str(stopped.value) == f"{refused.sentence} {refused.reason_line}"


def test_the_agent_is_found_in_either_folder_add_to_project_uses(tmp_path, monkeypatch):
    """receipt-reader.json's agent goes to agents/receipt_reader, and the demo plan's to
    agents/receipt_reader_demo. A folder that lost its __init__.py still counts, so the
    app does not quietly go back to the general model."""
    first = tmp_path / "agents" / "receipt_reader"
    demo = tmp_path / "agents" / "receipt_reader_demo"
    monkeypatch.setattr(extract_module, "AGENT_DIRS", (first, demo))
    assert extract_module.agent_dir() is None and not extract_module.agent_added()
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text("", encoding="utf-8")
    assert extract_module.agent_dir() == demo and extract_module.agent_added()
    (demo / "__init__.py").unlink()
    assert extract_module.agent_dir() is None
    (demo / "MANIFEST.json").write_text("{}", encoding="utf-8")
    assert extract_module.agent_dir() == demo
    first.mkdir()
    (first / "__init__.py").write_text("", encoding="utf-8")
    assert extract_module.agent_dir() == first


def test_an_agent_that_is_switched_off_names_its_flag(agent):
    class FeatureDisabled(RuntimeError):
        pass

    agent(FeatureDisabled("ARCHITECT_FEATURE_0123ABCD"))
    with pytest.raises(ExtractionError, match="set ARCHITECT_FEATURE_0123ABCD=1"):
        extract("KEDAI PAGI")


def test_the_terminal_names_the_exam_when_the_app_starts(monkeypatch, terminal):
    line = "Receipt agent: passed its exam. 59 of 100, pass mark 40, attempt 1."
    monkeypatch.setattr(main_module, "agent_line", lambda: line)
    with TestClient(app):
        pass
    assert terminal == [line]


def test_the_terminal_says_nothing_about_an_agent_that_is_not_there(monkeypatch, terminal):
    monkeypatch.setattr(extract_module, "agent_added", lambda: False)
    with TestClient(app):
        pass
    assert terminal == []
