"""Run the live TypeScript client e2e against a real, short-lived local server.

Synthetic fixture only: a fresh temporary workspace and signing key, a random
bearer token, a free loopback port, the reviewed Lectern content-recommender
fixture, and ``client-ts/scripts/e2e.mjs``. The server is always stopped. Build
the client first (``npm run build --prefix client-ts``).
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lectern"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int, server: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("server_exited")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server_start_timeout")


def main() -> int:
    source = json.loads((EXAMPLE / "profiles" / "content-recommender.json").read_text())
    # Windows can hold the stopped server's SQLite handle for a moment; cleanup of
    # the throwaway directory is not what this check verifies.
    with tempfile.TemporaryDirectory(prefix="ae2e", ignore_cleanup_errors=True) as raw:
        tmp = Path(raw)
        profile = dict(source)
        profile["workspace"] = str(tmp / "ws")
        profile["models"] = {
            "fake": {
                "provider": "fake",
                "fixture": str(EXAMPLE / "fixtures" / "content-responses.json"),
            }
        }
        profile["floors"] = {
            name: {**floor, "path": str((EXAMPLE / "profiles" / floor["path"]).resolve())}
            for name, floor in source["floors"].items()
        }
        profile_path = tmp / "profile.json"
        profile_path.write_text(json.dumps(profile))
        port = _free_port()
        env = {
            **os.environ,
            "ARCHITECT_API_TOKEN": secrets.token_urlsafe(48),
            "ARCHITECT_SIGNING_KEY_FILE": str(tmp / "signing.key"),
        }
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "architect.server",
                "--profile",
                str(profile_path),
                "--port",
                str(port),
            ],
            cwd=tmp,
            env=env,
        )
        try:
            _wait_listening(port, server, 60.0)
            client_env = {
                **env,
                "ARCHITECT_BASE_URL": f"http://127.0.0.1:{port}",
                "ARCHITECT_FIXTURE_SPEC": str(EXAMPLE / "specs" / "content-recommender.json"),
                "ARCHITECT_FIXTURE_INPUT": str(
                    EXAMPLE / "fixtures" / "content-recommender-input.json"
                ),
                "ARCHITECT_FIXTURE_AUTHORIZED": "1",
            }
            expected = tmp / "expected.json"
            expected.write_text(
                json.dumps({"content_id": "fractions-practice", "review_required": True})
            )
            client_env["ARCHITECT_FIXTURE_EXPECTED"] = str(expected)
            return subprocess.run(
                ["node", "scripts/e2e.mjs"], cwd=ROOT / "client-ts", env=client_env
            ).returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            time.sleep(1.0)  # let Windows release the store's file handles


if __name__ == "__main__":
    raise SystemExit(main())
