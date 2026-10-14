"""HTTP service gates over real fake-profile Compiler, Stage 4, Registry and Runtime."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from architect import server
from architect.models import ModelAdapter
from architect.profiles import ProfileError, Services, build_services, load_profile
from architect.runtime import (
    ArtifactExpiredError,
    BudgetExceeded,
    RunResult,
    UnlicensedArtifactError,
)
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 16 + "-local-fixture-bearer-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SENTINEL = "PRIVATE_SENTINEL_7f3a"


def make_spec(*, install: bool = False, task: str = "Return ok") -> Spec:
    return Spec(
        task=task,
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="tasks"),
        budget=Budget(),
        lifecycle=Lifecycle.INSTALL if install else Lifecycle.EXPIRE,
    )


def write_responses(tmp_path: Path, cost: float) -> None:
    (tmp_path / "responses.json").write_text(
        json.dumps(
            [
                {"text": "ok", "tokens_in": 8, "tokens_out": 12, "cost_usd": cost, "model": "fake"}
                for _ in range(40)
            ]
        ),
        encoding="utf-8",
    )


def setup_profile(tmp_path: Path, *, inferred: Spec | None = None) -> Path:
    (tmp_path / "cases.json").write_text(
        json.dumps([{"case_id": str(i), "task": {}, "expected": "ok"} for i in range(6)]),
        encoding="utf-8",
    )
    write_responses(tmp_path, 0.00002)
    (tmp_path / "inference.json").write_text(
        json.dumps(
            [
                {
                    "text": (inferred or make_spec(install=True)).model_dump_json(),
                    "tokens_in": 8,
                    "tokens_out": 12,
                    "cost_usd": 0.00002,
                    "model": "fake",
                }
            ]
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "workspace": "work",
                "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
                "inference": {"provider": "fake", "model": "fake", "fixture": "inference.json"},
                "floors": {"tasks": {"kind": "held_out_tasks", "path": "cases.json"}},
                "compile": {"max_candidates": 1, "enable_rewriting": False},
            }
        ),
        encoding="utf-8",
    )
    return profile


@pytest.fixture
def services(tmp_path: Path) -> Iterator[Services]:
    profile = load_profile(setup_profile(tmp_path))
    built = build_services(profile, profile.workspace)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(server.create_app(services, api_token=TOKEN)) as test_client:
        yield test_client


def post(client: TestClient, path: str, payload: object) -> httpx.Response:
    response: httpx.Response = client.post(
        path, content=json.dumps(payload), headers={**AUTH, "Content-Type": "application/json"}
    )
    return response


def compile_spec(client: TestClient, spec: Spec, *, approved: bool = False) -> dict[str, object]:
    response = post(
        client, "/compile", {"spec": spec.model_dump(mode="json"), "approved": approved}
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def run(client: TestClient, artifact_id: object, **flags: bool) -> httpx.Response:
    return post(client, "/run", {"artifact_id": artifact_id, "input": {}, **flags})


def assert_private(response: httpx.Response, services: Services) -> None:
    text = response.text
    for forbidden in (
        str(services.workspace),
        services.workspace.as_posix(),
        "signature",
        "directory",
        "report",
        "profile",
        'license"',
        SENTINEL,
    ):
        assert forbidden not in text


def catalog(services: Services) -> sqlite3.Connection:
    return sqlite3.connect(services.workspace / "server" / "catalog.sqlite3")


# ------------------------------------------------------------------ construction


@pytest.mark.parametrize("token", ["", "short", "x" * 31, "has space " * 4, "é" * 40])
def test_create_app_rejects_weak_tokens(services: Services, token: str) -> None:
    with pytest.raises(ValueError, match="invalid_api_token"):
        server.create_app(services, api_token=token)


def test_only_five_operations_and_no_docs(client: TestClient) -> None:
    # Phase 10 amended phase6-http.md from four operations to five (POST /route).
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/registry"):
        assert client.get(path).json() == {"error": "unauthorized"}
        authed = client.get(path, headers=AUTH)
        assert authed.status_code == 404 and authed.json() == {"error": "not_found"}
    wrong_method = client.get("/compile", headers=AUTH)
    assert wrong_method.status_code == 405
    assert wrong_method.json() == {"error": "method_not_allowed"}
    routes = {getattr(route, "path", None) for route in client.app.routes}  # type: ignore[attr-defined]
    assert routes == {"/compile", "/run", "/status/{artifact_id}", "/recompile", "/route"}


def test_app_lifespan_leaves_caller_services_open_and_unchanged(services: Services) -> None:
    before = (services.profile, services.workspace, services.compiler, services.registry)
    with TestClient(server.create_app(services, api_token=TOKEN)) as test_client:
        assert test_client.get("/status/" + "a" * 20, headers=AUTH).status_code == 404
    assert (services.profile, services.workspace, services.compiler, services.registry) == before
    assert not services.runtime.store._closed
    assert services.registry.list() == ()


# ------------------------------------------------------------------ authentication


async def test_authentication_precedes_body_reading(services: Services) -> None:
    app = server.create_app(services, api_token=TOKEN)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        raise AssertionError("body must not be read before authentication")

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    for headers in (
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", f"bearer {TOKEN}".encode())],
        [
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"authorization", f"Bearer {TOKEN}".encode()),
        ],
    ):
        sent.clear()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/compile",
            "raw_path": b"/compile",
            "query_string": b"",
            "root_path": "",
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 1),
            "headers": [(b"content-type", b"application/json"), *headers],
        }
        await app(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] == 401
        assert json.loads(sent[1]["body"]) == {"error": "unauthorized"}  # type: ignore[arg-type]
    for length in (b"abc", b"-1", b"9" * 13):
        sent.clear()
        scope["headers"] = [
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"content-length", length),
        ]
        await app(scope, receive, send)  # type: ignore[arg-type]
        assert sent[0]["status"] in (400, 413)


def test_unauthenticated_invalid_body_is_401_not_parsed(client: TestClient) -> None:
    response = client.post(
        "/compile",
        content=b"{not json" + SENTINEL.encode(),
        headers={"Authorization": "Bearer " + "x" * 48, "Content-Type": "application/json"},
    )
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert response.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ input bounds


def test_body_size_is_bounded(client: TestClient) -> None:
    big = b'{"request":"' + b"a" * (server.MAX_BODY_BYTES + 10) + b'"}'
    declared = client.post(
        "/compile", content=big, headers={**AUTH, "Content-Type": "application/json"}
    )
    assert declared.status_code == 413 and declared.json() == {"error": "request_too_large"}

    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b" " * (server.MAX_BODY_BYTES // 10)

    streamed = client.post(
        "/run", content=chunks(), headers={**AUTH, "Content-Type": "application/json"}
    )
    assert streamed.status_code == 413 and streamed.json() == {"error": "request_too_large"}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"[]",
        b"{not json",
        b'{"request": "x", "request": "y"}',
        b'{"request": NaN}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {"x": Infinity}}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {"x": -Infinity}}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": 1e400}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": ' + b"[" * 100 + b"]" * 100 + b"}",
        b'{"artifact_id": "../../etc", "input": {}}',
        b'{"artifact_id": "' + b"a" * 65 + b'", "input": {}}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {}, "enabled": "true"}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {}, "enabled": 1}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {}, "license": "x"}',
        b'{"artifact_id": "' + b"a" * 20 + b'", "input": {}, "path": "C:/x"}',
        b'{"artifact_id": "' + b"a" * 20 + b'"}',
        b"\xff\xfe",
    ],
)
def test_malformed_run_bodies_are_fixed_errors(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/run", content=body, headers={**AUTH, "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_media_type_and_status_identifier_bounds(client: TestClient) -> None:
    wrong = client.post("/run", content=b"{}", headers={**AUTH, "Content-Type": "text/plain"})
    assert wrong.status_code == 415 and wrong.json() == {"error": "unsupported_media_type"}
    for identifier in ("short", "a" * 65, "a" * 20 + "%2E%2E", "a" * 20 + "."):
        response = client.get("/status/" + identifier, headers=AUTH)
        assert response.status_code in (400, 404)
        assert response.json() in ({"error": "invalid_request"}, {"error": "not_found"})
    unknown = client.get("/status/" + "a" * 20, headers=AUTH)
    assert unknown.status_code == 404 and unknown.json() == {"error": "unknown_artifact"}
    assert run(client, "a" * 20).json() == {"error": "unknown_artifact"}
    rec = post(client, "/recompile", {"artifact_id": "a" * 20})
    assert rec.status_code == 404 and rec.json() == {"error": "unknown_artifact"}
    extra = post(client, "/recompile", {"artifact_id": "a" * 20, "spec": {}})
    assert extra.json() == {"error": "invalid_request"}


def test_compile_source_and_forged_authority_rules(client: TestClient) -> None:
    spec = make_spec(install=True).model_dump(mode="json")
    cases: list[tuple[object, str]] = [
        ({}, "exactly_one_source"),
        ({"request": "x", "spec": spec}, "exactly_one_source"),
        ({"request": "x", "approved": True}, "approval_requires_spec"),
        ({"spec": spec, "approved": "true"}, "invalid_request"),
        ({"spec": spec, "approved": True, "artifact": {"directory": "x"}}, "invalid_request"),
        ({"spec": spec, "report": {"signature": SENTINEL}}, "invalid_request"),
        ({"spec": {**spec, "approved": True}}, "invalid_request"),
        ({"spec": {**spec, "budget": {"cost_usd": "0.1"}}}, "invalid_request"),
        ({"spec": {**spec, "lifecycle": "forever"}}, "invalid_request"),
        ({"request": " "}, "invalid_request"),
        ({"request": "x" * (server.MAX_REQUEST_CHARS + 1)}, "invalid_request"),
        ({"request": SENTINEL, "profile": {"models": {}}}, "invalid_request"),
    ]
    for payload, code in cases:
        response = post(client, "/compile", payload)
        assert response.status_code == 400, payload
        assert response.json() == {"error": code}
        assert SENTINEL not in response.text


# ------------------------------------------------------------------ ephemeral flow


def test_ephemeral_compile_run_expire_and_status(client: TestClient, services: Services) -> None:
    response = post(client, "/compile", {"spec": make_spec().model_dump(mode="json")})
    assert response.status_code == 200
    assert_private(response, services)
    body = response.json()
    assert set(body) == {
        "accepted",
        "approval_required",
        "spec",
        "artifact_id",
        "failures",
        "stages",
        "warm_started",
    }
    assert body["accepted"] and not body["approval_required"] and body["failures"] == []
    assert body["stages"] == [1, 2, 4]
    assert Spec.model_validate(body["spec"]) == make_spec()
    artifact_id = body["artifact_id"]
    assert isinstance(artifact_id, str) and len(artifact_id) >= 16
    assert "/" not in artifact_id and make_spec().spec_hash not in artifact_id

    status = client.get(f"/status/{artifact_id}", headers=AUTH)
    assert status.json() == {
        "artifact_id": artifact_id,
        "licensed": True,
        "expired": False,
        "lifecycle": "expire",
        "spec_hash": make_spec().spec_hash,
        "version": 1,
    }
    first = run(client, artifact_id)
    assert first.status_code == 200, first.text
    assert_private(first, services)
    result = first.json()
    assert set(result) == {"run_id", "output", "tokens_in", "tokens_out", "cost_usd", "wall_time_s"}
    assert result["output"] == "ok" and result["tokens_in"] == 8 and result["tokens_out"] == 12
    # Ephemeral delivery retains single-use semantics even with every flag set.
    again = run(client, artifact_id, enabled=True, human_reviewed=True)
    assert again.status_code == 410 and again.json() == {"error": "artifact_expired"}
    expired = client.get(f"/status/{artifact_id}", headers=AUTH).json()
    assert expired["expired"] is True and expired["licensed"] is False
    rec = post(client, "/recompile", {"artifact_id": artifact_id})
    assert rec.status_code == 409 and rec.json() == {"error": "not_installed"}


def test_failed_compile_returns_no_artifact(
    client: TestClient, services: Services, tmp_path: Path
) -> None:
    (tmp_path / "cases.json").write_text(
        json.dumps([{"case_id": str(i), "task": {}, "expected": "different"} for i in range(6)]),
        encoding="utf-8",
    )
    body = compile_spec(client, make_spec(task="Fails floor"))
    assert body["accepted"] is False and body["artifact_id"] is None
    failures = body["failures"]
    assert isinstance(failures, list) and failures
    assert all(isinstance(code, str) for code in failures)
    assert (
        not (services.workspace / "server" / "catalog.sqlite3").exists()
        or catalog(services).execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    )
    unavailable = compile_spec(
        client,
        make_spec().model_copy(
            update={
                "floor": FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="missing")
            }
        ),
    )
    assert unavailable["failures"] == ["floor_unavailable"]
    assert unavailable["artifact_id"] is None


# ------------------------------------------------------------------ installed flow


def install(client: TestClient) -> str:
    proposal = post(client, "/compile", {"request": "Make a persistent feature"})
    assert proposal.status_code == 200
    body = proposal.json()
    assert body["approval_required"] and not body["accepted"]
    assert body["artifact_id"] is None and body["stages"] == [0]
    reviewed = Spec.model_validate(body["spec"])
    approved = compile_spec(client, reviewed, approved=True)
    assert approved["accepted"] and approved["stages"] == [1, 2, 4]
    assert isinstance(approved["artifact_id"], str)
    return approved["artifact_id"]


def test_install_requires_exact_explicit_spec(client: TestClient, services: Services) -> None:
    spec = make_spec(install=True)
    proposal = compile_spec(client, spec)
    assert proposal == {
        "accepted": False,
        "approval_required": True,
        "spec": spec.model_dump(mode="json"),
        "artifact_id": None,
        "failures": [],
        "stages": [],
        "warm_started": False,
    }
    assert not list(services.workspace.glob("artifacts/**/harness.py"))
    assert services.registry.list() == ()
    inferred = post(client, "/compile", {"request": "please install", "approved": True})
    assert inferred.json() == {"error": "approval_requires_spec"}
    assert services.registry.list() == ()


def test_installed_run_requires_both_flags(client: TestClient, services: Services) -> None:
    artifact_id = install(client)
    entry = services.registry.list()[0]
    assert entry.name == make_spec(install=True).spec_hash
    for flags in (
        {},
        {"enabled": True},
        {"human_reviewed": True},
        {"enabled": False, "human_reviewed": True},
    ):
        denied = run(client, artifact_id, **flags)
        assert denied.status_code == 403 and denied.json() == {"error": "delivery_not_enabled"}
    for _ in range(2):
        allowed = run(client, artifact_id, enabled=True, human_reviewed=True)
        assert allowed.status_code == 200 and allowed.json()["output"] == "ok"
        assert_private(allowed, services)
    status = client.get(f"/status/{artifact_id}", headers=AUTH).json()
    assert status == {
        "artifact_id": artifact_id,
        "licensed": True,
        "expired": False,
        "lifecycle": "install",
        "spec_hash": entry.spec_hash,
        "version": entry.artifact_version,
        "registry_name": entry.name,
    }


def test_recompile_is_strict_and_uses_approved_spec(
    client: TestClient, services: Services, tmp_path: Path
) -> None:
    artifact_id = install(client)
    name = services.registry.list()[0].name
    # Equal quality and cost: fresh Stage 4 runs, but the tied result cannot ship.
    tied = post(client, "/recompile", {"artifact_id": artifact_id})
    assert tied.status_code == 200
    body = tied.json()
    assert body["accepted"] is False and body["artifact_id"] is None
    assert "not_better" in body["failures"] and 4 in body["stages"]
    assert services.registry.status(name).revision == 1
    # Worse (more expensive) is rejected and the prior pointer remains.
    write_responses(tmp_path, 0.00004)
    worse = post(client, "/recompile", {"artifact_id": artifact_id}).json()
    assert worse["accepted"] is False and "not_better" in worse["failures"]
    assert services.registry.status(name).revision == 1
    # Strictly cheaper at equal quality replaces through Registry acceptance.
    write_responses(tmp_path, 0.00001)
    better = post(client, "/recompile", {"artifact_id": artifact_id}).json()
    assert better["accepted"] is True and better["artifact_id"] == artifact_id
    recompiled = Spec.model_validate(better["spec"])
    # Exactly the reviewed proposal, including its provenance, not a re-inferred Spec.
    assert recompiled.spec_hash == make_spec(install=True).spec_hash
    assert recompiled.provenance and set(recompiled.provenance.values()) == {"inferred"}
    assert services.registry.status(name).revision == 2
    status = client.get(f"/status/{artifact_id}", headers=AUTH).json()
    assert status["licensed"] and status["version"] == services.registry.get(name).version
    ok = run(client, artifact_id, enabled=True, human_reviewed=True)
    assert ok.status_code == 200 and ok.json()["cost_usd"] == pytest.approx(0.00001)


def test_repeat_approved_install_cannot_replace_with_tie(
    client: TestClient, services: Services
) -> None:
    artifact_id = install(client)
    again = compile_spec(client, make_spec(install=True), approved=True)
    assert again["accepted"] is False and "not_better" in again["failures"]  # type: ignore[operator]
    # Same hash, different provenance: not the exact approved Spec, so no ID.
    assert again["artifact_id"] is None
    # A retried approval of the exact reviewed Spec learns its stable ID.
    with catalog(services) as db:
        (approved,) = db.execute(
            "SELECT approved_spec FROM artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
    retry = compile_spec(client, Spec.model_validate_json(approved), approved=True)
    assert retry["accepted"] is False and retry["failures"] == ["not_better"]
    assert retry["artifact_id"] == artifact_id
    assert services.registry.status(make_spec(install=True).spec_hash).revision == 1


# ------------------------------------------------------------------ persistence and tampering


def test_restart_preserves_ids_and_expiry(tmp_path: Path) -> None:
    profile_path = setup_profile(tmp_path)
    profile = load_profile(profile_path)
    first = build_services(profile, profile.workspace)
    try:
        with TestClient(server.create_app(first, api_token=TOKEN)) as client:
            installed = install(client)
            ephemeral = compile_spec(client, make_spec())["artifact_id"]
            assert run(client, ephemeral).status_code == 200
    finally:
        first.close()
    mode = (first.workspace / "server").stat().st_mode
    assert os.name == "nt" or mode & 0o077 == 0
    second = build_services(load_profile(profile_path), profile.workspace)
    try:
        with TestClient(server.create_app(second, api_token=TOKEN)) as client:
            assert client.get(f"/status/{installed}", headers=AUTH).json()["licensed"] is True
            ran = run(client, installed, enabled=True, human_reviewed=True)
            assert ran.status_code == 200 and ran.json()["output"] == "ok"
            status = client.get(f"/status/{ephemeral}", headers=AUTH).json()
            assert status["expired"] is True and status["licensed"] is False
            assert run(client, ephemeral).json() == {"error": "artifact_expired"}
    finally:
        second.close()


def test_tampered_installed_artifact_is_not_trusted(client: TestClient, services: Services) -> None:
    artifact_id = install(client)
    directory = services.registry.list()[0].directory
    harness = directory / "harness.py"
    harness.write_text(harness.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    denied = run(client, artifact_id, enabled=True, human_reviewed=True)
    assert denied.status_code == 403 and denied.json() == {"error": "unlicensed_artifact"}
    assert client.get(f"/status/{artifact_id}", headers=AUTH).json()["licensed"] is False
    rec = post(client, "/recompile", {"artifact_id": artifact_id})
    assert rec.status_code == 403 and rec.json() == {"error": "unlicensed_artifact"}


def test_tampered_ephemeral_report_is_not_trusted(client: TestClient, services: Services) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    location = catalog(services).execute("SELECT location FROM artifacts").fetchone()[0]
    report = services.compiler.artifact_root / location / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["floor_score"] = 0.5
    report.write_text(json.dumps(payload), encoding="utf-8")
    assert run(client, artifact_id).json() == {"error": "unlicensed_artifact"}
    status = client.get(f"/status/{artifact_id}", headers=AUTH).json()
    assert status["licensed"] is False and status["expired"] is False


@pytest.mark.parametrize(
    "location", ["../../..", "..", "/etc", "C:/Windows", "a\\..\\b", "", "./x", "missing/1"]
)
def test_forged_catalog_paths_are_rejected(
    client: TestClient, services: Services, location: str
) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    with catalog(services) as db:
        db.execute("UPDATE artifacts SET location=?", (location,))
    response = run(client, artifact_id)
    assert response.status_code == 403 and response.json() == {"error": "unlicensed_artifact"}
    assert client.get(f"/status/{artifact_id}", headers=AUTH).json()["licensed"] is False


def test_catalog_cannot_redirect_id_to_another_licensed_artifact(
    client: TestClient, services: Services
) -> None:
    first = compile_spec(client, make_spec(task="First"))["artifact_id"]
    second = compile_spec(client, make_spec(task="Second"))["artifact_id"]
    with catalog(services) as db:
        other = db.execute(
            "SELECT location FROM artifacts WHERE artifact_id=?", (second,)
        ).fetchone()[0]
        db.execute("UPDATE artifacts SET location=? WHERE artifact_id=?", (other, first))
    assert run(client, first).json() == {"error": "unlicensed_artifact"}
    assert run(client, second).status_code == 200


def test_forged_catalog_cannot_turn_ephemeral_into_installed(
    client: TestClient, services: Services
) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    with catalog(services) as db:
        db.execute("UPDATE artifacts SET kind='installed'")
    response = run(client, artifact_id, enabled=True, human_reviewed=True)
    assert response.status_code == 403 and response.json() == {"error": "unlicensed_artifact"}
    assert services.registry.list() == ()


def test_symlinked_artifact_directory_is_rejected(
    client: TestClient, services: Services, tmp_path: Path
) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    location = catalog(services).execute("SELECT location FROM artifacts").fetchone()[0]
    directory = services.compiler.artifact_root / location
    outside = tmp_path / "outside"
    shutil.copytree(directory, outside)
    shutil.rmtree(directory)
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlinks unavailable")
        # Unprivileged Windows: a directory junction is the equivalent escape.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(directory), str(outside)],
            capture_output=True,
            check=True,
            timeout=30,
        )
        assert directory.is_junction()
    response = run(client, artifact_id)
    assert response.status_code == 403 and response.json() == {"error": "unlicensed_artifact"}
    assert not (outside / ".consumed").exists()


# ------------------------------------------------------------------ privacy and errors


def test_errors_and_logs_never_contain_content(
    client: TestClient,
    services: Services,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    artifact_id = compile_spec(client, make_spec())["artifact_id"]

    async def explode(*args: object, **kwargs: object) -> RunResult:
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(services.runtime, "run", explode)
    response = post(client, "/run", {"artifact_id": artifact_id, "input": {"q": SENTINEL}})
    assert response.status_code == 500 and response.json() == {"error": "run_failed"}

    async def nonfinite(*args: object, **kwargs: object) -> RunResult:
        return RunResult.model_construct(
            run_id="r",
            output=float("nan"),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            wall_time_s=0.0,
        )

    monkeypatch.setattr(services.runtime, "run", nonfinite)
    bad = run(client, artifact_id)
    assert bad.status_code == 500 and bad.json() == {"error": "internal_error"}

    async def broken_compile(*args: object, **kwargs: object) -> object:
        raise OSError(SENTINEL + str(services.workspace))

    monkeypatch.setattr(services.compiler, "compile", broken_compile)
    failed = post(client, "/compile", {"request": SENTINEL})
    assert failed.status_code == 500 and failed.json() == {"error": "internal_error"}
    assert SENTINEL not in caplog.text and TOKEN not in caplog.text
    assert str(services.workspace) not in caplog.text


def test_budget_errors_are_fixed_codes(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exhausted(*args: object, **kwargs: object) -> object:
        raise BudgetExceeded(SENTINEL)

    monkeypatch.setattr(services.compiler, "compile", exhausted)
    response = post(client, "/compile", {"request": "x"})
    assert response.status_code == 422 and response.json() == {"error": "budget_exceeded"}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ArtifactExpiredError(SENTINEL), 410, "artifact_expired"),
        (UnlicensedArtifactError(SENTINEL), 403, "unlicensed_artifact"),
        (BudgetExceeded(SENTINEL), 422, "budget_exceeded"),
        (ImportError(SENTINEL), 503, "backend_unavailable"),
    ],
)
def test_runtime_gate_errors_map_to_fixed_codes(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    code: str,
) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]

    async def gate(*args: object, **kwargs: object) -> RunResult:
        raise error

    monkeypatch.setattr(services.runtime, "run", gate)
    response = run(client, artifact_id)
    assert response.status_code == status and response.json() == {"error": code}


def test_telemetry_busy_after_execution_is_never_a_delivery(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is taken after the up-front probe: the run consumed, but never delivers."""
    monkeypatch.setattr(server, "LOCK_TIMEOUT_S", 0.2)
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    monkeypatch.setattr(services.runtime.store, "probe", lambda: None)
    holder = sqlite3.connect(services.runtime.store.directory / "index.sqlite3")
    try:
        holder.execute("BEGIN IMMEDIATE")
        response = run(client, artifact_id)
    finally:
        holder.rollback()
        holder.close()
    assert response.status_code == 503 and response.json() == {"error": "storage_busy"}
    assert_private(response, services)
    status = client.get(f"/status/{artifact_id}", headers=AUTH).json()
    assert status["expired"] is True and status["licensed"] is False
    assert run(client, artifact_id).json() == {"error": "artifact_expired"}


