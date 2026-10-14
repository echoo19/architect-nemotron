"""Forge driver: an opt-in Pi agent session in the Stage 3 slot (Phase 9, Section 8).

The agent writes sandbox language v2 source in a throwaway workspace and reaches
trusted code only through the five tools in ``forge_tools``. Candidates live in a
forge-private store the agent cannot read. After the session the single best
accepted candidate is promoted into ``artifacts/`` and handed to the ordinary
Stage 4 ``verify()`` by the Compiler; nothing from Stage 4 ever reaches a session.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from architect.backends.pi import FORGE_TOOL_NAMES, PiBackend, PiConfig, TrustedTool
from architect.compiler.forge_audit import AuditError, AuditSession, current_audit
from architect.compiler.forge_pack import pack_hash
from architect.compiler.forge_settings import ForgeSettings
from architect.compiler.forge_templates import default_template, instantiate
from architect.compiler.forge_tools import (
    ALLOWED_PRESETS,
    FORGE_EVENTS,
    ForgeRank,
    ForgeSession,
    forge_rank,
    forge_tools,
    submit_source,
)
from architect.compiler.forge_turns import ForgeTurn, TurnLog
from architect.compiler.forge_workspace import language_text, prepare_workspace
from architect.compiler.types import (
    AdapterFactory,
    CandidateEvaluation,
    FloorFactory,
    LibraryLookup,
)
from architect.floors import FloorEvaluator
from architect.models import ModelAdapter, ModelRequest, ModelResponse, UsageQuote
from architect.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatStreaming,
    DoneEvent,
    StopReason,
    ToolSpec,
    UsageEvent,
)
from architect.presets import Preset
from architect.runtime import BudgetExceeded, RunContext, Runtime
from architect.runtime.wallet import compilation_wallet
from architect.spec import Budget, HarnessArtifact

__all__ = [
    "FORGE_INSTRUCTIONS",
    "FORGE_JSON_KEYS",
    "ForgeRank",
    "ForgeResult",
    "forge",
    "forge_prompt",
    "forge_rank",
]

FORGE_INSTRUCTIONS: Final = """\
You are Forge. You write harness.py in the sandbox language of the card at the end of
this prompt (LANGUAGE.md holds the same card; you never need to read it). It is
interpreted, not Python: only its 4 fixed imports, no classes, only what the card lists.

The harness runs at play time with the play model named in SPEC.md, often a small
model with a short context window. Keep prompts short, parse replies with
extract_json, and always have a fallback when a reply does not parse.

Games: keep state in variables, not in prompts. Use digest() to remember visited
states, prefer untried actions before asking the model, and summarise frames with
grid_text or objects instead of sending raw grids.

Beat BASELINE.json by engineering for this task, not by rewording its prompt. DESIGN.md
lists the strategies, such as code before model and the task's rules enforced in code.

Starting harnesses, already bound to this task, are in templates/. Text tasks:
templates/extract_verify.py and templates/prompt_chain.py. Image inputs:
templates/vision_two_pass.py. Games: templates/game_explorer.py and
templates/game_hypothesis.py.

The loop:
1. Read SPEC.md, DESIGN.md, TUNING.md, BASELINE.json, harness.py and the template
   that fits the task.
2. Write harness.py from the current file or a template. After that, change it with
   edit instead of writing the whole file again.
3. Call forge_check until it returns ok. It is free. It lists every refused line,
   each with its rule and the allowed form: fix them all, then check again.
4. Call forge_dryrun on one case. It is free and shows your prompts and output.
5. Call forge_measure on a few tuning cases, then on more of them.
6. Improve the harness from what you measured.
7. Call forge_submit when forge_measure shows it beats BASELINE.json. Submit early;
   the best accepted submission wins.

Use forge_spawn to split off a sub-task with clear input and output schemas, then
call it from harness.py as: await context.harness("name", value).

Tuning results are evidence, not a license. A hidden verifier you cannot query
decides. Never copy expected answers or long input text into code: forge_submit
rejects it.

Do not read files outside this directory. Every tool result ends with a left
object showing the remaining budget and tool calls.

