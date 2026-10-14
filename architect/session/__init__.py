"""Interactive session package (``architect`` with no arguments).

``main`` is the entry ``cli.py`` pre-dispatches to for ``architect`` and
``architect session ...``. It parses the four session flags, picks a console
(``StdConsole`` or a ``--script`` file) and runs ``Session`` on one asyncio loop.
Its exception mapping is the same as ``cli.main``: every failure leaves the
process as a fixed JSON code on stderr, never as text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from architect.cli import CLIError
from architect.profiles import ProfileError

from .codes import SessionError, code_for
from .commands import HELP
from .io import Console, ScriptedConsole, StdConsole
from .session import Session
from .state import State

__all__ = ["main", "Session", "SessionError", "ScriptedConsole"]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CLIError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="architect session",
        description="Interactive session: describe a goal, review the Spec, approve, compile, "
        "install and run.",
        epilog=HELP[State.READY],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", type=Path, help="workspace directory (default .architect)")
    parser.add_argument("--profile", type=Path, help="profile file (default: discovered)")
    parser.add_argument("--script", type=Path, help="read commands from this file")
    parser.add_argument("--no-probe", action="store_true", help="skip the first-run login probe")
    return parser


def _error(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 1


class _EchoingConsole(ScriptedConsole):
    """The ``--script`` driver: a scripted console whose output also reaches stdout."""

    def __init__(self, lines: Sequence[str]) -> None:
        super().__init__(lines)
        self._stdout = StdConsole(sys.stdin, sys.stdout)

    def write(self, text: str) -> None:
        super().write(text)
        self._stdout.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Return an exit code; the session closes its services on every exit."""
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        console: Console
        if args.script is not None:
            try:
                lines = Path(args.script).read_text(encoding="utf-8").splitlines()
            except OSError:
                raise CLIError("invalid_arguments") from None
            console = _EchoingConsole(lines)
        else:
            console = StdConsole(sys.stdin, sys.stdout)
        session = Session(
            console,
            cwd=Path.cwd(),
            workspace=args.workspace,
            profile_path=args.profile,
            probe=not args.no_probe,
        )
        return asyncio.run(session.run())
    except SystemExit as exc:
        return int(exc.code or 0)  # argparse --help remains a normal CLI operation.
    except (CLIError, ProfileError, SessionError) as exc:
        return _error(code_for(exc))
    except KeyboardInterrupt:
        _error("interrupted")
        return 130
    except Exception:
        # Provider, fixture and filesystem exceptions can contain user secrets.
        return _error("command_failed")
