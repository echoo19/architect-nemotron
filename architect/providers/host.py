"""The host seams the provider modules touch, injectable for offline tests.

``Host`` bundles PATH lookup, the environment, the home directory, the HTTP
transport (None: real network) and the foreground runner used for official CLI
logins. Tests pass a ``Host`` with an ``httpx.MockTransport`` and fakes; nothing
in ``architect.providers`` reaches the network, a CLI or a local server otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

__all__ = ["Host", "run_foreground"]


def run_foreground(argv: Sequence[str]) -> int:
    """Run an official login command with this terminal (stdin, stdout, stderr inherited).

    The caller makes sure no prompt or key listener owns the terminal meanwhile; a
    missing executable or OS failure is exit code 127, never an exception.
    """
    try:
        return subprocess.run(list(argv), check=False).returncode
    except (OSError, ValueError):
        return 127


@dataclass(frozen=True)
class Host:
    which: Callable[[str], str | None] = shutil.which
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    transport: httpx.AsyncBaseTransport | None = None
    runner: Callable[[Sequence[str]], int] = run_foreground
