"""First-class stdlib CLI. JSON results are content-bearing, not telemetry.

An install proposal is not approval. Only an operator-supplied --approved-spec
file permits installation; neither request text nor model JSON can approve it.

Forge (Phase 9) is opt-in per invocation (``--forge*``) or per profile. When the
effective settings enable it, the summed compile + Forge cap is shown before any
spend and the operator must answer ``Continue? [y/N]`` (or pass ``--yes``); a
non-interactive stdin without ``--yes`` is ``forge_cancelled`` with nothing spent.

Routing (Phase 10) orders licensed versions and never licenses: ``route`` shows the
decision (and with ``--run --yes`` runs only a granted installed reuse),
``do --reuse-installed`` grants installed reuse to one invocation, ``--no-route``
compiles without the router, ``forge run`` and ``forge integrate`` are the Forge
delivery modes, ``export verify`` re-checks an exported package and ``roles`` shows
the role router's evidence.

W7: ``verify <path>`` (or ``verify --published <name>``) prints a plain verdict on a
report, an artifact, an exported package or a published report folder, checked offline
with the keys ``--workspace`` or the ``ARCHITECT_*`` variables name (never ``./.architect``
on its own: without them only the shipped key counts). ``--require-key <fingerprint>``
accepts only that key's Ed25519 signature, so a result stripped back to the HMAC is
refused. ``keys generate|list|trust|sign`` manage the Ed25519 keys behind it
(``architect.trust``). Neither needs a profile.

``retake <agent-folder> --profile <p>`` lets an agent whose files changed take its exam
again (``--practice`` scores the change on practice cases and is not an attempt) and
``undo <agent-folder>`` puts back the last version that passed; see
``architect.session.retake``. Both print plain lines. ``retake`` exits 0 when it passed, 1
when it did not and 2 when it was refused with no attempt used; ``undo`` exits 0 when the
folder was restored and 2 when there was nothing to restore.

``judge <code>`` stores a hackathon judge code as the Nebius key, with the judges'
relay as Nebius's URL, in the workspace the conversational CLI and the desktop app
share (``--workspace``, else ``ARCHITECT_WORKSPACE``, else ``~/.architect``); see
``architect.providers.judge``. It needs no profile and never prints the code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, cast

from pydantic import JsonValue, TypeAdapter

from architect.compiler import CompileResult
from architect.compiler.forge_audit import AuditError, ForgeAudit, audit_directory, audit_scope
from architect.compiler.types import ForgeSettings
from architect.orchestrator import OrchestrationError
from architect.profiles import (
    Profile,
    ProfileError,
    Services,
    build_services,
    forge_settings_of,
    load_profile,
)
from architect.registry import Registry, RegistryError
from architect.runtime import ArtifactExpiredError, BudgetExceeded, UnlicensedArtifactError
from architect.spec import HarnessArtifact, Lifecycle, Spec

if TYPE_CHECKING:
    from architect.bench import BenchRow
    from architect.bench.rows import Variant
    from architect.bench.runner import BenchConfig


class CLIError(ValueError):
    """Fixed error code; never construct from provider or user content."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CLIError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="architect",
        description="Compile and run quality-gated agents.",
        epilog="Run with no arguments for the Architect CLI (flags: -p, -c, -r, --model, "
        "--workspace, --permission, --no-probe); 'architect session' is the step-by-step "
        "session and 'architect session --help' lists its flags.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("compile", "do", "create", "run", "recompile"):
        command = commands.add_parser(name)
        positional = {"compile": "request_or_spec", "do": "goal", "create": "request",
                      "run": "artifact", "recompile": "name"}[name]
        command.add_argument(positional)
        command.add_argument("--profile", required=True, type=Path)
        if name in ("compile", "create"):
            command.add_argument("--approved-spec", type=Path)
        if name in ("do", "create"):
            command.add_argument("--repo", required=True, type=Path)
        if name == "do":
            command.add_argument("--reuse-installed", action="store_true")
        if name == "run":
            command.add_argument("--input", required=True)
        else:
            command.add_argument("--no-route", action="store_true")
            _forge_flags(command)
    route = commands.add_parser("route", help="show the routing decision for a task")
    route.add_argument("request_or_spec")
    route.add_argument("--profile", required=True, type=Path)
    route.add_argument("--origin", choices=("cli", "session", "orchestrator"), default="cli")
    route.add_argument("--run", dest="run_input")
    route.add_argument("--yes", action="store_true")
    route.add_argument("--json", action="store_true")
    roles = commands.add_parser("roles", help="the role router's policy and evidence")
    roles.add_argument("--profile", required=True, type=Path)
    roles.add_argument("--json", action="store_true")
    export = commands.add_parser("export", help="verify an exported harness package")
    export_actions = export.add_subparsers(dest="export_command", required=True)
    verify = export_actions.add_parser("verify")
    verify.add_argument("package", type=Path)
    verify.add_argument("--profile", type=Path)
    verify.add_argument("--workspace", type=Path)
    verify.add_argument("--reverify", action="store_true")
    verify.add_argument("--yes", action="store_true")
    verify.add_argument("--json", action="store_true")
    check = commands.add_parser(
        "verify", help="check a signed report, artifact, package or published folder"
    )
    check.add_argument("target", nargs="?", type=Path)
    check.add_argument("--published", metavar="NAME")
    check.add_argument("--workspace", type=Path)
    check.add_argument("--require-key", metavar="FINGERPRINT")
    check.add_argument("--json", action="store_true")
    retake = commands.add_parser(
        "retake", help="a changed agent takes its exam again, or tries on practice cases"
    )
    retake.add_argument("folder", type=Path)
    retake.add_argument("--profile", required=True, type=Path)
    retake.add_argument("--practice", action="store_true")
    retake.add_argument("--yes", action="store_true")
    retake.add_argument("--json", action="store_true")
    undo = commands.add_parser("undo", help="put back the version of an agent that passed")
    undo.add_argument("folder", type=Path)
    undo.add_argument("--profile", type=Path)
    undo.add_argument("--workspace", type=Path)
    undo.add_argument("--json", action="store_true")
    keys = commands.add_parser("keys", help="Ed25519 keys: make, list, trust, sign")
    key_actions = keys.add_subparsers(dest="keys_command", required=True)
    generate = key_actions.add_parser("generate")
    generate.add_argument("--name", default="this host")
    generate.add_argument("--pub", type=Path)
    listing = key_actions.add_parser("list")
    listing.add_argument("--json", action="store_true")
    trusting = key_actions.add_parser("trust")
    trusting.add_argument("public_key", type=Path)
    trusting.add_argument("--name", required=True)
    signing = key_actions.add_parser("sign")
    signing.add_argument("folder", type=Path)
    signing.add_argument("--name")
    for action in (generate, listing, trusting, signing):
        action.add_argument("--workspace", type=Path)
    judge = commands.add_parser(
        "judge", help="use a judge code: Nemotron through the judges' capped relay"
    )
    judge.add_argument("code")
    judge.add_argument("--url", help="the relay's address, ending in /v1")
    judge.add_argument("--workspace", type=Path)
    for name in ("status", "registry"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, type=Path)
        if name == "status":
            command.add_argument("name", nargs="?")
    _bench_parser(commands.add_parser("bench", help="measure presets on a frozen suite"))
    forge = commands.add_parser("forge", help="Forge delivery modes and reports")
    forge_actions = forge.add_subparsers(dest="forge_command", required=True)
    report = forge_actions.add_parser("report")
    report.add_argument("artifact", type=Path)
    report.add_argument("--json", action="store_true")
    pack = forge_actions.add_parser("pack")
    pack.add_argument("--profile", type=Path)
    pack.add_argument("--spec", type=Path)
    pack.add_argument("--source", type=Path, action="append", default=[])
    pack.add_argument("--term", action="append", default=[])
    pack.add_argument("--json", action="store_true")
    # The compile flags that change hashed settings, so the printed hash is the one a
    # compile with the same flags records in forge.json.
    pack.add_argument("--forge-usd", type=float)
    pack.add_argument("--forge-minutes", type=float)
    pack.add_argument("--forge-model")
    pack.add_argument("--forge-mode", choices=("agent", "template"))
    run_mode = forge_actions.add_parser("run")
    run_mode.add_argument("request_or_spec")
    run_mode.add_argument("--profile", required=True, type=Path)
    run_mode.add_argument("--input", required=True)
    run_mode.add_argument("--cases", type=Path)
    run_mode.add_argument("--reuse-installed", action="store_true")
    run_mode.add_argument("--json", action="store_true")
    _forge_mode_flags(run_mode)
    integrate = forge_actions.add_parser("integrate")
    integrate.add_argument("request")
    integrate.add_argument("--profile", required=True, type=Path)
    integrate.add_argument("--repo", required=True, type=Path)
    integrate.add_argument("--into", required=True)
    integrate.add_argument("--approved-spec", type=Path)
    integrate.add_argument("--package")
    placement = integrate.add_mutually_exclusive_group()
    placement.add_argument("--stage-only", action="store_true")
    placement.add_argument("--package-only", action="store_true")
    integrate.add_argument("--reuse-installed", action="store_true")
    integrate.add_argument("--json", action="store_true")
    _forge_mode_flags(integrate)
    return parser


