"""Cost controls on the Pi parent side, for both transports (W3b).

- A reply cut at the output limit is re-asked once with a short instruction, never echoed,
  and none of its calls runs.
- ``final_requires``: a final answer is re-asked once before the required capability was
  ever called, then once after each of its calls while it has calls left.
- A refused ``read`` of a missing file or a directory says so, with what is there.
- ``context_cap="parent"``: old tool-result bodies are dropped, and old write and edit texts
  and the bulky arguments of Forge's dry runs and spawns trimmed, oldest first. The
  extension applies the parent's rule before it sends a context, so the wire stays small,
  and the parent applies it again to what arrives.

Offline: scripted adapters, the real pinned Pi SDK, or an inert protocol peer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_pi import _READY, ProtocolProcess, install_protocol_peer
from test_pi_native import Streaming, call, context, malformed, pack, paired, reply

from architect.backends.pi import (
    _CUT_CORRECTION,
    _CUT_REPLY,
    _DROPPED_RESULT,
    _NOTE_REFUSED,
    _REPAIR_MESSAGE,
    _TRIMMED_ARGUMENT,
    _TRIMMED_WRITE,
    _UNFINISHED,
    _UNFINISHED_AGAIN,
    _UNFINISHED_AGAIN_CORRECTION,
    _UNFINISHED_CORRECTION,
    PARENT_TEXTS,
    PiBackend,
    PiConfig,
    PiError,
    TrustedTool,
    _carries_note,
    _ContextCap,
    _read_refusal,
)
from architect.models import FakeAdapter, ModelRequest, ModelResponse
from architect.models.chat import ChatMessage
from architect.runtime import RunContext
from architect.spec import Budget

SUBMIT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}


def submit_tool(seen: list[Any], max_calls: int = 3) -> TrustedTool:
    async def submit(arguments: dict[str, Any]) -> Any:
        seen.append(arguments)
        return {"ok": True, "code": None}

    return TrustedTool(
        name="forge_submit",
        description="Submit the draft.",
        parameters=SUBMIT_SCHEMA,
        callback=submit,
        max_calls=max_calls,
    )


def submitting_backend(seen: list[Any], max_calls: int = 3, **config: Any) -> PiBackend:
    settings = PiConfig(
        allowed_tools=("read", "write", "forge_submit"), final_requires="forge_submit", **config
    )
    return PiBackend(settings, capabilities={"forge_submit": submit_tool(seen, max_calls)})


class Envelope(FakeAdapter):
    """A JSON-envelope adapter: scripted replies with their own output token counts."""

    def __init__(self, *replies: tuple[str, int]) -> None:
        super().__init__(
            responses=[
                ModelResponse(text=text, model="fake", tokens_in=10, tokens_out=out, cost_usd=0)
                for text, out in replies
            ]
        )
        self.prompts: list[dict[str, Any]] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(json.loads(request.prompt))
        return await super().complete(request)


def envelope(**call_: Any) -> str:
    return json.dumps({"tool_calls": [{"id": "c1", **call_}]})


# -- a reply cut at the output limit ----------------------------------------------------


async def test_native_cut_text_is_re_asked_with_an_instruction_only(tmp_path: Path) -> None:
    adapter = Streaming(
        reply(text="I will now write the whole file " + "x" * 400, stop="max_tokens"),
        reply(call("write", "w1", path="a.txt", content="a")),
        reply(text="Done."),
    )
    backend = PiBackend()
    assert await backend.run(context(tmp_path, adapter), {}, pack("write")) == "Done."
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a"
    _, first, _, _ = adapter.sent[0]
    _, again, _, _ = adapter.sent[1]
    assert again == (*first, ChatMessage("user", _CUT_REPLY))
    assert all("x" * 40 not in message.text for message in again)
    evidence = backend.last_evidence
    assert evidence is not None and (evidence.model_calls, evidence.repairs) == (3, 1)


async def test_native_cut_with_complete_calls_runs_none_of_them(tmp_path: Path) -> None:
    # A cut call can arrive complete: a server ends a reply by sending the rest of its
    # tool parser's partial parse. So no call of a cut reply runs; it is re-asked.
    adapter = Streaming(
        reply(call("write", "w1", path="a.txt", content="a"), stop="max_tokens"),
        reply(text="Done."),
    )
    backend = PiBackend()
    assert await backend.run(context(tmp_path, adapter), {}, pack("write")) == "Done."
    assert not (tmp_path / "a.txt").exists()
    _, first, _, _ = adapter.sent[0]
    _, again, _, _ = adapter.sent[1]
    assert again == (*first, ChatMessage("user", _CUT_REPLY))
    assert backend.last_evidence is not None and backend.last_evidence.repairs == 1


async def test_native_cut_inside_arguments_never_echoes_the_reply(tmp_path: Path) -> None:
    adapter = Streaming(
        reply(call("read", "r1", path="a.txt"), malformed("write", "w1"), text="Writing.",
              stop="max_tokens"),
        reply(call("write", "w2", path="b.txt", content="b")),
        reply(text="Done."),
    )
    backend = PiBackend()
    assert await backend.run(context(tmp_path, adapter), {}, pack("read", "write")) == "Done."
    _, first, _, _ = adapter.sent[0]
    _, again, _, _ = adapter.sent[1]
    assert again == (*first, ChatMessage("user", _CUT_REPLY))  # no narration, no calls
    _, after, _, _ = adapter.sent[2]
    assert paired(after) and [m.role for m in after] == ["user", "assistant", "tool"]


async def test_envelope_cut_reply_is_never_echoed(tmp_path: Path) -> None:
    cap = 256
    adapter = Envelope(
        ('{"tool_calls":[{"id":"c1","name":"write","arguments":{"content":"' + "y" * 300, cap),
        ("not json either", 12),
        (envelope(name="write", arguments={"path": "a.txt", "content": "a"}), 12),
        ('{"text": "done"}', 12),
    )
    ctx = RunContext(adapter, Budget(cost_usd=1, compute_s=60), repository=tmp_path)
    backend = PiBackend(PiConfig(repair_prose=True))
    bound = {**pack("write"), "max_output_tokens": cap}
    assert await backend.run(ctx, {}, bound) == "done"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a"
    first, cut, other, _ = adapter.prompts
    assert "correction" not in first
    # Used the whole cap: dropped, re-asked with a short instruction, never echoed.
    assert cut["correction"] == _CUT_CORRECTION and "previous_reply" not in cut
    assert "y" * 40 not in json.dumps(cut)
    # A malformed reply under the cap is still shown back, bounded, as before.
    assert other["previous_reply"] == "not json either"
    assert other["correction"] == _REPAIR_MESSAGE
    evidence = backend.last_evidence
    assert evidence is not None and evidence.repairs == 2


# -- a final answer before the required capability ---------------------------------------


async def test_native_final_text_before_any_submission_is_re_asked_once(tmp_path: Path) -> None:
    seen: list[Any] = []
    adapter = Streaming(
        reply(text="Still working on the harness."),
        reply(call("forge_submit", "s1")),
        reply(text="Submitted."),
    )
    backend = submitting_backend(seen, max_calls=1)  # none left after it: no second re-ask
    ctx = context(tmp_path, adapter, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, pack("read", "forge_submit")) == "Submitted."
    assert seen == [{}]
    _, first, _, _ = adapter.sent[0]
    _, nudged, _, _ = adapter.sent[1]
    assert nudged == (
        *first,
        ChatMessage("assistant", "Still working on the harness."),
        ChatMessage("user", _UNFINISHED.format(tool="forge_submit")),
    )
    # Pi's own context never holds the re-asked final answer.
    _, after, _, _ = adapter.sent[2]
    assert paired(after) and [m.role for m in after] == ["user", "assistant", "tool"]
    evidence = backend.last_evidence
    assert evidence is not None and (evidence.model_calls, evidence.repairs) == (3, 1)


async def test_the_final_text_re_ask_happens_once_per_session(tmp_path: Path) -> None:
    seen: list[Any] = []
    adapter = Streaming(reply(text="A progress note."), reply(text="Another note."))
    backend = submitting_backend(seen)
    ctx = context(tmp_path, adapter, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, pack("read", "forge_submit")) == "Another note."
    assert seen == [] and len(adapter.sent) == 2


async def test_after_each_submission_a_final_answer_is_re_asked_once(tmp_path: Path) -> None:
    seen: list[Any] = []
    adapter = Streaming(
        reply(call("forge_submit", "s1")),
        reply(text="Let me simplify the harness further."),  # re-asked: submissions remain
        reply(call("forge_submit", "s2")),
        reply(text="Done."),  # re-asked once more after the second submission
        reply(text="Done, the second one is better."),
    )
    backend = submitting_backend(seen)
    ctx = context(tmp_path, adapter, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, pack("forge_submit")) == "Done, the second one is better."
    assert seen == [{}, {}] and len(adapter.sent) == 5
    _, before, _, _ = adapter.sent[1]
    _, nudged, _, _ = adapter.sent[2]
    assert nudged == (
        *before,
        ChatMessage("assistant", "Let me simplify the harness further."),
        ChatMessage("user", _UNFINISHED_AGAIN.format(tool="forge_submit")),
    )
    _, last, _, _ = adapter.sent[4]
    assert last[-1] == ChatMessage("user", _UNFINISHED_AGAIN.format(tool="forge_submit"))
    assert backend.last_evidence is not None and backend.last_evidence.repairs == 2


async def test_no_re_ask_once_no_call_remains_or_when_it_is_not_granted(tmp_path: Path) -> None:
    seen: list[Any] = []
    adapter = Streaming(reply(call("forge_submit", "s1")), reply(text="Done."))
    ctx = context(tmp_path, adapter, is_evaluation=False, is_build=True)
    backend = submitting_backend(seen, max_calls=1)
    assert await backend.run(ctx, {}, pack("forge_submit")) == "Done."
    assert len(adapter.sent) == 2
    # Outside a build the capability is not granted, so nothing is required of the model.
    plain = Streaming(reply(text="Nothing to build."))
    assert await submitting_backend(seen).run(
        context(tmp_path, plain), {}, pack("read", "forge_submit")
    ) == "Nothing to build."
    assert len(plain.sent) == 1
    # And without ``final_requires`` a final answer ends the session at once.
    bare = Streaming(reply(text="Done early."))
    backend = PiBackend(
        PiConfig(allowed_tools=("read", "forge_submit")),
        capabilities={"forge_submit": submit_tool(seen)},
    )
    ctx = context(tmp_path, bare, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, pack("read", "forge_submit")) == "Done early."


async def test_envelope_final_text_before_any_submission_is_re_asked_once(
    tmp_path: Path,
) -> None:
    seen: list[Any] = []
    adapter = Envelope(
        ('{"text": "I am still encountering issues with the sandbox."}', 12),
        (envelope(name="forge_submit", arguments={}), 12),
        ('{"text": "Submitted."}', 12),
    )
    ctx = RunContext(
        adapter,
        Budget(cost_usd=1, compute_s=60),
        repository=tmp_path,
        is_evaluation=False,
        is_build=True,
    )
    backend = submitting_backend(seen, max_calls=1, repair_prose=True)
    assert await backend.run(ctx, {}, pack("read", "forge_submit")) == "Submitted."
    assert seen == [{}]
    first, nudged, _ = adapter.prompts
    assert "correction" not in first
    assert nudged["correction"] == _UNFINISHED_CORRECTION.replace("{tool}", "forge_submit")
    assert "previous_reply" not in nudged and nudged["pi_context"] == first["pi_context"]
    assert backend.last_evidence is not None and backend.last_evidence.repairs == 1


async def test_envelope_final_text_after_a_submission_is_re_asked_once(tmp_path: Path) -> None:
    seen: list[Any] = []
    adapter = Envelope(
        (envelope(name="forge_submit", arguments={}), 12),
        ('{"text": "Let me simplify the harness further."}', 12),
        ('{"text": "Done."}', 12),
    )
    ctx = RunContext(
        adapter,
        Budget(cost_usd=1, compute_s=60),
        repository=tmp_path,
        is_evaluation=False,
        is_build=True,
    )
    backend = submitting_backend(seen, repair_prose=True)
    assert await backend.run(ctx, {}, pack("read", "forge_submit")) == "Done."
    _, second, nudged = adapter.prompts
    assert "correction" not in second
    assert nudged["correction"] == _UNFINISHED_AGAIN_CORRECTION.replace("{tool}", "forge_submit")
    assert '{"text":"<final answer>"}' in nudged["correction"]  # finishing stays allowed
    assert "previous_reply" not in nudged and nudged["pi_context"] == second["pi_context"]
    assert backend.last_evidence is not None and backend.last_evidence.repairs == 1


def test_final_requires_must_name_a_capability() -> None:
    with pytest.raises(ValueError):
        PiConfig(final_requires="Not A Tool")
    assert PiConfig(final_requires="forge_submit").final_requires == "forge_submit"


# -- a refused read says what is there ----------------------------------------------------


def workspace(root: Path) -> Path:
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "extract_verify.py").write_text("x", encoding="utf-8")
    (root / "templates" / "prompt_chain.py").write_text("x", encoding="utf-8")
    (root / "templates" / "nested").mkdir()
    (root / "harness.py").write_text("x", encoding="utf-8")
    (root / ".architect").mkdir()
    (root / "private").mkdir()
    (root / "private" / "labels.json").write_text("{}", encoding="utf-8")
    return root.resolve()  # as PiBackend resolves its repository


def test_a_missing_file_lists_its_nearest_directory(tmp_path: Path) -> None:
    root = workspace(tmp_path / "ws")
    denied = (root / "private",)
    assert _read_refusal(root, "templates/extractor.py", denied) == "\n".join(
        [
            "Not found: templates/extractor.py",
            "templates/ holds:",
            "templates/extract_verify.py",
            "templates/nested/",
            "templates/prompt_chain.py",
        ]
    )
    text = _read_refusal(root, "nowhere/deeper/file.py", denied)
    assert text is not None and text.startswith("Not found: nowhere/deeper/file.py\n")
    assert "The working directory holds:" in text and "harness.py" in text
    # Never a denied path or the control-plane directory.
    assert "private" not in text and ".architect" not in text


def test_a_directory_lists_its_entries(tmp_path: Path) -> None:
    root = workspace(tmp_path / "ws")
    assert _read_refusal(root, "templates", ()) == "\n".join(
        [
            "templates/ is a directory, not a file.",
            "templates/ holds:",
            "templates/extract_verify.py",
            "templates/nested/",
            "templates/prompt_chain.py",
        ]
    )
    root_text = _read_refusal(root, ".", ())
    assert root_text is not None
    assert root_text.startswith("That is the working directory, not a file.\n")
    assert "templates/" in root_text and ".architect" not in root_text
    assert _read_refusal(root, "templates/nested", ()) == (
        "templates/nested/ is a directory, not a file.\ntemplates/nested/ is empty."
    )


def test_a_large_directory_listing_is_bounded(tmp_path: Path) -> None:
    root = (tmp_path / "ws").resolve()
    (root / "many").mkdir(parents=True)
    for index in range(70):
        (root / "many" / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
    text = _read_refusal(root, "many", ())
    assert text is not None and text.endswith("... and 6 more")
    assert text.count("many/f") == 64
    # A huge directory is not walked whole: past 512 entries there is no count.
    (root / "huge").mkdir()
    for index in range(520):
        (root / "huge" / f"g{index:03d}.txt").write_text("x", encoding="utf-8")
    text = _read_refusal(root, "huge", ())
    assert text is not None and text.endswith("huge/g063.txt\n... and more")


@pytest.mark.parametrize(
    "raw",
    [
        "../outside.txt",
        "private/labels.json",
        "private/missing.json",
        "private",
        ".architect",
        ".architect/signing.key",
        "harness.py",  # an ordinary file: another guard refused it, the policy text stays
        "~/x",
        "",
        None,
    ],
)
def test_other_refusals_keep_the_policy_text(tmp_path: Path, raw: object) -> None:
    root = workspace(tmp_path / "ws")
    assert _read_refusal(root, raw, (root / "private",)) is None


async def test_the_model_sees_the_listing_through_the_real_sdk(tmp_path: Path) -> None:
    root = workspace(tmp_path / "ws")
    adapter = Streaming(
        reply(call("read", "r1", path="templates/extractor.py")),
        reply(call("read", "r2", path="templates")),
        reply(call("read", "r3", path="private/labels.json")),
        reply(text="Done."),
    )
    backend = PiBackend(PiConfig(denied_paths=(root / "private",)))
    assert await backend.run(context(root, adapter), {}, pack("read")) == "Done."
    _, last, _, _ = adapter.sent[3]
    missing, directory, private = (m for m in last if m.role == "tool")
    assert missing.is_error and missing.text.startswith("Not found: templates/extractor.py\n")
    assert "templates/extract_verify.py" in missing.text
    assert directory.text.startswith("templates/ is a directory, not a file.\n")
    assert private.text == "Tool denied by Architect policy"


# -- the parent-side context cap ------------------------------------------------------------


def result(call_id: str, text: str) -> dict[str, Any]:
    return {"role": "toolResult", "toolCallId": call_id, "toolName": "read",
            "content": [{"type": "text", "text": text}], "isError": False}


def assistant(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": list(parts)}


def tool_call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def conversation(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"systemPrompt": "sys", "messages": [{"role": "user", "content": "task"}, *messages]}


def test_under_the_limit_the_context_is_untouched() -> None:
    cap = _ContextCap(10_000)
    value = conversation(assistant(tool_call("a", "read", path="x")), result("a", "A" * 500))
    assert cap.apply(value) is value
    assert cap.apply(None) is None and cap.apply({"messages": "x"}) == {"messages": "x"}


def test_oldest_results_and_writes_go_first_down_to_three_quarters() -> None:
    cap = _ContextCap(2000)
    big = "W" * 600
    value = conversation(
        assistant(tool_call("a", "read", path="LANGUAGE.md")),        # 1
        result("a", "L" * 700),                                        # 2: oldest result
        assistant(tool_call("b", "write", path="harness.py", content=big)),  # 3
        result("b", "Successfully wrote 600 bytes"),                   # 4: short, kept
        assistant(tool_call("c", "read", path="TUNING.md")),           # 5
        result("c", "T" * 700),                                        # 6
        assistant(tool_call("d", "write", path="harness.py", content="V" * 300)),  # 7: latest
        result("d", "Successfully wrote 300 bytes"),                   # 8
    )
    thinned = cap.apply(value)
    assert isinstance(thinned, dict) and thinned is not value
    messages = thinned["messages"]
    # 700 + 600 + 700 + 300 (+ short results) > 2000: oldest first until <= 1500.
    assert messages[2]["content"] == [{"type": "text", "text": _DROPPED_RESULT}]
    note = _TRIMMED_WRITE.format(chars=600)
    assert messages[3]["content"][0]["arguments"] == {"path": "harness.py", "content": note}
    assert messages[6]["content"][0]["text"] == "T" * 700  # back under the floor: kept
    assert messages[4] == value["messages"][4]
    # The latest assistant turn and its result are never touched.
    assert messages[7] == value["messages"][7] and messages[8] == value["messages"][8]
    # The input is never modified, and the same context thins the same way again.
    assert value["messages"][2]["content"][0]["text"] == "L" * 700
    assert cap.apply(value) == thinned


def test_old_edit_texts_are_trimmed_like_write_contents() -> None:
    cap = _ContextCap(2400)
    pairs = [{"oldText": "o" * 500, "newText": "n" * 500}, {"oldText": "x", "newText": "y"}]
    value = conversation(
        assistant(tool_call("a", "edit", path="h.py", edits=pairs)),            # 1: a list
        result("a", "ok"),
        assistant(tool_call("b", "edit", path="h.py", edits="E" * 600)),         # 3: a string
        result("b", "ok"),
        assistant(tool_call("c", "edit", path="h.py", oldText="L" * 300, newText="M" * 300)),
        result("c", "ok"),                                                        # 5: legacy
        assistant(tool_call("d", "read", path="h.py")),                           # 7: latest
        result("d", "R" * 200),
    )
    note = _TRIMMED_WRITE.format
    # 2,408 characters > 2,400: the oldest texts go first until the count is <= 1,800.
    thinned = cap.apply(value)
    assert isinstance(thinned, dict)
    messages = thinned["messages"]
    assert messages[1]["content"][0]["arguments"]["edits"] == [
        {"oldText": note(chars=500), "newText": note(chars=500)},
        {"oldText": "x", "newText": "y"},  # no longer than its note: left alone
    ]
    assert messages[3:] == value["messages"][3:]
    # The context grows past the limit again: string edits, legacy pairs and old results
    # go next, in message order; an earlier removal holds.
    grown = conversation(
        *value["messages"][1:], assistant(tool_call("e", "read", path="big")),
        result("e", "Z" * 1500),
    )
    again = cap.apply(grown)
    assert isinstance(again, dict)
    later = again["messages"]
    assert later[1] == messages[1]
    assert later[3]["content"][0]["arguments"] == {"path": "h.py", "edits": note(chars=600)}
    assert later[5]["content"][0]["arguments"] == {
        "path": "h.py", "oldText": note(chars=300), "newText": note(chars=300),
    }
    assert later[8]["content"] == [{"type": "text", "text": _DROPPED_RESULT}]
    assert later[10] == grown["messages"][10]  # the latest result stays whole
    # The input is never changed in place.
    assert value["messages"][1]["content"][0]["arguments"]["edits"] == pairs


def test_removals_hold_so_the_prefix_stays_stable() -> None:
    cap = _ContextCap(2000)
    head = [
        assistant(tool_call("a", "read", path="x")), result("a", "A" * 1200),
        assistant(tool_call("b", "read", path="y")), result("b", "B" * 900),
    ]
    first = cap.apply(conversation(*head))
    assert isinstance(first, dict)
    assert first["messages"][2]["content"][0]["text"] == _DROPPED_RESULT
    # A small next turn removes nothing new: everything before it is byte-identical.
    grown = [*head, assistant(tool_call("c", "read", path="z")), result("c", "C" * 100)]
    second = cap.apply(conversation(*grown))
    assert isinstance(second, dict)
    assert second["messages"][: len(first["messages"])] == first["messages"]


def test_old_forge_call_arguments_are_trimmed_whole() -> None:
    cap = _ContextCap(2000)
    replies = ["y" * 300, "z" * 300]
    cases = {"authored": [{"input": {"text": "i" * 200}, "expected": {"n": 1}}] * 2}
    spawn = {
        "name": "child", "goal": "g" * 400, "input_schema": {"type": "object"},
        "output_schema": {"type": "object"}, "cases": cases,
    }
    value = conversation(
        assistant(tool_call("a", "forge_dryrun", case=0, replies=replies)),          # 1
        result("a", "ok"),
        assistant({"type": "toolCall", "id": "b", "name": "forge_spawn", "arguments": spawn}),
        result("b", "ok"),                                                           # 4
        assistant(tool_call("c", "forge_dryrun", case=1, replies=["w" * 500])),      # 5
        result("c", "ok"),
        assistant(tool_call("d", "read", path="h.py")),                              # 7: latest
        result("d", "R" * 100),
    )
    thinned = cap.apply(value)
    assert isinstance(thinned, dict)
    messages = thinned["messages"]
    note = _TRIMMED_ARGUMENT.format
    size = len(json.dumps(replies, separators=(",", ":")))
    # 607 + 400 + 17 + 17 + 496 + 504 + 100 (+ short results) = 2,147 > 2,000: oldest
    # first, each value whole (a list or object by its compact JSON), until the count is
    # <= 1,500. Values no longer than their note (the schemas) are left alone.
    assert messages[1]["content"][0]["arguments"] == {"case": 0, "replies": note(chars=size)}
    spawned = messages[3]["content"][0]["arguments"]
    assert spawned["goal"] == note(chars=400) and spawned["input_schema"] == {"type": "object"}
    assert spawned["cases"] == cases  # back under the floor: kept
    assert messages[5:] == value["messages"][5:]
    assert value["messages"][1]["content"][0]["arguments"]["replies"] == replies  # unchanged
    assert cap.apply(value) == thinned


async def test_the_parent_cap_replaces_the_bridge_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi_context = {
        "systemPrompt": "sys",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": '{"task":"t"}'}]},
            assistant(tool_call("a", "read", path="a.txt")),
            result("a", "A" * 900),
            assistant(tool_call("b", "read", path="b.txt")),
            result("b", "B" * 900),
        ],
        "tools": [{"name": "read", "description": "Read", "parameters": {"type": "object"}}],
    }
    peer = ProtocolProcess([_READY, {"kind": "model", "id": 1, "context": pi_context}])
    install_protocol_peer(monkeypatch, peer)
    adapter = Streaming(reply(text="Done."))
    bound = {**pack("read"), "context_policy": {"max_characters": 1000}}
    with pytest.raises(PiError, match="Invalid Pi protocol"):  # the peer's stream ends
        await PiBackend(PiConfig(context_cap="parent")).run(context(tmp_path, adapter), {}, bound)
    init = peer.sent[0]
    assert init["kind"] == "init" and "context_policy" not in init["pack"]
    assert "final_requires" not in init
    # The extension gets the parent's rule in place of the policy: same numbers, same notes.
    assert init["context_cap"] == {
        "max_characters": 1000, "floor": 750, "dropped": _DROPPED_RESULT,
        "trimmed": _TRIMMED_WRITE, "trimmed_argument": _TRIMMED_ARGUMENT,
    }
    [(_, messages, _, _)] = adapter.sent
    tools = [m for m in messages if m.role == "tool"]
    assert [m.text for m in tools] == [_DROPPED_RESULT, "B" * 900]


async def test_by_default_the_bridge_keeps_its_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ProtocolProcess([_READY])
    install_protocol_peer(monkeypatch, peer)
    with pytest.raises(PiError):
        await PiBackend().run(context(tmp_path, Streaming()), {}, pack("read"))
    assert peer.sent[0]["pack"]["context_policy"] == {"max_characters": 60000}
    assert "context_cap" not in peer.sent[0]  # the init message is as it always was


async def test_the_parent_cap_through_the_real_sdk(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("A" * 900, encoding="utf-8")
    (tmp_path / "b.txt").write_text("B" * 900, encoding="utf-8")
    adapter = Streaming(
        reply(call("read", "r1", path="a.txt")),
        reply(call("read", "r2", path="b.txt")),
        reply(text="Done."),
    )
    backend = PiBackend(PiConfig(context_cap="parent"))
    bound = {**pack("read"), "context_policy": {"max_characters": 1000}}
    assert await backend.run(context(tmp_path, adapter), {}, bound) == "Done."
    _, last, _, _ = adapter.sent[2]
    first, second = (m for m in last if m.role == "tool")
    # Dropped whole by the parent; the bridge did not slice it first.
    assert first.text == _DROPPED_RESULT and second.text.count("B") == 900


def numbered_lines(count: int, prefix: str) -> str:
    return "".join(f"{prefix} line {index:04d}\n" for index in range(count))


def editing_session() -> Streaming:
    old = numbered_lines(60, "a")[200:800]
    return Streaming(
        reply(call("write", "w1", path="a.txt", content=numbered_lines(60, "a"))),
        reply(call("read", "r1", path="a.txt")),
        reply(call("edit", "e1", path="a.txt", edits=[{"oldText": old, "newText": "B" * 500}])),
        reply(call("read", "r2", path="a.txt")),
        reply(call("write", "w2", path="c.txt", content=numbered_lines(50, "c"))),
        reply(call("edit", "e2", path="c.txt", oldText="c line 0007\n", newText="C" * 400)),
        reply(call("read", "r3", path="c.txt")),
        reply(text="Done."),
    )


async def test_the_extension_thins_the_wire_exactly_as_the_parent_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extension's copy of the rule and ``_ContextCap`` agree: what the model sees is
    the same whether the bridge thinned the context first or the parent did it alone."""
    bound = {**pack("read", "write", "edit"), "context_policy": {"max_characters": 1800}}
    changed: list[bool] = []
    sizes: list[int] = []
    real_apply = _ContextCap.apply

    def spy(self: _ContextCap, pi_context: object) -> object:
        thinned = real_apply(self, pi_context)
        changed.append(thinned is not pi_context)
        sizes.append(len(json.dumps(pi_context)))  # what crossed the wire
        return thinned

    monkeypatch.setattr(_ContextCap, "apply", spy)
    both = editing_session()
    (tmp_path / "both").mkdir()
    backend = PiBackend(PiConfig(context_cap="parent"))
    assert await backend.run(context(tmp_path / "both", both), {}, bound) == "Done."
    # The bridge sent contexts already under the cap: the parent never changed one.
    assert len(changed) == 8 and not any(changed)
    wire = list(sizes)
    # The same session with the extension's copy switched off: the parent thins alone.
    monkeypatch.setattr(
        _ContextCap, "bridge_rule", lambda self: {"max_characters": 0, "floor": 0}
    )
    changed.clear()
    sizes.clear()
    alone = editing_session()
    (tmp_path / "alone").mkdir()
    backend = PiBackend(PiConfig(context_cap="parent"))
    assert await backend.run(context(tmp_path / "alone", alone), {}, bound) == "Done."
    assert any(changed)  # here the parent did the thinning
    assert [messages for _, messages, _, _ in both.sent] == [
        messages for _, messages, _, _ in alone.sent
    ]
    seen = "\n".join(
        message.text + json.dumps([item.arguments for item in message.tool_calls])
        for message in both.sent[-1][1]
    )
    assert _DROPPED_RESULT in seen and _TRIMMED_WRITE.format(chars=500) in seen
    # And the wire stayed smaller than it is without the extension's copy of the rule.
    assert max(wire) < max(sizes)


DRYRUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["case"],
    "properties": {
        "case": {"type": "integer", "minimum": 0},
        "replies": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
    },
}


def dryrun_session() -> tuple[Streaming, PiBackend]:
    async def dryrun(arguments: dict[str, Any]) -> Any:
        return {"ok": True, "case": arguments["case"]}

    tool = TrustedTool(
        name="forge_dryrun",
        description="Run the draft on one case with scripted replies.",
        parameters=DRYRUN_SCHEMA,
        callback=dryrun,
        max_calls=8,
    )
    adapter = Streaming(
        *(
            reply(call("forge_dryrun", f"d{index}", case=index, replies=[letter * 400] * 2))
            for index, letter in enumerate("abcde")
        ),
        reply(call("read", "r1", path="a.txt")),
        reply(text="Done."),
    )
    backend = PiBackend(
        PiConfig(context_cap="parent", allowed_tools=("read", "forge_dryrun")),
        capabilities={"forge_dryrun": tool},
    )
    return adapter, backend


async def test_the_extension_trims_forge_arguments_as_the_parent_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extension's copy of ``_BULKY_ARGUMENTS`` agrees with the parent's: a session of
    dry runs with long scripted replies reaches the model the same either way."""
    (tmp_path / "a.txt").write_text("A" * 50, encoding="utf-8")
    bound = {**pack("read", "forge_dryrun"), "context_policy": {"max_characters": 2000}}
    changed: list[bool] = []
    real_apply = _ContextCap.apply

    def spy(self: _ContextCap, pi_context: object) -> object:
        thinned = real_apply(self, pi_context)
        changed.append(thinned is not pi_context)
        return thinned

    monkeypatch.setattr(_ContextCap, "apply", spy)
    both, backend = dryrun_session()
    ctx = context(tmp_path, both, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, bound) == "Done."
    assert len(changed) == 7 and not any(changed)  # the bridge sent them already thinned
    monkeypatch.setattr(
        _ContextCap, "bridge_rule", lambda self: {"max_characters": 0, "floor": 0}
    )
    changed.clear()
    alone, backend = dryrun_session()
    ctx = context(tmp_path, alone, is_evaluation=False, is_build=True)
    assert await backend.run(ctx, {}, bound) == "Done."
    assert any(changed)  # here the parent did the trimming
    assert [messages for _, messages, _, _ in both.sent] == [
        messages for _, messages, _, _ in alone.sent
    ]
    [first, *_] = [m for m in both.sent[-1][1] if m.tool_calls]
    size = len(json.dumps(["a" * 400] * 2, separators=(",", ":")))  # 807
    assert first.tool_calls[0].arguments == {
        "case": 0, "replies": _TRIMMED_ARGUMENT.format(chars=size),
    }


# -- a context note is never written into a file (Forge pack v4) ----------------------------

# The write note before pack v4, as the bake-off's super-40 pasted it into harness.py.
OLD_WRITE_NOTE = (
    "[8737 characters, removed to keep the conversation under its size limit. Read the "
    "file to see its current text.]"
)


def test_the_write_note_is_a_short_instruction_not_a_stand_in_for_text() -> None:
    note = _TRIMMED_WRITE.format(chars=8737)
    # No size and no brackets: nothing in it reads like the file's text or a reference
    # to it, and it says what to do instead.
    assert note == _TRIMMED_WRITE and "8737" not in note and not note.startswith("[")
    assert "This note is not file content: read the file for its current content." in note
    assert len(note) < len(OLD_WRITE_NOTE) and note.isascii()
    assert PARENT_TEXTS["context_trimmed"] == note
    assert PARENT_TEXTS["context_note_refused"] == _NOTE_REFUSED


@pytest.mark.parametrize(
    "text",
    [
        _TRIMMED_WRITE,
        OLD_WRITE_NOTE,
        _DROPPED_RESULT,
        _TRIMMED_ARGUMENT.format(chars=12),
        # Pasted with other text, with a word changed, wrapped or in another case.
        "x = 1\n" + _TRIMMED_WRITE + "\n",
        OLD_WRITE_NOTE.replace("current text", "current content"),
        OLD_WRITE_NOTE.replace(" ", "\n    "),
        _TRIMMED_WRITE.upper(),
        "# this note is not file  content",
    ],
)
def test_every_context_note_is_caught_in_each_shape_of_a_write_or_edit(text: str) -> None:
    for name, arguments in (
        ("write", {"path": "h.py", "content": text}),
        ("edit", {"path": "h.py", "edits": [{"oldText": "a", "newText": text}]}),
        ("edit", {"path": "h.py", "edits": [{"oldText": text, "newText": "b"}]}),
        ("edit", {"path": "h.py", "edits": json.dumps([{"oldText": "a", "newText": text}])}),
        ("edit", {"path": "h.py", "oldText": "a", "newText": text}),
    ):
        assert _carries_note(name, arguments), (name, arguments)


def test_ordinary_file_text_is_never_taken_for_a_note() -> None:
    for name, arguments in (
        ("write", {"path": "h.py", "content": "async def run(context, task):\n    return 1\n"}),
        ("write", {"path": "h.py", "content": "Keep the reply short to fit the size limit."}),
        ("edit", {"path": "h.py", "edits": [{"oldText": "a", "newText": "note = 'b'"}]}),
        ("edit", {"path": "h.py", "edits": [{"oldText": 1, "newText": None}]}),
        ("write", {"path": "h.py", "content": None}),
        ("read", {"path": _TRIMMED_WRITE}),  # only a write's or an edit's text counts
    ):
        assert not _carries_note(name, arguments), (name, arguments)


async def test_a_trimmed_write_pasted_back_is_refused_and_the_file_kept(tmp_path: Path) -> None:
    """The live failure: once the cap trims an old write, its note is all the history shows
    of that text, and an engineer copied it into the file as the new text. That write is
    refused with a message that says what to do, and the file keeps its text."""
    text = numbered_lines(60, "a")
    adapter = Streaming(
        reply(call("write", "w1", path="h.py", content=text)),
        reply(call("read", "r1", path="h.py")),
        reply(call("read", "r2", path="h.py")),
        reply(call("write", "w2", path="h.py", content=_TRIMMED_WRITE)),
        reply(call("edit", "e1", path="h.py",
                   edits=[{"oldText": "a line 0001\n", "newText": OLD_WRITE_NOTE}])),
        reply(call("edit", "e2", path="h.py",
                   edits=[{"oldText": "a line 0001\n", "newText": "b line 0001\n"}])),
        reply(text="Done."),
    )
    bound = {**pack("read", "write", "edit"), "context_policy": {"max_characters": 1500}}
    backend = PiBackend(PiConfig(context_cap="parent"))
    assert await backend.run(context(tmp_path, adapter), {}, bound) == "Done."
    # The note was all the history held of the first write when the paste came.
    _, before, _, _ = adapter.sent[3]
    [first] = [
        item.arguments for message in before for item in message.tool_calls
        if item.id == "w1"
    ]
    assert first == {"path": "h.py", "content": _TRIMMED_WRITE}
    # Neither note reached the file; the ordinary edit after them did.
    assert (tmp_path / "h.py").read_text(encoding="utf-8") == text.replace(
        "a line 0001\n", "b line 0001\n"
    )
    _, last, _, _ = adapter.sent[-1]
    results = {message.tool_call_id: message for message in last if message.role == "tool"}
    for refused in ("w2", "e1"):
        assert results[refused].is_error and results[refused].text == _NOTE_REFUSED
    assert not results["e2"].is_error


async def test_without_the_parent_cap_no_note_exists_and_nothing_is_refused(
    tmp_path: Path,
) -> None:
    # Only a session whose context the parent caps can show a note, so only there is
    # one refused: elsewhere the same text is ordinary file text.
    adapter = Streaming(
        reply(call("write", "w1", path="notes.md", content=OLD_WRITE_NOTE)), reply(text="Done.")
    )
    assert await PiBackend().run(context(tmp_path, adapter), {}, pack("write")) == "Done."
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == OLD_WRITE_NOTE


def test_parent_texts_cover_every_re_ask() -> None:
    assert PARENT_TEXTS["cut"] == _CUT_REPLY and PARENT_TEXTS["unfinished"] == _UNFINISHED
    assert PARENT_TEXTS["unfinished_again"] == _UNFINISHED_AGAIN
    assert PARENT_TEXTS["unfinished_again_envelope"] == _UNFINISHED_AGAIN_CORRECTION
    assert all(isinstance(text, str) and text for text in PARENT_TEXTS.values())
    assert all(text.isascii() for text in PARENT_TEXTS.values())
