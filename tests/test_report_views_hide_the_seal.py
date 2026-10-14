"""The Ed25519 seal never shows in the report tool or the agent page (W6 with W7).

On a host that holds an Ed25519 key, Stage 4 seals every report next to its HMAC (W7).
The report tool is what the conductor's model gets, and what ``/report`` and the agents
screen's "Full report" button print as rows (the hub keeps those in its replay log). The
agent page is the desktop's ``agent`` read. Both show the report's measured fields only:
the seal, the HMAC and the verification id stay in report.json on disk. Offline: fake
profile, scripted conversation model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_desktop_bridge import call, idle, started
from test_desktop_bridge_more import install, ok

from architect import trust

# Keys that would carry a credential if a view copied the report as written.
CREDENTIAL_KEYS = ("ed25519", "signature", "verification_id")


def _sealed_report(directory: Path) -> dict[str, str]:
    """The installed report's credentials, as written on disk."""
    data = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    seal = data["ed25519"]
    return {
        "seal signature": seal["signature"],
        "seal fingerprint": seal["fingerprint"],
        "hmac": data["signature"],
        "verification id": data["verification_id"],
    }


def _shown(value: Any, credentials: dict[str, str]) -> list[str]:
    """Each credential value, and each credential key, that ``value`` holds anywhere."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    found = [name for name, secret in credentials.items() if secret in text]
    return found + [key for key in CREDENTIAL_KEYS if json.dumps(key) in text]


async def test_the_report_tool_and_the_agent_page_never_show_the_seal(tmp_path: Path) -> None:
    # The host's Ed25519 key beside its HMAC key (conftest's temporary authority), and the
    # public half in the host's own trust list, as `architect keys` leaves them.
    public = trust.generate_key(trust.private_key_path())
    trust.add_trusted_key(public, "bench", trust.trust_file_path())
    bridge, wire = await started(tmp_path)
    try:
        await install(bridge, "alpha")
        assert bridge.app is not None and bridge.repl is not None
        directory = bridge.app.services.registry.status("alpha").directory
        sealed = _sealed_report(directory)
        assert sealed["seal fingerprint"] == trust.fingerprint(public)  # it is sealed

        # The report tool, as the conductor's model would call it.
        conductor = bridge.repl.conductor
        tool = next(item for item in conductor.tools if item.name == "report")
        result = await tool.run({"name": "alpha"}, conductor.context)
        assert isinstance(result, dict) and isinstance(result["report"], dict)
        assert result["licensed"] is True and result["report"]["passed"] is True
        assert _shown(result, sealed) == []

        # The agents screen: its rows, then the agent page.
        await call(bridge, 2, "agents")
        assert _shown(ok(wire, 2), sealed) == []
        await call(bridge, 3, "agent", name="alpha")
        page = ok(wire, 3)
        assert page["licensed"] is True and page["report"]["passed"] is True
        assert _shown(page, sealed) == []

        # "Full report" (``/report alpha``): every line the app gets, rows included.
        mark = len(wire.lines)
        await call(bridge, 4, "send", text="/report alpha")
        await idle(bridge)
        titles = [event.get("title") for event in wire.events("rows")]
        assert "Test report" in titles, titles
        assert _shown(wire.lines[mark:], sealed) == []
    finally:
        await bridge.close()
