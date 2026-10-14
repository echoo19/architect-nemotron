"""Structured reads and writes for the desktop bridge.

The terminal formats these as text; the app needs them as data. Every method calls the
same Python functions the slash commands call (provider status and sign-in, the ``/model``
picker rows, ``config.toml``, MCP rows, presets, the profile, build overrides, the
registry, ``ARCHITECT.md``); none reimplements their rules.

``BridgeData`` is a mixin of ``architect.desktop.bridge.Bridge``: ``m_<name>`` methods
are protocol requests. Methods marked "before boot" also work before ``boot``, which is
how the app runs first-run setup as structured steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..agent.sessions import clean_title, title_of
from ..agent.transcript import Transcript
from ..providers.host import Host
from .views import ViewLog, copy_view, remove_view

if TYPE_CHECKING:
    from ..agent.app import App
    from ..agent.repl import Repl
    from ..profiles import Profile, Services
    from ..providers.catalog import ProviderEntry
    from ..providers.discovery import Discovered
    from ..registry import Registry
    from ..spec import HarnessArtifact
    from .bridge import DesktopFrontend

__all__ = ["BridgeData", "BridgeError", "jsonable"]

FILE_LIMIT = 500
_URL = re.compile(r"https://[^\s\"'<>]+")
_SCOPES = ("project", "global")


class BridgeError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and value != value:
        return None
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


@dataclasses.dataclass
class _Run:
    """A retake or a practice run in progress. Counts and dollars only."""

    kind: str  # "retake" or "practice"
    plan: dict[str, Any]
    started: float
    done: int = 0
    total: int | None = None
    spent_usd: float | None = None
    task: asyncio.Task[None] | None = None

    def facts(self) -> dict[str, Any]:
        total = self.total if self.total is not None else self.plan.get("total")
        left = self.plan.get("left")
        if self.plan.get("cases") == "unused" and isinstance(left, int) and total:
            left -= total
        return {
            "attempt": self.plan.get("attempt"), "cases": self.plan.get("cases"),
            "done": self.done, "total": total, "spent_usd": self.spent_usd,
            "cap_usd": self.plan.get("cap_usd"), "left_after": left,
            "unit": self.plan.get("unit"), "pass_mark": self.plan.get("pass_mark"),
        }


def _code(exc: BaseException) -> str:
    from architect.session.codes import code_for

    return code_for(exc)


class BridgeData:
    """The structured ``m_*`` methods (see the module docstring)."""

    repl: Repl | None
    app: App | None
    cwd: Path
    busy: bool
    frontend: DesktopFrontend
    host: Host
    view: ViewLog | None
    _signed: dict[str, tuple[str | None, dict[str, Discovered]]]
    _retakes: dict[str, _Run]
    _retake_results: dict[str, dict[str, Any]]

    # Provided by Bridge.
    def _need(self) -> Repl:
        raise NotImplementedError

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def push_state(self) -> None:
        raise NotImplementedError

    # -- the login runner (no terminal) ---------------------------------------------------

    def make_host(self) -> Host:
        return Host(runner=self.run_login)

    def run_login(self, argv: Sequence[str]) -> int:
        """Run an official login command without a terminal (``codex login``).

        Runs in a worker thread (``asyncio.to_thread``). Output is shown in the
        conversation line by line; each https URL it prints becomes a ``link`` event the
        app opens. ``send`` takes a lock around one write, so calling it from this thread
        is safe. A missing executable is exit code 127, never an exception.
        """
        try:
            process = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
        except (OSError, ValueError):
            return 127
        seen: set[str] = set()
        stream = process.stdout
        if stream is not None:
            for line in stream:
                text = line.rstrip("\r\n")
                if text.strip():
                    self.send({"type": "event", "event": {"kind": "output", "text": text}})
                for raw in _URL.findall(text):
                    url = raw.rstrip(".,;:)]}'\"")
                    if url not in seen:
                        seen.add(url)
                        self.send({"type": "event", "event": {
                            "kind": "link", "url": url,
                            "text": "Finish signing in in your browser"}})
        try:
            return process.wait()
        except OSError:
            return 127

    # -- where (before boot too) ----------------------------------------------------------

    def _where(self, workspace: str | None, cwd: str | None) -> tuple[Path, Path]:
        """(cwd, workspace): the running app's, else what ``boot`` would use."""
        if self.app is not None:
            return Path(self.app.conductor.cwd), Path(self.app.workspace)
        from ..agent.app import agent_workspace

        here = Path(cwd).resolve() if cwd else self.cwd
        return here, agent_workspace(here, Path(workspace) if workspace else None)

    # -- providers and models -------------------------------------------------------------

    def _provider_models(self, item: ProviderEntry, status_models: Sequence[str]) -> list[str]:
        from ..agent.commands_providers import _offline

        if item.kind == "local":
            return list(status_models)
        try:
            return list(_offline(item))
        except Exception:
            return list(item.models)

    async def _choices(self, ws: Path, probe: bool) -> list[dict[str, Any]]:
        from ..providers.onboarding import onboarding_choices

        choices = await onboarding_choices(ws, host=self.host, probe_local=probe)
        return [{
            "id": c.provider.id,
            "label": c.provider.label,
            "kind": c.provider.kind,
            "sign_in": c.provider.sign_in,
            "state": c.status.state,
            "ready": c.status.ready,
            "detail": c.status.detail,
            "credential": c.status.credential,
            "models": self._provider_models(c.provider, c.status.models),
            "recommended": c.recommended,
            "suggested_model": c.model,
            "default_model": c.provider.default_model,
            "note": c.provider.note,
            "key_url": c.provider.key_url,
        } for c in choices]

    async def m_providers(self, probe: bool = True, workspace: str | None = None,
                          cwd: str | None = None) -> dict[str, Any]:
        """Every provider (``/providers``) in catalog order, with the recommendation.

        Before boot too (pass the same ``workspace``/``cwd`` as ``boot``).
        """
        from ..providers.catalog import CATALOG

        _, ws = self._where(workspace, cwd)
        rows = await self._choices(ws, probe)
        order = {item.id: index for index, item in enumerate(CATALOG)}
        rows.sort(key=lambda row: order.get(row["id"], len(order)))
        return {"providers": rows}

    async def m_models(self) -> dict[str, Any]:
        """The ``/model`` picker's rows: profile models first, then ready providers' models.
        ``ladder`` is the profile's ``compile.model_ladder``, the models a build tries."""
        from ..agent.commands_providers import _label, picker_rows
        from ..agent.config import context_window
        from ..providers.catalog import find
        from ..providers.signin import price_label

        repl = self._need()
        current = repl.conductor.adapter.model
        overrides = repl.conductor.extras.get("build_overrides")
        rows = await picker_rows(repl)
        return {
            "current": current,
            "next_compile": (overrides.get("model") if isinstance(overrides, dict) else None),
            "ladder": list(repl.app.services.profile.compile.model_ladder),
            "models": [{
                "name": name,
                "provider": provider,
                "label": _label(provider),
                "price": price_label(find(provider), name),
                "context_tokens": context_window(name),
                "configured": configured,
                "current": name == current,
            } for provider, name, configured in rows],
        }

    # -- first run (before boot) ----------------------------------------------------------

    async def m_onboarding(self, workspace: str | None = None, cwd: str | None = None,
                           probe: bool = True) -> dict[str, Any]:
        """First-run state: is there a profile ``boot`` would find, and every provider."""
        from ..session.bootstrap import SESSION_PROFILE, discover_profile

        here, ws = self._where(workspace, cwd)
        found = (Path(self.app.profile_path) if self.app is not None
                 else discover_profile(here, ws, None))
        return {
            "workspace": str(ws),
            "cwd": str(here),
            "profile_exists": found is not None and found.is_file(),
            "profile_path": str(found if found is not None else ws / SESSION_PROFILE),
            "choices": await self._choices(ws, probe),
        }

    async def m_sign_in(self, provider: str, workspace: str | None = None,
                        cwd: str | None = None) -> dict[str, Any]:
        """``signin.sign_in`` with questions and secrets as ``ask`` messages (before boot too).

        ChatGPT runs ``codex login`` without a terminal; its URL arrives as a ``link`` event.
        """
        from ..providers.catalog import find
        from ..providers.discovery import list_models
        from ..providers.signin import price_label, prompter_for_ui, sign_in

        item = find(provider)
        if item is None:
            raise BridgeError("unknown_provider", provider)
        _, ws = self._where(workspace, cwd)
        ws.mkdir(parents=True, exist_ok=True)
        result = await sign_in(item, ws, prompter_for_ui(self.frontend), host=self.host)
        models = tuple(result.models)
        if result.ok and not models and item.kind != "cli":
            found = await list_models(item, workspace=ws, host=self.host)
            models = tuple(found.models)
        if result.ok:
            self._signed[item.id] = (result.base_url, {m.id: m for m in models})
        return {
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "base_url": result.base_url,
            "default_model": item.default_model,
            "models": [{"id": m.id, "price": price_label(item, m)} for m in models],
        }

    async def m_choose_model(self, provider: str, model: str, workspace: str | None = None,
                             cwd: str | None = None) -> dict[str, Any]:
        """Add ``provider``'s ``model`` to the profile.

        Before boot: ``register_model`` into ``<workspace>/session-profile.json`` (the file
        the first run writes), so ``boot`` finds it and skips setup. After boot: what
        ``/model <provider>:<model>`` does (register, reload, switch).
        """
        from ..providers.catalog import find
        from ..providers.onboarding import ProviderError, register_model
        from ..providers.signin import price_label
        from ..session.bootstrap import SESSION_PROFILE

        item = find(provider)
        if item is None:
            raise BridgeError("unknown_provider", provider)
        base_url, listed = self._signed.get(item.id, (None, {}))
        chosen = listed.get(model)
        price = chosen.price if chosen is not None else None
        if self.repl is not None:
            from ..agent.commands_providers import _register_and_use

            ok = await _register_and_use(self.repl, item, model, price=price, base_url=base_url)
            self.push_state()
            if not ok:
                raise BridgeError("model_not_added", "The reason is shown in the conversation.")
            return {"profile_path": str(self.repl.app.profile_path), "model": model,
                    "price": price_label(item, chosen or model)}
        _, ws = self._where(workspace, cwd)
        path = ws / SESSION_PROFILE
        try:
            key = await asyncio.to_thread(
                register_model, path, item.id, model, workspace=ws, base_url=base_url,
                price=price, host=self.host)
        except ProviderError as exc:
            raise BridgeError(exc.code) from None
        return {"profile_path": str(path), "model": key, "price": price_label(item, chosen or key)}

    # -- config.toml ----------------------------------------------------------------------

    async def m_config(self) -> dict[str, Any]:
        """``/config`` as data: effective values, defaults, kinds, choices, sources, files."""
        from ..agent.config import KEYS, workspace_config_path

        repl = self._need()
        config = repl.app.config
        files = [str(workspace_config_path(repl.app.workspace))]
        if config.project_path is not None:
            files.append(str(config.project_path))
        return {
            "values": {key: config.get(key) for key in KEYS},
            "defaults": {key: spec.default for key, spec in KEYS.items()},
            "kinds": {key: spec.kind for key, spec in KEYS.items()},
            "choices": {key: list(spec.choices) for key, spec in KEYS.items() if spec.choices},
            "ranges": {key: [spec.minimum, spec.maximum] for key, spec in KEYS.items()
                       if spec.minimum is not None or spec.maximum is not None},
            "help": {key: spec.help for key, spec in KEYS.items()},
            "sources": {key: config.source(key) for key in KEYS},
            "errors": list(config.errors),
            "files": files,
        }

    async def m_set_config(self, key: str, value: Any = None) -> dict[str, Any]:
        """``/config set``: the workspace file's writer, then the same reload and effects.

        ``None`` (or ``"default"``) removes the key.
        """
        from ..agent.commands import _reload_config
        from ..agent.config import KEYS, ConfigError, set_value, workspace_config_path
        from ..agent.repl import set_permission
        from ..agent.tools import Permission

        repl = self._need()
        if key not in KEYS:
            raise BridgeError("unknown_key", key)
        path = workspace_config_path(repl.app.workspace)
        try:
            set_value(path, key, "" if value is None else value)
        except ConfigError as exc:
            raise BridgeError(exc.code, exc.detail) from None
        _reload_config(repl)
        config = repl.app.config
        if config.source(key) != "project":
            conductor = repl.conductor
            effective = config.get(key)
            if key == "permission" and isinstance(effective, str):
                set_permission(conductor, cast(Permission, effective))
            elif key == "session_budget_usd":
                conductor.settings.session_usd = config.number(key)
            elif key == "session_minutes":
                conductor.settings.session_s = config.number(key) * 60
        self.push_state()
        return await self.m_config()

    # -- MCP, presets ---------------------------------------------------------------------

    async def m_mcp(self) -> dict[str, Any]:
        """``/mcp`` as data (changes go through quiet ``/mcp ...`` commands)."""
        from ..agent.commands_mcp import mcp_manager

        repl = self._need()
        manager = mcp_manager(repl)
        await manager.apply()
        servers = []
        for row in manager.rows():
            state = manager.servers.get(str(row["name"]))
            servers.append({
                **row,
                "enabled": bool(state.config.enabled) if state is not None else True,
                "tool_names": ([tool.name for tool in state.tools]
                               if state is not None and state.status == "ready" else []),
                "sources": ([str(p) for p in state.config.sources] if state is not None else []),
            })
        return {"servers": servers, "problems": list(manager.problems)}

    async def m_presets(self) -> dict[str, Any]:
        """Every preset (``spawnable`` ones run with ``/spawn``); read-only."""
        from ..agent.tools import SPAWNABLE
        from ..presets import all_presets
        from ..presets._execution import _INSTRUCTIONS

        return {"presets": [{
            "name": preset.name,
            "description": _INSTRUCTIONS.get(preset.name, ""),
            "components": list(preset.components),
            "removable_components": list(preset.removable_components),
            "tunable_params": {
                name: {"values": jsonable(list(span.values)), "default": jsonable(span.default)}
                for name, span in preset.tunable_params.items()},
            "spawnable": preset.name in SPAWNABLE,
        } for preset in all_presets()]}

    # -- the profile ----------------------------------------------------------------------

    async def m_profile(self) -> dict[str, Any]:
        """The profile's editable parts (models, compile, forge, roles, routing)."""
        from ..profiles import load_profile
        from ..providers.onboarding import provider_of

        repl = self._need()
        path = Path(repl.app.profile_path)
        try:
            profile = load_profile(path)
        except Exception:
            profile = repl.app.services.profile
        compile_ = profile.compile
        roles = {
            role: (config.model if config is not None else None)
            for role, config in (("inference", profile.inference),
                                 ("decomposition", profile.decomposition),
                                 ("rewrite", profile.rewrite),
                                 ("forge_agent", profile.forge_agent))
        }
        return {
            "path": str(path),
            "format": "json" if path.suffix.lower() == ".json" else "toml",
            "editable": path.suffix.lower() == ".json",
            "models": {name: {"provider": provider_of(config), "adapter": config.provider,
                              "model": name, "base_url": config.base_url}
                       for name, config in profile.models.items()},
            "compile": {
                "model_ladder": list(compile_.model_ladder),
                "compile_budget": compile_.compile_budget.model_dump(mode="json"),
                "max_candidates": compile_.max_candidates,
                "strategy": compile_.strategy,
                "enable_rewriting": compile_.enable_rewriting,
                "max_rewrites": compile_.max_rewrites,
            },
            "forge": compile_.forge.model_dump(mode="json"),
            "roles": {**profile.roles.model_dump(mode="json"), "models": roles,
                      "builder_model": profile.builder_model},
            "routing": profile.routing.model_dump(mode="json"),
        }

    async def m_set_profile(self, path: list[str], value: Any = None) -> dict[str, Any]:
        """Set one key path in the JSON profile (``None`` removes it), validate, reload.

        An invalid result puts the file back: ``invalid_profile`` with the reason code.
        """
        from ..profiles import ProfileError, load_profile

        repl = self._need()
        if (not isinstance(path, list) or not path
                or not all(isinstance(part, str) and part for part in path)):
            raise BridgeError("invalid_params", "path is a list of keys")
        file = Path(repl.app.profile_path)
        if file.suffix.lower() != ".json":
            raise BridgeError("profile_not_json")
        try:
            before = _read_text(file)
            payload = json.loads(before)
        except (OSError, ValueError):
            raise BridgeError("invalid_profile", "unreadable") from None
        if not isinstance(payload, dict):
            raise BridgeError("invalid_profile", "not_an_object")
        node: dict[str, Any] = payload
        for part in path[:-1]:
            child = node.get(part)
            if child is None:
                child = {}
                node[part] = child
            if not isinstance(child, dict):
                raise BridgeError("invalid_path", part)
            node = child
        if value is None:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = value
        _write_text(file, json.dumps(payload, indent=2) + "\n")
        try:
            load_profile(file)
        except ProfileError as exc:
            _write_text(file, before)
            raise BridgeError("invalid_profile", str(exc) or "invalid_profile") from None
        reload = repl.conductor.extras.get("reload")
        if callable(reload):
            try:
                reload()
            except Exception as exc:
                _write_text(file, before)
                try:
                    reload()
                except Exception:
                    pass
                raise BridgeError("invalid_profile", _code(exc)) from None
        self.push_state()
        return await self.m_profile()

    async def m_set_role(self, role: str, model: str | None = None) -> dict[str, Any]:
        """Point one of Architect's own roles at a profile model (``None``: the default).

        A role holds a full adapter config plus its model name, so the model's own entry
        in ``models`` is copied (key references and executables included) rather than
        rebuilt here.
        """
        if role not in ("inference", "decomposition", "rewrite", "forge_agent"):
            raise BridgeError("invalid_role", role)
        if model is None:
            return await self.m_set_profile([role], None)
        repl = self._need()
        file = Path(repl.app.profile_path)
        if file.suffix.lower() != ".json":
            raise BridgeError("profile_not_json")
        try:
            payload = json.loads(_read_text(file))
        except (OSError, ValueError):
            raise BridgeError("invalid_profile", "unreadable") from None
        models = payload.get("models") if isinstance(payload, dict) else None
        entry = models.get(model) if isinstance(models, dict) else None
        if not isinstance(entry, dict):
            raise BridgeError("model_not_configured", model)
        return await self.m_set_profile([role], {**entry, "model": model})

    # -- build overrides ------------------------------------------------------------------

    def _overrides(self) -> dict[str, Any]:
        from ..agent.build import OVERRIDE_KEYS, override_lines

        repl = self._need()
        ctx = repl.conductor.context
        overrides = ctx.extras.get("build_overrides")
        lines = override_lines(ctx)
        return {
            "overrides": jsonable(overrides if isinstance(overrides, dict) else {}),
            "lines": lines,
            "override_lines": "\n".join(lines),
            "keys": list(OVERRIDE_KEYS),
        }

    async def m_overrides(self) -> dict[str, Any]:
        """What the next compile uses instead of inferred defaults (``/floor``, ``/budget``)."""
        return self._overrides()

    async def m_set_override(self, key: str, value: Any = None) -> dict[str, Any]:
        """``build.set_override`` (``None`` clears the key); refusals are error codes."""
        from ..agent.build import set_override
        from ..agent.tools import ToolError

        repl = self._need()
        try:
            text = set_override(repl.conductor.context, key, value)
        except ToolError as exc:
            raise BridgeError(exc.code, exc.detail) from None
        return {**self._overrides(), "text": text}

    async def m_clear_overrides(self) -> dict[str, Any]:
        from ..agent.build import clear_overrides

        text = clear_overrides(self._need().conductor.context)
        return {**self._overrides(), "text": text}

    # -- the registry ---------------------------------------------------------------------

    def _registry(self) -> Registry:
        return self._need().app.services.registry

    async def m_agents(self) -> dict[str, Any]:
        """Every installed agent, one row each; a broken entry is its own row (``ok`` false)."""
        registry = self._registry()
        try:
            names = _current_names(registry)
        except Exception as exc:
            return {"agents": [], "error": _code(exc)}
        rows = [_agent_row(registry, name) for name in names]
        for row in rows:
            row.update(self._standing(row))
        return {"agents": rows}

    def _standing(self, row: dict[str, Any]) -> dict[str, Any]:
        """Where the agent stands in the project it was added to, for its row in the list.

        ``stopped`` is why its folder refuses calls (``changed``, ``changed_undo_only`` or
        ``cannot_check``). ``placed`` is the score of the version in the project when that
        passed a retake, so the row shows the same score as the agent's page. Empty when
        the agent runs as built or was never added. Counts only."""
        from ..session.retake import placed_folder, status

        folder: Path | None = None
        try:
            workspace = self._need().app.services.workspace
            folder = placed_folder(workspace, row["name"], {row.get("spec_hash", "")})
            if folder is None:
                return {}
            state = status(folder, workspace)
        except Exception:
            # The folder is there and cannot be read as an agent: it refuses calls.
            return {"stopped": "changed_undo_only"} if folder is not None else {}
        if state.reason is not None:
            return {"stopped": state.reason}
        if state.attempt > 1 and state.right is not None and state.total:
            return {
                "placed": {
                    "right": state.right, "total": state.total,
                    "pass_mark": state.pass_mark, "attempt": state.attempt,
                }
            }
        return {}

    async def m_agent(self, name: str) -> dict[str, Any]:
        """Everything an agent page shows: spec, report, Forge record, design, history, files.

        The report is the signed one's aggregate fields and exam counts; its signatures
        (the HMAC and the Ed25519 seal) and verification id stay on disk (the page draws
        nothing from them). ``sample`` is the input of the first practice (tuning) case of
        the agent's floor, when the profile names that floor and its file reads; it is
        absent otherwise, and never an exam case."""
        from ..agent.tools import harness_design
        from ..session.forge import read_forge_record
        from ..spec import HarnessArtifact

        registry = self._registry()
        row = _agent_row(registry, name)
        if "directory" not in row:
            raise BridgeError("unknown_agent", name)
        directory = Path(row["directory"])
        try:
            artifact: HarnessArtifact | None = HarnessArtifact.load(directory)
        except Exception:
            artifact = None
        detail: dict[str, Any] = {**row, "status": "ok" if row["ok"] else row.get("error")}
        if artifact is not None:
            report = artifact.report
            detail.update({
                "licensed": bool(row["ok"]) and artifact.licensed,
                "spec": artifact.spec.model_dump(mode="json"),
                "spec_hash": artifact.spec.spec_hash,
                "preset": artifact.preset,
                "model": artifact.model,
                "report": (report.model_dump(mode="json",
                                             exclude={"signature", "ed25519", "verification_id"})
                           if report is not None else None),
            })
            sample = _practice_sample(self._need().app.services.profile, artifact)
            if sample is not None:
                detail["sample"] = sample
        else:
            detail.update({"licensed": False, "spec": None, "report": None})
        detail["forge"] = read_forge_record(directory)
        detail["design"] = harness_design(directory)
        try:
            detail["history"] = [{
                "revision": entry.revision, "spec_hash": entry.spec_hash,
                "artifact_version": entry.artifact_version, "directory": str(entry.directory),
                "current": entry.revision == row.get("revision"),
            } for entry in registry.history(name)]
        except Exception:
            detail["history"] = []
        try:
            detail["drift"] = registry.drift(name)
        except Exception as exc:
            detail["drift"] = None
            detail.setdefault("drift_error", _code(exc))
        detail["files"] = _files(directory)
        tree = directory / "subharnesses"
        detail["subharnesses"] = sorted(
            child.name for child in tree.iterdir() if child.is_dir()) if tree.is_dir() else []
        return cast(dict[str, Any], jsonable(detail))

    async def m_run_agent(self, name: str, input: Any = "") -> dict[str, Any]:
        """Run an installed agent the way ``/run`` does (``run_agent`` through the Repl).

        The run is a quiet turn (``turn_started``/``turn_finished`` with ``quiet``), so the
        line it shows in the conversation folds like any command's. The result carries
        the run's cost, seconds, model calls, run id and the provider's completion id."""
        from ..models import relay

        repl = self._need()
        if self.busy:
            raise BridgeError("busy", "Wait for the current turn to finish.")
        text = f"/run {name}"
        started = time.monotonic()
        refusals = relay.mark()
        self.send({"type": "turn_started", "text": text, "quiet": True,
                   "at": round(time.time(), 3)})
        try:
            result = await repl.call_tool("run_agent", {"name": name, "input": input})
        finally:
            self.send({"type": "turn_finished", "text": text, "quiet": True,
                       "seconds": round(time.monotonic() - started, 2),
                       "at": round(time.time(), 3)})
            if self.view is not None:
                self.view.flush()
        self.push_state()
        if not isinstance(result, dict):
            refused = relay.since(refusals)
            if refused is not None:  # the relay said why, or nothing answered: as in the CLI
                raise BridgeError(refused, relay.sentence(refused))
            # Repl.call_tool printed the reason under the "/run <name>" line this run
            # opened in the conversation; Your agents shows no conversation, so name it.
            raise BridgeError(
                "run_failed", f"The reason is in the conversation, under /run {name}."
            )
        return cast(dict[str, Any], jsonable(result))

    async def m_rollback(self, name: str, revision: int | None = None) -> dict[str, Any]:
        """Make an earlier revision current (``Registry.rollback``)."""
        from ..registry import RegistryError

        try:
            self._registry().rollback(name, revision)
        except RegistryError as exc:
            raise BridgeError(str(exc) or "rollback_failed") from None
        return await self.m_agent(name)

    # -- retake ---------------------------------------------------------------------------

    def _placed(self, name: str) -> tuple[Services, Path | None]:
        """The folder the agent ``name`` was last added to a project at, if it was."""
        from ..session.retake import placed_folder

        services = self._need().app.services
        hashes: set[str] = set()
        try:
            hashes = {entry.spec_hash for entry in services.registry.history(name)}
        except Exception:
            pass
        return services, placed_folder(services.workspace, name, hashes)

    def _placed_or_refuse(self, name: str) -> tuple[Services, Path]:
        services, folder = self._placed(name)
        if folder is None:
            raise BridgeError("not_placed", "This agent has not been added to a project.")
        if str(folder) in self._retakes:
            raise BridgeError("busy", "Wait for the run on this agent to finish.")
        return services, folder

    def _shown(self, folder: Path) -> str:
        try:
            return folder.resolve().relative_to(self.cwd.resolve()).as_posix()
        except (ValueError, OSError):
            return folder.as_posix()

    def _retake_view(self, services: Services, folder: Path) -> dict[str, Any]:
        """Where a placed agent stands, for its page: counts, fixed words, file names and
        dollars. No case, case id or per-case result is in it."""
        from ..session import retake as retakes

        key = str(folder)
        workspace = services.workspace
        state = retakes.status(folder, workspace, services)
        view: dict[str, Any] = {
            "placed": True, "folder": self._shown(folder), **state.facts(),
            **retakes.exam_facts(folder, workspace, services),
            "attempts": [item.facts() for item in retakes.attempts(folder, workspace)],
            "plan": None, "progress": None, "last": None, "practice": None, "refused": None,
        }
        run = self._retakes.get(key)
        if run is not None and run.kind == "retake":
            view["state"] = "retaking"
            view["progress"] = run.facts()
            return view
        if state.state != "stopped":
            self._retake_results.pop(key, None)
            return view
        digest = retakes.edit_digest(folder)
        kept = self._retake_results.get(key)
        if kept is not None and kept.get("edit") != digest:
            del self._retake_results[key]  # the files changed again: it no longer applies
            kept = None
        if state.can_retake:
            try:
                view["plan"] = retakes.plan(folder, services)
            except retakes.RetakeError as exc:
                # Only the exam itself could say this (its cases changed, say): the
                # page shows why, and offers Undo alone.
                said = [line for line in exc.lines if line not in view["lines"]]
                view.update(can_retake=False, refusal=exc.code, lines=[*view["lines"], *said])
        last = retakes.last_result(folder, workspace)
        if last is not None:
            view["last"] = last.facts()
            outcome = (kept or {}).get("retake")
            if isinstance(outcome, dict) and outcome.get("attempt") == last.number:
                view["last"].update(
                    pass_mark=outcome.get("pass_mark"), cost_usd=outcome.get("cost_usd"),
                    seconds=outcome.get("seconds"),
                )
            if last.result == "did_not_pass":
                view["state"] = "did_not_pass"
        if run is not None:
            view["practice"] = {"running": True}
        elif kept is not None and isinstance(kept.get("practice"), dict):
            view["practice"] = {"running": False, **kept["practice"]}
        if kept is not None and kept.get("refused"):
            view["refused"] = kept["refused"]
        return view

    async def m_retake_status(self, name: str) -> dict[str, Any]:
        """Where the agent stands in the project it was added to: passed, stopped (with
        the changed files and whether a retake is allowed), retaking (a count of answered
        cases, never a score), or did not pass; its attempts; and what a retake would
        cost. ``{"placed": false}`` for an agent never added to a project."""
        services, folder = self._placed(name)
        if folder is None:
            return {"placed": False}
        return cast(dict[str, Any], jsonable(self._retake_view(services, folder)))

    def _start_run(self, kind: str, services: Services, folder: Path) -> None:
        """Start a retake or a practice run of the changed agent as a background task.

        The page showed the attempt, its cases, the estimate and the cost cap
        (``retake_status``'s ``plan``) before the click that got here, so the click is the
        consent. A refusal uses no attempt and is kept as plain lines for the page."""
        from ..runtime.wallet import active_wallets
        from ..session import retake as retakes

        key = str(folder)
        planned = retakes.plan(folder, services)  # RetakeError: refused, nothing started
        digest = retakes.edit_digest(folder)
        kept = self._retake_results.get(key)
        if kept is None or kept.get("edit") != digest:
            kept = self._retake_results[key] = {"edit": digest}
        kept.pop("refused", None)
        run = _Run(kind=kind, plan=planned, started=time.monotonic())

        def on_case(done: int, total: int) -> None:
            run.done, run.total = done, total
            wallets = active_wallets()
            if wallets:
                run.spent_usd = wallets[-1].cost_usd

        async def work() -> None:
            try:
                if kind == "retake":
                    outcome = await retakes.retake(
                        folder, services, lambda _line: True, on_case=on_case
                    )
                else:
                    outcome = await retakes.practice(folder, services, lambda _line: True)
                kept[kind] = outcome.facts()
            except asyncio.CancelledError:
                pass
            except retakes.RetakeError as exc:
                kept["refused"] = list(exc.lines)
            except Exception as exc:  # provider errors may carry secrets: fixed words only
                from ..session.codes import explain

                what = "retake" if kind == "retake" else "practice run"
                why = explain(_code(exc))
                why = "" if why.startswith("No explanation") else f"{why} "
                kept["refused"] = [
                    f"The {what} could not finish. {why}Your agent stays stopped."
                ]
            finally:
                self._retakes.pop(key, None)

        run.task = asyncio.create_task(work())
        self._retakes[key] = run

    async def m_retake(self, name: str) -> dict[str, Any]:
        """The changed agent takes its exam again (``architect.session.retake.retake``).
        Returns at once with the ``retaking`` state; ``retake_status`` follows it."""
        from ..session.retake import RetakeError

        services, folder = self._placed_or_refuse(name)
        try:
            self._start_run("retake", services, folder)
        except RetakeError as exc:
            raise BridgeError(exc.code, " ".join(exc.lines)) from None
        return cast(dict[str, Any], jsonable(self._retake_view(services, folder)))

    async def m_retake_cancel(self, name: str) -> dict[str, Any]:
        """Cancel a running retake or practice run. A cancelled retake keeps its attempt
        and its cases (the record says cancelled); the agent stays stopped."""
        services, folder = self._placed(name)
        if folder is None:
            return {"placed": False}
        run = self._retakes.get(str(folder))
        if run is not None and run.task is not None:
            run.task.cancel()
            with contextlib.suppress(BaseException):
                await run.task
        return cast(dict[str, Any], jsonable(self._retake_view(services, folder)))

    async def m_practice(self, name: str) -> dict[str, Any]:
        """Score the change on the exam's practice half. Not an attempt."""
        from ..session.retake import RetakeError

        services, folder = self._placed_or_refuse(name)
        try:
            self._start_run("practice", services, folder)
        except RetakeError as exc:
            raise BridgeError(exc.code, " ".join(exc.lines)) from None
        return cast(dict[str, Any], jsonable(self._retake_view(services, folder)))

    async def m_undo(self, name: str) -> dict[str, Any]:
        """Put back the version that passed, byte for byte. 0 model calls."""
        from ..session import retake as retakes

        services, folder = self._placed_or_refuse(name)
        try:
            outcome = await asyncio.to_thread(retakes.undo, folder, services.workspace)
        except retakes.RetakeError as exc:
            raise BridgeError(exc.code, " ".join(exc.lines)) from None
        self._retake_results.pop(str(folder), None)
        view = self._retake_view(services, folder)
        view["undone"] = list(outcome.lines)
        return cast(dict[str, Any], jsonable(view))

    # -- images ---------------------------------------------------------------------------

    def _pending(self) -> dict[str, Any]:
        repl = self._need()
        return {"pending": [{"name": rel, "media_type": image.media_type,
                             "width": image.width, "height": image.height,
                             "sha256": image.sha256} for rel, image in repl.pending_images]}

    async def m_attach_image(self, path: str) -> dict[str, Any]:
        """Attach an image from any location to the next message (``/image`` without the
        working-directory rule: the person picked the file)."""
        from ..agent.repl import load_image

        repl = self._need()
        file = _absolute(path, Path(repl.conductor.cwd))
        if not file.is_file():
            raise BridgeError("not_found", str(file))
        image = load_image(file)
        if image is None:
            raise BridgeError("invalid_image", "PNG, JPEG, WebP or GIF, within the size limit")
        repl.pending_images.append((file.name, image))
        self.push_state()
        return self._pending()

    async def m_detach_image(self, index: int | None = None) -> dict[str, Any]:
        """Remove one pending image (or all)."""
        repl = self._need()
        if index is None:
            repl.pending_images.clear()
        elif 0 <= index < len(repl.pending_images):
            repl.pending_images.pop(index)
        self.push_state()
        return self._pending()

    # -- ARCHITECT.md ---------------------------------------------------------------------

    def _memory_path(self, scope: str) -> Path:
        if scope not in _SCOPES:
            raise BridgeError("invalid_scope", scope)
        repl = self._need()
        if scope == "global":
            return Path(repl.app.workspace) / "ARCHITECT.md"
        return Path(repl.conductor.cwd) / "ARCHITECT.md"

    async def m_memory(self, scope: str = "project") -> dict[str, Any]:
        """The ARCHITECT.md ``/memory edit`` opens (project, or ``global`` for the workspace's)."""
        from ..agent.conductor import INSTRUCTION_FILES

        path = self._memory_path(scope)
        exists = path.is_file()
        try:
            text = path.read_text(encoding="utf-8", errors="replace") if exists else ""
        except OSError:
            raise BridgeError("unreadable", str(path)) from None
        result: dict[str, Any] = {"scope": scope, "path": str(path), "text": text,
                                  "exists": exists}
        if scope == "project":
            here = Path(self._need().conductor.cwd)
            used = next((here / n for n in INSTRUCTION_FILES if (here / n).is_file()), None)
            result["in_use"] = str(used) if used is not None else None
        return result

    async def m_save_memory(self, scope: str, text: str) -> dict[str, Any]:
        """Write ARCHITECT.md and reload the conductor's instructions (as ``/memory edit``)."""
        from ..agent.conductor import project_instructions

        repl = self._need()
        path = self._memory_path(scope)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(path, text)
        except OSError:
            raise BridgeError("unwritable", str(path)) from None
        repl.conductor.instructions = project_instructions(repl.conductor.cwd, repl.app.workspace)
        self.push_state()
        return await self.m_memory(scope)

    # -- saved conversations --------------------------------------------------------------

    async def m_search_sessions(self, query: str, limit: int = 40) -> dict[str, Any]:
        """Saved conversations whose title or text contains ``query`` (case-insensitive),
        newest first, each with a short excerpt around the first hit."""
        repl = self._need()
        needle = query.strip().lower()
        if not needle:
            return {"results": []}
        store = repl.app.store
        results: list[dict[str, Any]] = []
        for info in store.list():
            hit = needle in info.title.lower()
            excerpt = ""
            try:
                text = store.path(info.id).read_text(encoding="utf-8")
            except (OSError, ValueError):
                text = ""
            lowered = text.lower()
            at = lowered.find(needle)
            if at >= 0:
                hit = True
                # The jsonl escapes newlines; show a readable slice of the hit.
                raw = text[max(0, at - 60): at + len(needle) + 80]
                for mark in ("\\n", "\\t", '\\"', "```", "**", "`"):
                    raw = raw.replace(mark, '"' if mark == '\\"' else " ")
                excerpt = " ".join(raw.split())
            if hit:
                results.append({**dataclasses.asdict(info), "excerpt": excerpt})
            if len(results) >= limit:
                break
        return {"results": results}

    async def m_delete_session(self, id: str) -> dict[str, Any]:
        """Delete a saved conversation: its transcript and its index row. The one this
        process is showing cannot be deleted (close it first)."""
        repl = self._need()
        if id == repl.conductor.session_id:
            raise BridgeError("session_open", id)
        store = repl.app.store
        try:
            path = store.path(id)
        except ValueError:
            raise BridgeError("invalid_session_id", id) from None
        rows = store._read_index()
        if id not in rows and not path.exists():
            raise BridgeError("not_found", id)
        rows.pop(id, None)
        try:
            path.unlink(missing_ok=True)
            store._write_index(rows)
        except OSError:
            raise BridgeError("unwritable", str(path)) from None
        remove_view(store, id)
        return {"deleted": id}

    async def m_fork_session(self, id: str, upto: int | None = None) -> dict[str, Any]:
        """Copy a saved conversation into a new one (Codex's ``/fork``).

        ``upto`` keeps the first that many of your messages and everything answered
        to them, so a fork can start from an earlier point; without it the whole
        conversation is copied. The original is left as it was.
        """
        repl = self._need()
        store = repl.app.store
        try:
            path = store.path(id)
        except ValueError:
            raise BridgeError("invalid_session_id", id) from None
        if id == repl.conductor.session_id and self.busy:
            raise BridgeError("busy", "Wait for the current turn to finish.")
        if not path.is_file():
            raise BridgeError("not_found", id)
        transcript = Transcript.from_jsonl(_read_text(path))
        messages = list(transcript.messages)
        if upto is not None:
            if upto < 1:
                raise BridgeError("invalid_params", "upto must be at least 1")
            users = [i for i, m in enumerate(messages) if m.role == "user"]
            if upto < len(users):
                messages = messages[: users[upto]]
        if not messages:
            raise BridgeError("empty", id)
        row = store._read_index().get(id)
        title = row.title if row else title_of(transcript)
        new_id = uuid.uuid4().hex[:12]
        info = store.save(
            new_id, Transcript(messages=messages), Path(row.cwd) if row and row.cwd else Path.cwd(),
            title=clean_title(f"{title} (fork)"),
        )
        if info is None:
            raise BridgeError("empty", id)
        if upto is None:
            # All of it: what it showed too. A fork from an earlier point shows its transcript.
            copy_view(store, id, new_id)
        return {"id": new_id, "title": info.title, "cwd": info.cwd, "messages": len(messages)}


