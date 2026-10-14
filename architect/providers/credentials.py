"""API keys: the environment first, else ``<workspace>/credentials.json``.

A credential is named by a provider id (``openai``, ``openrouter``, ``custom``...).
``get`` checks the provider's key variables (``catalog.key_envs``) and only then the
file. The file is ``{"version": 1, "keys": {name: key}, "base_urls": {name: url}}``,
written atomically and created with mode 0600 where the OS honours it (POSIX; on
Windows it inherits the user profile's ACL). ``base_urls`` remembers non-default
server URLs (custom endpoints, a moved local server); they are not secret.

Keys are never logged, printed, put in telemetry, transcripts or error messages:
every failure here is a ``CredentialError`` with a fixed code. The profile refers
to a key by name (``AdapterConfig.api_key_ref``), never by value.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from .catalog import key_envs, key_required

__all__ = [
    "CREDENTIALS_FILE",
    "CredentialError",
    "get",
    "get_base_url",
    "names",
    "path_for",
    "remove",
    "resolve",
    "set_base_url",
    "set_key",
    "source",
]

CREDENTIALS_FILE = "credentials.json"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MAX_KEY = 4096


class CredentialError(Exception):
    """A fixed code (``invalid_name``, ``invalid_key``, ``invalid_url``, ``write_failed``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def path_for(workspace: Path) -> Path:
    return workspace / CREDENTIALS_FILE


def _check_name(name: str) -> str:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise CredentialError("invalid_name")
    return name


def _read(workspace: Path) -> dict[str, dict[str, str]]:
    """The file's maps; a missing or unreadable file is empty (never an error shown)."""
    try:
        raw = json.loads(path_for(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"keys": {}, "base_urls": {}}
    result: dict[str, dict[str, str]] = {"keys": {}, "base_urls": {}}
    if isinstance(raw, dict):
        for section in result:
            values = raw.get(section)
            if isinstance(values, dict):
                result[section] = {
                    str(k): v for k, v in values.items() if isinstance(v, str) and v
                }
    return result


def _write(workspace: Path, data: dict[str, dict[str, str]]) -> None:
    target = path_for(workspace)
    temporary = target.with_name(target.name + ".tmp")
    text = json.dumps({"version": 1, **data}, indent=2, sort_keys=True) + "\n"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise CredentialError("write_failed") from None


def get(name: str, workspace: Path, *, env: Mapping[str, str] | None = None) -> str | None:
    """The key for ``name``: its environment variables first, else the file; None if absent."""
    environ = os.environ if env is None else env
    for variable in key_envs(name):
        value = environ.get(variable, "").strip()
        if value:
            return value
    return _read(workspace)["keys"].get(name)


def source(name: str, workspace: Path, *, env: Mapping[str, str] | None = None) -> str | None:
    """Where ``get`` would find the key: the variable's name, ``"file"``, or None."""
    environ = os.environ if env is None else env
    for variable in key_envs(name):
        if environ.get(variable, "").strip():
            return variable
    return "file" if name in _read(workspace)["keys"] else None


def resolve(ref: str, workspace: Path, *, env: Mapping[str, str] | None = None) -> str | None:
    """What the adapter factory sends: the key, ``""`` for a keyless-capable provider
    with no key (local servers, custom URLs), or None when a required key is missing."""
    key = get(ref, workspace, env=env)
    if key is not None:
        return key
    return None if key_required(ref) else ""


def set_key(name: str, key: str, workspace: Path) -> None:
    """Store a key (replacing any). The key must be one printable token, no spaces."""
    _check_name(name)
    value = key.strip() if isinstance(key, str) else ""
    if not value or len(value) > _MAX_KEY or any(ch.isspace() or not ch.isprintable()
                                                 for ch in value):
        raise CredentialError("invalid_key")
    data = _read(workspace)
    data["keys"][name] = value
    _write(workspace, data)


def remove(name: str, workspace: Path) -> bool:
    """Forget the stored key and URL for ``name``; True when something was removed."""
    _check_name(name)
    data = _read(workspace)
    removed = data["keys"].pop(name, None) is not None
    removed = data["base_urls"].pop(name, None) is not None or removed
    if removed:
        _write(workspace, data)
    return removed


def names(workspace: Path) -> tuple[str, ...]:
    """Credential names stored in the file (never their values)."""
    return tuple(sorted(_read(workspace)["keys"]))


def set_base_url(name: str, url: str, workspace: Path) -> None:
    _check_name(name)
    value = url.strip().rstrip("/") if isinstance(url, str) else ""
    if not re.fullmatch(r"https?://[^\s]+", value) or len(value) > 2048:
        raise CredentialError("invalid_url")
    data = _read(workspace)
    data["base_urls"][name] = value
    _write(workspace, data)


def get_base_url(name: str, workspace: Path) -> str | None:
    return _read(workspace)["base_urls"].get(name)
