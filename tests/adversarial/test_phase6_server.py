"""Independent HTTP gate tests with real local Services and narrow fault injection.

Faults model integration regressions or damaged private disk state. They do not
claim that untrusted HTTP clients can patch Python or write private catalog data.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from architect import server as server_module
from architect.compiler import CompileResult
from architect.profiles import Services, build_services, load_profile
from architect.server import create_app
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

TOKEN = "independent-fixture-only-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "PRIVATE_CUSTOMER_A731"


def spec(*, installed: bool = False) -> Spec:
    return Spec(task="Return accepted", input_schema={"type": "object"},
                output_schema={"type": "string"}, domain="synthesizer",
                floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS,
                                      limit=1, dataset="review"),
                budget=Budget(), lifecycle=Lifecycle.INSTALL if installed else Lifecycle.EXPIRE)


def responses(path: Path, cost: float = .00004) -> None:
    path.write_text(json.dumps([
        {"text": "accepted", "tokens_in": 9000, "tokens_out": 11000,
         "cost_usd": cost, "model": "fake"} for _ in range(40)
    ]), encoding="utf-8")


@pytest.fixture
def services(tmp_path: Path) -> Iterator[Services]:
    (tmp_path / "cases.json").write_text(json.dumps([
        {"case_id": str(i), "task": {}, "expected": "accepted"} for i in range(8)
    ]), encoding="utf-8")
    responses(tmp_path / "responses.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "workspace": "work",
        "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
        "floors": {"review": {"kind": "held_out_tasks", "path": "cases.json"}},
        "compile": {"max_candidates": 1, "enable_rewriting": False},
    }), encoding="utf-8")
    profile = load_profile(profile_path)
    built = build_services(profile, profile.workspace)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(create_app(services, api_token=TOKEN)) as active:
        yield active


def compile_body(client: TestClient, *, installed: bool = False) -> dict:
    response = client.post("/compile", headers=AUTH, json={
        "spec": spec(installed=installed).model_dump(mode="json"), "approved": installed,
    })
    assert response.status_code == 200, response.text
    return response.json()


def installed_id(client: TestClient) -> str:
    body = compile_body(client, installed=True)
    assert body["accepted"], body
    return body["artifact_id"]


def run(client: TestClient, artifact_id: str, **flags: bool) -> httpx.Response:
    return client.post("/run", headers=AUTH, json={
        "artifact_id": artifact_id, "input": {}, **flags,
    })


def catalog_path(services: Services) -> Path:
    return services.workspace / "server" / "catalog.sqlite3"


def artifact_directory(services: Services, artifact_id: str) -> Path:
    with sqlite3.connect(catalog_path(services)) as db:
        row = db.execute("SELECT location FROM artifacts WHERE artifact_id=?",
                         (artifact_id,)).fetchone()
    return services.compiler.artifact_root / row[0]


@pytest.mark.parametrize("report_fault", ["missing", "different"])
def test_compile_requires_matching_result_report(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch,
    report_fault: str,
) -> None:
    real_compile = services.compiler.compile

    async def fault(source: Spec, **kwargs: object) -> CompileResult:
        result = await real_compile(source, **kwargs)
        assert result.artifact is not None and result.artifact.licensed
        assert result.report is not None
        report = None if report_fault == "missing" else result.report.model_copy(
            update={"verification_id": "different-measurement"})
        return result.model_copy(update={"report": report})

    monkeypatch.setattr(services.compiler, "compile", fault)
    body = compile_body(client)
    assert body["accepted"] is False
    assert body["artifact_id"] is None


def test_compile_reauthenticates_persisted_report_before_acceptance(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_compile = services.compiler.compile

    async def fault(source: Spec, **kwargs: object) -> CompileResult:
        result = await real_compile(source, **kwargs)
        assert result.artifact is not None and result.artifact.licensed
        (result.artifact.directory / "report.json").unlink()
        return result

    monkeypatch.setattr(services.compiler, "compile", fault)
    body = compile_body(client)
    assert body["accepted"] is False
    assert body["artifact_id"] is None


@pytest.mark.parametrize("code", [SECRET, "customer.secret", "C:private", "api-key-123"])
def test_rejected_compile_does_not_echo_arbitrary_compact_strings(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    real_compile = services.compiler.compile

    async def fault(source: Spec, **kwargs: object) -> CompileResult:
        result = await real_compile(source, **kwargs)
        return result.model_copy(update={"failures": (code,)})

    monkeypatch.setattr(services.compiler, "compile", fault)
    body = compile_body(client)
    assert not body["accepted"]
    assert body["failures"] == ["compile_failure"]


def test_tied_recompile_has_client_contract_code_and_preserves_pointer(
    client: TestClient, services: Services,
) -> None:
    artifact_id = installed_id(client)
    before = services.registry.status(spec(installed=True).spec_hash)
    response = client.post("/recompile", headers=AUTH, json={"artifact_id": artifact_id})
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["failures"] == ["not_better"]
    assert services.registry.status(before.name) == before


async def test_recompile_cannot_replay_preexisting_license_as_fresh_stage4(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(services, api_token=TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH) as client:
        first = await client.post("/compile", json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True})
        assert first.json()["accepted"]
        name = spec(installed=True).spec_hash
        before = services.registry.status(name)
        responses(tmp_path / "responses.json", .00001)
        stale = await services.compiler.compile(spec(installed=True), approved=True)
        assert stale.artifact is not None and stale.artifact.licensed

        async def replay(*args: object, **kwargs: object) -> CompileResult:
            return stale

        monkeypatch.setattr(services.compiler, "compile", replay)
        response = await client.post("/recompile", json={
            "artifact_id": first.json()["artifact_id"]})
        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert services.registry.status(name) == before


async def test_concurrent_cheaper_recompile_publishes_only_one_revision(
    services: Services, tmp_path: Path,
) -> None:
    app = create_app(services, api_token=TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH) as client:
        first = await client.post("/compile", json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True})
        assert first.json()["accepted"]
        artifact_id = first.json()["artifact_id"]
        responses(tmp_path / "responses.json", .00001)
        results = await asyncio.gather(*[
            client.post("/recompile", json={"artifact_id": artifact_id}) for _ in range(2)
        ])
        assert all(response.status_code == 200 for response in results)
        bodies = [response.json() for response in results]
        assert sum(body["accepted"] for body in bodies) == 1, bodies
        assert services.registry.status(spec(installed=True).spec_hash).revision == 2
        current = await client.get(f"/status/{artifact_id}")
        assert current.json()["licensed"]


@pytest.mark.parametrize("filename", ["harness.py", "spec.json", "config.json", "report.json"])
def test_hardlinked_artifact_files_fail_closed(
    client: TestClient, services: Services, tmp_path: Path, filename: str,
) -> None:
    artifact_id = compile_body(client)["artifact_id"]
    target = artifact_directory(services, artifact_id) / filename
    outside = tmp_path / ("outside-" + filename)
    shutil.copyfile(target, outside)
    target.unlink()
    os.link(outside, target)
    response = run(client, artifact_id)
    assert response.status_code == 403
    assert response.json() == {"error": "unlicensed_artifact"}


def test_hardlinked_catalog_fails_closed_without_touching_external_database(
    services: Services, tmp_path: Path,
) -> None:
    directory = services.workspace / "server"
    directory.mkdir()
    outside = tmp_path / "external.sqlite3"
    with sqlite3.connect(outside) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    before = outside.read_bytes()
    os.link(outside, catalog_path(services))
    with pytest.raises(Exception, match="catalog_unavailable"):
        create_app(services, api_token=TOKEN)
    assert outside.read_bytes() == before


def test_catalog_removed_after_startup_is_not_silently_recreated(
    client: TestClient, services: Services,
) -> None:
    artifact_id = compile_body(client)["artifact_id"]
    catalog_path(services).unlink()
    response = client.get(f"/status/{artifact_id}", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"error": "catalog_unavailable"}
    assert not catalog_path(services).exists()


@pytest.mark.parametrize("filename", ["harness.py", "spec.json", "config.json", "report.json"])
def test_symlinked_artifact_files_are_denied(
    client: TestClient, services: Services, tmp_path: Path, filename: str,
) -> None:
    artifact_id = compile_body(client)["artifact_id"]
    target = artifact_directory(services, artifact_id) / filename
    outside = tmp_path / ("outside-" + filename)
    shutil.copyfile(target, outside)
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit unprivileged file symlinks")
    response = run(client, artifact_id)
    assert response.status_code == 403
    assert response.json() == {"error": "unlicensed_artifact"}


@pytest.mark.parametrize("payload", [
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":1e999}',
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":{"x":1,"x":2}}',
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":' + '[' * 65 + '0' + ']' * 65 + '}',
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":{},"human_reviewed":1}',
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":{},"profile":{"key":"PRIVATE"}}',
    '{"artifact_id":"aaaaaaaaaaaaaaaa","input":{},"report":{"passed":true}}',
])
def test_json_protocol_and_authority_faults_are_private(client: TestClient, payload: str) -> None:
    response = client.post("/run", headers={**AUTH, "Content-Type": "application/json"},
                           content=payload)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


async def test_duplicate_auth_denied_before_receive(services: Services) -> None:
    app = create_app(services, api_token=TOKEN)
    messages: list[dict] = []

    async def receive() -> dict:
        pytest.fail("Unauthorized request body was read")

    async def send(message: dict) -> None:
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "POST", "scheme": "http", "path": "/compile",
               "raw_path": b"/compile", "query_string": b"", "root_path": "",
               "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
               "headers": [(b"authorization", f"Bearer {TOKEN}".encode())] * 2}, receive, send)
    assert messages[0]["status"] == 401
    assert json.loads(messages[1]["body"]) == {"error": "unauthorized"}


def test_caller_ownership_high_token_delivery_and_one_shot(
    services: Services,
) -> None:
    with TestClient(create_app(services, api_token=TOKEN)) as first:
        artifact_id = compile_body(first)["artifact_id"]
    assert not services.runtime.store._closed
    assert services.workspace / "server" in services.profile.pi.denied_paths
    with TestClient(create_app(services, api_token=TOKEN)) as second:
        response = run(second, artifact_id)
        assert response.status_code == 200
        assert response.json()["tokens_in"] == 9000
        assert response.json()["tokens_out"] == 11000
        assert run(second, artifact_id).status_code == 410
        body = second.get(f"/status/{artifact_id}", headers=AUTH).json()
        assert body["expired"] and not body["licensed"]
    assert not services.runtime.store._closed


def test_wire_auth_before_body_and_real_compile_delivery(services: Services) -> None:
    """Real loopback HTTP, not TestClient; no body sent with duplicated auth."""
    ready = threading.Event()

    class SignaledServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = SignaledServer(uvicorn.Config(create_app(services, api_token=TOKEN),
                                          access_log=False, log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        assert ready.wait(10), "Loopback server did not start"
        with socket.create_connection(("127.0.0.1", port), timeout=5) as wire:
            wire.sendall((f"POST /compile HTTP/1.1\r\nHost: localhost\r\n"
                          f"Authorization: Bearer {TOKEN}\r\n"
                          f"Authorization: Bearer {TOKEN}\r\n"
                          "Content-Type: application/json\r\n"
                          "Content-Length: 10000\r\nConnection: close\r\n\r\n").encode())
            data = wire.recv(4096)
            assert b"401 Unauthorized" in data
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=AUTH,
                          trust_env=False, timeout=30) as client:
            proposal = client.post("/compile", json={
                "spec": spec(installed=True).model_dump(mode="json")}).json()
            assert proposal["approval_required"] and proposal["artifact_id"] is None
            accepted = client.post("/compile", json={
                "spec": proposal["spec"], "approved": True}).json()
            assert accepted["accepted"]
            artifact_id = accepted["artifact_id"]
            for flags in ({}, {"enabled": True}, {"human_reviewed": True}):
                denied = client.post("/run", json={"artifact_id": artifact_id,
                                                   "input": {}, **flags})
                assert denied.status_code == 403
            delivered = client.post("/run", json={"artifact_id": artifact_id, "input": {},
                                                  "enabled": True, "human_reviewed": True})
            assert delivered.status_code == 200
            assert delivered.json()["output"] == "accepted"
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()
    assert not services.runtime.store._closed


@pytest.mark.parametrize("fault", ["changed_spec", "missing_stage4"])
def test_exact_spec_and_stage4_are_required(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    real_compile = services.compiler.compile

    async def changed(source: Spec, **kwargs: object) -> CompileResult:
        result = await real_compile(source, **kwargs)
        if fault == "changed_spec":
            return result.model_copy(update={"spec": result.spec.model_copy(
                update={"tools": ("unauthorized-tool",)})})
        return result.model_copy(update={"stages": (1, 2)})

    monkeypatch.setattr(services.compiler, "compile", changed)
    body = compile_body(client, installed=True)
    assert not body["accepted"] and body["artifact_id"] is None
    assert services.registry.list() == ()


def test_invalid_catalog_root_is_fixed_and_preserves_caller_services(services: Services) -> None:
    (services.workspace / "server").write_text(SECRET, encoding="utf-8")
    with pytest.raises(Exception, match="catalog_unavailable") as caught:
        create_app(services, api_token=TOKEN)
    assert SECRET not in str(caught.value)
    assert not services.runtime.store._closed


def test_catalog_private_location_cannot_leak_through_status(
    client: TestClient, services: Services,
) -> None:
    artifact_id = installed_id(client)
    private_location = "C:/private/" + SECRET
    with sqlite3.connect(catalog_path(services)) as db:
        db.execute("UPDATE artifacts SET location=? WHERE artifact_id=?",
                   (private_location, artifact_id))
    response = client.get(f"/status/{artifact_id}", headers=AUTH)
    assert SECRET not in response.text
    assert private_location not in response.text
    assert response.status_code == 500
    assert response.json() == {"error": "catalog_unavailable"}


# ------------------------------------------------------------ lock contention (H2)


@contextmanager
def loopback(app: object) -> Iterator[str]:
    """Real uvicorn on an ephemeral loopback port; always stopped on exit."""
    ready = threading.Event()

    class SignaledServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = SignaledServer(uvicorn.Config(app, access_log=False, log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        assert ready.wait(10), "Loopback server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        assert not worker.is_alive()


@contextmanager
def write_lock(database: Path) -> Iterator[None]:
    """Hold the SQLite write lock the way a concurrent CLI install would."""
    holder = sqlite3.connect(database, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_held_registry_lock_does_not_stall_other_requests(
    services: Services, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "LOCK_TIMEOUT_S", 2.0)
    app = create_app(services, api_token=TOKEN)
    with loopback(app) as base, httpx.Client(base_url=base, headers=AUTH,
                                             trust_env=False, timeout=30) as client:
        accepted = client.post("/compile", json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True}).json()
        installed = accepted["artifact_id"]
        ephemeral = client.post("/compile", json={
            "spec": spec().model_dump(mode="json")}).json()["artifact_id"]
        delivery = {"artifact_id": installed, "input": {}, "enabled": True,
                    "human_reviewed": True}
        outcomes: dict[str, httpx.Response] = {}

        def blocked(key: str, method: str, path: str, body: object = None) -> None:
            with httpx.Client(base_url=base, headers=AUTH, trust_env=False,
                              timeout=30) as own:
                outcomes[key] = (own.post(path, json=body) if method == "POST"
                                 else own.get(path))

        with write_lock(services.registry.root / "registry.sqlite3"):
            waiting = [
                threading.Thread(target=blocked, args=("run", "POST", "/run", delivery)),
                threading.Thread(target=blocked, args=("status", "GET", f"/status/{installed}")),
            ]
            for thread in waiting:
                thread.start()
            time.sleep(.3)  # Both are now parked on the registry lock in worker threads.
            started = time.monotonic()
            with httpx.Client(base_url=base, trust_env=False, timeout=30) as anonymous:
                rejected = anonymous.get(f"/status/{installed}")
            other = client.get(f"/status/{ephemeral}")
            elapsed = time.monotonic() - started
            assert rejected.status_code == 401 and rejected.json() == {"error": "unauthorized"}
            assert other.status_code == 200 and other.json()["licensed"] is True
            assert elapsed < 1.0, elapsed
            assert all(thread.is_alive() for thread in waiting)
            for thread in waiting:
                thread.join(timeout=20)
        for key in ("run", "status"):
            assert outcomes[key].status_code == 503, key
            assert outcomes[key].json() == {"error": "storage_busy"}
        # Contention is not revocation: once the lock is gone, delivery works again.
        assert client.get(f"/status/{installed}").json()["licensed"] is True
        assert client.post("/run", json=delivery).status_code == 200


def test_held_telemetry_lock_does_not_stall_other_requests_or_consume(
    services: Services, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another process holding the telemetry index lock parks /run off the loop."""
    monkeypatch.setattr(server_module, "LOCK_TIMEOUT_S", 1.5)
    app = create_app(services, api_token=TOKEN)
    with loopback(app) as base, httpx.Client(base_url=base, headers=AUTH,
                                             trust_env=False, timeout=30) as client:
        ephemeral = client.post("/compile", json={
            "spec": spec().model_dump(mode="json")}).json()["artifact_id"]
        outcome: dict[str, httpx.Response] = {}

        def blocked() -> None:
            with httpx.Client(base_url=base, headers=AUTH, trust_env=False,
                              timeout=30) as own:
                outcome["run"] = own.post("/run", json={"artifact_id": ephemeral,
                                                        "input": {}})

        with write_lock(services.runtime.store.directory / "index.sqlite3"):
            waiting = threading.Thread(target=blocked)
            waiting.start()
            time.sleep(.3)  # The run is now parked on the telemetry lock in a worker.
            started = time.monotonic()
            with httpx.Client(base_url=base, trust_env=False, timeout=30) as anonymous:
                rejected = anonymous.get(f"/status/{ephemeral}")
            other = client.get(f"/status/{ephemeral}")
            elapsed = time.monotonic() - started
            assert rejected.status_code == 401 and rejected.json() == {"error": "unauthorized"}
            assert other.status_code == 200 and other.json()["expired"] is False
            assert elapsed < 1.0, elapsed
            assert waiting.is_alive()
            waiting.join(timeout=20)
        assert outcome["run"].status_code == 503
        assert outcome["run"].json() == {"error": "storage_busy"}
        # Refused before consumption: the one-shot artifact is still deliverable.
        status = client.get(f"/status/{ephemeral}").json()
        assert status["licensed"] is True and status["expired"] is False
        delivered = client.post("/run", json={"artifact_id": ephemeral, "input": {}})
        assert delivered.status_code == 200 and delivered.json()["output"] == "accepted"
    assert services.runtime.store.rows()[-1].accepted is True


