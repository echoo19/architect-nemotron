"""Forge delivery modes (Phase 10 Section 12): ``forge run`` and ``forge integrate``.

``forge run`` builds an ephemeral harness, lets Stage 4 license it, runs it once and
expires it. ``forge integrate`` builds, licenses and installs a harness, exports it as
a self-contained package and places it in a repository only behind the four P9 gates.
Forge modes never license anything themselves: Stage 4 remains the only authority.
"""

from __future__ import annotations

from .cases import cases_floor, load_cases
from .errors import ExportError, ForgeModeError
from .export import (
    ExportCheck,
    ExportManifest,
    export_package,
    place_package,
    verify_export,
)
from .integrate import IntegrateOptions, IntegrateResult, forge_integrate
from .place_tool import place_package_tool
from .run import ForgeDelivery, ForgeRunResult, deliverable_ephemeral, forge_run
from .shim import ExportedHarness, FeatureDisabled
from .words import exam_line

__all__ = [
    "ExportCheck",
    "ExportError",
    "ExportManifest",
    "ExportedHarness",
    "FeatureDisabled",
    "ForgeDelivery",
    "ForgeModeError",
    "ForgeRunResult",
    "IntegrateOptions",
    "IntegrateResult",
    "cases_floor",
    "deliverable_ephemeral",
    "exam_line",
    "export_package",
    "forge_integrate",
    "forge_run",
    "load_cases",
    "place_package",
    "place_package_tool",
    "verify_export",
]
