"""Shared grid functions: host parity for grid_diff, bounds, and speed on 64x64 grids."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from architect.games import grids
from architect.games.local import LocalGameProvider
from architect.games.types import GameAction, GameLimits


def reference_grid_diff(
    before: tuple[tuple[int, ...], ...], after: tuple[tuple[int, ...], ...]
) -> JsonValue:
    """The pre-Phase 9 ``games/execution.py:_grid_diff`` body, copied from commit fdd7c41."""
    if len(before) != len(after) or any(
        len(a) != len(b) for a, b in zip(before, after, strict=True)
    ):
        return {"cells": None, "box": None, "resized": True}
    xs: list[int] = []
    ys: list[int] = []
    for y, (old_row, new_row) in enumerate(zip(before, after, strict=True)):
        for x, (old, new) in enumerate(zip(old_row, new_row, strict=True)):
            if old != new:
                xs.append(x)
                ys.append(y)
    if not xs:
        return {"cells": 0, "box": None}
    return {"cells": len(xs), "box": [min(xs), min(ys), max(xs), max(ys)]}


def random_grid(seed: int, width: int = 64, height: int = 64, colors: int = 16) -> list[list[int]]:
    rng = random.Random(seed)
    return [[rng.randrange(colors) for _ in range(width)] for _ in range(height)]


async def local_frames() -> list[tuple[tuple[int, ...], ...]]:
    """Settled frames from the committed local game fixtures, stepped with every action."""
    provider = LocalGameProvider()
    frames: list[tuple[tuple[int, ...], ...]] = []
    for ref in await provider.discover():
        session = await provider.open(ref, limits=GameLimits(max_actions=8, max_turns=8))
        try:
            observation = await session.observe()
            frames.append(observation.frames[-1])
            for name in observation.available_actions:
                if name in {"RESET", "ACTION6"}:
                    continue
                if observation.state in {"WIN", "GAME_OVER"}:
                    break
                observation = await session.step(GameAction(name=name))
                frames.append(observation.frames[-1])
        finally:
            await session.close()
    return frames


async def test_grid_diff_is_identical_to_the_old_host_function_on_committed_fixtures() -> None:
    frames = await local_frames()
    assert len(frames) >= 4
    for before in frames:
        for after in frames:
            assert grids.grid_diff(before, after) == reference_grid_diff(before, after)
    samples = [tuple(tuple(row) for row in random_grid(seed, 5, 4, 3)) for seed in range(20)]
    samples.append(((0, 0),))
    for before in samples:
        for after in samples:
            assert grids.grid_diff(before, after) == reference_grid_diff(before, after)


def test_host_strategy_imports_the_shared_grid_diff() -> None:
    from architect.games import execution

    before = ((0, 0, 0), (0, 0, 0))
    after = ((0, 5, 0), (0, 0, 7))
    assert execution._grid_diff(before, after) == grids.grid_diff(before, after)
    assert grids.grid_diff(before, after) == {"cells": 2, "box": [1, 0, 2, 1]}
    assert grids.grid_diff(before, ((0, 0),)) == {"cells": None, "box": None, "resized": True}


@pytest.mark.parametrize(
    "value",
    [
        None, 3, "grid", [], [[]], [[16]], [[-1]], [[True]], [[1.0]], [[0, 1], [0]],
        [[0] * 65], [[0]] * 65, [[0], "0"], {"a": 1},
    ],
)
def test_validate_grid_applies_the_games_types_rule(value: object) -> None:
    with pytest.raises(grids.GridError, match="^sandbox_type$"):
        grids.validate_grid(value)


def test_validate_grid_accepts_lists_and_tuples() -> None:
    assert grids.validate_grid(((1, 2), (3, 4))) == [[1, 2], [3, 4]]
    assert grids.validate_grid([[15] * 64] * 64) == [[15] * 64] * 64


def test_grid_text_objects_colours_cells_crop_and_equality() -> None:
    grid = [
        [0, 1, 1, 0],
        [0, 1, 0, 2],
        [3, 0, 0, 2],
    ]
    assert grids.grid_text(grid) == "0110\n0102\n3002"
    assert grids.grid_text([[10, 15]]) == "af"
    assert grids.objects(grid) == [
        {"color": 1, "cells": 3, "x0": 1, "y0": 0, "x1": 2, "y1": 1, "cx": 1.33, "cy": 0.33},
        {"color": 2, "cells": 2, "x0": 3, "y0": 1, "x1": 3, "y1": 2, "cx": 3.0, "cy": 1.5},
        {"color": 3, "cells": 1, "x0": 0, "y0": 2, "x1": 0, "y1": 2, "cx": 0.0, "cy": 2.0},
    ]
    assert [item["color"] for item in grids.objects(grid, 1)] == [0, 0, 0, 2, 3]  # type: ignore[index]
    assert grids.count_colors(grid) == {"0": 6, "1": 3, "2": 2, "3": 1}
    assert grids.find_cells(grid, 2) == [[3, 1], [3, 2]]
    assert grids.crop(grid, 1, 0, 2, 1) == [[1, 1], [1, 0]]
    assert grids.crop(grid, -5, -5, 0, 0) == [[0]]
    assert grids.crop(grid, 3, 2, 99, 99) == [[2]]
    assert grids.crop(grid, 2, 2, 1, 1) == []
    assert grids.crop(grid, 10, 10, 20, 20) == []
    assert grids.grid_equal(grid, [list(row) for row in grid])
    assert not grids.grid_equal(grid, grid[:2])
    assert not grids.grid_equal([[0, 1]], [[0, 2]])
    assert not grids.grid_equal([[0, 1]], [[0]])


def test_grid_function_bounds() -> None:
    checker = [[(x + y) % 2 for x in range(64)] for y in range(64)]
    assert len(grids.objects(checker)) == 256  # 2048 single cells, capped
    assert len(grids.find_cells([[1] * 64] * 64, 1)) == 4096
    with pytest.raises(grids.GridError):
        grids.find_cells(checker, 16)
    with pytest.raises(grids.GridError):
        grids.objects(checker, -1)
    with pytest.raises(grids.GridError):
        grids.crop(checker, 0, 0, True, 1)  # type: ignore[arg-type]
    with pytest.raises(grids.GridError):
        grids.grid_diff(checker, checker, 4097)
    assert grids.grid_diff(checker, checker, 0) == {"cells": 0, "box": None}


@pytest.mark.parametrize(
    "work",
    [
        lambda a, b: grids.grid_diff(a, b),
        lambda a, b: grids.objects(a),
        lambda a, b: grids.grid_text(a),
    ],
    ids=["grid_diff", "objects", "grid_text"],
)
def test_64x64_grid_functions_run_under_50_ms(
    work: Callable[[list[list[int]], list[list[int]]], object],
) -> None:
    a, b = random_grid(1, colors=3), random_grid(2, colors=3)
    work(a, b)  # warm up
    best = min(_timed(work, a, b) for _ in range(5))
    assert best < 0.05


def _timed(work: Callable[[list[list[int]], list[list[int]]], object], a: list[list[int]],
           b: list[list[int]]) -> float:
    started = time.perf_counter()
    work(a, b)
    return time.perf_counter() - started