def test_held_catalog_lock_is_a_fixed_busy_code(
    services: Services, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "LOCK_TIMEOUT_S", .2)
    with TestClient(create_app(services, api_token=TOKEN)) as client:
        artifact_id = compile_body(client)["artifact_id"]
        with write_lock(catalog_path(services)):
            response = client.get(f"/status/{artifact_id}", headers=AUTH)
            assert response.status_code == 503
            assert response.json() == {"error": "storage_busy"}
        assert client.get(f"/status/{artifact_id}", headers=AUTH).status_code == 200


def test_server_lock_wait_leaves_caller_registry_unchanged(services: Services) -> None:
    create_app(services, api_token=TOKEN)
    assert services.registry.lock_timeout == 30.0


# -------------------------------------------------------- idempotent approval (H3)


def test_retried_identical_approval_returns_existing_id_without_replacing(
    client: TestClient, services: Services,
) -> None:
    artifact_id = installed_id(client)
    name = spec(installed=True).spec_hash
    before = services.registry.status(name)
    retry = compile_body(client, installed=True)
    assert retry["accepted"] is False
    assert retry["failures"] == ["not_better"]
    assert retry["artifact_id"] == artifact_id
    assert services.registry.status(name) == before
    assert run(client, artifact_id, enabled=True, human_reviewed=True).status_code == 200


