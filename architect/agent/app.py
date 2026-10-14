"""Boot and wiring for the Architect CLI (Phase 12, Sections 3 and 9).

``boot`` finds (or, on the first run, writes) a profile, builds metered Services and a
``Conductor`` bound to them, and returns an ``App``. The first run reuses the session's
zero-config setup: the logged-in ``claude``/``codex`` CLIs are detected, the user picks a
model (Enter takes the recommended one), one tiny login probe runs unless ``--no-probe``,
and the profile is written to ``<workspace>/session-profile.json``.

Build mode, Forge and the coder are separate packages bound lazily: when one is missing
its tool stays unbound and reports itself unavailable to the model.

Phase 13: the first run is the providers package's sign-in (``first_run``: detected
providers, a recommendation, one keypress); the CLI detection above is the fallback when
that package is missing. Enabled MCP servers' tools are bridged into the conductor.
``config.toml`` (workspace, then project) supplies defaults that command-line flags
override. The coder is wrapped so each run is checkpointed for ``/undo``; the conductor's
context limit follows its model's context window.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from architect.models import ModelAdapter
from architect.profiles import (
    Profile,
    ProfileError,
    Services,
    build_services,
    in_workspace,
    is_nebius,
    load_profile,
)
from architect.runtime import Runtime
from architect.session.bootstrap import (
    SESSION_PROFILE,
    CliCandidate,
    ProbeResult,
    choose_inference,
    detect_clis,
    discover_profile,
    model_rows,
    probe_login,
    profile_from_payload,
    profile_payload,
    write_profile,
)
from architect.session.codes import SessionError
from architect.session.observe import Meter, ObservedAdapter, observe_services

from .checkpoints import CheckpointStore, checkpointed
from .conductor import Conductor, ConductorSettings, project_instructions
from .config import CHARS_PER_TOKEN, Config, context_window, load_config
from .events import UI, Notice
from .sessions import SessionStore
from .tools import Permission

__all__ = [
    "SIGN_IN_FIRST",
    "SIGN_IN_FIRST_TEXT",
    "App",
    "AppOptions",
    "BootError",
    "agent_workspace",
    "boot",
    "bootstrap_profile",
    "build_app",
    "conductor_model",
    "attach_mcp",
    "close_mcp",
    "first_run",
]

WORKSPACE_ENV = "ARCHITECT_WORKSPACE"
SIGNING_KEY_ENV = "ARCHITECT_SIGNING_KEY_FILE"
# A profile on Nebius Token Factory (the receipt example's) with no Nebius key or judge
# code stored yet: the CLI and the app say to sign in first.
SIGN_IN_FIRST = "sign_in_first"
SIGN_IN_FIRST_TEXT = (
    "This folder runs on Nebius Token Factory and no key is stored yet. Sign in first: run "
    "'architect judge <code>' with your judge code, or set NEBIUS_API_KEY to your Nebius "
    "key, then start Architect again."
)
NO_PROVIDER = (
    "No profile and no logged-in CLI found. Log in with 'claude' or 'codex' once (or write "
    "architect.json, see docs/usage.md), then run 'architect' again."
)
_CLI_NAMES = {"claude-code": "claude", "codex": "codex"}


class BootError(Exception):
    """Setup could not finish; ``code`` is a fixed word, never provider text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class AppOptions:
    cwd: Path = field(default_factory=Path.cwd)
    workspace: Path | None = None
    profile: Path | None = None
    model: str | None = None
    permission: Permission | None = None
    probe: bool = True
    session_usd: float | None = None
    session_minutes: float | None = None
    config: Config | None = None  # loaded by boot when not given


def agent_workspace(cwd: Path, explicit: Path | None) -> Path:
    """``--workspace``, else ``ARCHITECT_WORKSPACE``, else ``~/.architect``."""
    if explicit is not None:
        return (cwd / explicit).resolve()
    configured = os.environ.get(WORKSPACE_ENV)
    if configured:
        return (cwd / Path(configured)).resolve()
    return Path.home() / ".architect"


def add_detected_model(
    profile_path: Path,
    name: str,
    detect: Callable[[], tuple[CliCandidate, ...]] = detect_clis,
) -> bool:
    """Register a model a logged-in CLI offers into the JSON profile; True when available.

    The first-run profile lists every detected model, but older profiles list only the one
    chosen then; ``--model`` and ``/model`` add a detected model on demand.
    """
    row = next((r for r in model_rows(detect()) if r.model == name), None)
    if row is None or profile_path.suffix.lower() != ".json":
        return False
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("models", {}), dict):
        return False
    models = payload.setdefault("models", {})
    if name not in models:
        models[name] = {"provider": row.provider, "executable": row.executable}
        try:
            write_profile(payload, profile_path)
        except (OSError, SessionError):
            return False
    return True


