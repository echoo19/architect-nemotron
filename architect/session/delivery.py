"""Phase 10 session verbs: route, reuse, roles, export, rate, forge run, forge integrate.

Authority is unchanged (Section 13.2): the approval keyword is still written only in
``Session._compile`` and ``_approved_hash`` only in ``approve <hash8>``. These verbs
never approve, license or install:

- ``route`` is pure advice (nothing is recorded, run, approved or compiled);
- ``reuse <name> <hash8> {json}`` grants reuse for this one call, re-loads the entry
  (``load_reuse_target``) and runs it through ``Session._run_artifact``;
- ``forge run {json}`` needs lifecycle expire; ``forge integrate <repo> <into>`` needs a
  prior ``approve <hash8>`` of this Spec. Both show every consent line and ask with the
  session confirmation before any spend, and again before anything is placed;
- ``export`` stages a package of an installed licensed result (no repository write);
- ``rate good|bad`` writes one content-free ``RatingRecord`` for the last delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from architect.registry import RegistryError
from architect.routing.log import OutcomeRecord, RatingRecord, RouteLog, RouteLogError
from architect.routing.router import InstalledRef, load_reuse_target, route
from architect.spec import Lifecycle, Spec
from architect.telemetry import categorical

from . import render
from .codes import SessionError
from .commands import Command, split_first
from .routes import (
    advice,
    deliver_command,
    installed_match,
    probe_for,
    roles_table,
    route_screen,
)
from .state import State

if TYPE_CHECKING:
    from architect.compiler.types import ForgeSettings
    from architect.forge_modes.run import AgentChoice
    from architect.profiles import Services

    from .session import Session

__all__ = [
    "CONTINUE_PROMPT",
    "export_verb",
    "forge_integrate_verb",
    "forge_run_verb",
    "rate_verb",
    "reuse_verb",
    "roles_verb",
    "route_verb",
]

CONTINUE_PROMPT = "Continue? [y/N] "
REUSE_HINT = "reuse <name> <hash8> {json}"
# ``rate_no_run``: the delivery commands this state offers (``run {json}`` is RESULT's).
_RATE_HINTS: dict[State, str] = {
    State.READY: "run <name|dir> {json}, then rate good|bad",
    State.REVIEW: (
        "reuse <name> <hash8> {json} | forge run {json} | run <name|dir> {json}, then "
        "rate good|bad"
    ),
    State.RESULT: "run {json} | run <name|dir> {json}, then rate good|bad",
}


def _profile_label(session: Session) -> str | None:
    return str(session.profile_path) if session.profile_path is not None else None


def route_verb(session: Session, spec: Spec) -> None:
    """REVIEW ``route``: the Section 8.4 table for the Spec under review (pure), each
    code's fixed sentence and the command that acts on it."""
    draft = session.draft
    decision = advice(
        session._services(),
        spec,
        origin="session",
        forbid_reuse=draft is not None and draft.origin == "registry",
    )
    session._write(
        route_screen(
            decision,
            profile=_profile_label(session),
            surface="session",
            hash8=spec.spec_hash[:8],
            deliver=deliver_command(spec),
        )
    )


def roles_verb(session: Session) -> None:
    """READY ``roles``: the ``architect roles`` table."""
    session._write(roles_table(session._services()))


def _record(services: Services, record: OutcomeRecord | RatingRecord) -> None:
    router = services.router
    log = router.log if router is not None else RouteLog(services.workspace / "routes")
    log.append(record)