async def test_retry_gets_no_id_for_an_installation_the_server_did_not_approve(
    services: Services,
) -> None:
    direct = await services.compiler.compile(spec(installed=True), approved=True)
    assert direct.artifact is not None
    services.registry.install(spec(installed=True).spec_hash, direct.artifact)
    app = create_app(services, api_token=TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH) as client:
        retry = (await client.post("/compile", json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True})).json()
    assert retry["accepted"] is False and retry["failures"] == ["not_better"]
    assert retry["artifact_id"] is None
    # The retry left a reservation for its own candidate. It never names the outside
    # install, so neither a second retry nor its private row yields a usable ID.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH) as client:
        again = (await client.post("/compile", json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True})).json()
        assert again["artifact_id"] is None and again["failures"] == ["not_better"]
        with sqlite3.connect(catalog_path(services)) as db:
            rows = db.execute("SELECT artifact_id, version FROM artifacts").fetchall()
        assert len(rows) == 1 and rows[0][1] == 0
        status = await client.get(f"/status/{rows[0][0]}")
        assert status.status_code == 404 and status.json() == {"error": "unknown_artifact"}
        delivery = await client.post("/run", json={"artifact_id": rows[0][0], "input": {},
                                                   "enabled": True, "human_reviewed": True})
        assert delivery.status_code == 404


