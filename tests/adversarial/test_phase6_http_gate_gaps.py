"""HTTP gate gaps: damaged host state and trusted integration fault injection.

These tests do not claim resistance to privileged Python or filesystem writers.
"""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from architect import server
from architect.compiler import CompileResult
from architect.profiles import ProfileError, Services, build_services, load_profile
from architect.spec import Budget, FloorDefinition, FloorKind, Lifecycle, Spec

TOKEN = "independent-fixture-only-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "PRIVATE_CUSTOMER_A731"


def spec(*, installed: bool = False) -> Spec:
    return Spec(
        task="Return accepted",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        domain="synthesizer",
        floor=FloorDefinition(kind=FloorKind.HELD_OUT_TASKS, limit=1, dataset="review"),
        budget=Budget(),
        lifecycle=Lifecycle.INSTALL if installed else Lifecycle.EXPIRE,
    )


def responses(path: Path, cost: float = 0.00004) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "text": "accepted",
                    "tokens_in": 9000,
                    "tokens_out": 11000,
                    "cost_usd": cost,
                    "model": "fake",
                }
                for _ in range(40)
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def services(tmp_path: Path) -> Iterator[Services]:
    (tmp_path / "cases.json").write_text(
        json.dumps([{"case_id": str(i), "task": {}, "expected": "accepted"} for i in range(8)]),
        encoding="utf-8",
    )
    responses(tmp_path / "responses.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "workspace": "work",
                "models": {"fake": {"provider": "fake", "fixture": "responses.json"}},
                "floors": {"review": {"kind": "held_out_tasks", "path": "cases.json"}},
                "compile": {"max_candidates": 1, "enable_rewriting": False},
            }
        ),
        encoding="utf-8",
    )
    profile = load_profile(profile_path)
    built = build_services(profile, profile.workspace)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(server.create_app(services, api_token=TOKEN)) as active:
        yield active


