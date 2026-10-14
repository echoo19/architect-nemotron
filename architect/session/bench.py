"""The session verb ``bench`` (Phase 8 contract Section 11).

``bench smoke`` runs the frozen suite's smoke tier on the fake provider with
deterministic oracles: free, never prompts. ``bench quick|full [model,model]``
and ``bench arc [model]`` run on the session profile's models: the bound line is
printed first and nothing is spent until the operator answers ``y`` to
``Continue? [y/N]`` (a scripted console must supply that line too). While a
bench runs the session is in ``BENCHING``: ``cancel`` stops it, the rows written
so far are kept and the in-flight cell is recorded as ``cancelled``.
``bench compare <id> <id>`` and ``bench list`` are READY-state reads.
``bench resume <id>`` continues a stopped bench with its saved settings (Phase 11
Section 3): only its undone cells are bounded, confirmed and run.

Rows go to ``<workspace>/bench/<bench_id>/rows.jsonl``. Everything printed is a
fixed-format table or line of counts, labels and money; bench failures are the
closed ``bench_*`` codes. Stage 4 stays the only licensing authority: the bench
compiles in throwaway workspaces and never installs into the operator's
registry (``bench arc`` installs its manufactured harness only into a throwaway
registry inside the cell workspace, deleted with it). While a bench runs, each
cell points ``ARCHITECT_SIGNING_KEY_FILE`` at its throwaway key; the session pins
its own key first so ``status`` keeps naming the key that signs its artifacts.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from architect.bench import BenchError, BenchRow, compare, read_rows, suite_digest
from architect.bench import render as tables
from architect.bench.providers import evidence_label, provider_for
from architect.bench.runner import (
    BenchConfig,
    BenchProgress,
    BenchResult,
    bound,
    load_resume,
    run_bench,
)
from architect.bench.suite import SUITES
from architect.profiles import Profile, ProfileError, load_profile
from architect.spec import Budget

from . import render
from .codes import SessionError
from .commands import Command
from .observe import ProgressPolicy
from .state import Progress, Spend, State

if TYPE_CHECKING:
    from .session import Session

__all__ = ["CONFIRM_PROMPT", "handle_bench"]

CONFIRM_PROMPT = "Continue? [y/N] "
Tier = Literal["smoke", "quick", "full", "arc"]
_TIERS: tuple[Tier, ...] = ("smoke", "quick", "full", "arc")


def _names(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


async def handle_bench(session: Session, cmd: Command) -> None:
    """Dispatch ``bench <sub> ...``; bench failures surface as their fixed codes."""
    if not cmd.args:
        session._fail("not_a_command", hint="bench smoke")
        return
    sub = cmd.args[0].lower()
    try:
        if sub == "list":
            _list(session)
        elif sub == "compare":
            _compare(session, cmd.args[1:])
        elif sub == "resume":
            if len(cmd.args) != 2:
                session._fail("not_a_command", hint="bench resume <id>")
                return
            await _resume(session, cmd.args[1])
        elif sub in _TIERS:
            await _run(session, _tier(sub), cmd.args[1:])
        else:
            session._fail("not_a_command", hint="help bench")
    except BenchError as exc:
        raise SessionError(exc.code) from None


def _tier(text: str) -> Tier:
    for tier in _TIERS:
        if tier == text:
            return tier
    raise BenchError("bench_tier_unknown")


def _out(session: Session) -> Path:
    return session.workspace / "bench"


def _config(session: Session, tier: Tier, args: tuple[str, ...]) -> BenchConfig:
    out = _out(session)
    if tier == "smoke":
        if args:
            raise BenchError("bench_model_unconfigured")  # smoke is the fake provider only
        # The smoke tier carries its negative control: noisy must fail its floor.
        return BenchConfig(
            tier="smoke", provider="fake", seed=7, variants=("default", "noisy"), out=out
        )
    profile = session._profile()
    models = _names(",".join(args)) if args else (session._compile_model(profile),)
    if tier == "arc" and len(models) != 1:
        raise BenchError("bench_model_unconfigured")
    if any(model not in profile.models for model in models):
        raise BenchError("bench_model_unconfigured")
    return BenchConfig(
        tier=tier,
        provider="profile",
        profile=session.profile_path,
        models=models,
        seed=7,
        out=out,
    )


def _progress_line(item: BenchProgress) -> str:
    stage = "-" if item.stage is None else str(item.stage)
    return render.wrap(
        f"{render.clock(item.elapsed_s)} bench {item.done}/{item.total} {item.task_id} "
        f"{render.ascii_text(item.model)}: stage {stage}, {item.calls} calls, "
        f"{render.money(item.cost_usd)}"
    ) + "\n"


async def _run(session: Session, tier: Tier, args: tuple[str, ...]) -> None:
    config = _config(session, tier, args)
    profile = session._profile() if config.provider == "profile" else None
    await _execute(session, config, profile, bench_id=str(uuid.uuid4()), resume=False)


async def _resume(session: Session, reference: str) -> None:
    config = load_resume(_out(session), reference)
    profile: Profile | None = None
    if config.provider == "profile":
        if config.profile is None:
            raise BenchError("bench_resume_mismatch")
        try:
            profile = load_profile(config.profile)
        except ProfileError:
            raise BenchError("bench_invalid_profile") from None
    await _execute(session, config, profile, bench_id=reference.lower(), resume=True)


async def _execute(
    session: Session,
    config: BenchConfig,
    profile: Profile | None,
    *,
    bench_id: str,
    resume: bool,
) -> None:
    tier = config.tier
    limits = await asyncio.to_thread(
        bound, config, profile=profile, resume=bench_id if resume else None
    )
    session._line(
        tables.bound_line(tier, len(limits.models), limits.total_usd, limits.calls)
    )
    if config.provider != "fake" and not await session._confirm(CONFIRM_PROMPT, default=False):
        raise BenchError("bench_cancelled")  # declined before any spend
    rows_path = config.out / bench_id / "rows.jsonl"
    policy = ProgressPolicy(every_calls=5, every_s=20.0)
    last_cell = [-1]

    def on_progress(item: BenchProgress) -> None:
        line = _progress_line(item)
        session._last_progress = line
        session.bench_status = (
            f"benching {item.done}/{item.total} {item.task_id} {render.ascii_text(item.model)}"
        )
        snapshot = Progress(
            elapsed_s=item.elapsed_s,
            stage=item.stage,
            stage_text="",
            calls=item.calls,
            in_flight=0,
            case_rows=0,
            quality_rows=0,
            rewrite_rows=0,
            accepted_rows=0,
            spend=Spend(calls=item.calls, cost_usd=item.cost_usd),
            budget=Budget(),
            left_usd=0.0,
            left_s=0.0,
        )
        new_cell = item.done != last_cell[0]
        if policy.should_print(snapshot) or new_cell:
            last_cell[0] = item.done
            session._write(line)

    started = session._clock()
    # Pin the session's key before any cell swaps the process-wide variable.
    session.pinned_signing_key = session._signing_key()
    session.state = State.BENCHING
    session.bench_status = f"benching 0/{len(limits.cells)}"
    work: asyncio.Task[BenchResult] = asyncio.create_task(
        run_bench(
            config,
            profile=profile,
            on_progress=on_progress,
            clock=session._clock,
            bench_id=bench_id,
            resume=bench_id if resume else None,
        )
    )
    try:
        await session._await_work(work)
    finally:
        session.bench_status = None
        session._last_progress = ""
        session.pinned_signing_key = None
    cancelled = work.cancelled()
    exc = None if cancelled else work.exception()
    rows: list[BenchRow] = []
    if rows_path.is_file():
        try:
            rows = read_rows(rows_path)
        except BenchError:
            rows = []
    spend = Spend(
        calls=sum(row.calls for row in rows),
        tokens_in=sum(row.tokens_in for row in rows),
        tokens_out=sum(row.tokens_out for row in rows),
        cost_usd=sum(row.cost_usd for row in rows),
        wall_s=max(0.0, session._clock() - started),
    )
    session.session_spend.add(spend)
    session.state = State.EXIT if session._quit else State.READY
    if cancelled:
        session._line(
            f"bench cancelled after {render.money(spend.cost_usd)}, "
            f"{render.duration(spend.wall_s)}; rows so far are kept"
        )
    elif isinstance(exc, BenchError):
        session._fail(exc.code)
    elif exc is not None:
        session._fail_exception(exc)
    if rows:
        session._write(tables.bench_table(rows))
        session._line(f"rows written to {rows_path}")
    session._footer()


def _resolve(session: Session, reference: str) -> list[BenchRow]:
    path = Path(reference)
    if not path.is_absolute() and not path.exists():
        path = _out(session) / reference
    if path.is_dir():
        path = path / "rows.jsonl"
    if not path.is_file():
        raise BenchError("bench_rows_unreadable")
    return read_rows(path)


def _compare(session: Session, args: tuple[str, ...]) -> None:
    if len(args) != 2:
        session._fail("not_a_command", hint="bench compare <id> <id>")
        return
    comparison = compare(_resolve(session, args[0]), _resolve(session, args[1]))
    session._write(tables.compare_table(comparison))
    session._footer()


def _list(session: Session) -> None:
    profile = session._profile()
    bounds: list[tuple[str, str, float, int, str]] = []
    for model in profile.models:
        config = BenchConfig(
            tier="quick",
            provider="profile",
            profile=session.profile_path,
            models=(model,),
            out=_out(session),
        )
        try:
            limits = bound(config, profile=profile)
            evidence = evidence_label(provider_for(config, profile, model))
        except BenchError:
            continue
        bounds.append(("quick", model, limits.total_usd, limits.calls, evidence))
    recorded: list[tuple[str, int, str, str]] = []
    out = _out(session)
    if out.is_dir():
        for rows_path in sorted(out.glob("*/rows.jsonl")):
            try:
                rows = read_rows(rows_path)
            except BenchError:
                continue
            recorded.append(
                (
                    rows_path.parent.name,
                    len(rows),
                    ",".join(sorted({row.tier for row in rows})) or "-",
                    ",".join(sorted({row.evidence for row in rows})) or "-",
                )
            )
    session._write(
        tables.list_table(
            SUITES["v1"],
            version="v1",
            digest=suite_digest("v1"),
            bounds=bounds,
            recorded=recorded,
        )
    )
    session._footer()
