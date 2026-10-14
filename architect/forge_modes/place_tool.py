"""The single-use trusted Pi tool that places an export package (Phase 10 Section 12.5).

Offered only to the ``forge integrate`` builder run, after the four gates of P9
(spec approval, Forge spend consent, a Stage 4 license, export consent). Pi never
sees the staging directory or the package bytes: the host copies the already staged
package into exactly ``<into>/<package>`` and Pi receives only the relative path, a
file count and the manifest digest. ``requires_build=False`` follows the
``compile_agent`` gate, so the tool is never offered while ``context.is_evaluation``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import JsonValue

from architect.backends.pi import TrustedTool

from .errors import ExportError
from .export import place_package, read_manifest

__all__ = ["PLACE_TOOL", "place_package_tool"]

PLACE_TOOL = "place_harness_package"
_PARAMETERS: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"into": {"type": "string", "maxLength": 512}},
    "required": ["into"],
    "additionalProperties": False,
}
_DESCRIPTION = (
    "Place the verified, host-staged harness package into the repository. Call exactly "
    "once, only after compile_agent succeeded, with into equal to the operator's "
    "directory. The host copies the package; you receive its relative path, file count "
    "and manifest digest. The feature stays behind its disabled flag for human review."
)


def place_package_tool(
    staged: Path,
    repo: Path,
    into: str,
    *,
    ready: Callable[[], bool],
    active: Callable[[], bool],
    denied: Iterable[Path] = (),
) -> TrustedTool:
    staged_dir = Path(staged)
    repo_dir = Path(repo)
    protected = tuple(Path(path) for path in denied)
    placed = False

    async def callback(arguments: dict[str, JsonValue]) -> JsonValue:
        nonlocal placed
        if not active():
            return {"error": "factory_capability_expired"}
        if not ready():
            return {"error": "integrate_not_ready"}
        if arguments.get("into") != into:
            return {"error": "export_path_invalid"}
        if placed:
            return {"error": "factory_call_limit"}
        placed = True
        try:
            manifest = await asyncio.to_thread(
                place_package, staged_dir, repo_dir, into, denied=protected
            )
            _, manifest_sha256 = await asyncio.to_thread(read_manifest, staged_dir)
        except ExportError as exc:
            return {"error": exc.code}
        return {
            "path": f"{Path(into).as_posix()}/{manifest.package}",
            "files": len(manifest.files) + 1,
            "manifest_sha256": manifest_sha256,
        }

    return TrustedTool(
        name="place_harness_package",
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        callback=callback,
        max_calls=1,
        requires_build=False,
    )