async def reuse_verb(session: Session, spec: Spec, cmd: Command) -> None:
    """``reuse <name> <hash8> {json}``: a typed-token grant for this call only (P3)."""
    name, rest = split_first(cmd.rest)
    token, text = split_first(rest)
    if not name or not token or not text or text[0] not in "{[":
        session._fail("not_a_command", hint=REUSE_HINT)
        return
    if token != spec.spec_hash[:8]:
        session._fail("stale_approval_token", hint=f"reuse {name} {spec.spec_hash[:8]} {{json}}")
        return
    session._parse_input(text)  # invalid JSON is refused before any routing
    services = session._services()
    router = services.router
    if router is None:
        raise SessionError("route_no_reuse")
    hash8 = spec.spec_hash[:8]
    probe = probe_for(services, spec, origin="session", grant=True)
    # A pure check first: a refused grant is never appended to the route log.
    preview = await asyncio.to_thread(route, probe, index=router.index, policy=router.policy)
    match = installed_match(preview) if preview.action == "reuse_installed" else None
    if match is None:
        session._write(
            route_screen(
                preview, profile=_profile_label(session), surface="session", hash8=hash8,
                deliver=deliver_command(spec),
            )
        )
        raise SessionError("route_no_reuse")
    if match.name != name:
        try:
            await asyncio.to_thread(services.registry.status, name)
        except RegistryError:
            session._fail("unknown_name", hint=f"reuse {match.name} {hash8} {{json}}")
            return
        session._line(
            f"reuse: {name} is not the installed match for this Spec; the match is "
            f"{match.name}@{match.revision}. A grant names the entry it reuses, so nothing "
            "was run"
        )
        session._fail("route_no_reuse", hint=f"reuse {match.name} {hash8} {{json}}")
        return
    decision = await asyncio.to_thread(router.route, probe)
    target = decision.target
    if (
        decision.action != "reuse_installed"
        or not isinstance(target, InstalledRef)
        or target.name != name
    ):
        raise SessionError("route_target_changed")
    artifact = await asyncio.to_thread(load_reuse_target, decision, services.registry, spec)
    session._line(
        f"reuse {target.name}@{target.revision}: installed and licensed on a fresh load; "
        "Stage 4 issued that license, routing only chose it"
    )
    await session._run_artifact(artifact, text)
    with contextlib.suppress(RouteLogError, ValueError):
        router.outcome(
            decision,
            OutcomeRecord(
                decision_id=decision.decision_id,
                timestamp=datetime.now(UTC),
                outcome="delivered",
                stage4_passed=None,
                evidence_class=router.index.evidence_class(artifact.model),
            ),
        )


async def export_verb(session: Session) -> None:
    """RESULT ``export``: stage a package of the installed licensed result (no repo)."""
    from architect.forge_modes import export_package
    from architect.forge_modes.export import SIGNED_LABEL, default_package, evidence_label_of

    outcome = session.outcome
    entry = session._last_entry
    artifact = outcome.result.artifact if outcome is not None else None
    if (
        outcome is None
        or artifact is None
        or entry is None
        or entry.spec_hash != artifact.spec.spec_hash
        or artifact.spec.lifecycle is not Lifecycle.INSTALL
    ):
        raise SessionError("export_unverified")
    services = session._services()
    package = default_package(entry.spec_hash)
    manifest, _digest = await asyncio.to_thread(
        export_package, entry, services.workspace / "exports", package
    )
    staged = services.workspace / "exports" / manifest.artifact_digest[:16] / manifest.package
    session._line(f"export staged: {staged}")
    attested = "Ed25519-signed" if evidence_label_of(artifact) == SIGNED_LABEL else "host-attested"
    session._line(
        f"{attested} package; feature flag OFF; nothing was written to any repository"
    )


def rate_verb(session: Session, cmd: Command) -> None:
    """RESULT ``rate good|bad``: one content-free rating of the last delivery."""
    if len(cmd.args) != 1 or cmd.args[0] not in ("good", "bad"):
        session._fail("not_a_command", hint="rate good|bad")
        return
    last = session.last_delivery
    if last is None:
        session._fail("rate_no_run", hint=_RATE_HINTS.get(session.state, ""))
        return
    run_id, digest = last
    rating: Literal["good", "bad"] = "good" if cmd.args[0] == "good" else "bad"
    try:
        _record(
            session._services(),
            RatingRecord(
                run_id=UUID(run_id),
                timestamp=datetime.now(UTC),
                target_digest=categorical(digest),
                rating=rating,
            ),
        )
    except (RouteLogError, ValueError, OSError):
        raise SessionError("evidence_store_unavailable") from None
    session._line(
        f"rated {rating}: run {render.hash8(run_id)} (content-free; a rating orders versions, "
        "never licenses)"
    )


