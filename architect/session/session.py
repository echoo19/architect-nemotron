"""The interactive session state machine (contract Sections 3.10, 4, 5, 10 and 11).

One asyncio loop drives one ``Console``. Every operator line is parsed by trusted code
(``commands.parse``) and dispatched to the handler of the current ``State``; every
expected failure is printed as a fixed code (``codes``) and never raised out of
``handle``. Model output enters only as validated data (a ``Spec`` from
``infer_spec``, cases from ``floors.draft_cases``) and is rendered, never parsed for
commands.

Authority is preserved by construction:

- ``approved=`` is written exactly once, in ``_compile``, from ``self.approved(spec)``;
  ``self._approved_hash`` is assigned exactly once, in the ``approve <hash8>`` branch of
  ``_review`` after the typed token matched the Spec on screen and no blocking blocker
  remained. Every edit yields a new hash, so a stale approval silently stops matching.
- ``licensed`` is always ``HarnessArtifact.licensed`` on an artifact reloaded from disk:
  at render time (``Outcome.licensed``), right before ``Registry.install`` and right
  before ``Runtime.run``. The session caches no license.
- ``Registry.install`` is called only from ``install <name> <hash8>``, and
  ``allow_budget_increase`` is true only when that line carried the literal flag.
- The compile wallet changes only through ``budget compile``; blockers show arithmetic
  and the exact fixing command. The one Spec field the session sets on its own is an
  inferred ``budget.cost_usd`` below the model's smallest reservation, lifted to two
  calls on a session-written profile (``_lift_budget``) and announced with a note; it is
  a reviewable, operator-marked field, never a wallet, and file-loaded Specs keep the
  ``budget_below_quote`` blocker instead. The one shape fix is ``_normalize_schemas``:
  a bare property map a model returned as ``input_schema``/``output_schema`` is wrapped
  into a JSON Schema object right after Stage 0 (still marked inferred, one note line);
  ``{}`` and property-less object schemas stay as proposed and block with
  ``input_schema_missing``/``output_schema_missing``.

Concurrency: while a Stage 0 inference or a compile runs, the console reader and the
work task are awaited together (``asyncio.wait`` with ``FIRST_COMPLETED``) so
``cancel``, ``status``, ``spend``, ``help`` and ``quit`` keep working. ``quit``,
``exit`` and EOF cancel the running work on every console, interactive or not, so a
piped script never keeps spending after its last line. With a non-interactive console
(a ``--script`` file or piped stdin) a line that is not a busy-state command is held and
applied after the work finishes, because such a transcript always wins the race and its
lines are meant in order; an interactive operator who types it while work runs is told
``busy_compiling`` immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypeVar, cast, get_args

from pydantic import JsonValue, TypeAdapter

from architect.compiler import CompileConfig, CompileResult, recompiling
from architect.compiler.inference import _INFERENCE_BUDGET, infer_spec
from architect.compiler.types import ForgeSettings
from architect.models import AdapterError, ModelAdapter
from architect.profiles import (
    Profile,
    ProfileError,
    Services,
    build_services,
    load_profile,
    resolve_forge_agent,
)
from architect.registry import RegistryEntry, RegistryError
from architect.runtime import ArtifactExpiredError, Runtime
from architect.spec import Budget, FloorKind, HarnessArtifact, Lifecycle, PresetName, Spec

from . import render
from .bootstrap import (
    SESSION_PROFILE,
    CliCandidate,
    ModelRow,
    ProbeResult,
    _adapter,
    choose_inference,
    default_workspace,
    detect_clis,
    discover_profile,
    inference_quote,
    min_call_cost,
    model_rows,
    probe_login,
    profile_from_payload,
    profile_payload,
    write_profile,
)
from .codes import SessionError, code_for, hint_for
from .commands import (
    GLOBAL,
    HELP,
    REGISTRY_READ,
    VOCABULARY,
    Command,
    allowed,
    globals_for,
    help_for,
    parse,
    split_first,
)
from .edit import (
    Blocker,
    blockers,
    diff_lines,
    normalize_schema,
    parse_value,
    set_field,
    suggested_cost,
    unset_field,
)
from .floors import (
    DEFAULT_DRAFT_COUNT,
    HONESTY_NOTICE,
    add_case,
    add_file,
    draft_cases,
    find_case,
    new_draft,
    partition_preview,
    ready,
    register,
    review,
    save_dataset,
    summaries,
    summarize,
    synthetic_label,
)
from .io import Console
from .observe import CompileWatcher, JournalCursor, Meter, ProgressPolicy, observe_services
from .state import Draft, FloorDraft, FloorSummary, Outcome, Progress, Spend, State, StatusView

__all__ = ["NO_PROVIDER_TEXT", "VERSION_LINE", "Session"]

VERSION_LINE = "architect 0.1.0"
NO_PROVIDER_TEXT = (
    "No profile and no logged-in CLI found. Log in with 'claude' or 'codex' once, or write "
    "architect.json (see docs/usage.md), then run 'architect' again."
)
SIGNING_KEY_ENV = "ARCHITECT_SIGNING_KEY_FILE"
CLARIFY_HINT = (
    "Tell me the input you will give the agent and the output you expect "
    "(or press Enter to give up): "
)
HELD_NOTE = "  held until the running work finishes"
COMPILE_BUDGET_NOTE = (
    "  CLI models take 15-30 s per call; 2 candidates x 12 tuning cases + 12 held-out is "
    "about 36 calls"
)

_T = TypeVar("_T")
_PRESET_NAMES: tuple[str, ...] = tuple(get_args(PresetName))
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_RESULT_ONLY = VOCABULARY[State.RESULT] - VOCABULARY[State.REVIEW] - VOCABULARY[State.READY]
_DRAFT_ONLY = VOCABULARY[State.REVIEW] - VOCABULARY[State.READY]
_CLI_NAMES: dict[str, str] = {"claude-code": "claude", "codex": "codex"}


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a sibling temporary and ``os.replace``.

    Raises ``OSError`` (the callers map it to their code). A directory target is refused
    before anything is created, and a temporary that could not be moved into place is
    removed, so a failed ``save`` never leaves a ``.tmp`` copy of the Spec behind.
    """
    if path.is_dir():
        raise IsADirectoryError(str(path))
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _budget_time(seconds: float) -> str:
    whole = int(seconds)
    if 60 <= whole < 3600 and whole % 60 == 0:
        return f"{whole // 60}m"
    return render.duration(seconds)