Stop by replying with plain text and no tool call when you are done, or when left
is exhausted.
"""



def forge_prompt() -> str:
    """What Forge adds to its engineer's system prompt: the instructions, then the
    language card (LANGUAGE.md). A system prompt is never trimmed, so the card stays in
    every turn's context however long the session runs, unlike a read of LANGUAGE.md."""
    return FORGE_INSTRUCTIONS + "\n" + language_text()


FORGE_JSON_KEYS: Final = (
    "language",
    "mode",
    "agent_model",
    "play_model",
    "checks",
    "dryruns",
    "measures",
    "submissions",
    "accepted",
    "spawns",
    "agent_cost_usd",
    "agent_tokens_in",
    "agent_tokens_out",
    "wall_s",
    "harness_nodes",
    "harness_bytes",
    "stage4_calls",
    "verifications_for_spec",
    "notes",
    "children",
    "pack_sha256",
    "turns",
)
_PI_TOOLS: Final = ("read", "write", "edit", *sorted(FORGE_TOOL_NAMES))
_RESULT_CHARS: Final = 16384


class ForgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    best: CandidateEvaluation
    mode: Literal["agent", "template"]
    checks: int = Field(ge=0)
    dryruns: int = Field(ge=0)
    measures: int = Field(ge=0)
    submissions: int = Field(ge=0)
    accepted: int = Field(ge=0)
    spawns: int = Field(ge=0)
    agent_cost_usd: float = Field(ge=0)
    agent_tokens_in: int = Field(ge=0)
    agent_tokens_out: int = Field(ge=0)
    wall_s: float = Field(ge=0)
    failures: tuple[str, ...]
    # The Forge pack this session ran with (``forge_pack``) and its per-call rows.
    pack_sha256: str | None = None
    turns: tuple[ForgeTurn, ...] = ()


def _quoted(quote: Callable[[], UsageQuote]) -> UsageQuote | None:
    """A call's quote, or None when it cannot be made (never masks the call's own error)."""
    try:
        return quote()
    except Exception:  # noqa: BLE001 - the meter only records; the runtime decides
        return None


class _Metered:
    """Count the agent model's own usage; tokens are metered, never capped.

    Every call also adds one content-free ``ForgeTurn`` row (``turns``), handed to
    ``log`` as it ends, failed and cancelled calls included. A call that ended before
    reporting its final usage counts its quote, marked ``estimated``, as the runtime and
    the wallets keep it.
    """

    def __init__(self, inner: ModelAdapter) -> None:
        self._inner = inner
        self.model = inner.model
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.turns: list[ForgeTurn] = []
        self.log: TurnLog | None = None

    def quote(self, request: ModelRequest) -> UsageQuote:
        return self._inner.quote(request)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        try:
            response = await self._inner.complete(request)
        except BaseException:
            quote = _quoted(lambda: self._inner.quote(request))
            self._turn(None, started, stop=None, ok=False, quote=quote)
            raise
        self._turn(response, started, stop=None, ok=True)
        return response

    def _turn(
        self,
        usage: ModelResponse | UsageEvent | None,
        started: float,
        *,
        stop: StopReason | None,
        ok: bool,
        quote: UsageQuote | None = None,
    ) -> None:
        """Count one call: its usage, else (none was reported) its quote, else nothing."""
        if usage is not None:
            prompt, completion, cost = usage.tokens_in, usage.tokens_out, usage.cost_usd
            cached, reasoning = usage.cache_read_tokens, usage.reasoning_tokens
            estimated = isinstance(usage, UsageEvent) and usage.estimated
        elif quote is not None:
            prompt, cached, reasoning = quote.tokens_in, 0, 0
            completion, cost, estimated = quote.tokens_out, quote.cost_usd, True
        else:
            prompt = cached = reasoning = completion = 0
            cost, estimated = 0.0, False
        self.tokens_in += prompt
        self.tokens_out += completion
        self.cost_usd += cost
        row = ForgeTurn(
            turn=len(self.turns) + 1,
            prompt_tokens=prompt,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            completion_tokens=completion,
            cost_usd=cost,
            seconds=round(max(0.0, time.monotonic() - started), 3),
            stop=stop,
            estimated=estimated,
            ok=ok,
        )
        self.turns.append(row)
        if self.log is not None:
            self.log.append(row)


class _StreamingAgent(ModelAdapter, ChatStreaming, Protocol):
    """An agent adapter that also streams native chats."""