def conductor_model(profile: Profile, requested: str | None = None) -> str:
    if requested:
        if requested not in profile.models:
            raise BootError("model_not_configured")
        return requested
    ladder = profile.compile.model_ladder
    if ladder:
        return ladder[0]
    if not profile.models:
        raise BootError("model_not_configured")
    return next(iter(profile.models))


# -- first run ------------------------------------------------------------------------


async def bootstrap_profile(
    ui: UI,
    workspace: Path,
    *,
    probe: bool = True,
    detect: Callable[[], tuple[CliCandidate, ...]] = detect_clis,
    prober: Callable[[ModelAdapter, Runtime], Awaitable[ProbeResult]] = probe_login,
    services_factory: Callable[[Profile, Path], Services] = build_services,
) -> Path:
    """Write ``<workspace>/session-profile.json`` from the logged-in CLIs; return its path."""
    candidates = detect()
    found = {candidate.provider: candidate for candidate in candidates}
    seen = ", ".join(
        f"{name} found" if provider in found else f"{name} not found"
        for provider, name in _CLI_NAMES.items()
    )
    ui.emit(Notice(f"First run: looking for logged-in CLIs on PATH: {seen}."))
    rows = model_rows(candidates)
    if not rows:
        ui.emit(Notice(NO_PROVIDER, "error"))
        raise BootError("no_provider")
    listing = "\n".join(
        f"  {row.index}. {row.model} via {_CLI_NAMES.get(row.provider, row.provider)}"
        f" (about ${row.min_call_cost:.4f} per small call)"
        + ("  <- recommended" if row is rows[0] else "")
        for row in rows
    )
    ui.emit(Notice("Models Architect can use:\n" + listing))
    recommended = rows[0]
    row = None
    for _ in range(3):
        answer = await ui.ask(
            "Model for Architect (number or name; Enter = recommended)",
            default=recommended.model,
            choices=[r.model for r in rows],
        )
        if answer is None:
            raise BootError("setup_cancelled")
        text = answer.strip() or recommended.model
        row = next((r for r in rows if text in (str(r.index), r.model)), None)
        if row is not None:
            break
        ui.emit(Notice(f"'{text}' is not in the list.", "warn"))
    if row is None:
        raise BootError("model_not_configured")
    inference = choose_inference(row.model, candidates, rows)
    try:
        payload = profile_payload(row, inference, candidates)
    except SessionError as exc:
        raise BootError(str(exc)) from None
    # Every model the logged-in CLIs offer, so /model can switch and build mode's
    # "cheapest that passes" has a ladder to climb; the chosen one stays the default.
    listed = payload["models"]
    assert isinstance(listed, dict)
    for other in rows:
        listed.setdefault(other.model, {"provider": other.provider, "executable": other.executable})
    path = workspace / SESSION_PROFILE
    if probe and row.probe_fits:
        cli = _CLI_NAMES.get(row.provider, row.provider)
        ui.emit(Notice(f"Checking the {cli} login with one tiny metered call..."))
        try:
            profile = profile_from_payload(payload, workspace)
            services = services_factory(profile, profile.workspace)
        except (SessionError, ProfileError, OSError):
            raise BootError("invalid_profile") from None
        try:
            result = await prober(services.adapter_factory(row.model), services.runtime)
        finally:
            services.close()
        if result.status == "not_logged_in":
            ui.emit(Notice(f"{cli} is not logged in: run '{cli}' once and sign in.", "error"))
            raise BootError("provider_not_logged_in")
        if result.status in ("unavailable", "error"):
            ui.emit(Notice(f"{cli} did not answer the login probe.", "error"))
            raise BootError("provider_unavailable")
        ui.emit(Notice(f"{cli}: logged in."))
    try:
        write_profile(payload, path)
    except SessionError as exc:
        raise BootError(str(exc)) from None
    ui.emit(Notice(f"Profile written: {path} (edit it to add models, floors or budgets)."))
    return path


# -- the app --------------------------------------------------------------------------


