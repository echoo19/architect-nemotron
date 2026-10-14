"""The conductor: the agent the user talks to (Phase 12, Section 3).

A Python loop over a text model with the Pi-style JSON envelope protocol, so it works with
any model Architect can call (the ``claude`` and ``codex`` CLIs, Ollama, HTTP APIs). It
answers questions itself and delegates work to subagents; it never writes files.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from architect.backends.pi import _control_reply, _tool_attempt
from architect.models import ModelAdapter
from architect.runtime import BudgetExceeded, RunContext

from .events import UI, AssistantText, Notice, Thinking, ToolFinished, ToolStarted
from .tools import (
    Permission,
    Tool,
    ToolContext,
    ToolError,
    control_spec,
    default_tools,
    result_summary,
    tool_summary,
)
from .transcript import Transcript

__all__ = ["Conductor", "ConductorSettings", "Mode", "TurnResult", "project_instructions"]

Mode = Literal["normal", "build"]
INSTRUCTION_FILES = ("ARCHITECT.md", "AGENTS.md", "CLAUDE.md")
INSTRUCTION_BYTES = 16 * 1024
MAX_REPAIRS = 2

SYSTEM = """\
You are Architect, a terminal agent whose specialty is building agents, harnesses and
agent pipelines, and which also handles ordinary tasks by delegating them.

How you work:
- You never edit files yourself. For any change to files, call `code` with a complete
  description; the coder subagent makes the change with the user's approval.
- For structured one-off jobs (extract, summarize, plan, verify, analyze, rank), call
  `spawn` with the matching preset.
- When the user wants an agent, harness, bot or pipeline built (for example "make an agent
  that extracts invoice fields" or "build a harness for ARC-AGI-3"), call `make` with their
  request in their words. Build mode interviews them briefly, infers a quality floor and a
  budget with defaults, then compiles, tests and installs the agent. Do not write the agent
  yourself and do not ask the interview questions yourself.
- A `make` result names the agent it installed in `installed`. When it names none, the new
  build was not installed: say so plainly, with its `outcome` when it has one. An agent of
  the same name that `agents` lists is then an earlier build, not this one.
- A `make` result with `failures` and no `installed` is a build that ended without an
  agent. Tell the user what stopped it and the `next_step` it gives, then stop: no `code`,
  no shell, no second build and no other tool to work around it. What to do next is the
  user's decision.
- To run an agent that is already installed, call `run_agent`; `agents` lists them.
- Read and search the working directory (`read_file`, `list_dir`, `glob`, `grep`) when you
  need context. Answer simple questions directly without tools.
- Be brief. Report what happened, including failures, plainly.

Reply protocol: every reply is exactly one JSON object and nothing else. To use tools reply
{"tool_calls": [{"id": "call-1", "name": "<tool>", "arguments": {...}}]}. To answer the user
reply {"text": "<markdown answer>"}. Only the tools listed below exist.
"""
BUILD_MODE = (
    "The user switched to BUILD MODE: treat their message as a description of an agent, "
    "harness or pipeline to build and call `make` with it, unless it is clearly a question."
)
# The whole answer of a turn whose build the user set aside (``Conductor.set_aside``).
SET_ASIDE = "Set aside. Nothing was built."
# How a turn ends when its build ended without an agent and the model then asked for
# tools in place of an answer (``Conductor.ended``).
BUILD_ENDED = "The build ended without an agent, and nothing was installed."
# What the fixed answer adds for a build that ended in an error and not in a result: the
# error's code stays in the tool result, where the transcript and the logs keep it.
BUILD_ERROR = "It stopped on an error before it finished. You can ask for the build again."
# How the fixed answer starts when the error was raised before anything was built.
NOT_STARTED = "No build was started"


def _not_started(code: str, detail: str) -> str:
    """Why no build was started, in plain words with the error's own detail (which setting,
    which file, which model), for the codes that are raised before a build begins; "" for
    any other code. Asking for the same build again would raise the same error, so the
    words say what to change and never "ask for the build again" alone."""
    if code == "invalid_arguments":
        if detail == "description":
            return "the request did not say what the agent should do."
        if detail == "target":
            return "the request did not say what to forge."
        if detail == "spec_file":
            return "the request did not name a spec file."
        return "the request could not be read."
    if code in ("invalid_override", "unknown_override"):
        setting = f"'{detail}'" if detail else "one of them"
        return (
            f"the build setting {setting} has a value a build cannot use. Set it again, "
            "then ask for the build."
        )
    if code == "not_a_file":
        return f"{detail or 'the file the build was given'} is not a file."
    if code == "model_not_configured":
        model = f"the model {detail}" if detail else "the chosen model"
        return (
            f"{model} is not in this profile. Choose one that is with /model, then ask for "
            "the build."
        )
    if code in ("build_mode_unavailable", "forge_unavailable"):
        return "build mode is not available in this session."
    return ""


def build_error_result(code: str, detail: str = "") -> dict[str, JsonValue]:
    """A build that ended in an error, in the shape of a build's own result, so the fixed
    answer (``build_ended_text``) can be made from it.

    An error raised before anything was built gets an ``outcome`` that starts with
    ``NOT_STARTED`` and says what the error was: the answer then never says that a build
    ran or ended. Any other error may have come from the middle of a build: the answer
    says the build ended without an agent and stopped on an error, with the error's own
    detail when it has one. The code stays in ``failures`` and in the tool result."""
    reason = _not_started(code, detail.strip())
    if reason:
        return {"licensed": False, "failures": [code], "outcome": f"{NOT_STARTED}: {reason}"}
    step = BUILD_ERROR
    if detail.strip():
        first, _, rest = BUILD_ERROR.partition(". ")
        step = f"{first}: {detail.strip().rstrip('.')}. {rest}"
    return {"licensed": False, "failures": [code], "next_step": step}


def build_ended_text(result: dict[str, JsonValue]) -> str:
    """One fixed answer for a build that ended without an agent: what its result says to
    do next, as the build wrote it. A result with an ``outcome`` (a build that was compiled
    and refused) leads with it: it already says nothing was installed, and that is said
    once. The failure codes are not part of the answer: a person reads this, and the codes
    are in the build's result, which the transcript and the logs keep."""
    outcome = result.get("outcome")
    parts = [] if isinstance(outcome, str) and outcome.strip() else [BUILD_ENDED]
    for key in ("outcome", "next_step"):
        step = result.get(key)
        if isinstance(step, str) and step.strip():
            step = step.strip()
            end = "" if step.endswith((".", "!", "?")) else "."
            parts.append(step[0].upper() + step[1:] + end)
    return " ".join(parts)


