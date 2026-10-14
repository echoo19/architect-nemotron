"""Native pure builtins of sandbox language v2, with fuel charging and value bounds.

Every function takes and returns JSON values (plus the opaque ``RangeValue``),
is deterministic and total over its documented domain (task ints are unbounded),
and charges ``1 + ceil(work / 256)`` fuel plus the walk and digit charges below, so
fuel bounds wall time. A wrong type raises the catchable ``DataError``;
resource bounds raise ``Fatal``, which no ``except`` in harness code matches.
No function here can reach a Python object other than the JSON value given.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Final, cast

from architect.games import grids
from architect.runtime.sandbox_language import (
    ARITY,
    BUILTINS,
    CONTAINER_LIMIT,
    DATA_ERROR_CODES,
    FUEL_UNIT,
    INT_BOUND,
    NESTING_LIMIT,
    RANGE_LIMIT,
    STRING_LIMIT,
)

__all__ = [
    "BUILTIN_FUNCTIONS",
    "MUTATING_FUNCTIONS",
    "DataError",
    "Fatal",
    "Meter",
    "RangeValue",
    "SandboxFault",
    "contains",
    "equal",
    "fault",
    "search_charge",
]

PARSE_LIMIT: Final = 128 * 1024
# What may follow a fence opener before its body: a tag, blanks, one line break. It
# always matches and never backtracks; the body scan is ``str.find`` (linear).
_FENCE_TAG = re.compile(r"[A-Za-z]*[ \t]*\r?\n?")
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0", ""})
# Walk charges (the "builtin work and copy charges" of contract Section 4.9). The
# Section 4.6 charge 1 + ceil(work / 256) is a floor; these keep 10M fuel at the
# contracted 5-10 s wherever a call visits more than 256 things per fuel unit.
# Calibrated against one interpreter node (about 0.25 us):
# - a native (C) loop costs 1 per 16 items it visits;
# - a Python-level loop costs 1 per item it visits;
# - equality compares memory: 1 per 4096 characters, charged on the total;
# - ordering compares strings char by char: 1 per 1024 characters, charged on the
#   total characters times the comparisons each item takes part in;
# - parsing JSON costs 1 per 2 input characters, and bracket scanning 1 per 4;
# - substring search (find, count, in, split, replace) costs 1 per 1024 of its
#   worst-case window-times-needle character compares (``search_charge``).
# Digit charges: an int beyond 2**63 (only the task, a capability reply or a preset
# can supply one; harness arithmetic, ``int`` and ``parse_json`` never make one) has
# ``w`` 64-bit words. Rendering it in decimal is quadratic, 1 + w*w/64 fuel (4,300
# digits: about 185 us, charged 778); arithmetic on it costs 1 + (w1 + w2)**2 / 256
# (4,300-digit ``*``, ``//``, ``%``: about 45 us, charged 778), charged before the work.
_ELEMENT_SHIFT: Final = 4
_CHAR_SHIFT: Final = 12
_ORDER_SHIFT: Final = 10
_PARSE_SHIFT: Final = 1
_SCAN_SHIFT: Final = 2
_WORD_SHIFT: Final = 6
_DIGIT_SHIFT: Final = 6
_ARITH_SHIFT: Final = 8
_SEARCH_SHIFT: Final = 10


class SandboxFault(Exception):
    """A runtime failure of harness code, carrying only a fixed code and a line."""

    def __init__(self, code: str, line: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.line = line


class DataError(SandboxFault):
    """A pure data failure; the only kind a harness ``try/except`` catches."""


class Fatal(SandboxFault):
    """A resource, effect or protocol failure; never caught by harness code."""


def fault(code: str) -> SandboxFault:
    return DataError(code) if code in DATA_ERROR_CODES else Fatal(code)


class Meter:
    """The fuel budget of one run."""

    __slots__ = ("limit", "remaining")

    def __init__(self, fuel: int) -> None:
        self.limit = fuel
        self.remaining = fuel

    @property
    def used(self) -> int:
        return self.limit - max(self.remaining, 0)

    def charge(self, units: int) -> None:
        self.remaining -= units
        if self.remaining < 0:
            raise Fatal("sandbox_fuel")

    def work(self, work: int) -> None:
        self.charge(1 + (max(work, 0) + FUEL_UNIT - 1) // FUEL_UNIT)


def native_walk(meter: Meter, items: int) -> None:
    """Charge a native loop over ``items`` things: 1 fuel per 16."""
    meter.charge(items >> _ELEMENT_SHIFT)


def search_charge(meter: Meter, haystack: int, needle: int, passes: int = 1) -> None:
    """Charge the worst case of ``passes`` native substring searches, before they run.

    CPython's fastsearch (3.12) can compare every needle character at every window:
    its plain search does when the needle is under 6 characters, the haystack under
    2,500, or the needle under 100 and the haystack under 30,000; its adaptive search
    (a needle over a third of the haystack) does over its last 2,000 windows; its
    two-way search is linear. The window bound below covers all three, and also the
    repeated searches of ``split`` and ``replace``, which resume after each match and
    so visit disjoint windows. A one-character needle is a linear ``memchr``. Measured
    at about 10 compares per ns: 1 fuel per 1,024 compares is about 0.1 us.
    """
    if needle < 2 or needle > haystack:
        return
    windows = haystack - needle + 1
    if needle >= 100:
        windows = min(windows, 2_500 + 2_048)
    elif needle >= 6:
        windows = min(windows, 30_000)
    meter.charge((passes * windows * needle) >> _SEARCH_SHIFT)


def reverse_search_charge(meter: Meter, haystack: int, needle: int) -> None:
    """Charge the worst case of one native reverse substring search (``rfind``).

    CPython's reverse search has no two-way or adaptive fallback: every window can compare
    the whole needle, so no window bound applies. A one-character needle is a linear scan.
    """
    if needle < 2 or needle > haystack:
        return
    meter.charge(((haystack - needle + 1) * needle) >> _SEARCH_SHIFT)


def range_length(start: int, stop: int, step: int) -> int:
    """``len(range(start, stop, step))`` in exact integer arithmetic.

    ``len`` of a Python range raises ``OverflowError`` past ``sys.maxsize``; task ints
    are unbounded, so the length is computed without it.
    """
    if step > 0:
        return max(0, (stop - start + step - 1) // step)
    return max(0, (start - stop - step - 1) // -step)


class RangeValue:
    """The one opaque non-JSON value: a lazy integer range of at most 100,000 items."""

    __slots__ = ("start", "step", "stop")

    def __init__(self, start: int, stop: int, step: int) -> None:
        self.start, self.stop, self.step = start, stop, step

    def __len__(self) -> int:
        return range_length(self.start, self.stop, self.step)

    def items(self) -> range:
        return range(self.start, self.stop, self.step)


# ---------------------------------------------------------------- value bounds


def check_str(value: str) -> str:
    if len(value) > STRING_LIMIT:
        raise Fatal("sandbox_value_limit")
    return value


def check_size(value: list[object] | dict[str, object]) -> None:
    if len(value) > CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")


def is_number(value: object) -> bool:
    return type(value) in (int, float)


def int_words(value: object) -> int:
    """64-bit words of an int at least 2**63 in magnitude; 0 for every other value."""
    if type(value) is int and not -INT_BOUND < value < INT_BOUND:
        return value.bit_length() >> _WORD_SHIFT
    return 0


def arithmetic_charge(left: object, right: object, meter: Meter) -> None:
    """Charge ``+ - * / // %`` on an int beyond 2**63 before computing it.

    Bounded operands cost nothing extra; a big operand pays for the digit work
    (at worst quadratic) that the result bound would only reject afterwards.
    """
    words = int_words(left) + int_words(right)
    if words:
        meter.charge(1 + ((words * words) >> _ARITH_SHIFT))


def number(value: object) -> int | float:
    """Bound an arithmetic result: ints below 2**63 in magnitude, finite floats."""
    if type(value) is int:
        if -INT_BOUND < value < INT_BOUND:
            return value
        raise DataError("sandbox_value")
    if type(value) is float and math.isfinite(value):
        return value
    raise DataError("sandbox_value")


def store(value: object, meter: Meter, depth: int = 2) -> object:
    """Deep-copy a container placed into another; 1 fuel per copied element.

    Every container slot therefore holds its own tree: no value contains itself
    or shares a subtree, so copying and encoding are linear in real size.
    ``depth`` is the nesting level the value lands at (a top-level value is 1), so
    ``store(items, meter, 1)`` copies a list as a new root, charging every element.
    """
    kind = type(value)
    if kind is list:
        if depth > NESTING_LIMIT:
            raise Fatal("sandbox_value_limit")
        items = cast(list[object], value)
        meter.charge(len(items))
        return [store(item, meter, depth + 1) for item in items]
    if kind is dict:
        if depth > NESTING_LIMIT:
            raise Fatal("sandbox_value_limit")
        mapping = cast(dict[str, object], value)
        meter.charge(len(mapping))
        return {key: store(item, meter, depth + 1) for key, item in mapping.items()}
    if kind is RangeValue:
        raise DataError("sandbox_type")
    return value


def check_json(value: object, depth: int = 0, meter: Meter | None = None) -> int:
    """Refuse a range anywhere and nesting beyond 40 before serializing.

    Returns the number of values visited (the Python-level walk ``canonical`` pays for).
    With a ``meter``, every int beyond 2**63 is charged its quadratic decimal
    conversion as it is visited, before anything is serialized.
    """
    kind = type(value)
    if kind is int:
        words = int_words(value)
        if words and meter is not None:
            meter.charge(1 + ((words * words) >> _DIGIT_SHIFT))
        return 1
    if kind is list or kind is dict:
        if depth >= NESTING_LIMIT:
            raise Fatal("sandbox_value_limit")
        items = cast(dict[str, object], value).values() if kind is dict else cast(
            list[object], value
        )
        visited = 1
        for item in items:
            visited += check_json(item, depth + 1, meter)
        return visited
    if kind is RangeValue:
        raise DataError("sandbox_type")
    return 1


def canonical(value: object, meter: Meter | None = None) -> str:
    """Sorted-key compact JSON, the rendering of ``str``, ``to_json`` and f-strings.

    With a ``meter``, the Python-level walk costs 1 fuel per value visited, and an
    int beyond 2**63 also pays for its digits.
    """
    visited = check_json(value, meter=meter)
    if meter is not None:
        meter.charge(visited)
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise DataError("sandbox_type") from exc


def render(value: object, meter: Meter) -> str:
    """``str(x)``: a str as-is, anything else as canonical JSON."""
    if type(value) is str:
        meter.work(len(value))
        return value
    text = canonical(value, meter)
    meter.work(len(text))
    return check_str(text)


# ---------------------------------------------------------------- comparison


def _str_chars(items: Sequence[object], kinds: set[type], meter: Meter) -> int:
    """Total characters of the strs in ``items``; a mixed list is walked in Python."""
    if kinds == {str}:
        return sum(map(len, cast(Sequence[str], items)))
    meter.charge(len(items))
    return sum(len(item) for item in items if type(item) is str)


def _flat(kinds: set[type]) -> bool:
    return list not in kinds and dict not in kinds


def equal(left: object, right: object, meter: Meter) -> bool:
    """Python ``==`` over JSON values, charging for the elements and characters compared.

    A container holding no container is compared natively; any other container is
    walked in Python at 1 fuel per element, so every compared element is paid for.
    Depth is bounded by construction.
    """
    kind = type(left)
    if kind is list:
        if type(right) is not list:
            return False
        first, second = cast(list[object], left), cast(list[object], right)
        if len(first) != len(second):
            return False
        meter.charge(1 + (len(first) >> _ELEMENT_SHIFT))
        kinds = set(map(type, first))
        if _flat(kinds):
            if str in kinds:
                meter.charge(_str_chars(first, kinds, meter) >> _CHAR_SHIFT)
            return first == second
        meter.charge(len(first))
        return all(equal(a, b, meter) for a, b in zip(first, second, strict=True))
    if kind is dict:
        if type(right) is not dict:
            return False
        one, other = cast(dict[str, object], left), cast(dict[str, object], right)
        if len(one) != len(other):
            return False
        meter.charge(1 + (len(one) >> _ELEMENT_SHIFT))
        native_walk(meter, len(one))  # the native key-set comparison
        if one.keys() != other.keys():
            return False
        values = list(one.values())
        kinds = set(map(type, values))
        if _flat(kinds):
            if str in kinds:
                meter.charge(_str_chars(values, kinds, meter) >> _CHAR_SHIFT)
            return one == other
        meter.charge(len(one))
        return all(equal(value, other[key], meter) for key, value in one.items())
    if type(right) is list or type(right) is dict:
        return False
    if kind is str and type(right) is str:
        meter.charge(min(len(cast(str, left)), len(right)) >> _CHAR_SHIFT)
    return left == right


def contains(needle: object, items: list[object], meter: Meter) -> bool:
    """``needle in items``, charging like ``equal`` for every element compared."""
    meter.work(len(items))
    native_walk(meter, len(items))
    kind = type(needle)
    if kind is list or kind is dict:
        meter.charge(len(items))
        return any(equal(needle, item, meter) for item in items)
    if kind is str:
        # A str equality compares memory only between strs of the needle's length.
        size = len(cast(str, needle))
        if set(map(type, items)) == {str}:
            same = list(map(len, cast(list[str], items))).count(size)
        else:
            same = len(items)
        meter.charge((size * same) >> _CHAR_SHIFT)
    return needle in items


def ordering_charge(values: Sequence[object], comparisons: int, meter: Meter) -> None:
    """Charge ``comparisons`` native orderings among the strs ``values`` for their characters.

    A str comparison walks at most the shorter string, so each item's characters are
    paid once per comparison it takes part in: ``ceil(comparisons / len(values))``.
    Numbers carry no character charge.
    """
    if not values or set(map(type, values)) != {str}:
        return
    chars = sum(map(len, cast(Sequence[str], values)))
    rounds = -(-comparisons // len(values))
    meter.charge((chars * rounds) >> _ORDER_SHIFT)


# ---------------------------------------------------------------- argument guards


def _str(value: object) -> str:
    if type(value) is not str:
        raise DataError("sandbox_type")
    return value


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise DataError("sandbox_type")
    return cast(list[object], value)


def _dict(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise DataError("sandbox_type")
    return cast(dict[str, object], value)


def _int(value: object) -> int:
    if type(value) is not int:
        raise DataError("sandbox_type")
    return value


def _grid(value: object, meter: Meter) -> list[list[int]]:
    """Validate a grid argument; the native per-row checks cost 1 fuel per 16 cells."""
    try:
        grid = grids.validate_grid(value)
    except grids.GridError as exc:
        raise DataError("sandbox_type") from exc
    native_walk(meter, len(grid) * len(grid[0]))
    return grid


def _nonempty(value: object) -> str:
    text = _str(value)
    if not text:
        raise DataError("sandbox_value")
    return text


Builtin = Callable[[Meter, Sequence[object]], object]


# ---------------------------------------------------------------- core builtins


def _len(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    value = args[0]
    if type(value) in (str, list, dict, RangeValue):
        return len(cast(Sequence[object], value))
    raise DataError("sandbox_type")


def _str_builtin(meter: Meter, args: Sequence[object]) -> object:
    return render(args[0], meter)


def _parse_number(value: object, meter: Meter, target: type[int] | type[float]) -> int | float:
    if type(value) is bool:
        meter.work(1)
        return target(value)
    if is_number(value):
        meter.work(1)
        if target is int and not math.isfinite(cast(float, value)):
            raise DataError("sandbox_value")
        try:
            converted = target(cast(float, value))
        except OverflowError:  # float() of an int beyond the float range (task ints)
            raise DataError("sandbox_value") from None
        return number(converted)
    text = _str(value)
    meter.work(len(text))
    if len(text) > 400:
        raise DataError("sandbox_value")
    try:
        parsed = target(text.strip())
    except (ValueError, OverflowError) as exc:
        raise DataError("sandbox_value") from exc
    return number(parsed)


def _int_builtin(meter: Meter, args: Sequence[object]) -> object:
    return _parse_number(args[0], meter, int)


def _float_builtin(meter: Meter, args: Sequence[object]) -> object:
    return _parse_number(args[0], meter, float)


def _bool_builtin(meter: Meter, args: Sequence[object]) -> object:
    value = args[0]
    if type(value) is str:
        text = value
        meter.work(len(text))
        folded = text.strip().lower()
        if folded in _TRUE:
            return True
        if folded in _FALSE:
            return False
        raise DataError("sandbox_value")
    meter.work(1)
    if type(value) is RangeValue:
        raise DataError("sandbox_type")
    return bool(value)


def _abs(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    if not is_number(args[0]):
        raise DataError("sandbox_type")
    return number(abs(cast(float, args[0])))


def _round(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    if not is_number(args[0]):
        raise DataError("sandbox_type")
    value = cast(float, args[0])
    if len(args) == 1:
        return number(round(value))
    digits = _int(args[1])
    if not 0 <= digits <= 15:
        raise DataError("sandbox_value")
    return number(round(value, digits))


_NUMBER_KINDS: Final = frozenset({int, float})


def _ordered(values: list[object], meter: Meter) -> list[object]:
    """A homogeneous list of numbers or of strs, else ``sandbox_type``.

    The type check is one native pass (1 fuel per 16 items).
    """
    native_walk(meter, len(values))
    kinds = set(map(type, values))
    if kinds <= _NUMBER_KINDS or kinds == {str}:
        return values
    raise DataError("sandbox_type")


def _extreme(meter: Meter, args: Sequence[object], pick: Callable[[list[float]], float]) -> object:
    if len(args) == 2:
        # max(a, b) is max([a, b]): first the 1 fuel that list's node would cost.
        meter.charge(1)
        values = [args[0], args[1]]
    else:
        values = _list(args[0])
    meter.work(len(values))
    if not values:
        raise DataError("sandbox_value")
    native_walk(meter, len(values))  # the native min/max pass
    ordering_charge(_ordered(values, meter), len(values), meter)
    return pick(cast(list[float], values))


def _min(meter: Meter, args: Sequence[object]) -> object:
    return _extreme(meter, args, min)


def _max(meter: Meter, args: Sequence[object]) -> object:
    return _extreme(meter, args, max)


def _sum(meter: Meter, args: Sequence[object]) -> object:
    values = _list(args[0])
    meter.work(len(values))
    if not set(map(type, values)) <= _NUMBER_KINDS:
        raise DataError("sandbox_type")
    meter.charge(len(values))  # the bounded fold below is a Python loop
    total: int | float = 0
    try:
        for item in values:
            total = number(total + cast(float, item))
    except OverflowError:  # an int beyond the float range meets a float (task ints)
        raise DataError("sandbox_value") from None
    return total


def _truths(meter: Meter, args: Sequence[object]) -> list[object]:
    """``any`` and ``all``: a list of JSON values, whose truth is Python's."""
    values = _list(args[0])
    meter.work(len(values))
    native_walk(meter, len(values))  # the native truth pass
    return values