def _forge_mode_flags(command: argparse.ArgumentParser) -> None:
    """``forge run`` / ``forge integrate`` force ``enabled`` and ``trigger=always``."""
    command.add_argument("--forge-usd", type=float)
    command.add_argument("--forge-minutes", type=float)
    command.add_argument("--forge-model")
    command.add_argument("--forge-mode", choices=("agent", "template"))
    command.add_argument("--yes", action="store_true")
    command.add_argument("--audit", type=Path)


def _forge_flags(command: argparse.ArgumentParser) -> None:
    """``--forge*`` and ``--yes`` (Section 12.2): overrides for this invocation only.
    ``--audit DIR`` (Phase 14) copies every Forge submission into DIR."""
    command.add_argument("--forge", action="store_true")
    command.add_argument("--forge-usd", type=float)
    command.add_argument("--forge-minutes", type=float)
    command.add_argument("--forge-model")
    command.add_argument("--forge-trigger", choices=("always", "failed"))
    command.add_argument("--forge-mode", choices=("agent", "template"))
    command.add_argument("--yes", action="store_true")
    command.add_argument("--audit", type=Path)


def _forge_audit(args: argparse.Namespace) -> ForgeAudit | None:
    """Phase 14, Section 8.1: ``--audit DIR``, else ``ARCHITECT_FORGE_AUDIT_DIR``, for the
    compile-family commands; ``audit_unwritable`` before any spend when DIR cannot be
    written."""
    audited = args.command in ("compile", "create", "do", "recompile") or (
        args.command == "forge" and args.forge_command in ("run", "integrate")
    )
    directory = audit_directory(getattr(args, "audit", None)) if audited else None
    if directory is None:
        return None
    try:
        return ForgeAudit(directory)
    except AuditError:
        raise CLIError("audit_unwritable") from None


def _forge_overrides(args: argparse.Namespace) -> dict[str, object]:
    """The ``--forge*`` flags as ``ForgeSettings`` fields; nothing when none is given."""
    values: dict[str, object] = {}
    if getattr(args, "forge", False):
        values["enabled"] = True
    if getattr(args, "forge_usd", None) is not None:
        values["max_usd"] = args.forge_usd
    if getattr(args, "forge_minutes", None) is not None:
        values["max_s"] = args.forge_minutes * 60.0
    if getattr(args, "forge_model", None) is not None:
        values["agent_model"] = args.forge_model
    if getattr(args, "forge_trigger", None) is not None:
        values["trigger"] = args.forge_trigger
    if getattr(args, "forge_mode", None) is not None:
        values["mode"] = args.forge_mode
    return values


def forge_settings(base: ForgeSettings, overrides: dict[str, object]) -> ForgeSettings:
    """``base`` with ``overrides``; an out-of-range dollar or minute cap is a fixed code."""
    from pydantic import ValidationError

    try:
        return ForgeSettings.model_validate({**base.model_dump(), **overrides})
    except ValidationError as exc:
        fields = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        if fields & {"max_usd", "max_s"}:
            raise CLIError("forge_budget_invalid") from None
        raise CLIError("invalid_arguments") from None


def _with_forge(profile: Profile, args: argparse.Namespace) -> Profile:
    """The profile with this invocation's Forge overrides; unchanged without flags."""
    overrides = _forge_overrides(args)
    if not overrides:
        return profile
    settings = forge_settings(forge_settings_of(profile.compile), overrides)
    compile_config = profile.compile.model_copy(update={"forge": settings})
    updated = profile.model_copy(update={"compile": compile_config})
    updated._base, updated._source = profile._base, profile._source
    return updated


def _known_spec(args: argparse.Namespace, services: Services) -> Spec | None:
    """The spec a compile will use when it is known without inference (no spend)."""
    try:
        if args.command == "recompile":
            return services.registry.get(args.name).spec
        if args.command == "compile":
            if args.approved_spec is not None:
                return _read_spec(args.approved_spec)
            source = _source(args.request_or_spec)
            return source if isinstance(source, Spec) else None
        if args.command == "create" and args.approved_spec is not None:
            return _read_spec(args.approved_spec)
    except (CLIError, RegistryError):
        return None
    return None


def _forge_consent(args: argparse.Namespace, services: Services) -> object | None:
    """Show the summed cap and ask before any spend; no consent is ``forge_cancelled``.

    Phase 10 Section 11.3: under ``roles.policy = "evidence"`` the ``forge_agent`` role
    is resolved first and ``forge agent: <model> (mode M, turns N)`` prints immediately
    before the unchanged Phase 9 cap line. Under the default ``pinned`` policy the
    agent is exactly the Phase 9 one and the output is byte-identical to Phase 9.
    Returns the evidence-routed role choice (None when pinned or Forge is off), whose
    Section 11.4 outcome ``_dispatch`` records once the command has finished.
    """
    profile = getattr(services, "profile", None)
    if not isinstance(profile, Profile):
        return None  # no profile-backed services: Forge cannot be enabled
    settings = forge_settings_of(profile.compile)
    if not settings.enabled:
        return None  # opt-in: nothing is printed or asked
    spec = _known_spec(args, services)
    choice: object | None = None
    if getattr(getattr(services, "roles", None), "policy", "pinned") == "evidence":
        if services.compiler.smoke_factory is None:
            raise CLIError("forge_disabled")
        from architect.forge_modes.errors import ForgeModeError
        from architect.forge_modes.run import consent_text

        try:
            text, _settings, choice = consent_text(services, services.compiler, settings, spec)
        except ForgeModeError as exc:
            raise CLIError(exc.code) from None
        print(text, file=sys.stderr, flush=True)
    else:
        verifications: int | None = None
        if spec is not None:
            counter = getattr(services.compiler.library, "verification_count", None)
            verifications = counter(spec.spec_hash) if callable(counter) else 0
        from architect.session.forge import cap_line

        if services.compiler.smoke_factory is None:
            # Forge runs only with a smoke floor (Section 8.2); it must never silently not run.
            raise CLIError("forge_disabled")
        compile_usd = profile.compile.compile_budget.cost_usd
        print(cap_line(compile_usd, settings, verifications), file=sys.stderr, flush=True)
    if getattr(args, "yes", False):
        return choice
    if not _interactive() or not _ask(CONFIRM_PROMPT, json_mode=True):
        from architect.forge_modes._routing import record_role

        record_role(services, choice, "excluded")  # an operator cancel (Section 11.4)
        raise CLIError("forge_cancelled")
    return choice


async def _with_forge_outcome(
    args: argparse.Namespace, services: Services, choice: object
) -> int:
    """Run a compile-path command and record its ``forge_agent`` outcome (Section 11.4):
    ``success`` when Forge's accepted submission was licensed, ``failure`` when Forge ran
    otherwise, ``excluded`` when Forge never ran. Only under ``roles.policy = "evidence"``;
    the pinned default never gets here, so its output stays byte-identical."""
    from architect.forge_modes._routing import record_role
    from architect.forge_modes.run import forge_role_outcome

    compiler = services.compiler
    previous = compiler.forge_listener
    forged: list[object] = []

    def listen(result: Any) -> None:
        forged.append(result)
        if previous is not None:
            previous(result)

    compiler.forge_listener = listen
    outcome: Any = "excluded"
    try:
        code = await _command(args, services)
        outcome = forge_role_outcome(code == 0, forged)
        return code
    except (KeyboardInterrupt, asyncio.CancelledError):
        outcome = "excluded"  # an operator cancel
        raise
    except BaseException:
        outcome = "failure" if forged else "excluded"
        raise
    finally:
        compiler.forge_listener = previous
        record_role(services, choice, outcome)


def _delivery_count(text: str) -> int | str:
    """``--delivery N|all``: a count of replay cases, or every case of a public split."""
    if text == "all":
        return text
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid_arguments") from None


