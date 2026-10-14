"""Generated code removal after an ephemeral delivery (Phase 10 Section 3).

Moved unchanged from ``orchestrator/__init__.py`` (``_EPHEMERAL_CODE`` and
``_discard_code``) so the orchestrator and ``forge run`` share one rule. Only the
listed generated files are removed, and only under the artifact root; the report,
spec, config, ``forge.json`` and ``subharnesses/`` stay as evidence. Anything
outside the root (an installed registry entry, a library snapshot) is untouched.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["EPHEMERAL_CODE", "discard_code"]

# Only these generated files are removed after an ephemeral delivery.
EPHEMERAL_CODE = ("harness.py", "pi-pack.json", "preset.json", "rewrite.diff")


def discard_code(root: Path, directory: Path) -> None:
    """Remove generated ephemeral code under the artifact root, keeping evidence."""
    try:
        base = root.resolve()
        target = directory.resolve()
        if directory.is_symlink() or not target.is_relative_to(base) or target == base:
            return
        for name in EPHEMERAL_CODE:
            path = target / name
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
    except OSError:
        pass