class Session:
    """The state machine; see the module docstring for what it may never do."""

    def __init__(
        self,
        console: Console,
        *,
        cwd: Path,
        workspace: Path | None = None,
        profile_path: Path | None = None,
        probe: bool = True,
        services_factory: Callable[[Profile, Path], Services] = build_services,
        detect: Callable[[], tuple[CliCandidate, ...]] = detect_clis,
        prober: Callable[[ModelAdapter, Runtime], Awaitable[ProbeResult]] = probe_login,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = 2.0,
    ) -> None:
        self.console = console
        self._cwd = cwd
        self._explicit_profile = profile_path
        self._probe = probe
        self._services_factory = services_factory
        self._detect = detect
        self._prober = prober
        self._clock = clock
        self._poll_s = poll_s
        self.workspace = default_workspace(cwd, workspace)
        self.state = State.BOOT
        self.draft: Draft | None = None
        self.outcome: Outcome | None = None
        self.floor: FloorDraft | None = None
        self.services: Services | None = None
        self.profile_path: Path | None = None
        self.meter = Meter(clock)
        self.session_spend = Spend()
        self._approved_hash: str | None = None
        self._approved_draft: Draft | None = None
        self._stage0: Spend | None = None
        self._compile_spend: Spend | None = None
        # (spec_hash, name@rev) when the REVIEW route line showed a granted reuse.
        self._routed_reuse: tuple[str, str] | None = None
        self._last_entry: RegistryEntry | None = None
        self._last_progress = ""
        self._progress_policy = ProgressPolicy()
        self._pending: str | None = None
        self._hold = False
        self._quit = False
        self._busy_prompt = "architect> "
        self._exit_code = 0
        self._started = clock()
        self._closed = True
        self._work: asyncio.Task[object] | None = None
        self._floor_configured = False
        # "benching <done>/<total> <task> <model>" for status while a bench runs.
        self.bench_status: str | None = None
        # The session's own signing key, pinned while a bench runs: each bench cell
        # points ARCHITECT_SIGNING_KEY_FILE at a throwaway key for its duration, and
        # status/profile must keep naming the key that signs the operator's artifacts.
        self.pinned_signing_key: Path | None = None
        # Phase 9: this session's CompileConfig.forge override (``forge`` verb); None is
        # the profile's setting. It never touches the Spec or the profile file.
        self.forge_override: ForgeSettings | None = None
        self.forge_line: str | None = None
        self._forge_active = False
        # True once the operator confirmed Forge for this session (``forge on`` or the
        # pre-compile prompt of a profile-enabled Forge); ``forge off`` clears it.
        self.forge_confirmed = False
        # Phase 10: (run_id, artifact_digest) of the last delivery, for ``rate``; the
        # inference role is pinned once the operator chose a model (``use model``).
        self.last_delivery: tuple[str, str] | None = None
        self.inference_pinned = False
        self._routed_forge: ForgeSettings | None = None
        # The compile-path ``forge_agent`` role choice (``roles.policy = "evidence"``)
        # and the content-free Forge results of that compile, for its Section 11.4
        # outcome; both are per compile.
        self._forge_choice: object | None = None
        self._forged: list[object] = []

    # ----------------------------------------------------------------- public surface

    async def run(self) -> int:
        """0 on quit/EOF, 1 when SETUP fails, 130 on Ctrl-C at a prompt."""
        self._guard_loop(asyncio.get_running_loop())
        try:
            try:
                code = await self._boot()
            except asyncio.CancelledError:
                self._uncancel()
                return 130
            if code is not None:
                # Every EXIT edge prints the footer and bye, SETUP's included (Section 4).
                self.state = State.EXIT
                self._footer()
                self._write("bye\n")
                return code
            while self.state is not State.EXIT and not self._quit:
                try:
                    line = await self._read(self._prompt())
                    if line is None:
                        break
                    await self.handle(line)
                except asyncio.CancelledError:
                    self._uncancel()
                    self._fail("interrupted", hint="")
                    self._exit_code = 130
                    break
            self.state = State.EXIT
            self._footer()
            self._write("bye\n")
            return self._exit_code
        except KeyboardInterrupt:
            return 130
        finally:
            await self.close()

    def _guard_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Route the loop's unhandled-exception reports through the closed code table.

        ``asyncio.run`` reports every task that ended with an exception when it shuts the
        loop down (``Runner.close`` after a second Ctrl-C, for one), and the default
        handler prints the exception's text. The session's handler prints ``code_for``
        of it and nothing else; a handler installed before it (a test's spy) still runs.
        """
        previous = loop.get_exception_handler()

        def handler(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            exc = context.get("exception")
            code = code_for(exc) if isinstance(exc, BaseException) else "command_failed"
            with contextlib.suppress(Exception):
                self._fail(code, hint="")
            if previous is not None:
                previous(loop, context)

        loop.set_exception_handler(handler)

    async def handle(self, line: str) -> None:
        """One console line; expected failures are printed as codes, never raised."""
        cmd = parse(line, self.state)
        if cmd is None:
            return
        try:
            await self._dispatch(cmd)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_exception(exc)

    def token(self) -> str | None:
        if self.state in (State.REVIEW, State.FLOOR, State.COMPILING, State.INFERRING):
            return self.draft.hash8 if self.draft is not None else None
        if self.state is State.RESULT and self.outcome is not None:
            return render.hash8(self.outcome.result.spec.spec_hash)
        return None

    def approved(self, spec: Spec) -> bool:
        """True only for the Draft whose exact hash the operator typed; edits break it."""
        return (
            self._approved_hash is not None
            and self._approved_hash == spec.spec_hash
            and self._approved_draft is self.draft
        )

    async def close(self) -> None:
        work = self._work
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        self._work = None
        if self.services is not None and not self._closed:
            self._closed = True
            self.services.close()

    # ---------------------------------------------------------------- console helpers

    def _write(self, text: str) -> None:
        self.console.write(text)

    def _line(self, text: str) -> None:
        self._write(render.wrap(text) + "\n")

    def _session_view(self) -> Spend:
        return replace(self.session_spend, wall_s=max(0.0, self._clock() - self._started))

    def _footer(self) -> None:
        self._write(render.footer(self._session_view(), self._compile_spend))

    def _screen(self, text: str) -> None:
        self._write(text)
        self._footer()

    def _fail(self, code: str, hint: str | None = None, fields: Sequence[str] = ()) -> None:
        spec = self.draft.spec if self.draft is not None else None
        self._write(render.failure(code, hint_for(code, spec) if hint is None else hint))
        if fields:
            self._line("  fields: " + ", ".join(fields))

    def _fail_exception(self, exc: BaseException) -> None:
        fields = exc.fields if isinstance(exc, SessionError) else ()
        self._fail(code_for(exc), fields=fields)

    async def _read(self, prompt: str) -> str | None:
        if self._pending is not None:
            # A line held while work ran is replayed at the prompt it was meant for; the
            # transcript marked it "held" where it was read, so the echo here shows where
            # it took effect.
            line, self._pending = self._pending, None
            self._write(prompt)
            if not self.console.interactive:
                self._write(line + "\n")
            return line
        return await self.console.read(prompt)

    async def _confirm(self, prompt: str, *, default: bool) -> bool:
        answer = await self._read(prompt)
        if answer is None:
            return False
        text = answer.strip().lower()
        if not text:
            return default
        return text in {"y", "yes"}

    @staticmethod
    def _uncancel() -> None:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()

    def _prompt(self) -> str:
        state = self.state
        token = self.token() or "--------"
        if state is State.REVIEW:
            return f"review {token}> "
        if state is State.COMPILING:
            return f"compiling {token}> "
        if state is State.RESULT:
            return f"result {token}> "
        if state is State.FLOOR:
            dataset = self.floor.dataset if self.floor is not None else "?"
            return f"floor {dataset}> "
        if state is State.INFERRING:
            # Section 4 fixes ten prompts and none for INFERRING: the reader keeps the
            # prompt of the state the inference was started from (READY or REVIEW).
            return self._busy_prompt
        if state is State.SETUP:
            return "setup> "
        if state is State.BENCHING:
            return "bench> "
        return "architect> "

    # ---------------------------------------------------------------- service helpers

    def _services(self) -> Services:
        if self.services is None:
            raise SessionError("invalid_profile")
        return self.services

    def _profile(self) -> Profile:
        return self._services().profile

    def _compile_model(self, profile: Profile) -> str:
        ladder = profile.compile.model_ladder
        return ladder[0] if ladder else next(iter(profile.models))

    def _rebuild_services(self) -> None:
        """Load the profile and build fresh Services; the old ones close only after that.

        A rebuild that fails (a rewrite the profile loader rejects, an adapter that cannot
        be built) leaves the session on the Services it had, so ``use model``, ``budget
        compile`` and floor ``accept`` degrade to one printed code instead of answering
        ``invalid_profile`` until quit. Two stores are briefly open on one journal;
        ``TelemetryStore`` serialises writers through SQLite, so that is safe.
        """
        if self.profile_path is None:
            raise SessionError("invalid_profile")
        profile = load_profile(self.profile_path)
        workspace = profile.resolve(profile.workspace)
        os.environ.setdefault(SIGNING_KEY_ENV, str(workspace / "signing.key"))
        services = self._services_factory(profile, profile.workspace)
        observed = observe_services(services, self.meter)
        old, was_open = self.services, not self._closed
        self.services = observed
        self.workspace = workspace
        self._closed = False
        if old is not None and was_open:
            old.close()

    def _require_editable(self) -> Path:
        path = self.profile_path
        if path is None or path.name != SESSION_PROFILE or path.suffix.lower() != ".json":
            raise SessionError("profile_not_editable")
        return path

    def _rewrite_profile(self, mutate: Callable[[dict[str, JsonValue]], None]) -> None:
        path = self._require_editable()
        try:
            payload = _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SessionError("invalid_profile") from None
        mutate(payload)
        try:
            _atomic_write(path, json.dumps(payload, indent=2) + "\n")
        except OSError:
            raise SessionError("profile_write_failed") from None

    def _quote(self, adapter: ModelAdapter) -> float:
        try:
            return inference_quote(adapter)
        except (AdapterError, ValueError):
            return 0.0

    def _min_call_cost(self, model: str) -> float | None:
        services = self.services
        if services is None or model not in services.profile.models:
            return None
        try:
            return min_call_cost(services.adapter_factory(model))
        except (AdapterError, ProfileError, ValueError):
            return None

    def _floor_summary(self, spec: Spec) -> FloorSummary | None:
        services = self.services
        if services is None:
            return None
        try:
            return summarize(services.profile, spec.floor.dataset, spec.floor.limit)
        except SessionError:
            return None

    def _summaries(self) -> tuple[FloorSummary, ...]:
        services = self.services
        if services is None:
            return ()
        try:
            return summaries(services.profile)
        except SessionError:
            return ()

    def _blockers(self, spec: Spec) -> tuple[Blocker, ...]:
        return blockers(spec, self._profile(), self._min_call_cost(spec.model_target))

    def _account(self, before: Spend, started: float) -> Spend:
        after = self.meter.snapshot()
        spend = Spend(
            calls=after.calls - before.calls,
            tokens_in=after.tokens_in - before.tokens_in,
            tokens_out=after.tokens_out - before.tokens_out,
            cost_usd=after.cost_usd - before.cost_usd,
            wall_s=max(0.0, self._clock() - started),
        )
        self.session_spend.add(spend)
        return spend

    def _signing_key(self) -> Path:
        if self.pinned_signing_key is not None:
            return self.pinned_signing_key
        configured = os.environ.get(SIGNING_KEY_ENV)
        return Path(configured) if configured else self.workspace / "signing.key"

    # -------------------------------------------------------------------- BOOT / SETUP

    async def _boot(self) -> int | None:
        path = discover_profile(self._cwd, self.workspace, self._explicit_profile)
        new = "" if self.workspace.is_dir() else " (new)"
        shown = str(path) if path is not None else "none"
        self._line(f"{VERSION_LINE}  workspace {self.workspace}{new}  profile {shown}")
        if path is None:
            return await self._setup()
        self.profile_path = path
        try:
            self._rebuild_services()
        except (ProfileError, RegistryError, OSError) as exc:
            self._fail(code_for(exc))
            self._line(f"  profile {path}")
            return 1
        profile = self._profile()
        model = self._compile_model(profile)
        config = profile.models.get(model)
        provider = config.provider if config is not None else "?"
        inference = profile.inference.model if profile.inference is not None else "none"
        budget = profile.compile.compile_budget
        self._line(
            f"model {model} via {provider}  inference {inference}  compile budget "
            f"{render.money(budget.cost_usd)} / {_budget_time(budget.compute_s)}"
        )
        self._enter_ready()
        return None

    async def _setup(self) -> int | None:
        self.state = State.SETUP
        self._line(
            "Nothing printed by this session is a license or an approval. Costs are "
            "API-equivalent estimates, not a bill."
        )
        candidates = self._detect()
        found: dict[str, CliCandidate] = {c.provider: c for c in candidates}
        parts: list[str] = []
        for provider, name in _CLI_NAMES.items():
            candidate = found.get(provider)
            parts.append(
                f"{name} found ({candidate.executable})" if candidate else f"{name} not found"
            )
        self._line("looking for logged-in CLIs on PATH: " + ", ".join(parts))
        rows = model_rows(candidates)
        if not rows:
            self._line(NO_PROVIDER_TEXT)
            self._fail("no_provider", hint="")
            return 1
        while True:
            self._write(render.model_menu(rows))
            row = await self._choose_model(rows, single=len(candidates) == 1)
            if row is None:
                return 0
            inference = choose_inference(row.model, candidates, rows)
            if inference is not None and inference != row.model:
                self._line(
                    f"note: {row.model} reserves {render.money(self._row_quote(row))} per "
                    "inference call, above the fixed $1.00 Stage-0 budget; Specs will be "
                    f"inferred with {inference} and compiled for {row.model}"
                )
            payload = profile_payload(row, inference, candidates)
            path = self.workspace / SESSION_PROFILE
            summary = render.profile_summary(profile_from_payload(payload, self.workspace), path)
            self._write(summary)
            self._line(COMPILE_BUDGET_NOTE)
            answer = await self._read(f"confirm> write {path}? [Y/n] ")
            if answer is None:
                return 0
            if answer.strip().lower() in {"n", "no"}:
                continue
            break
        if self._probe:
            code = await self._run_probe(row, payload)
            if code is not None:
                return code
        write_profile(payload, path)
        self._line(f"profile written {path} (edit it to add models, floors or budgets)")
        self.profile_path = path
        try:
            self._rebuild_services()
        except (ProfileError, RegistryError, OSError) as exc:
            self._fail(code_for(exc))
            return 1
        self._enter_ready(first_run=True)
        return None

    def _row_quote(self, row: ModelRow) -> float:
        """The per-inference-call reservation ``can_infer`` was judged on (Section 6)."""
        return self._quote(_adapter(row.provider, row.model, row.executable))

    async def _choose_model(self, rows: Sequence[ModelRow], *, single: bool) -> ModelRow | None:
        if single and not self.console.interactive:
            return rows[0]
        while True:
            answer = await self._read("model> ")
            if answer is None:
                return None
            text = answer.strip()
            if text.lower() == "q":
                return None
            if not text:
                return rows[0]
            for row in rows:
                if text == str(row.index) or text == row.model:
                    return row
            self._fail("model_not_configured", hint="")
            self._write(render.model_menu(rows))

    async def _run_probe(self, row: ModelRow, payload: dict[str, JsonValue]) -> int | None:
        cli = _CLI_NAMES.get(row.provider, row.provider)
        self._line(
            f"checking {row.provider} login with one tiny metered call (about "
            f"{render.money(row.probe_cost)} reserved, typically under $0.01, up to 90 s)..."
        )
        if not row.probe_fits:
            self._line(f"{cli}: probe quote exceeds the $1.00 control budget; skipped")
            return None
        profile = profile_from_payload(payload, self.workspace)
        try:
            services = self._services_factory(profile, profile.workspace)
        except (ProfileError, RegistryError, OSError) as exc:
            self._fail(code_for(exc))  # Nothing is written; the operator can start again.
            return 1
        try:
            adapter = services.adapter_factory(row.model)
            result = await self._prober(adapter, services.runtime)
        finally:
            services.close()
        self.session_spend.add(result.spend)
        status = result.status
        if status in {"ok", "over_budget"}:
            spend = result.spend
            if status == "ok":
                self._line(
                    f"{cli}: logged in ({render.duration(spend.wall_s)}, "
                    f"{render.money(spend.cost_usd)})"
                )
            else:
                self._line(f"{cli}: probe quote exceeds the $1.00 control budget; skipped")
            return None
        if status == "not_logged_in":
            self._line(f"{cli}: not logged in - run '{Path(row.executable).name}' once and sign in")
            self._fail("provider_not_logged_in", hint="")
        elif status == "unavailable":
            self._line(f"{cli}: unavailable")
            self._fail("provider_unavailable", hint="")
        else:
            self._fail("provider_failed", hint="")
        return 1

    # -------------------------------------------------------------------- dispatching

    async def _dispatch(self, cmd: Command) -> None:
        state = self.state
        if cmd.verb in GLOBAL and cmd.verb in globals_for(state):
            await self._global(cmd)
            return
        if cmd.verb == "text":
            # Prose is a paid action only in READY, and only after the infer? question.
            if state is State.READY:
                await self._ready(cmd)
            else:
                self._disallowed(cmd)
            return
        if not allowed(cmd, state):
            self._disallowed(cmd)
            return
        if state is State.READY:
            await self._ready(cmd)
        elif state is State.REVIEW:
            await self._review(cmd)
        elif state is State.FLOOR:
            await self._floor(cmd)
        elif state is State.RESULT:
            await self._result(cmd)
        elif state in (State.INFERRING, State.COMPILING, State.BENCHING):
            self._busy_command(cmd)
        else:
            self._disallowed(cmd)

    def _disallowed(self, cmd: Command) -> None:
        state = self.state
        hint: str | None = None
        if state is State.INFERRING:
            code = "busy_inferring"
        elif state is State.COMPILING:
            code = "busy_compiling"
        elif state is State.BENCHING:
            code = "busy_benching"
        elif cmd.verb == "text":
            code = "not_a_command"
            if state is State.REVIEW:
                hint = "revise <text>"
        elif state is State.READY and cmd.verb in _DRAFT_ONLY:
            code = "no_draft"
        elif cmd.verb in _RESULT_ONLY:
            code = "no_result"
        else:
            code = "not_a_command"
            if state is State.RESULT and cmd.verb == "forge":
                # Forge modes run from REVIEW; the approval of this Spec is kept there.
                hint = "again, then forge run {json} | forge integrate <repo> <into>"
        self._fail(code, hint=hint)
        if cmd.verb == "text" and state not in (
            State.INFERRING,
            State.COMPILING,
            State.BENCHING,
        ):
            return
        verbs = sorted(VOCABULARY[state] | globals_for(state))
        self._line("allowed here: " + ", ".join(verbs))

    def _busy_command(self, cmd: Command) -> None:
        if cmd.verb == "cancel":
            self._cancel_work()

    def _cancel_work(self) -> bool:
        """Cancel the running task, if any, and say so; the one place that line is printed.

        False, silently, when nothing runs or the task is already cancelling: a Ctrl-C on
        the heels of a typed quit, or an EOF and a Ctrl-C from one keystroke, cancel once.
        """
        work = self._work
        if work is None or work.done() or work.cancelling():
            return False
        work.cancel()
        self._fail("cancelled", hint="")
        return True

    async def _global(self, cmd: Command) -> None:
        verb = cmd.verb
        if verb == "help":
            self._write(help_for(cmd.args[0], self.state) if cmd.args else HELP[self.state])
        elif verb == "status":
            self._screen(render.status_screen(self._status_view()))
            if self.state in (State.COMPILING, State.BENCHING) and self._last_progress:
                self._write(self._last_progress)
        elif verb == "spend":
            self._footer()
        elif verb == "profile":
            self._profile_screen()
        else:  # quit / exit
            self._quit = True
            self._cancel_work()

    def _status_view(self) -> StatusView:
        services = self.services
        profile = services.profile if services is not None else None
        model = self._compile_model(profile) if profile is not None else None
        inference = profile.inference.model if profile and profile.inference else None
        budget: Budget = (
            profile.compile.compile_budget if profile else CompileConfig().compile_budget
        )
        return StatusView(
            state=self.state,
            workspace=self.workspace,
            profile_path=self.profile_path,
            model=model,
            inference_model=inference,
            floors=self._summaries(),
            compile_budget=budget,
            draft=self.draft,
            outcome=self.outcome,
            last_entry=self._last_entry,
            session_spend=self._session_view(),
            compile_spend=self._compile_spend,
            signing_key=self._signing_key(),
            bench=self.bench_status if self.state is State.BENCHING else None,
            forge=self.forge_line,
        )

    def _profile_screen(self) -> None:
        services = self._services()
        profile = services.profile
        lines = [f"profile {self.profile_path or 'none'}", f"workspace {self.workspace}"]
        for model, config in profile.models.items():
            cost = self._min_call_cost(model)
            can_infer = "no"
            try:
                adapter = services.adapter_factory(model)
                can_infer = "yes" if self._quote(adapter) <= _INFERENCE_BUDGET.cost_usd else "no"
            except (AdapterError, ProfileError, ValueError):
                pass
            lines.append(
                f"model {model}  provider {config.provider}  executable "
                f"{config.executable or '-'}  min reservation "
                f"{render.money(cost) if cost is not None else '-'}  can infer {can_infer}"
            )
        self._write(render.wrap("\n".join(lines)) + "\n")
        self._write(render.floors_screen(self._summaries()))
        budget = profile.compile.compile_budget
        self._line(
            f"compile budget {render.money(budget.cost_usd)} / {_budget_time(budget.compute_s)}"
        )
        self._line(f"signing key {self._signing_key()}")
        self._footer()

    # ------------------------------------------------------------------- state entry

    def _enter_ready(self, *, first_run: bool = False) -> None:
        self.state = State.READY
        self.floor = None
        if first_run:
            profile = self._profile()
            model = profile.inference.model if profile.inference else self._compile_model(profile)
            self._line(
                "Describe what the agent should do. I will propose a Spec; nothing is compiled "
                f"until you approve it. Text you type here is sent to {model}; do not paste "
                "secrets."
            )
            return
        count = 0
        services = self.services
        if services is not None:
            try:
                count = len(services.registry.list())
            except RegistryError:
                count = 0
        self._line(f"Describe what the agent should do, or type help. Registry: {count} installed.")

    def _enter_review(self) -> None:
        self.state = State.REVIEW
        self.floor = None
        self._show_spec()
        self._route_line()

    def _route_line(self) -> None:
        """Phase 10 Section 13.2: one advice line, when the profile routes compiles.

        Pure: nothing is recorded, run, approved or compiled; any routing failure shows
        nothing (P5). A profile without a ``routing`` table keeps the Phase 9 screen.
        """
        services = self.services
        draft = self.draft
        if services is None or draft is None or getattr(services, "router", None) is None:
            return
        if not getattr(services.compiler, "route_all", False):
            return
        try:
            from .routes import advice, deliver_command, route_line

            spec = draft.spec
            decision = advice(
                services, spec, origin="session", forbid_reuse=draft.origin == "registry"
            )
            line = route_line(decision, hash8=spec.spec_hash[:8], deliver=deliver_command(spec))
        except Exception:
            return
        self._routed_reuse = None
        if decision.action == "reuse_installed":
            from .routes import target_label

            self._routed_reuse = (spec.spec_hash, target_label(decision))
        self._line(line)

    def _show_spec(self, *, with_blockers: bool = True) -> None:
        draft = self.draft
        if draft is None:
            self._fail("no_draft")
            return
        spec = draft.spec
        found = self._blockers(spec) if with_blockers else ()
        self._screen(
            render.spec_screen(
                draft,
                found,
                floor=self._floor_summary(spec),
                min_call_cost=self._min_call_cost(spec.model_target),
                stage0=self._stage0,
                approved=self.approved(spec),
            )
        )

    def _enter_result(self) -> None:
        self.state = State.RESULT
        self._show_result()

    def _show_result(self) -> None:
        outcome = self.outcome
        if outcome is None:
            self._fail("no_result")
            return
        spec = outcome.result.spec
        self._screen(render.result_screen(outcome, self._floor_summary(spec), spec))

    # -------------------------------------------------------------------- busy driving

    async def _await_work(self, work: asyncio.Task[_T]) -> None:
        """Drive the console until ``work`` finishes or is cancelled by the operator."""
        self._work = cast(asyncio.Task[object], work)
        read_task: asyncio.Task[str | None] | None = None
        eof = interrupted = False
        try:
            while not work.done():
                # The reader is re-armed only while lines can still be applied: not after
                # a held line (its successors are meant for later states) and not after
                # quit or EOF (the work is being cancelled; a sticky EOF would spin).
                if read_task is None and not (self._hold or self._quit or eof):
                    read_task = asyncio.create_task(self.console.read(self._prompt()))
                waiting: set[asyncio.Future[object]] = {cast(asyncio.Future[object], work)}
                if read_task is not None:
                    waiting.add(cast(asyncio.Future[object], read_task))
                try:
                    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    # Ctrl-C while work runs cancels the work, not the session.
                    self._uncancel()
                    interrupted = True
                    self._cancel_work()
                    continue
                if read_task is not None and read_task in done:
                    line = read_task.result()
                    read_task = None
                    if line is None:
                        # EOF, piped or interactive: cancel the paid work (Section 5.1); a
                        # script never keeps spending past its last line.
                        eof = True
                        self._cancel_work()
                    else:
                        await self._busy_line(line)
        finally:
            self._work = None
            self._hold = False
            if eof and not interrupted:
                # EOF ends the session, unless a Ctrl-C came with it: a console that
                # cannot tell an aborted read from end of input reports both, and the
                # contract for Ctrl-C (Section 4) is REVIEW with the approval kept. A
                # real EOF is read again at the next prompt and ends the session there.
                self._quit = True
            if not work.done():
                # Only a console failure gets here; never leave paid work running unwatched.
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if read_task is not None:
                if read_task.done():
                    late = read_task.result()
                    if late is not None:
                        self._pending = late
                else:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)

    async def _busy_line(self, line: str) -> None:
        cmd = parse(line, self.state)
        interactive = self.console.interactive
        if cmd is None:
            if not interactive:
                self._hold_line(line)
            return
        if allowed(cmd, self.state):
            # Busy-state verbs are answered as they arrive on every console; quit and exit
            # are among them and cancel the work (Sections 5.1 and 5.5).
            await self.handle(line)
        elif not interactive:
            # A transcript's other lines are meant in order: hold this one for the next
            # state and read no further until the work has finished.
            self._hold_line(line)
        else:
            self._disallowed(cmd)

    def _hold_line(self, line: str) -> None:
        """Keep a scripted line for the next prompt and say so where it was read."""
        self._pending = line
        self._hold = True
        self._line(HELD_NOTE)

    # --------------------------------------------------------------------- INFERRING

    def _initial_overrides(self) -> dict[str, JsonValue]:
        """The compile model when the ladder has exactly one; never a floor.

        The profile's only floor is not bound here: a goal about another dataset would be
        verified against the wrong cases without anyone noticing. The inferred dataset
        stays on the Spec and ``floor_not_configured`` names the configured ones.
        """
        profile = self._profile()
        overrides: dict[str, JsonValue] = {}
        if len(profile.compile.model_ladder) == 1:
            overrides["model_target"] = profile.compile.model_ladder[0]
        return overrides

    def _session_profile(self) -> bool:
        path = self.profile_path
        return path is not None and path.name == SESSION_PROFILE

    def _normalize_schemas(self, spec: Spec) -> Spec:
        """Wrap bare property maps a model proposed as schemas into JSON Schema objects.

        Applied only to what Stage 0 just returned (``describe`` and ``revise``); Specs
        from ``spec``, ``load`` and ``recompile`` are left exactly as written. The field
        keeps provenance ``inferred`` (the operator did not type it) and one line per
        changed field says what happened; the token is computed from the wrapped Spec.
        """
        dump = spec.model_dump(mode="json")
        changed: list[tuple[str, list[str]]] = []
        for field in ("input_schema", "output_schema"):
            value, wrapped = normalize_schema(dump[field])
            if not wrapped or not isinstance(value, dict):
                continue
            dump[field] = value
            properties = value.get("properties")
            changed.append((field, sorted(properties) if isinstance(properties, dict) else []))
        if not changed:
            return spec
        normalized = Spec.model_validate_json(json.dumps(dump, allow_nan=False), strict=True)
        for field, names in changed:
            listed = ", ".join(render.ascii_text(name) for name in names)
            self._line(f"normalized {field} into a JSON Schema object (properties: {listed})")
        return normalized

    def _lift_budget(self, spec: Spec, overrides: dict[str, JsonValue]) -> Spec:
        """Lift an inferred per-run budget to two calls of its model on a session profile.

        Models propose ``budget.cost_usd`` without knowing the reservation a CLI model
        needs per call, so on a profile this session wrote the value is lifted through
        the same edit path as ``set budget.cost_usd`` (provenance ``user``, kept in
        ``overrides`` so a revise proposes it again). One note line says so and how to
        change it. Specs from ``spec``, ``load`` or ``recompile`` are left as loaded and
        keep the ``budget_below_quote`` blocker; the compile wallet is never touched.
        """
        if not self._session_profile():
            return spec
        cost = self._min_call_cost(spec.model_target)
        if cost is None or spec.budget.cost_usd >= cost:
            return spec
        raised = suggested_cost(cost, 2)
        try:
            lifted, top = set_field(spec, "budget.cost_usd", raised)
        except SessionError:
            return spec
        overrides.update(top)
        # model_target is a key of the trusted profile here (min_call_cost was quoted).
        self._line(
            f"budget.cost_usd set to {render.money(raised)} so 2 calls of "
            f"{spec.model_target} fit; change with set budget.cost_usd <usd>"
        )
        return lifted

    async def _run_stage0(
        self, text: str, adapter: ModelAdapter, overrides: dict[str, JsonValue]
    ) -> tuple[Spend, Spec | None, str | None]:
        """One metered inference call; returns (spend, spec, failure code)."""
        runtime = self._services().runtime
        started = self._clock()
        before = self.meter.snapshot()
        work = asyncio.create_task(infer_spec(text, adapter, runtime, dict(overrides)))
        await self._await_work(work)
        spend = self._account(before, started)
        if work.cancelled():
            return spend, None, "cancelled"
        exc = work.exception()
        if exc is not None:
            return spend, None, code_for(exc)
        return spend, work.result().spec, None

    def _inference_adapter(self, services: Services) -> ModelAdapter | None:
        """The ``inference`` role (Section 11.3): pinned by ``use model``; routed only
        under ``roles.policy = "evidence"``; otherwise exactly today's adapter."""
        adapter = services.compiler.inference_adapter
        roles = getattr(services, "roles", None)
        if self.inference_pinned or roles is None or roles.policy != "evidence":
            return adapter
        try:
            chosen = roles.adapter("inference")
        except Exception:
            return adapter  # P5: role routing never blocks Stage 0.
        return chosen if chosen is not None else adapter

    async def _infer(self, goal: str, overrides: dict[str, JsonValue]) -> None:
        services = self._services()
        adapter = self._inference_adapter(services)
        if adapter is None:
            self._fail("no_inference_model")
            return
        self.state = State.INFERRING
        self._busy_prompt = "architect> "
        text = goal
        for attempt in range(2):
            self._line(
                f"inferring Spec with {adapter.model} (one call, reserved "
                f"{render.money(self._quote(adapter))}) ..."
            )
            spend, spec, code = await self._run_stage0(text, adapter, overrides)
            if spec is not None:
                self._line(
                    f"stage 0 done in {render.duration(spend.wall_s)}, "
                    f"{render.money(spend.cost_usd)}"
                )
                spec = self._lift_budget(self._normalize_schemas(spec), overrides)
                self._stage0 = spend
                self.draft = Draft(
                    spec=spec,
                    origin="inferred",
                    goal=text,
                    user_fields=dict(overrides),
                    original=spec,
                )
                if self._quit:
                    self.state = State.EXIT
                    return
                self._enter_review()
                return
            if code != "cancelled":
                # Whoever cancelled (cancel, quit, EOF, Ctrl-C) already printed the code.
                self._fail(code or "inference_failed", hint="")
            if self._quit:
                self.state = State.EXIT
                return
            if code != "inference_failed" or attempt == 1 or self._pending_command():
                # A command a script typed ahead is meant for the next prompt; it is never
                # prompt text for a paid retry the script did not ask for (Section 5.8).
                break
            self._line(CLARIFY_HINT)
            answer = await self._read("clarify> ")
            if answer is None or not answer.strip():
                break
            text = text + "\n\nClarification: " + answer.strip()
        self._enter_ready()

    def _pending_command(self) -> bool:
        pending = self._pending
        if pending is None:
            return False
        cmd = parse(pending, State.READY)
        return cmd is not None and cmd.verb != "text"

    async def _revise(self, text: str) -> None:
        draft = self.draft
        if draft is None:
            self._fail("no_draft")
            return
        if draft.goal is None:
            self._fail("no_goal")
            return
        services = self._services()
        adapter = self._inference_adapter(services)
        if adapter is None:
            self._fail("no_inference_model")
            return
        # Section 5.3: every revise sends the ORIGINAL prose plus this one revision;
        # draft.goal is never rewritten, so revisions do not accumulate.
        goal = draft.goal + "\n\nRevision: " + text
        self.state = State.INFERRING
        self._busy_prompt = f"review {draft.hash8}> "
        self._line(
            f"inferring Spec with {adapter.model} (one call, reserved "
            f"{render.money(self._quote(adapter))}) ..."
        )
        spend, spec, code = await self._run_stage0(goal, adapter, draft.user_fields)
        self.state = State.EXIT if self._quit else State.REVIEW
        if spec is None:
            if code != "cancelled":
                self._fail(code or "inference_failed", hint="")
            return
        self._line(
            f"stage 0 done in {render.duration(spend.wall_s)}, {render.money(spend.cost_usd)}"
        )
        spec = self._lift_budget(self._normalize_schemas(spec), draft.user_fields)
        self._stage0 = spend
        before = draft.spec
        draft.spec = spec
        draft.edits += 1
        self._write(render.diff_screen(diff_lines(before, spec), draft.hash8))

    # ------------------------------------------------------------------------- READY

    async def _ready(self, cmd: Command) -> None:
        verb = cmd.verb
        services = self._services()
        if verb == "text":
            adapter = services.compiler.inference_adapter
            if adapter is None:
                self._fail("no_inference_model")
                return
            prompt = (
                f"infer? infer a Spec from this text with {adapter.model} (reserves "
                f"{render.money(self._quote(adapter))}, typically a few cents)? [Y/n] "
            )
            if await self._confirm(prompt, default=True):
                await self._infer(cmd.rest, self._initial_overrides())
            else:
                self._line("not inferred; type help for commands")
        elif verb == "describe":
            if not cmd.rest:
                self._fail("not_a_command", hint="describe <text>")
                return
            await self._infer(cmd.rest, self._initial_overrides())
        elif verb in {"spec", "load"}:
            spec = self._load_spec(cmd.rest)
            self._stage0 = None
            self.draft = Draft(spec=spec, origin="file", goal=None, user_fields={}, original=spec)
            self._enter_review()
        elif verb in REGISTRY_READ:
            await self._registry_read(cmd)
        elif verb == "rollback":
            await self._rollback(cmd)
        elif verb == "recompile":
            if not cmd.args:
                self._fail("not_a_command", hint="recompile <name>")
                return
            incumbent = services.registry.get(cmd.args[0])
            self._stage0 = None
            self.draft = Draft(
                spec=incumbent.spec,
                origin="registry",
                goal=None,
                user_fields={},
                original=incumbent.spec,
            )
            self._enter_review()
        elif verb == "floor":
            if not cmd.args:
                self._fail("not_a_command", hint="floor <dataset> [kind]")
                return
            kind_text = cmd.args[1] if len(cmd.args) > 1 else FloorKind.HELD_OUT_TASKS.value
            try:
                kind = FloorKind(kind_text)
            except ValueError:
                raise SessionError("floor_kind_unsupported_in_session") from None
            self._enter_floor(cmd.args[0], kind, State.READY)
        elif verb == "use":
            if len(cmd.args) == 2 and cmd.args[0] == "model":
                self._use_model(cmd.args[1])
            else:
                self._fail("not_a_command", hint="use model <id>")
        elif verb == "budget":
            self._budget_compile(cmd)
        elif verb == "bench":
            from .bench import handle_bench

            await handle_bench(self, cmd)
        elif verb == "roles":
            from .delivery import roles_verb

            roles_verb(self)
        elif verb == "rate":
            from .delivery import rate_verb

            rate_verb(self, cmd)
        else:
            self._disallowed(cmd)

    async def _registry_read(self, cmd: Command) -> None:
        """``list``, ``info``, ``floors`` and ``run <name|dir> {json}``: disk only, no Draft.

        Shared by READY, REVIEW and RESULT so a Spec under review or a fresh result can
        be compared with what is installed without leaving the state. ``run`` here
        always names an installed artifact or a directory; the RESULT-state
        ``run {json}`` on the compiled artifact is dispatched before this is reached.
        """
        services = self._services()
        verb = cmd.verb
        if verb == "list":
            rows = [(entry, self._licensed_on_disk(entry)) for entry in services.registry.list()]
            self._screen(render.registry_screen(rows))
        elif verb == "info":
            if not cmd.args:
                self._fail("not_a_command", hint="info <name>")
                return
            self._show_entry(services.registry.status(cmd.args[0]))
        elif verb == "floors":
            self._screen(render.floors_screen(summaries(services.profile)))
        elif verb == "run":
            reference, text = split_first(cmd.rest)
            if not reference or reference[0] in "{[":
                self._fail("not_a_command", hint="run <name|dir> {json}")
                return
            await self._run_artifact(self._load_artifact(reference), text)
        else:
            self._disallowed(cmd)

    def _load_spec(self, text: str) -> Spec:
        value = text.strip()
        if not value:
            raise SessionError("invalid_spec_file")
        if value.startswith("{"):
            try:
                return Spec.model_validate_json(value)
            except ValueError:
                raise SessionError("invalid_spec") from None
        try:
            return Spec.model_validate_json((self._cwd / value).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SessionError("invalid_spec_file") from None

    @staticmethod
    def _licensed_on_disk(entry: RegistryEntry) -> bool:
        try:
            return HarnessArtifact.load(entry.directory).licensed
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _load_from_disk(directory: Path) -> HarnessArtifact:
        try:
            return HarnessArtifact.load(directory)
        except (OSError, ValueError, TypeError):
            raise SessionError("unlicensed_artifact") from None

    def _load_artifact(self, reference: str) -> HarnessArtifact:
        """A registry name or an artifact directory; always read from disk."""
        path = self._cwd / reference
        if path.is_dir():
            return self._load_from_disk(path)
        return self._services().registry.get(reference)

    def _show_entry(self, entry: RegistryEntry) -> None:
        artifact = self._load_from_disk(entry.directory)
        self._last_entry = entry
        self._screen(render.entry_screen(entry, artifact, self._floor_summary(artifact.spec)))

    async def _rollback(self, cmd: Command) -> None:
        registry = self._services().registry
        if not cmd.args:
            self._fail("not_a_command", hint="rollback <name> [rev]")
            return
        name = cmd.args[0]
        if len(cmd.args) == 1:
            current = registry.status(name)
            if current.revision <= 1:
                raise RegistryError("no_previous_revision")
            previous = current.revision - 1
            self._line(
                f"To roll back {name} from revision {current.revision} to {previous} type: "
                f"rollback {name} {previous}"
            )
            return
        try:
            revision = int(cmd.args[1])
        except ValueError:
            raise RegistryError("invalid_revision") from None
        self._show_entry(registry.rollback(name, revision))

    def _parse_input(self, text: str) -> JsonValue:
        try:
            task: JsonValue = _JSON.validate_json(text)
            json.dumps(task, allow_nan=False)
        except (ValueError, TypeError):
            raise SessionError("invalid_input_json") from None
        return task

    async def _run_artifact(self, artifact: HarnessArtifact, text: str) -> None:
        task = self._parse_input(text)
        services = self._services()
        expire = artifact.spec.lifecycle is Lifecycle.EXPIRE
        if expire and (artifact.directory / ".consumed").exists():
            raise ArtifactExpiredError("Artifact execution attempt already consumed")
        fresh = self._load_from_disk(artifact.directory)
        if not fresh.licensed:
            raise SessionError("unlicensed_artifact")
        if expire:
            self._line("this artifact expires after this one attempt, even on failure")
        adapter = services.adapter_factory(fresh.model)
        try:
            digest: str | None = fresh.artifact_digest  # before an expire run consumes it
        except (OSError, ValueError, TypeError):
            digest = None
        started = self._clock()
        before = self.meter.snapshot()
        try:
            run = await services.runtime.run(fresh, task, adapter)
        finally:
            self._account(before, started)
        if digest is not None:
            self.last_delivery = (run.run_id, digest)
        self._screen(render.run_screen(run, fresh.model, expired_after=expire))

    def _use_model(self, model: str) -> None:
        self._require_editable()
        services = self._services()
        if model not in services.profile.models:
            raise SessionError("model_not_configured")
        can_infer = self._quote(services.adapter_factory(model)) <= _INFERENCE_BUDGET.cost_usd

        def mutate(payload: dict[str, JsonValue]) -> None:
            compile_cfg = payload.get("compile")
            if not isinstance(compile_cfg, dict):
                compile_cfg = {}
                payload["compile"] = compile_cfg
            compile_cfg["model_ladder"] = [model]
            models = payload.get("models")
            config = models.get(model) if isinstance(models, dict) else None
            if can_infer and isinstance(config, dict):
                payload["inference"] = {**config, "model": model}
            else:
                payload.pop("inference", None)

        self._rewrite_profile(mutate)
        self.inference_pinned = True  # Section 11.3: ``use model`` pins the inference role
        if not can_infer:
            self._line(
                f"note: {model} cannot infer Specs under the fixed $1.00 Stage-0 budget; "
                "describe is unavailable until a claude-code model is present"
            )
        self._rebuild_services()
        self._line(f"model {model} selected for this session; profile rewritten")

    def _budget_screen(self, spec: Spec) -> None:
        self._screen(
            render.budget_screen(
                spec,
                self._profile(),
                self._min_call_cost(spec.model_target),
                self._floor_summary(spec),
            )
        )

    def _budget_compile(self, cmd: Command) -> None:
        """``budget compile <usd> <seconds>``: the only in-session change to the wallet."""
        if len(cmd.args) != 3 or cmd.args[0] != "compile":
            self._fail("not_a_command", hint="budget compile <usd> <seconds>")
            return
        try:
            usd = float(cmd.args[1])
            seconds = float(cmd.args[2])
        except ValueError:
            raise SessionError("invalid_value_json") from None
        if not (usd > 0 and seconds > 0 and usd < float("inf") and seconds < float("inf")):
            raise SessionError("invalid_value_json")

        def mutate(payload: dict[str, JsonValue]) -> None:
            compile_cfg = payload.get("compile")
            if not isinstance(compile_cfg, dict):
                compile_cfg = {}
                payload["compile"] = compile_cfg
            compile_cfg["compile_budget"] = {"cost_usd": usd, "compute_s": seconds}

        self._rewrite_profile(mutate)
        self._rebuild_services()
        self._line(f"compile budget {render.money(usd)} / {_budget_time(seconds)}")

    # ------------------------------------------------------------------------ REVIEW

    def _challenge(self, spec: Spec) -> None:
        if spec.lifecycle is not Lifecycle.INSTALL:
            self._line("this Spec has lifecycle expire and needs no approval; type compile")
            return
        self._line(
            f"To approve and compile exactly this Spec (spec_hash {spec.spec_hash}), type: "
            f"approve {render.hash8(spec.spec_hash)}"
        )

    def _refused(self, spec: Spec) -> bool:
        blocking = [blocker for blocker in self._blockers(spec) if blocker.blocking]
        if not blocking:
            return False
        self._line("cannot approve: " + ", ".join(blocker.code for blocker in blocking))
        for blocker in blocking:
            self._line(f"  fix: {blocker.fix}")
            if blocker.alternative is not None:
                self._line(f"  or: {blocker.alternative}")
        return True

    def _apply_edit(self, before: Spec, after: Spec, top: dict[str, JsonValue]) -> None:
        draft = self.draft
        assert draft is not None
        draft.spec = after
        draft.user_fields.update(top)
        draft.edits += 1
        self._write(render.diff_screen(diff_lines(before, after), draft.hash8))

    async def _review(self, cmd: Command) -> None:
        draft = self.draft
        if draft is None:
            self._fail("no_draft")
            return
        verb = cmd.verb
        spec = draft.spec
        if verb == "text":
            self._fail("not_a_command", hint="revise <text>")
        elif verb == "show":
            self._show_spec()
        elif verb == "json":
            self._write(render.spec_json(spec) + "\n")
        elif verb == "diff":
            self._write(render.diff_screen(diff_lines(draft.original, spec), draft.hash8))
        elif verb == "blockers":
            found = self._blockers(spec)
            if not found:
                self._line("no blockers")
            for blocker in found:
                kind = "blocking" if blocker.blocking else "warning"
                self._line(f"! {blocker.code}  {kind}  fix: {blocker.fix}")
                if blocker.alternative is not None:
                    self._line(f"  or: {blocker.alternative}")
        elif verb == "budget":
            if cmd.args:
                self._fail(
                    "not_a_command",
                    hint="budget (the wallet is set from READY: budget compile <usd> <seconds>)",
                )
                return
            self._budget_screen(spec)
        elif verb == "set":
            path, value = split_first(cmd.rest)
            if not path:
                self._fail("not_a_command", hint="set <path> <value>")
                return
            after, top = set_field(spec, path, parse_value(value))
            self._apply_edit(spec, after, top)
        elif verb == "unset":
            if len(cmd.args) != 1:
                self._fail("not_a_command", hint="unset <field>")
                return
            after, top = unset_field(spec, cmd.args[0])
            self._apply_edit(spec, after, top)
        elif verb == "revise":
            if not cmd.rest:
                self._fail("not_a_command", hint="revise <text>")
                return
            await self._revise(cmd.rest)
        elif verb == "floor":
            self._enter_floor(spec.floor.dataset, spec.floor.kind, State.REVIEW)
        elif verb == "save":
            self._save(spec, cmd.args[0] if cmd.args else None)
        elif verb == "approve":
            await self._approve(draft, cmd.args)
        elif verb == "compile":
            if spec.lifecycle is Lifecycle.INSTALL:
                self._fail("approval_token_required", hint="")
                self._challenge(spec)
                return
            if self._refused(spec):
                return
            self._line(
                "lifecycle expire: the compiler needs no approval; compiling exactly Spec "
                f"{draft.hash8}"
            )
            await self._compile()
        elif verb == "discard":
            if draft.edits > 0 and not await self._confirm(
                f"confirm> discard Spec {draft.hash8}? [y/N] ", default=False
            ):
                return
            self._drop_draft()
            self._enter_ready()
        elif verb == "forge":
            from .forge import handle_forge

            await handle_forge(self, cmd)
        elif verb == "route":
            from .delivery import route_verb

            route_verb(self, spec)
        elif verb == "reuse":
            from .delivery import reuse_verb

            await reuse_verb(self, spec, cmd)
        elif verb == "rate":
            from .delivery import rate_verb

            rate_verb(self, cmd)
        elif verb in REGISTRY_READ:
            await self._registry_read(cmd)
        else:
            self._disallowed(cmd)

    async def _approve(self, draft: Draft, args: tuple[str, ...]) -> None:
        spec = draft.spec
        if not args:
            self._challenge(spec)
            return
        if spec.lifecycle is not Lifecycle.INSTALL:
            self._challenge(spec)
            return
        if self._refused(spec):
            return
        if args[0] != spec.spec_hash[:8]:
            self._fail("stale_approval_token", hint="")
            self._challenge(spec)
            return
        # The single place approval is recorded: the operator typed the token of the
        # exact Spec on screen, and nothing blocks it.
        self._approved_hash = spec.spec_hash
        self._approved_draft = draft
        reuse = self._routed_reuse
        if reuse is not None and reuse[0] == spec.spec_hash:
            # A standing grant: the route line above named the installed entry.
            self._line(
                f"approved {draft.hash8} - routing to installed {reuse[1]} (no Stage 1 to 4 "
                "while it loads unchanged and licensed). Type cancel or press Ctrl-C to stop."
            )
            await self._compile(routed_to=reuse[1])
            return
        self._line(f"approved {draft.hash8} - compiling. Type cancel or press Ctrl-C to stop.")
        await self._compile()

    def _save(self, spec: Spec, target: str | None) -> None:
        if target is None:
            path = self.workspace / "proposals" / f"{spec.spec_hash}.json"
        else:
            path = self._cwd / target
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, render.spec_json(spec) + "\n")
        except OSError:
            raise SessionError("storage_error") from None
        self._line(f"saved {path}")

    def _drop_draft(self) -> None:
        self.draft = None
        self.outcome = None
        self._stage0 = None
        self._compile_spend = None
        self._last_progress = ""

    # --------------------------------------------------------------------- COMPILING

    def _on_progress(self, progress: Progress) -> None:
        """Every snapshot refreshes what ``status`` shows; the policy picks the lines."""
        if self._forge_active and progress.stage == 3:
            progress = replace(progress, stage_text="forge")  # Forge holds the Stage 3 slot
        line = render.progress_line(progress)
        self._last_progress = line
        if self._progress_policy.should_print(progress):
            self._write(line)

    async def _compile(self, *, routed_to: str | None = None) -> None:
        """Compile the Draft; ``routed_to`` names the installed entry a standing-grant
        reuse was advised to deliver (its banner promises no compile spend)."""
        draft = self.draft
        if draft is None:
            self._fail("no_draft")
            return
        services = self._services()
        spec = draft.spec
        profile = services.profile
        forge_agent = await self._forge_consent(services)
        self.state = State.COMPILING
        if routed_to is not None:
            self._write(render.route_banner(spec, profile, routed_to, self.approved(spec)))
        else:
            self._write(render.compile_banner(spec, profile, self.approved(spec)))
        self.meter.reset()
        self._progress_policy = ProgressPolicy()
        cursor = JournalCursor(services.runtime.store, spec.spec_hash)
        cursor.mark()
        stages: asyncio.Queue[int] = asyncio.Queue()
        watcher = CompileWatcher(
            meter=self.meter,
            cursor=cursor,
            budget=profile.compile.compile_budget,
            stages=stages,
            preset_names=_PRESET_NAMES,
            clock=self._clock,
            poll_s=self._poll_s,
        )
        started = self._clock()
        restore_forge = self._arm_forge(services, forge_agent)
        services.compiler.stage_listener = stages.put_nowait
        # Phase 10 P3: a session compile routes with origin ``session``, so only a
        # ``session`` grant in ``routing.reuse_origins`` reuses an installed entry here.
        # A ``recompile <name>`` draft is a fresh Stage 4 attempt and never reuses: the
        # task is created inside ``recompiling()``, so it carries the mark.
        marked = recompiling() if draft.origin == "registry" else contextlib.nullcontext()
        with marked:
            work: asyncio.Task[CompileResult] = asyncio.create_task(
                services.compiler.compile(spec, approved=self.approved(spec), origin="session")
            )
        watch = asyncio.create_task(watcher.watch(self._on_progress))
        try:
            await self._await_work(work)
        finally:
            services.compiler.stage_listener = None
            restore_forge()
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        spend = self.meter.snapshot()
        spend.wall_s = max(0.0, self._clock() - started)
        self._compile_spend = spend
        self.session_spend.add(spend)
        # The outcome is retrieved unconditionally, before any early return: a task that
        # finished with an exception in the same tick as a quit would otherwise be
        # collected unretrieved and asyncio would log the provider's text to stderr.
        cancelled = work.cancelled()
        exc = None if cancelled else work.exception()
        self._forge_outcome(
            None if cancelled or exc is not None else work.result(), cancelled=cancelled
        )
        if cancelled:
            self._line(
                f"cancelled after {render.money(spend.cost_usd)}, "
                f"{render.duration(spend.wall_s)}; unlicensed candidate directories may remain "
                f"under artifacts/{spec.spec_hash}/ and are never licensed"
            )
        elif exc is not None:
            self._fail_exception(exc)
        if cancelled or exc is not None:
            if self._quit:
                self.state = State.EXIT
            else:
                self._enter_review()
            return
        result = work.result()
        stages_text = ",".join(str(stage) for stage in result.stages) or "-"
        self._line(
            f"{render.clock(spend.wall_s)} compile finished in {render.duration(spend.wall_s)}: "
            f"{spend.calls} calls, {render.money(spend.cost_usd)}, stages {stages_text}"
        )
        approved_hash = spec.spec_hash if self.approved(spec) else None
        self.outcome = Outcome(result=result, approved_hash=approved_hash)
        if result.reused:
            self._reused_entry(result)
        if self._quit:
            self.state = State.EXIT
            return
        self._enter_result()

    def _reused_entry(self, result: CompileResult) -> None:
        """A routed reuse is the installed entry itself (Section 10.2): remember it as the
        last entry (``export`` stages it), and it is never installed again."""
        target = getattr(result.route, "target", None)
        name = getattr(target, "name", None)
        if not isinstance(name, str):
            return
        try:
            entry = self._services().registry.status(name)
        except RegistryError:
            return
        if entry.revision == getattr(target, "revision", None):
            self._last_entry = entry

    def _forge_outcome(self, result: CompileResult | None, *, cancelled: bool = False) -> None:
        """Section 11.4: the compile-path ``forge_agent`` choice gets its outcome.

        ``success`` when Forge produced the accepted submission and Stage 4 licensed it,
        ``failure`` when Forge ran otherwise, ``excluded`` when the operator cancelled or
        Forge never ran. A pinned role has no choice and records nothing.
        """
        from architect.forge_modes._routing import record_role
        from architect.forge_modes.run import forge_role_outcome

        choice, self._forge_choice = self._forge_choice, None
        forged, self._forged = self._forged, []
        if choice is None or self.services is None:
            return
        outcome: Literal["success", "failure", "excluded"]
        if cancelled:
            outcome = "excluded"  # an operator cancel (Section 11.4)
        elif result is None:
            outcome = "failure" if forged else "excluded"
        else:
            artifact = result.artifact
            licensed = (
                4 in result.stages
                and artifact is not None
                and artifact.licensed
                and not result.failures
            )
            outcome = forge_role_outcome(licensed, forged)
        record_role(self.services, choice, outcome)

    async def _forge_consent(self, services: Services) -> ModelAdapter | None:
        """Before any spend of a Forge compile: refuse, resolve the agent, show the cap, confirm.

        Whether Forge was enabled by the profile or by ``forge on``, a compile whose
        compiler has no smoke floor is ``forge_disabled`` (Forge must never silently
        not run). The agent adapter is resolved from the *effective* settings with the
        profile's one rule (``resolve_forge_agent``, Section 12.1), so ``forge model
        <name>`` really selects the agent that drives the session; an unknown name is
        ``forge_model_unknown`` before anything is printed, asked or spent. The fixed
        summed-cap line of Section 12.2 is then printed. A session that has not yet
        confirmed Forge is asked with the session confirmation; a refusal is
        ``forge_cancelled`` with nothing compiled or spent. Returns the resolved agent
        (None: the driver's own fallback), or None when Forge is off.
        """
        from .forge import ENABLE_PROMPT, cap_line, effective, spec_verifications

        settings = effective(self)
        self._routed_forge = None
        self._forge_choice = None
        self._forged = []
        if not settings.enabled:
            return None  # opt-in: nothing is printed or asked
        if services.compiler.smoke_factory is None:
            raise SessionError("forge_disabled")
        # A ProfileError (``forge_model_unknown``) is printed as its fixed code.
        agent = resolve_forge_agent(services.profile, settings, services.adapter_factory)
        roles = getattr(services, "roles", None)
        if settings.agent_model is None and roles is not None and roles.policy == "evidence":
            # Section 11.3: the forge_agent role is chosen before the Phase 9 line and
            # its line prints first; ``forge model`` (agent_model) pins the role.
            from architect.forge_modes.run import agent_line, resolve_agent

            draft = self.draft
            domain = draft.spec.domain if draft is not None else None
            routed_agent, routed, choice = resolve_agent(
                services, services.compiler, settings, domain
            )
            self._forge_choice = choice
            agent = routed_agent if routed_agent is not None else agent
            self._routed_forge = routed
            shown = agent or services.compiler.rewrite_adapter
            self._line(agent_line(shown.model if shown is not None else "-", routed))
            settings = routed
        compile_usd = services.profile.compile.compile_budget.cost_usd
        self._line(cap_line(compile_usd, settings, spec_verifications(self)))
        if self.forge_confirmed:
            return agent
        if not await self._confirm(ENABLE_PROMPT, default=False):
            self._forge_outcome(None, cancelled=True)  # an operator cancel
            raise SessionError("forge_cancelled")
        self.forge_confirmed = True
        return agent

    def _arm_forge(
        self, services: Services, agent: ModelAdapter | None = None
    ) -> Callable[[], None]:
        """Apply this session's Forge settings to one compile; returns the undo.

        The override replaces ``CompileConfig.forge`` on the compiler for this compile
        only, and ``Compiler.forge_adapter`` becomes ``agent`` (resolved from the same
        effective settings by ``_forge_consent``), so the session's ``agent_model`` is
        the agent that runs. When Forge is enabled its content-free result is turned
        into the ``forge ...`` stage line (counts and money only) as Stage 3 finishes.
        """
        from .forge import effective

        compiler = services.compiler
        previous = compiler.config
        settings = effective(self)
        if self._routed_forge is not None and settings.enabled:
            settings = self._routed_forge  # the consented role variant's mode and turns
        self._routed_forge = None
        if getattr(previous, "forge", None) != settings and "forge" in type(
            previous
        ).model_fields:
            compiler.config = previous.model_copy(update={"forge": settings})
        # ``_forge_consent`` already refused a Forge compile without a smoke floor.
        self._forge_active = settings.enabled and compiler.smoke_factory is not None
        swap_agent = self._forge_active and hasattr(compiler, "forge_adapter")
        previous_agent: ModelAdapter | None = (
            getattr(compiler, "forge_adapter", None) if swap_agent else None
        )
        if swap_agent:
            compiler.forge_adapter = agent
        original = getattr(compiler, "_forge", None) if self._forge_active else None
        if original is not None:

            async def witnessed(*args: object, **kwargs: object) -> object:
                result = await original(*args, **kwargs)
                self._forged.append(result)
                self._forge_result(result, settings)
                return result

            setattr(compiler, "_forge", witnessed)  # noqa: B010 - per-compile observer

        def restore() -> None:
            compiler.config = previous
            if swap_agent:
                compiler.forge_adapter = previous_agent
            self._forge_active = False
            if original is not None and "_forge" in vars(compiler):
                delattr(compiler, "_forge")

        return restore

    def _forge_result(self, result: object, settings: ForgeSettings) -> None:
        """Print the Forge stage line from a content-free ``ForgeResult``."""

        def count(name: str) -> int:
            value = getattr(result, name, 0)
            return value if type(value) is int and value >= 0 else 0

        spent = getattr(result, "agent_cost_usd", 0.0)
        line = render.forge_progress_line(
            checks=count("checks"),
            dryruns=count("dryruns"),
            measures=count("measures"),
            max_measures=settings.max_measures,
            submissions=count("submissions"),
            max_submissions=settings.max_submissions,
            spawns=count("spawns"),
            spent_usd=float(spent) if isinstance(spent, (int, float)) else 0.0,
            cap_usd=settings.max_usd,
        )
        self.forge_line = line.strip()
        self._write(line)

    # ------------------------------------------------------------------------ RESULT

    async def _result(self, cmd: Command) -> None:
        outcome = self.outcome
        if outcome is None:
            self._fail("no_result")
            return
        verb = cmd.verb
        if verb == "text":
            self._fail("not_a_command")
        elif verb == "report":
            self._show_result()
        elif verb == "spec":
            self._show_spec(with_blockers=False)
        elif verb == "json":
            self._write(render.spec_json(outcome.result.spec) + "\n")
        elif verb == "install":
            await self._install_command(outcome, cmd.args)
        elif verb == "run" and cmd.rest[:1] in {"{", "["}:
            # A JSON document right after ``run`` is an input for the artifact just
            # compiled; anything else names an installed artifact or a directory.
            artifact = outcome.result.artifact
            if artifact is None:
                raise SessionError("unlicensed_artifact")
            await self._run_artifact(artifact, cmd.rest)
        elif verb == "run" and not cmd.rest:
            self._fail("not_a_command", hint="run {json} | run <name|dir> {json}")
        elif verb == "save":
            self._save(outcome.result.spec, cmd.args[0] if cmd.args else None)
        elif verb == "again":
            self._enter_review()
        elif verb in {"new", "discard"}:
            self._drop_draft()
            self._enter_ready()
        elif verb == "export":
            from .delivery import export_verb

            await export_verb(self)
        elif verb == "rate":
            from .delivery import rate_verb

            rate_verb(self, cmd)
        elif verb in REGISTRY_READ:
            await self._registry_read(cmd)
        else:
            self._disallowed(cmd)

    async def _install_command(self, outcome: Outcome, args: tuple[str, ...]) -> None:
        if not args:
            self._fail("not_a_command", hint="install <name> [<hash8>]")
            return
        if outcome.result.reused:
            # Section 10.2: a routed reuse is an installed entry and no Stage 4 ran in
            # this compile, so it is never installed again under any name.
            target = getattr(outcome.result.route, "target", None)
            name = str(getattr(target, "name", "-"))
            label = f"{name}@{getattr(target, 'revision', '-')}"
            self._fail(
                "route_reuse_installed",
                hint=f"it is already installed as {label}; run {{json}} | info {name}",
            )
            return
        artifact = outcome.result.artifact
        if artifact is None or not outcome.licensed:
            raise SessionError("unlicensed_artifact")
        if artifact.spec.lifecycle is not Lifecycle.INSTALL:
            raise SessionError("not_installable")
        name = args[0]
        flag = "--allow-budget-increase"
        tokens = [arg for arg in args[1:] if arg != flag]
        token8 = render.hash8(artifact.spec.spec_hash)
        if not tokens:
            self._line(
                f'To install exactly this licensed artifact as "{name}", type: install {name} '
                f"{token8}"
            )
            report = artifact.report
            if report is not None:
                self._line(
                    f"  evidence   {report.floor_kind.value}  score {report.floor_score:.2f} >= "
                    f"limit {report.floor_limit:.2f}  held-out cases {report.held_out_count}  "
                    f"id {render.hash8(report.verification_id)}"
                )
            summary = self._floor_summary(artifact.spec)
            if summary is not None and synthetic_label(summary):
                self._line(f"             {synthetic_label(summary)}")
            return
        self._install(name, tokens[0], flag in args[1:], artifact)

    def _install(
        self, name: str, token: str, allow_increase: bool, artifact: HarnessArtifact
    ) -> None:
        if token != render.hash8(artifact.spec.spec_hash):
            raise SessionError("stale_approval_token")
        fresh = self._load_from_disk(artifact.directory)
        if fresh.spec.spec_hash != artifact.spec.spec_hash:
            # The directory now holds some other Spec's artifact; the typed token
            # binds to the hash on screen, never to a path.
            raise RegistryError("artifact_mismatch")
        if not fresh.licensed:
            raise SessionError("unlicensed_artifact")
        registry = self._services().registry
        try:
            entry = registry.install(name, fresh, allow_budget_increase=allow_increase)
        except RegistryError as exc:
            code = code_for(exc)
            hint = ""
            if code == "budget_increase":
                hint = (
                    f"install {name} {token} --allow-budget-increase (only if you accept a "
                    "higher per-run budget)"
                )
            self._fail(code, hint=hint)
            return
        self._last_entry = entry
        self._line(f"installed {name} revision {entry.revision} -> {entry.directory}")
        self._line("Next: run {json} | list | new")

    # ------------------------------------------------------------------------- FLOOR

    def _floor_limit(self, dataset: str) -> float | None:
        draft = self.draft
        if draft is not None and draft.spec.floor.dataset == dataset:
            return draft.spec.floor.limit
        return None

    def _enter_floor(self, dataset: str, kind: FloorKind, return_state: State) -> None:
        profile = self._profile()
        configured = profile.floors.get(dataset)
        if configured is not None:
            self.floor = FloorDraft(
                dataset=dataset,
                kind=configured.kind,
                cases=[],
                drafted_by_model=None,
                return_state=return_state,
            )
            self._floor_configured = True
        else:
            self.floor = new_draft(dataset, kind, return_state)
            self._floor_configured = False
        self.state = State.FLOOR
        self._show_floor()

    def _show_floor(self) -> None:
        draft = self.floor
        if draft is None:
            return
        if self._floor_configured:
            self._write(render.floors_screen([summarize(self._profile(), draft.dataset)]))
            self._line(
                f"floor '{draft.dataset}' is already configured; editing it here is not "
                "supported. Type back."
            )
            self._footer()
            return
        limit = self._floor_limit(draft.dataset)
        self._screen(render.floor_screen(draft, ready(draft), limit=limit))

    def _leave_floor(self) -> None:
        draft = self.floor
        target = draft.return_state if draft is not None else State.READY
        self.floor = None
        self._floor_configured = False
        if target is State.REVIEW and self.draft is not None:
            self._enter_review()
        else:
            self._enter_ready()

    async def _floor(self, cmd: Command) -> None:
        draft = self.floor
        if draft is None:
            self._fail("no_draft")
            return
        verb = cmd.verb
        if self._floor_configured and verb not in {"show", "back"}:
            self._fail("not_a_command", hint="back")
            return
        if verb == "text":
            self._fail("not_a_command", hint="add {json}")
        elif verb == "show":
            if not cmd.rest:
                self._show_floor()
            elif self._floor_configured:
                # A configured floor's cases are never printed (Section 7.5).
                self._fail("not_a_command", hint="back")
            else:
                self._screen(render.case_screen(find_case(draft, cmd.rest)))
        elif verb == "use":
            if not cmd.rest:
                self._fail("not_a_command", hint="use <path>")
                return
            path = self._cwd / cmd.rest
            count = add_file(draft, path)
            self._line(f"loaded {count} cases from {path.name}")
            self._show_floor()
        elif verb == "add":
            add_case(draft, self._parse_input(cmd.rest), origin="operator")
            self._show_floor()
        elif verb == "paste":
            await self._paste(draft)
        elif verb == "draft":
            await self._draft(draft, cmd.args)
        elif verb == "review":
            self._review_case(draft, cmd.rest)
        elif verb == "split":
            tuning, held_out = partition_preview(draft)
            self._line("tuning (harness sees during Stage 2): " + ", ".join(tuning))
            self._line("held-out (harness never sees): " + ", ".join(held_out))
        elif verb == "accept":
            self._accept(draft)
        elif verb == "back":
            if draft.cases and not await self._confirm(
                f"confirm> discard {len(draft.cases)} unsaved cases? [y/N] ", default=False
            ):
                return
            self._leave_floor()
        else:
            self._disallowed(cmd)

    async def _paste(self, draft: FloorDraft) -> None:
        buffer: list[str] = []
        prompt = self._prompt()  # Section 4 defines no paste prompt; the FLOOR one is kept.
        while True:
            line = await self._read(prompt)
            if line is None or line.strip() == ".":
                break
            buffer.append(line)
        text = "\n".join(buffer).strip()
        items: list[JsonValue]
        try:
            whole = self._parse_input(text)
            items = whole if isinstance(whole, list) else [whole]
        except SessionError:
            items = [self._parse_input(row) for row in buffer if row.strip()]
        for item in items:
            add_case(draft, item, origin="operator")
        self._line(f"added {len(items)} cases")
        self._show_floor()

    async def _draft(self, draft: FloorDraft, args: tuple[str, ...]) -> None:
        try:
            count = int(args[0]) if args else DEFAULT_DRAFT_COUNT
        except ValueError:
            raise SessionError("draft_count_out_of_range") from None
        spec_draft = self.draft
        if spec_draft is None:
            self._fail("no_draft")
            return
        services = self._services()
        spec = spec_draft.spec
        model = spec.model_target
        if model not in services.profile.models:
            model = self._compile_model(services.profile)
        adapter = services.adapter_factory(model)
        roles = getattr(services, "roles", None)
        if roles is not None and roles.policy == "evidence":
            # Section 11.3: the floor_draft role; its outcome is ``excluded`` (cost only).
            with contextlib.suppress(Exception):
                choice = roles.choose("floor_draft", domain=spec.domain)
                if choice.adapter is not None:
                    adapter = choice.adapter
                roles.record(choice, "excluded")
        self._write(HONESTY_NOTICE + "\n")
        started = self._clock()
        before = self.meter.snapshot()
        try:
            drafted = await draft_cases(draft, spec, count, adapter, services.runtime)
        finally:
            self._account(before, started)
        self._line(
            f"drafted {len(drafted)} cases (SYNTHETIC, unreviewed): review each with "
            "'review <id> ok', 'review <id> expected <json>' or 'review <id> drop'"
        )
        self._show_floor()

    def _review_case(self, draft: FloorDraft, rest: str) -> None:
        case_id, remainder = split_first(rest)
        action, value = split_first(remainder)
        if not case_id or action not in {"ok", "drop", "expected"}:
            self._fail("not_a_command", hint="review <id> ok | expected <json> | drop")
            return
        if action == "expected":
            review(draft, case_id, ("expected", parse_value(value)))
        elif action == "ok":
            review(draft, case_id, "ok")
        else:
            review(draft, case_id, "drop")
        # One decision line and the next step, not the whole screen (Section 5.4).
        after = None if action == "drop" else find_case(draft, case_id)
        line = render.reviewed_line(case_id, action, after)
        self._write(render.review_screen(draft, ready(draft), line))

    def _accept(self, draft: FloorDraft) -> None:
        reasons = ready(draft)
        if reasons:
            unreviewed = any("unreviewed" in reason for reason in reasons)
            code = "unreviewed_cases" if unreviewed else "floor_too_small"
            self._fail(code)
            for reason in reasons:
                self._line(f"blocked: {reason}")
            return
        profile_path = self._require_editable()
        path = save_dataset(self.workspace, draft)
        register(profile_path, draft.dataset, draft.kind, path)
        self._rebuild_services()
        summary = summarize(self._profile(), draft.dataset, self._floor_limit(draft.dataset))
        label = synthetic_label(summary)
        self._line(
            f"floor '{draft.dataset}' configured: {summary.count} cases -> {summary.tuning} "
            f"tuning / {summary.held_out} held-out {label}".rstrip()
        )
        self._leave_floor()
