"""Private worker entry point. Interpret validated syntax; never execute authored Python.

The subprocess is defense in depth and a cancellation boundary, not an OS jail.
The only objects visible to the language are plain JSON, never Python capabilities.
Language 1 keeps its original asynchronous interpreter unchanged. Language 2 runs
a synchronous, fuel-metered interpreter in a dedicated thread; its capability
RPCs are the same line-delimited JSON exchange with the host.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Final, cast

# -I removes cwd/PYTHONPATH. Resolve only the installed package; never add a
# writable checkout parent where a model could plant shadow dependency modules.
from pydantic import JsonValue, ValidationError  # noqa: E402

from architect.models.images import MAX_IMAGES_PER_REQUEST, ImageRef, ImageStub  # noqa: E402
from architect.presets import execute_preset  # noqa: E402
from architect.runtime import RunContext  # noqa: E402
from architect.runtime import sandbox_language as language  # noqa: E402
from architect.runtime._proc_limits import limit_self  # noqa: E402
from architect.runtime._sandbox_builtins import (  # noqa: E402
    BUILTIN_FUNCTIONS,
    MUTATING_FUNCTIONS,
    DataError,
    Fatal,
    Meter,
    RangeValue,
    SandboxFault,
    arithmetic_charge,
    check_json,
    check_str,
    contains,
    equal,
    is_number,
    number,
    ordering_charge,
    render,
    search_charge,
    store,
)
from architect.runtime.sandbox import (  # noqa: E402
    MESSAGE_LIMIT,
    SandboxError,
    _Comprehension,
    _decode,
    _encode,
    _literal,
    _ProgramV2,
    _utf8_size,
    _validated_program,
    _validated_tree,
)
from architect.spec import HarnessArtifact  # noqa: E402


def _write(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _read() -> bytes:
    return sys.stdin.buffer.readline(MESSAGE_LIMIT + 1)


def _send(message: object) -> None:
    _write(_encode(message))


def _receive() -> JsonValue:
    return _decode(_read())


class _RPC:
    async def preset(self, task: JsonValue) -> JsonValue:
        _send({"op": "preset", "task": task})
        response = _receive()
        if not isinstance(response, dict) or set(response) != {"value"}:
            raise SandboxError("sandbox_invalid_message")
        return response["value"]

    async def call(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        images: Sequence[ImageRef] = (),
        response_schema: dict[str, JsonValue] | None = None,
    ) -> str:
        if type(prompt) is not str or (
            max_output_tokens is not None
            and (type(max_output_tokens) is not int or max_output_tokens < 1)
        ):
            raise SandboxError("sandbox_invalid_call")
        # Only stubs exist in the worker; image bytes here are a protocol violation.
        if not all(type(image) is ImageStub for image in images):
            raise SandboxError("sandbox_invalid_call")
        if response_schema is not None and type(response_schema) is not dict:
            raise SandboxError("sandbox_invalid_call")
        message: dict[str, JsonValue] = {
            "op": "call",
            "prompt": prompt,
            "tokens": max_output_tokens,
        }
        if images:
            message["images"] = [image.sha256 for image in images]
        # Only a bound preset passes a schema (its spec's ``reply_json`` "schema"); the
        # host forwards it as ``response_schema`` and refuses it under any other mode.
        if response_schema is not None:
            message["schema"] = response_schema
        _send(message)
        response = _receive()
        if (
            not isinstance(response, dict)
            or set(response) != {"value"}
            or not isinstance(response["value"], str)
        ):
            raise SandboxError("sandbox_invalid_message")
        return response["value"]


class _Return(Exception):
    def __init__(self, value: JsonValue) -> None:
        self.value = value


class _Interpreter:
    def __init__(self, task: JsonValue) -> None:
        self.locals: dict[str, JsonValue] = {"task": task}
        self.rpc = _RPC()

    async def block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Return):
                raise _Return(None if statement.value is None else await self.expr(statement.value))
            if isinstance(statement, ast.Assign):
                name = cast(ast.Name, statement.targets[0]).id
                self.locals[name] = await self.expr(statement.value)
            elif isinstance(statement, ast.If):
                chosen = statement.body if await self.expr(statement.test) else statement.orelse
                await self.block(chosen)
            elif isinstance(statement, ast.Expr):
                await self.expr(statement.value)
            else:
                raise SandboxError("sandbox_statement")

    async def expr(self, node: ast.expr) -> JsonValue:
        value = await self._expr(node)
        _encode(value)  # Bound every intermediate value, not only final messages.
        return value

    async def _expr(self, node: ast.expr) -> JsonValue:
        if isinstance(node, ast.Constant):
            return cast(JsonValue, node.value)
        if isinstance(node, ast.Name):
            return self.locals[node.id]
        if isinstance(node, ast.List):
            return [await self.expr(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            result: dict[str, JsonValue] = {}
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                result[cast(str, cast(ast.Constant, key_node).value)] = await self.expr(value_node)
            return result
        if isinstance(node, ast.Subscript):
            container, key = await self.expr(node.value), await self.expr(node.slice)
            if isinstance(container, dict) and isinstance(key, str):
                return container[key]
            if isinstance(container, (str, list)) and type(key) is int:
                return container[key]
            raise SandboxError("sandbox_index")
        if isinstance(node, ast.UnaryOp):
            operand = await self.expr(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if type(operand) in (int, float):
                return -cast(int | float, operand)
            raise SandboxError("sandbox_unary")
        if isinstance(node, ast.BinOp):
            left, right = await self.expr(node.left), await self.expr(node.right)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            raise SandboxError("sandbox_add")
        if isinstance(node, ast.BoolOp):
            answer: JsonValue = None
            for item in node.values:
                answer = await self.expr(item)
                if isinstance(node.op, ast.And) and not answer:
                    break
                if isinstance(node.op, ast.Or) and answer:
                    break
            return answer
        if isinstance(node, ast.Compare):
            left = await self.expr(node.left)
            for op, item in zip(node.ops, node.comparators, strict=True):
                right = await self.expr(item)
                if isinstance(op, ast.Eq):
                    matched = left == right
                elif isinstance(op, ast.NotEq):
                    matched = left != right
                elif isinstance(right, (str, dict)) and isinstance(left, str):
                    matched = left in right
                elif isinstance(right, list):
                    matched = left in right
                else:
                    raise SandboxError("sandbox_comparison")
                if isinstance(op, ast.NotIn):
                    matched = not matched
                if not matched:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            value = await self.expr(node.args[0])
            if cast(ast.Name, node.func).id == "len":
                if not isinstance(value, (str, list, dict)):
                    raise SandboxError("sandbox_length")
                return len(value)
            # str(JSON) has deterministic JSON semantics, not object repr hooks.
            return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        if isinstance(node, ast.Await):
            call = cast(ast.Call, node.value)
            if isinstance(call.func, ast.Attribute):
                prompt = await self.expr(call.args[0])
                tokens: JsonValue = None
                if len(call.args) == 2:
                    tokens = await self.expr(call.args[1])
                elif call.keywords:
                    tokens = await self.expr(call.keywords[0].value)
                if not isinstance(prompt, str) or (tokens is not None and type(tokens) is not int):
                    raise SandboxError("sandbox_invalid_call")
                return await self.rpc.call(prompt, tokens)
            name, params, components, spec = [_literal(item) for item in call.args[:4]]
            config = spec.get("execution_config", {}) if isinstance(spec, dict) else {}
            if (
                name == "coder"
                or (isinstance(spec, dict) and spec.get("pipeline"))
                or (isinstance(config, dict) and config.get("kind") in {"game", "competition"})
            ):
                return await self.rpc.preset(await self.expr(call.args[5]))
            return await execute_preset(
                cast(str, name),
                cast(dict[str, JsonValue], params),
                cast(tuple[str, ...], components),
                cast(dict[str, JsonValue], spec),
                cast(RunContext, self.rpc),
                await self.expr(call.args[5]),
            )
        raise SandboxError("sandbox_expression")


# ------------------------------------------------------------------ language 2

_NORMAL: Final = 0
_BREAK: Final = 1
_CONTINUE: Final = 2
_RETURN: Final = 3
_MISSING: Final = object()
# Builtins whose result may be a container still inside their first argument.
_ALIASING: Final = frozenset({"get", "at"})
# ``isinstance(x, T)``: the value types each type name matches; as in Python, a bool is
# also an int.
_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "str": (str,),
    "int": (int, bool),
    "float": (float,),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}
_STACK_BYTES: Final = 64 * 2**20
_RECURSION_LIMIT: Final = 20_000
# Built before any run so reporting an out-of-memory failure needs no new allocation.
_MEMORY_FRAME: Final[dict[str, JsonValue]] = {"op": "error", "code": "sandbox_memory", "line": None}


def _truth(value: object) -> bool:
    if type(value) is RangeValue:
        raise DataError("sandbox_type")
    return bool(value)


def _index(container: object, key: object) -> object:
    """Read ``container[key]``: missing keys and indexes are catchable data errors."""
    kind = type(container)
    if kind is dict:
        if type(key) is not str:
            raise DataError("sandbox_type")
        try:
            return cast(dict[str, object], container)[key]
        except KeyError:
            raise DataError("sandbox_key") from None
    if kind is list or kind is str:
        if type(key) is not int:
            raise DataError("sandbox_type")
        try:
            return cast(Sequence[object], container)[key]
        except IndexError:
            raise DataError("sandbox_index") from None
    raise DataError("sandbox_type")


def _set_index(container: object, key: object, value: object) -> None:
    kind = type(container)
    if kind is dict:
        mapping = cast(dict[str, object], container)
        if type(key) is not str:
            raise DataError("sandbox_dictionary")
        if key not in mapping and len(mapping) >= language.CONTAINER_LIMIT:
            raise Fatal("sandbox_value_limit")
        mapping[key] = value
        return
    if kind is list:
        items = cast(list[object], container)
        if type(key) is not int:
            raise DataError("sandbox_type")
        index = key
        if not -len(items) <= index < len(items):
            raise DataError("sandbox_index")
        items[index] = value
        return
    raise DataError("sandbox_type")


class _InterpreterV2:
    """Walk validated language 2 syntax over JSON values, charging fuel per node.

    Helpers run as named AST blocks with a fresh locals dict; no function object
    is created. Control flow uses signal integers; data errors are the only
    exceptions a ``try`` catches.

    Nesting levels: value semantics make every container a node of exactly one tree,
    and a container never moves without being copied. ``self.level`` is an upper
    bound on the nesting level of the container an expression last produced (1 for
    a fresh value), and ``self.levels`` holds it for each local bound to a container
    inside another. Every store is bounded at ``level + 1``, so the depth-40 bound
    holds at construction even when mutating through an alias.
    """

    def __init__(self, program: _ProgramV2, meter: Meter) -> None:
        self.program = program
        self.meter = meter
        self.helpers = program.helpers
        self.constants: dict[str, object] = dict(program.constants)
        self.defaults = program.defaults
        self.frame: dict[str, object] = {}
        self.levels: dict[str, int] = {}
        self.level = 1
        self.return_level = 1
        self.returned: object = None
        self.return_line: int | None = None
        self.statements: dict[type, Callable[[ast.stmt], int]] = {
            ast.Return: self._return,  # type: ignore[dict-item]
            ast.Assign: self._assign,  # type: ignore[dict-item]
            ast.AnnAssign: self._annotated,  # type: ignore[dict-item]
            ast.AugAssign: self._augmented,  # type: ignore[dict-item]
            ast.If: self._if,  # type: ignore[dict-item]
            ast.For: self._for,  # type: ignore[dict-item]
            ast.While: self._while,  # type: ignore[dict-item]
            ast.Break: lambda node: _BREAK,
            ast.Continue: lambda node: _CONTINUE,
            ast.Pass: lambda node: _NORMAL,
            ast.Try: self._try,  # type: ignore[dict-item]
            ast.Expr: self._expression_statement,  # type: ignore[dict-item]
        }
        self.expressions: dict[type, Callable[[ast.expr], object]] = {
            ast.Constant: lambda node: cast(ast.Constant, node).value,
            ast.Name: self._name,  # type: ignore[dict-item]
            ast.List: self._list,  # type: ignore[dict-item]
            ast.Tuple: self._list,  # type: ignore[dict-item]
            ast.Dict: self._dict,  # type: ignore[dict-item]
            ast.Subscript: self._subscript,  # type: ignore[dict-item]
            ast.UnaryOp: self._unary,  # type: ignore[dict-item]
            ast.BinOp: self._binop,  # type: ignore[dict-item]
            ast.BoolOp: self._boolop,  # type: ignore[dict-item]
            ast.Compare: self._compare,  # type: ignore[dict-item]
            ast.IfExp: self._ifexp,  # type: ignore[dict-item]
            ast.JoinedStr: self._fstring,  # type: ignore[dict-item]
            ast.Call: self._call,  # type: ignore[dict-item]
            ast.Await: self._await,  # type: ignore[dict-item]
            _Comprehension: self._comprehension,  # type: ignore[dict-item]
        }

    # -------------------------------------------------------------- entry

    def execute(self, task: object) -> object:
        self.frame = {"task": task}
        signal = self.block(self.program.run.body)
        return self.returned if signal == _RETURN else None

    def block(self, statements: list[ast.stmt]) -> int:
        for statement in statements:
            signal = self.statement(statement)
            if signal:
                return signal
        return _NORMAL

    def statement(self, node: ast.stmt) -> int:
        meter = self.meter
        meter.remaining -= 1
        if meter.remaining < 0:
            raise Fatal("sandbox_fuel", node.lineno)
        handler = self.statements.get(type(node))
        if handler is None:
            raise SandboxError("sandbox_statement")
        try:
            return handler(node)
        except SandboxFault as exc:
            if exc.line is None:
                exc.line = node.lineno
            raise
        except OverflowError:
            # Defence in depth: builtins are total over unbounded task ints, but a
            # native int-to-C conversion that still overflows is a data error.
            raise DataError("sandbox_value", node.lineno) from None
        except MemoryError:
            raise Fatal("sandbox_memory", node.lineno) from None
        except RecursionError:
            # Unreachable by construction (depth <= 40, no recursion); kept fatal.
            raise Fatal("sandbox_value_limit", node.lineno) from None

    def ev(self, node: ast.expr) -> object:
        meter = self.meter
        meter.remaining -= 1
        if meter.remaining < 0:
            raise Fatal("sandbox_fuel", node.lineno)
        handler = self.expressions.get(type(node))
        if handler is None:
            raise SandboxError("sandbox_expression")
        try:
            return handler(node)
        except SandboxFault as exc:
            if exc.line is None:
                exc.line = node.lineno
            raise

    # -------------------------------------------------------------- statements

    def _return(self, node: ast.Return) -> int:
        if node.value is None:
            self.returned, self.return_level = None, 1
        else:
            self.returned = self.ev(node.value)
            self.return_level = self.level
        self.return_line = node.lineno
        return _RETURN

    def _bind(self, name: str, value: object, level: int) -> None:
        """Record the level of a local bound to a container inside another."""
        if level > 1 and (type(value) is list or type(value) is dict):
            self.levels[name] = level
        elif self.levels:
            self.levels.pop(name, None)

    def _lookup(self, name: str) -> object:
        try:
            return self.frame[name]
        except KeyError:
            raise Fatal("sandbox_unbound") from None

    def _assign(self, node: ast.Assign) -> int:
        return self._store(node.targets[0], self.ev(node.value))

    def _annotated(self, node: ast.AnnAssign) -> int:
        """``x: T = value`` is ``x = value``: the annotation is never evaluated."""
        return self._store(node.target, self.ev(cast(ast.expr, node.value)))

    def _store(self, target: ast.expr, value: object) -> int:
        if type(target) is ast.Name:
            self.frame[target.id] = value
            self._bind(target.id, value, self.level)
        elif type(target) is ast.Subscript:
            container, key, depth = self._slot(target)
            _set_index(container, key, store(value, self.meter, depth))
        else:
            level = self.level + 1
            names = cast(ast.Tuple, target).elts
            if type(value) is not list or len(cast(list[object], value)) != len(names):
                raise DataError("sandbox_value")
            for item, element in zip(names, cast(list[object], value), strict=True):
                name = cast(ast.Name, item).id
                self.frame[name] = element
                self._bind(name, element, level)
        return _NORMAL

    def _slot(self, target: ast.Subscript) -> tuple[object, object, int]:
        """Resolve ``root[k1]...[kn]`` to (container, last key, depth of the slot)."""
        keys: list[ast.expr] = []
        node: ast.expr = target
        while type(node) is ast.Subscript:
            keys.append(node.slice)
            node = node.value
        keys.reverse()
        name = cast(ast.Name, node).id
        container = self._lookup(name)
        for key in keys[:-1]:
            container = _index(container, self.ev(key))
        return container, self.ev(keys[-1]), self.levels.get(name, 1) + len(keys)

    def _augmented(self, node: ast.AugAssign) -> int:
        target, operator = node.target, type(node.op)
        if type(target) is ast.Name:
            name = target.id
            current = self._lookup(name)
            self.frame[name] = self._binary(operator, current, self.ev(node.value))
            if self.levels:
                self.levels.pop(name, None)  # A fresh list, a number or a str.
            return _NORMAL
        container, key, depth = self._slot(cast(ast.Subscript, target))
        current = _index(container, key)
        result = self._binary(operator, current, self.ev(node.value))
        _set_index(container, key, store(result, self.meter, depth))
        return _NORMAL

    def _if(self, node: ast.If) -> int:
        return self.block(node.body if _truth(self.ev(node.test)) else node.orelse)

    def _for(self, node: ast.For) -> int:
        iterable = self.ev(node.iter)
        item_level = self.level + 1  # Items of a list sit one level below it.
        kind = type(iterable)
        items: Sequence[object] | range
        if kind is list:
            items = list(cast(list[object], iterable))
            self.meter.charge(len(items) >> 8)
        elif kind is dict:
            items = list(cast(dict[str, object], iterable))
            self.meter.charge(len(items) >> 8)
        elif kind is str:
            items = cast(str, iterable)
        elif kind is RangeValue:
            items = cast(RangeValue, iterable).items()
        else:
            raise DataError("sandbox_type")
        target = node.target
        single = type(target) is ast.Name
        names = [] if single else [cast(ast.Name, item).id for item in cast(ast.Tuple, target).elts]
        meter, frame_body, levels = self.meter, node.body, self.levels
        if kind is not list:
            for name in names or [cast(ast.Name, target).id]:
                levels.pop(name, None)  # Keys, characters and integers.
        for item in items:
            meter.remaining -= 1
            if meter.remaining < 0:
                raise Fatal("sandbox_fuel")
            if single:
                name = cast(ast.Name, target).id
                self.frame[name] = item
                if kind is list:
                    levels[name] = item_level
            else:
                # Two names, or three for zip of three lists: the item unpacks exactly.
                if type(item) is not list or len(cast(list[object], item)) != len(names):
                    raise DataError("sandbox_value")
                values = cast(list[object], item)
                self.frame[names[0]] = values[0]
                self.frame[names[1]] = values[1]
                if len(names) == 3:
                    self.frame[names[2]] = values[2]
                if kind is list:
                    levels[names[0]] = levels[names[1]] = item_level + 1
                    if len(names) == 3:
                        levels[names[2]] = item_level + 1
            signal = self.block(frame_body)
            if signal == _BREAK:
                break
            if signal == _RETURN:
                return _RETURN
        return _NORMAL

    def _while(self, node: ast.While) -> int:
        while _truth(self.ev(node.test)):
            signal = self.block(node.body)
            if signal == _BREAK:
                break
            if signal == _RETURN:
                return _RETURN
        return _NORMAL

    def _try(self, node: ast.Try) -> int:
        frame = self.frame
        try:
            return self.block(node.body)
        except DataError as error:
            self.frame = frame
            handler = node.handlers[0]
            name = handler.name
            if name is None:
                return self.block(handler.body)
            # except T as e: e holds the error's fixed code while the handler runs, and is
            # unbound after it, however it ends, as Python unbinds it. Neither costs fuel.
            frame[name] = error.code
            self._bind(name, error.code, 1)
            try:
                return self.block(handler.body)
            finally:
                frame.pop(name, None)
                self.levels.pop(name, None)

    def _expression_statement(self, node: ast.Expr) -> int:
        self.ev(node.value)
        return _NORMAL

    # -------------------------------------------------------------- expressions

    def _name(self, node: ast.Name) -> object:
        value = self.frame.get(node.id, _MISSING)
        if value is not _MISSING:
            self.level = self.levels.get(node.id, 1)
            return value
        constant = self.constants.get(node.id, _MISSING)
        if constant is _MISSING:
            raise Fatal("sandbox_unbound")
        # Constants are read-only: a container is copied on every read.
        self.level = 1
        return store(constant, self.meter, 1)

    def _list(self, node: ast.List | ast.Tuple) -> object:
        meter = self.meter
        result = [store(self.ev(item), meter) for item in node.elts]
        if len(result) > language.CONTAINER_LIMIT:
            raise Fatal("sandbox_value_limit")  # pragma: no cover - the node cap is lower
        self.level = 1
        return result

    def _dict(self, node: ast.Dict) -> object:
        result: dict[str, object] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            key = self.ev(cast(ast.expr, key_node))
            if type(key) is not str:
                raise DataError("sandbox_dictionary")
            result[key] = store(self.ev(value_node), self.meter)
        self.level = 1
        return result

    def _subscript(self, node: ast.Subscript) -> object:
        container = self.ev(node.value)
        index = node.slice
        if type(index) is not ast.Slice:
            level = self.level + 1
            value = _index(container, self.ev(index))
            self.level = level
            return value
        bounds: list[int | None] = []
        for bound in (index.lower, index.upper):
            value = None if bound is None else self.ev(bound)
            if value is not None and type(value) is not int:
                raise DataError("sandbox_type")
            bounds.append(value)
        step = None if index.step is None else (1 if type(index.step) is ast.Constant else -1)
        if type(container) is str:
            text = container[bounds[0] : bounds[1] : step]
            self.meter.work(len(text))
            return text
        if type(container) is not list:
            raise DataError("sandbox_type")
        part = cast(list[object], container)[bounds[0] : bounds[1] : step]
        self.meter.work(len(part))
        self.level = 1
        return store(part, self.meter, 1)

    def _unary(self, node: ast.UnaryOp) -> object:
        operand = self.ev(node.operand)
        if type(node.op) is ast.Not:
            return not _truth(operand)
        if not is_number(operand):
            raise DataError("sandbox_type")
        return number(-cast(float, operand))

    def _binop(self, node: ast.BinOp) -> object:
        return self._binary(type(node.op), self.ev(node.left), self.ev(node.right))

    def _binary(self, operator: type, left: object, right: object) -> object:
        if operator is ast.Add:
            kinds = (type(left), type(right))
            if kinds == (str, str):
                text = cast(str, left) + cast(str, right)
                self.meter.charge(len(text) >> 8)
                return check_str(text)
            if kinds == (list, list):
                first, second = cast(list[object], left), cast(list[object], right)
                if len(first) + len(second) > language.CONTAINER_LIMIT:
                    raise Fatal("sandbox_value_limit")
                # Copy both sides as new roots: 1 fuel per copied element.
                meter = self.meter
                self.level = 1
                return cast(list[object], store(first, meter, 1)) + cast(
                    list[object], store(second, meter, 1)
                )
            if is_number(left) and is_number(right):
                arithmetic_charge(left, right, self.meter)
                return number(cast(float, left) + cast(float, right))
            raise DataError("sandbox_add")
        if not (is_number(left) and is_number(right)):
            raise DataError("sandbox_type")
        arithmetic_charge(left, right, self.meter)
        a, b = cast(float, left), cast(float, right)
        try:
            if operator is ast.Sub:
                return number(a - b)
            if operator is ast.Mult:
                return number(a * b)
            if b == 0:
                raise DataError("sandbox_value")
            if operator is ast.Div:
                return number(a / b)
            if operator is ast.FloorDiv:
                return number(a // b)
            return number(a % b)
        except (OverflowError, ZeroDivisionError, ValueError):
            raise DataError("sandbox_value") from None

    def _boolop(self, node: ast.BoolOp) -> object:
        conjunction = type(node.op) is ast.And
        answer: object = None
        for item in node.values:
            answer = self.ev(item)
            if _truth(answer) != conjunction:
                break
        return answer

    def _compare(self, node: ast.Compare) -> object:
        left = self.ev(node.left)
        for op, item in zip(node.ops, node.comparators, strict=True):
            right = self.ev(item)
            kind = type(op)
            if kind is ast.Is or kind is ast.IsNot:
                matched = (left is None) == (kind is ast.Is)
            elif type(left) is RangeValue or type(right) is RangeValue:
                raise DataError("sandbox_type")
            elif kind is ast.Eq:
                matched = equal(left, right, self.meter)
            elif kind is ast.NotEq:
                matched = not equal(left, right, self.meter)
            elif kind is ast.In or kind is ast.NotIn:
                container = type(right)
                if container is list:
                    matched = contains(left, cast(list[object], right), self.meter)
                elif (container is str or container is dict) and type(left) is str:
                    self.meter.work(len(cast(str, right)))
                    if container is str:
                        search_charge(self.meter, len(cast(str, right)), len(left))
                    matched = left in cast(str, right)
                else:
                    raise DataError("sandbox_type")
                if kind is ast.NotIn:
                    matched = not matched
            else:
                if not (
                    (is_number(left) and is_number(right))
                    or (type(left) is str and type(right) is str)
                ):
                    raise DataError("sandbox_type")
                if type(left) is str:
                    ordering_charge((left, right), 1, self.meter)
                a, b = cast(float, left), cast(float, right)
                if kind is ast.Lt:
                    matched = a < b
                elif kind is ast.LtE:
                    matched = a <= b
                elif kind is ast.Gt:
                    matched = a > b
                else:
                    matched = a >= b
            if not matched:
                return False
            left = right
        return True

    def _ifexp(self, node: ast.IfExp) -> object:
        return self.ev(node.body if _truth(self.ev(node.test)) else node.orelse)

    def _fstring(self, node: ast.JoinedStr) -> object:
        parts: list[str] = []
        total = 0
        for part in node.values:
            if type(part) is ast.FormattedValue:
                text = render(self.ev(part.value), self.meter)
            else:
                text = cast(str, cast(ast.Constant, part).value)
            total += len(text)
            if total > language.STRING_LIMIT:
                raise Fatal("sandbox_value_limit")
            parts.append(text)
        return "".join(parts)

    def _comprehension(self, node: _Comprehension) -> object:
        """A comprehension, desugared at check time: its loop nest runs here, as an
        expression, through the ordinary statement handlers, so it costs exactly the fuel
        and copies of the for loop it stands for (plus 1 for this node, as the read of
        that loop's result would cost).

        It has a scope of its own: the names it binds (its loop names and its result,
        which no source can spell) are restored to what they held, or removed, when it
        ends, however it ends. Nothing else can be bound inside it, so no other name of
        the enclosing scope changes. The return state ``any`` and ``all`` use to stop
        early is restored too.
        """
        frame, levels = self.frame, self.levels
        saved = [(name, frame.get(name, _MISSING), levels.get(name)) for name in node.names]
        returned = self.returned, self.return_level, self.return_line
        try:
            signal = self.block(node.body)
            if node.result is not None:
                value = frame[node.result]
            elif signal == _RETURN:
                value = self.returned
            else:  # pragma: no cover - the desugared body of any and all always returns
                raise SandboxError("sandbox_expression")
        finally:
            for name, old, level in saved:
                if old is _MISSING:
                    frame.pop(name, None)
                else:
                    frame[name] = old
                if level is None:
                    levels.pop(name, None)
                else:
                    levels[name] = level
            self.returned, self.return_level, self.return_line = returned
        self.level = 1
        return value

    def _call(self, node: ast.Call) -> object:
        func = node.func
        arguments: list[ast.expr]
        if type(func) is ast.Attribute:
            # A method spelling: ``value.name(args)`` is the builtin ``name(value, args)``.
            name = func.attr
            arguments = [func.value, *node.args]
        else:
            name = cast(ast.Name, func).id
            if name == "isinstance":
                return self._isinstance(node)
            if name not in BUILTIN_FUNCTIONS:
                return self._invoke(self.helpers[name], node.args)
            arguments = node.args
        builtin = BUILTIN_FUNCTIONS[name]
        first = self.ev(arguments[0])  # Every builtin takes at least one argument.
        target_level = self.level
        args = [first, *[self.ev(item) for item in arguments[1:]]]
        if node.keywords:
            args.append(self.ev(node.keywords[0].value))
        mutator = MUTATING_FUNCTIONS.get(name)
        if mutator is not None:
            value = mutator(self.meter, args, target_level + 1)
        elif name in _ALIASING:
            default_level = self.level
            value = builtin(self.meter, args)
            # A found item sits below the first argument; otherwise it is the default.
            missing = len(args) > 2 and value is args[2]
            self.level = default_level if missing else target_level + 1
            return value
        else:
            value = builtin(self.meter, args)
        self.level = 1
        return value

    def _isinstance(self, node: ast.Call) -> object:
        """``isinstance(x, T)``: only ``x`` is evaluated; T names types, checked statically."""
        value = self.ev(node.args[0])
        kinds = node.args[1]
        items = kinds.elts if isinstance(kinds, (ast.Tuple, ast.List)) else [kinds]
        names = [cast(ast.Name, item).id for item in items]
        self.meter.work(1)
        self.level = 1
        return any(type(value) in _TYPES[name] for name in names)

    def _invoke(
        self, helper: ast.FunctionDef | ast.AsyncFunctionDef, nodes: Sequence[ast.expr]
    ) -> object:
        """Run a helper; arguments alias, and keep their levels as parameter levels.

        A parameter left off at the end takes its default, a literal or a constant's
        value, copied for this call as a constant read is.
        """
        params = [arg.arg for arg in helper.args.args]
        if type(helper) is ast.AsyncFunctionDef:
            params = params[1:]  # ``context`` is never a value.
        frame: dict[str, object] = {}
        levels: dict[str, int] = {}
        for param, item in zip(params, nodes, strict=False):
            value = frame[param] = self.ev(item)
            if self.level > 1 and (type(value) is list or type(value) is dict):
                levels[param] = self.level
        if len(nodes) < len(params):
            defaults = self.defaults[helper.name]
            for param in params[len(nodes) :]:
                frame[param] = store(defaults[param], self.meter, 1)
        saved, saved_levels = self.frame, self.levels
        self.frame, self.levels = frame, levels
        try:
            signal = self.block(helper.body)
        finally:
            self.frame, self.levels = saved, saved_levels
        if signal == _RETURN:
            value, self.returned = self.returned, None
            self.level = self.return_level
            return value
        return None

    # -------------------------------------------------------------- capabilities

    def _await(self, node: ast.Await) -> object:
        call = cast(ast.Call, node.value)
        func = call.func
        if type(func) is ast.Attribute:
            capability = func.attr
            if capability == "call":
                return self._model_call(call)
            if capability == "observe":
                return self._rpc({"op": "observe"})
            if capability == "step":
                action = self.ev(call.args[0])
                if type(action) is not dict:
                    raise Fatal("sandbox_invalid_call")
                return self._rpc({"op": "step", "action": action})
            label = cast(ast.Constant, call.args[0]).value
            return self._rpc({"op": "harness", "name": label, "input": self.ev(call.args[1])})
        name = cast(ast.Name, func).id
        if name == "execute_preset":
            return self._preset(call)
        return self._invoke(self.helpers[name], call.args[1:])

    def _model_call(self, call: ast.Call) -> object:
        prompt = self.ev(call.args[0])
        tokens = self.ev(call.args[1]) if len(call.args) == 2 else None
        system: object = None
        images: object = None
        for keyword in call.keywords:
            value = self.ev(keyword.value)
            if keyword.arg == "max_output_tokens":
                tokens = value
            elif keyword.arg == "system":
                system = value
            else:
                images = value
        if (
            type(prompt) is not str
            or (tokens is not None and (type(tokens) is not int or tokens < 1))
            or (system is not None and type(system) is not str)
            or (type(system) is str and _utf8_size(system) > language.SYSTEM_LIMIT)
        ):
            raise Fatal("sandbox_invalid_call")
        message: dict[str, object] = {"op": "call", "prompt": prompt, "tokens": tokens}
        if system is not None:
            message["system"] = system
        if images is not None:
            names = _image_names(images)
            if names:
                message["images"] = names
        reply = self._rpc(message)
        if type(reply) is not str:
            raise SandboxError("sandbox_invalid_message")
        return reply

    def _binding(self, node: ast.expr, index: int) -> object:
        """One of execute_preset's bound arguments: a literal or a constant holding it."""
        if type(node) is ast.Name:
            value = copy.deepcopy(self.constants[node.id])
            return tuple(cast(list[object], value)) if index == 2 else value
        return _literal(node)

    def _preset(self, call: ast.Call) -> object:
        name, params, components, spec = [
            self._binding(item, index) for index, item in enumerate(call.args[:4])
        ]
        task = self.ev(call.args[5])
        config = spec.get("execution_config", {}) if isinstance(spec, dict) else {}
        if (
            name == "coder"
            or (isinstance(spec, dict) and spec.get("pipeline"))
            or (isinstance(config, dict) and config.get("kind") in {"game", "competition"})
        ):
            return self._rpc({"op": "preset", "task": task})
        _boundary(task, self.meter)
        value = asyncio.run(
            execute_preset(
                cast(str, name),
                cast(dict[str, JsonValue], params),
                cast(tuple[str, ...], components),
                cast(dict[str, JsonValue], spec),
                cast(RunContext, _RPC()),
                cast(JsonValue, task),
            )
        )
        self.level = 1
        return store(value, self.meter, 1)

    def _rpc(self, message: dict[str, object]) -> object:
        """One metered capability exchange; fuel bounds its wall time, not only the op cap.

        ``observe`` and a refused ``step`` are free of the op cap, so without this
        charge a loop over them would run until the host deadline. Each exchange
        costs ``RPC_FUEL`` for the round trip plus 1 per ``RPC_BYTES_PER_FUEL`` bytes
        of the encoded message (charged before it is sent) and of the raw reply
        (charged before it is decoded and walked).
        """
        data = _boundary(message, self.meter)
        self.meter.charge(language.RPC_FUEL + len(data) // language.RPC_BYTES_PER_FUEL)
        _write(data)
        raw = _read()
        self.meter.charge(len(raw) // language.RPC_BYTES_PER_FUEL)
        response = _decode(raw)
        if not isinstance(response, dict) or set(response) != {"value"}:
            raise SandboxError("sandbox_invalid_message")
        self.level = 1
        return response["value"]


def _boundary(value: object, meter: Meter) -> bytes:
    """Full encoding at an RPC boundary; failures there are fatal, never catchable.

    Only the digit work of ints beyond 2**63 is charged here, before encoding them;
    the caller charges the encoded bytes. Returns the encoded line.
    """
    try:
        check_json(value, meter=meter)
        return _encode(value)
    except DataError:
        raise Fatal("sandbox_invalid_call") from None
    except SandboxError as exc:
        if exc.code in {"sandbox_message_limit", "sandbox_value_limit"}:
            raise Fatal("sandbox_value_limit") from None
        raise Fatal("sandbox_invalid_call") from None


def _image_names(images: object) -> list[str]:
    """sha256 names of exact ``{"$image": <stub>}`` values taken from ``task``."""
    if type(images) is not list or len(cast(list[object], images)) > MAX_IMAGES_PER_REQUEST:
        raise Fatal("sandbox_invalid_call")
    names: list[str] = []
    for item in cast(list[object], images):
        if type(item) is not dict or set(cast(dict[str, object], item)) != {"$image"}:
            raise Fatal("sandbox_invalid_call")
        try:
            stub = ImageStub.model_validate(cast(dict[str, object], item)["$image"], strict=True)
        except ValidationError:
            raise Fatal("sandbox_invalid_call") from None
        names.append(stub.sha256)
    return names


def _run_v2(source: str, artifact: HarnessArtifact, task: JsonValue) -> None:
    """Interpret one language 2 run and send exactly one result or error frame."""
    program = _validated_program(source, artifact)
    assert artifact.forge is not None
    meter = Meter(artifact.forge.fuel)
    interpreter: _InterpreterV2 | None = _InterpreterV2(program, meter)
    frame: dict[str, JsonValue]
    try:
        assert interpreter is not None
        value = interpreter.execute(task)
        try:
            check_json(value)
            frame = {"op": "result", "value": cast(JsonValue, value), "fuel": meter.used}
            _encode(frame)
        except (DataError, SandboxError, RecursionError) as exc:
            code = "sandbox_type" if isinstance(exc, DataError) else "sandbox_value_limit"
            raise Fatal(code, interpreter.return_line) from None
    except SandboxFault as exc:
        frame = {"op": "error", "code": exc.code, "line": exc.line}
    except MemoryError:
        frame = _MEMORY_FRAME
    except RecursionError:  # pragma: no cover - statements map it with their line
        frame = {"op": "error", "code": "sandbox_value_limit", "line": None}
    interpreter = None  # Release every value before writing the frame.
    del interpreter
    _send(frame)


async def _in_thread(work: Callable[[], None]) -> None:
    """Run ``work`` on a thread with a large stack; re-raise what it raised."""
    failures: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            failures.append(exc)

    previous = threading.stack_size(_STACK_BYTES)
    try:
        thread = threading.Thread(target=target, name="sandbox-v2", daemon=True)
        thread.start()
    finally:
        threading.stack_size(previous)
    await asyncio.to_thread(thread.join)
    if failures:
        raise failures[0]


async def _main() -> None:
    initial = _receive()
    if (
        not isinstance(initial, dict)
        or set(initial) - {"language"} != {"source", "binding", "task"}
        or not isinstance(initial["source"], str)
        or not isinstance(initial["binding"], dict)
        or initial.get("language", 1) not in (1, 2)
        or type(initial.get("language", 1)) is not int
    ):
        raise SandboxError("sandbox_invalid_message")
    artifact = HarnessArtifact.model_validate({**initial["binding"], "directory": "."})
    if initial.get("language", 1) == 2:
        if artifact.forge is None:
            raise SandboxError("sandbox_invalid_message")
        source, task = initial["source"], initial["task"]
        await _in_thread(lambda: _run_v2(source, artifact, task))
        return
    if artifact.forge is not None:
        raise SandboxError("sandbox_invalid_message")
    run = _validated_tree(initial["source"], artifact)
    interpreter = _Interpreter(initial["task"])
    try:
        await interpreter.block(run.body)
    except _Return as result:
        _send({"op": "result", "value": result.value})
    else:
        _send({"op": "result", "value": None})


if __name__ == "__main__":
    try:
        limit_self(language.WORKER_MEMORY_POSIX)
        sys.setrecursionlimit(_RECURSION_LIMIT)
        asyncio.run(_main())
    except Exception:
        # No tracebacks, model text, local paths, or arbitrary stdout cross the boundary.
        _send({"op": "error"})
        sys.exit(1)