# ------------------------------------------------------------------ Forge modes


async def _consented(
    session: Session, services: Services, settings: ForgeSettings, spec: Spec, retry: str
) -> tuple[Callable[[str], bool], AgentChoice] | None:
    """Section 12.1 consent in an async session, with exactly one ``forge_agent`` choice.

    The role is resolved once (``prepare_agent``), before the ``forge agent:`` line
    prints; the operator answers; the paid call receives that same choice and proceeds
    only if its consent text is identical to the text shown. A refusal records the
    choice as ``excluded`` (an operator cancel, Section 11.4) and returns None.
    """
    from architect.forge_modes._routing import record_role
    from architect.forge_modes.run import prepare_agent

    # A ``ForgeModeError`` (``forge_model_unknown``...) is printed as its fixed code.
    shown, agent = prepare_agent(services, settings, spec)
    for line in shown.split("\n"):
        session._line(line)
    if not await session._confirm(CONTINUE_PROMPT, default=False):
        record_role(services, agent.choice, "excluded")
        session._fail("forge_cancelled", hint=f"{retry}, and answer y")
        return None
    return (lambda text: text == shown), agent


def _footer(session: Session) -> None:
    """The spend footer after a Forge verb; a quit prints it once on the way out."""
    if not session._quit:
        session._footer()


def _cancelled(session: Session, spec: Spec) -> None:
    """After a cancelled Forge build: what was spent, as a compile cancel says it."""
    spend = session._compile_spend
    cost = spend.cost_usd if spend is not None else 0.0
    wall = spend.wall_s if spend is not None else 0.0
    session._line(
        f"cancelled after {render.money(cost)}, {render.duration(wall)}; unlicensed "
        f"candidate directories may remain under artifacts/{spec.spec_hash}/ and are never "
        "licensed"
    )
    _footer(session)


async def _run_work[T](
    session: Session, work: Awaitable[T], *, continues: bool = False
) -> T | None:
    """Run paid work with the console live (cancel, status, quit); stderr is captured
    and re-shown on the console. None when it was cancelled.

    Its spend is the footer's ``this compile`` figure, as for a compile; ``continues``
    adds it to the figure of the step before (``forge integrate`` builds, then places).
    """
    buffer = io.StringIO()
    previous = session.state
    session.state = State.COMPILING
    started = session._clock()
    before = session.meter.snapshot()

    async def captured() -> T:
        with contextlib.redirect_stderr(buffer):
            return await work

    task = asyncio.create_task(captured())
    try:
        await session._await_work(task)
    finally:
        spend = session._account(before, started)
        if continues and session._compile_spend is not None:
            session._compile_spend.add(spend)
        else:
            session._compile_spend = spend
        session.state = State.EXIT if session._quit else previous
    for line in buffer.getvalue().splitlines():
        session._line(line)
    if task.cancelled():
        return None
    exc = task.exception()
    if exc is not None:
        raise exc
    return task.result()


