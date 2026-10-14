"""W6 review (camera-ready desktop): the held-out rule on paths the W6 tests do not drive.

The W6 tests show that a build sends the app nothing from the exam: the observers carry
practice data only and the result card carries the signed counts after ``compile()``
returns, with no verification id. The agents screen's "Full report" button (and
``/report <agent>`` typed in the composer) is another way into the same bridge: it prints
the signed report as rows, and the hub keeps them in its replay log.

The report tool now leaves the verification id out (the defect found in review, fixed).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).parents[1]
ROOT = TESTS.parent

# One build through the bridge (as tests/desktop_build_driver.py runs it), then the agents
# screen's "Full report" button: ``send`` with ``/report <name>``, as AgentsScreen does.
DRIVER = textwrap.dedent(
    """
    import asyncio, json, sys, time
    from pathlib import Path

    from desktop_build_driver import DESCRIPTION, NAME, fingerprint_exam_cases, verification_ids
    from forge_support import SECRET
    from session_support import fake_profile, response
    from test_agent_build import INTERVIEW, spec_json
    from test_agent_conductor import Scripted, calls
    from test_desktop_bridge import Wire, call

    from architect.desktop.bridge import Bridge


    async def finished(bridge, wire, count, seconds=90):
        asked = set()
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            for ask in wire.asks():
                if ask["id"] not in asked:
                    asked.add(ask["id"])
                    bridge.frontend.answer(int(ask["id"]), "")
            done = [line for line in wire.lines if line.get("type") == "turn_finished"]
            if len(done) >= count:
                return True
            await asyncio.sleep(0.02)
        return False


    async def main(root):
        profile = fake_profile(root, case_metadata={"private": SECRET})
        fingerprint_exam_cases(profile)
        (profile.parent / "inference.json").write_text(
            json.dumps([response(INTERVIEW), response(spec_json())]), encoding="utf-8")
        wire = Wire()
        bridge = Bridge(wire.write, adapter=Scripted(
            calls(("make", {"description": DESCRIPTION})), '{"text": "Built it."}'))
        await call(bridge, 1, "boot", cwd=str(root), workspace=str(root / "w"))
        await call(bridge, 2, "set_override", key="engineer", value="preset")
        await call(bridge, 3, "set_state", state="forge")
        await call(bridge, 4, "send", text=DESCRIPTION)
        built = await finished(bridge, wire, 1)
        mark = len(wire.lines)
        await call(bridge, 5, "send", text=f"/report {NAME}")
        reported = await finished(bridge, wire, 2)
        await bridge.close()
        after = wire.lines[mark:]
        sent = "\\n".join(json.dumps(line) for line in after)
        issued = verification_ids(root)
        return {
            "built": built,
            "reported": reported,
            "issued": issued,
            "row_titles": [e.get("title") for e in wire.events("rows")],
            "sent_ids": [value for value in issued if value in sent],
            "sent_key": "verification_id" in sent,
        }


    print(json.dumps(asyncio.run(main(Path(sys.argv[1]))), default=str))
    """
)


@pytest.fixture(scope="module")
def full_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("report")
    script = root / "drive.py"
    script.write_text(DRIVER, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(TESTS), str(ROOT)])}
    done = subprocess.run(
        [sys.executable, str(script), str(root / "run")],
        capture_output=True, text=True, timeout=240, env=env, cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    found: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return found


def test_the_full_report_runs_through_the_bridge(full_report: dict[str, Any]) -> None:
    """The setup the next test relies on: a licensed build, then /report printed rows."""
    assert full_report["built"] is True and full_report["reported"] is True
    assert full_report["issued"], "Stage 4 issued no report"
    assert "Test report" in full_report["row_titles"], full_report["row_titles"]


def test_the_full_report_sends_the_app_no_verification_id(full_report: dict[str, Any]) -> None:
    """The report tool leaves the verification id out, as the agent page does, so neither
    the rows the app prints nor the hub's replay log ever hold it."""
    assert full_report["sent_ids"] == []
    assert full_report["sent_key"] is False
