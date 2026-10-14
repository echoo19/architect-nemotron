"""Trusted, local JSON/TOML profiles and service lifetime management.

Paths are relative to the profile file; a path that starts with ``~`` is in the home
folder. ``models`` maps model IDs to adapter
settings; optional ``inference``, ``decomposition`` and ``rewrite`` settings also
require ``model``. Fake fixtures are ModelResponse JSON arrays. ``floors`` maps
dataset IDs to {kind, path, smoke?, checker?}: task/judge files are EvaluationCase arrays;
human edit files use EditLogRow JSONL. ``calibrations`` maps calibration dataset
IDs to CalibrationRow JSON arrays. Game environments need an explicit integration
and fail closed here; a score file is never a replacement for an environment.
``checker`` picks a held-out task comparison from the closed table in
``architect.floors.checkers`` (default ``equality``); no profile supplies code.

``workspace`` defaults to .architect. ``compile``, ``orchestrator`` and ``pi``
are their typed component settings. Pi shell permission is a trusted host-code
grant, not OS isolation. Never accept a profile from model output.

``builder_floor`` names the trusted coding floor required by ``create``;
``builder_model`` defaults to the first configured model. Evaluation fixture
task repo paths must name disposable checkouts, never the user delivery repo
or production data. These paths are trusted setup, not model-selected grants.
Pi file tools deny the profile, fixture files and workspace control-plane
subdirectories. Explicit ``pi.denied_paths`` also resolve against the profile
file. The whole workspace is not denied: it may hold disposable evaluation repos.

``forge_agent`` (Phase 9) is the optional adapter that drives a Forge session,
mirroring ``rewrite``. ``compile.forge.agent_model`` names the agent by profile
key (a ``models`` key or the ``forge_agent`` model); an unknown name is
``forge_model_unknown``. ``resolve_forge_agent`` is the one resolution rule, and
``Services.forge_adapter`` carries its result for the Compiler wiring.

``routing`` (Phase 10) is the task router's ``RoutePolicy`` and ``roles`` the role
router's ``RoleRouting``. With the default ``roles.policy = "pinned"`` every role
adapter is exactly today's binding; ``evidence`` switches a role only on
non-inferiority evidence. A role candidate naming a model outside ``models`` is
``route_model_unknown``; a parameter outside its role's closed set is
``route_param_invalid``. Routing never licenses: Stage 4 stays the only authority.

Phase 13 (providers): ``api_key_ref`` names a stored credential (a provider id such
as ``openrouter``) resolved by ``architect.providers.credentials``: that provider's
environment variables first, else ``<workspace>/credentials.json``. Local servers
and custom URLs may run keyless (an empty key, so no ``OPENAI_API_KEY`` leaks to
them). Every OpenAI-compatible provider is ``provider: "openai"`` with its
``base_url``; an entry without one uses the URL stored with its credential (a judge
code's relay), else the provider's catalog URL. The ``nebius`` credential selects the
adapter's Nebius Token Factory dialect (``models/http.py``), where ``think`` switches
NVIDIA Nemotron's reasoning per
role (off when unset). ``timeout_s``, ``temperature`` and ``top_p`` tune any
OpenAI-compatible entry. ``model_unpriced`` says whether a model has a dollar price
(``models/pricing.py`` documents the unpriced policy); build mode refuses to
compile on an unpriced model. The workspace's ``credentials.json`` and
``model-prices.json`` are control-plane files Pi never reaches.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from architect.backends.pi import PiBackend, PiConfig
from architect.compiler import CompileConfig, Compiler
from architect.compiler.types import ForgeSettings
from architect.floors import (
    CalibrationRow,
    EvaluationCase,
    FloorEvaluator,
    HeldOutTasks,
    HumanEditRate,
    LLMJudge,
)
from architect.floors.checkers import CHECKERS, CheckerName
from architect.library import HarnessLibrary
from architect.models import (
    AdapterError,
    AnthropicAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    FakeAdapter,
    ModelAdapter,
    OllamaAdapter,
    OpenAICompatibleAdapter,
)
from architect.models.http import OLLAMA_DEFAULT_NUM_CTX
from architect.models.pricing import is_unpriced
from architect.models.types import MODEL_ID_PATTERN
from architect.orchestrator import Orchestrator, OrchestratorConfig
from architect.registry import Registry
from architect.routing.evidence import EvidenceIndex
from architect.routing.labels import Provider
from architect.routing.log import RouteLog
from architect.routing.roles import (
    ROLE_NAMES,
    RoleAdapters,
    RoleName,
    RoleRouter,
    RoleRouting,
    RoleRoutingError,
    RoleVariant,
    check_roles,
)
from architect.routing.router import RoutePolicy, Router
from architect.runtime import Runtime
from architect.spec import FloorDefinition, FloorKind
from architect.telemetry import TelemetryStore
from architect.trust import ED25519_KEY_NAME, TRUST_FILE_NAME


class ProfileError(ValueError):
    """Invalid trusted configuration; messages never include file contents or keys."""


Nonempty = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AdapterConfig(_Settings):
    provider: Literal["fake", "anthropic", "openai", "ollama", "claude-code", "codex"]
    fixture: Path | None = None
    base_url: Nonempty | None = None
    # CLI providers only: command name or path of the locally logged-in CLI.
    executable: Nonempty | None = None
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = (
        None
    )
    # Phase 13: a credential name (a provider id) resolved through
    # ``architect.providers.credentials``; the key itself never enters the profile.
    api_key_ref: (
        Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")] | None
    ) = None
    # HTTP providers: a price table for models the bundled table does not list
    # (local Ollama models are $0 but still need an entry for token accounting).
    prices: Path | None = None
    # Ollama only: fixed context size.
    num_ctx: Annotated[int, Field(ge=512, le=1_048_576, strict=True)] | None = None
    # The thinking switch: Ollama's ``think``, and Nebius Token Factory's
    # ``chat_template_kwargs.enable_thinking`` (off when unset).
    think: bool | None = None
    # Ollama and OpenAI-compatible adapters: per-call timeout.
    timeout_s: Annotated[float, Field(gt=0, le=3600)] | None = None
    # OpenAI-compatible adapters: sampling that replaces each request's own.
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    top_p: Annotated[float, Field(gt=0, le=1)] | None = None
    # OpenAI-compatible adapters only (ValueError otherwise). The provider's model id when
    # the profile key is an alias: sent as "model", priced, and checked against the reply.
    api_model: (
        Annotated[str, StringConstraints(pattern=MODEL_ID_PATTERN, max_length=128)] | None
    ) = None
    # OpenAI-compatible adapters only: a ceiling on every call's max_tokens and reservation.
    max_tokens: Annotated[int, Field(ge=16, le=1_048_576, strict=True)] | None = None

    @model_validator(mode="after")
    def provider_fields(self) -> Self:
        keyed = self.api_key_env is not None or self.api_key_ref is not None
        if self.provider == "fake" and (self.base_url is not None or keyed):
            raise ValueError("Fake adapters cannot use network settings")
        if self.api_key_env is not None and self.api_key_ref is not None:
            raise ValueError("Use api_key_env or api_key_ref, not both")
        if self.provider != "fake" and self.fixture is not None:
            raise ValueError("Only fake adapters accept response fixtures")
        cli = self.provider in ("claude-code", "codex")
        if cli and self.base_url is not None:
            raise ValueError("CLI adapters cannot use a base URL")
        if self.provider == "codex" and keyed:
            raise ValueError("Codex adapters use the CLI login, not an API key")
        if not cli and self.executable is not None:
            raise ValueError("Only CLI adapters accept an executable")
        if self.prices is not None and (cli or self.provider == "fake"):
            raise ValueError("Only HTTP adapters accept a price table")
        if self.provider != "ollama" and self.num_ctx is not None:
            raise ValueError("num_ctx applies to Ollama adapters only")
        if self.think is not None and self.provider != "ollama" and not is_nebius(self):
            raise ValueError("think applies to Ollama and Nebius adapters only")
        if self.timeout_s is not None and self.provider not in ("ollama", "openai"):
            raise ValueError("timeout_s applies to Ollama and OpenAI-compatible adapters only")
        if self.provider != "openai" and (self.temperature, self.top_p) != (None, None):
            raise ValueError("temperature and top_p apply to OpenAI-compatible adapters only")
        if self.provider != "openai" and (self.api_model, self.max_tokens) != (None, None):
            raise ValueError("api_model and max_tokens apply to OpenAI-compatible adapters only")
        return self


def is_nebius(config: AdapterConfig) -> bool:
    """Whether ``config`` talks to Nebius Token Factory: an OpenAI-compatible adapter on
    the ``nebius`` credential, so dedicated endpoints at other URLs count too."""
    return config.provider == "openai" and config.api_key_ref == "nebius"


class NamedAdapterConfig(AdapterConfig):
    model: Nonempty


class FloorConfig(_Settings):
    kind: FloorKind
    path: Path
    smoke: Path | None = None
    # A name from the closed trusted table in ``architect.floors.checkers``;
    # only held-out task floors consult it. "equality" keeps exact comparison.
    checker: CheckerName = "equality"
    # Phase 14: a sidecar dataset manifest (``architect.floors.provenance``), relative to
    # the profile file like ``path``. It labels the floor's origin and never refuses it.
    manifest: Path | None = None
    # Retake: extra exam cases a retake of this exam draws on, relative to the profile
    # file like ``path``. No build or practice run reads them.
    retake_cases: Path | None = None
    # Retake: whether a changed agent may take this exam again. Left out, it may not: an
    # exam is retaken only where its entry says ``"retakes": true``. Architect writes that
    # for the exams it drafts (``session.floors.register``).
    retakes: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def manifest_kind(self) -> Self:
        if self.manifest is not None and self.kind != FloorKind.HELD_OUT_TASKS:
            raise ValueError("Only held_out_tasks floors may name a manifest")
        if self.retake_cases is not None and self.kind != FloorKind.HELD_OUT_TASKS:
            raise ValueError("Only held_out_tasks floors may name retake cases")
        return self

    @property
    def retakes_allowed(self) -> bool:
        """No exam is retaken unless its entry says so in as many words (``"retakes":
        true``): a changed design must not take an evidence exam by accident, whether or
        not the entry still names the dataset's manifest."""
        return self.retakes is True


