"""Phase 12 build mode (Section 5): interview, plan card, floor, compile, install (offline).

The fake provider answers Stage 0, the interview proposal and floor drafting from the
profile's inference fixture (one adapter instance, so its replies come in order); the play
model always answers "ok". Forge runs on a scripted Pi (``forge_support.ScriptedPi``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forge_support import REPAIR, ScriptedPi, ensure_sandbox, install_pi
from session_support import fake_profile, make_spec, response

from architect.agent import RecordingUI, ToolContext, ToolError
from architect.agent.build import (
    DESTINATION_QUESTION,
    PATH_QUESTION,
    agent_name,
    bar_domain,
    clear_overrides,
    default_engineer,
    forge_target,
    interview,
    make,
    override_lines,
    set_override,
)
from architect.agent.events import Notice, PlanCard, Progress
from architect.profiles import build_services, load_profile
from architect.providers.onboarding import NEBIUS_FORGE
from architect.spec import FloorDefinition, FloorKind, HarnessArtifact, Spec
from architect.telemetry import categorical

DESCRIPTION = "make an agent that extracts invoice fields from text"
INTERVIEW = json.dumps(
    {
        "inputs": {"value": "invoice text", "certain": True},
        "example": {"value": '{"text": "Invoice 7"}', "certain": True},
        "outputs": {"value": "vendor, total", "certain": False, "question": "Which fields?"},
        "correctness": {"value": "exact match", "certain": False, "question": "What is correct?"},
        "location": {"value": "installed agent", "certain": True},
        "limits": {"value": "defaults", "certain": True},
    }
)
CHEAP_OK = (
    "from pydantic import JsonValue\n"
    "from architect.runtime import RunContext\n\n"
    "async def run(context: RunContext, task: JsonValue) -> JsonValue:\n"
    '    return "ok"\n'
)


def spec_json(dataset: str = "tasks") -> str:
    return make_spec(dataset=dataset).model_dump_json()


def drafted(count: int = 12) -> str:
    return json.dumps([{"task": {"text": f"case {i}"}, "expected": "ok"} for i in range(count)])


class Harness:
    """A ToolContext on a fake profile, with a reload that rebuilds Services from disk."""

    def __init__(
        self,
        tmp_path: Path,
        replies: list[str],
        answers: list[str | None] | None = None,
        *,
        engineer: str | None = "preset",
        **profile: Any,
    ) -> None:
        self.path = fake_profile(tmp_path, **profile)
        (self.path.parent / "inference.json").write_text(
            json.dumps([response(text) for text in replies]), encoding="utf-8"
        )
        self.reloads = 0
        self.ui = RecordingUI(answers=list(answers or []))
        self.ctx = ToolContext(services=self._services(), cwd=tmp_path, ui=self.ui, model="fake")
        self.ctx.extras.update(profile_path=self.path, reload=self.reload, build_overrides={})
        if engineer is not None:
            set_override(self.ctx, "engineer", engineer)

    def _services(self) -> Any:
        profile = load_profile(self.path)
        return build_services(profile, profile.workspace)

    def reload(self) -> Any:
        self.reloads += 1
        old = self.ctx.services
        self.ctx.services = self._services()
        old.close()
        return self.ctx.services

    def cards(self) -> list[PlanCard]:
        """The plan cards (the result and export cards come after them)."""
        return [
            event for event in self.ui.events
            if isinstance(event, PlanCard) and event.title.startswith("Plan")
        ]

    def rows(self) -> dict[str, str]:
        return dict(self.cards()[-1].rows)

    def installed(self) -> list[str]:
        return [entry.name for entry in self.ctx.services.registry.list()]

    def close(self) -> None:
        self.ctx.services.close()


@pytest.fixture
def harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    made: list[Harness] = []

    def build(*args: Any, **kwargs: Any) -> Harness:
        item = Harness(tmp_path, *args, **kwargs)
        made.append(item)
        return item

    yield build
    for item in made:
        item.close()


async def test_interview_asks_only_uncertain_fields_and_enter_accepts_defaults(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()], ["", "", "each field equals the answer key", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert h.ui.questions == [
        DESTINATION_QUESTION, "Which fields?", "What is correct?", "Compile this agent?"
    ]
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["installed"] == "extracts-invoice-fields" and result["revision"] == 1
    # How to call it: the /run line and an input with the field names the agent takes.
    assert result["run"] == "/run extracts-invoice-fields <input>"
    assert result["input_example"] == {"text": "..."}
    assert result["floor"] == "1.00/1.00" and result["held_out"] == 3
    assert result["floor_origin"] == "user" and result["preset"] and result["model"] == "fake"
    assert h.installed() == ["extracts-invoice-fields"]
    installed = h.ctx.services.registry.get("extracts-invoice-fields")
    assert installed.licensed and installed.spec.lifecycle.value == "install"
    assert any(isinstance(e, Notice) and "installed extracts-invoice-fields v1" in e.text
               and " spent. A tuned " in e.text for e in h.ui.events)
    assert any(isinstance(e, Progress) and e.text.startswith("stage 4") for e in h.ui.events)


async def test_interview_answers_and_defaults(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW], ["", "", "fields equal the key"])
    settled = await interview(DESCRIPTION, h.ctx)
    assert settled is not None and settled.asked == ["location", "outputs", "correctness"]
    assert settled.destination == "run it here" and not settled.export
    assert settled.value("outputs") == "vendor, total"  # Enter kept the proposal
    assert settled.value("correctness") == "fields equal the key"
    assert "- Outputs: vendor, total" in settled.text()
    # Garbage from the model: defaults, and the three core questions are asked.
    h2 = harness(["no json here"], ["", "", "", ""])
    fallback = await interview(DESCRIPTION, h2.ctx)
    assert fallback is not None
    assert fallback.asked == ["location", "inputs", "outputs", "correctness"]
    assert fallback.proposed is False and fallback.value("location") == "run it here"


async def test_a_guessed_limit_is_never_asked_and_keeps_the_default(harness) -> None:  # type: ignore[no-untyped-def]
    guessed = json.loads(INTERVIEW)
    guessed["limits"] = {"value": "spaCy", "certain": False, "question": "OpenAI or local?"}
    h = harness([json.dumps(guessed)], ["", "", ""])
    settled = await interview(DESCRIPTION, h.ctx)
    assert settled is not None and settled.asked == ["location", "outputs", "correctness"]
    assert "OpenAI or local?" not in h.ui.questions
    assert "- Limits: about $0.10 and 60 s per run;" in settled.text()  # Stage 0 reads this
    assert "spaCy" not in settled.text()
    # A limit the user named is kept as proposed, and not asked either.
    named = json.loads(INTERVIEW)
    named["limits"] = {"value": "under $0.01 per run", "certain": True}
    h2 = harness([json.dumps(named)], ["", "", ""])
    kept = await interview(DESCRIPTION, h2.ctx)
    assert kept is not None and "limits" not in kept.asked
    assert kept.value("limits") == "under $0.01 per run"


async def test_skip_interview_accepts_every_default(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()])
    h.ctx.extras["skip_interview"] = True
    result = await make(DESCRIPTION, h.ctx)
    assert h.ui.questions == ["Compile this agent?"]
    assert isinstance(result, dict) and result["licensed"] is True


@pytest.mark.parametrize(
    ("answers", "stage"),
    [([None], "interview"), (["", "", "", None], "plan"), (["", "", "", "no"], "plan")],
)
async def test_cancel_paths_return_cancelled_and_compile_nothing(  # type: ignore[no-untyped-def]
    harness, answers: list[str | None], stage: str
) -> None:
    h = harness([INTERVIEW, spec_json()], answers)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["cancelled"] is True and result["stage"] == stage
    assert h.installed() == []
    assert not any(isinstance(e, Progress) and e.text.startswith("stage 1") for e in h.ui.events)


async def test_plan_card_shows_floor_budget_model_and_engineer(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()], ["", "", "", "no"])
    await make(DESCRIPTION, h.ctx)
    card = h.cards()[-1]
    rows = dict(card.rows)
    assert {"task", "engineer", "inputs", "outputs", "floor", "floor data", "budget", "model",
            "install as"} <= set(rows)
    assert rows["floor"] == "held_out_tasks >= 1.00 on 'tasks'"
    assert "configured floor 'tasks': 6 cases (3 tuning / 3 held-out)" == rows["floor data"]
    assert rows["budget"].startswith("$0.10 and 60 s per run; compile up to $10.00")
    assert rows["model"] == "fake" and rows["inputs"] == "text"
    # A sentence on the card, with a capital; programs read the plan's data, unchanged.
    assert rows["engineer"].startswith("Tuned preset only: ")
    assert (card.data or {})["engineer"] == "preset"
    assert "extracts-invoice-fields" in rows["install as"]
    assert card.footer.startswith("spec ")


async def test_synthetic_floor_is_drafted_registered_and_reloaded(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json("invoice-cases"), drafted()], ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert h.reloads == 1
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["floor_origin"] == "synthetic" and result["held_out"] == 6
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    assert payload["floors"]["invoice-cases"] == {
        "kind": "held_out_tasks", "path": "floors/invoice-cases.json", "retakes": True,
    }
    cases = json.loads((h.path.parent / "floors" / "invoice-cases.json").read_text("utf-8"))
    assert len(cases) == 12
    assert all(case["metadata"]["synthetic"] is True for case in cases)
    assert all(case["metadata"]["origin"] == "model_draft" for case in cases)
    rows = h.rows()
    assert "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)" == rows["floor data"]
    assert "invoice-cases" in h.ctx.services.profile.floors  # the reloaded Services


async def test_drafted_cases_relax_a_made_up_string_limit_and_the_plan_shows_it(harness) -> None:  # type: ignore[no-untyped-def]
    """The judges' walkthrough: Stage 0 limited the output with a pattern that its own
    drafted labels break. The limit goes and the cases stay (dropping them left the floor
    too small, or kept an agent that refuses real values), and the plan card shows the
    Spec that compiles."""
    limited = make_spec(dataset="invoice-cases").model_copy(
        update={"output_schema": {"type": "string", "pattern": "^[0-9]+$", "maxLength": 1}}
    )
    h = harness([INTERVIEW, limited.model_dump_json(), drafted()], ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["held_out"] == 6
    card = h.cards()[-1]
    notes = [text for label, text in card.rows if label == "note"]
    assert (
        "output schema relaxed to fit the cases: no pattern on the whole value, "
        "no maxLength on the whole value"
    ) in notes
    installed = h.ctx.services.registry.get("extracts-invoice-fields").spec
    assert installed.output_schema == {"type": "string"}
    assert card.data["spec_hash"] == installed.spec_hash != limited.spec_hash
    assert card.footer.startswith(f"spec {installed.spec_hash[:8]}")


async def test_a_configured_floor_relaxes_a_made_up_string_limit_too(harness) -> None:  # type: ignore[no-untyped-def]
    """A second build of the same job names the floor the first one drafted (live, through
    the relay): Stage 0 had added a date format and a pattern on the total this time, which
    the first build's labels break, so its exam could not be scored. The labels are the
    reference, so the limits go there as well."""
    limited = make_spec(dataset="tasks").model_copy(
        update={"output_schema": {"type": "string", "format": "date", "pattern": "^[0-9]+$"}}
    )
    h = harness([INTERVIEW, limited.model_dump_json()], ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["floor_origin"] == "user" and result["held_out"] == 3
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert (
        "output schema relaxed to fit the cases: no format on the whole value, "
        "no pattern on the whole value"
    ) in notes
    installed = h.ctx.services.registry.get("extracts-invoice-fields").spec
    assert installed.output_schema == {"type": "string"}


RECEIPT_CASES = [
    {"case_id": f"r{i}", "task": {"receipt": f"TOTAL {i}"}, "expected": {"total": i}}
    for i in range(6)
]
STRICT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("strict", [False, True])
async def test_a_configured_case_set_for_another_job_is_not_kept(harness, strict: bool) -> None:  # type: ignore[no-untyped-def]
    """Live, with the expense app's profile: for "Sort support emails into billing, bug
    report or question" Stage 0 named the profile's receipt case set as the exam. Its cases
    take ``receipt`` and the agent takes ``email_text``, so the build ended tuning_failed
    before any model answered a case. A configured set whose cases do not take the agent's
    input fields is not this job's exam: the cases are drafted, as when none is named."""
    spec = make_spec(dataset="tasks")
    if strict:
        spec = spec.model_copy(update={"input_schema": STRICT_INPUT})
    h = harness([INTERVIEW, spec.model_dump_json(), drafted()], ["", "", "", ""])
    cases_path = h.path.parent / "cases.json"
    cases_path.write_text(json.dumps(RECEIPT_CASES), encoding="utf-8")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["floor_origin"] == "synthetic" and result["held_out"] == 6
    rows = h.rows()
    assert rows["floor data"] == "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"
    assert rows["floor"].endswith("on 'extracts-invoice-fields-cases'")
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert notes[0] == (
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        "'tasks': its cases take receipt, and this agent takes text."
    )
    # The other job's set is still configured, and its file is untouched.
    floors = json.loads(h.path.read_text(encoding="utf-8"))["floors"]
    assert floors["tasks"] == {"kind": "held_out_tasks", "path": "cases.json"}
    assert json.loads(cases_path.read_text(encoding="utf-8")) == RECEIPT_CASES