def test_retry_heals_install_whose_catalog_write_was_lost(
    services: Services, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry committed, catalog busy: the identical approval's retry finishes the job."""
    monkeypatch.setattr(server_module, "LOCK_TIMEOUT_S", .2)
    original = server_module.Catalog.upsert_installed
    calls = 0

    def contended(self: server_module.Catalog, name: str, approved: Spec, version: int) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            with write_lock(catalog_path(services)):
                return original(self, name, approved, version)
        return original(self, name, approved, version)

    monkeypatch.setattr(server_module.Catalog, "upsert_installed", contended)
    name = spec(installed=True).spec_hash
    with TestClient(create_app(services, api_token=TOKEN)) as client:
        lost = client.post("/compile", headers=AUTH, json={
            "spec": spec(installed=True).model_dump(mode="json"), "approved": True})
        assert lost.status_code == 503 and lost.json() == {"error": "storage_busy"}
        installed = services.registry.status(name)  # Committed before the catalog failed.
        retry = compile_body(client, installed=True)
        assert retry["accepted"] is False and retry["failures"] == ["not_better"]
        artifact_id = retry["artifact_id"]
        assert isinstance(artifact_id, str)
        assert services.registry.status(name) == installed  # Nothing was replaced.
        assert compile_body(client, installed=True)["artifact_id"] == artifact_id
        status = client.get(f"/status/{artifact_id}", headers=AUTH).json()
        assert status["licensed"] is True and status["version"] == installed.artifact_version
        assert run(client, artifact_id, enabled=True, human_reviewed=True).status_code == 200


async def test_reservation_heals_only_its_own_candidate_and_exact_spec(
    services: Services,
) -> None:
    """A lost write is adopted only for the reserved candidate and the same approval."""
    service = server_module._Service(services)
    approved = spec(installed=True)
    name = approved.spec_hash

    def version() -> int:
        entry = service.catalog.find_installed(name)
        assert entry is not None
        return entry.version

    direct = await services.compiler.compile(approved, approved=True)
    assert direct.artifact is not None and direct.artifact.report is not None
    verification_id = direct.artifact.report.verification_id
    service.catalog.reserve(name, approved, "another-candidate")
    service._heal(name, approved)  # Nothing installed yet: nothing to heal.
    assert version() == 0
    services.registry.install(name, direct.artifact)  # Never reserved by the server.
    service._heal(name, approved)
    assert version() == 0
    other = approved.model_copy(update={"provenance": {"task": "user"}})
    assert other != approved and other.spec_hash == name
    # Reserved for a different approval of the same hash: never heals either Spec.
    service.catalog.reserve(name, other, verification_id)
    service._heal(name, other)
    service._heal(name, approved)
    assert version() == 0
    service.catalog.reserve(name, approved, verification_id)
    service._heal(name, approved)
    assert version() == direct.artifact.version


async def test_overlapping_approvals_still_heal_the_committed_candidate(
    services: Services,
) -> None:
    """A reserves C_A, B rebinds to C_B, C_A commits, A's catalog write is lost."""
    service = server_module._Service(services)
    approved = spec(installed=True)
    name = approved.spec_hash
    first = await services.compiler.compile(approved, approved=True)
    second = await services.compiler.compile(approved, approved=True)
    assert first.artifact is not None and first.artifact.report is not None
    assert second.artifact is not None and second.artifact.report is not None
    assert first.artifact.report.verification_id != second.artifact.report.verification_id
    service.catalog.reserve(name, approved, first.artifact.report.verification_id)
    service.catalog.reserve(name, approved, second.artifact.report.verification_id)
    installed = services.registry.install(name, first.artifact)
    entry = service.catalog.find_installed(name)
    assert entry is not None and entry.version == 0
    app = create_app(services, api_token=TOKEN)
    body = {"spec": approved.model_dump(mode="json"), "approved": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=AUTH) as client:
        retry = (await client.post("/compile", json=body)).json()
        assert retry["accepted"] is False and retry["failures"] == ["not_better"]
        artifact_id = retry["artifact_id"]
        assert isinstance(artifact_id, str)
        again = (await client.post("/compile", json=body)).json()
        assert again["artifact_id"] == artifact_id
        status = (await client.get(f"/status/{artifact_id}")).json()
        assert status["licensed"] is True and status["version"] == installed.artifact_version
        assert services.registry.status(name) == installed  # Nothing was replaced.


def test_snapshot_matches_untrusted_directories_by_identity_not_spelling(
    services: Services,
) -> None:
    """A differently spelled path (case alias, link) to a skipped directory is not fresh."""
    service = server_module._Service(services)
    root = Path(os.path.abspath(services.compiler.artifact_root))
    skipped = root / "skipped" / "1"
    skipped.mkdir(parents=True)
    (skipped / "report.json").write_text(SECRET, encoding="utf-8")
    snapshot = service._verification_ids()
    assert snapshot.untrusted and snapshot.identities

    def at(directory: Path) -> CompileResult:
        return CompileResult.model_construct(
            report=None, artifact=type("A", (), {"directory": directory})())

    assert snapshot.stale(at(skipped))
    alias = root / "alias"
    try:
        alias.symlink_to(root / "skipped", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlinks unavailable")
        # Unprivileged Windows: a directory junction is the equivalent alias.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(root / "skipped")],
            capture_output=True, check=True, timeout=30,
        )
        assert alias.is_junction()
    assert snapshot.stale(at(alias / "1"))
    assert snapshot.stale(at(alias / "1" / "missing"))
    upper = root / "SKIPPED" / "1"
    if upper.exists():  # A case-insensitive volume, such as default APFS.
        assert snapshot.stale(at(upper))
    fresh = root / "fresh" / "1"
    fresh.mkdir(parents=True)
    assert not snapshot.stale(at(fresh))