def test_unconfigured_delivery_model_is_unavailable(client: TestClient, services: Services) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]

    def unavailable(model: str) -> ModelAdapter:
        raise ProfileError("model_not_configured")

    # A separate caller-owned Services value; the original is never mutated.
    other = dataclasses.replace(services, adapter_factory=unavailable)
    with TestClient(server.create_app(other, api_token=TOKEN)) as other_client:
        response = run(other_client, artifact_id)
    assert response.status_code == 503 and response.json() == {"error": "backend_unavailable"}
    assert run(client, artifact_id).status_code == 200


def test_missing_artifact_metadata_is_unlicensed(client: TestClient, services: Services) -> None:
    artifact_id = compile_spec(client, make_spec())["artifact_id"]
    location = catalog(services).execute("SELECT location FROM artifacts").fetchone()[0]
    (services.compiler.artifact_root / location / "spec.json").unlink()
    assert run(client, artifact_id).json() == {"error": "unlicensed_artifact"}
    assert client.get(f"/status/{artifact_id}", headers=AUTH).json()["licensed"] is False


def test_corrupt_catalog_approval_fails_closed(client: TestClient, services: Services) -> None:
    artifact_id = install(client)
    with catalog(services) as db:
        db.execute("UPDATE artifacts SET spec_hash=?", ("0" * 64,))
    assert run(client, artifact_id, enabled=True, human_reviewed=True).json() == {
        "error": "unlicensed_artifact"
    }
    rec = post(client, "/recompile", {"artifact_id": artifact_id})
    assert rec.status_code == 500 and rec.json() == {"error": "catalog_unavailable"}
    with catalog(services) as db:
        db.execute(
            "UPDATE artifacts SET spec_hash=?, approved_spec='{'",
            (make_spec(install=True).spec_hash,),
        )
    rec = post(client, "/recompile", {"artifact_id": artifact_id})
    assert rec.status_code == 500 and rec.json() == {"error": "catalog_unavailable"}
    assert services.registry.status(make_spec(install=True).spec_hash).revision == 1