class _MeteredChat(_Metered):
    """``_Metered`` that forwards native chats, so the agent's Pi turns use native tool calls.

    Each stream is metered with the usage it reported with its ``DoneEvent``, the only
    final one. A stream that failed or was cancelled before then counts its quote, as
    ``RunContext.stream_call`` and the wallets keep it: an earlier usage, such as
    Anthropic's ``message_start``, leaves out the reply. So the agent's tokens, dollars
    and turn rows match the wallets for every streamed call.
    """

    def __init__(self, inner: _StreamingAgent) -> None:
        super().__init__(inner)
        self._chat = inner

    def quote_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> UsageQuote:
        return self._chat.quote_chat(system, messages, tools, max_output_tokens)

    async def stream_chat(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_output_tokens: int | None,
    ) -> AsyncIterator[ChatEvent]:
        usage: UsageEvent | None = None
        final: UsageEvent | None = None  # the usage reported with the DoneEvent
        stop: StopReason | None = None
        finished = False
        started = time.monotonic()
        quote = _quoted(
            lambda: self._chat.quote_chat(system, messages, tools, max_output_tokens)
        )
        stream = self._chat.stream_chat(system, messages, tools, max_output_tokens)
        try:
            async for event in stream:
                if isinstance(event, UsageEvent):
                    usage = event
                elif isinstance(event, DoneEvent):
                    stop, final = event.stop_reason, usage
                yield event
            finished = final is not None
        finally:
            try:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            finally:
                self._turn(final, started, stop=stop, ok=finished, quote=quote)


def _metered(inner: ModelAdapter) -> _Metered:
    """The agent's meter; it streams native chats exactly when ``inner`` does."""
    if isinstance(inner, ChatStreaming):
        return _MeteredChat(cast(_StreamingAgent, inner))
    return _Metered(inner)


def _telemetry_directory(runtime: Runtime) -> Path | None:
    """Where the runtime's telemetry lives (per-turn rows go next to it), if anywhere."""
    directory = getattr(getattr(runtime, "store", None), "directory", None)
    return directory if isinstance(directory, Path) else None


def _agent_limits(adapter: object) -> tuple[object | None, int | None, bool]:
    """The agent model's ``Price``, its adapter's own output ceiling (a profile's
    ``max_tokens``) and whether that adapter holds a reply to a per-request output cap
    (``output_cap_enforced``; a coding-agent CLI does not), found through metering
    wrappers (``_inner``)."""
    for _ in range(4):
        price = getattr(adapter, "_price", None)
        if price is not None:
            ceiling = getattr(adapter, "_max_tokens", None)
            enforced = getattr(adapter, "output_cap_enforced", True) is not False
            return cast(object, price), ceiling if type(ceiling) is int else None, enforced
        adapter = getattr(adapter, "_inner", None)
        if adapter is None:
            break
    return None, None, True


def _agent_output_cap(agent: ModelAdapter, settings: ForgeSettings) -> int | None:
    """``settings.agent_max_tokens`` for each agent reply, or None to keep the capacity.

    The cap bounds what one runaway reply costs. A $0 agent (a local model) keeps its
    own capacity, since a local server reserves its context for the cap, and so does an
    agent whose capacity (its price entry's, or its profile's lower ``max_tokens``) is
    already at or below it. An agent that cannot hold a reply to the cap (a coding-agent
    CLI) is never capped: its reservation would shrink while its reply did not.
    """
    price, ceiling, enforced = _agent_limits(agent)
    if not enforced:
        return None
    if price is not None:
        rates = (getattr(price, "input_per_million", 0), getattr(price, "output_per_million", 0))
        capacity = getattr(price, "output_capacity_tokens", None)
        if type(capacity) is int and ceiling is not None:
            capacity = min(capacity, ceiling)
        if rates == (0, 0) or (type(capacity) is int and capacity <= settings.agent_max_tokens):
            return None
    return settings.agent_max_tokens


def _pi_backend(config: PiConfig, capabilities: Mapping[str, TrustedTool]) -> Any:
    """The real pinned Pi SDK backend holding the five capability tools."""
    sink = FORGE_EVENTS.get()
    extra: dict[str, Any] = {"on_event": sink} if sink is not None else {}
    return cast(Any, PiBackend)(config, capabilities=MappingProxyType(dict(capabilities)), **extra)