@dataclass
class App:
    conductor: Conductor
    meter: Meter
    workspace: Path
    profile_path: Path
    store: SessionStore
    unavailable: tuple[str, ...] = ()
    config: Config = field(default_factory=Config)
    checkpoints: CheckpointStore | None = None
    _unpriced: dict[tuple[str, int], bool] = field(default_factory=dict, repr=False)

    @property
    def services(self) -> Services:
        services = self.conductor.services
        assert isinstance(services, Services)
        return services

    def save_session(self) -> None:
        conductor = self.conductor
        title = conductor.extras.get("session_title")
        try:
            self.store.save(conductor.session_id, conductor.transcript, conductor.cwd,
                            title=title if isinstance(title, str) and title else None)
        except (OSError, ValueError):
            conductor.ui.emit(Notice("Could not save this conversation.", "warn"))

    def unpriced(self) -> bool:
        """True when the conductor's model has no dollar price ("cost unknown")."""
        key = (self.conductor.adapter.model, id(self.conductor.services))
        cached = self._unpriced.get(key)
        if cached is not None:
            return cached
        try:
            from architect.profiles import model_unpriced

            value = model_unpriced(self.services.profile, self.conductor.adapter.model)
        except Exception:
            value = False
        self._unpriced = {key: value}
        return value

    # -- context fill (Phase 13, Section 5 item 2) ----------------------------------

    def context_tokens(self) -> int:
        """The conductor model's context window in tokens."""
        return context_window(self.conductor.adapter.model)

    def context_used_chars(self) -> int:
        conductor = self.conductor
        return len(conductor.system_prompt()) + len(conductor.transcript.render())

    def context_percent(self) -> float:
        total = self.context_tokens() * CHARS_PER_TOKEN
        return min(100.0, 100.0 * self.context_used_chars() / max(1, total))

    def sync_context_limit(self) -> None:
        """Size the conductor's transcript limit to the model's window (minus the prefix)."""
        conductor = self.conductor
        total = self.context_tokens() * CHARS_PER_TOKEN
        reserve = conductor.settings.max_output_tokens * CHARS_PER_TOKEN
        limit = int(total * 0.95) - len(conductor.system_prompt()) - reserve
        conductor.settings.context_chars = max(20_000, limit)

    def close(self) -> None:
        stop = _optional("architect.agent.commands_forge", "stop_servers")
        if callable(stop):
            try:
                stop()  # /serve services started by this CLI end with it
            except Exception:
                pass
        self.services.close()


def _optional(module: str, name: str) -> object | None:
    """``module.name`` when that package is present and importable, else None."""
    try:
        loaded = importlib.import_module(module)
    except Exception:  # a missing or broken optional package leaves the tool unbound
        return None
    value = getattr(loaded, name, None)
    return value if callable(value) else None


def _bind_optional(extras: dict[str, object]) -> tuple[str, ...]:
    missing: list[str] = []
    for key, module, name in (
        ("make", "architect.agent.build", "make"),
        ("forge", "architect.agent.build", "forge_target"),
        ("coder", "architect.agent.coder", "run_coder"),
    ):
        handler = _optional(module, name)
        if handler is None:
            missing.append(key)
        elif key == "coder":
            extras[key] = checkpointed(handler)  # type: ignore[arg-type]
        else:
            extras[key] = handler
    return tuple(missing)


def _nebius_key_missing(read: Callable[[], Profile]) -> bool:
    """Whether the profile ``read`` gives has a model on the Nebius credential and no
    Nebius key or judge code is set for it (in the environment or its workspace)."""
    from architect.providers import credentials

    try:
        profile = read()
    except ProfileError:
        return False
    if not any(is_nebius(config) for config in profile.models.values()):
        return False
    return credentials.resolve("nebius", profile.resolve(profile.workspace)) is None