# -- helpers ----------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _absolute(path: str, cwd: Path) -> Path:
    file = Path(path).expanduser()
    return file if file.is_absolute() else cwd / file


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _current_names(registry: Registry) -> list[str]:
    """Names with a current revision, read without loading any entry (so one damaged
    entry cannot hide the others, as ``Registry.list`` would)."""
    with registry._transaction() as db:
        return [str(row[0]) for row in db.execute("SELECT name FROM current ORDER BY name")]


def _agent_row(registry: Registry, name: str) -> dict[str, Any]:
    from ..spec import HarnessArtifact

    row: dict[str, Any] = {"name": name, "ok": False}
    try:
        entry = registry.status(name)  # authenticated: the license is re-read from disk
        row["ok"] = True
    except Exception as exc:
        row["error"] = _code(exc)
        try:
            with registry._transaction() as db:
                entry = registry._entry(db, name)
        except Exception:
            return row
    row.update({"revision": entry.revision, "spec_hash": entry.spec_hash,
                "artifact_version": entry.artifact_version, "directory": str(entry.directory)})
    try:
        artifact = HarnessArtifact.load(entry.directory)
    except Exception as exc:
        row["ok"] = False
        row.setdefault("error", _code(exc))
        return row
    spec, report = artifact.spec, artifact.report
    row.update({
        "licensed": bool(row["ok"]) and artifact.licensed,
        "task": spec.task[:160],
        "preset": artifact.preset,
        "model": artifact.model,
        "floor_kind": spec.floor.kind.value,
        "floor_limit": spec.floor.limit,
        "floor_score": report.floor_score if report is not None else None,
    })
    return row


def _practice_sample(profile: Profile, artifact: HarnessArtifact) -> Any:
    """The input of the first practice case of ``artifact``'s floor, split by the floor's
    own rule (``FloorEvaluator.tuning``: sorted ids, even positions practice), so an exam
    case can never come out. None when the profile does not name that floor, the floor
    is not a held-out task floor, or its file does not read."""
    from ..floors import HeldOutTasks
    from ..session.floors import load_cases
    from ..spec import FloorKind

    definition = artifact.spec.floor
    config = profile.floors.get(definition.dataset)
    if (
        config is None
        or definition.kind != FloorKind.HELD_OUT_TASKS
        or config.kind != FloorKind.HELD_OUT_TASKS
    ):
        return None
    try:
        tuning = HeldOutTasks(definition, load_cases(profile.resolve(config.path))).tuning
    except Exception:
        return None
    return tuning[0].task if tuning else None


def _files(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if len(files) >= FILE_LIMIT:
            break
        try:
            if path.is_file() and not path.is_symlink():
                files.append({"path": path.relative_to(directory).as_posix(),
                              "size": path.stat().st_size})
        except OSError:
            continue
    return files
