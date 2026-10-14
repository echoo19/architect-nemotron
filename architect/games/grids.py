"""Shared pure grid functions for the host game strategy and the sandbox builtins.

One implementation serves both sides so a harness sees exactly the host facts.
Coordinates are ``x`` = column and ``y`` = row, as in ``GameAction``. Every
function is pure and deterministic; none reads a provider or a session.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

__all__ = [
    "GRID_CELL_LIMIT",
    "ClockBands",
    "GridError",
    "count_colors",
    "crop",
    "find_cells",
    "grid_diff",
    "grid_equal",
    "grid_text",
    "objects",
    "validate_grid",
]

GRID_SIDE_LIMIT = 64
GRID_CELL_LIMIT = 4096
OBJECT_LIMIT = 256
_HEX = "0123456789abcdef"
_INT_ONLY = frozenset({int})


class GridError(ValueError):
    """A value is not a rectangular grid of int 0..15, at most 64x64 (``sandbox_type``)."""

    def __init__(self) -> None:
        super().__init__("sandbox_type")


def validate_grid(value: object) -> list[list[int]]:
    """Return ``value`` as a grid, or raise ``GridError`` (the ``games.types.Grid`` rule)."""
    if type(value) not in (list, tuple):
        raise GridError
    rows = list(value)  # type: ignore[call-overload]
    if not 1 <= len(rows) <= GRID_SIDE_LIMIT:
        raise GridError
    width = -1
    result: list[list[int]] = []
    for row in rows:
        if type(row) not in (list, tuple):
            raise GridError
        cells = list(row)
        if not 1 <= len(cells) <= GRID_SIDE_LIMIT or (width >= 0 and len(cells) != width):
            raise GridError
        width = len(cells)
        # Native passes per row (exact int, never bool; 0..15): no per-cell Python loop.
        if set(map(type, cells)) != _INT_ONLY or min(cells) < 0 or max(cells) > 15:
            raise GridError
        result.append(cells)
    return result


def grid_diff(
    before: Sequence[Sequence[int]], after: Sequence[Sequence[int]], limit: int = 4096
) -> JsonValue:
    """Cells that changed and their bounding box [x0, y0, x1, y1] (x column, y row).

    Moved verbatim from ``games/execution.py``: the output is identical for the
    host. ``limit`` (0..4096) is accepted for the sandbox signature; the verbatim
    summary never lists more than a count and a box, so it is always within it.
    """
    if type(limit) is not int or not 0 <= limit <= GRID_CELL_LIMIT:
        raise GridError
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


def grid_text(grid: Sequence[Sequence[int]]) -> str:
    """One lowercase hex digit per cell, rows joined by a newline."""
    return "\n".join("".join(map(_HEX.__getitem__, row)) for row in grid)


def objects(grid: Sequence[Sequence[int]], background: int = 0) -> list[JsonValue]:
    """4-connected same-colour components other than ``background``, in scan order.

    At most 256 components; each is ``{"color", "cells", "x0", "y0", "x1", "y1",
    "cx", "cy"}`` with the centroid rounded to 2 decimals.
    """
    if type(background) is not int or not 0 <= background <= 15:
        raise GridError
    height = len(grid)
    width = len(grid[0]) if height else 0
    seen = [[False] * width for _ in range(height)]
    found: list[JsonValue] = []
    for y0 in range(height):
        for x0 in range(width):
            color = grid[y0][x0]
            if seen[y0][x0] or color == background:
                continue
            if len(found) >= OBJECT_LIMIT:
                return found
            seen[y0][x0] = True
            stack = [(x0, y0)]
            count = sum_x = sum_y = 0
            left, top, right, bottom = x0, y0, x0, y0
            while stack:
                x, y = stack.pop()
                count += 1
                sum_x += x
                sum_y += y
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and not seen[ny][nx]
                        and grid[ny][nx] == color
                    ):
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            found.append(
                {
                    "color": color,
                    "cells": count,
                    "x0": left,
                    "y0": top,
                    "x1": right,
                    "y1": bottom,
                    "cx": round(sum_x / count, 2),
                    "cy": round(sum_y / count, 2),
                }
            )
    return found


def count_colors(grid: Sequence[Sequence[int]]) -> dict[str, JsonValue]:
    """``{"<color>": n}`` for every colour present, in ascending colour order."""
    counts = [0] * 16
    for row in grid:
        for cell in row:
            counts[cell] += 1
    return {str(color): n for color, n in enumerate(counts) if n}


def find_cells(grid: Sequence[Sequence[int]], color: int) -> list[JsonValue]:
    """``[[x, y]]`` of every cell with ``color`` in scan order, at most 4096."""
    if type(color) is not int or not 0 <= color <= 15:
        raise GridError
    found: list[JsonValue] = []
    for y, row in enumerate(grid):
        for x, cell in enumerate(row):
            if cell == color:
                found.append([x, y])
                if len(found) >= GRID_CELL_LIMIT:
                    return found
    return found


def crop(grid: Sequence[Sequence[int]], x0: int, y0: int, x1: int, y1: int) -> list[JsonValue]:
    """Inclusive bounds, clipped to the grid; an empty crop is ``[]``."""
    if any(type(bound) is not int for bound in (x0, y0, x1, y1)):
        raise GridError
    height = len(grid)
    width = len(grid[0]) if height else 0
    left, top = max(0, x0), max(0, y0)
    right, bottom = min(width - 1, x1), min(height - 1, y1)
    if left > right or top > bottom:
        return []
    return [list(grid[y][left : right + 1]) for y in range(top, bottom + 1)]


def grid_equal(a: Sequence[Sequence[int]], b: Sequence[Sequence[int]]) -> bool:
    """Same shape and the same colour in every cell."""
    return len(a) == len(b) and all(
        len(row_a) == len(row_b) and list(row_a) == list(row_b)
        for row_a, row_b in zip(a, b, strict=True)
    )


Frame = tuple[tuple[int, ...], ...]


class ClockBands:
    """Border rows and columns that change on almost every step, whatever the action.

    Many public ARC-AGI-3 games draw a step or energy bar along an edge that advances
    by a cell per action. Left in, it makes every frame new and every no-op a change.
    A row or column within ``band`` cells of an edge that changed on at least ``rate``
    of the same-size steps seen (after ``min_steps``) is a clock; ``mask`` blanks it
    so revisits and diffs describe the game, not the clock. Pure and deterministic.
    """

    def __init__(self, *, band: int = 3, rate: float = 0.8, min_steps: int = 5) -> None:
        self.band, self.rate, self.min_steps = band, rate, min_steps
        self.steps = 0
        self._rows: dict[int, int] = {}
        self._cols: dict[int, int] = {}
        self.rows: frozenset[int] = frozenset()
        self.cols: frozenset[int] = frozenset()

    def update(self, before: Frame, after: Frame) -> bool:
        """Count one step's changed border lines; True when the detected bands changed."""
        if len(before) != len(after) or any(
            len(a) != len(b) for a, b in zip(before, after, strict=True)
        ):
            return False
        height, width = len(after), len(after[0])
        band = self.band
        rows: set[int] = set()
        cols: set[int] = set()
        for y, (old, new) in enumerate(zip(before, after, strict=True)):
            if old == new:
                continue
            edge_row = y < band or y >= height - band
            for x, (a, b) in enumerate(zip(old, new, strict=True)):
                if a != b:
                    if edge_row:
                        rows.add(y)
                    if x < band or x >= width - band:
                        cols.add(x)
        self.steps += 1
        for y in rows:
            self._rows[y] = self._rows.get(y, 0) + 1
        for x in cols:
            self._cols[x] = self._cols.get(x, 0) + 1
        if self.steps < self.min_steps:
            return False
        limit = self.rate * self.steps
        rows_now = frozenset(y for y, n in self._rows.items() if n >= limit)
        cols_now = frozenset(x for x, n in self._cols.items() if n >= limit)
        changed = (rows_now, cols_now) != (self.rows, self.cols)
        self.rows, self.cols = rows_now, cols_now
        return changed

    def mask(self, frame: Frame) -> Frame:
        """``frame`` with clock rows and columns set to 0 (itself when there is no clock)."""
        if not self.rows and not self.cols:
            return frame
        cols = self.cols
        return tuple(
            tuple(0 for _ in row)
            if y in self.rows
            else (tuple(0 if x in cols else v for x, v in enumerate(row)) if cols else row)
            for y, row in enumerate(frame)
        )
