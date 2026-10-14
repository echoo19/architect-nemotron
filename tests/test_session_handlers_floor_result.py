"""FLOOR, COMPILING, RESULT and EXIT handlers of ``architect.session.session`` (contract 4-5).

Every test drives the state machine through a scripted console on the fake provider and
asserts the operator-visible contract: exact codes, prompts, screen text, state
transitions, spend and the files written. Nothing here reaches a TTY, the network or a
live CLI.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError
from session_support import (
    BlockingFakeAdapter,
    FakeClock,
    GatedConsole,
    SecretFakeAdapter,
    SentinelRequestAdapter,
    dense,
    fake_profile,
    make_spec,
    run_session,
    swapping_factory,
    unwrap,
)

from architect.compiler.inference import InferenceError
from architect.models import AdapterError, FakeAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.profiles import Profile, ProfileError, Services, build_services
from architect.registry import Registry, RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.session import ScriptedConsole, Session, render
from architect.session.codes import SessionError, explain
from architect.session.commands import help_for
from architect.session.floors import HONESTY_NOTICE
from architect.session.state import State
from architect.spec import Spec

EXPIRE = make_spec().spec_hash[:8]
INSTALL = make_spec(lifecycle="install").spec_hash[:8]
GOAL = "Return ok please"
READY_LINE = "Describe what the agent should do, or type help. Registry: 0 installed."
FLOOR_HEAD = "floor f-v1  held_out_tasks  cases"
PROGRESS = re.compile(r"^\[\d\d:\d\d\] stage \d \w+ +calls \d+ \(\d+ running\)  spent \$")


def approved_header(token: str) -> str:
    """The REVIEW/RESULT Spec header of an inferred install Spec whose token was typed."""
    return f"Spec {token}  lifecycle install  stage 0: $0.0000, 0.0s  origin inferred  approved"


def failure(code: str, hint: str = "") -> str:
    """The Section 8.5 failure block: code, explanation, optional next line, wrapped at 100."""
    return render.failure(code, hint)


def drafting_adapter(text: str) -> FakeAdapter:
    return FakeAdapter(
        responses=[
            ModelResponse(text=text, tokens_in=8, tokens_out=12, cost_usd=0.0002, model="fake")
        ]
    )


def two_cases() -> str:
    return json.dumps(
        [{"task": {"review": "r1"}, "expected": "ok"}, {"task": {"review": "r2"}, "expected": "ok"}]
    )


def raising_factory(exc: BaseException) -> Callable[[Profile, Path], Services]:
    """A services factory whose compiler raises ``exc`` instead of compiling."""

    def factory(profile: Profile, workspace: Path) -> Services:
        services = build_services(profile, workspace)

        async def compile(source: object, **kw: object) -> object:  # noqa: ANN401
            raise exc

        services.compiler.compile = compile  # type: ignore[method-assign]
        return services

    return factory


def _session(lines: list[str], tmp_path: Path, **kw: object) -> tuple[Session, ScriptedConsole]:
    console = ScriptedConsole(lines)
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        **kw,  # type: ignore[arg-type]
    )
    return session, console


class HookedConsole(ScriptedConsole):
    """Runs a hook the first time a given text is written (between two screens)."""

    def __init__(self, lines: list[str], hooks: dict[str, Callable[[], None]]) -> None:
        super().__init__(lines)
        self._hooks = hooks

    def write(self, text: str) -> None:
        super().write(text)
        for key in list(self._hooks):
            if key in text:
                self._hooks.pop(key)()


class PricedAdapter(FakeAdapter):
    """A fake whose per-call cost can be changed mid-session (a cheaper second compile)."""

    def __init__(self, cost: float) -> None:
        super().__init__(model="fake")
        self.cost = cost

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=8, tokens_out=12, cost_usd=self.cost)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="ok", tokens_in=8, tokens_out=12, cost_usd=self.cost, model="fake"
        )


class BlockOnceAdapter(BlockingFakeAdapter):
    """Blocks its first call until cancelled; every later call answers at once."""

    def __init__(self) -> None:
        super().__init__()
        self.blocked = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.blocked:
            self.blocked = True
            self.started.set()
            await self.release.wait()  # never set: only a cancellation ends this call
        return await FakeAdapter.complete(self, request)


class WaitingConsole(GatedConsole):
    """A gated console whose lines can also wait for an event or for printed output."""

    def __init__(
        self,
        lines: list[tuple[str, State]],
        *,
        before: dict[str, Callable[[], asyncio.Event]] | None = None,
        printed: dict[str, re.Pattern[str]] | None = None,
    ) -> None:
        super().__init__(lines)
        self._before = before or {}
        self._printed = printed or {}

    async def read(self, prompt: str) -> str | None:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            event = self._before.get(line)
            if event is not None:
                await asyncio.wait_for(event().wait(), timeout=30)
            pattern = self._printed.get(line)
            if pattern is not None:
                for _ in range(60_000):
                    if any(pattern.match(chunk) for chunk in self.output):
                        break
                    await asyncio.sleep(0.001)
        return await super().read(prompt)


# ------------------------------------------------------------------------------- FLOOR


def test_floor_show_use_add_and_prose(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    (tmp_path / "in.json").write_text(
        json.dumps([{"case_id": "f1", "task": "a", "expected": 1, "metadata": {"k": "v"}}]),
        encoding="utf-8",
    )
    lines = [
        "floor f-v1",
        "show",
        "use",
        "use in.json",
        'add {"task": {"review": "great"}, "expected": "pos"}',
        "add",
        "add [1, 2]",
        'add {"task": "no expected"}',
        "add {not json",
        "this is prose",
        '{"task": "looks like a command", "expected": 1}',
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    # entry, show, after use, after add: four renders of the floor screen
    assert transcript.count(FLOOR_HEAD) == 4
    assert failure("not_a_command", "use <path>") in transcript
    assert "loaded 1 cases from in.json" in transcript
    assert '  f1  [file]  task: "a"\n    expected: 1\n' in transcript
    assert '  f-v1-001  [op]  task: {"review":"great"}\n    expected: "pos"\n' in transcript
    assert "cases 2 (operator 1, file 1, model_draft 0)  unreviewed 0  split 1 / 1" in transcript
    # The empty screen names every way in; the ready one says accept.
    empty_next = 'Next: use <file> | add {"task":..., "expected":...} | paste | draft 20\n'
    assert transcript.count("blocked: below minimum: 0 cases, need 2\n") == 2
    assert transcript.count(empty_next) == 2  # entry and show
    assert "blocked: held-out partition would have 0 cases\n" + empty_next in transcript
    assert 'Next: add {"task":..., "expected":...} | paste | draft 20 | back\n' in transcript
    assert "ready: type accept\nNext: accept | back\n" in transcript
    assert transcript.count(failure("invalid_input_json")) == 2  # `add` alone, `add {not json`
    assert transcript.count(failure("invalid_floor_file")) == 2  # a list and a missing field
    assert transcript.count("error: not_a_command\n") == 3
    assert transcript.count(failure("not_a_command", "help")) == 2  # prose and a JSON line
    assert "allowed here:" not in transcript
    assert session.floor is not None and len(session.floor.cases) == 2
    assert [item.origin for item in session.floor.cases] == ["file", "operator"]
    assert session.floor.cases[0].case.metadata == {"k": "v"}
    assert not (tmp_path / "w" / "floors").exists()


def test_floor_paste_array_json_lines_and_terminator(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        "floor f-v1",
        "paste",
        '[{"task": "a", "expected": 1},',
        ' {"task": "b", "expected": 2}]',
        ".",
        "paste",
        '{"task": "c", "expected": 3}',
        "",
        '{"task": "d", "expected": 4}',
        "  .  ",
        "paste",
        '{"task": "single", "expected": 5}',
        ".",
        "paste",
        ".",
        "paste",
        '{"task": "e", "expected": 6}',
        "not json at all",
        ".",
        "split",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("added 2 cases") == 2  # the array, then JSON lines (blank skipped)
    assert "added 1 cases" in transcript  # one object
    assert "added 0 cases" in transcript  # an empty buffer
    assert failure("invalid_input_json") in transcript  # one bad row adds nothing
    assert session.floor is not None
    ids = [item.case.case_id for item in session.floor.cases]
    assert ids == [f"f-v1-00{i}" for i in range(1, 6)]
    assert all(item.origin == "operator" and item.reviewed for item in session.floor.cases)
    assert "tuning (harness sees during Stage 2): f-v1-001, f-v1-003, f-v1-005\n" in transcript
    assert "held-out (harness never sees): f-v1-002, f-v1-004\n" in transcript
    # Paste continuation lines keep the FLOOR prompt; there is no paste> prompt.
    assert "paste> " not in transcript
    assert transcript.count("floor f-v1> ") == len(lines) - 1  # every line after `floor f-v1`


def test_floor_split_sorts_ids_like_the_evaluator(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        "floor f-v1",
        "split",
        'add {"task": 1, "expected": 1, "case_id": "b"}',
        'add {"task": 2, "expected": 2, "case_id": "a"}',
        'add {"task": 3, "expected": 3, "case_id": "c"}',
        "split",
        "quit",
    ]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert "tuning (harness sees during Stage 2): \n" in transcript
    assert "held-out (harness never sees): \n" in transcript
    assert "tuning (harness sees during Stage 2): a, c\n" in transcript
    assert "held-out (harness never sees): b\n" in transcript


def test_floor_draft_range_errors_and_no_draft_spend_nothing(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    adapter = drafting_adapter(two_cases())
    lines = [
        "floor f-v1",
        "draft 2",
        "back",
        "spec new.json",
        "floor",
        "draft x",
        "draft 1",
        "draft 65",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert failure("no_draft") in transcript  # a floor started from READY has no Spec to draft for
    assert transcript.count(failure("draft_count_out_of_range")) == 3
    assert explain("draft_count_out_of_range") == "Draft between 2 and 64 cases at a time."
    assert "(SYNTHETIC, unreviewed)" not in transcript
    assert adapter.calls == 0 and session.session_spend.calls == 0
    assert session.floor is not None and session.floor.cases == []


def test_floor_bare_draft_asks_for_twenty_cases(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    adapter = SentinelRequestAdapter()  # answers "ok": the draft fails after one metered call
    lines = ["spec new.json", "floor", "draft", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert transcript.count(HONESTY_NOTICE) == 1
    assert failure("draft_count_out_of_range") not in transcript
    assert failure("inference_failed") in transcript
    assert len(adapter.requests) == 1 and '"count": 20' in adapter.requests[0].prompt
    assert session.session_spend.calls == 1
    assert help_for("draft") == (
        "  draft [n]                                one metered call proposing n SYNTHETIC cases"
        " (default 20)\n"
    )


def test_floor_draft_uses_the_compile_model_when_target_is_not_configured(
    tmp_path: Path,
) -> None:
    spec = make_spec(dataset="f-v1").model_copy(update={"model_target": "other"})
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    adapter = drafting_adapter(two_cases())
    lines = [
        "spec new.json",
        "floor",
        "draft 2",
        "review f-v1-s001 ok",
        "review f-v1-s002 ok",
        "accept",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert transcript.count(HONESTY_NOTICE) == 1
    assert "drafted 2 cases (SYNTHETIC, unreviewed): review each with 'review <id> ok'" in (
        transcript
    )
    assert adapter.calls == 1 and session.session_spend.calls == 1
    assert (
        "floor 'f-v1' configured: 2 cases -> 1 tuning / 1 held-out [SYNTHETIC: 2 of 2 cases "
        "model-drafted by fake, accepted by operator; not production evidence]"
    ) in unwrap(transcript)
    rows = json.loads((tmp_path / "w" / "floors" / "f-v1.json").read_text(encoding="utf-8"))
    assert [row["metadata"]["drafted_by_model"] for row in rows] == ["fake", "fake"]
    # Back in REVIEW: the Spec screen is re-rendered with the floor configured, same hash.
    tail = transcript.split("configured: 2 cases", 1)[1]
    assert f"Spec {spec.spec_hash[:8]}  lifecycle expire" in tail
    assert "configured: yes, 2 cases -> 1 tuning / 1 held-out (0 misses allowed)" in tail
    assert f"review {spec.spec_hash[:8]}> quit" in tail


def test_floor_draft_failure_is_one_code_and_is_metered(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    adapter = drafting_adapter("SECRET-PROVIDER-TEXT not a json array")
    lines = ["spec new.json", "floor", "draft 2", "accept", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert transcript.count(HONESTY_NOTICE) == 1
    assert failure("inference_failed") in transcript and "SECRET-PROVIDER-TEXT" not in transcript
    assert "(SYNTHETIC, unreviewed)" not in transcript
    assert adapter.calls == 1 and session.session_spend.calls == 1
    assert "error: floor_too_small" in transcript  # nothing was added by the failed draft
    assert session.floor is not None and session.floor.cases == []


def test_floor_review_ok_expected_drop_and_errors(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    drafted = json.dumps([{"task": {"review": f"r{i}"}, "expected": "ok"} for i in range(3)])
    adapter = drafting_adapter(drafted)
    lines = [
        "spec new.json",
        "floor",
        "draft 3",
        "review",
        "review f-v1-s001 maybe",
        "review f-v1-s001 expected",
        "review nope ok",
        "review f-v1-s001 ok",
        'review f-v1-s002 expected {"label": "fixed"}',
        "review f-v1-s003 drop",
        "review f-v1-s003 ok",
        "show",
        "accept",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    refusal = failure("not_a_command", "review <id> ok | expected <json> | drop")
    assert transcript.count(refusal) == 2
    assert failure("invalid_value_json") in transcript
    assert transcript.count(failure("unknown_case_id")) == 2  # unknown id, and a dropped one
    # After draft: the three drafted cases with content, unreviewed first (all of them here).
    assert (
        '  f-v1-s001  [draft ?]  task: {"review":"r0"}\n    expected: "ok"\n'
        '  f-v1-s002  [draft ?]  task: {"review":"r1"}\n    expected: "ok"\n'
        '  f-v1-s003  [draft ?]  task: {"review":"r2"}\n    expected: "ok"\n'
    ) in transcript
    # Each review answers with one line, the count and the next step; no screen reprint.
    assert transcript.split("floor f-v1> review f-v1-s001 ok\n", 1)[1].startswith(
        'reviewed f-v1-s001 ok  task: {"review":"r0"}\nunreviewed: 2\n'
        "next: review f-v1-s002 ok|expected <json>|drop\nfloor f-v1> "
    )
    assert transcript.split('floor f-v1> review f-v1-s002 expected {"label": "fixed"}\n', 1)[
        1
    ].startswith(
        'reviewed f-v1-s002 expected -> {"label":"fixed"}\nunreviewed: 1\n'
        "next: review f-v1-s003 ok|expected <json>|drop\nfloor f-v1> "
    )
    assert transcript.split("floor f-v1> review f-v1-s003 drop\n", 1)[1].startswith(
        "dropped f-v1-s003\nunreviewed: 0\nready: type accept\nfloor f-v1> "
    )
    assert transcript.count(FLOOR_HEAD) == 3  # entry, after draft, show
    # `show` after the reviews: flags and the corrected label, then accept.
    shown = transcript.split("floor f-v1> show\n", 1)[1]
    assert shown.startswith(
        "floor f-v1  held_out_tasks  cases 2 (operator 0, file 0, model_draft 2)  unreviewed 0"
        "  split 1 / 1\n"
        '  f-v1-s001  [draft ok]  task: {"review":"r0"}\n    expected: "ok"\n'
        '  f-v1-s002  [draft fixed]  task: {"review":"r1"}\n    expected: {"label":"fixed"}\n'
        "  1 held-out cases at limit 1.00 allow at most 0 misses; small floors license lucky"
        " harnesses\n"
        "ready: type accept\nNext: accept | back\n"
    )
    assert "f-v1-s003" not in shown
    rows = json.loads((tmp_path / "w" / "floors" / "f-v1.json").read_text(encoding="utf-8"))
    assert [row["case_id"] for row in rows] == ["f-v1-s001", "f-v1-s002"]
    assert rows[1]["expected"] == {"label": "fixed"} and rows[1]["metadata"]["corrected"] is True
    assert "corrected" not in rows[0]["metadata"]
    assert session.state is State.EXIT and session.floor is None


def test_floor_show_one_case_in_full_and_never_a_configured_one(tmp_path: Path) -> None:
    fake_profile(tmp_path)  # configures floor "tasks" with case ids "0".."5" and task {}
    spec = make_spec(dataset="f-v1")
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    # Both cases fit the spec's schemas (an object task, a string label): draft_cases drops
    # any that does not.
    drafted = json.dumps(
        [
            {"task": {"text": "great", "meta": {"lang": "en"}}, "expected": "pos"},
            {"task": {"text": "caf\u00e9 \x1b[2J"}, "expected": "x" * 150},
        ]
    )
    adapter = drafting_adapter(drafted)
    lines = [
        "spec new.json",
        "floor",
        "draft 2",
        "show f-v1-s001",
        'review f-v1-s002 expected "fixed"',
        "show f-v1-s002",
        "show nope",
        "back",
        "y",
        "discard",
        "floor tasks",
        "show",
        "show 0",
        "show 1",
        "back",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert transcript.split("floor f-v1> show f-v1-s001\n", 1)[1].startswith(
        "f-v1-s001  [draft ?]\n"
        "task:\n"
        "{\n"
        '  "meta": {\n'
        '    "lang": "en"\n'
        "  },\n"
        '  "text": "great"\n'
        "}\n"
        "expected:\n"
        '"pos"\n'
        "spend  session $0.0002"
    )
    second = transcript.split("floor f-v1> show f-v1-s002\n", 1)[1]
    assert second.startswith(
        'f-v1-s002  [draft fixed]\ntask:\n{\n  "text": "caf\\u00e9 \\u001b[2J"\n}\n'
        'expected:\n"fixed"\n'
    )
    assert transcript.isascii() and "\x1b" not in transcript
    assert transcript.count(failure("unknown_case_id")) == 1
    # The screen for a drafted case with a 150-character label clips it; `show` shows it whole.
    assert '    expected: "' + "x" * 82 + "...\n" in transcript
    # A configured floor is never printed, one case at a time or otherwise.
    configured = transcript.split("floor tasks> show\n", 1)[1]
    assert "floor 'tasks' is already configured" in configured
    assert transcript.count(failure("not_a_command", "back")) == 2  # show 0, show 1
    assert '"task": {}' not in transcript and "expected:\n\"ok\"" not in transcript
    assert session.state is State.EXIT


def test_floor_accept_refusals_name_every_reason(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    adapter = drafting_adapter(two_cases())
    lines = [
        "spec new.json",
        "floor",
        "accept",
        'add {"task": "a", "expected": 1}',
        "accept",
        "draft 2",
        "accept",
        "quit",
    ]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(adapter)
    )
    assert code == 0
    assert transcript.count("error: floor_too_small\n  " + explain("floor_too_small")) == 2
    assert explain("floor_too_small") == "The floor has fewer cases than its kind needs."
    assert "blocked: below minimum: 0 cases, need 2\nblocked: held-out partition would have 0" in (
        transcript
    )
    assert "blocked: below minimum: 1 cases, need 2\nblocked: held-out partition would have 0" in (
        transcript
    )
    assert (
        failure(
            "unreviewed_cases",
            "review <id> ok | review <id> expected <json> | review <id> drop",
        )
        in transcript
    )
    assert transcript.count("blocked: 2 model-drafted cases are unreviewed") >= 2  # screen, accept
    assert "configured: yes" not in transcript and "floor 'f-v1' configured" not in transcript
    assert not (tmp_path / "w" / "floors").exists()
    assert session.state is State.EXIT and session.floor is not None
    assert len(session.floor.cases) == 3


def test_floor_back_confirmation_and_return_states(tmp_path: Path) -> None:
    spec = make_spec(dataset="f-v1")
    fake_profile(tmp_path)
    (tmp_path / "new.json").write_text(spec.model_dump_json(), encoding="utf-8")
    token = spec.spec_hash[:8]
    lines = [
        "floor f-v1",
        "back",  # empty draft: no confirmation
        "spec new.json",
        "floor",
        'add {"task": "a", "expected": 1}',
        "back",
        "",  # empty answer defaults to No
        "back",
        "nope",  # anything but y/yes is No
        "back",
        "yes",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("confirm> discard 1 unsaved cases? [y/N] ") == 3
    assert transcript.count(READY_LINE) == 2  # boot and the return from the READY floor
    assert transcript.count(FLOOR_HEAD) == 3  # READY entry, REVIEW entry, after add
    # After the confirmed back the REVIEW screen is re-rendered with the same hash.
    after = transcript.split("[y/N] yes\n", 1)[1]
    assert after.startswith(f"Spec {token}  lifecycle expire (single use, no approval needed)")
    assert f"review {token}> quit" in after
    assert session.floor is None and session.draft is not None
    assert not (tmp_path / "w" / "floors").exists()


def test_floor_back_eof_at_the_confirmation_discards_nothing(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = ["floor f-v1", 'add {"task": "a", "expected": 1}', "back"]
    session, console = _session(lines, tmp_path)
    assert asyncio.run(session.run()) == 0
    assert console.prompts[-2:] == ["confirm> discard 1 unsaved cases? [y/N] ", "floor f-v1> "]
    assert session.state is State.EXIT
    assert not (tmp_path / "w" / "floors").exists()
    assert console.transcript().rstrip().endswith("bye")


def test_configured_floor_is_shown_and_only_back_works(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        "floor tasks",
        "show",
        'add {"task": "a", "expected": 1}',
        "use in.json",
        "accept",
        "back",
        f"describe {GOAL}",
        "floor",
        "back",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    cases = tmp_path / "w" / "cases.json"
    summary = f"tasks  held_out_tasks  6 cases -> 3 tuning / 3 held-out  {cases}"
    assert dense(transcript).count(dense(summary)) == 3  # entry, show, and the REVIEW entry
    already = "floor 'tasks' is already configured; editing it here is not supported. Type back."
    assert unwrap(transcript).count(already) == 3
    assert transcript.count(failure("not_a_command", "back")) == 3
    assert "confirm>" not in transcript and FLOOR_HEAD not in transcript
    assert transcript.count(READY_LINE) == 2
    assert transcript.count(f"Spec {EXPIRE}  lifecycle expire") == 2  # entry and after back
    assert transcript.count("floor tasks> ") == 6
    assert session.floor is None and session.state is State.EXIT
    rows = json.loads(cases.read_text(encoding="utf-8"))
    assert len(rows) == 6 and rows[0] == {"case_id": "0", "task": {}, "expected": "ok"}


# --------------------------------------------------------------------------- COMPILING


def test_status_spend_and_help_during_compile_and_busy_refusals(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    adapter = BlockingFakeAdapter()
    console = WaitingConsole(
        [
            (f"describe {GOAL}", State.READY),
            (f"approve {INSTALL}", State.REVIEW),
            ("status", State.COMPILING),
            ("spend", State.COMPILING),
            ("help", State.COMPILING),
            ("profile", State.COMPILING),
            (f"approve {INSTALL}", State.COMPILING),
            ("run {}", State.COMPILING),
            ("cancel", State.COMPILING),
            ("quit", State.REVIEW),
        ],
        printed={"status": PROGRESS},  # typed once the watcher has printed a stage line
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert adapter.started.is_set()
    segments = transcript.split(f"compiling {INSTALL}> ")
    assert len(segments) == 8  # seven lines typed at the compiling prompt
    status = segments[1]
    assert "state            compiling" in status
    assert f"draft            {INSTALL}  origin inferred  edits 0" in status
    assert "outcome          none" in status and "compile_spend    -" in status
    assert PROGRESS.match(status.rstrip("\n").rsplit("\n", 1)[1])  # the latest progress line
    footer = (
        "spend  session $0.0000 | this compile - | calls 1 | wall 0m00s | "
        "API-equivalent, not a bill"
    )
    spend_lines = [line for line in segments[2].splitlines() if not PROGRESS.match(line)]
    assert spend_lines == [footer]
    assert "Compiling; the reader stays active:" in segments[3]
    assert transcript.count(failure("busy_compiling")) == 3  # profile, approve, run
    assert transcript.count("allowed here: cancel, exit, help, quit, spend, status") == 3
    assert failure("cancelled") in transcript and "cancelled after $0.0000, 0.0s" in transcript
    assert transcript.count("approved by operator  (cancel or Ctrl-C to stop)") == 1
    assert transcript.count(f"approved {INSTALL} - compiling") == 1
    assert session.outcome is None and session.draft is not None
    assert session.approved(session.draft.spec)  # a cancel keeps the approval
    assert Registry(tmp_path / "w" / "registry").list() == ()


def test_compile_progress_prints_stage_changes_and_every_fifth_call(tmp_path: Path) -> None:
    """One line per stage change and per fifth completed call; never one per call."""
    fake_profile(tmp_path, cases=24, responses=60)  # 12 tuning + 12 held-out: 24 calls
    lines = [f"describe {GOAL}", "compile", "report", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0 and session.outcome is not None
    finished = re.search(r"\[00:00\] compile finished in 0\.0s: (\d+) calls, \$", transcript)
    assert finished is not None
    calls = int(finished.group(1))
    assert calls == 24  # two lines per call would be 48 progress lines
    progress = [line for line in transcript.splitlines() if PROGRESS.match(line)]
    assert 3 <= len(progress) <= 8, progress
    stages = [int(line.split()[2]) for line in progress]  # "[00:00] stage N ..."
    assert stages == sorted(stages) and stages[0] == 1 and set(stages) <= {1, 2, 4}
    # The fake clock never moves, so no line is owed to the 20 s rule.
    assert all(line.startswith("[00:00] stage ") for line in progress)
    assert "failed)" not in transcript


def test_compile_progress_prints_a_line_after_twenty_seconds(tmp_path: Path) -> None:
    """A blocked call prints nothing until 20 s of compile time passed; then one line."""
    fake_profile(tmp_path, lifecycle="install")
    adapter = BlockingFakeAdapter()
    clock = FakeClock()

    def advance() -> asyncio.Event:
        clock.advance(25.0)
        event = asyncio.Event()
        event.set()
        return event

    tuning = re.compile(r"^\[00:00\] stage 2 tuning")
    stale = re.compile(r"^\[00:25\] stage 2 tuning")
    console = WaitingConsole(
        [
            (f"describe {GOAL}", State.READY),
            (f"approve {INSTALL}", State.REVIEW),
            ("spend", State.COMPILING),  # once the first call is blocked and stage 2 is on screen
            ("cancel", State.COMPILING),  # the clock jumps 25 s, then the stale line appears
            ("quit", State.REVIEW),
        ],
        before={"spend": lambda: adapter.started, "cancel": advance},
        printed={"spend": tuning, "cancel": stale},
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=clock,
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    progress = [line for line in transcript.splitlines() if PROGRESS.match(line)]
    # One line per stage, nothing for the call that started and never returned, then one
    # line once 20 s passed; the poll ticks in between printed nothing.
    assert len(progress) == 3, progress
    assert progress[0].startswith("[00:00] stage 1 selection")
    assert progress[1].startswith("[00:00] stage 2 tuning")
    assert progress[2] == (
        "[00:25] stage 2 tuning        calls 0 (1 running)  spent $0.0000  left $10.00 / 1m35s"
    )
    assert "cancelled after $0.0000, 25.0s" in transcript


def test_result_registry_commands_read_installed_artifacts(tmp_path: Path) -> None:
    """list, info, floors and run <name> read the registry from RESULT; run {json} stays."""
    fake_profile(tmp_path, lifecycle="install")
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "list",
        "info demo",
        "floors",
        "run demo {}",
        "run nobody {}",
        "run {}",
        "run",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    after = transcript.split("installed demo revision 1 -> ", 1)[1]
    assert f"{'demo':<15} {1:>3}  {INSTALL:<8}  {1:>3}  yes" in after
    assert f"demo  revision 1  spec {INSTALL}  version 1  licensed yes" in after
    assert "tasks  held_out_tasks  6 cases -> 3 tuning / 3 held-out" in unwrap(after)
    assert after.count('\n"ok"\n') == 2  # run demo {} (installed) and run {} (this one)
    assert "error: unknown_name" in after
    assert failure("not_a_command", "run {json} | run <name|dir> {json}") in after
    assert transcript.count(f"result {INSTALL}> ") == 9  # every line stayed in RESULT
    assert session.outcome is not None and session.state is State.EXIT


def test_cancel_then_approve_again_compiles_to_a_license(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    adapter = BlockOnceAdapter()
    console = WaitingConsole(
        [
            (f"describe {GOAL}", State.READY),
            (f"approve {INSTALL}", State.REVIEW),
            ("cancel", State.COMPILING),
            ("show", State.REVIEW),
            (f"approve {INSTALL}", State.REVIEW),
            ("report", State.RESULT),
            ("quit", State.RESULT),
        ],
        before={"cancel": lambda: adapter.started},  # cancel lands inside the blocked call
    )
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    console.session = session
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert transcript.count(failure("cancelled")) == 1
    flat = unwrap(transcript)
    assert (
        f"cancelled after $0.0000, 0.0s; unlicensed candidate directories may remain under "
        f"artifacts/{make_spec(lifecycle='install').spec_hash}/ and are never licensed"
    ) in flat
    # Back in REVIEW the approval is intact: no new token dance, no stale token.
    assert transcript.count(approved_header(INSTALL)) == 2  # after the cancel, and on show
    assert transcript.count(f"approved {INSTALL} - compiling.") == 2
    assert "error: stale_approval_token" not in transcript
    assert transcript.count("licensed: YES (recomputed from disk)") == 2
    assert session.outcome is not None and session.outcome.licensed
    assert session.outcome.approved_hash == make_spec(lifecycle="install").spec_hash


def _validation_error() -> ValidationError:
    try:
        Spec.model_validate({"task": "SECRET-TEXT"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (InferenceError("SECRET-TEXT"), "inference_failed"),
        (BudgetExceeded("SECRET-TEXT", required_cost_usd=9.0), "budget_exceeded"),
        (_validation_error(), "invalid_spec"),
        (UnlicensedArtifactError("SECRET-TEXT"), "unlicensed_artifact"),
        (ArtifactExpiredError("SECRET-TEXT"), "artifact_expired"),
        (AdapterError("Provider CLI is unavailable"), "provider_unavailable"),
        (AdapterError("Provider CLI timed out"), "provider_timeout"),
        (AdapterError("sk-SECRET-TEXT"), "provider_failed"),
        (RegistryError("workspace_path_too_long"), "workspace_path_too_long"),
        (ProfileError("floor_not_configured"), "floor_not_configured"),
        (SessionError("storage_error"), "storage_error"),
        (SessionError("Not A Code SECRET-TEXT"), "unknown_failure"),
        (json.JSONDecodeError("SECRET-TEXT", "doc", 0), "invalid_input_json"),
        (PermissionError("SECRET-TEXT"), "storage_error"),
        (RuntimeError("SECRET-TEXT"), "command_failed"),
    ],
    ids=lambda value: value if isinstance(value, str) else type(value).__name__,
)
def test_compile_raising_prints_the_code_and_returns_to_review(
    tmp_path: Path, exc: BaseException, code: str
) -> None:
    fake_profile(tmp_path, lifecycle="install")
    lines = [f"describe {GOAL}", f"approve {INSTALL}", "show", "quit"]
    exit_code, transcript, session = run_session(
        lines, tmp_path, services_factory=raising_factory(exc)
    )
    assert exit_code == 0
    assert "SECRET" not in transcript
    assert failure(code) in transcript
    assert "compile finished" not in transcript and "Verification (stage 4)" not in transcript
    # REVIEW again: the banner once, the Spec screen on entry, after the failure and on show.
    assert transcript.count("approved by operator  (cancel or Ctrl-C to stop)") == 1
    assert transcript.count(f"Spec {INSTALL}  lifecycle install") == 3
    assert transcript.count(approved_header(INSTALL)) == 2
    assert f"review {INSTALL}> show\n" in transcript and f"review {INSTALL}> quit\n" in transcript
    assert "this compile $0.0000" in transcript.split(f"error: {code}", 1)[1]
    assert session.outcome is None and session.draft is not None
    assert session.draft.hash8 == INSTALL and session.approved(session.draft.spec)
    assert session.state is State.EXIT
    assert Registry(tmp_path / "w" / "registry").list() == ()


# ------------------------------------------------------------------------------ RESULT


def test_result_report_spec_json_install_run_save_and_again(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    spec = make_spec(lifecycle="install")
    edited = spec.model_copy(update={"budget": spec.budget.model_copy(update={"compute_s": 100})})
    token = edited.spec_hash[:8]
    lines = [
        f"describe {GOAL}",
        "set budget.compute_s 100",  # compile_deadline_short: a warning, shown only in REVIEW
        "blockers",
        f"approve {token}",
        "report",
        "spec",
        "json",
        "install",
        "install demo bogus123",
        "install demo",
        "run",
        "run NaN",
        "run [NaN]",
        "run {}",
        "save",
        "again",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("Verification (stage 4)") == 2  # entry and report
    assert transcript.count("licensed: YES (recomputed from disk)") == 2
    assert "Next: install <name> | run {json} | report | again | new" in transcript
    review_screen, rest = transcript.split(f"approved {token} - compiling", 1)
    assert "! compile_deadline_short  warning  fix: budget compile" in review_screen
    assert "- budget.compute_s 60.0\n+ budget.compute_s 100.0\nspec is now " + token in (
        review_screen
    )
    result_spec = rest.split(f"result {token}> spec\n", 1)[1].split(f"result {token}> ", 1)[0]
    assert result_spec.startswith(approved_header(token))
    assert "! compile_deadline_short" not in result_spec and "spec_hash " in result_spec
    assert session.outcome is not None
    compiled = session.outcome.result.spec
    assert compiled.spec_hash == edited.spec_hash
    assert compiled.model_dump_json(indent=2) + "\n" in rest
    assert failure("not_a_command", "install <name> [<hash8>]") in rest
    assert failure("stale_approval_token").rstrip("\n") in rest
    challenge = rest.split(f"result {token}> install demo\n", 1)[1].splitlines()
    assert challenge[0] == (
        f'To install exactly this licensed artifact as "demo", type: install demo {token}'
    )
    assert re.fullmatch(
        r"  evidence   held_out_tasks  score 1\.00 >= limit 1\.00  held-out cases 3  "
        r"id [0-9a-f]{8}",
        challenge[1],
    )
    assert "[SYNTHETIC" not in transcript
    assert "installed demo" not in transcript  # the token was never typed correctly
    assert Registry(tmp_path / "w" / "registry").list() == ()
    assert failure("not_a_command", "run {json} | run <name|dir> {json}") in rest  # `run`
    assert "error: unknown_name" in rest  # `run NaN` names an installed artifact
    assert transcript.count(failure("invalid_input_json")) == 1  # `run [NaN]`
    run_line = r'^run [0-9a-f]{8}  fake  \$\d\.\d{4}  \d+\.\ds  [\d,]+ in / [\d,]+ out\n"ok"\n'
    assert re.search(run_line, rest, re.M)
    assert "artifact consumed" not in transcript
    proposal = tmp_path / "w" / "proposals" / f"{edited.spec_hash}.json"
    assert dense(f"saved {proposal}") in dense(transcript)
    assert proposal.read_text(encoding="utf-8") == compiled.model_dump_json(indent=2) + "\n"
    after_again = rest.split(f"result {token}> again\n", 1)[1]
    assert after_again.startswith(approved_header(token))
    assert "! compile_deadline_short" in after_again
    assert f"review {token}> quit" in after_again
    assert session.draft is not None and session.draft.hash8 == token
    assert session.state is State.EXIT


def test_install_with_a_wrong_token_never_reaches_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = Registry.install

    def install(self: Registry, name: str, artifact: object, **kw: object) -> object:
        calls.append(name)
        return original(self, name, artifact, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Registry, "install", install)
    fake_profile(tmp_path, lifecycle="install")
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        f"install demo {EXPIRE}",
        f"install demo {INSTALL[::-1]}",
        f"install demo {INSTALL} extra",
        "quit",
    ]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count("error: stale_approval_token") == 2
    assert calls == ["demo"]  # only the exact token on screen reaches Registry.install
    assert "installed demo revision 1 -> " in transcript
    assert Registry(tmp_path / "w" / "registry").status("demo").revision == 1


def test_budget_increase_needs_the_flag(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install")
    adapter = PricedAdapter(0.00002)
    spec = make_spec(lifecycle="install")
    dearer = spec.model_copy(update={"budget": spec.budget.model_copy(update={"cost_usd": 0.5})})
    new = dearer.spec_hash[:8]
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        f"install demo {INSTALL}",
        "again",
        "set budget.cost_usd 0.5",
        f"approve {new}",
        f"install demo {new}",
        f"install demo {new} --allow-budget-increase",
        "quit",
    ]

    def cheapen() -> None:
        adapter.cost = 0.00001  # the second compile is cheaper, so it beats the incumbent

    console = HookedConsole(lines, {"installed demo revision 1": cheapen})
    session = Session(
        console,
        cwd=tmp_path,
        workspace=tmp_path / "w",
        clock=FakeClock(),
        poll_s=0.01,
        services_factory=swapping_factory(adapter),
    )
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert "installed demo revision 1 -> " in transcript
    assert f"- budget.cost_usd 0.1\n+ budget.cost_usd 0.5\nspec is now {new}" in transcript
    assert "error: budget_increase\n  " + explain("budget_increase") in transcript
    assert (
        f"next: install demo {new} --allow-budget-increase (only if you accept a higher per-run "
        "budget)"
    ) in unwrap(transcript)
    assert "installed demo revision 2 -> " in transcript
    registry = Registry(tmp_path / "w" / "registry")
    entry = registry.status("demo")
    assert entry.revision == 2 and entry.spec_hash == dearer.spec_hash
    assert registry.get("demo").spec.budget.cost_usd == 0.5


def test_expire_result_is_not_installable_and_runs_once(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [
        f"describe {GOAL}",
        "compile",
        "install demo",
        f"install demo {EXPIRE}",
        "run {not json}",
        "run {}",
        "run {}",
        "report",
        "new",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "Next: run {json} (single use) | again | new" in transcript
    assert transcript.count(failure("not_installable", "run {json}")) == 2
    assert explain("not_installable") == "Expire artifacts are single-use and cannot be installed."
    # A malformed input never consumes the single attempt.
    bad, rest = transcript.split(f"result {EXPIRE}> run {{not json}}\n", 1)
    assert "this artifact expires" not in bad
    assert rest.startswith("error: invalid_input_json\n")
    first, second = rest.split(f"result {EXPIRE}> run {{}}\n")[1:3]
    assert first.startswith("this artifact expires after this one attempt, even on failure\n")
    assert '\n"ok"\nartifact consumed (single use)\n' in first
    assert second.startswith(failure("artifact_expired"))
    assert "this artifact expires" not in second
    assert transcript.count("Verification (stage 4)") == 2
    assert Registry(tmp_path / "w" / "registry").list() == ()
    candidates = [
        p for p in (tmp_path / "w" / "artifacts").glob(f"{make_spec().spec_hash}/*") if p.is_dir()
    ]
    assert candidates and any((d / ".consumed").exists() for d in candidates)
    assert transcript.count(READY_LINE) == 2  # boot and after new
    assert session.draft is None and session.outcome is None and session.state is State.EXIT
    assert all(d.is_dir() for d in candidates)  # new leaves artifact directories untouched


def test_unlicensed_result_refuses_install_and_run(tmp_path: Path) -> None:
    fake_profile(tmp_path, lifecycle="install", held_out_expected="bad")
    lines = [
        f"describe {GOAL}",
        f"approve {INSTALL}",
        "install demo",
        f"install demo {INSTALL}",
        "run {}",
        "status",
        "discard",
        "quit",
    ]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert "licensed: NO (recomputed from disk)" in transcript
    assert transcript.count(failure("unlicensed_artifact", "again")) == 3
    assert "this artifact expires" not in transcript and '"ok"' not in transcript
    assert f"outcome          {INSTALL}  licensed no" in transcript
    assert transcript.count(READY_LINE) == 2
    assert session.draft is None and session.outcome is None
    assert Registry(tmp_path / "w" / "registry").list() == ()


def test_install_challenge_repeats_the_synthetic_label(tmp_path: Path) -> None:
    fake_profile(
        tmp_path,
        lifecycle="install",
        case_metadata={"synthetic": True, "origin": "model_draft", "drafted_by_model": "fake"},
    )
    lines = [f"describe {GOAL}", f"approve {INSTALL}", "install demo", "quit"]
    code, transcript, _ = run_session(lines, tmp_path)
    assert code == 0
    challenge = transcript.split(f"result {INSTALL}> install demo\n", 1)[1]
    label = "[SYNTHETIC: 6 of 6 cases model-drafted by fake, accepted by operator; not production "
    label += "evidence]"
    assert re.match(
        r'To install exactly this licensed artifact as "demo", type: install demo [0-9a-f]{8}\n'
        r"  evidence   held_out_tasks  score 1\.00 >= limit 1\.00  held-out cases 3  "
        r"id [0-9a-f]{8}\n {13}" + r"\s+".join(re.escape(word) for word in label.split()) + r"\n",
        challenge,
    )
    assert "installed demo" not in transcript
    assert Registry(tmp_path / "w" / "registry").list() == ()


def test_result_with_no_artifact_at_all_refuses_run_and_install(tmp_path: Path) -> None:
    """A floor file that breaks after boot ends the compile without an artifact."""
    fake_profile(tmp_path)
    cases = tmp_path / "w" / "cases.json"

    def corrupt() -> None:
        cases.write_text("not json", encoding="utf-8")

    lines = [f"describe {GOAL}", "compile", "run {}", "install demo", "report", "new", "quit"]
    console = HookedConsole(lines, {"Next: compile |": corrupt})
    session = Session(
        console, cwd=tmp_path, workspace=tmp_path / "w", clock=FakeClock(), poll_s=0.01
    )
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert "compile finished in 0.0s: 0 calls, $0.0000, stages -" in transcript
    screen = transcript.split("Verification (stage 4)", 1)[1]
    assert screen.startswith("  id -  stages -  licensed: NO (recomputed from disk)\n")
    assert "  artifact   " not in screen  # no artifact line: nothing was produced
    assert "  floor      not verified (no report)\n  failures\n  floor_unavailable" in screen
    assert f"{explain('floor_unavailable')}   next: floor" in unwrap(screen)
    assert "Next: again (edit and recompile) | report | new" in screen
    assert transcript.count(failure("unlicensed_artifact", "again")) == 2  # run, install
    assert transcript.count("Verification (stage 4)") == 2
    artifacts = tmp_path / "w" / "artifacts"
    assert not artifacts.exists() or not any(artifacts.iterdir())
    assert transcript.count(READY_LINE) == 2 and session.state is State.EXIT


def test_result_without_a_usable_artifact_refuses_run(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "compile", "run {}", "install demo", "quit"]
    code, transcript, session = run_session(
        lines, tmp_path, services_factory=swapping_factory(SecretFakeAdapter())
    )
    assert code == 0 and "sk-secret" not in transcript
    assert "licensed: NO (recomputed from disk)" in transcript
    assert "Next: again (edit and recompile) | report | new" in transcript
    assert transcript.count(failure("unlicensed_artifact", "again")) == 2
    assert session.outcome is not None and not session.outcome.licensed


def test_result_prose_and_json_lines_are_not_commands(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    lines = [f"describe {GOAL}", "compile", "please install it", '{"task": 1}', "[1]", "quit"]
    code, transcript, session = run_session(lines, tmp_path)
    assert code == 0
    assert transcript.count(failure("not_a_command", "help")) == 3
    assert "allowed here:" not in transcript  # prose gets the hint, not the verb list
    assert session.state is State.EXIT and session.outcome is not None


# ---------------------------------------------------------------------- EXIT / close


@pytest.mark.parametrize("verb", ["quit", "exit", "EXIT"])
def test_exit_prints_footer_then_bye_and_close_is_idempotent(tmp_path: Path, verb: str) -> None:
    fake_profile(tmp_path)
    session, console = _session([f"describe {GOAL}", "compile", "report", verb], tmp_path)
    assert asyncio.run(session.run()) == 0
    transcript = console.transcript()
    assert session.state is State.EXIT
    calls = session.session_spend.calls
    assert calls > 1
    assert re.search(
        rf"result {EXPIRE}> {verb}\nspend  session \$\d\.\d{{4}} \| this compile \$\d\.\d{{4}} \| "
        rf"calls {calls} \| wall 0m00s \| API-equivalent, not a bill\nbye\n$",
        transcript,
    )
    assert session.services is not None
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()  # closed by run()
    asyncio.run(session.close())  # a second and third close change nothing and raise nothing
    asyncio.run(session.close())
    assert console.transcript() == transcript


def test_close_before_boot_and_close_cancels_in_flight_work(tmp_path: Path) -> None:
    fake_profile(tmp_path)
    fresh, _ = _session([], tmp_path)
    asyncio.run(fresh.close())  # no services yet: nothing to close
    assert fresh.services is None and fresh.state is State.BOOT

    adapter = BlockingFakeAdapter()

    async def scenario() -> tuple[int, Session, GatedConsole]:
        console = GatedConsole(
            [(f"describe {GOAL}", State.READY), ("compile", State.REVIEW)], eof_at=State.REVIEW
        )
        session = Session(
            console,
            cwd=tmp_path,
            workspace=tmp_path / "w",
            clock=FakeClock(),
            poll_s=0.01,
            services_factory=swapping_factory(adapter),
        )
        console.session = session
        task = asyncio.create_task(session.run())
        await asyncio.wait_for(adapter.started.wait(), timeout=30)
        assert session.state is State.COMPILING
        await session.close()  # cancels the compile and closes the services once
        return await task, session, console

    code, session, console = asyncio.run(scenario())
    transcript = console.transcript()
    assert code == 0
    assert "cancelled after $0.0000" in transcript and transcript.rstrip().endswith("bye")
    assert session.outcome is None and session.state is State.EXIT
    assert session.services is not None
    with pytest.raises(ValueError):
        session.services.runtime.store.rows()
    assert Registry(tmp_path / "w" / "registry").list() == ()
