"""Scripted-model exit through the real command interface and upstream Pi."""
import asyncio
import json
import sys
from pathlib import Path


async def test_real_pi_factory_registry_cli_and_game_module(tmp_path: Path) -> None:
    script = Path(__file__).parent.parent / "scripts" / "phase5_exit.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(script), "--workspace", str(tmp_path / "exit"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode(errors="replace")
    summary = json.loads(output)
    assert summary["mode"] == "offline_scripted_model_real_pi"
    assert summary["live_provider"] is False
    assert summary["registry_cli_passed"] is True
    assert summary["reuse_fresh_verification"] is True
    assert summary["dogfood_native_tests"] == 12