def build_app(
    profile_path: Path,
    options: AppOptions,
    ui: UI,
    *,
    adapter: ModelAdapter | None = None,
    services_factory: Callable[[Profile, Path], Services] = build_services,
) -> App:
    """Services, meter and conductor for one profile. ``adapter`` replaces the conductor's.

    A profile that names ``~/.architect`` (a project's ``architect.json``, such as the
    receipt example's) works in this app's workspace: ``--workspace``,
    ``ARCHITECT_WORKSPACE`` or the desktop app's setting moves it there (``in_workspace``).
    """
    config = options.config if options.config is not None else Config()
    requested = options.model
    shared = agent_workspace(options.cwd, options.workspace)

    def read() -> Profile:
        return in_workspace(load_profile(profile_path), shared)

    try:
        profile = read()
        if requested and requested not in profile.models:
            if add_detected_model(profile_path, requested):
                profile = read()
        if requested is None:
            configured = config.text("model")
            if configured and configured not in profile.models:
                if add_detected_model(profile_path, configured):
                    profile = read()
            if configured and configured in profile.models:
                requested = configured
            elif configured:
                ui.emit(Notice(f"config.toml names model {configured}, which is not in the "
                               "profile; using the default. /model lists the choices.", "warn"))
        workspace = profile.resolve(profile.workspace)
        os.environ.setdefault(SIGNING_KEY_ENV, str(workspace / "signing.key"))
        meter = Meter()
        services = observe_services(services_factory(profile, profile.workspace), meter)
        model = conductor_model(profile, requested)
        chosen = ObservedAdapter(adapter, meter) if adapter is not None else None
        conductor_adapter = chosen or services.adapter_factory(model)
    except BootError:
        raise
    except ProfileError as exc:
        code = str(exc) or "invalid_profile"
        if code == "missing_api_key" and _nebius_key_missing(read):
            code = SIGN_IN_FIRST
        raise BootError(code) from None
    except Exception:
        raise BootError("invalid_profile") from None
    settings = ConductorSettings()
    permission = options.permission or config.text("permission")
    if permission in ("ask", "auto-edit", "full-auto", "plan"):
        settings.permission = permission  # type: ignore[assignment]
    if config.get("session_budget_usd") is not None and "session_budget_usd" in config.values:
        settings.session_usd = config.number("session_budget_usd")
    if config.get("session_minutes") is not None and "session_minutes" in config.values:
        settings.session_s = config.number("session_minutes") * 60
    if options.session_usd is not None:
        settings.session_usd = options.session_usd
    if options.session_minutes is not None:
        settings.session_s = options.session_minutes * 60
    checkpoints = CheckpointStore(workspace / "checkpoints")
    extras: dict[str, object] = {
        "meter": meter,
        "profile_path": profile_path,
        "build_overrides": {},
        "checkpoints": checkpoints,
        "web_fetch_allow_private": config.flag("web_fetch_allow_private"),
        "workspace": workspace,  # the read-only tools never show its secret files
    }
    unavailable = _bind_optional(extras)
    conductor = Conductor(services, conductor_adapter, options.cwd, ui, settings, extras=extras)
    conductor.instructions = project_instructions(options.cwd, workspace)
    for problem in config.errors:
        ui.emit(Notice(f"config: {problem}", "warn"))

    def reload() -> Services:
        """Rebuild Services from the profile file; the old ones close after the swap."""
        fresh_profile = read()
        fresh = observe_services(services_factory(fresh_profile, fresh_profile.workspace), meter)
        old = conductor.services
        conductor.services = fresh
        conductor.context.services = fresh
        if adapter is None:
            conductor.adapter = fresh.adapter_factory(conductor.adapter.model)
        if isinstance(old, Services):
            old.close()
        return fresh

    extras["reload"] = reload
    store = SessionStore(workspace / "sessions")
    app = App(conductor, meter, workspace, profile_path, store, unavailable, config, checkpoints)
    app.sync_context_limit()
    return app


async def boot(
    options: AppOptions,
    ui: UI,
    *,
    adapter: ModelAdapter | None = None,
    services_factory: Callable[[Profile, Path], Services] = build_services,
    detect: Callable[[], tuple[CliCandidate, ...]] = detect_clis,
    prober: Callable[[ModelAdapter, Runtime], Awaitable[ProbeResult]] = probe_login,
) -> App:
    """Find or set up the profile, build the app, attach MCP tools. Raises ``BootError``."""
    workspace = agent_workspace(options.cwd, options.workspace)
    if options.config is None:
        options.config = load_config(workspace, options.cwd)
    path = discover_profile(options.cwd, workspace, options.profile)
    if path is None:
        path = await first_run(ui, workspace)
        if path is not None:  # it may name the conversation model, after anyone read config
            options.config = load_config(workspace, options.cwd)
    if path is None:
        path = await bootstrap_profile(
            ui, workspace, probe=options.probe, detect=detect, prober=prober,
            services_factory=services_factory,
        )
    elif not path.is_file():
        raise BootError("invalid_profile")
    app = build_app(path, options, ui, adapter=adapter, services_factory=services_factory)
    await attach_mcp(app, options.cwd)
    return app


async def first_run(ui: UI, workspace: Path) -> Path | None:
    """The providers package's first-run picker (detected providers, a recommendation, one
    keypress); None only when that package is not installed."""
    try:
        from architect.providers.onboarding import ProviderError
        from architect.providers.signin import first_run as providers_first_run
    except ImportError:
        return None
    try:
        return await providers_first_run(ui, workspace)
    except ProviderError as exc:
        raise BootError(exc.code) from None


async def attach_mcp(app: App, cwd: Path) -> None:
    """Bridge the enabled MCP servers' tools into the conductor (never raises)."""
    try:
        from architect.mcp import manager_for, mcp_tools
    except ImportError:
        return
    try:
        app.conductor.extras["mcp"] = manager_for(app.workspace, cwd)
        bridged = await mcp_tools(app.workspace, cwd)
    except Exception:
        return
    if bridged:
        known = {tool.name for tool in app.conductor.tools}
        fresh = tuple(tool for tool in bridged if tool.name not in known)
        app.conductor.set_tools((*app.conductor.tools, *fresh))
        app.sync_context_limit()


async def close_mcp(app: App, cwd: Path) -> None:
    """Stop this app's MCP servers (end of the REPL or of ``-p``)."""
    try:
        from architect.mcp import close_mcp as close_servers
    except ImportError:
        return
    with contextlib.suppress(Exception):
        await close_servers(app.workspace, cwd)