def _verification_count(library: LibraryLookup | None, spec_hash: str) -> int:
    counter = getattr(library, "verification_count", None)
    if not callable(counter):
        return 0
    try:
        count = counter(spec_hash)
    except (OSError, ValueError):
        return 0
    return count if type(count) is int and count >= 0 else 0


def _record_verification(library: LibraryLookup | None, spec_hash: str) -> None:
    record = getattr(library, "record_forge_verification", None)
    if callable(record):
        try:
            record(spec_hash)
        except (OSError, ValueError):
            pass


def _resolved(path: str) -> Path:
    return Path(path).resolve()


def _outside(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return not any(
        resolved.is_relative_to(root.resolve()) or root.resolve().is_relative_to(resolved)
        for root in roots
    )


async def _agent_session(
    session: ForgeSession,
    agent: _Metered,
    pi_config: PiConfig,
) -> None:
    build = getattr(session.runtime, "build", None)
    if not callable(build):
        raise ValueError("forge_failed")
    settings = session.settings
    candidate = session.candidate
    workspace = prepare_workspace(session.workspace, candidate, session.floor, settings)
    # Phase 14: the agent's file tools never reach the audit directory.
    audited = (session.audit.root,) if session.audit is not None else ()
    config = PiConfig(
        node_executable=pi_config.node_executable,
        allowed_tools=_PI_TOOLS,
        allow_shell=False,
        denied_paths=(*pi_config.denied_paths, session.private, *audited),
        max_turns=settings.pi_max_turns,
        max_protocol_bytes=pi_config.max_protocol_bytes,
        max_tool_output_chars=_RESULT_CHARS,
        repair_prose=True,  # unfinished until a submission: never end on stray prose
        final_requires="forge_submit",  # a final answer before any submission: asked again
        context_cap="parent",  # old results and writes go first, the same way every time
    )
    backend = _pi_backend(config, forge_tools(session))
    pack: dict[str, JsonValue] = {
        "backend": "pi",
        "tools": list(_PI_TOOLS),
        "context_policy": {"max_characters": settings.context_chars},
        "prompt_fragment": forge_prompt(),
        "subagent_rules": [],
    }
    cap = _agent_output_cap(agent, settings)
    if cap is not None:
        pack["max_output_tokens"] = cap
    spec = candidate.artifact.spec.model_copy(
        update={
            "model_target": agent.model,
            "budget": Budget(cost_usd=settings.max_usd, compute_s=settings.max_s),
        },
        deep=True,
    )

    async def pi_harness(context: RunContext, task: JsonValue) -> JsonValue:
        return await context.code(task, pack)

    with session.runtime.use_pi_backend(backend):
        await build(
            spec,
            pi_harness,
            {"repo": str(workspace), "goal": "write harness.py"},
            agent,
            preset="forge_agent",
            repository=workspace,
        )


def _promote(session: ForgeSession, artifact_root: Path) -> HarnessArtifact:
    """Copy the best accepted candidate into a new version: the only write to artifacts/."""
    from architect.compiler.tuning import _allocate

    best = session.best
    assert best is not None
    source = best.artifact.directory
    directory, version = _allocate(artifact_root, best.artifact.spec)
    shutil.copyfile(source / "harness.py", directory / "harness.py")
    if (source / "subharnesses").is_dir():
        shutil.copytree(source / "subharnesses", directory / "subharnesses")
    promoted = best.artifact.model_copy(
        update={"directory": directory, "version": version, "report": None}
    )
    promoted.save()
    return promoted


def _forge_json(
    session: ForgeSession,
    promoted: HarnessArtifact,
    *,
    mode: str,
    agent: _Metered | None,
    wall_s: float,
    verifications: int,
    pack_sha256: str,
) -> dict[str, JsonValue]:
    children: list[JsonValue] = []
    for name in session.best_children:
        child = session.children[name]
        children.append(
            {
                "name": name,
                "artifact_digest": child.ref.artifact_digest,
                "case_origin": child.ref.case_origin,
                "licensed": child.artifact.licensed,
            }
        )
    values: dict[str, JsonValue] = {
        "language": 2,
        "mode": mode,
        "agent_model": agent.model if agent is not None else None,
        "play_model": promoted.model,
        "checks": session.checks,
        "dryruns": session.dryruns,
        "measures": session.measures,
        "submissions": session.submissions,
        "accepted": session.accepted,
        "spawns": session.spawns,
        "agent_cost_usd": round(agent.cost_usd, 9) if agent is not None else 0.0,
        "agent_tokens_in": agent.tokens_in if agent is not None else 0,
        "agent_tokens_out": agent.tokens_out if agent is not None else 0,
        "wall_s": round(wall_s, 3),
        "harness_nodes": session.best_nodes,
        "harness_bytes": session.best_source_bytes,
        "stage4_calls": 1,
        "verifications_for_spec": verifications,
        "notes": [{"chars": chars, "sha256": digest} for chars, digest in session.notes],
        "children": children,
        "pack_sha256": pack_sha256,
        "turns": [
            cast(JsonValue, row.model_dump(mode="json"))
            for row in (agent.turns if agent is not None else ())
        ],
    }
    return {key: values[key] for key in FORGE_JSON_KEYS}


async def forge(
    candidate: CandidateEvaluation,
    preset: Preset,
    floor: FloorEvaluator,
    smoke_floor: FloorEvaluator | None,
    adapter_factory: AdapterFactory,
    runtime: Runtime,
    artifact_root: Path,
    agent_adapter: ModelAdapter | None,
    settings: ForgeSettings,
    *,
    pi_config: PiConfig,
    floor_factory: FloorFactory,
    library: LibraryLookup | None,
    depth: int = 0,
) -> ForgeResult:
    """Run one Forge session and promote its best accepted candidate, if any.

    ``BudgetExceeded`` and the forge deadline end the session normally with the
    best accepted candidate so far. Only fixed codes are returned as failures.
    Under ``forge_audit`` (Phase 14) every submission is recorded; a record or the
    session's close that cannot be written ends it with ``forge_audit_failed`` and
    the Stage 2 candidate, so an unaudited Forge harness is never kept.
    """
    started = time.monotonic()
    artifact = candidate.artifact
    if (
        artifact.preset != preset.name
        or floor.definition != artifact.spec.floor
        or (smoke_floor is not None and smoke_floor is floor)
    ):
        raise ValueError("forge_failed")
    mode = settings.mode
    failures: list[str] = []
    # The pack as the session starts: its bridge files are read now, before Pi is spawned,
    # so a later edit on disk (a merge into a checkout a long run uses) is never recorded.
    pack_sha256 = pack_hash(settings)

    def result(
        best: CandidateEvaluation, session: ForgeSession | None, agent: _Metered | None
    ) -> ForgeResult:
        return ForgeResult(
            best=best,
            mode=mode,
            checks=session.checks if session else 0,
            dryruns=session.dryruns if session else 0,
            measures=session.measures if session else 0,
            submissions=session.submissions if session else 0,
            accepted=session.accepted if session else 0,
            spawns=session.spawns if session else 0,
            agent_cost_usd=agent.cost_usd if agent else 0.0,
            agent_tokens_in=agent.tokens_in if agent else 0,
            agent_tokens_out=agent.tokens_out if agent else 0,
            wall_s=max(0.0, time.monotonic() - started),
            failures=tuple(dict.fromkeys(failures)),
            pack_sha256=pack_sha256,
            turns=tuple(agent.turns) if agent else (),
        )

    spec_hash = artifact.spec.spec_hash
    if _verification_count(library, spec_hash) >= settings.max_verifications_per_spec:
        failures.append("forge_verification_cap")
        return result(candidate, None, None)
    agent: _Metered | None = None
    if mode == "agent":
        agent = _metered(agent_adapter or adapter_factory(artifact.model))
        agent.log = TurnLog(
            _telemetry_directory(runtime), spec_hash=spec_hash, model=agent.model, depth=depth
        )
    best = candidate
    # Phase 14, Section 8.3: an audit directory set by ``forge_audit`` is kept apart from
    # the workspace and the private store, and denied to the agent's file tools.
    audit = current_audit()
    audited = (audit.directory,) if audit is not None else ()
    with (
        TemporaryDirectory(prefix="architect-forge-private-", ignore_cleanup_errors=True) as p,
        TemporaryDirectory(prefix="architect-forge-", ignore_cleanup_errors=True) as w,
        ExitStack() as cleanup,
    ):
        private, workspace = _resolved(p), _resolved(w)
        protected = (artifact_root, *pi_config.denied_paths, *audited)
        if not (_outside(workspace, protected) and _outside(private, (artifact_root, *audited))):
            failures.append("forge_failed")
            return result(candidate, None, agent)
        closer: _AuditCloser | None = None
        if audit is not None:
            try:
                session_audit = audit.open_session(
                    spec_hash=spec_hash,
                    model=artifact.model,
                    agent_model=agent.model if agent is not None else None,
                    mode=mode,
                    depth=depth,
                )
            except AuditError:
                failures.append("forge_audit_failed")  # nothing ran, nothing is kept
                return result(candidate, None, agent)
            closer = _AuditCloser(session_audit, failures)
            cleanup.callback(closer.abandon)  # closes it on any exceptional exit too
        session = ForgeSession(
            candidate=candidate,
            floor=floor,
            smoke_floor=smoke_floor,
            adapter_factory=adapter_factory,
            runtime=runtime,
            settings=settings,
            workspace=workspace,
            private=private,
            floor_factory=floor_factory,
            depth=depth,
            allowed_presets=ALLOWED_PRESETS.get(),
            agent_adapter=agent_adapter,
            pi_config=pi_config,
            audit=closer.audit if closer is not None else None,
        )
        if closer is not None:
            closer.session = session
        with compilation_wallet(
            Budget(cost_usd=settings.max_usd, compute_s=settings.max_s)
        ) as wallet:
            session.wallet = wallet
            try:
                async with asyncio.timeout(max(wallet.remaining_s, 0.001)):
                    if mode == "template":
                        name = settings.template or default_template(artifact.spec)
                        await submit_source(session, instantiate(name, candidate))
                    else:
                        assert agent is not None
                        await _agent_session(session, agent, pi_config)
            except (BudgetExceeded, TimeoutError):
                pass  # The forge wallet or deadline ended the session: keep the best so far.
            except Exception:
                # No provider, Pi, tool or source exception text crosses this boundary.
                failures.append("forge_failed")
        if closer is not None and closer.audit.failed:
            failures.append("forge_audit_failed")  # an unaudited session promotes nothing
        elif session.best_number is None:
            failures.append("forge_no_submission")
        else:
            try:
                promoted = _promote(session, artifact_root)
                _record_verification(library, spec_hash)
                verifications = max(1, _verification_count(library, spec_hash))
                (promoted.directory / "forge.json").write_text(
                    json.dumps(
                        _forge_json(
                            session,
                            promoted,
                            mode=mode,
                            agent=agent,
                            wall_s=time.monotonic() - started,
                            verifications=verifications,
                            pack_sha256=pack_sha256,
                        ),
                        indent=2,
                        ensure_ascii=True,
                        allow_nan=False,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                assert session.best is not None
                best = CandidateEvaluation(
                    artifact=promoted, measurement=session.best.measurement
                )
            except (OSError, ValueError):
                failures.append("forge_failed")
                best = candidate
        if closer is not None:
            best = closer.close(best, candidate)
        return result(best, session, agent)


@dataclass
class _AuditCloser:
    """Closes one audit session on every exit path of ``forge()`` (Phase 14, 8.3)."""

    audit: AuditSession
    failures: list[str]
    session: ForgeSession | None = None
    closed: bool = False

    def _close(self) -> None:
        self.closed = True
        promoted = self.session.best_number if self.session is not None else None
        self.audit.close(promoted=promoted, failures=tuple(dict.fromkeys(self.failures)))

    def close(
        self, best: CandidateEvaluation, baseline: CandidateEvaluation
    ) -> CandidateEvaluation:
        """The normal exit: a session whose audit cannot be finished keeps nothing, so a
        promoted copy is removed and the Stage 2 baseline is returned instead."""
        try:
            self._close()
        except AuditError:
            if "forge_audit_failed" not in self.failures:
                self.failures.append("forge_audit_failed")
            if best is not baseline:
                shutil.rmtree(best.artifact.directory, ignore_errors=True)
                return baseline
        return best

    def abandon(self) -> None:
        """An exceptional exit (a cancelled build): close what can still be closed."""
        if not self.closed:
            with suppress(AuditError):
                self._close()