class Profile(_Settings):
    workspace: Path = Field(default_factory=lambda: Path(".architect"))
    models: dict[Nonempty, AdapterConfig] = Field(min_length=1)
    floors: dict[Nonempty, FloorConfig] = Field(default_factory=dict)
    calibrations: dict[Nonempty, Path] = Field(default_factory=dict)
    inference: NamedAdapterConfig | None = None
    decomposition: NamedAdapterConfig | None = None
    rewrite: NamedAdapterConfig | None = None
    # Phase 9: the Forge session's agent; the fallback is ``rewrite``, then the ladder.
    forge_agent: NamedAdapterConfig | None = None
    builder_floor: FloorDefinition | None = None
    builder_model: Nonempty | None = None
    compile: CompileConfig = Field(default_factory=CompileConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    pi: PiConfig = Field(default_factory=PiConfig)
    # Phase 10: the task router's policy and the role router's settings.
    routing: RoutePolicy = Field(default_factory=lambda: RoutePolicy.model_validate({}))
    roles: RoleRouting = Field(default_factory=lambda: RoleRouting.model_validate({}))
    _base: Path = PrivateAttr(default_factory=Path.cwd)
    _source: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def role_models(self) -> Self:
        check_roles(self.roles, self.models)
        return self

    def resolve(self, path: Path) -> Path:
        """``path`` relative to the profile file; one that starts with ``~`` is in the
        user's home folder (``"workspace": "~/.architect"`` shares the CLI's workspace,
        see ``in_workspace``)."""
        return (self._base / path.expanduser()).resolve()


# What a profile names to share the workspace of the conversational CLI and the desktop app.
SHARED_WORKSPACE = "~/.architect"


def in_workspace(profile: Profile, workspace: Path) -> Profile:
    """``profile`` with ``workspace`` as its workspace when it names ``~/.architect``.

    That value means the workspace the conversational CLI and the desktop app share, which
    is ``~/.architect`` unless ``--workspace``, ``ARCHITECT_WORKSPACE`` or the app's own
    setting moves it (``agent.app.agent_workspace``). A profile that names any other
    workspace keeps it."""
    if profile.workspace.as_posix().rstrip("/") != SHARED_WORKSPACE:
        return profile
    return profile.model_copy(update={"workspace": workspace.resolve()})


_JSON_PROFILE = TypeAdapter(dict[str, object])


def _role_code(error: ValidationError) -> str | None:
    """The fixed role routing code a validation failure carries, if any."""
    for detail in error.errors():
        cause = (detail.get("ctx") or {}).get("error")
        if isinstance(cause, RoleRoutingError):
            return cause.code
    return None


def load_profile(path: Path) -> Profile:
    """Load trusted setup only. Do not print validation errors containing secrets."""
    try:
        resolved = path.resolve(strict=True)
        text = resolved.read_text(encoding="utf-8")
        payload = (
            tomllib.loads(text)
            if resolved.suffix.lower() == ".toml"
            else (_JSON_PROFILE.validate_json(text))
        )
        profile = Profile.model_validate(payload)
        profile._base = resolved.parent
        profile._source = resolved
        return profile
    except ValidationError as exc:
        raise ProfileError(_role_code(exc) or "invalid_profile") from None
    except (OSError, ValueError, TypeError):
        raise ProfileError("invalid_profile") from None


def _adapter(profile: Profile, model: str, config: AdapterConfig) -> ModelAdapter:
    try:
        if config.provider == "fake":
            fixture = profile.resolve(config.fixture) if config.fixture is not None else None
            return FakeAdapter(model=model, fixture=fixture)
        key = None
        if config.api_key_env is not None:
            key = os.environ.get(config.api_key_env)
            if not key:
                raise ProfileError("missing_api_key")
        elif config.api_key_ref is not None:
            key = _credential(profile, config.api_key_ref)
        if config.provider == "claude-code":
            # Without api_key_env the child uses the CLI's subscription login.
            return ClaudeCodeAdapter(model, executable=config.executable or "claude", api_key=key)
        if config.provider == "codex":
            return CodexAdapter(model, executable=config.executable or "codex")
        prices = profile.resolve(config.prices) if config.prices is not None else None
        base_url = config.base_url
        if base_url is None and config.api_key_ref is not None:
            base_url = _credential_url(profile, config.api_key_ref)
        if config.provider == "anthropic":
            return AnthropicAdapter(
                model, api_key=key, base_url=base_url, prices_path=prices
            )
        if config.provider == "openai":
            return OpenAICompatibleAdapter(
                model,
                api_key=key,
                base_url=base_url,
                prices_path=prices,
                timeout=config.timeout_s,
                dialect="nebius" if is_nebius(config) else "openai",
                think=config.think,
                temperature=config.temperature,
                top_p=config.top_p,
                api_model=config.api_model,
                max_tokens=config.max_tokens,
            )
        return OllamaAdapter(
            model,
            api_key=key,
            base_url=base_url,
            prices_path=prices,
            num_ctx=config.num_ctx or OLLAMA_DEFAULT_NUM_CTX,
            think=config.think,
            timeout=config.timeout_s or 300.0,
        )
    except ProfileError:
        raise
    except (AdapterError, OSError, ValueError):
        raise ProfileError("adapter_unavailable") from None


def _credential(profile: Profile, ref: str) -> str:
    """The key ``ref`` names: environment first, else the workspace's credentials file.

    ``""`` for a keyless-capable provider without one (local servers, custom URLs);
    a missing required key is ``missing_api_key``. The value never reaches a message.
    """
    from architect.providers import credentials

    key = credentials.resolve(ref, profile.resolve(profile.workspace))
    if key is None:
        raise ProfileError("missing_api_key")
    return key


def _credential_url(profile: Profile, ref: str) -> str | None:
    """Where an entry that names the credential ``ref`` but no ``base_url`` sends its calls:
    the URL stored with that credential (a judge code's relay), else the provider's own
    from the catalog. None leaves the adapter's default."""
    from architect.providers import credentials
    from architect.providers.catalog import default_base_url, find

    stored = credentials.get_base_url(ref, profile.resolve(profile.workspace))
    if stored:
        return stored
    item = find(ref)
    return default_base_url(item) if item is not None else None


def model_unpriced(profile: Profile, model: str) -> bool:
    """True when ``model`` has no dollar price its adapter would use (``pricing.is_unpriced``).

    Build mode refuses to compile on such a model (``model_unpriced``); conversation
    and subagents may use it under a time budget with a "cost unknown" label. A
    model the profile does not configure counts as unpriced; ``fake`` is priced. An
    alias is priced by its ``api_model``, as its adapter is.
    """
    config: AdapterConfig | None = profile.models.get(model)
    if config is None:
        config = next((c for c in _named_configs(profile) if c is not None and c.model == model),
                      None)
    return config_unpriced(profile, model, config)


def config_unpriced(profile: Profile, model: str, config: AdapterConfig | None) -> bool:
    """``model_unpriced`` for one entry, e.g. a named role adapter with its own price table."""
    if config is None:
        return True
    if config.provider == "fake":
        return False
    prices = profile.resolve(config.prices) if config.prices is not None else None
    return is_unpriced(config.api_model or model, prices)


def forge_settings_of(config: CompileConfig) -> ForgeSettings:
    """``CompileConfig.forge`` (Section 8.1), or the disabled default when absent."""
    value = getattr(config, "forge", None)
    return value if isinstance(value, ForgeSettings) else ForgeSettings()


def resolve_forge_agent(
    profile: Profile,
    settings: ForgeSettings,
    adapter_factory: Callable[[str], ModelAdapter],
) -> ModelAdapter | None:
    """The Forge agent adapter, or None to fall back as Section 8.5 says.

    ``settings.agent_model`` names a profile key: the ``forge_agent`` model when it
    matches, else a ``models`` key; anything else is ``forge_model_unknown``.
    Without a name the ``forge_agent`` adapter is used when configured; None lets
    the driver fall back to ``rewrite`` and then the ladder model. No network
    request happens here.
    """
    named = profile.forge_agent
    wanted = settings.agent_model
    if wanted is not None:
        if named is not None and named.model == wanted:
            return _adapter(profile, named.model, named)
        if wanted not in profile.models:
            raise ProfileError("forge_model_unknown")
        return adapter_factory(wanted)
    if named is not None:
        return _adapter(profile, named.model, named)
    return None


def _compile_model(profile: Profile) -> str:
    """The first ladder model, else the first configured model (``Session._compile_model``)."""
    ladder = profile.compile.model_ladder
    return ladder[0] if ladder else next(iter(profile.models))


def _role_default(
    role: RoleName,
    profile: Profile,
    compiler: Compiler,
    orchestrator: Orchestrator,
    forge_adapter: ModelAdapter | None,
) -> tuple[ModelAdapter | None, RoleVariant]:
    fallback = _compile_model(profile)
    adapter: ModelAdapter | None
    if role == "inference":
        adapter, model = getattr(compiler, "inference_adapter", None), "fake"
    elif role == "decomposition":
        adapter = getattr(orchestrator, "decomposition_adapter", None)
        model = fallback
    elif role == "rewrite":
        adapter, model = getattr(compiler, "rewrite_adapter", None), fallback
    elif role == "forge_agent":
        # Exactly what the Compiler hands the Forge driver (``compiler/__init__.py``):
        # ``forge_adapter or rewrite_adapter``; None keeps the driver's own
        # ``adapter_factory(artifact.model)`` fallback.
        rewrite: ModelAdapter | None = getattr(compiler, "rewrite_adapter", None)
        adapter, model = forge_adapter or rewrite, fallback
    elif role == "floor_draft":
        adapter, model = None, fallback
    else:
        raise KeyError(role)
    return adapter, RoleVariant(model=adapter.model if adapter is not None else model)


def role_defaults(
    profile: Profile,
    compiler: Compiler,
    orchestrator: Orchestrator,
    forge_adapter: ModelAdapter | None,
) -> dict[RoleName, tuple[ModelAdapter | None, RoleVariant]]:
    """Today's binding per role (Section 11.2): the host-built adapter and pinned D.

    ``forge_agent`` is the Compiler's ``forge_adapter or rewrite_adapter``. None keeps
    each call site's own fallback: Stage 0 ``adapter_factory("fake")``, no rewrite,
    the Forge driver's ``adapter_factory(artifact.model)``, and floor drafting's
    ``spec.model_target`` else the first ladder model. Parameters are empty: D runs
    with the profile's own ``compile``, ``orchestrator`` and Forge settings.
    This is a snapshot; ``Services.roles`` reads the same binding live.
    """
    return {
        role: _role_default(role, profile, compiler, orchestrator, forge_adapter)
        for role in ROLE_NAMES
    }


class _LiveRoleDefaults(Mapping[RoleName, tuple[ModelAdapter | None, RoleVariant]]):
    """``role_defaults`` read on every lookup (Section 11.3).

    The Compiler's ``inference_adapter``, ``rewrite_adapter`` and ``forge_adapter``
    and the Orchestrator's ``decomposition_adapter`` are the pinned defaults, so the
    session's metered wrappers (``observe_services``) and ``Session._arm_forge``
    swaps are what every role call sees. A compiler without ``forge_adapter`` falls
    back to the resolved ``Services.forge_adapter``.
    """

    def __init__(
        self,
        profile: Profile,
        compiler: Compiler,
        orchestrator: Orchestrator,
        forge_adapter: ModelAdapter | None,
    ) -> None:
        self._profile = profile
        self._compiler = compiler
        self._orchestrator = orchestrator
        self._forge_adapter = forge_adapter

    def __getitem__(self, role: RoleName) -> tuple[ModelAdapter | None, RoleVariant]:
        if role not in ROLE_NAMES:
            raise KeyError(role)
        forge: ModelAdapter | None = getattr(self._compiler, "forge_adapter", self._forge_adapter)
        return _role_default(role, self._profile, self._compiler, self._orchestrator, forge)

    def __iter__(self) -> Iterator[RoleName]:
        return iter(ROLE_NAMES)

    def __len__(self) -> int:
        return len(ROLE_NAMES)


class _LiveFactory:
    """The Compiler's current ``adapter_factory``, read on every call.

    ``observe_services`` wraps ``compiler.adapter_factory`` in place with the session
    meter, so adapters for switched variants (and Stage 0's ``"fake"`` fallback) are
    built exactly as the Compiler builds them today.
    """

    def __init__(self, compiler: Compiler, fallback: Callable[[str], ModelAdapter]) -> None:
        self._compiler = compiler
        self._fallback = fallback

    def __call__(self, model: str) -> ModelAdapter:
        factory: Callable[[str], ModelAdapter] = getattr(
            self._compiler, "adapter_factory", self._fallback
        )
        return factory(model)


def live_roles(
    profile: Profile,
    compiler: Compiler,
    orchestrator: Orchestrator,
    forge_adapter: ModelAdapter | None,
    adapter_factory: Callable[[str], ModelAdapter],
    router: RoleRouter | None,
) -> RoleAdapters:
    """``Services.roles``: live pinned defaults, the Compiler's live factory, the pins."""
    return RoleAdapters(
        _LiveRoleDefaults(profile, compiler, orchestrator, forge_adapter),
        router,
        _LiveFactory(compiler, adapter_factory),
        _forge_pinned(profile),
    )


def _forge_pinned(profile: Profile) -> Callable[[RoleName], bool]:
    """Explicit pins disable routing for a role: ``compile.forge.agent_model`` (and so
    CLI ``--forge-model``) pins ``forge_agent``. Session pins are the session's own."""
    pinned = forge_settings_of(profile.compile).agent_model is not None

    def pins(role: RoleName) -> bool:
        return pinned and role == "forge_agent"

    return pins


@dataclass(frozen=True)
class Services:
    compiler: Compiler
    orchestrator: Orchestrator
    registry: Registry
    runtime: Runtime
    profile: Profile
    workspace: Path
    adapter_factory: Callable[[str], ModelAdapter]
    # Phase 9 seam: the resolved Forge agent (None: the driver's own fallback).
    forge_adapter: ModelAdapter | None = None
    # Phase 10: the task router (None when disabled or unavailable, P5) and the role
    # seam. An omitted ``roles`` is today's pinned binding built from the fields above.
    router: Router | None = None
    roles: RoleAdapters = cast(RoleAdapters, None)

    def __post_init__(self) -> None:
        if cast(object, self.roles) is None:
            roles = live_roles(
                self.profile,
                self.compiler,
                self.orchestrator,
                self.forge_adapter,
                self.adapter_factory,
                None,
            )
            object.__setattr__(self, "roles", roles)

    def close(self) -> None:
        """Idempotent; HTTP adapters own and close each request's client."""
        self.runtime.store.close()


# Workspace control-plane subdirectories Pi file tools never reach.
WORKSPACE_STATE = (
    "artifacts",
    "library",
    "registry",
    "telemetry",
    "proposals",
    "server",
    "routes",
    "exports",
    # Retake: the copies Undo restores and the record of exam attempts. A coder that could
    # write them could undo to its own files or hand itself unused cases again.
    "passed",
    "retakes",
)
# Phase 13: stored API keys and the generated price table (a coder that could rewrite
# prices could zero out a budget). Denied when present; Pi configs are recomputed per
# ``build_services`` and per coder run, so a file written by /login is covered next time.
WORKSPACE_FILES = ("credentials.json", "model-prices.json", "placements.json")
# W7: the Ed25519 private key and the trust list are denied whether or not they exist
# yet, so a coder can neither read the key nor create a trust list naming its own key.
WORKSPACE_TRUST = (ED25519_KEY_NAME, TRUST_FILE_NAME)


def _workspace_files(root: Path) -> set[Path]:
    return {root / name for name in WORKSPACE_FILES if (root / name).exists()}


def routing_declared(profile: Profile) -> bool:
    """True when the profile itself configures ``routing`` (and does not disable it)."""
    return "routing" in profile.model_fields_set and profile.routing.enabled


def _named_configs(profile: Profile) -> tuple[NamedAdapterConfig | None, ...]:
    return (profile.inference, profile.decomposition, profile.rewrite, profile.forge_agent)


def model_providers(profile: Profile) -> dict[str, Provider]:
    """Each configured model's provider: ``models`` first, then the named role adapters."""
    providers: dict[str, Provider] = {
        model: config.provider for model, config in profile.models.items()
    }
    for config in _named_configs(profile):
        if config is not None:
            providers.setdefault(config.model, config.provider)
    return providers


def _routing_stores(
    profile: Profile,
    workspace: Path,
    compiler: Compiler,
    registry: Registry,
    store: TelemetryStore,
    providers: Mapping[str, Provider],
) -> tuple[Router | None, EvidenceIndex | None, RouteLog | None]:
    """The task ``Router`` (None when ``routing.enabled`` is false), index and log.

    P5 (fail-open): if the evidence stores under ``workspace/routes`` cannot be
    opened, nothing is built and compiles and roles behave exactly as they do today.
    Construction reads no evidence; the stores are opened on first use.
    """
    try:
        library = compiler.library if isinstance(compiler.library, HarnessLibrary) else None
        log = RouteLog(workspace / "routes")
        index = EvidenceIndex(
            registry,
            library,
            store,
            log,
            workspace,
            providers=providers,
            promotion_reuses=library.promotion_reuses if library is not None else 3,
        )
        router = Router(index, profile.routing, log) if profile.routing.enabled else None
        return router, index, log
    except Exception:
        return None, None, None


def pi_denied_paths(profile: Profile | None, workspace: Path) -> set[Path]:
    """The host-owned paths Pi file tools are denied, before any caller extras.

    With a profile: its explicit ``pi.denied_paths`` (resolved against the profile
    file), the workspace control-plane subdirectories, the profile file itself,
    floor and smoke datasets, calibrations and fake-adapter fixtures. Without one
    (a fake-provider bench run): the workspace control-plane subdirectories only.
    """
    if profile is None:
        root = workspace.resolve()
        return (
            {root / name for name in (*WORKSPACE_STATE, *WORKSPACE_TRUST)}
            | _workspace_files(root)
        )
    root = profile.resolve(workspace)
    denied = {profile.resolve(path) for path in profile.pi.denied_paths}
    denied.update(root / name for name in (*WORKSPACE_STATE, *WORKSPACE_TRUST))
    denied.update(_workspace_files(root))
    if profile._source is not None:
        denied.add(profile._source)
    for floor_config in profile.floors.values():
        denied.add(profile.resolve(floor_config.path))
        if floor_config.smoke is not None:
            denied.add(profile.resolve(floor_config.smoke))
        if floor_config.manifest is not None:  # it lists held-out case ids
            denied.add(profile.resolve(floor_config.manifest))
        if floor_config.retake_cases is not None:
            denied.add(profile.resolve(floor_config.retake_cases))
    denied.update(profile.resolve(path) for path in profile.calibrations.values())
    adapters = (*profile.models.values(), *_named_configs(profile))
    for adapter_config in adapters:
        if adapter_config is not None and adapter_config.fixture is not None:
            denied.add(profile.resolve(adapter_config.fixture))
    return denied


def effective_pi_config(
    profile: Profile | None, workspace: Path, *, extra: Iterable[Path] = ()
) -> PiConfig:
    """The ``PiConfig`` every Pi session of this host runs with (Sections 8.3, 12.1).

    ``build_services`` and the bench both use this one rule; ``extra`` adds
    caller-owned secrets (a bench cell's signing key, telemetry, artifacts, rows).
    """
    denied = pi_denied_paths(profile, workspace)
    denied.update(Path(path).resolve() for path in extra)
    base = profile.pi if profile is not None else PiConfig()
    return base.model_copy(update={"denied_paths": tuple(sorted(denied))})


def _retake_source(
    profile: Profile, config: FloorConfig
) -> Callable[[], list[EvaluationCase]] | None:
    """Reads the floor's extra exam cases when a retake asks for them, never before."""
    if config.retake_cases is None:
        return None
    path = profile.resolve(config.retake_cases)

    def read() -> list[EvaluationCase]:
        return TypeAdapter(list[EvaluationCase]).validate_json(path.read_text(encoding="utf-8"))

    return read


def build_services(profile: Profile, workspace: Path) -> Services:
    """Wire only operator-configured adapters and datasets; close on setup failure."""
    workspace = profile.resolve(workspace)
    effective_pi = effective_pi_config(profile, workspace)
    # FeatureBuilder derives its scoped backend from Services.profile.pi, so it
    # must receive the same host-owned restrictions as the ordinary runtime.
    profile = profile.model_copy(update={"pi": effective_pi})

    def adapter_factory(model: str) -> ModelAdapter:
        config = profile.models.get(model)
        if config is None:
            raise ProfileError("model_not_configured")
        return _adapter(profile, model, config)

    def named(config: NamedAdapterConfig | None) -> ModelAdapter | None:
        return _adapter(profile, config.model, config) if config is not None else None

    # Validate configured backends before opening durable resources. No network
    # requests occur here, and no arbitrary Python evaluator import is supported.
    for model in profile.models:
        adapter_factory(model)
    for config in profile.floors.values():
        if config.kind == FloorKind.HELD_OUT_GAME_SCORE:
            raise ProfileError("unsupported_game_environment")
    inference = named(profile.inference)
    rewrite = named(profile.rewrite)
    # An unknown agent_model fails here, before any durable resource is opened.
    forge_adapter = resolve_forge_agent(
        profile, forge_settings_of(profile.compile), adapter_factory
    )
    decomposition = named(profile.decomposition)
    if decomposition is None:
        decomposition = named(profile.inference) or adapter_factory(next(iter(profile.models)))
    providers = model_providers(profile)
    store = TelemetryStore(workspace / "telemetry")
    try:
        runtime = Runtime(store, adapter_factory=adapter_factory, pi_backend=PiBackend(profile.pi))

        def floor_factory(definition: FloorDefinition, *, smoke: bool = False) -> FloorEvaluator:
            config = profile.floors.get(definition.dataset)
            if config is None or config.kind != definition.kind:
                raise ProfileError("floor_not_configured")
            path = config.smoke if smoke else config.path
            if path is None:
                raise ProfileError("smoke_not_configured")
            path = profile.resolve(path)
            try:
                if config.kind == FloorKind.HUMAN_EDIT_RATE:
                    return HumanEditRate.from_jsonl(definition, path)
                records = TypeAdapter(list[EvaluationCase]).validate_json(
                    path.read_text(encoding="utf-8")
                )
                if config.kind == FloorKind.HELD_OUT_TASKS:
                    tasks = HeldOutTasks(
                        definition,
                        records,
                        CHECKERS[config.checker] if config.checker != "equality" else None,
                    )
                    if config.retakes_allowed and not smoke:
                        tasks._allow_retakes(_retake_source(profile, config))
                    return tasks
                if config.kind == FloorKind.LLM_JUDGE:
                    calibration_path = profile.calibrations.get(
                        definition.calibration_dataset or ""
                    )
                    if calibration_path is None or not definition.judge_model:
                        raise ProfileError("calibration_not_configured")
                    calibration = TypeAdapter(list[CalibrationRow]).validate_json(
                        profile.resolve(calibration_path).read_text(encoding="utf-8")
                    )
                    return LLMJudge(
                        definition,
                        records,
                        calibration,
                        adapter_factory(definition.judge_model),
                        runtime,
                    )
                raise ProfileError("unsupported_game_environment")
            except ProfileError:
                raise
            except (OSError, ValueError):
                raise ProfileError("invalid_floor_fixture") from None

        def smoke_factory(definition: FloorDefinition) -> FloorEvaluator:
            return floor_factory(definition, smoke=True)

        compiler = Compiler(
            workspace / "artifacts",
            runtime,
            adapter_factory,
            floor_factory,
            config=profile.compile,
            inference_adapter=inference,
            rewrite_adapter=rewrite,
            smoke_factory=smoke_factory if any(c.smoke for c in profile.floors.values()) else None,
            # Section 12.1: the resolved Forge agent drives the session, and Pi runs
            # with the effective config (the denied set above, node, protocol cap).
            forge_adapter=forge_adapter,
            pi_config=effective_pi,
        )
        orchestrator = Orchestrator(compiler, decomposition, config=profile.orchestrator)
        registry = Registry(workspace / "registry")
        router, index, log = _routing_stores(
            profile, workspace, compiler, registry, store, providers
        )
        role_router = None
        if profile.roles.policy == "evidence" and index is not None and log is not None:

            def quote(model: str) -> float:
                # The fixed Stage 0 quote rule (``session/bootstrap.py``); lazy, no cycle.
                from architect.session.bootstrap import inference_quote

                return inference_quote(adapter_factory(model))

            role_router = RoleRouter(profile.roles, index, log, providers=providers, quote=quote)
        roles = live_roles(
            profile, compiler, orchestrator, forge_adapter, adapter_factory, role_router
        )
        # Phase 10 Section 11.3 / 10.2 (WP5): the Compiler's router and role seats.
        # Every compile routes only when the profile declares a ``routing`` table;
        # otherwise a compile routes only for an explicit reuse grant (P3), so the
        # default experience is exactly Phase 9's.
        compiler.router = router
        compiler.roles = roles
        compiler.route_all = routing_declared(profile)
        return Services(
            compiler=compiler,
            orchestrator=orchestrator,
            registry=registry,
            runtime=runtime,
            profile=profile,
            workspace=workspace,
            adapter_factory=adapter_factory,
            forge_adapter=forge_adapter,
            router=router,
            roles=roles,
        )
    except BaseException:
        store.close()
        raise
