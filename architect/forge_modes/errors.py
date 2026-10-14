"""Fixed-code errors of the Forge delivery modes (Phase 10 Section 12).

``str(error)`` is always one closed code from Section 13.4 (or an existing Phase 9
code such as ``forge_cancelled``), never file content, a path or model text.

A package that refuses to load also says so in plain words: ``ExportError.sentence`` and
``reason_line`` are what an app shows its user, and ``changed_files`` names the files.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

__all__ = [
    "CANNOT_CHECK",
    "RELOADED",
    "STOPPED",
    "UNDO_ONLY",
    "ExportError",
    "ForgeModeError",
    "StopReason",
    "reason_line",
]

# The one stop sentence, the same in the app, the terminal and Architect.
STOPPED = "Stopped: changed after its exam. 0 model calls."
# Nothing changed, but no key here can check the report: "changed" would be false.
CANNOT_CHECK = "Stopped: its exam report cannot be checked on this computer. 0 model calls."
# Nothing is wrong with the folder: it was written again (a retake that passed, or an
# undo) while the app kept running the version it had loaded.
RELOADED = "Stopped: this app loaded an earlier version of its agent. 0 model calls."
UNDO_ONLY = "A change to this file can only be undone."
_UNDO_ONLY_MANY = "A change to these files can only be undone."

# ``changed``: only the agent's own editable files differ, so a retake may be offered.
# ``changed_undo_only``: another file differs. ``cannot_check``: no file differs.
# ``reloaded``: no file differs and the folder passes its own checks; the caller holds
# the pin of the version it imported before the folder was written again.
StopReason = Literal["changed", "changed_undo_only", "cannot_check", "reloaded"]


def reason_line(
    reason: StopReason, changed_files: tuple[str, ...], details: Mapping[str, str] | None = None
) -> str:
    """The line under the stop sentence: ``Changed: artifact/config.json (model).``

    ``details`` says what changed inside a file, by file name, when the caller knows."""
    if reason == "cannot_check":
        return "No file changed. The key that signed its exam report is not set up here."
    if reason == "reloaded":
        return "The version in its folder passed its exam. Restart the app to load it."
    details = details or {}
    names = ", ".join(
        f"{name} ({details[name]})" if details.get(name) else name for name in changed_files
    )
    line = f"Changed: {names or 'its files'}."
    if reason == "changed_undo_only":
        line += " " + (UNDO_ONLY if len(changed_files) <= 1 else _UNDO_ONLY_MANY)
    return line


class ForgeModeError(ValueError):
    """``forge run`` / ``forge integrate`` refused before or instead of a result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExportError(ForgeModeError):
    """An export package could not be written, placed or verified.

    ``str()`` stays the bare code. When a package refuses to load, ``reason`` says which
    kind of stop it is, ``changed_files`` lists the files that differ from the ones that
    passed (package-relative names), and ``sentence`` and ``reason_line`` are the plain
    text to show. All four are empty for the other refusals. ``aside`` is set only when
    replacing a package failed half way: the folder it left the old package in."""

    def __init__(
        self,
        code: str,
        *,
        changed_files: tuple[str, ...] = (),
        reason: StopReason | None = None,
        aside: Path | None = None,
    ) -> None:
        super().__init__(code)
        self.changed_files = tuple(changed_files)
        self.reason = reason
        self.aside = aside

    @property
    def sentence(self) -> str | None:
        if self.reason is None:
            return None
        if self.reason == "reloaded":
            return RELOADED
        return CANNOT_CHECK if self.reason == "cannot_check" else STOPPED

    @property
    def reason_line(self) -> str | None:
        if self.reason is None:
            return None
        return reason_line(self.reason, self.changed_files)