async def test_a_configured_case_set_with_another_jobs_answers_is_not_kept(harness) -> None:  # type: ignore[no-untyped-def]
    """The same input field and another job's answers: no answer the agent may give can
    equal one of these labels, so this set is not kept either."""
    h = harness([INTERVIEW, spec_json("tasks"), drafted()], ["", "", "", ""])
    cases = [{"case_id": f"s{i}", "task": {"text": "x"}, "expected": {"mood": i}} for i in range(6)]
    (h.path.parent / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["floor_origin"] == "synthetic"
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert notes[0] == (
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        "'tasks': its answers do not fit what this agent gives back."
    )


async def test_a_set_that_is_not_kept_is_never_overwritten_by_the_drafted_one(harness) -> None:  # type: ignore[no-untyped-def]
    """Stage 0 named the set an earlier build of another job drafted under the name this
    build would use. The drafted cases go to a free name."""
    taken = spec_json("extracts-invoice-fields-cases")
    h = harness([INTERVIEW, taken, drafted()], ["", "", "", ""])
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    (h.path.parent / "old.json").write_text(json.dumps(RECEIPT_CASES), encoding="utf-8")
    payload["floors"]["extracts-invoice-fields-cases"] = {
        "kind": "held_out_tasks", "path": "old.json",
    }
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor"].endswith("on 'extracts-invoice-fields-cases-2'")
    floors = json.loads(h.path.read_text(encoding="utf-8"))["floors"]
    assert floors["extracts-invoice-fields-cases"]["path"] == "old.json"
    assert json.loads((h.path.parent / "old.json").read_text(encoding="utf-8")) == RECEIPT_CASES


RECEIPTS = Path(__file__).resolve().parents[1] / "examples" / "receipts"
RECEIPT_SENTENCE = "Read a receipt's text and give back the total, subtotal, tax and item count."
RECEIPT_FIELDS = ("total", "subtotal", "tax", "item_count")


def job_spec(
    task: str,
    takes: dict[str, Any],
    gives: dict[str, Any],
    dataset: str = "bench-receipts-demo",
    **more: Any,
) -> str:
    """A Spec as Stage 0 writes one for a sentence, naming ``dataset`` as its exam."""
    def strict(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}

    return Spec(
        task=task,
        input_schema=takes if "type" in takes else strict(takes),
        output_schema=strict(gives),
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset=dataset),
        budget=make_spec().budget,
        lifecycle=make_spec().lifecycle,
        **more,
    ).model_dump_json()


def with_receipt_set(h: Harness, name: str = "bench-receipts-demo") -> None:
    """The expense app's exam in the fake profile: the repository's 200 development
    receipts, with their manifest and the receipt checker."""
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    payload["floors"][name] = {
        "kind": "held_out_tasks",
        "path": str(RECEIPTS / "demo" / "floor.json"),
        "checker": "receipt_v1",
        "manifest": str(RECEIPTS / "demo" / "floor.manifest.json"),
    }
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()


def plan_notes(h: Harness) -> list[str]:
    return [text for label, text in h.cards()[-1].rows if label == "note"]


NUMBERS = {name: {"type": "number"} for name in RECEIPT_FIELDS}
STRINGS = {name: {"type": "string"} for name in RECEIPT_FIELDS}
PUBLISHED = {
    "total": {"type": ["integer", "null"]},
    "subtotal": {"type": ["integer", "null"]},
    "tax": {"type": ["integer", "null"]},
    "item_count": {"type": "integer"},
}
LOOSE_TEXT = {"type": "object", "properties": {"receipt_text": {"type": "string"}}}
DIGITS = {"type": "string", "pattern": "^\d+(\.\d+)?$"}
# What Stage 0 wrote live for this sentence (more-examples-5, step R).
LIVE = {
    "subtotal": {"oneOf": [{"type": "number"}, DIGITS]},
    "tax": {"oneOf": [{"type": "number"}, DIGITS]},
    "total": {"oneOf": [{"type": "number"}, DIGITS]},
    "item_count": {"oneOf": [{"type": "integer", "minimum": 0},
                             {"type": "string", "pattern": "^\d+$"}]},
}


