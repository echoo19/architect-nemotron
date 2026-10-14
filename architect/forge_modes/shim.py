"""The thin loader every exported package imports (Phase 10 Section 12.4).

``ExportedHarness`` re-verifies the package at every load: file hashes against the
manifest, the pinned digests, the bound children and the Stage 4 license. Any failed
check raises ``ExportError`` with a fixed code and nothing runs.

The license is checked with the HMAC key of the host that signed it or (W7) with a key
on this host's trust list when the report carries an Ed25519 seal, so a sealed package
runs on a clean host that holds only the public key. There is no digest-pinned run
mode: when neither is available the license is ``license_unverifiable`` (or
``key_untrusted`` for a seal from an unlisted key) and ``load`` refuses. In a sealed
package a changed byte is ``artifact_changed``. ``Runtime.run`` then re-checks the
license and the model once more. Installed (INSTALL) artifacts are never consumed by a
run; delivery telemetry lands in the run's workspace (the one given, else
``ARCHITECT_WORKSPACE``, else the profile's own).

Every check here, the one in ``Runtime.run`` included, uses configured keys only
(``trust.configured_only``): the HMAC key ``ARCHITECT_SIGNING_KEY_FILE`` names and the
trust list it or ``ARCHITECT_TRUST_FILE`` names, else the shipped key alone. A key or a
trust list in ``./.architect`` of whatever folder the process runs in never counts on
its own, so one that arrives with a downloaded repository cannot vouch for its code.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from architect import trust
from architect.spec import HarnessArtifact

from .errors import ExportError
from .export import ExportCheck, check_package, stop_error

if TYPE_CHECKING:
    from architect.models import ModelAdapter
    from architect.profiles import Profile
    from architect.runtime import Runtime

__all__ = ["ExportError", "ExportedHarness", "FeatureDisabled"]

# The variable that moves the workspace of the CLI and the desktop app (agent.app).
WORKSPACE_ENV = "ARCHITECT_WORKSPACE"


class FeatureDisabled(RuntimeError):
    """The package's feature flag is not set to ``"1"``; nothing was loaded or run."""


class ExportedHarness:
    def __init__(self, root: Path, pin: Mapping[str, JsonValue]) -> None:
        self.root = Path(root)
        # A private JSON copy: later edits to the caller's mapping change nothing.
        self.pin: dict[str, JsonValue] = json.loads(json.dumps(dict(pin), allow_nan=False))

    def verify(self) -> ExportCheck:
        """Integrity, pins, children and license (host key or trusted seal); never raises."""
        return check_package(self.root, self.pin, registry=None)

    def load(self) -> HarnessArtifact:
        """``verify()``, then the pinned artifact; any failed check raises ``ExportError``.

        The error's ``str()`` is the check's code. Its ``sentence`` and ``reason_line`` say
        the same in plain words ("Stopped: changed after its exam. 0 model calls.", then
        the files that changed), for the app to show. Nothing ran and no model was called.
        """
        with trust.configured_only():
            check = self.verify()
            if check.codes:
                raise stop_error(self.root, check.codes[0], self.pin)
            try:
                artifact = HarnessArtifact.load(self.root / "artifact")
            except (OSError, ValueError, TypeError):
                raise stop_error(self.root, "export_integrity_mismatch") from None
            if (
                artifact.artifact_digest != self.pin.get("artifact_digest")
                or artifact.spec.spec_hash != self.pin.get("spec_hash")
                or not artifact.licensed
            ):
                raise stop_error(self.root, "export_pin_mismatch", self.pin)
            return artifact

    async def run(
        self, value: JsonValue, *, profile: Path, workspace: Path | None = None
    ) -> JsonValue:
        """Load, then one delivery through the services the profile configures.

        The delivery's workspace is ``workspace``, else ``ARCHITECT_WORKSPACE``, else the
        profile's own. A profile that names ``~/.architect`` also reads its stored key and
        relay address there (``profiles.in_workspace``), as the CLI and the desktop app
        do, so a judge code stored with ``ARCHITECT_WORKSPACE`` set is the one it uses."""
        artifact = self.load()
        from architect.profiles import build_services, in_workspace, load_profile

        loaded = load_profile(Path(profile))
        chosen = _workspace(loaded, workspace)
        services = build_services(in_workspace(loaded, chosen), chosen)
        try:
            with trust.configured_only():  # Runtime.run re-checks under the same rule
                result = await services.runtime.run(
                    artifact, value, services.adapter_factory(artifact.model)
                )
        finally:
            services.close()
        return result.output

    async def run_with(
        self, runtime: Runtime, adapter: ModelAdapter, value: JsonValue
    ) -> JsonValue:
        """Load, then one delivery through the caller's runtime and adapter."""
        artifact = self.load()
        with trust.configured_only():  # Runtime.run re-checks under the same rule
            result = await runtime.run(artifact, value, adapter)
        return result.output


def _workspace(profile: Profile, explicit: Path | None) -> Path:
    """``explicit`` (relative to the profile file), else ``ARCHITECT_WORKSPACE``
    (relative to the current folder, as ``agent.app.agent_workspace`` reads it), else the
    workspace the profile names."""
    if explicit is not None:
        return profile.resolve(Path(explicit))
    configured = os.environ.get(WORKSPACE_ENV)
    if configured:
        return (Path.cwd() / Path(configured)).resolve()
    return profile.resolve(profile.workspace)
