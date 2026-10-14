"""Model-free probe of the cached public ARC-AGI-3 games (OFFLINE, CPU only).

Plays every cached public game with a scripted policy and reports what the official
OFFLINE scorecard, the level counter and the state graph say. No model is called, so
the numbers are a floor for what exploration alone reaches within an action budget,
and a check on which exploration patterns a harness should build in. Never a
benchmark or competition result.

    uv run --extra arc python scripts/arc_probe.py --policy explorer --actions 80
    uv run --extra arc python scripts/arc_probe.py --policy random --actions 80 --json

Policies:
- ``random``: a seeded uniform choice over the offered actions; ACTION6 clicks a
  seeded cell of a non-background object.
- ``explorer``: novelty first. In each state (a hash of the last frame) it tries each
  untried option once: every offered simple action and a click on the centre of each
  distinct object (connected non-background cells, largest first, at most 24). When a
  state has no untried option it walks the shortest known path to a state that has one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import sys
import tempfile
import time
from collections import Counter, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from architect.games.official import OfficialConfig, OfficialGameProvider  # noqa: E402
from architect.games.public import DEFAULT_CACHE, _complete  # noqa: E402
from architect.games.types import GameAction, GameLimits, GameRef, Observation  # noqa: E402

Option = tuple[str, int | None, int | None]
Frame = tuple[tuple[int, ...], ...]
MAX_OBJECTS = 24


BAND = 3  # a clock lives within this many rows or columns of the frame edge
CLOCK_RATE = 0.8
CLOCK_MIN_STEPS = 5


class Clock:
    """Border rows and columns that change on almost every step, whatever the action.

    Many public games draw a step or energy bar along an edge that advances by a cell
    per action. Hashing it makes every state look new and every no-op look like a
    change, so it is masked from state keys and effect checks once detected.
    """

    def __init__(self) -> None:
        self.steps = 0
        self.rows: Counter[int] = Counter()
        self.cols: Counter[int] = Counter()
        self.masked_rows: frozenset[int] = frozenset()
        self.masked_cols: frozenset[int] = frozenset()

    def update(self, before: Frame, after: Frame) -> None:
        if len(before) != len(after) or len(before[0]) != len(after[0]):
            return
        height, width = len(after), len(after[0])
        self.steps += 1
        rows, cols = set(), set()
        for y in range(height):
            if y >= BAND and y < height - BAND:
                border_cols = [x for x in range(width) if x < BAND or x >= width - BAND]
                for x in border_cols:
                    if before[y][x] != after[y][x]:
                        cols.add(x)
                continue
            for x in range(width):
                if before[y][x] != after[y][x]:
                    rows.add(y)
                    if x < BAND or x >= width - BAND:
                        cols.add(x)
        self.rows.update(rows)
        self.cols.update(cols)
        if self.steps >= CLOCK_MIN_STEPS:
            limit = CLOCK_RATE * self.steps
            self.masked_rows = frozenset(r for r, n in self.rows.items() if n >= limit)
            self.masked_cols = frozenset(c for c, n in self.cols.items() if n >= limit)

    def key(self, observation: Observation) -> str:
        frame = observation.frames[-1]
        digest = hashlib.sha256()
        for y, row in enumerate(frame):
            if y in self.masked_rows:
                continue
            if self.masked_cols:
                row = tuple(v for x, v in enumerate(row) if x not in self.masked_cols)
            digest.update(bytes(row))
        digest.update(str(observation.levels_completed).encode())
        return digest.hexdigest()[:16]


def _state(observation: Observation, clock: Clock | None = None) -> str:
    return (clock or Clock()).key(observation)


def _objects(frame: tuple[tuple[int, ...], ...]) -> list[tuple[int, int]]:
    """Centres (x, y) of connected same-colour regions that are not the background."""
    height, width = len(frame), len(frame[0])
    background = Counter(value for row in frame for value in row).most_common(1)[0][0]
    seen = [[False] * width for _ in range(height)]
    regions: list[tuple[int, int, int]] = []
    for y in range(height):
        for x in range(width):
            if seen[y][x] or frame[y][x] == background:
                continue
            colour, queue, cells = frame[y][x], deque([(x, y)]), []
            seen[y][x] = True
            while queue:
                cx, cy = queue.popleft()
                cells.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx]:
                        if frame[ny][nx] == colour:
                            seen[ny][nx] = True
                            queue.append((nx, ny))
            # The cell nearest the mean, so a click lands inside non-convex shapes.
            mx = sum(c[0] for c in cells) / len(cells)
            my = sum(c[1] for c in cells) / len(cells)
            px, py = min(cells, key=lambda c: (c[0] - mx) ** 2 + (c[1] - my) ** 2)
            regions.append((len(cells), px, py))
    regions.sort(key=lambda item: (-item[0], item[2], item[1]))
    return [(x, y) for _, x, y in regions[:MAX_OBJECTS]]


def _options(observation: Observation) -> list[Option]:
    options: list[Option] = []
    for name in observation.available_actions:
        if name == "RESET":
            continue
        if name == "ACTION6":
            options.extend(("ACTION6", x, y) for x, y in _objects(observation.frames[-1]))
        else:
            options.append((name, None, None))
    return options


class Explorer:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.tried: dict[str, set[Option]] = {}
        self.edges: dict[str, dict[Option, str]] = {}
        self.untried: dict[str, list[Option]] = {}
        self.effect: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _kind(option: Option) -> str:
        return option[0] if option[0] != "ACTION6" else f"ACTION6@{option[1]},{option[2]}"

    def observe(self, state: str, observation: Observation) -> None:
        if state not in self.untried:
            self.untried[state] = _options(observation)
            self.tried[state] = set()
            self.edges.setdefault(state, {})

    def record(self, before: str, option: Option, after: str) -> None:
        self.tried[before].add(option)
        self.edges[before][option] = after
        tries, hits = self.effect.get(self._kind(option), (0, 0))
        self.effect[self._kind(option)] = (tries + 1, hits + (after != before))

    def choose(self, state: str) -> Option:
        fresh = [item for item in self.untried[state] if item not in self.tried[state]]
        if fresh:
            # Options that changed the (clock-masked) state elsewhere first; seeded ties.
            def rank(item: Option) -> tuple[float, float]:
                tries, hits = self.effect.get(self._kind(item), (0, 0))
                return (-(hits + 1) / (tries + 2), self.rng.random())
            return min(fresh, key=rank)
        # Shortest known path to a state with an untried option; take its first step.
        queue: deque[tuple[str, Option | None]] = deque([(state, None)])
        visited = {state}
        while queue:
            node, first = queue.popleft()
            for option, target in self.edges.get(node, {}).items():
                if target in visited:
                    continue
                visited.add(target)
                step = first or option
                if any(item not in self.tried[target] for item in self.untried.get(target, [])):
                    return step
                queue.append((target, step))
        options = self.untried[state]
        return self.rng.choice(options) if options else ("RESET", None, None)


class Random:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def observe(self, state: str, observation: Observation) -> None:
        self.current = _options(observation)

    def record(self, before: str, option: Option, after: str) -> None:
        pass

    def choose(self, state: str) -> Option:
        return self.rng.choice(self.current) if self.current else ("RESET", None, None)


async def play(provider: OfficialGameProvider, ref: GameRef, policy_name: str, actions: int,
               seed: int) -> dict[str, object]:
    limits = GameLimits(max_actions=actions, max_turns=actions)
    started = time.monotonic()
    session = await provider.open(ref, limits=limits)
    policy = Explorer(seed) if policy_name == "explorer" else Random(seed)
    observation = await session.observe()
    clock = Clock()
    states: set[str] = set()
    visits: Counter[str] = Counter()
    resets = 0
    best_levels = observation.levels_completed
    first_level_at: int | None = None
    try:
        while session.actions_used < actions and observation.state not in ("WIN",):
            state = clock.key(observation)
            states.add(state)
            visits[state] += 1
            if observation.state == "GAME_OVER" or observation.state == "NOT_PLAYED":
                resets += 1
                observation = await session.step(GameAction(name="RESET"))
                continue
            policy.observe(state, observation)
            option = policy.choose(state)
            name, x, y = option
            action = GameAction(name=name, x=x, y=y)  # type: ignore[arg-type]
            before = observation.frames[-1]
            observation = await session.step(action)
            clock.update(before, observation.frames[-1])
            policy.record(state, option, clock.key(observation))
            if observation.levels_completed > best_levels:
                best_levels = observation.levels_completed
                if first_level_at is None:
                    first_level_at = session.actions_used
        score = await session.finalize()
    finally:
        await session.close()
    steps = sum(visits.values())
    return {
        "game": ref.game_id,
        "levels": best_levels,
        "win_levels": observation.win_levels,
        "first_level_at": first_level_at,
        "score": score.normalized,
        "score_source": score.source,
        "distinct_states": len(states),
        "revisit_rate": round(1 - len(states) / steps, 3) if steps else None,
        "resets": resets,
        "clock_rows": sorted(clock.masked_rows),
        "clock_cols": sorted(clock.masked_cols),
        "actions": session.actions_used,
        "wall_s": round(time.monotonic() - started, 1),
    }


def stage_games(cache: Path, families: str, staged: Path) -> list[GameRef]:
    """Copy the complete cached games (optionally only some families) into ``staged``."""
    games = sorted(path.parent for path in cache.rglob("metadata.json") if _complete(path.parent))
    if families:
        wanted = set(families.split(","))
        games = [game for game in games if game.parent.name in wanted]
    refs = []
    for directory in games:
        game_id = _complete(directory)
        assert game_id is not None
        shutil.copytree(directory, staged / game_id, symlinks=False)
        refs.append(GameRef(backend="official_offline", game_id=game_id, revision=directory.name))
    return refs


async def sweep(
    args: argparse.Namespace, staged: Path, refs: list[GameRef]
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    provider = OfficialGameProvider(OfficialConfig(mode="offline", staged_game_dir=staged))
    try:
        for ref in refs:
            try:
                row = await play(provider, ref, args.policy, args.actions, args.seed)
            except Exception as exc:  # a probe row, never a crash of the whole sweep
                row = {"game": ref.game_id, "error": type(exc).__name__}
            results.append(row)
            if not args.json:
                print(json.dumps(row), flush=True)
    finally:
        await provider.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--policy", choices=("random", "explorer"), default="explorer")
    parser.add_argument("--actions", type=int, default=80)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--games", default="", help="comma-separated game families, e.g. ls20,ft09")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--tmp", default="C:/t" if sys.platform == "win32" else None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.tmp:
        Path(args.tmp).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arcp-", dir=args.tmp) as staged_root:
        staged = Path(staged_root)
        refs = stage_games(Path(args.cache), args.games, staged)
        results = asyncio.run(sweep(args, staged, refs))
    ok = [row for row in results if "error" not in row]

    def mean(key: str, digits: int) -> float | None:
        values = [float(row[key] or 0) for row in ok]  # type: ignore[arg-type]
        return round(sum(values) / len(values), digits) if values else None

    summary = {
        "policy": args.policy, "actions": args.actions, "seed": args.seed,
        "games": len(results), "errors": len(results) - len(ok),
        "games_with_a_level": sum(1 for row in ok if row["levels"]),
        "levels": sum(int(row["levels"]) for row in ok),  # type: ignore[call-overload]
        "mean_score": mean("score", 4),
        "mean_distinct_states": mean("distinct_states", 1),
    }
    if args.json:
        print(json.dumps({"summary": summary, "games": results}))
    else:
        print(json.dumps({"summary": summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