REMINDER = (
    "Continue the conversation above. Reply now with exactly one JSON object: "
    '{"tool_calls": [...]} to use tools, or {"text": "..."} to answer the user.'
)
REPAIR = (
    "Your last reply was not a JSON object in the required format. Reply again with exactly "
    'one JSON object: {"tool_calls": [...]} or {"text": "..."}. No prose, no code fences.'
)


def project_instructions(cwd: Path, workspace: Path | None = None) -> str:
    """The first of ARCHITECT.md, AGENTS.md, CLAUDE.md in ``cwd`` plus the global file."""
    parts: list[str] = []
    if workspace is not None:
        global_file = workspace / "ARCHITECT.md"
        if global_file.is_file():
            text = global_file.read_text(encoding="utf-8", errors="replace")
            parts.append(text[:INSTRUCTION_BYTES])
    for name in INSTRUCTION_FILES:
        path = cwd / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")[:INSTRUCTION_BYTES]
            parts.append(f"# {name}\n{text}")
            break
    return "\n\n".join(parts)


@dataclass
class ConductorSettings:
    max_steps: int = 40
    max_calls_per_step: int = 8
    max_output_tokens: int = 4096
    call_usd: float = 1.0  # budget reserved for one conductor model call
    call_s: float = 300.0
    session_usd: float = 25.0  # conductor and subagent calls; a compile is approved on its card
    session_s: float = 3600.0
    context_chars: int = 400_000
    permission: Permission = "ask"


@dataclass
class TurnResult:
    text: str
    tool_calls: int
    cost_usd: float
    cancelled: bool = False