def _bench_parser(bench: argparse.ArgumentParser) -> None:
    """``architect bench run|compare|report|audit|list|pages|probe`` (Phase 8 Section 10).

    Tier, suite and variant names are validated by the bench, not argparse, so a
    wrong name is its fixed code (``bench_tier_unknown``...) rather than exit 2.
    Phase 14 adds the suite v2 run flags, ``compare --pair-arms``, ``report`` and
    the ``audit`` replay.
    """
    actions = bench.add_subparsers(dest="bench_command", required=True)
    run = actions.add_parser("run")
    run.add_argument("--tier")  # required unless --resume
    run.add_argument("--suite", default="v1")
    run.add_argument("--provider", choices=("fake", "profile"))
    run.add_argument("--profile", type=Path)
    run.add_argument("--models", default="")
    run.add_argument("--presets", default="")
    run.add_argument("--variant", action="append", dest="variants")
    run.add_argument("--route", default="")
    run.add_argument("--max-usd", type=float)
    run.add_argument("--seed", type=int, default=7)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--size", type=int)
    run.add_argument("--max-candidates", type=int, default=2)
    run.add_argument("--arc-games", type=int, default=5)
    run.add_argument("--arc-actions", type=int, default=24)
    run.add_argument("--arc-cache", type=Path)
    run.add_argument("--delivery", type=_delivery_count, default=0, metavar="N|all")
    run.add_argument("--timeout-s", type=float, default=3600.0)
    run.add_argument("--label", default="")
    run.add_argument("--out", type=Path, default=Path(".architect") / "bench")
    run.add_argument("--json", action="store_true")
    run.add_argument("--yes", action="store_true")
    run.add_argument("--forge-usd", type=float)
    run.add_argument("--forge-minutes", type=float)
    run.add_argument("--forge-model")
    # Phase 10 Section 14: replays of the routed arm (validated by BenchConfig, 1 to 100).
    run.add_argument("--rounds", type=int, default=1)
    # Phase 11 Section 3: continue a stopped run with its saved settings.
    run.add_argument("--resume", metavar="BENCH_ID")
    # Phase 14 Section 10.5: suite v2 and fair-arm flags.
    run.add_argument("--tasks", default="")
    run.add_argument("--no-tuning-replay", action="store_true")
    run.add_argument("--plain", action="store_true")
    run.add_argument("--reply-json", choices=("strict", "first", "last", "schema"))
    run.add_argument("--floor-split", choices=("evidence", "rehearsal"), default="evidence")
    run.add_argument("--delivery-split", choices=("delivery", "pilot"), default="delivery")
    run.add_argument("--audit", type=Path, metavar="DIR")
    run.set_defaults(
        run_defaults={action.dest: action.default for action in run._actions if action.dest}
    )
    roles_arm = actions.add_parser("roles")
    roles_arm.add_argument("--policy", required=True, choices=("pinned", "evidence"))
    roles_arm.add_argument("--json", action="store_true")
    compare = actions.add_parser("compare")
    # Exactly two sources without --pair-arms (base, candidate); any number with it.
    compare.add_argument("sources", nargs="+", metavar="SOURCE")
    compare.add_argument("--out", type=Path, default=Path(".architect") / "bench")
    compare.add_argument("--json", action="store_true")
    compare.add_argument("--expect", choices=("identical-scores",))
    compare.add_argument("--allow-fingerprint-mismatch", action="store_true")
    compare.add_argument("--pair-variants", metavar="BASE:CAND")
    _pair_arm_flags(compare)
    report = actions.add_parser("report")
    report.add_argument("sources", nargs="+", metavar="SOURCE")
    report.add_argument("--md", action="store_true")
    report.add_argument("--footer", type=Path, metavar="FILE")
    report.add_argument("--out", type=Path, default=Path(".architect") / "bench")
    _pair_arm_flags(report)
    audit = actions.add_parser("audit")
    audit.add_argument("audit_dir", type=Path, metavar="AUDIT_DIR")
    audit.add_argument("--profile", required=True, type=Path)
    audit.add_argument("--since", metavar="ISO")
    audit.add_argument("--until", metavar="ISO")
    audit.add_argument("--all", action="store_true")
    audit.add_argument("--max-usd", type=float)
    audit.add_argument("--yes", action="store_true")
    audit.add_argument("--out", type=Path, default=Path(".architect") / "bench" / "audits")
    audit.add_argument("--json", action="store_true")
    listing = actions.add_parser("list")
    listing.add_argument("--out", type=Path, default=Path(".architect") / "bench")
    listing.add_argument("--profile", type=Path)
    listing.add_argument("--suite", default="v1")
    listing.add_argument("--json", action="store_true")
    pages = actions.add_parser("pages")
    pages.add_argument("--seed", type=int, required=True)
    pages.add_argument("--count", type=int, required=True)
    pages.add_argument("--style", choices=("clean", "scan", "dense"), default="clean")
    pages.add_argument("--dpi", type=int, default=96)
    pages.add_argument("--kind", choices=("vision", "digitizer"), default="vision")
    pages.add_argument("--out", type=Path, required=True)
    pages.add_argument("--png", action="store_true")
    probe = actions.add_parser("probe")
    probe.add_argument("--profile", type=Path, required=True)
    probe.add_argument("--models", default="")
    probe.add_argument("--json", action="store_true")


def _pair_arm_flags(command: argparse.ArgumentParser) -> None:
    """``--pair-arms BASE,CAND`` (repeatable) and the statistics it uses (Section 12.1)."""
    command.add_argument("--pair-arms", action="append", metavar="BASE,CAND")
    command.add_argument("--margin", type=float, default=0.05)
    command.add_argument("--alpha", type=float, default=0.05)
    command.add_argument("--bootstrap", type=int, default=10_000)
    command.add_argument("--seed", type=int, default=0)


def _emit(value: JsonValue) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True))


