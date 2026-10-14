import pytest
from fastapi.testclient import TestClient

from app.extract import ExtractionError, Receipt
from app.main import app, get_extractor, get_store
from app.store import Store

# What the fake reader returns for each receipt text it knows.
FAKE = {
    "coffee": Receipt(total=25000, subtotal=25000, tax=None, item_count=1),
    "lunch": Receipt(total=52800, subtotal=48000, tax=4800, item_count=2),
    "no total": Receipt(total=None, subtotal=289000, tax=None, item_count=1),
}


def fake_extract(text: str) -> Receipt:
    if text not in FAKE:
        raise ExtractionError("the fake reader does not know this receipt")
    return FAKE[text]


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def client(store):
    """The API with an empty in-memory store and the fake reader."""
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_extractor] = lambda: fake_extract
    yield TestClient(app)
    app.dependency_overrides.clear()


class Refused(Exception):
    """Stands in for the error Architect raises when the agent's files changed after its
    exam: the code as its text, and the plain words beside it."""

    def __init__(self, reason: str = "changed", changed: str = "artifact/config.json") -> None:
        super().__init__("export_integrity_mismatch")
        self.reason = reason
        self.sentence = "Stopped: changed after its exam. 0 model calls."
        self.reason_line = f"Changed: {changed}."
        if reason == "changed_undo_only":
            self.reason_line += " A change to this file can only be undone."


class FakeAgent:
    """Stands in for the package in agents/receipt_reader: ``run`` answers with what it
    was given, or raises it. It keeps every call."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[tuple[object, object]] = []

    async def run(self, value: object, *, profile: object, workspace: object = None) -> object:
        self.calls.append((value, profile))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def agent(monkeypatch):
    """Puts a fake receipt agent into the project; give it an answer with ``agent(...)``."""
    import app.extract as extract_module

    def add(answer: object) -> FakeAgent:
        fake = FakeAgent(answer)
        monkeypatch.setattr(extract_module, "agent_added", lambda: True)
        monkeypatch.setattr(extract_module, "_agent", lambda: fake)
        return fake

    return add


@pytest.fixture
def terminal():
    """The lines the app writes to its own terminal."""
    import logging

    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = Keep()
    logger = logging.getLogger("expenses")
    logger.addHandler(handler)
    yield lines
    logger.removeHandler(handler)