def compile_body(client: TestClient, *, installed: bool = False) -> dict:
    response = client.post(
        "/compile",
        headers=AUTH,
        json={
            "spec": spec(installed=installed).model_dump(mode="json"),
            "approved": installed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def installed_id(client: TestClient) -> str:
    body = compile_body(client, installed=True)
    assert body["accepted"], body
    return body["artifact_id"]


def run(client: TestClient, artifact_id: str, **flags: bool) -> httpx.Response:
    return client.post(
        "/run",
        headers=AUTH,
        json={
            "artifact_id": artifact_id,
            "input": {},
            **flags,
        },
    )


def catalog_path(services: Services) -> Path:
    return services.workspace / "server" / "catalog.sqlite3"


def artifact_directory(services: Services, artifact_id: str) -> Path:
    with sqlite3.connect(catalog_path(services)) as db:
        row = db.execute(
            "SELECT location FROM artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
    return services.compiler.artifact_root / row[0]


@pytest.mark.parametrize("payload", [float("nan"), float("inf"), object()])
def test_response_serialization_is_finite_and_private(payload: object) -> None:
    response = server._json_response(200, {SECRET: payload})
    assert response.status_code == 500
    assert json.loads(response.body) == {"error": "invalid_output"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("location", ["root", "outside", "file", "redirect"])
def test_containment_rejects_root_outside_nondirectory_and_resolution_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "child"
    candidate.mkdir()
    if location == "root":
        candidate = root
    elif location == "outside":
        candidate = tmp_path
    elif location == "file":
        candidate.rmdir()
        candidate.write_text(SECRET, encoding="utf-8")
    else:
        # Model an FS alias without requiring Windows symlink privileges.
        original = Path.resolve

        def resolve(path: Path, *args: object, **kwargs: object) -> Path:
            return tmp_path if path == candidate else original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(server.ServerError) as caught:
        server._contained(root, candidate)
    assert (caught.value.status, caught.value.code) == (403, "unlicensed_artifact")


@pytest.mark.parametrize("predicate", ["is_symlink", "is_junction"])
def test_alias_predicates_fail_closed_without_following_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    predicate: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "candidate"
    candidate.mkdir()
    original = getattr(Path, predicate)
    monkeypatch.setattr(Path, predicate, lambda path: path == candidate or original(path))
    with pytest.raises(server.ServerError, match="unlicensed_artifact"):
        server._contained(root, candidate)
    assert list(candidate.iterdir()) == []


def test_link_walk_terminates_at_filesystem_root(tmp_path: Path) -> None:
    assert server._no_links(tmp_path, tmp_path / "not-an-ancestor") is None


@pytest.mark.parametrize("damage", ["directory", "corrupt-db", "invalid-row"])
def test_catalog_damage_is_fixed_and_not_repaired(
    client: TestClient,
    services: Services,
    damage: str,
) -> None:
    artifact_id = compile_body(client)["artifact_id"]
    database = catalog_path(services)
    if damage == "directory":
        database.unlink()
        database.parent.rmdir()
        database.parent.write_text(SECRET, encoding="utf-8")
    elif damage == "corrupt-db":
        database.write_bytes(SECRET.encode())
    else:
        with sqlite3.connect(database) as db:
            db.execute("UPDATE artifacts SET version=?", (SECRET,))
    for endpoint in ("/run", "/recompile"):
        payload = {"artifact_id": artifact_id}
        if endpoint == "/run":
            payload["input"] = {}
        response = client.post(endpoint, headers=AUTH, json=payload)
        assert response.status_code == 500
        assert response.json() == {"error": "catalog_unavailable"}
    if damage == "directory":
        assert database.parent.read_text(encoding="utf-8") == SECRET
    elif damage == "corrupt-db":
        assert database.read_bytes() == SECRET.encode()


@pytest.mark.parametrize("error", [ProfileError(SECRET), ImportError(SECRET)])
def test_compile_backend_errors_hide_private_details(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    async def fail(*args: object, **kwargs: object) -> CompileResult:
        raise error

    monkeypatch.setattr(services.compiler, "compile", fail)
    response = client.post("/compile", headers=AUTH, json={"spec": spec().model_dump(mode="json")})
    assert response.status_code == 503
    assert response.json() == {"error": "backend_unavailable"}


@pytest.mark.parametrize(
    "fault", ["corrupt-report", "read-error", "alias", "hard-link", "install-remnant"]
)
def test_damaged_snapshot_entry_is_skipped_but_never_trusted(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault: str,
) -> None:
    """One bad entry no longer denies every compile, and is never evidence of freshness."""
    real_compile = services.compiler.compile
    produced: list[CompileResult] = []

    async def recording(*args: object, **kwargs: object) -> CompileResult:
        result = await real_compile(*args, **kwargs)
        produced.append(result)
        return result

    monkeypatch.setattr(services.compiler, "compile", recording)
    artifact_id = compile_body(client)["artifact_id"]
    stale = produced[-1]
    directory = artifact_directory(services, artifact_id)
    report = directory / "report.json"
    if fault == "corrupt-report":
        report.write_text(SECRET, encoding="utf-8")
    elif fault == "read-error":
        # Fails only inside the snapshot: the artifact stays loadable and licensed,
        # so only the untrusted-location rule can refuse the replay below.
        snapshotting = False
        original_snapshot = server._Service._verification_ids
        original_read = Path.read_text

        def snapshot(self: server._Service) -> object:
            nonlocal snapshotting
            snapshotting = True
            try:
                return original_snapshot(self)
            finally:
                snapshotting = False

        def read(path: Path, *args: object, **kwargs: object) -> str:
            if snapshotting and path == report:
                raise OSError(SECRET)
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(server._Service, "_verification_ids", snapshot)
        monkeypatch.setattr(Path, "read_text", read)
    elif fault == "alias":
        original_link = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path == directory or original_link(path)
        )
    elif fault == "hard-link":
        os.link(report, tmp_path / "extra-link-to-old-report.json")
    else:
        remnant = services.registry.root / ".install-crashremnant"
        remnant.mkdir()
        (remnant / "report.json").write_text('{"truncated": ', encoding="utf-8")

    fresh = compile_body(client)
    assert fresh["accepted"] is True and fresh["artifact_id"] != artifact_id

    async def replay(*args: object, **kwargs: object) -> CompileResult:
        return stale

    monkeypatch.setattr(services.compiler, "compile", replay)
    replayed = compile_body(client)
    assert replayed["accepted"] is False and replayed["artifact_id"] is None
    assert SECRET not in json.dumps(replayed)


def test_untrusted_location_alone_refuses_a_replay(services: Services) -> None:
    """Direct check of the rule the read-error case relies on."""
    service = server._Service(services)
    artifact = services.compiler.artifact_root / "a" / "1"
    result = CompileResult.model_construct(
        report=None, artifact=type("A", (), {"directory": artifact})()
    )
    # Production records untrusted locations normcased, as the snapshot compares them.
    parent = os.path.normcase(os.path.abspath(artifact.parent))
    inside = server._Snapshot(identifiers=frozenset(), untrusted=(parent,))
    sibling = server._Snapshot(identifiers=frozenset(), untrusted=(parent + "b",))
    assert inside.stale(result) is True
    assert sibling.stale(result) is False
    assert service._verification_ids().untrusted == ()


def test_freshness_snapshot_counts_skips_without_naming_them(
    client: TestClient, services: Services, caplog: pytest.LogCaptureFixture
) -> None:
    remnant = services.registry.root / f".install-{SECRET}"
    remnant.mkdir()
    (remnant / "report.json").write_text(SECRET, encoding="utf-8")
    with caplog.at_level("WARNING", logger="architect.server"):
        assert compile_body(client)["accepted"] is True
    assert "skipped 1 untrusted" in caplog.text
    assert SECRET not in caplog.text and str(services.workspace) not in caplog.text


@pytest.mark.parametrize("fault", ["root-alias", "root-unlistable"])
def test_freshness_snapshot_root_damage_still_prevents_compiler_invocation(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    compile_body(client)
    root = Path(os.path.abspath(services.compiler.artifact_root))
    if fault == "root-alias":
        original_link = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == root or original_link(path))
    else:
        original_iterdir = Path.iterdir

        def iterdir(path: Path) -> Iterator[Path]:
            if path == root:
                raise OSError(SECRET)
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", iterdir)
    calls = []

    async def unexpected(*args: object, **kwargs: object) -> CompileResult:
        calls.append(args)
        raise AssertionError("must snapshot before compiler invocation")

    monkeypatch.setattr(services.compiler, "compile", unexpected)
    response = client.post("/compile", headers=AUTH, json={"spec": spec().model_dump(mode="json")})
    assert response.status_code == 500
    assert response.json() == {"error": "compile_failure"}
    assert calls == []


def test_missing_registry_database_denies_delivery_without_recreation(
    client: TestClient,
    services: Services,
) -> None:
    artifact_id = installed_id(client)
    database = services.registry.root / "registry.sqlite3"
    database.unlink()
    response = run(client, artifact_id, enabled=True, human_reviewed=True)
    assert response.status_code == 403
    assert response.json() == {"error": "unlicensed_artifact"}
    status = client.get(f"/status/{artifact_id}", headers=AUTH)
    assert status.json()["licensed"] is False
    assert status.json()["expired"] is False
    assert not database.exists()


@pytest.mark.parametrize("fault", ["artifact-directory", "unapproved-install"])
def test_compile_trusted_result_regressions_cannot_publish(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real_compile = services.compiler.compile

    async def broken(source: Spec | str, **kwargs: object) -> CompileResult:
        result = await real_compile(spec(installed=fault == "unapproved-install"), approved=True)
        assert result.artifact is not None and result.artifact.licensed
        if fault == "artifact-directory":
            artifact = result.artifact.model_copy(update={"directory": services.workspace})
            result = result.model_copy(update={"artifact": artifact})
        return result

    monkeypatch.setattr(services.compiler, "compile", broken)
    response = client.post("/compile", headers=AUTH, json={"request": "return accepted"})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False and body["artifact_id"] is None
    assert body["failures"] == [
        "approval_required" if fault == "unapproved-install" else "unverified"
    ]
    assert services.registry.list() == ()
    with sqlite3.connect(catalog_path(services)) as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["spec", "approval"])
def test_recompile_requires_unchanged_preapproved_spec(
    client: TestClient,
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    artifact_id = installed_id(client)
    before = services.registry.list()[0]
    real_compile = services.compiler.compile

    async def changed(source: Spec, **kwargs: object) -> CompileResult:
        result = await real_compile(source, **kwargs)
        update = (
            {"spec": source.model_copy(update={"task": "not approved"})}
            if fault == "spec"
            else {"approval_required": True}
        )
        return result.model_copy(update=update)

    monkeypatch.setattr(services.compiler, "compile", changed)
    response = client.post("/recompile", headers=AUTH, json={"artifact_id": artifact_id})
    assert response.status_code == 200
    assert response.json()["failures"] == ["spec_changed"]
    assert response.json()["accepted"] is False
    assert response.json()["artifact_id"] is None
    assert services.registry.status(before.name) == before


@pytest.mark.parametrize("started", [False, True])
async def test_guard_suppresses_fault_text_and_never_double_starts(started: bool) -> None:
    sent = []
    reads = []

    async def receive() -> dict:
        reads.append(True)
        raise AssertionError("guard must not read body")

    async def send(message: dict) -> None:
        sent.append(message)

    async def broken(scope: dict, receive: object, send: object) -> None:
        if started:
            await send({"type": "http.response.start", "status": 204, "headers": []})
        raise RuntimeError(SECRET)

    await server._Guard(broken, TOKEN)(
        {"type": "http", "headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}, receive, send
    )
    assert reads == []
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [
        204 if started else 500
    ]
    if not started:
        assert json.loads(sent[-1]["body"]) == {"error": "internal_error"}
    assert SECRET not in repr(sent)


async def test_guard_drops_unsupported_protocol_without_dispatch_or_reads() -> None:
    calls = []

    async def unexpected(*args: object) -> None:
        calls.append(args)

    await server._Guard(unexpected, TOKEN)({"type": "websocket"}, unexpected, unexpected)
    assert calls == []


@pytest.mark.parametrize(
    "error,expected",
    [
        (HTTPException(418, SECRET), (400, "invalid_request")),
        (HTTPException(503, SECRET), (500, "internal_error")),
        (RequestValidationError([{"private": SECRET}]), (400, "invalid_request")),
    ],
)
async def test_framework_errors_do_not_reflect_details(
    services: Services,
    error: Exception,
    expected: tuple[int, str],
) -> None:
    app = server.create_app(services, api_token=TOKEN)
    handler = app.exception_handlers[
        RequestValidationError if isinstance(error, RequestValidationError) else HTTPException
    ]
    response = await handler(None, error)
    assert response.status_code == expected[0]
    assert json.loads(response.body) == {"error": expected[1]}


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("::1", True),
        ("127.0.0.1", True),
        ("not-a-host", False),
        ("192.0.2.1", False),
    ],
)
def test_loopback_requires_local_ip_or_exact_localhost(host: str, expected: bool) -> None:
    assert server._loopback(host) is expected


@pytest.mark.parametrize(
    "arguments", [["--port", "0"], ["--port", "65536"], ["--token-env", "INVALID-NAME"]]
)
def test_main_argument_bounds_fail_before_profile_load(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert server.main(["--profile", SECRET, *arguments]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "invalid_arguments"}


def test_main_help_exits_without_services(capsys: pytest.CaptureFixture[str]) -> None:
    assert server.main(["--help"]) == 0
    captured = capsys.readouterr()
    assert "--profile" in captured.out and not captured.err


@pytest.mark.parametrize("failure,code", [(KeyboardInterrupt(), 130), (RuntimeError(SECRET), 1)])
def test_main_closes_owned_services_on_interrupt_or_failure(
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    code: int,
) -> None:
    import architect.profiles

    monkeypatch.setattr(architect.profiles, "load_profile", lambda _: services.profile)
    monkeypatch.setattr(architect.profiles, "build_services", lambda *args: services)
    monkeypatch.setenv("ARCHITECT_API_TOKEN", TOKEN)

    async def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(uvicorn.Server, "serve", fail)
    assert server.main(["--profile", SECRET, "--host", "0.0.0.0", "--allow-non-loopback"]) == code
    assert services.runtime.store._closed
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == ("" if code == 130 else '{"error": "server_failed"}\n')


def test_module_main_guard_has_fixed_error_without_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ARCHITECT_API_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["architect.server", "--profile", SECRET])
    # Execute the production entry point in-process so coverage sees its guard.
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(Path(server.__file__)), run_name="__main__")
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error": "missing_api_token"}


@pytest.mark.parametrize(
    "token", [None, 123, "x" * 513, "x" * 31 + "\x00", "x" * 31 + "\t", "x" * 31 + "\n"]
)
def test_token_type_length_and_control_character_gates(token: object) -> None:
    assert server._valid_token(token) is False


@pytest.mark.parametrize("size", [32, 512])
def test_token_exact_length_boundaries(size: int) -> None:
    assert server._valid_token("x" * size) is True


@pytest.mark.parametrize("extra_depth,accepted", [(0, True), (1, False)])
def test_json_depth_boundary_and_finite_decimal(extra_depth: int, accepted: bool) -> None:
    # Object at depth 1, 62 arrays, scalar at depth 64 is the permitted maximum.
    value = (
        '{"artifact_id":"'
        + "a" * 20
        + '","input":'
        + ("[" * (62 + extra_depth))
        + "1.25"
        + ("]" * (62 + extra_depth))
        + "}"
    )
    if accepted:
        parsed = server._parse(value.encode(), server.RunRequest)
        scalar = parsed.input
        for _ in range(62):
            assert isinstance(scalar, list) and len(scalar) == 1
            scalar = scalar[0]
        assert scalar == 1.25
        assert parsed.enabled is False and parsed.human_reviewed is False
    else:
        with pytest.raises(server.ServerError) as caught:
            server._parse(value.encode(), server.RunRequest)
        assert (caught.value.status, caught.value.code) == (400, "invalid_request")


@pytest.mark.parametrize("authenticated", [False, True])
async def test_duplicate_content_length_gate_precedes_dispatch_and_body(
    authenticated: bool,
) -> None:
    calls = []
    sent = []

    async def unexpected(*args: object) -> None:
        calls.append(args)
        raise AssertionError("must not dispatch or consume body")

    async def send(message: dict) -> None:
        sent.append(message)

    headers = [(b"content-length", b"1"), (b"content-length", b"9999999999999")]
    if authenticated:
        headers.append((b"authorization", f"Bearer {TOKEN}".encode()))
    await server._Guard(unexpected, TOKEN)({"type": "http", "headers": headers}, unexpected, send)
    assert calls == []
    assert sent[0]["status"] == (400 if authenticated else 401)
    assert json.loads(sent[1]["body"]) == {
        "error": "invalid_request" if authenticated else "unauthorized"
    }