@dataclass
class Conductor:
    services: object  # architect.profiles.Services
    adapter: ModelAdapter
    cwd: Path
    ui: UI
    settings: ConductorSettings = field(default_factory=ConductorSettings)
    tools: Sequence[Tool] = field(default_factory=default_tools)
    transcript: Transcript = field(default_factory=Transcript)
    mode: Mode = "normal"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    own_usd: float = 0.0  # spend counted here only when no session meter is attached
    started: float = field(default_factory=time.monotonic)
    extras: dict[str, object] = field(default_factory=dict)
    instructions: str = ""
    # Messages the user sent while a turn runs ("steering"): taken between steps, so
    # the model reads them after the current tool calls without the turn stopping.
    steering: list[str] = field(default_factory=list)
    on_steered: Callable[[str], None] | None = None
    # `make` calls in the running turn: one build per user turn, so the model never starts
    # a second build the user did not ask for.
    makes_this_turn: int = 0
    # Whether the user cancelled the running turn's build (Not now on the plan, or a
    # cancelled question): the turn then ends with SET_ASIDE and no further model call.
    build_set_aside: bool = False
    # The result of this turn's build when it ended without an agent (not licensed, and
    # not set aside by the user). No tool runs in the turn after it: live, a model given
    # such a result started the coder, which asked for 57 shell commands.
    build_ended: dict[str, JsonValue] | None = None

    def steer(self, text: str) -> None:
        """Add a message to the running turn; it is read before the next model call."""
        if text.strip():
            self.steering.append(text)

    def take_steering(self) -> None:
        """Move waiting steering messages into the transcript (between steps only)."""
        while self.steering:
            text = self.steering.pop(0)
            self.transcript.user(text)
            if self.on_steered is not None:
                self.on_steered(text)

    def __post_init__(self) -> None:
        self._by_name = {tool.name: tool for tool in self.tools}
        self.context = ToolContext(
            services=self.services,  # type: ignore[arg-type]
            cwd=self.cwd,
            ui=self.ui,
            model=self.adapter.model,
            permission=self.settings.permission,
            extras=self.extras,
            budget_left=self.remaining_usd,
        )

    def supports_images(self) -> bool:
        """Whether this turn can show attached images to the model: an envelope-path adapter
        whose price entry declares image input (the reservation needs that ceiling)."""
        from .native import streaming_adapter

        if streaming_adapter(self.adapter) is not None:
            return False  # the native chat messages carry text only (Phase 13)
        adapter: object = self.adapter
        for _ in range(4):
            price = getattr(adapter, "_price", None)
            if price is not None:
                return getattr(price, "image", None) is not None
            adapter = getattr(adapter, "_inner", None)
            if adapter is None:
                break
        return False

    def set_tools(self, tools: Sequence[Tool]) -> None:
        """Replace the tool set (MCP servers added or removed); names must stay unique."""
        self.tools = tuple(tools)
        self._by_name = {tool.name: tool for tool in self.tools}

    # -- prompt -----------------------------------------------------------------

    def system_prompt(self) -> str:
        listing = json.dumps([tool.listing() for tool in self.tools], ensure_ascii=False)
        parts = [SYSTEM, "Tools:\n" + listing, f"Working directory: {self.cwd}"]
        if self.instructions:
            parts.append("Project instructions:\n" + self.instructions)
        return "\n\n".join(parts) + "\n\n"

    def _prompt(self, extra: str | None) -> str:
        self.transcript.fit(self.settings.context_chars)
        prompt = "Conversation (JSON messages, oldest first):\n" + self.transcript.render()
        if self.mode == "build":
            prompt += "\n\n" + BUILD_MODE
        prompt += "\n\n" + (extra or REMINDER)
        return prompt

    # -- build mode ---------------------------------------------------------------

    def routes_to_make(self, text: str) -> bool:
        """Whether this turn goes straight to ``make`` without asking the model first.

        In build mode the first request of a conversation is a description to build, so
        it is never answered as a chat message (a model could read a file in the folder
        and answer the request itself). Questions (ending with "?"), commands, replies of
        under four words and every later message still go to the model, which has the
        context for follow-ups such as a changed bar."""
        if self.mode != "build" or "make" not in self._by_name:
            return False
        stripped = text.strip()
        if stripped.startswith("/") or stripped.endswith("?") or len(stripped.split()) < 4:
            return False
        return not any(m.role == "tool" and m.name == "make" for m in self.transcript.messages)

    async def route_make(self, text: str) -> None:
        """Run ``make`` on the user's words as the turn's first step, recorded as a call."""
        call: dict[str, JsonValue] = {
            "id": f"make-{uuid.uuid4().hex[:8]}",
            "name": "make",
            "arguments": {"description": text},
        }
        self.transcript.assistant(calls=[call])
        await self._dispatch(call)

    def set_aside(self, calls: int, spent_before: float) -> TurnResult:
        """End a turn right after a build (``make`` or ``forge``) the user cancelled, with
        one fixed sentence.

        No model call follows: in filmed takes a model given the cancelled result went on
        by itself, trying ``make`` again or asking questions of its own. Calls the model
        asked for after the build in the same step do not run."""
        self.transcript.assistant(SET_ASIDE)
        self.ui.emit(AssistantText(SET_ASIDE))
        return TurnResult(SET_ASIDE, calls, self.spent_usd - spent_before)

    def ended(self, calls: int, spent_before: float) -> TurnResult:
        """End a turn whose build ended without an agent when the model asks for tools in
        place of an answer: none of them runs, and the answer is ``build_ended_text``."""
        answer = build_ended_text(self.build_ended or {})
        self.transcript.assistant(answer)
        self.ui.emit(AssistantText(answer))
        return TurnResult(answer, calls, self.spent_usd - spent_before)

    # -- budget -----------------------------------------------------------------

    @property
    def spent_usd(self) -> float:
        """Session spend: the session meter (every call, subagents included) when attached."""
        meter = self.extras.get("meter")
        value = getattr(getattr(meter, "spend", None), "cost_usd", None)
        return float(value) if isinstance(value, int | float) else self.own_usd

    def remaining_usd(self) -> float:
        return max(0.0, self.settings.session_usd - self.spent_usd)

    def _charge(self, amount: float) -> None:
        if self.extras.get("meter") is None:
            self.own_usd += max(0.0, amount)

    async def _call(self, extra: str | None = None) -> str:
        """One metered conductor call against the session budget."""
        budget = min(self.settings.call_usd, self.remaining_usd())
        if budget <= 0:
            raise BudgetExceeded("session_budget_exhausted")
        spec = control_spec(
            "Architect conductor turn", self.adapter.model, output_schema={"type": "string"},
            cost_usd=budget, compute_s=self.settings.call_s,
        )
        system, prompt = self.system_prompt(), self._prompt(extra)
        max_tokens = self.settings.max_output_tokens

        pending = self.extras.get("pending_images")
        images = tuple(pending) if isinstance(pending, list) and self.supports_images() else ()

        async def one(context: RunContext, task: JsonValue) -> JsonValue:
            # Attached images ride along with every call of the turn they were sent in.
            return await context.call(prompt, max_tokens, system=system, images=images)

        self.ui.emit(Thinking(True))
        try:
            result = await self.services.runtime.evaluate(  # type: ignore[attr-defined]
                spec, one, None, self.adapter, preset="conductor", components=("conductor",)
            )
        finally:
            self.ui.emit(Thinking(False))
        self._charge(result.cost_usd)
        output = result.output
        return output if isinstance(output, str) else json.dumps(output)

    # -- the loop ---------------------------------------------------------------

    async def turn(self, text: str) -> TurnResult:
        """Run one user turn to its final answer. Cancellation keeps completed work."""
        from .native import native_turn, streaming_adapter  # Phase 13; imports this module

        self.makes_this_turn = 0
        self.build_set_aside = False
        self.build_ended = None
        native = streaming_adapter(self.adapter)
        if native is not None:
            return await native_turn(self, native, text)
        self.transcript.user(text)
        spent_before = self.spent_usd
        calls = 0
        used_tools = False
        names = list(self._by_name)
        try:
            if self.routes_to_make(text):
                calls += 1
                used_tools = True
                await self.route_make(text)
                if self.build_set_aside:
                    return self.set_aside(calls, spent_before)
            for _ in range(self.settings.max_steps):
                self.take_steering()
                repairs = 0
                extra: str | None = None
                while True:
                    reply = await self._call(extra)
                    control = _control_reply(reply, self.settings.max_calls_per_step)
                    if control is not None:
                        break
                    if repairs < MAX_REPAIRS and _tool_attempt(reply, names, used_tools=used_tools):
                        repairs += 1
                        extra = REPAIR
                        continue
                    control = (json.dumps({"text": reply.strip()}), False)
                    break
                value = json.loads(control[0])
                if "text" in value:
                    answer = str(value["text"])
                    self.transcript.assistant(answer)
                    self.ui.emit(AssistantText(answer))
                    return TurnResult(answer, calls, self.spent_usd - spent_before)
                requested = value["tool_calls"]
                if self.build_ended is not None:
                    return self.ended(calls, spent_before)
                self.transcript.assistant(calls=requested)
                used_tools = True
                for call in requested:
                    calls += 1
                    await self._dispatch(call)
                    if self.build_set_aside:
                        return self.set_aside(calls, spent_before)
                    if self.build_ended is not None:
                        break  # the calls asked for after the build do not run
            answer = "I stopped after the step limit for one turn; say 'continue' to go on."
            self.transcript.assistant(answer)
            self.ui.emit(Notice(answer, "warn"))
            return TurnResult(answer, calls, self.spent_usd - spent_before)
        except asyncio.CancelledError:
            self.transcript.note("The user interrupted this turn; finished tool results are kept.")
            raise
        except BudgetExceeded:
            answer = (
                f"The session budget (${self.settings.session_usd:.2f}) is used up; raise it with "
                "/budget session <usd>."
            )
            self.transcript.note(answer)
            self.ui.emit(Notice(answer, "error"))
            return TurnResult(answer, calls, self.spent_usd - spent_before)

    async def _dispatch(self, call: dict[str, JsonValue]) -> None:
        call_id = str(call.get("id", "call"))
        name = str(call.get("name", ""))
        raw = call.get("arguments")
        args: dict[str, JsonValue] = raw if isinstance(raw, dict) else {}
        tool = self._by_name.get(name)
        self.ui.emit(ToolStarted(call_id, name, tool_summary(name, args)))
        if tool is None:
            known: list[JsonValue] = list(self._by_name)
            self._finish(call_id, name, {"error": "unknown_tool", "tools": known}, False)
            return
        if name == "make":
            if self.makes_this_turn >= 1:
                refusal: JsonValue = {
                    "error": "one_build_per_turn",
                    "detail": "A build already ran in this turn. Tell the user what happened; "
                              "they can ask for another build.",
                }
                self._finish(call_id, name, refusal, False)
                return
            self.makes_this_turn += 1
        try:
            result = await tool.run(args, self.context)
        except ToolError as exc:
            self._finish(call_id, name, {"error": exc.code, "detail": exc.detail}, False)
            self._build_failed(name, args, exc.code, exc.detail)
        except BudgetExceeded:
            raise
        except asyncio.CancelledError:
            self.transcript.tool(call_id, name, {"error": "interrupted"}, ok=False)
            self.ui.emit(ToolFinished(call_id, name, False, "interrupted"))
            raise
        except Exception as exc:  # a tool failure the model can see and work around
            failure: JsonValue = {"error": "tool_failed", "detail": type(exc).__name__}
            self._finish(call_id, name, failure, False)
            self._build_failed(name, args, "tool_failed")
        else:
            if isinstance(result, dict) and isinstance(result.get("cost_usd"), int | float):
                self._charge(float(result["cost_usd"]))  # type: ignore[arg-type]
            self._finish(call_id, name, result, True)
            # Build mode (make, and forge on a description) returns {"cancelled": true,
            # "stage": ...} when the user cancels the interview or declines the plan; the
            # turn ends there (``set_aside``).
            cancelled = isinstance(result, dict) and result.get("cancelled") is True
            if cancelled and name in ("make", "forge"):
                self.build_set_aside = True
            elif (
                name in ("make", "forge")
                and isinstance(result, dict)
                and result.get("licensed") is False
            ):
                self.build_ended = result

    def _build_failed(
        self, name: str, args: dict[str, JsonValue], code: str, detail: str = ""
    ) -> None:
        """A build that ended in an error is a build without an agent too: no tool runs
        for the rest of the turn, as after a result that says ``licensed: false``, and the
        fixed answer is made from the error (``build_error_result``). Any other tool's
        failure stays one the model can see and work around, and so does ``forge`` on
        ARC-AGI, which builds no agent."""
        if name not in ("make", "forge"):
            return
        if name == "forge":
            from .build import is_arc  # build imports this module

            target = args.get("target")
            if isinstance(target, str) and is_arc(target):
                return
        self.build_ended = build_error_result(code, detail)

    def _finish(self, call_id: str, name: str, result: JsonValue, ok: bool) -> None:
        self.transcript.tool(call_id, name, result, ok=ok)
        self.ui.emit(ToolFinished(call_id, name, ok, result_summary(result)))
