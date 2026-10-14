"""Shared fixtures for the Phase 10 routing tests (WP1): specs, signed artifacts, fakes.

Everything here is local and deterministic: artifacts are signed with the
per-test throwaway key from ``conftest.py`` and no adapter ever calls a model.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest

from architect.compiler.verification import _signed
from architect.routing.evidence import InstalledRef, TemplateRef, VersionEvidence
from architect.routing.labels import EvidenceClass, EvidenceLabel
from architect.routing.signature import TaskSignature, signature
from architect.spec import Budget, FloorDefinition, FloorKind, HarnessArtifact, Lifecycle, Spec
from architect.spec import TestReport as Report

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every non-loopback connection (the event loop's self-pipe stays usable)."""

    def guarded(original: Any) -> Any:
        def connect(self: socket.socket, address: Any, *args: Any) -> Any:
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise AssertionError("network access in a hermetic test")
            return original(self, address, *args)

        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(socket.socket.connect_ex))


def make_spec(
    task: str = "Classify the support ticket priority",
    *,
    lifecycle: Lifecycle = Lifecycle.INSTALL,
    model: str = "fake",
    domain: str = "general",
    kind: FloorKind = FloorKind.HELD_OUT_TASKS,
    limit: float = 0.5,
    output: dict[str, Any] | None = None,
    budget: Budget | None = None,
    tools: tuple[str, ...] = (),
) -> Spec:
    return Spec(
        task=task,
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema=output or {"type": "string"},
        floor=FloorDefinition(kind=kind, dataset="heldout", limit=limit),
        budget=budget or Budget(cost_usd=0.1, compute_s=10),
        model_target=model,
        lifecycle=lifecycle,
        domain=domain,
        tools=tools,
    )


class Maker:
    """Signed, licensed artifacts in fresh directories under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.count = 0

    def __call__(
        self,
        spec: Spec | None = None,
        *,
        score: float = 0.8,
        cost: float = 0.04,
        mean: float | None = 0.03,
        compute: float = 1.0,
        held_out: int = 30,
        model: str | None = None,
        version: int = 1,
        code: str = "return task",
    ) -> HarnessArtifact:
        self.count += 1
        spec = spec or make_spec()
        directory = self.root / f"a{self.count}"
        directory.mkdir(parents=True)
        (directory / "harness.py").write_text(
            f"async def run(context, task):\n    # {self.count}\n    {code}\n", encoding="utf-8"
        )
        artifact = HarnessArtifact(
            spec=spec, version=version, directory=directory, preset="synthesizer",
            model=model or spec.model_target, components=("a", "b"),
        )
        report = _signed(
            Report(
                spec_hash=spec.spec_hash, artifact_digest=artifact.artifact_digest, passed=True,
                floor_score=score, floor_limit=spec.floor.limit, floor_kind=spec.floor.kind,
                tokens=10, cost_usd=cost, mean_cost_usd=mean, compute_s=compute,
                component_count=artifact.component_count, verification_id=uuid4().hex,
                held_out_count=held_out,
            )
        )
        artifact = artifact.model_copy(update={"report": report})
        artifact.save()
        return artifact


# ------------------------------------------------------------ fake evidence


def _digest(label: str) -> str:
    return (label.encode().hex() * 64)[:64]


def installed_ev(
    spec: Spec,
    *,
    name: str = "task",
    revision: int = 1,
    model: str = "m1",
    m: float = 0.3,
    n: int = 30,
    sec_lcb: float = 0.0,
    h: float = 0.5,
    cost: float = 0.01,
    report_cost: float | None = None,
    compute: float = 1.0,
    latency: float = 1.0,
    k: int = 2,
    licensed: bool | None = True,
    consumed: bool = False,
    drift: Literal["no_window", "stable", "drifting"] | None = "no_window",
    cls: EvidenceClass | None = "live_billed",
    digest: str | None = None,
) -> VersionEvidence:
    labels: tuple[EvidenceLabel, ...] = ("stage4_signed",) + ((cls,) if cls else ())
    return VersionEvidence(
        origin="installed",
        ref=InstalledRef(
            name=name, revision=revision, artifact_digest=digest or _digest(f"{name}{revision}"),
            spec_hash=spec.spec_hash,
        ),
        model=model, spec=spec, signature=signature(spec), licensed=licensed,
        consumed=consumed, drift=drift, evidence_class=cls, m=m,
        m_lcb=m - 0.5 / max(n, 1) ** 0.5, sec_lcb=sec_lcb, h=h, cost_usd=cost,
        compute_s=compute, report_cost_usd=cost if report_cost is None else report_cost,
        latency_s=latency, latency_basis="bound", k=k, held_out_count=n,
        evidence=labels + ("latency_bound", "drift_window_none"),
    )


def template_ev(
    spec: Spec,
    *,
    key: str,
    model: str = "m1",
    s: int = 0,
    f: int = 0,
    sequence: int = 1,
    m: float = 0.3,
    n: int = 30,
    cost: float = 0.01,
    k: int = 2,
    evicted: bool = False,
    licensed: bool | None = True,
    promoted: bool = False,
    cls: EvidenceClass | None = "live_billed",
) -> VersionEvidence:
    from architect.routing.evidence import wilson

    labels: tuple[EvidenceLabel, ...] = ("stage4_signed",) + ((cls,) if cls else ())
    return VersionEvidence(
        origin="template",
        ref=TemplateRef(key=key, artifact_digest=key, model=model),
        model=model, spec=spec, signature=signature(spec), licensed=licensed, evicted=evicted,
        evidence_class=cls, m=m, m_lcb=m - 0.5 / max(n, 1) ** 0.5, sec_lcb=0.0, h=0.5,
        cost_usd=cost, compute_s=1.0, report_cost_usd=cost, latency_s=1.0,
        latency_basis="bound", k=k, held_out_count=n, successes=s, failures=f,
        promoted=promoted, sequence=sequence, r=wilson(s, s + f, 1.0), evidence=labels,
    )


def key(label: str) -> str:
    return _digest(label)


class FakeIndex:
    """An in-memory ``EvidenceIndex`` stand-in: what ``route()`` reads, nothing more."""

    def __init__(
        self,
        installed: tuple[VersionEvidence, ...] = (),
        templates: tuple[VersionEvidence, ...] = (),
        *,
        classes: dict[str, EvidenceClass] | None = None,
        history: tuple[bool, int, int] = (False, 0, 0),
        earlier: dict[str, tuple[VersionEvidence, ...]] | None = None,
    ) -> None:
        self._installed = installed
        self._templates = templates
        default: dict[str, EvidenceClass] = {"m1": "live_billed", "fake": "synthetic_fake"}
        self.classes = classes if classes is not None else default
        self.history = history
        self.earlier = earlier or {}

    def evidence_class(self, model: str) -> EvidenceClass | None:
        return self.classes.get(model)

    def installed(self, sig: TaskSignature) -> tuple[VersionEvidence, ...]:
        return self._installed

    def templates(
        self, sig: TaskSignature, models: tuple[str, ...], limit: int
    ) -> tuple[VersionEvidence, ...]:
        return tuple(item for item in self._templates if item.model in models)

    def stage4_history(self, sig: TaskSignature) -> tuple[bool, int, int]:
        return self.history

    def earlier_revisions(self, name: str, current: int) -> tuple[VersionEvidence, ...]:
        return self.earlier.get(name, ())
