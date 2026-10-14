"""Can this provider be used now? Cheap checks only, never a model call.

- CLIs: is the command on PATH, and is there a login on disk (Codex:
  ``$CODEX_HOME/auth.json`` or ``~/.codex/auth.json``; Claude:
  ``~/.claude/.credentials.json``). The Claude CLI may keep its login in the OS
  keychain instead, so a found ``claude`` without the file is still usable
  (``cli_found``); an unconfirmed Codex login is not.
- APIs: is a key in the environment or stored (never its value).
- Local servers and custom URLs: does ``GET`` on the model list answer within a
  short timeout (skipped with ``probe=False``). Listing models loads none.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import credentials, judge
from .catalog import CATALOG, ProviderEntry
from .discovery import list_models
from .host import Host

__all__ = [
    "LOCAL_TIMEOUT_S",
    "Credential",
    "ProviderStatus",
    "State",
    "all_statuses",
    "login_file_present",
    "provider_status",
]

LOCAL_TIMEOUT_S = 1.5
State = Literal[
    "signed_in", "cli_found", "cli_missing", "key_env", "key_stored", "no_key",
    "reachable", "unreachable", "not_checked", "not_configured",
]
# What an API provider signs in with: a key, or a judge code (Nebius through the judges'
# relay, ``providers.judge``), so a front end can say which one is stored.
Credential = Literal["key", "judge_code"]


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    state: State
    ready: bool
    detail: str
    models: tuple[str, ...] = ()  # what a reachable local server reported
    credential: Credential | None = None  # set in key_env and key_stored only


def login_file_present(item: ProviderEntry, host: Host) -> bool:
    if item.id == "chatgpt":
        configured = host.env.get("CODEX_HOME", "").strip()
        root = Path(configured) if configured else host.home / ".codex"
        return (root / "auth.json").is_file()
    configured = host.env.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(configured) if configured else host.home / ".claude"
    return (root / ".credentials.json").is_file()


def _cli(item: ProviderEntry, host: Host) -> ProviderStatus:
    command = item.executable or item.id
    found = host.which(command)
    if not found:
        how = ("install Claude Code, then run 'claude' and log in" if item.id == "claude"
               else "install the Codex CLI, then /login chatgpt")
        return ProviderStatus(item.id, "cli_missing", False, f"{command} not found ({how})")
    if login_file_present(item, host):
        return ProviderStatus(item.id, "signed_in", True, f"signed in ({os.path.basename(found)})")
    if item.id == "claude":
        return ProviderStatus(item.id, "cli_found", True,
                              "claude found (login not confirmed; run 'claude' if calls fail)")
    return ProviderStatus(item.id, "cli_found", False, "codex found, not signed in")


async def _server(item: ProviderEntry, workspace: Path, host: Host, probe: bool,
                  timeout_s: float) -> ProviderStatus:
    stored = credentials.get_base_url(item.id, workspace)
    if item.id == "custom" and not stored:
        return ProviderStatus(item.id, "not_configured", False, "no URL yet (/login custom)")
    if not probe:
        return ProviderStatus(item.id, "not_checked", False, "not checked")
    found = await list_models(item, workspace=workspace, host=host, timeout_s=timeout_s)
    where = found.base_url or "?"
    if found.ok:
        count = len(found.models)
        return ProviderStatus(item.id, "reachable", True,
                              f"reachable at {where}, {count} model(s)",
                              tuple(model.id for model in found.models))
    if found.code in ("unauthorized", "no_key"):
        return ProviderStatus(item.id, "reachable", False, f"{where} needs a key")
    return ProviderStatus(item.id, "unreachable", False, f"not reachable at {where}")


async def provider_status(
    item: ProviderEntry,
    workspace: Path,
    *,
    host: Host | None = None,
    probe: bool = True,
    timeout_s: float = LOCAL_TIMEOUT_S,
) -> ProviderStatus:
    host = host or Host()
    if item.kind == "cli":
        return _cli(item, host)
    if item.kind == "local" or item.id == "custom":
        return await _server(item, workspace, host, probe, timeout_s)
    where = credentials.source(item.id, workspace, env=host.env)
    if where == "file":
        code = item.id == "nebius" and judge.is_code(credentials.get(item.id, workspace, env={}))
        return ProviderStatus(item.id, "key_stored", True,
                              "judge code stored" if code else "key stored",
                              credential="judge_code" if code else "key")
    if where is not None:
        return ProviderStatus(item.id, "key_env", True, f"key in {where}", credential="key")
    return ProviderStatus(item.id, "no_key", False, f"no key (/login {item.id})")


async def all_statuses(
    workspace: Path, *, host: Host | None = None, probe: bool = True
) -> tuple[ProviderStatus, ...]:
    """Every catalog provider's status, in catalog order (local probes run together)."""
    host = host or Host()
    return tuple(await asyncio.gather(*(
        provider_status(item, workspace, host=host, probe=probe) for item in CATALOG
    )))