def _error(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 1


def _read_spec(path: Path) -> Spec:
    try:
        return Spec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CLIError("invalid_spec_file") from None


def _source(value: str) -> Spec | str:
    if value.lstrip().startswith("{"):
        try:
            return Spec.model_validate_json(value)
        except ValueError:
            raise CLIError("invalid_spec") from None
    path = Path(value)
    try:
        if path.is_file():
            return _read_spec(path)
    except OSError:
        pass  # A long plain-language request is not a filesystem path.
    if value.lower().endswith(".json"):
        raise CLIError("invalid_spec_file")
    return value


def _proposal(spec: Spec, workspace: Path) -> Path:
    directory = workspace / "proposals"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{spec.spec_hash}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".proposal-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(spec.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _route_summary(result: CompileResult) -> dict[str, JsonValue]:
    """Content-free: the action, the target's digest and the fixed codes."""
    decision = result.route
    assert decision is not None
    target = decision.target
    return {
        "action": decision.action,
        "target_digest": None if target is None else target.artifact_digest,
        "codes": list(decision.codes),
        "note": "routing orders licensed versions; Stage 4 alone decides every license",
    }


def _compiled(result: CompileResult, services: Services, *, name: str | None = None) -> int:
    # The route is shown by ``architect route``; a compile's JSON keeps its Phase 9 keys
    # and gains ``reused``/``route`` only when an installed entry was reused.
    payload: dict[str, JsonValue] = result.model_dump(mode="json", exclude={"route", "reused"})
    if result.reused and result.route is not None:
        payload["reused"] = True
        payload["route"] = _route_summary(result)
    if result.approval_required:
        payload["proposal_path"] = str(_proposal(result.spec, services.workspace))
        payload["accepted"] = False
        _emit(payload)
        return 0  # A successful review checkpoint, not a licensed artifact.
    artifact = result.artifact
    accepted = artifact is not None and artifact.licensed and not result.failures
    payload["accepted"] = accepted
    if (
        accepted
        and artifact is not None
        and artifact.spec.lifecycle == Lifecycle.INSTALL
        and not result.reused  # a reused entry is already installed; never reinstall it
    ):
        entry = services.registry.install(name or artifact.spec.spec_hash, artifact)
        payload["registry_entry"] = entry.model_dump(mode="json")
    _emit(payload)
    return 0 if accepted else _error("compile_rejected")


async def _dispatch(args: argparse.Namespace, services: Services) -> int:
    if getattr(args, "no_route", False):
        # ``--no-route`` (A/B comparison): this invocation's compiles, including the
        # builder's and the orchestrator's, run exactly as a Compiler without a router.
        services.compiler.router = None
    if args.command in ("compile", "recompile", "do", "create"):
        # Blocking stdin read before any compile, as ``bench run`` asks before its loop.
        choice = await asyncio.to_thread(_forge_consent, args, services)
        if choice is not None:
            return await _with_forge_outcome(args, services, choice)
    return await _command(args, services)


async def _command(args: argparse.Namespace, services: Services) -> int:
    if args.command == "route":
        return await _route_command(args, services)
    if args.command == "roles":
        from architect.session.routes import roles_table

        if args.json:
            _emit(_roles_json(services))
        else:
            print(roles_table(services), end="")
        return 0
    if args.command == "forge":
        return await _forge_mode(args, services)
    if args.command == "export":
        return await _export_verify(args, services)
    if args.command == "compile":
        if args.approved_spec is not None:
            # Do not infer again or merge an unreviewed request into this spec.
            source: Spec | str = _read_spec(args.approved_spec)
        else:
            source = _source(args.request_or_spec)
        result = await services.compiler.compile(
            source, approved=args.approved_spec is not None, **_no_route(args)
        )
        return _compiled(result, services)
    if args.command == "recompile":
        incumbent = services.registry.get(args.name)
        # A recompile is a fresh Stage 4 attempt (Section 8.5): it may warm-start through
        # the router but never reuses, not even under a standing ``cli`` grant, so an
        # acceptance here always comes from a Stage 4 run in this call.
        result = await services.compiler.compile(
            incumbent.spec, approved=True, allow_reuse=False, **_no_route(args)
        )
        return _compiled(result, services, name=args.name)
    if args.command == "run":
        try:
            task: JsonValue = TypeAdapter(JsonValue).validate_json(args.input)
            json.dumps(task, allow_nan=False)
        except (ValueError, TypeError):
            raise CLIError("invalid_input_json") from None
        directory = Path(args.artifact)
        if (directory / ".consumed").exists():
            raise ArtifactExpiredError("Artifact execution attempt already consumed")
        artifact = HarnessArtifact.load(directory)
        run = await services.runtime.run(artifact, task, services.adapter_factory(artifact.model))
        _emit(run.model_dump(mode="json"))
        return 0
    repo = args.repo.resolve()
    if not repo.is_dir():
        raise CLIError("invalid_repository")
    if args.command == "do":
        # The grants are passed only when given, so an unflagged ``do`` is exactly Phase 9.
        grants: dict[str, Any] = {}
        if args.reuse_installed:
            grants["reuse_installed"] = True
        if args.no_route:
            grants["no_route"] = True
        orchestration = await services.orchestrator.run(args.goal, repo=repo, **grants)
    else:
        from architect.orchestrator.features import FeatureBuilder

        approved = _read_spec(args.approved_spec) if args.approved_spec is not None else None
        builder = FeatureBuilder(services)
        orchestration = await builder.create(args.request, repo, approved_spec=approved)
    _emit(orchestration.model_dump(mode="json"))
    return 0 if orchestration.accepted else _error("orchestration_rejected")



# ----------------------------------------------------------------------- routing


def _no_route(args: argparse.Namespace) -> dict[str, Any]:
    """``--no-route`` only when given (the compile's origin is ``cli`` by default)."""
    return {"no_route": True} if getattr(args, "no_route", False) else {}


def _input_json(value: str) -> JsonValue:
    """``--input``/``--run`` JSON, or ``@FILE`` for a JSON file; NaN is refused."""
    text = value
    if value.startswith("@"):
        try:
            text = Path(value[1:]).read_text(encoding="utf-8")
        except OSError:
            raise CLIError("invalid_input_json") from None
    try:
        task: JsonValue = TypeAdapter(JsonValue).validate_json(text)
        json.dumps(task, allow_nan=False)
    except (ValueError, TypeError):
        raise CLIError("invalid_input_json") from None
    return task


async def _stage0(text: str, services: Services) -> Spec:
    """Stage 0 for ``route <prose>``: one capped inference call, its cost on stderr."""
    from architect.compiler.inference import _INFERENCE_BUDGET, infer_spec
    from architect.runtime.wallet import compilation_wallet

    compiler = services.compiler
    adapter = compiler.inference_adapter or compiler.adapter_factory("fake")
    with compilation_wallet(_INFERENCE_BUDGET) as wallet:
        inferred = await infer_spec(text, adapter, compiler.runtime, None)
    print(f"route: stage 0 cost ${wallet.cost_usd:.4f} (API-equivalent)", file=sys.stderr,
          flush=True)
    return inferred.spec


async def _route_command(args: argparse.Namespace, services: Services) -> int:
    """``architect route``: the decision; it never compiles. ``--run`` executes only a
    granted ``reuse_installed`` (Section 10.1) and nothing else."""
    from architect.session.routes import advice, route_screen

    source = _source(args.request_or_spec)
    if args.run_input is not None:
        if not isinstance(source, Spec):
            raise CLIError("invalid_arguments")  # --run takes an explicit Spec only
        task = _input_json(args.run_input)
        return await _route_run(args, services, source, task)
    spec = source if isinstance(source, Spec) else await _stage0(source, services)
    decision = await asyncio.to_thread(advice, services, spec, origin=args.origin)
    if args.json:
        _emit(decision.model_dump(mode="json"))
    else:
        print(route_screen(decision, profile=str(args.profile)), end="")
    return 0


async def _route_run(
    args: argparse.Namespace, services: Services, spec: Spec, task: JsonValue
) -> int:
    from datetime import UTC, datetime

    from architect.routing.log import OutcomeRecord
    from architect.routing.router import RouteError, load_reuse_target
    from architect.session.routes import probe_for, route_screen

    router = services.router
    if router is None or not args.yes:
        # ``--run`` executes an installed harness only with the per-call ``--yes`` grant;
        # a standing ``cli`` grant in ``routing.reuse_origins`` never runs one without it.
        return _error("route_no_reuse")
    probe = probe_for(services, spec, origin="cli", grant=True)
    decision = await asyncio.to_thread(router.route, probe)
    if not args.json:
        print(route_screen(decision, profile=str(args.profile)), end="", file=sys.stderr)
    try:
        artifact = await asyncio.to_thread(load_reuse_target, decision, services.registry, spec)
    except RouteError as exc:
        return _error(exc.code)
    run = await services.runtime.run(artifact, task, services.adapter_factory(artifact.model))
    try:
        router.outcome(
            decision,
            OutcomeRecord(
                decision_id=decision.decision_id,
                timestamp=datetime.now(UTC),
                outcome="delivered",
                stage4_passed=None,
                evidence_class=router.index.evidence_class(artifact.model),
                delivery_cost_usd=run.cost_usd,
            ),
        )
    except Exception:
        pass  # P5: an unrecorded outcome never breaks a delivery.
    payload = run.model_dump(mode="json")
    payload["reused"] = True
    payload["route"] = {"action": decision.action, "codes": list(decision.codes),
                        "target_digest": artifact.artifact_digest}
    _emit(payload)
    return 0


def _roles_json(services: Services) -> JsonValue:
    """``roles --json``: the same content as the table (Section 13.1), as data."""
    from architect.session.routes import role_evidence

    value: JsonValue = json.loads(json.dumps(role_evidence(services), allow_nan=False))
    return value


def _consent(args: argparse.Namespace) -> Callable[[str], bool]:
    """Show each consent text on stderr; ``--yes`` or an interactive ``y`` answers it."""

    from architect.forge_modes.run import wrapped

    def ask(text: str) -> bool:
        # Wrapped at 100 columns like the session; the text itself is unchanged.
        print(wrapped(text), file=sys.stderr, flush=True)
        if getattr(args, "yes", False):
            return True
        return _interactive() and _ask(CONFIRM_PROMPT, json_mode=True)

    return ask


def _mode_settings(args: argparse.Namespace, profile: Profile) -> ForgeSettings:
    values: dict[str, object] = {}
    if args.forge_usd is not None:
        values["max_usd"] = args.forge_usd
    if args.forge_minutes is not None:
        values["max_s"] = args.forge_minutes * 60.0
    if args.forge_model is not None:
        values["agent_model"] = args.forge_model
    if args.forge_mode is not None:
        values["mode"] = args.forge_mode
    return forge_settings(forge_settings_of(profile.compile), values)


_INTEGRATE_OK = frozenset({"integrate_reused_installed"})


async def _forge_mode(args: argparse.Namespace, services: Services) -> int:
    """``forge run`` and ``forge integrate`` (Section 12); refusals are fixed codes."""
    from architect.forge_modes import ForgeModeError, IntegrateResult, forge_integrate, forge_run

    settings = _mode_settings(args, services.profile)
    try:
        if args.forge_command == "run":
            task = _input_json(args.input)
            source = _source(args.request_or_spec)
            result = await forge_run(
                source,
                task,
                services,
                settings,
                consent=_consent(args),
                cases=args.cases,
                reuse_installed=args.reuse_installed,
            )
            _emit(result.model_dump(mode="json"))
            first = result.codes[0] if result.codes else "forge_run_unlicensed"
            delivered = first in {"forge_run_delivered", "forge_run_reused_installed"}
            return 0 if delivered else _error(first)
        repo = args.repo.resolve()
        if not repo.is_dir():
            raise CLIError("invalid_repository")
        approved = _read_spec(args.approved_spec) if args.approved_spec is not None else None
        outcome = await forge_integrate(
            args.request,
            repo,
            services,
            settings,
            into=args.into,
            package=args.package,
            approved_spec=approved,
            consent=_consent(args),
            stage_only=args.stage_only,
            package_only=args.package_only,
            reuse_installed=args.reuse_installed,
        )
    except ForgeModeError as exc:
        return _error(exc.code)
    if not isinstance(outcome, IntegrateResult):
        return _compiled(outcome, services)
    _emit(outcome.model_dump(mode="json"))
    failed = [code for code in outcome.codes if code not in _INTEGRATE_OK]
    return _error(failed[0]) if failed else 0


async def _export_verify(args: argparse.Namespace, services: Services | None) -> int:
    """``export verify <package-dir>`` (Section 12.7): 0 only when every check passes."""
    from architect.forge_modes import ExportError, verify_export
    from architect.session.exports import evidence_table, reverify
    from architect.trust import workspace_scope

    package = args.package.resolve()
    registry = services.registry if services is not None else None
    try:
        # Only configured keys count (never ./.architect on its own); --workspace names them.
        with workspace_scope(args.workspace):
            check = await asyncio.to_thread(verify_export, package, registry=registry)
    except ExportError as exc:
        return _error(exc.code)
    payload: dict[str, JsonValue] = {"check": check.model_dump(mode="json")}
    if args.reverify:
        if services is None:
            raise CLIError("invalid_arguments")  # a paid re-verification needs a profile
        payload["reverify"] = await reverify(package, services, _consent(args))
    if args.json:
        _emit(payload)
    else:
        print(evidence_table(package, check), end="")
        if "reverify" in payload:
            print(json.dumps(payload["reverify"], sort_keys=True))
    passed = (
        check.integrity and check.pins and check.children and check.licensed is True
        and not check.codes
    )
    if not passed:
        return _error(check.codes[0] if check.codes else "export_unverified")
    return 0


# ------------------------------------------------------------------------- trust (W7)

_KEY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _verify(args: argparse.Namespace) -> int:
    """``verify <path>`` or ``verify --published <name>``: 0 only when verified.

    ``--require-key`` takes the full fingerprint (64 hex digits, as ``keys list`` shows
    it); a shorter or other spelling is ``invalid_arguments``.
    """
    from architect.trust import workspace_scope
    from architect.verdict import verify_path, verify_published

    if (args.target is None) == (args.published is None):
        raise CLIError("invalid_arguments")
    required = args.require_key
    if required is not None:
        required = required.strip().lower()
        if _FINGERPRINT.fullmatch(required) is None:
            raise CLIError("invalid_arguments")
    with workspace_scope(args.workspace):
        if args.published is not None:
            verdict = verify_published(args.published, require_key=required)
        else:
            verdict = verify_path(args.target, require_key=required)
    if args.json:
        _emit(verdict.facts())
    else:
        print(verdict.text(), end="")
    if not verdict.verified:
        return _error(verdict.code or "verify_target_unknown")
    return 0


# ------------------------------------------------------------------------- retake

RETAKE_REFUSED = 2


def _shared_workspace(profile: Profile) -> Profile:
    """A profile that names ``~/.architect`` works where the app and the placed package
    do: ``ARCHITECT_WORKSPACE`` moves it (``profiles.in_workspace``)."""
    from architect.agent.app import agent_workspace
    from architect.profiles import in_workspace

    return in_workspace(profile, agent_workspace(Path.cwd(), None))


def _workspace_key(workspace: Path) -> None:
    """Sign and check with the workspace's own key unless a variable names another, as a
    session does: the key that signed the agent's first report lives there."""
    from architect.trust import SIGNING_KEY_ENV, SIGNING_KEY_NAME

    os.environ.setdefault(SIGNING_KEY_ENV, str(workspace / SIGNING_KEY_NAME))


def _plain(lines: Sequence[str], *, json_mode: bool) -> None:
    """Plain result lines: stdout, or stderr when stdout carries the JSON result."""
    for line in lines:
        print(line, file=sys.stderr if json_mode else sys.stdout, flush=True)


def _retake_refused(code: str, lines: Sequence[str], *, json_mode: bool) -> int:
    if json_mode:
        _emit({"refused": code, "lines": list(lines)})
    else:
        _plain(lines, json_mode=False)
    return RETAKE_REFUSED


def _retake_consent(args: argparse.Namespace) -> Callable[[str], bool]:
    """One line with the attempt, its cases and the cost; ``--yes`` or a typed ``y``."""

    def ask(text: str) -> bool:
        if args.yes or not _interactive():
            _plain((text,), json_mode=args.json)
            return bool(args.yes)
        return _ask(f"{text} {CONFIRM_PROMPT}", json_mode=args.json)

    return ask


def _retake_progress() -> Callable[[int, int], None] | None:
    """A count of answered cases on stderr, for a person watching; never a score."""
    try:
        if not sys.stderr.isatty():
            return None
    except (OSError, ValueError):
        return None

    def show(done: int, total: int) -> None:
        end = "\n" if done >= total else ""
        sys.stderr.write(f"\r{done} of {total} answered. The score shows at the end.{end}")
        sys.stderr.flush()

    return show


def _retake(args: argparse.Namespace) -> int:
    """``retake <agent-folder> --profile <p> [--practice] [--yes] [--json]``."""
    from architect.session import retake as retakes

    profile = _shared_workspace(load_profile(args.profile))
    workspace = profile.resolve(profile.workspace)
    _workspace_key(workspace)
    folder = args.folder.resolve()
    services = build_services(profile, profile.workspace)
    try:
        state = retakes.status(folder, services.workspace, services)
        if state.state == "stopped" and state.can_retake and not args.practice:
            _plain(state.lines, json_mode=args.json)
        run = retakes.practice if args.practice else retakes.retake
        extra = {} if args.practice else {"on_case": _retake_progress()}
        try:
            outcome = asyncio.run(run(folder, services, _retake_consent(args), **extra))
        except retakes.RetakeError as exc:
            lines = list(exc.lines)
            if exc.code == "cancelled" and not args.yes and not _interactive():
                lines.append("To go ahead, run it again with --yes.")
            return _retake_refused(exc.code, lines, json_mode=args.json)
    finally:
        services.close()
    if args.json:
        _emit(outcome.facts())
        return 0 if outcome.passed or outcome.kind == "practice" else 1
    _plain(outcome.lines, json_mode=False)
    if outcome.kind == "retake" and not outcome.passed:
        # What to type next, with the folder and the profile as they were given.
        _plain(
            (
                f"Undo: architect undo {args.folder}",
                "Try a change without using an attempt: architect retake "
                f"{args.folder} --profile {args.profile} --practice",
            ),
            json_mode=False,
        )
    return 0 if outcome.passed or outcome.kind == "practice" else 1


def _undo(args: argparse.Namespace) -> int:
    """``undo <agent-folder>``: the workspace is ``--workspace``, else the profile's, else
    ``ARCHITECT_WORKSPACE``, else ``~/.architect``."""
    from architect.agent.app import agent_workspace
    from architect.session import retake as retakes
    from architect.trust import workspace_scope

    if args.profile is not None and args.workspace is not None:
        raise CLIError("invalid_arguments")
    if args.profile is not None:
        profile = _shared_workspace(load_profile(args.profile))
        workspace = profile.resolve(profile.workspace)
    else:
        workspace = agent_workspace(Path.cwd(), args.workspace)
    if args.workspace is None:
        _workspace_key(workspace)
    try:
        with workspace_scope(workspace if args.workspace is not None else None):
            outcome = retakes.undo(args.folder.resolve(), workspace)
    except retakes.RetakeError as exc:
        return _retake_refused(exc.code, exc.lines, json_mode=args.json)
    if args.json:
        _emit(outcome.facts())
    else:
        _plain(outcome.lines, json_mode=False)
    return 0


def _judge(args: argparse.Namespace) -> int:
    """``judge <code>``: check the code at the judges' relay, then store it; 0 when stored."""
    from architect.agent.app import agent_workspace
    from architect.providers import judge

    workspace = agent_workspace(Path.cwd(), args.workspace)
    result = asyncio.run(judge.use_code(args.code, workspace, url=args.url))
    if not result.ok:
        print(judge.explain(result.code), file=sys.stderr)
        return _error(result.code or "judge_failed")
    print(judge.summary(result, workspace))
    print(_judge_next(Path.cwd(), workspace))
    return 0


def _judge_next(cwd: Path, workspace: Path) -> str:
    """What to run after ``judge``. A folder with a profile (examples/expense-app) or a
    workspace that has one asks no first-run questions."""
    from architect.session.bootstrap import discover_profile

    command = _architect_command(cwd)
    if discover_profile(cwd, workspace, None) is None:
        return (f"Next: run '{command}' here and press Enter at each first-run question, or "
                "open the desktop app and choose Nebius Token Factory.")
    return f"Next: run '{command}' here, or start a new conversation in the desktop app."


def _architect_command(cwd: Path) -> str:
    """``uv run architect`` in this checkout, else with ``--project``: from a folder that
    is a uv project of its own (examples/expense-app), plain ``uv run`` would make an
    environment there without Architect."""
    root = Path(__file__).resolve().parent.parent
    here = cwd.resolve()
    if here == root or not (root / "pyproject.toml").is_file():
        return "uv run architect"
    try:
        project = Path(os.path.relpath(root, here)).as_posix()
    except ValueError:  # another drive
        project = root.as_posix()
    if " " in project:
        project = f'"{project}"'
    return f"uv run --project {project} architect"


def _keys(args: argparse.Namespace) -> int:
    """``keys generate|list|trust|sign``; every refusal is one fixed code."""
    from architect.trust import TrustError, workspace_scope

    try:
        with workspace_scope(args.workspace):
            command = args.keys_command
            if command == "generate":
                return _keys_generate(args)
            if command == "list":
                return _keys_list(args)
            if command == "trust":
                return _keys_trust(args)
            return _keys_sign(args)
    except TrustError as exc:
        return _error(exc.code)


def _keys_generate(args: argparse.Namespace) -> int:
    from architect import trust

    if _KEY_NAME.fullmatch(args.name) is None:
        raise CLIError("invalid_arguments")
    if trust.load_trust().error == "trust_list_invalid":
        raise trust.TrustError("trust_list_invalid")  # a missing named list is created
    public_path = args.pub
    if public_path is not None:
        try:
            held = public_path.exists() and "BEGIN PUBLIC KEY" in public_path.read_text(
                encoding="ascii", errors="replace"
            )
        except OSError:
            raise CLIError("invalid_arguments") from None
        if held:
            raise trust.TrustError("key_exists")  # never replace a real public key
    path = trust.private_key_path()
    public = trust.generate_key(path)
    trust.add_trusted_key(public, args.name)
    pem = trust.public_key_pem(public)
    print(f"created Ed25519 key {trust.fingerprint(public)}")
    print(f"private key: {path} (keep it on this host; never commit or copy it)")
    print(f"this workspace now trusts it as {args.name}")
    print(f"trust list: {os.path.abspath(trust.trust_file_path())}")
    if public_path is not None:
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_bytes(pem.encode("ascii"))
        print(f"public key: {public_path}")
    else:
        print(pem, end="")
    return 0


def _keys_list(args: argparse.Namespace) -> int:
    from architect import trust

    listed = trust.load_trust()
    if listed.error is not None:
        raise trust.TrustError(listed.error)
    keys = sorted(listed.keys.values(), key=lambda key: (key.source != "shipped", key.name))
    private = trust.load_private_key()
    signing = trust.fingerprint(private.public_key()) if private is not None else None
    if args.json:
        _emit(
            {
                "trusted": [
                    {"fingerprint": key.fingerprint, "name": key.name, "source": key.source}
                    for key in keys
                ],
                "signing_key": signing,
            }
        )
        return 0
    if not keys:
        print("this workspace trusts no keys yet")
    for key in keys:
        print(f"{key.fingerprint}  {key.label}")
    if signing is None:
        print("this host holds no Ed25519 private key, so it signs nothing")
    else:
        print(f"this host signs with key {signing}")
    if listed.path is not None:
        print(f"trust list: {listed.path}")
    return 0


def _keys_trust(args: argparse.Namespace) -> int:
    from architect import trust

    if _KEY_NAME.fullmatch(args.name) is None:
        raise CLIError("invalid_arguments")
    try:
        if args.public_key.stat().st_size > 65_536:
            raise trust.TrustError("key_invalid")
        text = args.public_key.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise trust.TrustError("key_invalid") from None
    key = trust.parse_public_key(text)
    added = trust.add_trusted_key(key, args.name)
    state = "now trusts" if added else "already trusted"
    print(f"this workspace {state} key {trust.fingerprint(key)}")
    print(f"trust list: {os.path.abspath(trust.trust_file_path())}")
    return 0


def _keys_sign(args: argparse.Namespace) -> int:
    from architect import trust

    manifest = trust.sign_published(args.folder, args.name)
    count = len(manifest.files)
    print(
        f"signed {manifest.name}: {count} file{'' if count == 1 else 's'} with key "
        f"{manifest.ed25519.fingerprint[:16]}"
    )
    print(f"check it with: architect verify {args.folder}")
    return 0


# ------------------------------------------------------------------------- bench


def _names(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _route(text: str) -> dict[str, str]:
    from architect.bench import BenchError

    route: dict[str, str] = {}
    for item in _names(text):
        role, sep, model = item.partition("=")
        if not sep or not role.strip() or not model.strip() or role.strip() in route:
            raise BenchError("bench_route_invalid")
        route[role.strip()] = model.strip()
    return route


def _say(text: str, *, json_mode: bool) -> None:
    """Human lines go to stdout, or to stderr when stdout carries the JSON result."""
    print(text, file=sys.stderr if json_mode else sys.stdout, flush=True)


def _bench_profile(path: Path | None) -> Profile | None:
    from architect.bench import BenchError

    if path is None:
        return None
    try:
        return load_profile(path)
    except ProfileError:
        raise BenchError("bench_invalid_profile") from None


def _bench_config(args: argparse.Namespace) -> BenchConfig:
    from pydantic import ValidationError

    from architect.bench import SUITES, BenchError, suite_digest
    from architect.bench.runner import BenchConfig
    from architect.bench.suite import tiers_for

    suite_digest(args.suite)  # bench_suite_unknown (and v2 data checks) before anything else
    if args.tier not in tiers_for(args.suite):
        raise BenchError("bench_tier_unknown")
    tasks = _names(args.tasks)
    if set(tasks) - {task.id for task in SUITES[args.suite]}:
        raise BenchError("bench_task_unknown")
    # Section 8.1/10: the provider defaults to the profile; only --provider fake needs none.
    provider = args.provider or "profile"
    # The fake smoke tier carries its negative control (noisy) unless variants are named.
    fake_smoke = args.tier == "smoke" and provider == "fake"
    variants = tuple(args.variants or (("default", "noisy") if fake_smoke else ("default",)))
    if any(item not in _BENCH_VARIANTS for item in variants):
        raise BenchError("bench_variant_unknown")
    forge: dict[str, object] = {}
    if args.forge_usd is not None:
        forge["max_usd"] = args.forge_usd
    if args.forge_minutes is not None:
        forge["max_s"] = args.forge_minutes * 60.0
    if args.forge_model is not None:
        forge["agent_model"] = args.forge_model
    if forge:
        # The same fixed code as on compile/do/create/recompile (Section 12.4). Only the
        # flags given are passed on: the bench layers them over the profile's settings.
        forge_settings(ForgeSettings(), forge)
    if provider == "profile" and args.profile is None:
        raise BenchError("bench_profile_required")
    route = _route(args.route)
    if set(route) - {"vision", "solver", "verifier"}:
        raise BenchError("bench_route_invalid")
    try:
        return BenchConfig.model_validate(
            {
                "suite": args.suite,
                "tier": args.tier,
                "provider": provider,
                "profile": args.profile,
                "models": _names(args.models),
                "presets": _names(args.presets),
                "variants": variants,
                "route": route,
                "max_usd": args.max_usd,
                "seed": args.seed,
                "repeats": args.repeats,
                "size": args.size,
                "max_candidates": args.max_candidates,
                "arc_games": args.arc_games,
                "arc_actions": args.arc_actions,
                "arc_cache": args.arc_cache,
                "delivery_n": args.delivery,
                "label": args.label,
                "out": args.out,
                "timeout_s": args.timeout_s,
                "forge": forge or None,
                "rounds": args.rounds,
                "tasks": tuple(dict.fromkeys(tasks)),
                "tuning_replay": not args.no_tuning_replay,
                "plain": args.plain,
                "reply_json": args.reply_json,
                "floor_split": args.floor_split,
                "delivery_split": args.delivery_split,
                "audit": args.audit,
            }
        )
    except ValidationError:
        raise CLIError("invalid_arguments") from None


_BENCH_VARIANTS = ("default", "noisy", "images-all-stages", "forge", "template", "routed")


def _pair_arms(values: Sequence[str] | None) -> list[tuple[str, str]]:
    """``--pair-arms BASE,CAND`` values; each side is ``MODEL:VARIANT`` or a run label."""
    arms: list[tuple[str, str]] = []
    for value in values or ():
        base, sep, cand = value.partition(",")
        base, cand = base.strip(), cand.strip()
        if not sep or not base or not cand or "," in cand or base == cand:
            raise CLIError("invalid_arguments")
        arms.append((base, cand))
    return arms


def _pair_settings(args: argparse.Namespace) -> dict[str, Any]:
    """The statistics settings of ``--pair-arms`` (Section 12.1), checked."""
    if not 0 < args.alpha < 1 or not 0 <= args.margin < 1:
        raise CLIError("invalid_arguments")
    if not 1_000 <= args.bootstrap <= 100_000:
        raise CLIError("invalid_arguments")
    return {
        "alpha": args.alpha,
        "margin": args.margin,
        "bootstrap": args.bootstrap,
        "seed": args.seed,
    }


def _pair_variants(text: str | None) -> tuple[Variant, Variant] | None:
    """``BASE:CAND`` for ``compare --pair-variants``; both must be bench variants."""
    from architect.bench import BenchError

    if text is None:
        return None
    base, sep, cand = text.partition(":")
    if not sep:
        raise CLIError("invalid_arguments")
    if base not in _BENCH_VARIANTS or cand not in _BENCH_VARIANTS or base == cand:
        raise BenchError("bench_variant_unknown")
    return cast("Variant", base), cast("Variant", cand)


CONFIRM_PROMPT = "Continue? [y/N] "


def _interactive() -> bool:
    """True only for a console an operator can answer; a pipe, file or CI shell is not."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def _ask(prompt: str, *, json_mode: bool) -> bool:
    """Show ``prompt`` where the human lines go and read one answer; EOF is a no.

    The prompt never goes to stdout in ``--json`` mode, where stdout carries only
    the result object.
    """
    stream = sys.stderr if json_mode else sys.stdout
    stream.write(prompt)
    stream.flush()
    try:
        answer = sys.stdin.readline()
    except (EOFError, OSError, ValueError):
        answer = ""
    if not answer.endswith("\n"):
        stream.write("\n")  # end of input: end the prompt line before any error code
        stream.flush()
    return answer.strip().lower() in {"y", "yes"}


def _bench_run(args: argparse.Namespace) -> int:
    """Consent is explicit before any spend on a provider other than fake.

    ``--yes`` is the non-interactive consent. Without it the bound is computed and
    shown, then an interactive console is asked ``Continue? [y/N]``; anything but
    ``y`` (including end of input) and any non-interactive stdin is
    ``bench_cancelled`` with nothing spent. The question is asked before the event
    loop starts, so an interrupt there exits at once (130) with no worker thread.
    """
    from architect.bench import BenchError, render
    from architect.bench.runner import BenchProgress, Bound, bound, check_max_usd, run_bench

    resume: str | None = args.resume
    if resume is None:
        if args.tier is None:
            raise CLIError("invalid_arguments")  # --tier is required without --resume
        config = _bench_config(args)
    else:
        # A resumed run takes its settings from run.json; only --out, --json and --yes
        # may accompany it, so a changed flag is never silently ignored.
        free = {"resume", "out", "json", "yes", "help", "run_defaults"}
        defaults: dict[str, object] = args.run_defaults
        if any(
            getattr(args, dest, None) != default
            for dest, default in defaults.items()
            if dest not in free
        ):
            raise CLIError("invalid_arguments")
        from architect.bench.runner import load_resume

        config = load_resume(args.out, resume)
    profile = _bench_profile(config.profile) if config.provider == "profile" else None
    json_mode = bool(args.json)
    shown: dict[str, object] = {"key": None, "at": 0.0}

    def show(limits: Bound) -> None:
        _say(
            render.bound_line(config.tier, len(limits.models), limits.total_usd, limits.calls),
            json_mode=json_mode,
        )

    on_bound: Callable[[Bound], Awaitable[bool]] | None = None
    if config.provider != "fake" and not args.yes:
        limits = bound(config, profile=profile, resume=resume)
        check_max_usd(config, limits)  # a refused run never asks
        show(limits)
        if not _interactive() or not _ask(CONFIRM_PROMPT, json_mode=json_mode):
            raise BenchError("bench_cancelled")  # fail closed: no consent, no spend
    else:

        async def announce(limits: Bound) -> bool:
            show(limits)
            return True

        on_bound = announce

    def progress(item: BenchProgress) -> None:
        key = (item.done, item.stage)
        at = shown["at"]
        if key == shown["key"] and isinstance(at, float) and item.elapsed_s - at < 20.0:
            return
        shown["key"], shown["at"] = key, item.elapsed_s
        _say(
            render.progress_line(
                item.elapsed_s,
                item.done,
                item.total,
                item.task_id,
                item.model,
                item.stage,
                item.calls,
                item.cost_usd,
            ),
            json_mode=json_mode,
        )

    result = asyncio.run(
        run_bench(
            config, profile=profile, on_progress=progress, on_bound=on_bound, resume=resume
        )
    )
    if json_mode:
        _emit(
            {
                "bench_id": result.bench_id,
                "rows_path": str(result.rows_path),
                "summary": result.summary.model_dump(mode="json"),
                "bound_usd": result.bound_usd,
                "skipped": [list(item) for item in result.skipped],
            }
        )
    else:
        print(render.bench_table(result.rows), end="")
        print(render.wrap(f"rows written to {result.rows_path}"), end="")
    if any("forge_did_not_run" in row.failures for row in result.rows):
        # Forge must never silently not run: the rows are kept, the run fails.
        return _error("forge_did_not_run")
    if any("route_did_not_run" in row.failures for row in result.rows):
        # Phase 10 Section 14: nor may the router; the rows are kept, the run fails.
        return _error("route_did_not_run")
    return 0


def _bench_roles(args: argparse.Namespace) -> int:
    """``bench roles --policy pinned|evidence``: replay the committed fake role logs
    through the real ``RoleRouter`` ($0, no provider; ``synthetic_fake``)."""
    from architect.bench.router_sim import roles_arm, roles_table

    arm = roles_arm(args.policy)
    if args.json:
        print(arm.model_dump_json())
    else:
        print(roles_table(arm), end="")
    return 0


def _rows_at(reference: str, out: Path) -> list[BenchRow]:
    """A rows.jsonl path, a run directory, or a bench id under ``out``."""
    from architect.bench import BenchError, read_rows

    path = Path(reference)
    if path.is_dir():
        path = path / "rows.jsonl"
    elif not path.exists():
        path = out / reference / "rows.jsonl"
    if not path.is_file():
        raise BenchError("bench_rows_unreadable")
    return read_rows(path)


def _bench_compare(args: argparse.Namespace) -> int:
    """Two sources as before, or ``--pair-arms`` over any number of pooled sources."""
    from architect.bench import compare, pair_arms, render

    arms = _pair_arms(args.pair_arms)
    if arms:
        if args.pair_variants is not None or args.expect is not None:
            raise CLIError("invalid_arguments")
        settings = _pair_settings(args)
        rows = [row for source in args.sources for row in _rows_at(source, args.out)]
        paired = pair_arms(
            rows, arms, allow_fingerprint_mismatch=args.allow_fingerprint_mismatch, **settings
        )
        if args.json:
            print(paired.model_dump_json())
        else:
            print(render.pairs_table(paired), end="")
        return 0
    if len(args.sources) != 2:
        raise CLIError("invalid_arguments")
    comparison = compare(
        _rows_at(args.sources[0], args.out),
        _rows_at(args.sources[1], args.out),
        allow_fingerprint_mismatch=args.allow_fingerprint_mismatch,
        expect=args.expect,
        pair_variants=_pair_variants(args.pair_variants),
    )
    if args.json:
        print(comparison.model_dump_json())
    else:
        print(render.compare_table(comparison), end="")
    return 0  # A regression verdict is data, not a failure.


def _bench_report(args: argparse.Namespace) -> int:
    """``bench report``: rows of one suite as ASCII or Markdown (Section 13.1)."""
    from architect.bench import pair_arms, render

    arms = _pair_arms(args.pair_arms)
    settings = _pair_settings(args)
    footer: str | None = None
    if args.footer is not None:
        try:
            footer = args.footer.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CLIError("invalid_arguments") from None
    rows = [row for source in args.sources for row in _rows_at(source, args.out)]
    paired = pair_arms(rows, arms, **settings) if arms else None
    print(render.report(rows, markdown=args.md, pairs=paired, footer=footer), end="")
    return 0


def _bench_audit(args: argparse.Namespace) -> int:
    """``bench audit``: replay audited Forge submissions on their exam (Section 13.2)."""
    from architect.bench.audit import AuditRequest, replay_audit

    try:
        request = AuditRequest.from_flags(
            args.audit_dir,
            since=args.since,
            until=args.until,
            include_all=args.all,
            max_usd=args.max_usd,
            out=args.out,
        )
    except ValueError:
        raise CLIError("invalid_arguments") from None
    profile = _bench_profile(args.profile)
    assert profile is not None  # --profile is required
    json_mode = bool(args.json)

    def consent(line: str) -> bool:
        _say(line, json_mode=json_mode)
        if args.yes:
            return True
        return _interactive() and _ask(CONFIRM_PROMPT, json_mode=json_mode)

    result = asyncio.run(replay_audit(request, profile, consent=consent))
    if json_mode:
        print(result.model_dump_json())
    else:
        from architect.bench.audit import audit_table

        print(audit_table(result), end="")
    return 0


def _recorded(out: Path) -> list[tuple[str, int, str, str]]:
    from architect.bench import BenchError, read_rows

    found: list[tuple[str, int, str, str]] = []
    if not out.is_dir():
        return found
    for rows_path in sorted(out.glob("*/rows.jsonl")):
        try:
            rows = read_rows(rows_path)
        except BenchError:
            continue
        tiers = ",".join(sorted({row.tier for row in rows})) or "-"
        evidence = ",".join(sorted({row.evidence for row in rows})) or "-"
        found.append((rows_path.parent.name, len(rows), tiers, evidence))
    return found


def _bench_list(args: argparse.Namespace) -> int:
    from architect.bench import SUITES, BenchError, render, suite_digest
    from architect.bench.providers import evidence_label, provider_for
    from architect.bench.runner import BenchConfig, bound
    from architect.bench.suite import tiers_for

    digest = suite_digest(args.suite)
    tasks = SUITES[args.suite]
    profile = _bench_profile(args.profile)
    bounds: list[tuple[str, str, float, int, str]] = []
    models = tuple(profile.models) if profile is not None else ("fake",)
    # The quick tier where the suite has one (v1); otherwise the suite's first tier.
    tier = "quick" if "quick" in tiers_for(args.suite) else next(iter(tiers_for(args.suite)))
    for model in models:
        config = BenchConfig(
            suite=args.suite,
            tier=tier,
            provider="profile" if profile is not None else "fake",
            models=(model,),
            out=args.out,
        )
        try:
            limits = bound(config, profile=profile)
            evidence = evidence_label(provider_for(config, profile, model))
        except BenchError:
            # A read-only listing never fails on a credential it does not need;
            # the model is left out, as the session's ``bench list`` does.
            continue
        bounds.append((tier, model, limits.total_usd, limits.calls, evidence))
    recorded = _recorded(args.out)
    if args.json:
        _emit(
            {
                "suite": args.suite,
                "suite_digest": digest,
                "tasks": [
                    {
                        "id": task.id,
                        "kind": task.kind,
                        "sizes": {tier: size for tier, size in task.sizes.items()},
                        "floor_limit": task.floor_limit,
                        "checker": task.checker,
                        "preset": task.preset,
                        "requires": [str(item) for item in sorted(task.requires)],
                        "floor_origin": task.floor_origin,
                    }
                    for task in tasks
                ],
                "bounds": [
                    {"tier": t, "model": m, "bound_usd": u, "calls": c, "evidence": e}
                    for t, m, u, c, e in bounds
                ],
                "recorded": [
                    {"bench_id": b, "rows": n, "tiers": t, "evidence": e}
                    for b, n, t, e in recorded
                ],
            }
        )
    else:
        print(
            render.list_table(
                tasks, version=args.suite, digest=digest, bounds=bounds, recorded=recorded
            ),
            end="",
        )
    return 0


def _bench_pages(args: argparse.Namespace) -> int:
    from architect.bench import BenchError, generate_pages, write_dataset

    if not 1 <= args.count <= 1000:
        raise CLIError("invalid_arguments")
    try:
        pages = generate_pages(args.seed, args.count, args.style, dpi=args.dpi)
    except ImportError:
        raise BenchError("bench_extra_missing") from None
    except ValueError:
        raise CLIError("invalid_arguments") from None
    try:
        files = write_dataset(
            args.out,
            pages,
            args.kind,
            dataset_id=f"pages-{args.style}-{args.seed}",
            seed=args.seed,
            dpi=args.dpi,
            png=args.png,
        )
    except OSError:
        raise BenchError("bench_out_unwritable") from None
    _emit(
        {
            "cases": str(files.cases),
            "manifest": str(files.manifest),
            "pages": len(pages),
            "pngs": len(files.pngs),
            "synthetic": True,
        }
    )
    return 0


async def _bench_probe(args: argparse.Namespace) -> int:
    from architect.bench import BenchError, generate_pages, render
    from architect.bench.providers import build_factory, probe_models, resolve_models
    from architect.bench.runner import BenchConfig
    from architect.models.images import ImageInput
    from architect.models.pricing import load_prices

    profile = _bench_profile(args.profile)
    if profile is None:
        raise BenchError("bench_profile_required")
    config = BenchConfig(
        tier="quick", provider="profile", profile=args.profile, models=_names(args.models)
    )
    models = resolve_models(config, profile)
    try:
        # A ``clean`` page is stdlib-only, so a text-only model probes without the
        # bench extra; should rendering ever need Pillow it is a bench code, as in pages.
        page = ImageInput.from_bytes(generate_pages(7, 1, "clean", key_page=False)[0].image)
    except ImportError:
        raise BenchError("bench_extra_missing") from None
    rows = await probe_models(
        build_factory(config, profile, None),
        load_prices(),
        models,
        page=page,
        providers={model: profile.models[model].provider for model in models},
    )
    if args.json:
        _emit([row.model_dump(mode="json") for row in rows])
    else:
        print(render.probe_table(rows), end="")
    return 0


def _bench(args: argparse.Namespace) -> int:
    """Every bench failure is one fixed code on stderr and exit 1 (Section 10)."""
    from architect.bench import BenchError

    try:
        command = args.bench_command
        if command == "run":
            return _bench_run(args)
        if command == "compare":
            return _bench_compare(args)
        if command == "report":
            return _bench_report(args)
        if command == "audit":
            return _bench_audit(args)
        if command == "list":
            return _bench_list(args)
        if command == "pages":
            return _bench_pages(args)
        if command == "roles":
            return _bench_roles(args)
        return asyncio.run(_bench_probe(args))
    except BenchError as exc:
        return _error(exc.code)
    except CLIError as exc:
        if str(exc) == "invalid_arguments":
            _error("invalid_arguments")
            return 2
        return _error(str(exc))
    except KeyboardInterrupt:
        _error("interrupted")
        return 130


def _forge_report(args: argparse.Namespace) -> int:
    """``architect forge report <artifact-dir> [--json]`` (content-free ``forge.json``)."""
    from architect.session.forge import forge_report_table, read_forge_record

    record = read_forge_record(args.artifact)
    if record is None:
        return _error("forge_report_missing")
    if args.json:
        _emit(record)
    else:
        print(forge_report_table(record), end="")
    return 0


def _forge_pack(args: argparse.Namespace) -> int:
    """``architect forge pack [--profile P] [--spec S] [--source F]... [--term T]... [--json]``.

    The Forge pack's sha256 (what a pre-registration pins), the sha256 of each part, and
    the task-text check: no ``--term`` as a whole word, and no run of words copied from
    a ``--source`` file or from the spec's task and schema prose (the spec's
    identifier-like field names are terms too). Exit 0 when the pack is clean, else 1.
    The settings are the profile's ``compile.forge``, or the defaults without one, with
    the same ``--forge-usd``, ``--forge-minutes``, ``--forge-model`` and ``--forge-mode``
    overrides ``compile`` takes.
    """
    from architect.compiler.forge_pack import (
        PACK_FORMAT,
        RUN_WORDS,
        pack_hash,
        pack_manifest,
        pack_parts,
        pack_settings,
        spec_sources,
        spec_terms,
        task_text_findings,
    )

    base = (
        forge_settings_of(load_profile(args.profile).compile)
        if args.profile is not None
        else ForgeSettings()
    )
    settings = forge_settings(base, _forge_overrides(args))
    terms = [term for term in args.term if term.strip()]
    sources: list[str] = []
    if args.spec is not None:
        spec = _read_spec(args.spec)
        terms += spec_terms(spec)
        sources += spec_sources(spec)
    for path in args.source:
        try:
            sources.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            raise CLIError("invalid_arguments") from None
    terms = sorted(set(terms), key=str.casefold)
    findings = task_text_findings(pack_parts(settings), terms=terms, sources=sources)
    digest = pack_hash(settings)
    manifest = pack_manifest(settings)
    if args.json:
        _emit(
            {
                "pack_sha256": digest,
                "format": PACK_FORMAT,
                "parts": cast(JsonValue, manifest),
                "settings": pack_settings(settings),
                "check": {
                    "terms": cast(JsonValue, terms),
                    "sources": len(sources),
                    "run_words": RUN_WORDS,
                    "clean": not findings,
                    "findings": [
                        {"part": item.part, "kind": item.kind, "text": item.text}
                        for item in findings
                    ],
                },
            }
        )
    else:
        scope = (
            f"{len(terms)} term{'' if len(terms) == 1 else 's'}, "
            f"{len(sources)} source{'' if len(sources) == 1 else 's'}, "
            f"runs of {RUN_WORDS} words"
        )
        lines = [f"forge pack {digest}", f"format {PACK_FORMAT}"]
        lines += [f"  {name:<30} {value[:16]}" for name, value in sorted(manifest.items())]
        if findings:
            lines.append(f"task text: {len(findings)} found ({scope})")
            lines += [f"  {item.part}  {item.kind}  {item.text}" for item in findings]
        else:
            lines.append(f"task text: none found ({scope})")
        print("\n".join(lines))
    return 1 if findings else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Return an exit code; all opened runtime stores close on every exit."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["serve"]:
        # Phase 13: `architect serve` is the HTTP service (python -m architect.server).
        from architect.server import main as serve_main

        return serve_main(args_list[1:])
    if args_list and args_list[0] == "session":
        from architect.session import main as session_main

        return session_main(args_list[1:])
    if not args_list or (args_list[0].startswith("-") and args_list[0] not in ("-h", "--help")):
        # Phase 12: no subcommand opens the conversational CLI (and takes its flags).
        from architect.agent.repl import main as agent_main

        return agent_main(args_list)
    try:
        try:
            args = _parser().parse_args(args_list)
        except CLIError:
            if args_list[0] == "bench":
                _error("invalid_arguments")
                return 2  # Section 10: argument errors of ``bench`` exit 2.
            raise
        if args.command == "bench":
            return _bench(args)
        if args.command == "forge" and args.forge_command == "report":
            return _forge_report(args)
        if args.command == "forge" and args.forge_command == "pack":
            return _forge_pack(args)
        if args.command == "export" and args.profile is None:
            return asyncio.run(_export_verify(args, None))
        if args.command == "verify":
            return _verify(args)
        if args.command == "keys":
            return _keys(args)
        if args.command == "retake":
            return _retake(args)
        if args.command == "undo":
            return _undo(args)
        if args.command == "judge":
            return _judge(args)
        if args.command in ("status", "registry"):
            registry = Registry(args.workspace.resolve() / "registry")
            if args.command == "status" and args.name is not None:
                _emit(registry.status(args.name).model_dump(mode="json"))
            else:
                _emit([entry.model_dump(mode="json") for entry in registry.list()])
            return 0
        if args.command == "forge" and args.forge_command == "integrate":
            # It signs with this folder's .architect keys unless a variable names others,
            # then re-checks the package it placed with configured keys only (W7). Name
            # those keys, as a session does, so the re-check uses the keys that signed.
            # Verification commands (verify, export verify) stay explicit.
            from architect.trust import SIGNING_KEY_ENV, signing_key_path

            os.environ.setdefault(SIGNING_KEY_ENV, os.path.abspath(signing_key_path()))
        profile = _with_forge(load_profile(args.profile), args)
        audit = _forge_audit(args)
        services = build_services(profile, profile.workspace)
        try:
            with audit_scope(audit):
                return asyncio.run(_dispatch(args, services))
        finally:
            services.close()
    except SystemExit as exc:
        return int(exc.code or 0)  # argparse --help remains a normal CLI operation.
    except (CLIError, ProfileError) as exc:
        return _error(str(exc))
    except OrchestrationError as exc:
        code = str(exc)
        if code not in {"builder_floor_required", "approved_feature_must_be_installed"}:
            code = "orchestration_failed"
        return _error(code)
    except _fixed_errors() as exc:
        return _error(_fixed_code(exc))
    except RegistryError:
        return _error("registry_rejected")
    except ArtifactExpiredError:
        return _error("artifact_expired")
    except UnlicensedArtifactError:
        return _error("unlicensed_artifact")
    except BudgetExceeded:
        return _error("budget_exceeded")
    except ImportError:
        return _error("backend_unavailable")
    except KeyboardInterrupt:
        _error("interrupted")
        return 130
    except Exception:
        # Provider, fixture and filesystem exceptions can contain user secrets.
        return _error("command_failed")


def _fixed_errors() -> tuple[type[Exception], ...]:
    """Phase 10 errors whose ``code`` is one fixed Section 13.4 code."""
    from architect.forge_modes.errors import ForgeModeError
    from architect.routing.router import RouteError
    from architect.session.exports import ReverifyError

    return (ForgeModeError, RouteError, ReverifyError)


def _fixed_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.isascii() else "command_failed"