def test_recompile_below_floor_keeps_prior_pointer(
    client: TestClient, services: Services, tmp_path: Path
) -> None:
    artifact_id = install(client)
    (tmp_path / "cases.json").write_text(
        json.dumps([{"case_id": str(i), "task": {}, "expected": "different"} for i in range(6)]),
        encoding="utf-8",
    )
    body = post(client, "/recompile", {"artifact_id": artifact_id}).json()
    assert body["accepted"] is False and body["artifact_id"] is None
    assert body["failures"] and "registry_rejected" not in body["failures"]
    assert services.registry.status(make_spec(install=True).spec_hash).revision == 1
    assert run(client, artifact_id, enabled=True, human_reviewed=True).status_code == 200


def test_failure_codes_are_sanitized() -> None:
    assert server._failure_codes(
        ["floor_not_met", "floor_not_met", "private path", "x" * 65, "PRIVATE_MARKER"]
    ) == ("floor_not_met", "compile_failure")


# ------------------------------------------------------------------ local helper


def test_main_serves_loopback_without_access_log_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    import architect.profiles

    profile = setup_profile(tmp_path)
    captured: dict[str, object] = {}
    real_build = architect.profiles.build_services

    def tracking_build(*args: object, **kwargs: object) -> Services:
        built = real_build(*args, **kwargs)  # type: ignore[arg-type]
        captured["services"] = built
        return built

    async def fake_serve(self: uvicorn.Server, sockets: object = None) -> None:
        captured["config"] = self.config

    monkeypatch.setattr(architect.profiles, "build_services", tracking_build)
    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setenv("ARCHITECT_TEST_BEARER", TOKEN)
    assert server.main(["--profile", str(profile), "--token-env", "ARCHITECT_TEST_BEARER"]) == 0
    config = captured["config"]
    assert isinstance(config, uvicorn.Config)
    assert config.host == "127.0.0.1" and config.access_log is False
    assert config.server_header is False and config.proxy_headers is False
    built = captured["services"]
    assert isinstance(built, Services) and built.runtime.store._closed


def test_main_rejects_missing_token_and_remote_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = setup_profile(tmp_path)
    monkeypatch.delenv("ARCHITECT_API_TOKEN", raising=False)
    assert server.main(["--profile", str(profile)]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "missing_api_token"}
    monkeypatch.setenv("ARCHITECT_API_TOKEN", TOKEN)
    assert server.main(["--profile", str(profile), "--host", "0.0.0.0"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "non_loopback_host"}
    assert server.main(["--profile", str(tmp_path / "missing.json")]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_profile"}
    assert server.main(["--bogus"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "invalid_arguments"}
    assert not (tmp_path / "work" / "server").exists()


def test_module_entry_point_fails_closed_without_token(tmp_path: Path) -> None:
    profile = setup_profile(tmp_path)
    environment = {key: value for key, value in os.environ.items() if key != "ARCHITECT_API_TOKEN"}
    environment["ARCHITECT_SIGNING_KEY_FILE"] = str(tmp_path / "authority.key")
    result = subprocess.run(
        [sys.executable, "-m", "architect.server", "--profile", str(profile)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=environment,
    )
    assert result.returncode == 1 and not result.stdout
    assert json.loads(result.stderr) == {"error": "missing_api_token"}