@pytest.mark.parametrize(
    ("takes", "gives"),
    [
        ({"receipt_text": {"type": "string"}}, PUBLISHED),
        (LOOSE_TEXT, PUBLISHED),
        ({"type": "string"}, PUBLISHED),
        ({"receipt": {"type": "string"}}, STRINGS),
        ({"receipt_text": {"type": "string"}}, NUMBERS),
        ({"text": {"type": "string"}, "currency": {"type": "string"}}, STRINGS),
        ({"receipt_text": {"type": "string", "minLength": 1}}, LIVE),
    ],
    ids=["renamed", "renamed-loose", "plain-string", "string-amounts", "numbers", "two-fields",
         "live-one-of"],
)
async def test_the_receipt_sentence_keeps_the_real_receipts_as_its_exam(  # type: ignore[no-untyped-def]
    harness, takes: dict[str, Any], gives: dict[str, Any]
) -> None:
    """The review's finding: for the first Describe example Stage 0 may call the input
    ``receipt_text`` where the profile's 200 real receipts call it ``receipt``, or type the
    amounts as text. The set was then dropped and 12 drafted cases took its place. A set
    whose answers have exactly the agent's answer fields is this job's exam: it stays, and
    the agent takes the field its cases use and gives back the types its answers have."""
    h = harness([INTERVIEW, job_spec(RECEIPT_SENTENCE, takes, gives), drafted()],
                ["", "", "", "no"])
    with_receipt_set(h)
    result = await make(RECEIPT_SENTENCE, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result
    rows = h.rows()
    assert rows["floor"].endswith("on 'bench-receipts-demo'")
    assert "200" in rows["floor data"] and "100 tuning / 100 held-out" in rows["floor data"]
    assert "drafted" not in rows["floor data"]
    assert rows["inputs"] == "receipt"
    assert rows["outputs"] == "item_count, subtotal, tax, total"
    notes = plan_notes(h)
    assert not any("not the case set" in note for note in notes)
    assert h.reloads == 1  # nothing was drafted or registered
    assert list(json.loads(h.path.read_text(encoding="utf-8"))["floors"]) == [
        "tasks", "bench-receipts-demo",
    ]
    renamed = takes != {"receipt": {"type": "string"}}
    said = [note for note in notes if note.startswith("The exam is the case set")]
    assert len(said) == 1 and "'bench-receipts-demo'" in said[0]
    assert ("takes receipt" in said[0]) is renamed
    assert ("types" in said[0]) is (gives is not PUBLISHED)


async def test_a_reconciled_receipt_reader_sits_the_real_receipts(harness) -> None:  # type: ignore[no-untyped-def]
    """The build itself, to the end: the agent that is examined takes ``receipt`` and may
    answer an integer or null for an amount, as the receipts' answers do."""
    spec = job_spec(RECEIPT_SENTENCE, {"receipt_text": {"type": "string"}}, STRINGS)
    h = harness([INTERVIEW, spec, drafted()], ["", "", "", ""], responses=900)
    with_receipt_set(h)
    result = await make(RECEIPT_SENTENCE, h.ctx)
    assert isinstance(result, dict) and result.get("cancelled") is None, result
    # The fake model answers "ok" to every receipt, so nothing passes; what matters is that
    # the exam is the receipts and no case was refused for its input field.
    assert result["licensed"] is False and result["compiled"] is True
    assert h.rows()["floor"].endswith("on 'bench-receipts-demo'")
    counts = result["held_out_counts"]
    assert isinstance(counts, dict) and counts["cases"] == 100  # every exam receipt was sat
    assert result["floor_origin"] == "public_dataset"


EMAIL_SENTENCE = "Sort support emails into billing, bug report or question"
INVOICE_SENTENCE = "Pull the invoice number, date, vendor and total out of invoice text"


def cases_of(task: dict[str, Any], expected: dict[str, Any], count: int = 12) -> str:
    return json.dumps([
        {"task": {key: f"{value} {i}" for key, value in task.items()}, "expected": expected}
        for i in range(count)
    ])


@pytest.mark.parametrize(
    ("sentence", "takes", "gives", "answer", "base", "why"),
    [
        (
            EMAIL_SENTENCE, {"email_text": {"type": "string"}}, {"category": {"type": "string"}},
            {"category": "billing"}, "sort-support-emails",
            "its answers are total, subtotal, tax and item_count, and this agent gives back "
            "category",
        ),
        (
            INVOICE_SENTENCE, {"invoice_text": {"type": "string"}},
            {name: {"type": "string"} for name in ("invoice_number", "date", "vendor", "total")},
            {"invoice_number": "A-1", "date": "2026-01-02", "vendor": "Acme", "total": "10.00"},
            "pull-invoice-number",
            "its answers are total, subtotal, tax and item_count, and this agent gives back "
            "invoice_number, date, vendor and total",
        ),
    ],
    ids=["emails", "invoices"],
)
async def test_another_jobs_sentence_still_gets_drafted_cases_and_the_plan_says_why(  # type: ignore[no-untyped-def]
    harness, sentence: str, takes: dict[str, Any], gives: dict[str, Any],
    answer: dict[str, Any], base: str, why: str,
) -> None:
    """Live, with the expense app's profile: Stage 0 named the receipt set as the exam of
    the email sorter and of the invoice reader. Its answers have other fields than these
    agents give back, so it cannot be their exam; the cases are drafted, and the plan says
    in one sentence which exam is used and why the named set is not."""
    task = {next(iter(takes)): "text"}
    h = harness([INTERVIEW, job_spec(sentence, takes, gives), cases_of(task, answer)],
                ["", "", "", "no"])
    with_receipt_set(h)
    result = await make(sentence, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result
    rows = h.rows()
    assert rows["floor data"] == "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"
    assert rows["floor"].endswith(f"on '{base}-cases'")
    assert plan_notes(h)[0] == (
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        f"'bench-receipts-demo': {why}."
    )


async def test_the_users_case_file_relaxes_a_made_up_string_limit(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cases = [{"case_id": f"c{i}", "task": {"text": "x"}, "expected": "ok"} for i in range(6)]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    limited = make_spec(dataset="invoice-cases").model_copy(
        update={"output_schema": {"type": "string", "minLength": 5}}
    )
    h = harness([INTERVIEW, limited.model_dump_json()], ["", "", "", ""])
    set_override(h.ctx, "floor_file", "cases.json")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert "output schema relaxed to fit the cases: no minLength on the whole value" in notes


def test_interview_questions_are_whole_and_the_example_keeps_its_own() -> None:
    from architect.agent.build import _clip, _parse_interview

    rambling = (
        "Should the agent accept " + "every label variation, " * 10 + "or as found in the text?"
    )
    proposal = json.dumps(
        {
            "outputs": {"value": "vendor, total", "certain": False,
                        "question": "Which  fields\nshould it return?"},
            "correctness": {"value": "exact match", "certain": False, "question": rambling},
            "example": {"value": {"text": "Invoice 7"}, "certain": False,
                        "question": "Should label variations count as the same field?"},
        }
    )
    answers, usable = _parse_interview(proposal, DESCRIPTION)
    assert usable and answers["outputs"].question == "Which fields should it return?"
    # Too long to show whole, so the stock question stays instead of a cut one.
    assert answers["correctness"].question == "What counts as a correct output?"
    assert answers["example"].question == "One example input?"
    assert answers["example"].value == '{"text": "Invoice 7"}'
    text = ("Return one JSON object with exactly these four keys: total, subtotal, tax, "
            "item_count. The total is on a line such as TOTAL, GRAND TOTAL or AMOUNT DUE.")
    assert _clip(text, 120) == (
        "Return one JSON object with exactly these four keys: total, subtotal, tax, "
        "item_count. The total is on a line such as..."
    )
    assert _clip("alpha beta gamma", 10) == "alpha beta..."
    assert _clip("x" * 30, 10) == "x" * 10 + "..."
    assert _clip("short", 10) == "short"


def strict_spec(limit: float = 0.95, dataset: str = "invoice-cases") -> str:
    floor = FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=limit, dataset=dataset)
    return make_spec(dataset=dataset).model_copy(update={"floor": floor}).model_dump_json()


async def test_a_drafted_floor_gets_a_bar_that_one_miss_still_clears(harness) -> None:  # type: ignore[no-untyped-def]
    """With 6 drafted exam cases, Stage 0's 0.95 means 6 of 6: one wrong drafted label would
    refuse every rung. The plan lowers the bar to 0.80 and says why."""
    h = harness([INTERVIEW, strict_spec(), drafted()], ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor"] == "held_out_tasks >= 0.80 on 'invoice-cases'"
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert (
        "bar lowered from 0.95 to 0.80: at 0.95, one miss in 6 drafted exam cases would "
        "refuse the agent"
    ) in notes
    assert h.ctx.services.registry.get(str(result["installed"])).spec.floor.limit == 0.8


async def test_a_bar_set_with_floor_is_kept_on_drafted_cases(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, strict_spec(), drafted()], ["", "", "", ""])
    set_override(h.ctx, "floor_limit", 0.95)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor"] == "held_out_tasks >= 0.95 on 'invoice-cases'"
    assert not [text for label, text in h.cards()[-1].rows if text.startswith("bar lowered")]


def test_the_lowered_bar_is_the_highest_step_one_miss_clears() -> None:
    from architect.agent.build import _reachable_bar

    def bar(limit: float, tuning: int, held_out: int) -> tuple[float, str]:
        spec = Spec.model_validate_json(strict_spec(limit, "d"))
        lowered, note = _reachable_bar(spec, "d", tuning, held_out)
        return lowered.floor.limit, note

    assert bar(0.95, 6, 6)[0] == 0.8 and bar(1.0, 5, 5)[0] == 0.8
    assert bar(0.9, 4, 4)[0] == 0.75 and bar(0.9, 3, 3)[0] == 0.65
    assert bar(0.85, 6, 6)[0] == 0.8
    assert bar(0.8, 6, 6) == (0.8, "")  # one miss already clears it: 5 of 6 is 0.83
    assert bar(0.9, 2, 2) == (0.9, "")  # too few cases to lower it to anything useful
    limit, note = bar(0.95, 7, 5)
    assert limit == 0.8 and note.endswith("one miss in 5 drafted exam cases would refuse the agent")
    limit, note = bar(0.95, 4, 6)
    assert limit == 0.75 and "one miss in 4 drafted practice cases" in note


async def test_draft_failure_stops_with_a_next_step(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness(
        [INTERVIEW, spec_json("invoice-cases"), "not a case list", "still not a case list"],
        ["", "", ""],
    )
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False
    assert result["failures"] == ["floor_draft_failed"] and "/floor add" in str(result["next_step"])
    assert h.reloads == 0 and h.installed() == []
    drafts = [row for row in h.ctx.services.runtime.store.rows()
              if row.preset_origin == categorical("floor_draft")]
    assert len(drafts) == 2  # asked once more, then stopped
    # What a person reads: what happened, that nothing was built, and what to do next.
    step = str(result["next_step"])
    assert step.startswith("the model that writes the exam cases (fake) answered twice")
    assert "could be read as a list of cases" in step
    assert "names the input and output fields" in step
    said_once_and_plainly(step)


def said_once_and_plainly(step: str) -> None:
    """A stop's next step: it does not say again that nothing was built (the line before
    it says so), it holds no failure code, and it says what the cases file holds."""
    assert "nothing was built" not in step and "nothing was installed" not in step
    assert "floor_" not in step and "JSON list" in step
    assert '"case_id"' in step and '"task"' in step and '"expected"' in step
    assert step.count("/floor add <file>") == 1


def broken_draft(count: int = 12) -> str:
    """``drafted`` with the break of the live reply (``MALFORMED_DRAFT``): the fifth case
    never closes, the array closes and opens again, the sixth case never opens."""
    cases = [
        f'  {{\n    "task": {{"text": "case {i}"}},\n    "expected": "ok"\n  }}'
        for i in range(count)
    ]
    cases[4] = cases[4][: -len("\n  }")] + "\n  ]"
    cases[5] = "  [" + cases[5][len("  {") :]
    return "[\n" + ",\n".join(cases) + "\n]"


async def test_a_draft_that_is_not_json_builds_on_its_whole_cases(harness) -> None:  # type: ignore[no-untyped-def]
    """Live, the first Describe example: the drafting reply was not valid JSON, twice, and
    the build stopped with no plan. The 10 whole cases of such a reply are the exam now,
    and the plan says so."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken_draft())
    h = harness([INTERVIEW, spec_json("invoice-cases"), broken_draft(), broken_draft()],
                ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor data"].startswith("10 cases drafted, SYNTHETIC (5 tuning / 5 held-out)")
    notes = [text for label, text in h.cards()[-1].rows if label == "note"]
    assert (
        "the reply of the model that writes the exam cases (fake) was not one list of "
        "cases: the 10 complete cases in it are used as they were written, and the rest is "
        "left out"
    ) in notes
    cases = json.loads(
        (h.path.parent / "floors" / "invoice-cases.json").read_text(encoding="utf-8")
    )
    assert [case["task"]["text"] for case in cases] == [
        f"case {i}" for i in range(12) if i not in (4, 5)
    ]
    assert all(case["metadata"]["synthetic"] is True for case in cases)


async def test_a_broken_reply_with_few_whole_cases_is_not_an_exam(harness) -> None:  # type: ignore[no-untyped-def]
    """The review's finding: a reply cut short with two whole cases became one practice
    case and a one-case exam, which can license. Drafted cases are an exam only from 6 up,
    3 to practice on and 3 to be examined on: below 3 in a half the bar has no step between
    "every case" and "all but one of two". This reply closed its array and opened it
    again, so the message does not say it broke off."""
    h = harness([INTERVIEW, spec_json("invoice-cases"), broken_draft(6), broken_draft(6)],
                ["", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False, result
    assert result["failures"] == ["floor_too_small"]
    step = str(result["next_step"])
    assert step.startswith(
        "the reply of the model that writes the exam cases (fake) was not one list of "
        "cases, and 4 complete cases in it fit what this agent takes and gives back. An "
        "exam of drafted cases needs at least 6 (3 to practice on, 3 to be examined on). "
    )
    said_once_and_plainly(step)
    assert h.reloads == 0 and h.installed() == []
    assert not (h.path.parent / "floors" / "invoice-cases.json").exists()


async def test_six_whole_cases_of_a_broken_reply_are_an_exam(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json("invoice-cases"), broken_draft(8), broken_draft(8)],
                ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor data"].startswith("6 cases drafted, SYNTHETIC (3 tuning / 3 held-out)")


async def test_too_few_usable_drafted_cases_stop_with_the_counts(harness) -> None:  # type: ignore[no-untyped-def]
    """The minimum stays: with one usable case there is no exam, and the message says how
    many cases came back, how many fit and what to do."""
    cases = [{"task": {"text": "case 0"}, "expected": "ok"}]
    cases += [{"task": f"not an object {i}", "expected": "ok"} for i in range(11)]
    h = harness([INTERVIEW, spec_json("invoice-cases"), json.dumps(cases)], ["", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False
    assert result["failures"] == ["floor_too_small"]
    step = str(result["next_step"])
    assert step.startswith(
        "the model that writes the exam cases (fake) was asked for 12 cases and 1 fits "
    )
    assert "An exam of drafted cases needs at least 6" in step
    said_once_and_plainly(step)
    assert h.reloads == 0 and h.installed() == []
    assert not (h.path.parent / "floors" / "invoice-cases.json").exists()


async def test_a_draft_that_does_not_parse_is_asked_for_once_more(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json("invoice-cases"), "not a case list", drafted()],
                ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert h.rows()["floor data"] == "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"


async def test_unlicensed_result_returns_failures_and_next_step(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()], ["", "", "", ""], held_out_expected="nope")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False and result["compiled"] is True
    failures = result["failures"]
    assert isinstance(failures, list) and "primary_floor_failed" in failures
    assert "/floor add" in str(result["next_step"])
    assert h.installed() == []
    assert any(isinstance(e, Notice) and e.level == "warn" for e in h.ui.events)


async def test_overrides_are_validated_and_applied(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()], ["", "", "", ""])
    assert set_override(h.ctx, "floor_limit", "90%") == "floor limit 0.90"
    assert set_override(h.ctx, "budget_usd", "$0.25") == "per-run budget $0.25"
    assert set_override(h.ctx, "budget_s", "2m") == "per-run time 2 min"
    assert set_override(h.ctx, "model", "fake") == "model fake"
    assert set_override(h.ctx, "name", "invoices") == "install name invoices"
    for key, value, code in [
        ("floor_limit", 2, "invalid_override"),
        ("budget_usd", -1, "invalid_override"),
        ("budget_s", "soon", "invalid_override"),
        ("model", "nope", "model_not_configured"),
        ("name", "Not Kebab", "invalid_override"),
        ("engineer", "wizard", "invalid_override"),
        ("colour", "red", "unknown_override"),
    ]:
        with pytest.raises(ToolError) as caught:
            set_override(h.ctx, key, value)
        assert caught.value.code == code
    lines = override_lines(h.ctx)
    assert "floor limit: 0.90" in lines and "model: fake" in lines
    result = await make(DESCRIPTION, h.ctx)
    rows = h.rows()
    assert rows["floor"] == "held_out_tasks >= 0.90 on 'tasks'"
    assert rows["budget"].startswith("$0.25 and 2 min per run")
    assert isinstance(result, dict) and result["installed"] == "invoices", result
    spec = h.ctx.services.registry.get("invoices").spec
    assert spec.budget.cost_usd == 0.25 and spec.budget.compute_s == 120
    assert spec.floor.limit == 0.9 and spec.provenance["budget"] == "user"
    assert clear_overrides(h.ctx) and h.ctx.extras["build_overrides"] == {}


async def test_a_rebuild_of_the_same_agent_is_a_new_revision(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json(), INTERVIEW, spec_json()], [""] * 8)
    first = await make(DESCRIPTION, h.ctx)
    assert isinstance(first, dict) and first["revision"] == 1
    second = await make(DESCRIPTION, h.ctx)
    # Same Spec and a no-better artifact: the registry keeps the incumbent and says why.
    assert isinstance(second, dict) and second["licensed"] is True
    assert second["installed"] is None and second["install_refused"] == "not_better"
    assert h.installed() == ["extracts-invoice-fields"]
    # Which version stays installed, in the result and in the next step.
    assert second["kept"] == "extracts-invoice-fields v1"
    assert second["outcome"] == (
        "This build was licensed but not installed: it is not better than the installed "
        "extracts-invoice-fields v1, which stays installed."
    )
    assert str(second["next_step"]).startswith(
        "the installed extracts-invoice-fields v1 is kept (this build is not better)"
    )


async def test_a_declined_budget_increase_is_said_in_words(harness, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A rebuild that needs a higher per-run budget is installed only on a yes; on a no the
    outcome the conductor passes on says why in words, not as the code."""
    h = harness([INTERVIEW, spec_json(), INTERVIEW, spec_json()], [""] * 8)
    first = await make(DESCRIPTION, h.ctx)
    assert isinstance(first, dict) and first["installed"] == "extracts-invoice-fields"

    async def declined(ctx: ToolContext, name: str, artifact: HarnessArtifact) -> tuple[None, str]:
        return None, "budget_increase"

    monkeypatch.setattr("architect.agent.build._install", declined)
    second = await make(DESCRIPTION, h.ctx)
    assert isinstance(second, dict) and second["install_refused"] == "budget_increase"
    assert second["kept"] == "extracts-invoice-fields v1"
    assert second["outcome"] == (
        "This build was licensed but not installed: it asks for a higher per-run budget than "
        "the installed extracts-invoice-fields v1, which stays installed."
    )


async def test_a_refused_rebuild_says_the_earlier_build_stays_installed(harness) -> None:  # type: ignore[no-untyped-def]
    """A live run called the earlier installed build "Licensed" after a rebuild was refused:
    the result now says the new build was refused and which version stays installed."""
    h = harness([INTERVIEW, spec_json(), INTERVIEW, spec_json()], [""] * 8)
    first = await make(DESCRIPTION, h.ctx)
    assert isinstance(first, dict) and first["installed"] == "extracts-invoice-fields"
    # The held-out half of the cases now expects another answer, so the rebuild is refused.
    cases = h.path.parent / "cases.json"
    rows = json.loads(cases.read_text(encoding="utf-8"))
    for index, row in enumerate(rows):
        if index % 2 == 1:
            row["expected"] = "nope"
    cases.write_text(json.dumps(rows), encoding="utf-8")
    second = await make(DESCRIPTION, h.ctx)
    assert isinstance(second, dict) and second["licensed"] is False, second
    assert "installed" not in second and second["kept"] == "extracts-invoice-fields v1"
    assert second["outcome"] == (
        "This build was refused, so nothing was installed or exported. The installed "
        "extracts-invoice-fields v1 is an earlier build and stays as it was."
    )
    assert list(second)[:3] == ["licensed", "compiled", "outcome"]  # first in the result
    results = [e for e in h.ui.events if isinstance(e, PlanCard) and e.title.startswith("Result")]
    assert results[-1].title == "Result: not licensed"
    assert dict(results[-1].rows)["destination"] == (
        "nothing installed or exported; extracts-invoice-fields v1 from an earlier build stays "
        "installed"
    )
    assert h.installed() == ["extracts-invoice-fields"]
    assert h.ctx.services.registry.status("extracts-invoice-fields").revision == 1


async def test_a_refused_first_build_keeps_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW, spec_json()], ["", "", "", ""], held_out_expected="nope")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False and result["kept"] is None
    assert result["outcome"] == "This build was refused, so nothing was installed or exported."
    card = [e for e in h.ui.events if isinstance(e, PlanCard) and e.title.startswith("Result")]
    assert dict(card[-1].rows)["destination"] == "nothing installed or exported"


async def test_software_destination_asks_before_writing_and_a_no_writes_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    exporting = json.loads(INTERVIEW)
    exporting["location"] = {"value": "export it into this repo", "certain": True}
    h = harness([json.dumps(exporting), spec_json()], ["", "", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert h.ui.questions[:2] == [DESTINATION_QUESTION, PATH_QUESTION]
    assert isinstance(result, dict) and result["licensed"] is True
    assert result["destination"] == "into software"
    export = result["export"]
    assert isinstance(export, dict) and export["cancelled"] is True  # RecordingUI answers no
    assert export["path"] == "agents/extracts_invoice_fields"
    assert not (h.ctx.cwd / "agents").exists()


async def test_forge_arc_without_permission_explains_and_spends_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness([INTERVIEW])
    rows_before = len(h.ctx.services.runtime.store.rows())
    result = await forge_target("build a harness for ARC-AGI-3", h.ctx)
    assert isinstance(result, dict) and result["ran"] is False and result["target"] == "arc"
    assert result["reason"] == "arc_run_not_allowed" and result["cost_usd"] == 0.0
    commands = result["commands"]
    assert isinstance(commands, dict)
    assert "scripts/arc_harness.py" in str(commands["script"])
    assert "--tier arc" in str(commands["bench"])
    assert isinstance(result["how"], list) and len(result["how"]) >= 3
    assert h.ctx.services.compiler.inference_adapter.calls == 0
    assert len(h.ctx.services.runtime.store.rows()) == rows_before
    # Allowed, but no play model chosen: still nothing runs.
    h.ctx.extras["allow_arc_run"] = True
    again = await forge_target("arc", h.ctx)
    assert isinstance(again, dict) and again["reason"] == "no_play_model"


def test_agent_names_are_short_kebab_case() -> None:
    assert agent_name(DESCRIPTION) == "extracts-invoice-fields"
    assert agent_name("make an agent test extraction pipeline like the one in Lectern") == (
        "test-extraction-pipeline"
    )
    assert agent_name("build a harness for ARC-AGI-3") == "arc-agi-3"
    assert agent_name("!!!") == "agent"


# -- Forge as the engineer ------------------------------------------------------------


@pytest.fixture
async def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await ensure_sandbox(monkeypatch, tmp_path / "probe")


def pi_session(source: str) -> list[dict[str, Any]]:
    return [
        {"write": {"path": "harness.py", "content": source}},
        {"tool": "forge_submit", "arguments": {"note": "custom"}},
        {"text": "done"},
    ]


async def test_forge_is_the_default_engineer_and_its_harness_is_installed(  # type: ignore[no-untyped-def]
    harness, sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(pi_session(CHEAP_OK)))
    h = harness([INTERVIEW, spec_json("invoice-cases"), drafted()], ["", "", "", ""], engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    rows = h.rows()
    assert rows["engineer"].startswith("Forge (fake) writes a custom harness; baseline to beat:")
    assert rows["forge budget"].startswith("up to $5.00 and 60 min")
    assert pi.runs == 1
    assert isinstance(result, dict) and result["licensed"] is True, result
    assert result["stages"] == [1, 2, 3, 4]
    design = result["design"]
    assert isinstance(design, dict) and design["engineer"] == "forge", design
    assert design["submissions"] == 1 and design["accepted"] == 1
    assert "custom harness written by Forge" in str(design["summary"])
    installed = h.ctx.services.registry.get(str(result["installed"]))
    assert installed.licensed and installed.execution_mode == "sandbox"
    assert (installed.directory / "harness.py").read_text(encoding="utf-8") == CHEAP_OK
    # Every compiler seat the build swapped is back.
    compiler = h.ctx.services.compiler
    assert "stage_listener" not in vars(compiler) and "forge_listener" not in vars(compiler)
    assert compiler.config.forge.enabled is False and compiler.smoke_factory is None


SUPER = "nvidia/nemotron-3-super-120b-a12b"
# A Nebius Token Factory entry, as the Nebius role pack writes one. Its key is never sent:
# no plan in these tests is approved, and the address refuses connections.
NEBIUS = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_ref": "nebius"}


def engineer_on_nebius(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the fake profile the Nebius pack's Forge engineer and Forge cap, then reload."""
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    payload["forge_agent"] = {**NEBIUS, "model": SUPER, "think": False, "temperature": 0.0}
    payload["compile"]["forge"] = dict(NEBIUS_FORGE)
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()


async def test_on_token_factory_the_tuned_preset_is_the_default_engineer(  # type: ignore[no-untyped-def]
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness([INTERVIEW, spec_json()] * 2, ["", "", "", "no"] * 2, engineer=None)
    assert default_engineer(h.ctx) == "forge"  # every other provider keeps Forge
    engineer_on_nebius(h, monkeypatch)
    assert default_engineer(h.ctx) == "preset"
    assert "engineer: tuned preset only (no Forge)" in override_lines(h.ctx)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["cancelled"] is True
    rows = h.rows()
    assert rows["engineer"].startswith("Tuned preset only: ")
    assert rows["engineer"].endswith("(Forge off)")
    assert "forge budget" not in rows and "forge pack" not in rows
    assert rows["budget"] == "$0.10 and 60 s per run; compile up to $10.00 and 2 min"
    data = h.cards()[-1].data or {}
    assert (data["engineer"], data["build_usd"], data["build_s"]) == ("preset", 10.0, 120.0)
    # Forge is one choice away, and its own cap then fits a judge code's day.
    set_override(h.ctx, "engineer", "forge")
    assert any(line.startswith("engineer: Forge (agent), up to $1.20 and 45 min")
               for line in override_lines(h.ctx))
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["cancelled"] is True
    rows = h.rows()
    assert rows["engineer"].startswith(f"Forge ({SUPER}) writes a custom harness")
    assert rows["forge budget"].startswith("up to $1.20 and 45 min on top of the compile budget")
    data = h.cards()[-1].data or {}  # the compile wallet plus Forge's cap, as enforced
    assert (data["engineer"], data["build_usd"], data["build_s"]) == ("forge", 11.2, 2820.0)
    assert not h.installed()


def test_without_a_forge_agent_the_conversation_model_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    path = tmp_path / "p.json"
    path.write_text(json.dumps({
        "workspace": ".",
        "models": {"fake": {"provider": "fake"}, SUPER: NEBIUS},
    }), encoding="utf-8")
    profile = load_profile(path)
    services = build_services(profile, profile.workspace)
    try:
        ctx = ToolContext(services=services, cwd=tmp_path, ui=RecordingUI(), model=SUPER)
        assert default_engineer(ctx) == "preset"
        ctx.model = "fake"
        assert default_engineer(ctx) == "forge"
    finally:
        services.close()


def test_the_bar_views_scale_is_fixed_by_the_bar_alone() -> None:
    """Half a unit on 0.1 steps, 0.2 of it below the bar, inside [0, 1]: the scale a
    build's bar view starts from (the desktop widens it only for a score beyond it)."""
    assert bar_domain(0.40) == [0.2, 0.7]  # the receipts bar
    assert bar_domain(0.45) == [0.2, 0.7]
    assert bar_domain(0.80) == [0.5, 1.0]
    assert bar_domain(1.0) == [0.5, 1.0]
    assert bar_domain(0.1) == [0.0, 0.5]
    assert bar_domain(0.3) == [0.1, 0.6]


async def test_the_plan_and_result_cards_carry_their_values_for_the_desktop(  # type: ignore[no-untyped-def]
    harness, sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pi(monkeypatch, ScriptedPi(pi_session(CHEAP_OK)))
    h = harness([INTERVIEW, spec_json("invoice-cases"), drafted()], ["", "", "", ""], engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    plan = h.cards()[-1]
    assert plan.data is not None
    pack = str(plan.data["pack_hash"])
    assert dict(plan.rows)["forge pack"].startswith(pack) and len(pack) == 64
    spec_hash = h.ctx.services.registry.get(str(result["installed"])).spec.spec_hash
    assert plan.data["spec_hash"] == spec_hash and plan.footer.startswith(f"spec {spec_hash[:8]}")
    # Stage 0's 1.00, lowered so one miss in the 6 drafted exam cases still clears it.
    assert plan.data["ladder"] == ["fake"] and plan.data["bar"] == 0.8
    assert plan.data["domain"] == [0.5, 1.0]  # the bar view's starting scale
    assert (plan.data["practice"], plan.data["exam"]) == (6, 6)
    [card] = [e for e in h.ui.events if isinstance(e, PlanCard) and e.title.startswith("Result")]
    assert card.data is not None
    assert card.data["licensed"] is True and card.data["spec_hash"] == spec_hash
    assert card.data["pack_hash"] == pack  # the pack Forge ran with, from its forge.json
    assert card.data["exam"] == {"cases": 6, "correct": 6, "invalid": 0, "over_budget": 0}
    rungs = card.data["rungs"]
    assert isinstance(rungs, list)
    [rung] = rungs
    assert rung["ships"] is True and rung["counts"] == card.data["exam"]


async def test_forge_that_does_not_beat_the_baseline_keeps_the_preset(  # type: ignore[no-untyped-def]
    harness, sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pi(monkeypatch, ScriptedPi(pi_session(REPAIR)))  # same score, three times the cost
    h = harness([INTERVIEW, spec_json()], ["", "", "", ""], engineer=None)
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    design = result["design"]
    assert isinstance(design, dict) and design["engineer"] == "preset_baseline", design
    assert design["forge_ran"] is True and "baseline was kept" in str(design["summary"])
    artifact = h.ctx.services.registry.get(str(result["installed"]))
    assert isinstance(artifact, HarnessArtifact) and artifact.execution_mode != "sandbox"


async def test_forge_target_forces_forge_even_for_the_preset_engineer(  # type: ignore[no-untyped-def]
    harness, sandbox: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = install_pi(monkeypatch, ScriptedPi(pi_session(CHEAP_OK)))
    h = harness([INTERVIEW, spec_json()], ["", "", "", ""], engineer="preset")
    result = await forge_target("an agent that extracts invoice fields", h.ctx)
    assert pi.runs == 1
    assert isinstance(result, dict) and 3 in result["stages"]  # type: ignore[operator]


async def test_forge_arc_runs_the_preview_manufacture_only_when_allowed(  # type: ignore[no-untyped-def]
    harness, scripted_public_games: Path
) -> None:
    """The script's path on the scripted OFFLINE public games, fixture agent, template Forge:
    no provider, no CLI and no local model is involved. It needs the arc extra, so it runs
    in CI's arc-extra job."""
    pytest.importorskip("arc_agi", reason="needs the arc extra: uv sync --extra arc")
    pytest.importorskip("arcengine", reason="needs the arc extra: uv sync --extra arc")
    h = harness([INTERVIEW])
    h.ctx.extras["arc_play"] = {
        "adapter": "fixture", "model": "fixture", "cache": scripted_public_games, "games": 5,
        "seed": 11, "max_actions": 8, "cost_usd": 1.0, "compute_s": 600.0, "offline": True,
        "forge_mode": "template",
    }
    idle = await forge_target("arc", h.ctx)
    assert isinstance(idle, dict) and idle["ran"] is False
    h.ctx.extras["allow_arc_run"] = True
    result = await forge_target("arc", h.ctx)
    assert isinstance(result, dict) and result["ran"] is True, result
    assert result["licensed"] is True and result["accepted"] is True
    summary = result["summary"]
    assert isinstance(summary, dict) and summary["adapter"] == "fixture"
    assert summary["score_meaning"] == "pipeline_check_only"


def test_per_run_seconds_are_lifted_to_fit_two_cli_calls(tmp_path: Path) -> None:
    """Live evidence: an interview default of 3 s per run failed every claude CLI run."""
    from types import SimpleNamespace

    from architect.agent.build import _call_seconds, _lift_budget
    from architect.profiles import load_profile
    from architect.spec import Budget, FloorDefinition, FloorKind, Spec

    profile_path = tmp_path / "p.json"
    profile_path.write_text(json.dumps({
        "workspace": ".",
        "models": {"claude-haiku-4-5-20251001": {"provider": "claude-code"}},
        "compile": {"model_ladder": ["claude-haiku-4-5-20251001"]},
    }), encoding="utf-8")
    profile = load_profile(profile_path)

    def no_adapter(model: str) -> None:
        raise RuntimeError("not built in this test")

    services = SimpleNamespace(profile=profile, adapter_factory=no_adapter)
    spec = Spec(
        task="t", input_schema={}, output_schema={"type": "object"},
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="d"),
        budget=Budget(cost_usd=1.0, compute_s=3.0), model_target="claude-haiku-4-5-20251001",
    )
    assert _call_seconds(services, spec) == 60.0  # type: ignore[arg-type]
    lifted, note = _lift_budget(services, spec, {})  # type: ignore[arg-type]
    assert lifted.budget.compute_s == 120.0
    assert note == "per-run budget raised to 2 min so two model calls fit"
    kept, note = _lift_budget(services, spec, {"budget_s": 3.0})  # type: ignore[arg-type]
    assert kept.budget.compute_s == 3.0 and note == ""  # a user-set budget is never touched

    class Priced:  # one call quoted at $0.012
        def quote(self, request: object) -> SimpleNamespace:
            return SimpleNamespace(cost_usd=0.012)

    priced = SimpleNamespace(profile=profile, adapter_factory=lambda model: Priced())
    cheap = spec.model_copy(update={"budget": Budget(cost_usd=0.001, compute_s=3.0)})
    both, note = _lift_budget(priced, cheap, {})  # type: ignore[arg-type]
    assert (both.budget.cost_usd, both.budget.compute_s) == (0.03, 120.0)
    assert note == "per-run budget raised to $0.03 and 2 min so two model calls fit"


def test_nebius_calls_get_time_for_reasoning(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from architect.agent.build import _call_seconds
    from architect.profiles import load_profile
    from architect.spec import Budget, FloorDefinition, FloorKind, Spec

    nebius = {"provider": "openai", "base_url": "https://api.tokenfactory.nebius.com/v1",
              "api_key_ref": "nebius"}
    profile_path = tmp_path / "p.json"
    profile_path.write_text(json.dumps({
        "workspace": ".",
        "models": {
            "nvidia/Nemotron-3_5-Lightning": nebius,
            "nvidia/nemotron-3-super-120b-a12b": {**nebius, "timeout_s": 45},
            "gpt-4o-mini": {"provider": "openai"},
        },
    }), encoding="utf-8")
    services = SimpleNamespace(profile=load_profile(profile_path))

    def seconds(model: str) -> float:
        spec = Spec(
            task="t", input_schema={}, output_schema={"type": "object"},
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="d"),
            budget=Budget(cost_usd=1.0, compute_s=3.0), model_target=model,
        )
        return _call_seconds(services, spec)  # type: ignore[arg-type]

    assert seconds("nvidia/Nemotron-3_5-Lightning") == 90.0
    assert seconds("nvidia/nemotron-3-super-120b-a12b") == 45.0  # bounded by its own timeout
    assert seconds("gpt-4o-mini") == 30.0


def test_forge_events_show_notable_tool_calls_only() -> None:
    from types import SimpleNamespace

    from architect.agent.build import _forge_events
    from architect.agent.events import SubagentActivity

    ui = RecordingUI()
    ctx = SimpleNamespace(ui=ui)
    sink = _forge_events(ctx)  # type: ignore[arg-type]
    sink({"event": "tool_start", "tool": "forge_measure", "summary": "x"})
    sink({"event": "tool_end", "tool": "read", "ok": True, "summary": "SPEC.md"})
    sink({"event": "tool_end", "tool": "forge_measure", "ok": True, "summary": "score 0.83"})
    sink({"event": "tool_end", "tool": "forge_check", "ok": False, "summary": "line 4"})
    shown = [e.text for e in ui.events if isinstance(e, SubagentActivity)]
    assert shown == ["forge_measure score 0.83", "forge_check (failed) line 4"]


def test_image_agents_need_real_cases_not_drafted_text() -> None:
    from architect.agent.build import IMAGE_FLOOR_STEP, reads_images
    from architect.spec import Budget, FloorDefinition, FloorKind, Spec

    def spec(task: str, schema: dict[str, Any]) -> Spec:
        return Spec(
            task=task, input_schema=schema, output_schema={"type": "object"},
            floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=0.9, dataset="d"),
            budget=Budget(cost_usd=1.0, compute_s=60.0), model_target="fake",
        )

    pages = spec("Extract questions from scanned SAT test pages",
                 {"type": "object", "properties": {"page_images": {"type": "array"}}})
    invoice = spec("Extract invoice fields from plain text",
                   {"type": "object", "properties": {"invoice_text": {"type": "string"}}})
    web = spec("Summarize the text of web pages", {"type": "object"})
    assert reads_images(pages)
    assert not reads_images(invoice) and not reads_images(web)
    assert "architect bench pages" in IMAGE_FLOOR_STEP


# -- what the terminal shows while a build runs and after it ends --------------------------

LADDER_RUNGS: list[dict[str, Any]] = [
    {"model": "nvidia/Nemotron-3_5-Lightning", "licensed": False, "ships": False,
     "counts": {"cases": 100, "correct": 25, "invalid": 0, "over_budget": 0}},
    {"model": "nvidia/nemotron-3-super-120b-a12b", "licensed": True, "ships": True,
     "counts": {"cases": 100, "correct": 51, "invalid": 0, "over_budget": 0}},
    {"model": "nvidia/Nemotron-3-Ultra-550b-a55b", "licensed": True, "ships": False,
     "counts": {"cases": 100, "correct": 49, "invalid": 0, "over_budget": 0}},
]


async def test_stage_lines_name_the_model_and_one_rung_shows_no_ladder(harness) -> None:  # type: ignore[no-untyped-def]
    import re

    h = harness([INTERVIEW, spec_json()], ["", "", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is True, result
    lines = [e.text for e in h.ui.events if isinstance(e, Progress) and e.text.startswith("stage")]
    assert any(
        re.fullmatch(r"stage 2 tune: \d+ calls, \$[\d.]+ so far \(fake\)", line) for line in lines
    )
    assert any(line.startswith("stage 4 verify: ") for line in lines)
    assert not any("rung" in line for line in lines)  # one model: no rung count
    [card] = [e for e in h.ui.events if isinstance(e, PlanCard) and e.title.startswith("Result")]
    assert "ladder" not in dict(card.rows)


async def test_a_running_stage_repeats_its_line_with_fresh_counts_and_its_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a slow call runs, the repeated line says how long the stage has run, so the
    build reads as waiting rather than stuck."""
    import asyncio
    import time
    from types import SimpleNamespace

    from architect.agent import build

    class Tally:
        def spent(self) -> tuple[int, float]:
            return 640, 0.04

    ui = RecordingUI()
    monkeypatch.setattr(build, "HEARTBEAT_S", 0.001)
    rung = build._Rung(stage=2, model="nvidia/Nemotron-3_5-Lightning", index=1, count=3,
                       since=time.monotonic() - 150)
    beat = asyncio.ensure_future(
        build._heartbeat(SimpleNamespace(ui=ui), Tally(), False, rung)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    beat.cancel()
    lines = [e.text for e in ui.events if isinstance(e, Progress)]
    assert len(lines) >= 2
    assert set(lines) == {
        "stage 2 tune: 640 calls, $0.04 so far, 2.5 min in this stage "
        "(nvidia/Nemotron-3_5-Lightning, rung 1 of 3)"
    }
    # The line a stage opens with has no time yet.
    assert build._stage_line(rung, Tally(), False) == (  # type: ignore[arg-type]
        "stage 2 tune: 640 calls, $0.04 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)"
    )
    assert [build._elapsed(s) for s in (30.2, 89.6, 119.6, 137, 3600)] == [
        "30 s", "90 s", "2 min", "2.3 min", "60 min"
    ]


def test_the_result_card_lists_each_rungs_exam_after_the_floor(harness) -> None:  # type: ignore[no-untyped-def]
    from architect.agent.build import _ladder_text, _result_card

    assert _ladder_text([*LADDER_RUNGS, {"model": "other", "counts": None}]) == (
        "Nemotron-3_5-Lightning 25/100 refused · nemotron-3-super-120b-a12b 51/100 licensed, "
        "ships · Nemotron-3-Ultra-550b-a55b 49/100 licensed · other no exam"
    )
    h = harness([])
    _result_card(h.ctx, "Result: r v1", None, {}, "0.51/0.40", 0.31, make_spec(),
                 ("run it here (installed)", "/run r <input>"), {"rungs": LADDER_RUNGS})
    [card] = [e for e in h.ui.events if isinstance(e, PlanCard)]
    labels = [label for label, _ in card.rows]
    assert labels.index("ladder") == labels.index("floor") + 1
    assert dict(card.rows)["ladder"].startswith("Nemotron-3_5-Lightning 25/100 refused")
    assert dict(card.rows)["cost"] == "$0.31 to build; up to $0.10 and 60 s per run"


def test_the_cost_row_says_when_failed_calls_are_left_out(harness) -> None:  # type: ignore[no-untyped-def]
    """The session meter books a failed call at $0, while the judges' relay charges one in
    full when the client left after sending it: the build's cost is then a lower bound."""
    from types import SimpleNamespace

    from architect.agent.build import _result_card, _Tally

    h = harness([])
    meter = SimpleNamespace(calls=10, failed=1, spend=SimpleNamespace(cost_usd=0.5))
    h.ctx.extras["meter"] = meter
    tally = _Tally(h.ctx)
    meter.calls, meter.failed, meter.spend.cost_usd = 40, 4, 0.505
    assert tally.failed() == 3 and tally.spent() == (30, pytest.approx(0.005))
    _result_card(h.ctx, "Result: not licensed", None, {}, "0.26/0.40", 0.005, make_spec(),
                 ("nothing installed or exported", "try again"), failed=tally.failed())
    _result_card(h.ctx, "Result: r v1", None, {}, "0.51/0.40", 0.005, make_spec(),
                 ("run it here (installed)", "/run r <input>"), failed=1)
    costs = [dict(e.rows)["cost"] for e in h.ui.events if isinstance(e, PlanCard)]
    assert costs == [
        "at least $0.0050 to build (3 failed calls not counted); up to $0.10 and 60 s per run",
        "at least $0.0050 to build (1 failed call not counted); up to $0.10 and 60 s per run",
    ]
    # Without the meter, telemetry keeps each failed call's quote: nothing is left out.
    del h.ctx.extras["meter"]
    assert _Tally(h.ctx).failed() == 0


def test_a_rung_refused_on_practice_alone_says_so_in_the_ladder() -> None:
    from architect.agent.build import _ladder_text

    perfect = {"model": "nvidia/nemotron-3-super-120b-a12b", "licensed": False, "ships": False,
               "score": 1.0, "limit": 0.95, "codes": ["tuning_failed"],
               "counts": {"cases": 6, "correct": 6, "invalid": 0, "over_budget": 0}}
    assert _ladder_text([perfect]) == (
        "nemotron-3-super-120b-a12b 6/6 refused (below the bar in practice)"
    )
    # Below the bar on the exam as well: the exam score says why, so no reason is added.
    both = {**perfect, "score": 0.5, "codes": ["primary_floor_failed", "tuning_failed"],
            "counts": {"cases": 6, "correct": 3, "invalid": 0, "over_budget": 0}}
    assert _ladder_text([both]) == "nemotron-3-super-120b-a12b 3/6 refused"


def test_a_build_refused_on_practice_alone_says_a_license_needs_both(harness) -> None:  # type: ignore[no-untyped-def]
    from architect.agent.build import _next_step

    h = harness([])
    step = _next_step(["tuning_failed"], make_spec(), h.ctx.services, None)
    assert step.startswith("the practice cases missed the bar, and a license needs both")
    both = _next_step(["primary_floor_failed", "tuning_failed"], make_spec(), h.ctx.services, None)
    assert not both.startswith("the practice cases missed the bar")
    # The same word for the bar as the reason beside it ("below the bar on the exam").
    assert both == (
        "add more real cases with /floor add <file>, or lower the bar with /floor <limit>"
    )


# -- the second review: a shared answer name, the minimum, the prose preset -----------------

REVIEW_SENTENCE = "Label a product review as positive, negative or neutral"
SENTIMENT = {"type": "string", "enum": ["positive", "negative", "neutral"]}
TOPIC_CASES = [
    {"case_id": f"t{i}", "task": {"headline": f"headline {i}"}, "expected": {"label": label}}
    for i, label in enumerate(["sports", "politics", "tech"] * 2)
]


def with_topic_set(h: Harness, cases: list[dict[str, Any]], **entry: Any) -> None:
    """A case set an earlier build of another job left in the profile as ``topics``."""
    (h.path.parent / "topics.json").write_text(json.dumps(cases), encoding="utf-8")
    payload = json.loads(h.path.read_text(encoding="utf-8"))
    payload["floors"]["topics"] = {"kind": "held_out_tasks", "path": "topics.json", **entry}
    h.path.write_text(json.dumps(payload), encoding="utf-8")
    h.reload()


@pytest.mark.parametrize("label", [SENTIMENT, {"type": "string"}], ids=["enum", "free"])
async def test_a_set_that_shares_only_an_answer_name_is_not_this_jobs_exam(  # type: ignore[no-untyped-def]
    harness, label: dict[str, Any]
) -> None:
    """The second review's must-fix: Stage 0 names the topic set (``headline`` ->
    ``label``) for the review labeler (``review`` -> ``label``). The answers share a field
    name and nothing else. The agent was rebuilt to take ``headline``, its list of values
    was dropped, and it was licensed on topic labels. Now the cases are drafted for this
    job, the agent is the one that was described, and the note gives the true reason."""
    spec = job_spec(REVIEW_SENTENCE, {"review": {"type": "string"}}, {"label": label}, "topics")
    reviews = cases_of({"review": "a review"}, {"label": "neutral"})
    h = harness([INTERVIEW, spec, reviews], ["", "", "", "no"])
    with_topic_set(h, TOPIC_CASES)
    result = await make(REVIEW_SENTENCE, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result
    rows = h.rows()
    assert rows["inputs"] == "review"
    assert rows["floor data"] == "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"
    assert rows["floor"].endswith("on 'label-product-review-cases'")
    notes = plan_notes(h)
    assert notes[0] == (
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        "'topics': its cases take headline, and this agent takes review."
    )
    assert not any("relaxed" in note or "same fields" in note for note in notes)
    # The other job's set is untouched.
    assert json.loads((h.path.parent / "topics.json").read_text(encoding="utf-8")) == TOPIC_CASES


async def test_a_list_of_values_is_not_dropped_to_make_a_named_set_fit(harness) -> None:  # type: ignore[no-untyped-def]
    """The same mistake with the same input name: both jobs take ``text``. The topic
    labels are outside the values the agent gives back. Dropping that list (as is done for
    cases drafted for this job) would make the topic set this agent's exam. The sentence
    does not spell the values out, so the list is one Stage 0 made up."""
    sentence = "Label the mood of a product review"
    spec = job_spec(sentence, {"text": {"type": "string"}}, {"label": SENTIMENT}, "topics")
    reviews = cases_of({"text": "a review"}, {"label": "neutral"})
    h = harness([INTERVIEW, spec, reviews], ["", "", "", "no"])
    with_topic_set(h, [{**case, "task": {"text": case["task"]["headline"]}}
                       for case in TOPIC_CASES])
    result = await make(sentence, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result
    assert h.rows()["floor data"] == "12 cases drafted, SYNTHETIC (6 tuning / 6 held-out)"
    assert plan_notes(h) == [
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        "'topics': its answers do not fit what this agent gives back.",
        "bar lowered from 0.90 to 0.80: at 0.90, one miss in 6 drafted exam cases would "
        "refuse the agent",
    ]


async def test_an_extra_answer_field_keeps_the_receipts_and_the_note_says_it_is_not_examined(  # type: ignore[no-untyped-def]
    harness,
) -> None:
    """Stage 0 adds ``currency`` to the four receipt fields. The receipt checker reads the
    four and ignores the rest, so the 200 receipts stay the exam; the plan says that
    currency is not examined, and why the set was kept."""
    gives = {**PUBLISHED, "currency": {"type": "string"}}
    h = harness([INTERVIEW, job_spec(RECEIPT_SENTENCE, {"receipt_text": {"type": "string"}}, gives),
                 drafted()], ["", "", "", "no"])
    with_receipt_set(h)
    await make(RECEIPT_SENTENCE, h.ctx)
    rows = h.rows()
    assert rows["floor"].endswith("on 'bench-receipts-demo'") and "200" in rows["floor data"]
    assert plan_notes(h) == [
        "The exam is the case set 'bench-receipts-demo' (200 cases), which has a manifest "
        "and its own checker. To sit it, this agent takes receipt, as its cases do, and "
        "every case fits after that. Its checker reads total, subtotal, tax and item_count "
        "and nothing else, so currency is not examined."
    ]


async def test_a_near_miss_answer_name_drafts_and_the_plan_says_so(harness) -> None:  # type: ignore[no-untyped-def]
    """``items_count`` for ``item_count``: not matched on a resemblance. The receipts are
    not the exam of this build, and the plan says which fields differ."""
    gives = {name: {"type": "integer"} for name in ("total", "subtotal", "tax", "items_count")}
    answer = {"total": 1, "subtotal": 1, "tax": 0, "items_count": 1}
    h = harness([INTERVIEW, job_spec(RECEIPT_SENTENCE, {"receipt": {"type": "string"}}, gives),
                 cases_of({"receipt": "TOTAL"}, answer)], ["", "", "", "no"])
    with_receipt_set(h)
    await make(RECEIPT_SENTENCE, h.ctx)
    assert plan_notes(h)[0] == (
        "The exam is 12 cases that fake wrote for this job (SYNTHETIC), not the case set "
        "'bench-receipts-demo': its answers are total, subtotal, tax and item_count, and "
        "this agent gives back total, subtotal, tax and items_count."
    )


async def test_a_valid_reply_that_leaves_few_cases_is_not_an_exam_either(harness) -> None:  # type: ignore[no-untyped-def]
    """The minimum of 6 held only for cases kept from a broken reply. A valid list of 12
    in which 8 repeat an input or do not fit left 2 practice and 2 exam cases, where one
    answer decides the licence."""
    cases = [{"task": {"text": f"case {i % 4}"}, "expected": "ok"} for i in range(12)]
    h = harness([INTERVIEW, spec_json("invoice-cases"), json.dumps(cases)], ["", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["licensed"] is False, result
    assert result["failures"] == ["floor_too_small"]
    step = str(result["next_step"])
    assert step.startswith(
        "the model that writes the exam cases (fake) was asked for 12 cases and 4 fit what "
        "this agent takes and gives back. An exam of drafted cases needs at least 6 (3 to "
        "practice on, 3 to be examined on). "
    )
    said_once_and_plainly(step)
    assert h.reloads == 0 and h.installed() == []
    assert not (h.path.parent / "floors" / "invoice-cases.json").exists()


async def test_the_users_own_cases_file_keeps_its_minimum_of_two(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cases = [{"case_id": f"c{i}", "task": {"text": "x"}, "expected": "ok"} for i in range(4)]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    h = harness([INTERVIEW, spec_json("invoice-cases")], ["", "", "", "no"])
    set_override(h.ctx, "floor_file", "cases.json")
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result


async def test_a_reply_that_was_cut_short_is_said_to_have_broken_off(harness) -> None:  # type: ignore[no-untyped-def]
    cut = drafted(5)[:-1] + ', {"task": {"text": "case 5"}, "expec'
    h = harness([INTERVIEW, spec_json("invoice-cases"), cut, cut], ["", "", ""])
    result = await make(DESCRIPTION, h.ctx)
    assert isinstance(result, dict) and result["failures"] == ["floor_too_small"], result
    assert str(result["next_step"]).startswith(
        "the reply of the model that writes the exam cases (fake) broke off partway, and 5 "
        "complete cases in it fit what this agent takes and gives back. An exam of drafted "
        "cases needs at least 6 (3 to practice on, 3 to be examined on). "
    )


@pytest.mark.parametrize(
    ("gives", "hint", "preset"),
    [
        ({"label": SENTIMENT}, "synthesizer", "extractor"),
        ({"label": SENTIMENT, "score": {"type": "integer"}}, "synthesizer", "extractor"),
        ({"label": SENTIMENT, "why": {"type": "string"}}, "synthesizer", "synthesizer"),
        ({"label": SENTIMENT}, "verifier", "verifier"),
    ],
    ids=["label", "label-and-number", "has-free-text", "another-preset"],
)
async def test_a_job_with_no_free_text_in_its_answer_is_not_built_on_the_prose_preset(  # type: ignore[no-untyped-def]
    harness, gives: dict[str, Any], hint: str, preset: str
) -> None:
    """Live, run D of more-examples-4: for "Label a product review as positive, negative
    or neutral" Stage 0 chose the synthesizer preset, whose prompt asks for clear prose.
    Nemotron 3.5 Lightning and Nemotron 3 Super wrote a paragraph where one JSON object
    was wanted (replayed: 19 of 24 replies not JSON; 24 of 24 JSON under the extractor's
    prompt), so only the most expensive model passed a trivially easy job."""
    answer = {name: ("neutral" if name == "label" else 1 if name == "score" else "because")
              for name in gives}
    spec = job_spec(REVIEW_SENTENCE, {"review": {"type": "string"}}, gives, "review-cases",
                    preset_hint=hint)
    h = harness([INTERVIEW, spec, cases_of({"review": "a review"}, answer)], ["", "", "", "no"])
    result = await make(REVIEW_SENTENCE, h.ctx)
    assert isinstance(result, dict) and result.get("stage") == "plan", result
    assert h.rows()["engineer"] == f"Tuned preset only: {preset} (Forge off)"
    said = [note for note in plan_notes(h) if "extractor preset" in note]
    assert bool(said) is (preset != hint)
    if said:
        assert said == [
            "built on the extractor preset: this agent gives back fixed values and no free "
            "text, and the synthesizer preset the plan first named asks the model for prose"
        ]


def test_a_preset_the_user_set_and_a_plain_text_answer_keep_the_prose_preset() -> None:
    from architect.agent.build import _preset_for_answers

    inferred = Spec.model_validate_json(job_spec(
        REVIEW_SENTENCE, {"review": {"type": "string"}}, {"label": SENTIMENT},
        preset_hint="synthesizer",
    ))
    changed, note = _preset_for_answers(inferred)
    assert changed.preset_hint == "extractor" and note
    assert changed.model_dump(exclude={"preset_hint"}) == inferred.model_dump(
        exclude={"preset_hint"}
    )
    dump = inferred.model_dump(mode="json")
    theirs = Spec.model_validate({**dump, "provenance": {"preset_hint": "user"}})
    assert _preset_for_answers(theirs) == (theirs, "")
    prose = Spec.model_validate({**dump, "output_schema": {"type": "string"}})
    assert _preset_for_answers(prose) == (prose, "")
    # No hint and the prose preset as the domain: Stage 1 would select it by the domain.
    by_domain = Spec.model_validate({**dump, "preset_hint": None, "domain": "synthesizer"})
    assert _preset_for_answers(by_domain)[0].preset_hint == "extractor"
    owned = Spec.model_validate({
        **dump, "preset_hint": None, "domain": "synthesizer", "provenance": {"domain": "user"},
    })
    assert _preset_for_answers(owned) == (owned, "")
    nested = Spec.model_validate({**dump, "output_schema": {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
    }})
    assert _preset_for_answers(nested) == (nested, "")