def _any(meter: Meter, args: Sequence[object]) -> object:
    return any(_truths(meter, args))


def _all(meter: Meter, args: Sequence[object]) -> object:
    return all(_truths(meter, args))


def _sorted(meter: Meter, args: Sequence[object]) -> object:
    values = _list(args[0])
    reverse = args[1] if len(args) > 1 else False
    if type(reverse) is not bool:
        raise DataError("sandbox_type")
    size = len(values)
    comparisons = size * max(1, math.ceil(math.log2(size))) if size > 1 else size
    meter.work(comparisons)
    native_walk(meter, comparisons)  # a native comparison costs about 16 per fuel
    ordering_charge(_ordered(values, meter), comparisons, meter)
    return sorted(cast(list[float], values), reverse=reverse)


def _range(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    bounds = [_int(item) for item in args]
    if len(bounds) == 1:
        start, stop, step = 0, bounds[0], 1
    else:
        start, stop = bounds[0], bounds[1]
        step = bounds[2] if len(bounds) == 3 else 1
    if step == 0:
        raise DataError("sandbox_value")
    # Exact arithmetic: a span beyond sys.maxsize is the Fatal bound, not an OverflowError.
    if range_length(start, stop, step) > RANGE_LIMIT:
        raise Fatal("sandbox_value_limit")
    return RangeValue(start, stop, step)


def _enumerate(meter: Meter, args: Sequence[object]) -> object:
    values = _list(args[0])
    check_size(values)
    meter.work(len(values))
    meter.charge(len(values))
    return [[index, store(item, meter, 3)] for index, item in enumerate(values)]


def _zip(meter: Meter, args: Sequence[object]) -> object:
    """Two or three lists: work = the shortest length, plus 1 fuel for each item taken
    from each list, plus the copies of what is taken."""
    lists = [_list(item) for item in args]
    size = min(len(items) for items in lists)
    meter.work(size)
    meter.charge(len(lists) * size)
    return [[store(items[index], meter, 3) for items in lists] for index in range(size)]


def _list_builtin(meter: Meter, args: Sequence[object]) -> object:
    value = args[0]
    if type(value) is RangeValue:
        items = value.items()
        meter.work(len(items))
        if len(items) > CONTAINER_LIMIT:
            raise Fatal("sandbox_value_limit")
        native_walk(meter, len(items))
        return list(items)
    if type(value) is str:
        text = value
        meter.work(len(text))
        if len(text) > CONTAINER_LIMIT:
            raise Fatal("sandbox_value_limit")
        native_walk(meter, len(text))
        return list(text)
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        meter.work(len(mapping))
        native_walk(meter, len(mapping))
        return list(mapping)
    values = _list(value)
    meter.work(len(values))
    return store(values, meter, 1)


def _copy(meter: Meter, args: Sequence[object]) -> object:
    """``l.copy()`` and ``d.copy()``: the whole value, copied as ``list(l)`` copies a list."""
    value = args[0]
    container = cast(list[object], value) if type(value) is list else _dict(value)
    meter.work(len(container))
    return store(container, meter, 1)


def _kind(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    value = args[0]
    if value is None:
        return "null"
    names: dict[type, str] = {
        bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"
    }
    name = names.get(type(value))
    if name is None:
        raise DataError("sandbox_type")
    return name


def _keys(meter: Meter, args: Sequence[object]) -> object:
    mapping = _dict(args[0])
    meter.work(len(mapping))
    native_walk(meter, len(mapping))
    return list(mapping)


def _values(meter: Meter, args: Sequence[object]) -> object:
    mapping = _dict(args[0])
    meter.work(len(mapping))
    return store(list(mapping.values()), meter, 1)


def _items(meter: Meter, args: Sequence[object]) -> object:
    mapping = _dict(args[0])
    meter.work(len(mapping))
    meter.charge(len(mapping))
    return [[key, store(item, meter, 3)] for key, item in mapping.items()]


def _default(args: Sequence[object]) -> object:
    """``get`` and ``at`` take an optional default, None when left out (as ``dict.get``)."""
    return args[2] if len(args) > 2 else None


def _get(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    mapping, key = _dict(args[0]), _str(args[1])
    return mapping[key] if key in mapping else _default(args)


def _at(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    container, index = args[0], _int(args[1])
    if type(container) is RangeValue:
        items = container.items()
        return items[index] if -len(items) <= index < len(items) else _default(args)
    if type(container) not in (list, str):
        raise DataError("sandbox_type")
    sequence = cast(Sequence[object], container)
    return sequence[index] if -len(sequence) <= index < len(sequence) else _default(args)


# The mutating builtins take ``depth``: the nesting level at which the stored value
# lands, one below the target's own level. The interpreter tracks the level of every
# local that aliases a container inside another, so mutation through an alias keeps
# the nesting bound at construction. Called with two arguments, the target is a root.


def _append(meter: Meter, args: Sequence[object], depth: int = 2) -> object:
    target = _list(args[0])
    if len(target) >= CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    meter.work(1)
    target.append(store(args[1], meter, depth))
    return None


def _extend(meter: Meter, args: Sequence[object], depth: int = 2) -> object:
    target, extra = _list(args[0]), _list(args[1])
    if len(target) + len(extra) > CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    meter.work(len(extra))
    target.extend(cast(list[object], store(extra, meter, depth - 1)))
    return None


def _insert(meter: Meter, args: Sequence[object], depth: int = 2) -> object:
    target, index = _list(args[0]), _int(args[1])
    if len(target) >= CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    meter.work(len(target))
    # list.insert clamps the index; clamping first keeps a task int beyond ssize_t total.
    index = max(-len(target) - 1, min(index, len(target)))
    target.insert(index, store(args[2], meter, depth))
    return None


def _pop(meter: Meter, args: Sequence[object]) -> object:
    target = _list(args[0])
    meter.work(1)
    return target.pop() if target else None


def _set_key(meter: Meter, args: Sequence[object], depth: int = 2) -> object:
    target = _dict(args[0])
    if type(args[1]) is not str:
        raise DataError("sandbox_dictionary")
    key = args[1]
    if key not in target and len(target) >= CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    meter.work(1)
    target[key] = store(args[2], meter, depth)
    return None


# ---------------------------------------------------------------- strings


def _split(meter: Meter, args: Sequence[object]) -> object:
    if len(args) == 1:
        # No separator: Python's split on runs of whitespace, a native check of every
        # character.
        text = _str(args[0])
        meter.work(len(text))
        native_walk(meter, len(text))
        parts = text.split()
    else:
        text, separator = _str(args[0]), _nonempty(args[1])
        meter.work(len(text))
        search_charge(meter, len(text), len(separator))
        parts = text.split(separator)
    if len(parts) > CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    native_walk(meter, len(parts))
    return cast(list[object], parts)


def _join(meter: Meter, args: Sequence[object]) -> object:
    separator, parts = _str(args[0]), _list(args[1])
    native_walk(meter, 2 * len(parts))  # the type pass and the length pass
    if parts and set(map(type, parts)) != {str}:
        raise DataError("sandbox_type")
    texts = cast(list[str], parts)
    total = sum(map(len, texts)) + len(separator) * max(len(texts) - 1, 0)
    meter.work(total)
    if total > STRING_LIMIT:
        raise Fatal("sandbox_value_limit")
    return separator.join(cast(list[str], parts))


def _text_op(operation: Callable[[str], str]) -> Builtin:
    def apply(meter: Meter, args: Sequence[object]) -> object:
        text = _str(args[0])
        meter.work(len(text))
        if not text.isascii():  # non-ASCII case mapping is about 4 ns per char
            native_walk(meter, len(text))
        return check_str(operation(text))

    return apply


def _strip_op(operation: Callable[[str, str | None], str]) -> Builtin:
    """``strip``, ``lstrip`` and ``rstrip``. With no characters (or None) they remove
    whitespace, charged as ``strip(s)`` always was. With characters, each one removed is
    looked up in them natively, so the worst case (every character of ``s`` compared with
    every one of ``chars``) is charged before the work, as a substring search is."""

    def apply(meter: Meter, args: Sequence[object]) -> object:
        text = _str(args[0])
        chars = args[1] if len(args) > 1 else None
        if chars is None:
            meter.work(len(text))
            if not text.isascii():  # a non-ASCII whitespace test is a table lookup
                native_walk(meter, len(text))
            return check_str(operation(text, None))
        removed = _str(chars)
        meter.work(len(text))
        native_walk(meter, len(text) + len(removed))
        meter.charge((len(text) * len(removed)) >> _SEARCH_SHIFT)
        return check_str(operation(text, removed))

    return apply


def _test_op(operation: Callable[[str], bool]) -> Builtin:
    """``isdigit`` and the other character tests, as Python's own: a native table lookup
    for every character."""

    def apply(meter: Meter, args: Sequence[object]) -> object:
        text = _str(args[0])
        meter.work(len(text))
        native_walk(meter, len(text))
        return operation(text)

    return apply


def _rfind(meter: Meter, args: Sequence[object]) -> object:
    text, sub = _str(args[0]), _str(args[1])
    meter.work(len(text))
    reverse_search_charge(meter, len(text), len(sub))
    return text.rfind(sub)


def _chr(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    code = _int(args[0])
    if not 0 <= code <= 0x10FFFF:
        raise DataError("sandbox_value")
    return chr(code)


def _ord(meter: Meter, args: Sequence[object]) -> object:
    meter.work(1)
    text = args[0]
    if type(text) is not str or len(text) != 1:
        raise DataError("sandbox_type")
    return ord(text)


def _pair_op(
    operation: Callable[[str, str], object], *, nonempty: bool = False, search: bool = False
) -> Builtin:
    def apply(meter: Meter, args: Sequence[object]) -> object:
        text = _str(args[0])
        other = _nonempty(args[1]) if nonempty else _str(args[1])
        meter.work(len(text))
        if search:
            search_charge(meter, len(text), len(other))
        return operation(text, other)

    return apply


def _splitlines(meter: Meter, args: Sequence[object]) -> object:
    text = _str(args[0])
    meter.work(len(text))
    lines = text.splitlines()
    if len(lines) > CONTAINER_LIMIT:
        raise Fatal("sandbox_value_limit")
    native_walk(meter, len(lines))
    return cast(list[object], lines)


def _replace(meter: Meter, args: Sequence[object]) -> object:
    text, old, new = _str(args[0]), _nonempty(args[1]), _str(args[2])
    meter.work(len(text))
    # Three searches: the bound's count, then ``str.replace``'s own count and scan.
    search_charge(meter, len(text), len(old), passes=3)
    # Bound the result before building it: a small input can otherwise blow up.
    if len(text) + text.count(old) * (len(new) - len(old)) > STRING_LIMIT:
        raise Fatal("sandbox_value_limit")
    return text.replace(old, new)


# ---------------------------------------------------------------- JSON


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError("non-finite number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _bounded(value: object, depth: int = 0) -> bool:
    """Construction bounds on parsed JSON, and ints below 2**63 as arithmetic keeps them."""
    if type(value) is int:
        return -INT_BOUND < value < INT_BOUND
    if type(value) is list or type(value) is dict:
        if depth >= NESTING_LIMIT:
            return False
        items = cast(dict[str, object], value).values() if type(value) is dict else cast(
            list[object], value
        )
        return len(items) <= CONTAINER_LIMIT and all(_bounded(item, depth + 1) for item in items)
    return type(value) is not str or len(value) <= STRING_LIMIT


def parse_json_text(text: str, meter: Meter | None = None) -> object:
    """Strict JSON or None: over 128 KiB, deeper than 40, non-finite or duplicate keys.

    An int at least 2**63 in magnitude also gives None: harness code can then only
    hold such an int when the task or a capability reply supplied it.

    With a ``meter``, a parse costs 1 fuel plus 1 per 2 input characters: the duplicate
    key hook and the bound check are Python-level walks of what was parsed.
    """
    if meter is not None:
        meter.charge(1 + (len(text) >> _PARSE_SHIFT))
    if len(text) > PARSE_LIMIT or len(text.encode("utf-8", "surrogatepass")) > PARSE_LIMIT:
        return None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError, TypeError):
        return None
    return value if _bounded(value) else None


def _balanced_end(text: str, start: int, meter: Meter | None = None) -> int:
    """Index just after the bracket closing ``text[start]``, or -1.

    With a ``meter``, the Python-level scan costs 1 fuel plus 1 per 4 characters
    it may visit, charged before scanning.
    """
    if meter is not None:
        meter.charge(1 + ((len(text) - start) >> _SCAN_SHIFT))
    stack: list[str] = []
    quoted = escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack.pop() != char:
                return -1
            if not stack:
                return index + 1
    return -1


def _first_balanced(text: str, meter: Meter | None = None) -> object:
    tried = 0
    position = 0
    while tried < 16:
        found = (text.find("{", position), text.find("[", position))
        starts = [index for index in found if index >= 0]
        if not starts:
            return None
        start = min(starts)
        end = _balanced_end(text, start, meter)
        tried += 1
        if end > 0:
            value = parse_json_text(text[start:end], meter)
            if value is not None:
                return value
        position = start + 1
    return None


def _fence_bodies(text: str) -> Iterator[str]:
    """The bodies of the closed code fences of ``text``, in order, in one linear pass.

    A fence is ```, an optional ASCII-letter tag, spaces or tabs, an optional line
    break, then the body up to the next ```. An opener with no closer ends the scan:
    no later opener can have one either.
    """
    position = 0
    while (start := text.find("```", position)) >= 0:
        tag = _FENCE_TAG.match(text, start + 3)
        body = start + 3 if tag is None else tag.end()
        end = text.find("```", body)
        if end < 0:
            return
        yield text[body:end]
        position = end + 3


def extract_json_text(text: str, meter: Meter | None = None) -> object:
    """The first balanced ``{...}`` or ``[...]``, fenced or bare, parsed strictly.

    With a ``meter``, every fence, parse and bracket scan is charged as it happens.
    """
    if len(text) > PARSE_LIMIT:
        return None
    if meter is not None:
        search_charge(meter, len(text), 3)  # the fence scan
    for fenced in _fence_bodies(text):
        if meter is not None:
            meter.charge(4)  # a fence costs about 1 us before its body is read
        body = fenced.strip()
        value = parse_json_text(body, meter)
        if type(value) in (list, dict):
            return value
        value = _first_balanced(body, meter)
        if value is not None:
            return value
    return _first_balanced(text, meter)


def _parse_json(meter: Meter, args: Sequence[object]) -> object:
    text = _str(args[0])
    meter.work(len(text))
    return parse_json_text(text, meter)


def _extract_json(meter: Meter, args: Sequence[object]) -> object:
    text = _str(args[0])
    meter.work(len(text))
    return extract_json_text(text, meter)


def _to_json(meter: Meter, args: Sequence[object]) -> object:
    text = canonical(args[0], meter)
    meter.work(len(text))
    return check_str(text)


def _canonical_bytes(value: object, meter: Meter | None = None) -> bytes:
    """UTF-8 of canonical JSON, total over JSON strings.

    A lone surrogate (legal in JSON input, ``parse_json`` output and model replies)
    is encoded as its code-unit bytes instead of failing; valid text is unchanged.
    """
    return canonical(value, meter).encode("utf-8", "surrogatepass")


def digest_value(value: object) -> str:
    """64-hex sha256 of canonical sorted-key JSON; independent of ``PYTHONHASHSEED``."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(meter: Meter, args: Sequence[object]) -> object:
    data = _canonical_bytes(args[0], meter)
    meter.work(len(data))
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- grids


def _cells(meter: Meter, grid: list[list[int]], *, python: bool = False) -> None:
    """The Section 4.6 charge (work = cells), plus 1 per cell for a Python-level scan."""
    cells = len(grid) * len(grid[0])
    meter.work(cells)
    if python:
        meter.charge(cells)


def _grid_diff(meter: Meter, args: Sequence[object]) -> object:
    before, after = _grid(args[0], meter), _grid(args[1], meter)
    limit = _int(args[2]) if len(args) == 3 else grids.GRID_CELL_LIMIT
    if not 0 <= limit <= grids.GRID_CELL_LIMIT:
        raise DataError("sandbox_value")
    _cells(meter, before if len(before) * len(before[0]) >= len(after) * len(after[0]) else after,
           python=True)
    return grids.grid_diff(before, after, limit)


def _grid_text(meter: Meter, args: Sequence[object]) -> object:
    grid = _grid(args[0], meter)
    _cells(meter, grid)
    return grids.grid_text(grid)


def _objects(meter: Meter, args: Sequence[object]) -> object:
    grid = _grid(args[0], meter)
    background = _int(args[1]) if len(args) == 2 else 0
    if not 0 <= background <= 15:
        raise DataError("sandbox_value")
    _cells(meter, grid, python=True)
    meter.charge(len(grid) * len(grid[0]))  # the flood fill visits each cell's 4 neighbours
    return grids.objects(grid, background)


def _count_colors(meter: Meter, args: Sequence[object]) -> object:
    grid = _grid(args[0], meter)
    _cells(meter, grid, python=True)
    return grids.count_colors(grid)


def _find_cells(meter: Meter, args: Sequence[object]) -> object:
    grid, color = _grid(args[0], meter), _int(args[1])
    if not 0 <= color <= 15:
        raise DataError("sandbox_value")
    _cells(meter, grid, python=True)
    return grids.find_cells(grid, color)


def _crop(meter: Meter, args: Sequence[object]) -> object:
    grid = _grid(args[0], meter)
    bounds = [_int(item) for item in args[1:5]]
    _cells(meter, grid)
    return grids.crop(grid, bounds[0], bounds[1], bounds[2], bounds[3])


def _grid_equal(meter: Meter, args: Sequence[object]) -> object:
    left, right = _grid(args[0], meter), _grid(args[1], meter)
    _cells(meter, left)
    return grids.grid_equal(left, right)


BUILTIN_FUNCTIONS: Final[Mapping[str, Builtin]] = MappingProxyType(
    {
        "len": _len,
        "str": _str_builtin,
        "int": _int_builtin,
        "float": _float_builtin,
        "bool": _bool_builtin,
        "abs": _abs,
        "round": _round,
        "min": _min,
        "max": _max,
        "sum": _sum,
        "any": _any,
        "all": _all,
        "sorted": _sorted,
        "range": _range,
        "enumerate": _enumerate,
        "zip": _zip,
        "list": _list_builtin,
        "kind": _kind,
        "keys": _keys,
        "values": _values,
        "items": _items,
        "get": _get,
        "at": _at,
        "append": _append,
        "extend": _extend,
        "insert": _insert,
        "pop": _pop,
        "set_key": _set_key,
        "copy": _copy,
        "split": _split,
        "join": _join,
        "strip": _strip_op(str.strip),
        "lstrip": _strip_op(str.lstrip),
        "rstrip": _strip_op(str.rstrip),
        "lower": _text_op(str.lower),
        "upper": _text_op(str.upper),
        "startswith": _pair_op(str.startswith),
        "endswith": _pair_op(str.endswith),
        "replace": _replace,
        "find": _pair_op(str.find, search=True),
        "rfind": _rfind,
        "count": _pair_op(str.count, nonempty=True, search=True),
        "splitlines": _splitlines,
        "isdigit": _test_op(str.isdigit),
        "isalpha": _test_op(str.isalpha),
        "isalnum": _test_op(str.isalnum),
        "isspace": _test_op(str.isspace),
        "isupper": _test_op(str.isupper),
        "islower": _test_op(str.islower),
        "chr": _chr,
        "ord": _ord,
        "parse_json": _parse_json,
        "extract_json": _extract_json,
        "to_json": _to_json,
        "digest": _digest,
        "grid_diff": _grid_diff,
        "diff_cells": _grid_diff,
        "grid_text": _grid_text,
        "objects": _objects,
        "count_colors": _count_colors,
        "find_cells": _find_cells,
        "crop": _crop,
        "grid_equal": _grid_equal,
    }
)

Mutator = Callable[[Meter, Sequence[object], int], object]

MUTATING_FUNCTIONS: Final[Mapping[str, Mutator]] = MappingProxyType(
    {"append": _append, "extend": _extend, "insert": _insert, "set_key": _set_key}
)

assert set(BUILTIN_FUNCTIONS) == set(BUILTINS) == set(ARITY)
# pop stores nothing, so it needs no landing depth.
assert set(MUTATING_FUNCTIONS) | {"pop"} == {n for n, spec in BUILTINS.items() if spec.mutating}