async def forge_run_verb(session: Session, cmd: Command) -> None:
    """REVIEW ``forge run {json}`` (Section 12.1): lifecycle expire only."""
    from architect.forge_modes import forge_run

    from .forge import effective

    draft = session.draft
    if draft is None:
        raise SessionError("no_draft")
    spec = draft.spec
    if spec.lifecycle is not Lifecycle.EXPIRE:
        raise SessionError("forge_run_needs_expire")
    _sub, text = split_first(cmd.rest)
    if not text:
        session._fail("not_a_command", hint="forge run {json}")
        return
    task = session._parse_input(text)
    services = session._services()
    settings = effective(session)
    if services.compiler.smoke_factory is None:
        raise SessionError("forge_disabled")
    consented = await _consented(session, services, settings, spec, "forge run {json}")
    if consented is None:
        _footer(session)
        return
    consent, agent = consented
    result = await _run_work(
        session, forge_run(spec, task, services, settings, consent=consent, agent=agent)
    )
    if result is None:
        _cancelled(session, spec)
        return
    if result.output is not None:
        session._write(json.dumps(result.output, indent=2, ensure_ascii=True) + "\n")
    codes = ",".join(result.codes) or "-"
    delivered = bool(result.codes) and result.codes[0] in (
        "forge_run_delivered",
        "forge_run_reused_installed",
    )
    if delivered and result.run_id is not None and result.artifact_digest is not None:
        # ``rate good|bad`` now rates this delivery (REVIEW allows rate).
        session.last_delivery = (result.run_id, result.artifact_digest)
    if delivered:
        verdict = "delivered once; Stage 4 licensed it, routing never licenses"
    elif result.licensed:
        verdict = "not delivered: Stage 4 licensed it, but the run did not complete"
    else:
        verdict = "not delivered: Stage 4 did not license a harness, so nothing was run"
    session._line(f"forge run codes {codes}; {verdict}")
    _footer(session)


