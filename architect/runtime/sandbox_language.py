"""The single table of sandbox language v2: constructs, builtins, capabilities, codes, limits.

``_GrammarV2`` (``sandbox.py``), the v2 interpreter (``_sandbox_worker.py``) and
``_sandbox_builtins.BUILTIN_FUNCTIONS`` read these tables, and ``language_markdown``
renders them as the Forge ``LANGUAGE.md`` card, so the document a model reads cannot
drift from the validator. ``RULES`` holds the fixed sentence each static refusal shows
(``refusal_message``): what broke and the form that is allowed. Everything here is
ASCII and deterministic.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, NamedTuple

__all__ = [
    "ALTERNATIVES",
    "ARITY",
    "BUILTINS",
    "CAPABILITIES",
    "CONTAINER_LIMIT",
    "DATA_ERROR_CODES",
    "DEFAULT_FUEL",
    "EXCEPTION_NAMES",
    "EXPRESSIONS",
    "HINTS",
    "GENERATOR_CALLEES",
    "LANGUAGE_VERSION",
    "LOOP_NAMES_LIMIT",
    "MAX_FUEL",
    "METHODS",
    "MODULES",
    "NODE_LIMIT_V2",
    "RESERVED",
    "RULES",
    "REFUSALS_SHOWN",
    "RUNTIME_ERROR_CODES",
    "STATEMENTS",
    "TYPE_NAMES",
    "BuiltinSpec",
    "CapabilitySpec",
    "Rule",
    "language_markdown",
    "refusal_message",
]

LANGUAGE_VERSION: Final = 2
NODE_LIMIT_V2: Final = 8000
DEFAULT_FUEL: Final = 10_000_000
MAX_FUEL: Final = 100_000_000
DEPTH_LIMIT_V2: Final = 40
SOURCE_LIMIT_V2: Final = 64 * 1024
HELPER_LIMIT: Final = 32
CONSTANT_LIMIT: Final = 64
LOOP_DEPTH_LIMIT: Final = 6
PARAMETER_LIMIT: Final = 8
STRING_LIMIT: Final = 131_072
CONTAINER_LIMIT: Final = 65_536
NESTING_LIMIT: Final = 40
RANGE_LIMIT: Final = 100_000
SYSTEM_LIMIT: Final = 64 * 1024
INT_BOUND: Final = 2**63
FUEL_UNIT: Final = 256
RPC_FUEL: Final = 64
RPC_BYTES_PER_FUEL: Final = 8
WORKER_MEMORY_WINDOWS: Final = 512 * 2**20
WORKER_MEMORY_POSIX: Final = 2**30
# The most refusals one check lists; every other refused line is counted, not listed.
REFUSALS_SHOWN: Final = 20
CONSTANT_NAME: Final = r"^[A-Z][A-Z0-9_]{0,63}$"
HELPER_NAME: Final = r"^[a-z][a-z0-9_]{0,63}$"


class BuiltinSpec(NamedTuple):
    name: str
    signature: str
    returns: str
    work: str
    mutating: bool
    hint: str


class CapabilitySpec(NamedTuple):
    name: str
    source_form: str
    op: str
    game_only: bool


def _builtins(*rows: BuiltinSpec) -> Mapping[str, BuiltinSpec]:
    return MappingProxyType({row.name: row for row in rows})


_B = BuiltinSpec
BUILTINS: Final[Mapping[str, BuiltinSpec]] = _builtins(
    _B("len", "len(x)", "int", "1", False, "x is a str, list, dict or range"),
    _B("str", "str(x)", "str", "output chars", False, "a str is unchanged; else compact JSON"),
    _B("int", "int(x)", "int", "input chars", False, "number, bool or numeric str"),
    _B("float", "float(x)", "float", "input chars", False, "number, bool or numeric str"),
    _B("bool", "bool(x)", "bool", "input chars", False, "str accepts true/false/yes/no/1/0"),
    _B("abs", "abs(x)", "number", "1", False, "x is an int or float"),
    _B("round", "round(x[, n])", "number", "1", False, "n is an int 0..15"),
    _B("min", "min(list) or min(a, b)", "number or str", "items", False,
       "min(a, b) is min([a, b]), at the same fuel"),
    _B("max", "max(list) or max(a, b)", "number or str", "items", False,
       "max(a, b) is max([a, b]), at the same fuel"),
    _B("sum", "sum(list)", "number", "items", False, "a list of numbers; sum([]) is 0"),
    _B("any", "any(list)", "bool", "items", False, "True when an item is truthy; any([]) is False"),
    _B("all", "all(list)", "bool", "items", False, "False when an item is falsy; all([]) is True"),
    _B("sorted", "sorted(list, reverse=False)", "list", "n*ceil(log2 n)", False,
       "all numbers or all strs"),
    _B("range", "range(a[, b[, c]])", "range", "1 plus 1 per iteration", False,
       "at most 100000 items; use in for, len, list or at"),
    _B("enumerate", "enumerate(list)", "[[i, v]]", "items", False, "for i, v in enumerate(l):"),
    _B("zip", "zip(l1, l2[, l3])", "[[a, b]] or [[a, b, c]]", "items", False,
       "stops at the shortest list"),
    _B("list", "list(x)", "list", "items", False, "from a range, str, dict (keys) or list (copy)"),
    _B("kind", "kind(x)", "str", "1", False, "null, bool, int, float, str, list or dict"),
    _B("keys", "keys(d)", "list", "items", False, "d is a dict"),
    _B("values", "values(d)", "list", "items", False, "d is a dict"),
    _B("items", "items(d)", "[[k, v]]", "items", False, "for k, v in items(d):"),
    _B("get", "get(d, k[, default])", "value", "1", False,
       "never raises on a missing key; the default is None when left out"),
    _B("at", "at(l, i[, default])", "value", "1", False,
       "never raises on a missing index; the default is None when left out"),
    _B("append", "append(l, v)", "null", "copied elements", True, "l is a local name"),
    _B("extend", "extend(l, l2)", "null", "copied elements", True, "l is a local name"),
    _B("insert", "insert(l, i, v)", "null", "copied elements", True, "l is a local name"),
    _B("pop", "pop(l)", "last item or null", "1", True, "l is a local name"),
    _B("set_key", "set_key(d, k, v)", "null", "copied elements", True,
       "d is a local name; k is a str"),
    _B("copy", "copy(x)", "list or dict", "copied elements", False,
       "a list or dict, copied whole as list(l) copies a list"),
    _B("split", "split(s[, sep])", "list of str", "input chars", False,
       "sep is a non-empty str; without it, split on runs of whitespace"),
    _B("join", "join(sep, list_of_str)", "str", "input chars", False, "join(', ', parts)"),
    _B("strip", "strip(s[, chars])", "str", "input chars", False,
       "removes surrounding whitespace, or the characters in chars"),
    _B("lstrip", "lstrip(s[, chars])", "str", "input chars", False, "strip at the start only"),
    _B("rstrip", "rstrip(s[, chars])", "str", "input chars", False, "strip at the end only"),
    _B("lower", "lower(s)", "str", "input chars", False, "s is a str"),
    _B("upper", "upper(s)", "str", "input chars", False, "s is a str"),
    _B("startswith", "startswith(s, p)", "bool", "input chars", False, "s and p are strs"),
    _B("endswith", "endswith(s, p)", "bool", "input chars", False, "s and p are strs"),
    _B("replace", "replace(s, a, b)", "str", "input chars", False,
       "a is non-empty; the result is at most 131072 chars"),
    _B("find", "find(s, sub)", "int", "input chars", False, "-1 when absent"),
    _B("rfind", "rfind(s, sub)", "int", "input chars", False, "the last match; -1 when absent"),
    _B("count", "count(s, sub)", "int", "input chars", False, "sub is a non-empty str"),
    _B("splitlines", "splitlines(s)", "list of str", "input chars", False, "s is a str"),
    _B("isdigit", "isdigit(s)", "bool", "input chars", False, "as Python's str.isdigit"),
    _B("isalpha", "isalpha(s)", "bool", "input chars", False, "as Python's str.isalpha"),
    _B("isalnum", "isalnum(s)", "bool", "input chars", False, "as Python's str.isalnum"),
    _B("isspace", "isspace(s)", "bool", "input chars", False, "as Python's str.isspace"),
    _B("isupper", "isupper(s)", "bool", "input chars", False, "as Python's str.isupper"),
    _B("islower", "islower(s)", "bool", "input chars", False, "as Python's str.islower"),
    _B("chr", "chr(i)", "str", "1", False, "i is an int from 0 to 1114111"),
    _B("ord", "ord(c)", "int", "1", False, "c is a str of one character"),
    _B("parse_json", "parse_json(s)", "value or null", "input chars", False,
       "null on invalid, deep, non-finite, duplicate-key or 2**63+ int JSON"),
    _B("extract_json", "extract_json(s)", "value or null", "input chars", False,
       "first {...} or [...] in prose or a code fence"),
    _B("to_json", "to_json(x)", "str", "output chars", False, "sorted-key compact JSON"),
    _B("digest", "digest(x)", "str", "canonical bytes", False, "64-hex sha256 of canonical JSON"),
    _B("grid_diff", "grid_diff(a, b[, limit])", "dict", "cells", False,
       "{cells, box: [x0, y0, x1, y1]} or {resized: true}"),
    _B("diff_cells", "diff_cells(a, b[, limit])", "dict", "cells", False,
       "alias of grid_diff; limit <= 4096"),
    _B("grid_text", "grid_text(grid)", "str", "cells", False, "one hex digit per cell per row"),
    _B("objects", "objects(grid[, background])", "list", "cells", False,
       "<= 256 of {color, cells, x0, y0, x1, y1, cx, cy}"),
    _B("count_colors", "count_colors(grid)", "dict", "cells", False, "{'<color>': n}"),
    _B("find_cells", "find_cells(grid, color)", "[[x, y]]", "cells", False, "at most 4096"),
    _B("crop", "crop(grid, x0, y0, x1, y1)", "list", "cells", False,
       "inclusive bounds, clipped; empty is []"),
    _B("grid_equal", "grid_equal(a, b)", "bool", "cells", False, "same shape and cells"),
)
del _B

# (minimum, maximum) positional arguments; ``sorted`` also takes ``reverse=``.
ARITY: Final[Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "len": (1, 1), "str": (1, 1), "int": (1, 1), "float": (1, 1), "bool": (1, 1),
        "abs": (1, 1), "round": (1, 2), "min": (1, 2), "max": (1, 2), "sum": (1, 1),
        "any": (1, 1), "all": (1, 1),
        "sorted": (1, 1), "range": (1, 3), "enumerate": (1, 1), "zip": (2, 3),
        "list": (1, 1), "kind": (1, 1), "keys": (1, 1), "values": (1, 1), "items": (1, 1),
        "get": (2, 3), "at": (2, 3), "append": (2, 2), "extend": (2, 2), "insert": (3, 3),
        "pop": (1, 1), "set_key": (3, 3), "copy": (1, 1), "split": (1, 2), "join": (2, 2),
        "strip": (1, 2), "lstrip": (1, 2), "rstrip": (1, 2),
        "lower": (1, 1), "upper": (1, 1), "startswith": (2, 2), "endswith": (2, 2),
        "replace": (3, 3), "find": (2, 2), "rfind": (2, 2), "count": (2, 2),
        "splitlines": (1, 1), "isdigit": (1, 1), "isalpha": (1, 1), "isalnum": (1, 1),
        "isspace": (1, 1), "isupper": (1, 1), "islower": (1, 1), "chr": (1, 1), "ord": (1, 1),
        "parse_json": (1, 1), "extract_json": (1, 1), "to_json": (1, 1), "digest": (1, 1),
        "grid_diff": (2, 3), "diff_cells": (2, 3), "grid_text": (1, 1), "objects": (1, 2),
        "count_colors": (1, 1), "find_cells": (2, 2), "crop": (5, 5), "grid_equal": (2, 2),
    }
)

MUTATING: Final = frozenset(name for name, spec in BUILTINS.items() if spec.mutating)

# Method spellings: ``value.name(args)`` is exactly the builtin ``name(value, args)``, with
# the receiver as the first argument. Nothing is looked up on a value; any other method
# or attribute is refused before the source runs.
METHODS: Final = frozenset(
    {
        "append", "extend", "insert", "pop", "get", "keys", "values", "items", "split",
        "join", "strip", "lower", "upper", "startswith", "endswith", "replace", "find",
        "count", "splitlines",
        # Forge pack v4.
        "copy", "lstrip", "rstrip", "rfind", "isdigit", "isalpha", "isalnum", "isspace",
        "isupper", "islower",
    }
)

# ``isinstance(x, T)``: T is one of these names or a tuple or list of them. As in Python,
# a bool is also an int.
TYPE_NAMES: Final = frozenset({"str", "int", "float", "bool", "list", "dict"})

# ``except T:`` and ``except (T1, T2) as e:``: each T is one of these names (Python's own
# exception names), never evaluated. Any of them catches every data error, as a bare
# ``except:`` does, and ``e`` holds that error's code, a str, until the handler ends.
EXCEPTION_NAMES: Final = frozenset(
    {
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "LookupError",
        "ArithmeticError", "ZeroDivisionError", "OverflowError", "AttributeError",
        "UnicodeError",
    }
)
# The most names a for loop or a comprehension's for clause binds: zip of three lists.
LOOP_NAMES_LIMIT: Final = 3

# A generator expression is accepted only as the list argument of these builtins: the
# only one for most, the second of join(sep, values), also as sep.join(values). any and
# all stop at the first item that decides them; the others get the list it builds.
GENERATOR_CALLEES: Final = frozenset({"any", "all", "sum", "min", "max", "sorted", "list", "join"})

# Names that are never a helper, a parameter or a variable.
RESERVED: Final = frozenset(
    {"run", "context", "task", "execute_preset", "call", "observe", "step", "harness",
     "isinstance"}
)

CAPABILITIES: Final[Mapping[str, CapabilitySpec]] = MappingProxyType(
    {
        "call": CapabilitySpec(
            "call", "await context.call(prompt[, tokens], system=S, images=L)", "call", False
        ),
        "execute_preset": CapabilitySpec(
            "execute_preset", "await execute_preset(<exact binding>, context, x)", "preset", False
        ),
        "observe": CapabilitySpec("observe", "await context.observe()", "observe", True),
        "step": CapabilitySpec("step", "await context.step(action)", "step", True),
        "harness": CapabilitySpec(
            "harness", 'await context.harness("<bound name>", value)', "harness", False
        ),
    }
)

# Every statement and expression form, with one example each (the card's two tables).
STATEMENTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "assignment": 'x = 1; a, b = pair; d["k"] = v; l[0] = v',
        "annotated assignment": "n: int = 0 (the annotation is ignored)",
        "augmented assignment": "n += 1, also -=, *=, //= and %=",
        "if": "if a: ... elif b: ... else: ...",
        "for": "for x in l:, for k, v in items(d): or for a, b, c in zip(l1, l2, l3): over a "
        "list, dict keys, str or range",
        "while": "while n < 10:",
        "break, continue, pass": "break and continue only inside a loop",
        "try": "try: ... except: ... catches data errors only; so do except ValueError: and "
        "except (KeyError, TypeError) as e:, with e set to the error's code",
        "return": "return value",
        "call statement": "append(out, x); out.append(x); clean(x); await context.call(p)",
    }
)

EXPRESSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "arithmetic": "a + b, a - b, a * b, a / b, a // b, a % b, -a (+ also joins strs, lists)",
        "comparison": "==, !=, <, <=, >, >=, in, not in, x is None, x is not None",
        "logic": "a and b, a or b, not a",
        "conditional": "a if ok else b",
        "f-string": 'f"{name}: {n}"',
        "containers": '[1, 2], (1, 2) (a list), {"k": v} (str keys)',
        "index and slice": 'x[0], d["k"], s[1:3], l[::-1] (step 1 or -1)',
        "call": "len(x), clean(x), sorted(l, reverse=True), isinstance(x, (int, float))",
        "method": 's.split(","), l.append(x), d.get("k")',
        "comprehension": "[f(x) for x in l if x], {k: v for k, v in items(d)}, {x for x in l}",
        "generator": "any(x > 0 for x in l), also in all, sum, min, max, sorted, list and join",
    }
)

DATA_ERROR_CODES: Final = frozenset(
    {
        "sandbox_key",
        "sandbox_index",
        "sandbox_type",
        "sandbox_value",
        "sandbox_dictionary",
        "sandbox_add",
    }
)
FATAL_ERROR_CODES: Final = frozenset(
    {
        "sandbox_unbound",
        "sandbox_fuel",
        "sandbox_memory",
        "sandbox_value_limit",
        "sandbox_invalid_call",
    }
)
RUNTIME_ERROR_CODES: Final = DATA_ERROR_CODES | FATAL_ERROR_CODES

HINTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Validation (static) codes.
        "sandbox_source_limit": "harness.py is larger than 64 KiB.",
        "sandbox_invalid_source": "The source does not parse as Python syntax.",
        "sandbox_ast_limit": "Too many syntax nodes (8000) or nesting deeper than 40.",
        "sandbox_import": "Only the 4 fixed from-imports are allowed; no other import.",
        "sandbox_entrypoint": "End the file with exactly one async def run(context, task).",
        "sandbox_signature": (
            "Use async def run(context, task): annotations are fine; no defaults, no other "
            "parameters."
        ),
        "sandbox_statement": (
            "Unsupported statement: no raise, assert, with, del, global, class, import or "
            "async for; break/continue only in a loop."
        ),
        "sandbox_expression": (
            "Unsupported expression: no attributes, unlisted methods, lambda, set literal or "
            "generator expression outside the builtins that take one."
        ),
        "sandbox_assignment": (
            "Assign only to local names or subscripts of them; never to task, context, a "
            "CONSTANT or a helper."
        ),
        "sandbox_dictionary": "Dictionary keys must be str values; no ** unpacking.",
        "sandbox_comparison": "Use is / is not only with None; compare values with == or <.",
        "sandbox_call": (
            "Call a builtin or helper by name with positional arguments and the right count; "
            "see the builtin table."
        ),
        "sandbox_capability": (
            "Only await context.call/observe/step/harness, execute_preset or an async helper; "
            "observe/step need a game spec."
        ),
        "sandbox_call_limit": "Language 1 allows at most 64 await sites.",
        "sandbox_preset_call": "Call execute_preset with the 6 exact bound arguments.",
        "sandbox_binding_mismatch": (
            "The preset literals or the harness name do not match this artifact's binding."
        ),
        "sandbox_literal_required": "This argument must be a literal value.",
        "sandbox_invalid_json": "The value is not JSON: null, bool, number, str, list or dict.",
        "sandbox_recursion": "Helpers may not call themselves directly or through each other.",
        "sandbox_loop_depth": "Loops may nest at most 6 deep.",
        "sandbox_helper": (
            "Helper: def name(a, b=1) or async def name(context, b); a lower_case name that is "
            "not a builtin's; at most 8 parameters; sync helpers never await."
        ),
        "sandbox_constant": (
            "Module constants are NAME = literal with an UPPER_CASE name; at most 64, no calls."
        ),
        "sandbox_language": "This construct needs sandbox language 2; this artifact is bound "
        "to language 1.",
        # Runtime codes: data errors (catchable by try/except).
        "sandbox_key": "A dict key is missing; use get(d, k, default).",
        "sandbox_index": "A list or str index is out of range; use at(l, i, default).",
        "sandbox_type": "A value has the wrong type for this operation or builtin; see kind(x).",
        "sandbox_value": (
            "Invalid value: parse failure, division by zero, empty min/max, or a number out of "
            "range."
        ),
        "sandbox_add": "+ needs two strs, two lists or two numbers.",
        # Runtime codes: fatal errors (never caught).
        "sandbox_unbound": "A local name was read before any assignment.",
        "sandbox_fuel": "The fuel budget ran out; loops must terminate and do less work.",
        "sandbox_memory": "The worker ran out of memory; build smaller values.",
        "sandbox_value_limit": (
            "A str over 131072 chars, a list or dict over 65536 entries, nesting over 40, a "
            "range over 100000, or an RPC value over 128 KiB."
        ),
        "sandbox_invalid_call": (
            "A capability call was refused: bad arguments, an unoffered action, an unknown "
            "image, or the call cap."
        ),
        # Language 1 runtime and host protocol codes.
        "sandbox_unary": "Unary minus needs a number.",
        "sandbox_length": "len needs a str, list or dict.",
        "sandbox_message_limit": "A message crossing the sandbox boundary exceeds 128 KiB.",
        "sandbox_invalid_message": "The sandbox worker sent a malformed message.",
        "sandbox_worker_failed": "The sandbox worker failed without a reported code.",
        "sandbox_protocol_failed": "The sandbox pipe failed.",
        "sandbox_source_unreadable": "harness.py could not be read as UTF-8.",
        "sandbox_invalid_preset": "This preset cannot run through the host preset RPC.",
        # Sub-harness execution codes (runtime/subharness.py).
        "subharness_unknown": "The harness name is not bound to this artifact.",
        "subharness_digest_mismatch": "The bound sub-harness changed after binding.",
        "subharness_unlicensed": "The bound sub-harness is not licensed.",
        "subharness_depth": "Sub-harnesses may nest at most 4 deep.",
        "subharness_schema_error": "The sub-harness output does not match its output schema.",
    }
)


class Rule(NamedTuple):
    """One static refusal: its fixed code and the sentences a refusal message shows.

    ``text`` says what broke and the form that is allowed. ``named``, when set, replaces
    it when the refusal names one of the language's own identifiers (a builtin, a method,
    a reserved name, a binding key). ``<name>`` stands for that identifier, ``<form>``
    for a builtin's signature and ``<advice>`` for its ``ALTERNATIVES`` entry. Source
    text never fills any of them.
    """

    code: str
    text: str
    named: str = ""


_R = Rule
_NAMED = sorted(EXCEPTION_NAMES)
_EXCEPTIONS = ", ".join(_NAMED[:-1]) + " or " + _NAMED[-1]
_BINDING = (
    "Copy the first four arguments exactly from the starting harness.py or a template; "
    "only the last argument may change."
)
RULES: Final[Mapping[str, Rule]] = MappingProxyType(
    {
        # The file as a whole.
        "source_limit": _R(
            "sandbox_source_limit",
            "harness.py is over 64 KiB. Shorten long prompts and tables, or move repeated "
            "code into helpers.",
        ),
        "ast_limit": _R(
            "sandbox_ast_limit",
            "The file has more than 8,000 syntax nodes or nests deeper than 40 levels. "
            "Replace long literal tables and deep nesting with loops and helpers.",
        ),
        "syntax": _R(
            "sandbox_invalid_source",
            "Python cannot parse this line. Check the brackets, quotes, colon and "
            "indentation on it and on the line before it.",
        ),
        "syntax_indent": _R(
            "sandbox_invalid_source",
            "The indentation here does not parse. Indent each block by 4 spaces, the same "
            "way throughout, with no tabs.",
        ),
        "syntax_string": _R(
            "sandbox_invalid_source",
            "A string starts here and is never closed. Close it with the same quote, or use "
            "triple quotes for text over several lines.",
        ),
        "syntax_bracket": _R(
            "sandbox_invalid_source",
            "A bracket here is never closed or does not match. Check each pair of (), [] "
            "and {}.",
        ),
        "syntax_colon": _R(
            "sandbox_invalid_source",
            "A def, if, elif, else, for, while, try or except line must end with a colon.",
        ),
        "syntax_character": _R(
            "sandbox_invalid_source",
            "This line has a character Python does not accept outside a string, such as a "
            "curly quote. Use plain ASCII quotes and operators.",
        ),
        "syntax_newlines": _R(
            "sandbox_invalid_source",
            "The file arrived as one line, with each line break written as the two "
            "characters \\n. Write the file again with real line breaks.",
        ),
        "syntax_backslash": _R(
            "sandbox_invalid_source",
            "A backslash outside a string must be the last character of its line. Move it "
            "into a string or remove it.",
        ),
        "syntax_fstring": _R(
            "sandbox_invalid_source",
            "This f-string does not parse. Use plain {name} parts, with the other kind of "
            "quote inside the braces.",
        ),
        "syntax_null": _R(
            "sandbox_invalid_source", "The file contains a NUL character. Remove it."
        ),
        # Module level.
        "import": _R(
            "sandbox_import",
            "Only these imports exist, at the top and each at most once: from __future__ "
            "import annotations, from pydantic import JsonValue, from architect.presets "
            "import execute_preset, from architect.runtime import RunContext. Delete this "
            "one; the builtins cover the rest.",
        ),
        "import_module": _R(
            "sandbox_import",
            "No module can be imported here. Delete this import; the builtins cover the rest.",
            "No module can be imported, so delete this import of <name>: <advice>",
        ),
        "run_last": _R(
            "sandbox_entrypoint",
            "The file must end with async def run(context, task):. Put the constants and "
            "helpers above it.",
        ),
        "run_twice": _R(
            "sandbox_entrypoint",
            "Only one run may exist, and it must be the last definition in the file.",
        ),
        "module_statement": _R(
            "sandbox_entrypoint",
            "Module level holds only the 4 imports, UPPER_CASE = literal constants, def and "
            "async def helpers, then run. Move this statement into a function.",
        ),
        "run_signature": _R(
            "sandbox_signature",
            "Write run's first line as async def run(context: RunContext, task: JsonValue) "
            "-> JsonValue: (the annotations may be left off). No other parameter name, "
            "default, *args or decorator is allowed.",
        ),
        "constant_name": _R(
            "sandbox_constant",
            "A module-level assignment makes a constant: one UPPER_CASE name and a literal, "
            "as in LIMIT = 3. Keep other variables inside a function.",
        ),
        "constant_duplicate": _R(
            "sandbox_constant",
            "This constant is already assigned above. Each constant is assigned once.",
        ),
        "constant_limit": _R(
            "sandbox_constant",
            "A module has at most 64 constants. Group values in a list or dict constant.",
        ),
        "constant_value": _R(
            "sandbox_constant",
            "A constant's value must be a literal: a number, a string, True, False, None, or "
            "a list or dict of them with str keys. No calls, names or f-strings.",
        ),
        "constant_change": _R(
            "sandbox_constant",
            "Constants cannot be changed at module level. Compute new values inside a "
            "function.",
        ),
        "constant_no_value": _R(
            "sandbox_constant",
            "An annotation with no value, such as LIMIT: int, declares nothing. Give it a "
            "value, LIMIT: int = 3, or delete the line.",
        ),
        # Helpers.
        "helper_name": _R(
            "sandbox_helper",
            "Helper names are lower_case: a lowercase letter, then lowercase letters, digits "
            "or _, at most 64 characters.",
        ),
        "helper_reserved": _R(
            "sandbox_helper", "<name> is reserved, so no helper can take it. Rename the helper."
        ),
        "helper_builtin_name": _R(
            "sandbox_helper",
            "<name> is a builtin, so no helper can take that name. Call <name>(...) "
            "directly, or rename the helper, for example to my_<name>.",
        ),
        "helper_duplicate": _R(
            "sandbox_helper",
            "A helper with this name is already defined above. Give each helper its own "
            "name.",
        ),
        "helper_limit": _R(
            "sandbox_helper", "A module has at most 32 helpers. Merge the small ones."
        ),
        "helper_decorator": _R(
            "sandbox_helper", "Helpers take no decorators. Delete the @ line."
        ),
        "helper_type_params": _R(
            "sandbox_helper",
            "Type parameters such as def f[T](x) are not supported. Delete the [T]; plain "
            "annotations are fine.",
        ),
        "helper_parameter_kind": _R(
            "sandbox_helper",
            "Helpers take plain positional parameters: def name(a, b, c=0). No *args, "
            "**kwargs, / or * markers.",
        ),
        "helper_parameter_count": _R(
            "sandbox_helper",
            "A helper takes at most 8 parameters. Pass a dict or a list to group values.",
        ),
        "helper_parameter_name": _R(
            "sandbox_helper",
            "This parameter name is not allowed: no reserved name (task, run, call, step and "
            "the like), no CONSTANT and nothing starting with __; context may only be the "
            "first parameter of an async helper. Rename it.",
        ),
        "helper_default_value": _R(
            "sandbox_helper",
            "A default value must be a literal or a module CONSTANT, as in def name(a, n=3) "
            "or def name(a, n=LIMIT).",
        ),
        "helper_async_context": _R(
            "sandbox_helper",
            "An async helper's first parameter must be named context, with no default: async "
            "def name(context, a). Call it as await name(context, a).",
        ),
        "helper_sync_await": _R(
            "sandbox_helper",
            "A def helper cannot await or use context. Make it async def name(context, ...) "
            "and call it with await name(context, ...).",
        ),
        "nested_def": _R(
            "sandbox_helper",
            "Functions cannot be defined inside a function. Move this def to module level and "
            "pass it what it needs as parameters.",
        ),
        "async_helper_await": _R(
            "sandbox_helper",
            "An async helper is called with await and context first: await name(context, "
            "...).",
        ),
        "async_helper_context_arg": _R(
            "sandbox_helper",
            "Pass context as the first argument of an async helper: await name(context, ...).",
        ),
        "recursion": _R(
            "sandbox_recursion",
            "Helpers cannot call themselves, directly or through other helpers. Use a loop, "
            "with a list as a work stack if needed.",
        ),
        "annotation_form": _R(
            "sandbox_expression",
            "An annotation must name a type, such as str, list[str], dict[str, int] or "
            "int | None. Annotations are never evaluated, so calls and other expressions "
            "cannot appear in one.",
        ),
        # Statements.
        "assign_chained": _R(
            "sandbox_assignment", "Assign one target at a time: a = 0, then b = 0."
        ),
        "assign_task": _R(
            "sandbox_assignment",
            "task is run's input and cannot be reassigned. Store the new value under another "
            "name.",
        ),
        "assign_context": _R(
            "sandbox_assignment", "context cannot be assigned. Use another variable name."
        ),
        "assign_reserved": _R(
            "sandbox_assignment", "<name> is reserved and cannot be a variable. Use another name."
        ),
        "assign_constant": _R(
            "sandbox_assignment",
            "This name is a module constant, which is read-only. Use a new lower_case name "
            "for the changed value.",
        ),
        "assign_helper": _R(
            "sandbox_assignment", "This name belongs to a helper. Use another variable name."
        ),
        "assign_name": _R(
            "sandbox_assignment",
            "Variable names are letters, digits and _, at most 64 characters, and cannot "
            "start with __.",
        ),
        "assign_target": _R(
            "sandbox_assignment",
            "Assign to a name, to an item of a local variable (d[\"k\"] = v), or unpack into "
            "names (a, b = pair). Attributes do not exist.",
        ),
        "assign_unpack": _R(
            "sandbox_assignment", "Unpack into 2 to 8 plain names: a, b = pair."
        ),
        "assign_slice": _R(
            "sandbox_assignment",
            "A slice cannot be assigned. Build the new list instead: l = l[:i] + new + l[j:].",
        ),
        "assign_root": _R(
            "sandbox_assignment",
            "Only items of a local variable can be assigned: d[\"k\"] = v. task, context and "
            "constants are read-only; bind the value to a local name first.",
        ),
        "augmented_op": _R(
            "sandbox_assignment",
            "Only +=, -=, *=, //= and %= exist. Write x = x / 2 for division.",
        ),
        "mutate_root": _R(
            "sandbox_assignment",
            "append, extend, insert, pop and set_key change their first argument, which must "
            "be a plain local variable such as out. Bind an inner list or dict to a name "
            "first: inner = d[\"k\"].",
        ),
        "for_target": _R(
            "sandbox_assignment",
            "A for loop or a comprehension binds one name, two or three: for x in values, for "
            "k, v in items(d) or for a, b, c in zip(l1, l2, l3).",
        ),
        "loop_else": _R(
            "sandbox_statement",
            "for ... else and while ... else are not supported. Use a flag variable.",
        ),
        "loop_depth": _R(
            "sandbox_loop_depth", "Loops nest at most 6 deep. Move an inner loop into a helper."
        ),
        "break_outside_loop": _R(
            "sandbox_statement", "break and continue work only inside a loop."
        ),
        "try_shape": _R(
            "sandbox_statement",
            "Write try: with exactly one except, such as except: or except ValueError as e:, "
            "and no else or finally.",
        ),
        "except_shape": _R(
            "sandbox_statement",
            "Write except:, except ValueError: or except (KeyError, TypeError) as e:, naming "
            "only " + _EXCEPTIONS + ". Each catches every data error, and e holds its code.",
        ),
        "expression_statement": _R(
            "sandbox_statement",
            "This line computes a value and drops it. Assign it, return it, or call "
            "something that acts: append(out, x), clean(x), await context.call(p).",
        ),
        "annotation_no_value": _R(
            "sandbox_statement",
            "An annotation with no value, such as n: int, declares nothing. Give it a value, "
            "n: int = 0, or delete the line.",
        ),
        "raise": _R(
            "sandbox_statement",
            "raise is not supported. Return a fallback value instead; a data error inside "
            "try is caught by except.",
        ),
        "assert": _R(
            "sandbox_statement",
            "assert is not supported. Write if not ok: and return a fallback value.",
        ),
        "with": _R(
            "sandbox_statement", "with is not supported: there are no files or other resources."
        ),
        "del": _R(
            "sandbox_statement",
            "del is not supported. Reassign the name, or build the value again without the "
            "item.",
        ),
        "global": _R(
            "sandbox_statement",
            "global and nonlocal are not supported. Pass values in as parameters and return "
            "results.",
        ),
        "class": _R(
            "sandbox_statement",
            "Classes are not supported. Use dicts for records and helpers for behavior.",
        ),
        "async_block": _R(
            "sandbox_statement",
            "async for and async with are not supported. Await capabilities inside a plain "
            "for loop.",
        ),
        "match": _R("sandbox_statement", "match is not supported. Use if and elif."),
        "import_inside": _R(
            "sandbox_statement",
            "Imports are not supported inside a function, and no module exists to import. "
            "Use the builtins.",
        ),
        "statement": _R(
            "sandbox_statement",
            "This statement is not supported. The language card lists the statements that "
            "exist.",
        ),
        # Expressions.
        "literal": _R(
            "sandbox_expression",
            "Literals are None, True, False, ints below 2**63, finite floats and strings. No "
            "bytes, complex numbers or Ellipsis.",
        ),
        "unknown_name": _R(
            "sandbox_expression",
            "This name is not defined here. Assign it first, take it as a parameter, or use "
            "a module CONSTANT; builtins and helpers can only be called.",
        ),
        "context_value": _R(
            "sandbox_expression",
            "context is not a value. Use it only as await context.call(...), await "
            "context.harness(...), or as the first argument of an async helper.",
        ),
        "starred": _R(
            "sandbox_expression",
            "* unpacking is not supported. Join lists with a + b or extend(out, items).",
        ),
        "dict_unpack": _R(
            "sandbox_dictionary",
            "{**d} is not supported. Copy the keys in a for loop, or set them one by one.",
        ),
        "slice_step": _R("sandbox_expression", "A slice step must be 1 or -1: s[a:b] or l[::-1]."),
        "operator": _R(
            "sandbox_expression",
            "This operator does not exist here. Use + - * / // % on numbers (+ also joins "
            "strs and lists), and, or, not, and comparisons; no **, bit operators, @, unary + "
            "or ~.",
        ),
        "is_none_only": _R(
            "sandbox_comparison",
            "is and is not compare only with None: x is None. Compare values with ==.",
        ),
        "fstring_format": _R(
            "sandbox_expression",
            "f-strings take plain {expr} parts only, with no !r and no :format. Round with "
            "round(x, 2) first.",
        ),
        "lambda": _R("sandbox_expression", "lambda is not supported. Define a helper with def."),
        "generator": _R(
            "sandbox_expression",
            "A generator expression works only as the list argument of any, all, sum, min, "
            "max, sorted, list or join. Anywhere else write a list comprehension in brackets: "
            "[x for x in values].",
        ),
        "set_literal": _R(
            "sandbox_expression",
            "Sets do not exist. Use a list, or a dict with True values for fast lookups.",
        ),
        "walrus": _R(
            "sandbox_expression", "The := operator is not supported. Assign on its own line first."
        ),
        "attribute": _R(
            "sandbox_expression",
            "Values have no attributes. Use builtins such as keys(d), get(d, k) and len(x).",
        ),
        "module_use": _R(
            "sandbox_expression",
            "This name comes from an import, and no module exists here. Use the builtins the "
            "language card lists.",
            "<name> is not available, since no module can be imported: <advice>",
        ),
        "class_use": _R(
            "sandbox_expression",
            "Classes are not supported, so this name stands for nothing here. Keep each "
            "record in a dict instead, as in {\"name\": value}.",
        ),
        "yield": _R("sandbox_expression", "yield is not supported. Build a list and return it."),
        "expression": _R(
            "sandbox_expression",
            "This expression is not supported. The language card lists the expressions that "
            "exist.",
        ),
        # Calls.
        "callee": _R(
            "sandbox_expression",
            "Only builtins and helpers can be called, by name: name(args), or by a listed "
            "method spelling such as s.split(sep).",
        ),
        "method_unknown": _R(
            "sandbox_expression",
            "Values have no method by this name. The methods that exist are the listed "
            "builtins written after their first argument, such as s.split(sep), "
            "l.append(x) and d.get(k).",
            "<name> does not exist here: <advice>",
        ),
        "method_arity": _R(
            "sandbox_call",
            "This method call has the wrong number of arguments. The value before the dot "
            "is the first argument of <form>.",
        ),
        "missing_function": _R(
            "sandbox_call",
            "There is no function by this name. Use a builtin the language card lists, or "
            "define a helper.",
            "<name> does not exist here: <advice>",
        ),
        "builtin_arity": _R(
            "sandbox_call",
            "This call to <name> has the wrong number of arguments. Its form is <form>.",
        ),
        "builtin_keyword": _R(
            "sandbox_call",
            "Builtins take positional arguments only; the one keyword is sorted(l, "
            "reverse=True).",
        ),
        "call_starred": _R(
            "sandbox_call",
            "Pass arguments one by one: f(a, b). *args and **kwargs are not supported in "
            "calls.",
        ),
        "helper_arity": _R(
            "sandbox_call",
            "This helper call passes the wrong number of arguments. Pass one value per "
            "parameter, in order; parameters with defaults may be left off at the end.",
        ),
        "helper_keyword": _R(
            "sandbox_call",
            "Helpers take positional arguments only: name(a, 5), not name(a, n=5).",
        ),
        "isinstance_form": _R(
            "sandbox_call",
            "Write isinstance(x, T) with T one of str, int, float, bool, list and dict, or a "
            "tuple of them.",
        ),
        # Capabilities.
        "await_capability": _R(
            "sandbox_capability",
            "This is a capability and must be awaited: await context.call(...), await "
            "execute_preset(...), await context.harness(...).",
        ),
        "await_target": _R(
            "sandbox_capability",
            "Only capabilities and async helpers can be awaited: await context.call(...), "
            "await execute_preset(...), await context.harness(...) or await name(context, "
            "...).",
        ),
        "capability_unknown": _R(
            "sandbox_capability",
            "context offers call and harness, plus observe and step for games. Nothing else "
            "exists on it.",
        ),
        "game_only": _R(
            "sandbox_capability",
            "observe and step exist only when SPEC.md's execution kind is a game.",
        ),
        "call_args": _R(
            "sandbox_call",
            "Write await context.call(prompt) or await context.call(prompt, max_tokens), "
            "with optional system=... and images=[...]; max_output_tokens= replaces the "
            "second argument.",
        ),
        "game_args": _R(
            "sandbox_call",
            "Write await context.observe() with no arguments and await context.step(action) "
            "with one.",
        ),
        "harness_args": _R(
            "sandbox_call",
            "Write await context.harness(\"name\", value): the literal name of a sub-harness "
            "from forge_spawn, then one value.",
        ),
        "harness_unbound": _R(
            "sandbox_binding_mismatch",
            "context.harness needs the literal name of a sub-harness that forge_spawn made "
            "in this session. Spawn it first, or remove the call.",
        ),
        "preset_shape": _R(
            "sandbox_preset_call",
            "Call the bound preset with six positional arguments, as the starting harness.py "
            "does: await execute_preset(NAME, PARAMS, COMPONENTS, SPEC, context, value).",
        ),
        "preset_literal": _R(
            "sandbox_literal_required",
            "The first four arguments of execute_preset must be the binding's literal "
            "values, or UPPER_CASE constants that hold them. " + _BINDING,
        ),
        "binding_preset": _R(
            "sandbox_binding_mismatch",
            "The preset name in execute_preset differs from this harness's binding. "
            + _BINDING,
        ),
        "binding_params": _R(
            "sandbox_binding_mismatch",
            "The params argument of execute_preset differs from this harness's binding. "
            + _BINDING,
            "The params argument of execute_preset differs from this harness's binding, "
            "first at the key <name>. " + _BINDING,
        ),
        "binding_components": _R(
            "sandbox_binding_mismatch",
            "The components argument of execute_preset differs from this harness's binding. "
            + _BINDING,
        ),
        "binding_spec": _R(
            "sandbox_binding_mismatch",
            "The spec argument of execute_preset differs from this harness's binding. "
            + _BINDING,
            "The spec argument of execute_preset differs from this harness's binding, first "
            "at the key <name>. " + _BINDING,
        ),
    }
)
del _R, _NAMED, _EXCEPTIONS

# Python names a model often reaches for that this language does not have, each with the
# form that does the job here. Both are fixed text: a refusal names one only when the
# source used exactly that name, so no other source text is ever shown.
ALTERNATIVES: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Functions.
        "print": "nothing is printed; return the value, or check it with forge_dryrun.",
        "reversed": "use l[::-1].",
        "type": "use kind(x), or isinstance(x, str).",
        "dict": "use a literal such as {} or {\"k\": v}, or a comprehension such as "
        "{k: v for k, v in pairs}.",
        "set": "use a set comprehension {x for x in values}, which gives a list without repeats.",
        "tuple": "use a list such as [a, b].",
        "map": "use a list comprehension such as [f(x) for x in values].",
        "filter": "use a list comprehension such as [x for x in values if x].",
        "getattr": "values have no attributes; use get(d, \"k\").",
        "hasattr": "use \"k\" in d.",
        "repr": "use to_json(x) or str(x).",
        "format": "use an f-string such as f\"{x}\".",
        "next": "use a for loop with break, or at(found, 0) after found = "
        "[x for x in values if x].",
        "iter": "use a for loop over the value.",
        "divmod": "use a // b and a % b.",
        "pow": "multiply in a loop: ** does not exist.",
        "open": "there are no files.",
        "input": "the input is task.",
        "eval": "nothing is evaluated from text; parse JSON with parse_json(s).",
        "exec": "nothing is evaluated from text.",
        # Methods.
        "isnumeric": "use s.isdigit().",
        "isdecimal": "use s.isdigit().",
        "add": "use append(l, x) after if x not in l:, or d[x] = True.",
        "sort": "use l = sorted(l).",
        "reverse": "use l = l[::-1].",
        "index": "use find(s, sub) for a str, or a for loop over enumerate(l) for a list.",
        "remove": "build a new list without the item in a for loop.",
        "clear": "assign a new empty value: l = [].",
        "update": "set the keys one by one: d[k] = v.",
        "setdefault": "use if k not in d: d[k] = default.",
        "title": "use upper and lower on slices.",
        "capitalize": "use upper(s[:1]) + lower(s[1:]).",
        "zfill": "add \"0\" characters in a loop, or use an f-string with str(x).",
        "partition": "use split(s, sep) and join the parts after the first.",
        # Regular expressions, as methods of a pattern or a match.
        **dict.fromkeys(
            ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "group"),
            "there are no regular expressions; use find, split, replace, startswith and "
            "endswith, or test characters in a for loop.",
        ),
    }
)

# The modules an engineer model imported in the G1 sessions (and a few it is likely to),
# each with the builtins that replace it. A refusal names a module only when the source
# imported exactly that name; no module is ever imported.
MODULES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "json": "json.loads(s) is parse_json(s), which gives None for text that is not JSON, "
        "json.dumps(x) is to_json(x), and extract_json(s) finds JSON inside prose or a code "
        "fence.",
        "re": "there are no regular expressions; use find, split, replace, startswith, "
        "endswith and count, or test characters in a for loop.",
        "typing": "annotations are never evaluated, so Any, Optional, List and Dict work in "
        "them without an import.",
        "dataclasses": "there are no classes; keep each record in a dict, as in "
        "{\"name\": value}.",
        "enum": "there are no classes; use UPPER_CASE string constants, as in KIND_A = \"a\".",
        "asyncio": "the host awaits run itself, so await each capability in turn and call no "
        "event loop.",
        "architect": "only the 4 imports at the top exist, every builtin such as "
        "extract_json needs no import, and a sub-harness is called as await "
        "context.harness(\"name\", value).",
        "forge": "forge_check, forge_dryrun, forge_measure and forge_submit are tools you "
        "call, not functions harness.py can use.",
        "math": "use round, abs, min, max, // and %; there is no **, so multiply instead.",
        "collections": "use a dict, with get(d, k, 0) + 1 for counts.",
        "string": "use s.isdigit(), s.isalpha() and the other is tests, or write the "
        "characters you need as a constant, as in DIGITS = \"0123456789\".",
        "itertools": "use for loops with enumerate, zip and range.",
        "functools": "write a helper with def.",
        "decimal": "use int or float, with round(x, n).",
        "copy": "use l.copy() or d.copy(), which copy the whole value.",
    }
)
_MODULE_RULES: Final = frozenset({"import_module", "module_use"})

_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def refusal_message(
    code: str | None, rule: str | None, name: str | None, line: int | None
) -> str:
    """The one or two plain sentences a refusal shows: where, what broke, the allowed form.

    Built only from ``RULES``, ``ALTERNATIVES``, ``MODULES``, ``BUILTINS`` and ``HINTS``;
    ``name`` is one of the language's own identifiers or a key of those tables (never other
    source text) and is used only when it is a plain identifier. Empty when nothing is
    known about the code.
    """
    where = f"Line {line}: " if type(line) is int and line >= 1 else ""
    known = name if isinstance(name, str) and _IDENTIFIER.fullmatch(name) else None
    entry = RULES.get(rule) if isinstance(rule, str) else None
    if entry is None:
        hint = HINTS.get(code, "") if isinstance(code, str) else ""
        return where + hint if hint else ""
    form = BUILTINS[known].signature if known is not None and known in BUILTINS else "the builtin"
    table = MODULES if rule in _MODULE_RULES else ALTERNATIVES
    advice = table.get(known, "") if known is not None else ""

    def fill(text: str) -> str:
        return (
            text.replace("<name>", known or "this name")
            .replace("<form>", form)
            .replace("<advice>", advice)
        )

    if entry.named and known is not None and ("<advice>" not in entry.named or advice):
        return where + fill(entry.named)
    return where + fill(entry.text)


# ---------------------------------------------------------------- the language card

_BUILTIN_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Values",
        ("len", "str", "int", "float", "bool", "abs", "round", "min", "max", "sum", "any",
         "all", "sorted", "range", "enumerate", "zip", "list", "kind"),
    ),
    (
        "Lists and dicts",
        ("keys", "values", "items", "get", "at", "append", "extend", "insert", "pop",
         "set_key", "copy"),
    ),
    (
        "Strings",
        ("split", "join", "strip", "lstrip", "rstrip", "lower", "upper", "startswith",
         "endswith", "replace", "find", "rfind", "count", "splitlines", "isdigit", "isalpha",
         "isalnum", "isspace", "isupper", "islower", "chr", "ord"),
    ),
    ("JSON", ("parse_json", "extract_json", "to_json", "digest")),
    (
        "Grids, for games",
        ("grid_diff", "diff_cells", "grid_text", "objects", "count_colors", "find_cells",
         "crop", "grid_equal"),
    ),
)

_WIDTH: Final = 84
_METHOD_FORMS: Final = (
    "s.split(sep)", "s.split()", "sep.join(parts)", "s.strip()", "s.rstrip(chars)",
    "s.lower()", "s.upper()", "s.startswith(p)", "s.endswith(p)", "s.replace(a, b)",
    "s.find(sub)", "s.rfind(sub)", "s.count(sub)", "s.splitlines()", "s.isdigit()",
    "d.get(k[, default])", "d.keys()", "d.values()", "d.items()", "d.copy()", "l.append(v)",
    "l.extend(l2)", "l.insert(i, v)", "l.pop()", "l.copy()",
)

_CARD_HEAD = '''\
# Sandbox language card (v2)

harness.py is read by an interpreter, never run as Python. Only what this card
lists exists; forge_check lists every line it refuses, each with the rule and the
allowed form. Values are JSON: None, True, False, int, float, str, list, and dict
with str keys, plus the range that range() returns. Nothing reaches files, the
network, the clock or Python objects, and every step costs fuel.

## File shape

```python
"""What this harness does and why, in 3 to 6 lines."""
from __future__ import annotations
from pydantic import JsonValue
from architect.presets import execute_preset
from architect.runtime import RunContext

LIMIT = 3


def clean(text: str, width: int = LIMIT) -> str:
    return strip(text)[:width]


async def ask(context: RunContext, prompt: str) -> str:
    return await context.call(prompt, 256)


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    reply = await ask(context, "Answer in JSON: " + to_json(task))
    data = extract_json(reply)
    if data is None:
        return {}
    return data
```

- Imports: only these 4 lines, each optional, at the top, each once. No module can
  be imported; a refusal names the builtins that replace json, re, typing and others.
- Constants: UPPER_CASE = literal (a number, string, True, False, None, or a list
  or dict of them). They are read-only; reading a list or dict constant copies it.
- run comes last: async def run(context, task):, with exactly those two names. An
  if __name__ == "__main__": block at module level is ignored; nothing in it runs.

## Helper rules

- def name(a, b): is a sync helper. async def name(context, a): is an async
  helper, called as await name(context, a); its first parameter is always
  named context.
- At most 32 helpers, each defined once. A helper name is lower_case and is not
  a builtin (lower, split, get and the rest) or one of run, context, task,
  execute_preset, call, observe, step, harness and isinstance.
- At most 8 plain parameters. A default is a literal or a CONSTANT, copied for
  each call, as in def f(x, n=3). No *args, **kwargs, / or * markers, and no
  decorators.
- Call a helper with positional arguments: f(x) or f(x, 5). Parameters with
  defaults may be left off at the end. No keyword arguments.
- A sync helper never awaits and never uses context. No helper calls itself,
  directly or through other helpers.
- A helper sees its parameters, its own variables and the constants. A variable
  may reuse a builtin's name (count = 0); a call by that name still reaches the
  builtin.
- Type annotations are allowed wherever Python allows them and are ignored, never
  evaluated: def f(s: str) -> list[str]: and n: int = 0 both work. An annotation
  names a type; calls cannot appear in one.
'''

_CARD_TAIL = '''\
## Capabilities

Always awaited:

- await context.call(prompt[, max_tokens], system=S, images=L) returns the model's
  reply as a str.
- await execute_preset(NAME, PARAMS, COMPONENTS, SPEC, context, value) runs the
  bound preset. Copy the first four arguments exactly from the starting
  harness.py or a template (UPPER_CASE constants that hold them are fine); only
  value changes.
- await context.harness("name", value) calls a sub-harness made by forge_spawn.
- Games only: await context.observe() and await context.step(action).

## Comprehensions

- They run as the for loops they stand for, at the same fuel, and their names stay
  inside them. {x for x in l} gives a list without repeats, in first-seen order.
- A generator expression is only the list argument of any, all, sum, min, max,
  sorted, list or join; any and all stop at the first item that decides them.

## Refused, and why

- Imports beyond the 4, classes, global, nonlocal, with, del, raise, assert,
  lambda, yield and match: there are no modules, objects, files or exceptions
  here. Return a fallback value instead of raising.
- Other methods and attributes, such as l.sort(), d.update() or s.title(): use
  the builtins, or a short loop.
- Python functions this language lacks: print (return the value), reversed
  (l[::-1]), type (kind or isinstance), and dict(), set() and tuple() (use
  literals or comprehensions).
- is and is not with anything but None; **, bit operators and slice steps other
  than 1 or -1; f-string !r and :format.
- Keyword arguments, except sorted(l, reverse=True) and context.call's system=,
  images= and max_output_tokens=.
- Reassigning task, context or a constant, and task["k"] = v.
- More than one except, or a type that is not one of Python's exception names
  (Exception, ValueError and the like); try with else or finally; for or while
  with else.
'''


_KEEP: Final = "\x00"  # a space that a wrapped line never breaks at


def _whole(form: str) -> str:
    return form.replace(" ", _KEEP)


def _wrap(text: str, bullet: bool = False) -> list[str]:
    indent = ("- ", "  ") if bullet else ("", "")
    lines = textwrap.wrap(
        text, width=_WIDTH, initial_indent=indent[0], subsequent_indent=indent[1]
    )
    return [line.replace(_KEEP, " ") for line in lines]


def _bullet(text: str) -> list[str]:
    return _wrap(text, bullet=True)


def _builtin_lines() -> list[str]:
    grouped = [name for _, names in _BUILTIN_GROUPS for name in names]
    if sorted(grouped) != sorted(BUILTINS):  # pragma: no cover - a table edit missed a group
        raise AssertionError("every builtin belongs to exactly one card group")
    lines = ["## Builtins", ""]
    for title, names in _BUILTIN_GROUPS:
        signatures = ", ".join(_whole(BUILTINS[name].signature) for name in names)
        lines += _bullet(f"{title}: {signatures}.")
    notes = (
        "min and max take one list or two values: max(l) or max(a, b). split(s) with no sep "
        "splits on runs of whitespace; strip, lstrip and rstrip remove whitespace, or the "
        "characters given. copy(x) copies a list or dict whole. get and at never raise; "
        "their default is None when left out. append, extend, insert, pop and set_key change "
        "their first argument, which must be a local variable. isinstance(x, T) takes "
        + ", ".join(sorted(TYPE_NAMES))
        + " or a tuple of them; as in Python, True is an int."
    )
    methods = (
        "Methods are the same builtins written after their first argument: "
        + ", ".join(_whole(form) for form in _METHOD_FORMS)
        + ". No other method or attribute exists."
    )
    return [*lines, "", *_wrap(notes), "", *_wrap(methods)]


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows)
    return lines


def language_markdown() -> str:
    """The deterministic ASCII language card: ``LANGUAGE.md`` and the end of Forge's prompt."""
    lines = [_CARD_HEAD.rstrip("\n"), "", "## Statements", ""]
    lines += _table(("Form", "Example"), list(STATEMENTS.items()))
    lines += ["", "## Expressions", ""]
    lines += _table(("Form", "Example"), list(EXPRESSIONS.items()))
    lines += ["", *_builtin_lines(), "", _CARD_TAIL.rstrip("\n"), "", "## Errors and limits", ""]
    lines += _bullet(
        "Data errors, caught by try/except: " + ", ".join(sorted(DATA_ERROR_CODES)) + "."
    )
    lines += _bullet(
        "Fatal errors, which end the run: " + ", ".join(sorted(FATAL_ERROR_CODES)) + "."
    )
    lines += _bullet(
        f"Limits: {SOURCE_LIMIT_V2 // 1024} KiB of source, {NODE_LIMIT_V2:,} syntax nodes, "
        f"{HELPER_LIMIT} helpers, {CONSTANT_LIMIT} constants, {PARAMETER_LIMIT} parameters, "
        f"loops {LOOP_DEPTH_LIMIT} deep, strs of {STRING_LIMIT:,} chars, lists and dicts of "
        f"{CONTAINER_LIMIT:,} entries, values {NESTING_LIMIT} deep, ranges of "
        f"{RANGE_LIMIT:,} items."
    )
    lines += _bullet(
        f"Fuel: the binding sets it (default {DEFAULT_FUEL:,}). Every evaluated node costs "
        f"1, a builtin 1 more per {FUEL_UNIT} units of work, and a capability call "
        f"{RPC_FUEL} plus 1 per {RPC_BYTES_PER_FUEL} bytes sent and received. Loops must end."
    )
    return "\n".join(lines) + "\n"
