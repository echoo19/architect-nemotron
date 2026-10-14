"""Manufacture a brand-new ARC-AGI-3 harness from scratch and run it.

Every invocation starts from nothing: a fresh temporary workspace, signing key,
registry and library, and a fresh random draw of public ARC-AGI-3 games split
into the five manifest roles. Architect then infers the Spec, the operator
approval is applied explicitly, Stage 4 licenses the harness on official OFFLINE
scorecards of held-out public games, the registry installs it, and the installed
harness plays the delivery games. Nothing is reused between runs.

Public game files are downloaded over HTTP with the anonymous public key (never
imported here; only the official worker process runs game code) into
``--cache`` (default ``.architect/arc-public``, gitignored) and are never
committed. ``--adapter fixture`` is a deterministic no-cost agent that only
exercises manufacturing and official scoring: its scores are not ARC results,
and ``--floor-limit`` defaults to 0 for that reason. ``--adapter anthropic``
plays with a live model (needs ``ANTHROPIC_API_KEY``; spends real money up to
``--cost-usd``); ``--adapter claude-code`` uses the logged-in Claude Code CLI.
Both cloud adapters set the host-side ``allow_online_model`` opt-in, so the
summary says ``"model_binding": "online_preview"``: a cloud model breaks the
no-internet evaluation rule, so such a run is a harness test only. No run is a
competition submission or compliance evidence.

    uv run --extra arc python scripts/arc_harness.py --games 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from architect.games.public import (
    DEFAULT_CACHE,
    LOCAL_PRICES,
    ONLINE_ADAPTERS,
    ROLES,
    FixtureAgent,
    PreviewOptions,
    build,
    close_workspace,
    fetch_public_games,
    stage,
)
from architect.games.public import manufacture as manufacture_preview

ROOT = Path(__file__).resolve().parents[1]
__all__ = [
    "LOCAL_PRICES",
    "ONLINE_ADAPTERS",
    "ROLES",
    "FixtureAgent",
    "PreviewOptions",
    "build",
    "close_workspace",
    "fetch_public_games",
    "main",
    "manufacture",
    "options_from_args",
    "stage",
]


def options_from_args(args: argparse.Namespace) -> PreviewOptions:
    """The parsed argv as ``PreviewOptions``; flags a caller omits keep their defaults."""
    names = PreviewOptions.model_fields
    return PreviewOptions.model_validate(
        {name: getattr(args, name) for name in names if hasattr(args, name)}
    )


async def manufacture(args: argparse.Namespace, tmp: Path) -> dict[str, object]:
    """Script entry point kept for callers: ``architect.games.public.manufacture``."""
    return await manufacture_preview(options_from_args(args), tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--games", type=int, default=5, help="public games drawn per run")
    parser.add_argument("--seed", type=int, help="game draw; random and recorded by default")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--adapter", choices=("fixture", "anthropic", "claude-code", "ollama"), default="fixture"
    )
    parser.add_argument(
        "--prices", type=Path, default=LOCAL_PRICES, help="price table for --adapter ollama"
    )
    parser.add_argument(
        "--call-timeout-s", type=float, default=300.0, help="per-call timeout for --adapter ollama"
    )
    parser.add_argument(
        "--num-ctx", type=int, default=65_536, help="fixed context size for --adapter ollama"
    )
    parser.add_argument(
        "--think",
        choices=("on", "off", "default"),
        default="default",
        help="thinking switch for --adapter ollama (thinking-capable models only)",
    )
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--floor-limit", type=float, default=0.0)
    parser.add_argument("--cost-usd", type=float, default=1.0)
    parser.add_argument("--compute-s", type=float, default=900.0)
    parser.add_argument("--max-actions", type=int, default=24)
    parser.add_argument("--out", type=Path, help="also write the JSON summary here")
    parser.add_argument(
        "--offline", action="store_true", help="use the cached public games; no network"
    )
    args = parser.parse_args()
    args.cache = args.cache.resolve()
    args.think = {"on": True, "off": False, "default": None}[args.think]
    if args.adapter == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"error": "missing_anthropic_api_key"}), file=sys.stderr)
        return 2
    # Short root: the registry refuses long workspace paths on stock Windows.
    with tempfile.TemporaryDirectory(prefix="arch") as raw:
        (Path(raw) / "games").mkdir()
        env_key = os.environ.get("ARCHITECT_SIGNING_KEY_FILE")
        os.environ["ARCHITECT_SIGNING_KEY_FILE"] = str(Path(raw) / "signing.key")
        try:
            summary = asyncio.run(manufacture(args, Path(raw)))
        finally:
            if env_key is None:
                os.environ.pop("ARCHITECT_SIGNING_KEY_FILE", None)
            else:
                os.environ["ARCHITECT_SIGNING_KEY_FILE"] = env_key
        # Report before the workspace is removed: the summary is the product, and
        # a cleanup failure must be visible after it, never instead of it.
        text = json.dumps(summary, indent=2)
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
        print(text)
    return 0 if summary.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