async def forge_integrate_verb(session: Session, cmd: Command) -> None:
    """REVIEW ``forge integrate <repo> <into>`` (Section 12.2) after ``approve <hash8>``.

    The four P9 gates in order: the approval already typed, Forge spend consent, the
    Stage 4 license (build, install, re-load, staged export) and export consent. Only
    then the guarded FeatureBuilder/Pi path places the package behind its disabled flag.
    """
    from architect.forge_modes import IntegrateOptions, IntegrateResult, forge_integrate
    from architect.forge_modes.errors import ExportError
    from architect.forge_modes.export import _target, default_package, read_manifest
    from architect.forge_modes.integrate import export_line, placement_codes
    from architect.orchestrator import OrchestrationError
    from architect.orchestrator.features import FeatureBuilder

    from .forge import effective

    draft = session.draft
    if draft is None:
        raise SessionError("no_draft")
    if len(cmd.args) != 3:
        session._fail("not_a_command", hint="forge integrate <repo> <into>")
        return
    spec = draft.spec
    if spec.lifecycle is not Lifecycle.INSTALL:
        # Only an INSTALL Spec can be approved and integrated (Section 12.2); say so
        # before the approval hint, which an expire Spec could never satisfy.
        session._fail(
            "approved_feature_must_be_installed",
            hint=f"set lifecycle install, then approve {spec.spec_hash[:8]} (runs a normal "
            "paid compile), then again, then forge integrate <repo> <into>",
        )
        return
    if not session.approved(spec):
        # ``approve`` is the only place approval is recorded, and it compiles; say so.
        session._fail(
            "approval_required",
            hint=f"approve {spec.spec_hash[:8]} (runs a normal paid compile), then again, "
            "then forge integrate <repo> <into> (a Forge build)",
        )
        return
    services = session._services()
    repo = (session._cwd / cmd.args[1]).resolve()
    if not repo.is_dir():
        raise SessionError("invalid_repository")
    into = cmd.args[2]
    package = default_package(spec.spec_hash)
    denied = tuple(services.profile.pi.denied_paths)
    try:
        destination = _target(repo, into, package, denied)
    except ExportError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise SessionError("export_path_invalid") from None
    if destination.exists():
        raise SessionError("export_target_exists")
    if services.profile.builder_floor is None:
        raise SessionError("builder_floor_required")
    if services.compiler.smoke_factory is None:
        raise SessionError("forge_disabled")
    request = draft.goal or spec.task
    settings = effective(session)
    retry = "forge integrate <repo> <into>"
    consented = await _consented(session, services, settings, spec, retry)
    if consented is None:
        _footer(session)
        return
    consent, agent = consented

    def build() -> Awaitable[object]:
        # ``stage_only``: the session asks the export consent itself, then places the
        # package through the FeatureBuilder/Pi path below.
        return forge_integrate(
            request, repo, services, settings, into=into, package=package,
            approved_spec=spec, consent=consent, stage_only=True, agent=agent,
        )

    try:
        staged_result = await _run_work(session, build())
    except BaseException:
        _footer(session)
        raise
    if staged_result is None:
        _cancelled(session, spec)
        return
    if not isinstance(staged_result, IntegrateResult):
        _footer(session)
        raise SessionError("feature_compile_failed")
    staged = Path(str(staged_result.export["package_path"]))
    # The consent, FeatureBuilder's check and the post-placement re-hash all bind to the
    # manifest hash ``forge_integrate`` returned when it staged the package; a staging
    # copy changed since (even repacked self-consistently) is refused, nothing placed.
    returned = staged_result.export.get("manifest_sha256")
    try:
        manifest, manifest_sha256 = await asyncio.to_thread(read_manifest, staged)
    except ExportError:
        manifest_sha256 = ""
    if not isinstance(returned, str) or manifest_sha256 != returned:
        session._line(
            f"integrate: not placed; the staged package at {staged} changed after the build"
        )
        session._fail("export_integrity_mismatch", hint=f"{retry} builds again with Forge")
        _footer(session)
        return
    files = staged_result.export.get("files")
    size = staged_result.export.get("bytes")
    entry = f"{staged_result.name}@{staged_result.revision}"
    session._line(export_line(
        manifest.package, files if isinstance(files, int) else 0,
        size if isinstance(size, int) else 0, into,
    ))
    if not await session._confirm(CONTINUE_PROMPT, default=False):
        # The installed entry and the staging directory are kept; nothing is placed.
        session._line(
            f"integrate: {entry} stays installed and licensed; the package stays staged at "
            f"{staged}; nothing was written to {into}"
        )
        profile = _profile_label(session) or "<profile>"
        session._fail(
            "export_cancelled",
            hint=f"{retry} builds again with Forge; to place this installed entry instead, "
            f"save <spec.json> here, then: architect forge integrate <request> --profile "
            f"{profile} --repo {repo} --into {into} --approved-spec <spec.json> "
            "--reuse-installed",
        )
        _footer(session)
        return
    options = IntegrateOptions(
        entry=InstalledRef(
            name=staged_result.name,
            revision=staged_result.revision,
            artifact_digest=manifest.artifact_digest,
            spec_hash=staged_result.spec_hash,
        ),
        staged=staged,
        into=into,
        package=manifest.package,
        manifest_sha256=manifest_sha256,
    )
    codes: list[str] = []
    try:
        orchestration = await _run_work(
            session,
            FeatureBuilder(services).create(request, repo, approved_spec=spec, integrate=options),
            continues=True,
        )
    except OrchestrationError as exc:
        # The build is paid for and installed: report the refusal, never lose it.
        orchestration = None
        codes.append(str(exc))
    if orchestration is not None:
        codes.extend(orchestration.failures)
    if not destination.exists():
        session._line(f"integrate: not placed; the package stays staged at {staged}")
    else:
        # Section 12.2 step 7: re-hash against the manifest the operator consented to,
        # and re-check pins, children and the license; report, never delete.
        codes.extend(
            await asyncio.to_thread(
                placement_codes, destination, manifest_sha256, services.registry
            )
        )
        session._line(f"integrate: placed {into}/{manifest.package}; feature flag OFF; "
                      "human review required")
    session._line(
        "integrate codes " + (",".join(code for code in dict.fromkeys(codes) if code) or "-")
    )
    _footer(session)

