"""A closed capability language, interpreted in a disposable subprocess.

This is NOT OS isolation for arbitrary Python. Authored Python is never imported,
compiled, evaluated or executed. Its AST must belong to the grammar below. Only
JSON values, bounded control flow, and guarded asynchronous capabilities exist.
Trusted preset code runs in the worker with a model RPC proxy; the real adapter,
wallet, and all accounting remain in the parent process.

Two language versions exist, selected by the artifact binding. Language 1 (an
artifact with ``forge is None``) is frozen: straight-line code, a static await
cap and per-intermediate bounds. Language 2 (``artifact.forge.language == 2``)
adds helpers, fuel-bounded loops, arithmetic, mutation with value semantics,
DataError-only ``try/except``, native builtins, and the per-turn ``observe`` /
``step``, licensed ``harness`` and ``call(system=, images=)`` capabilities. Its
table lives in ``sandbox_language.py``.

Images never cross the pipe. The host lifts image literals out of the task
before the initial message and attaches them to the run; the worker sees only
content-free stubs, and a ``call`` message names images by sha256 alone. The
host resolves those names against the run's own table and nothing else.
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import re
import signal
import sys
from collections.abc import Iterator
from tempfile import TemporaryDirectory
from typing import Literal, NamedTuple, TypeVar, cast

from pydantic import JsonValue

from architect.models.images import MAX_IMAGES_PER_REQUEST, ImageInput, ImageStub
from architect.presets import get_preset
from architect.presets.images import lift_images
from architect.runtime import Harness, RunContext
from architect.runtime import sandbox_language as language
from architect.runtime._proc_limits import Closeable, contain
from architect.spec import ForgeBinding, HarnessArtifact, Spec

SOURCE_LIMIT = 64 * 1024
MESSAGE_LIMIT = 128 * 1024
NODE_LIMIT = 2000
CALL_LIMIT = 64
DEPTH_LIMIT = 40
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CALL_KEYS = frozenset({"op", "prompt", "tokens"})
_IMAGE_CALL_KEYS = _CALL_KEYS | {"images"}
_SYSTEM_CALL_KEYS = _IMAGE_CALL_KEYS | {"system"}
_GAME_KINDS = frozenset({"game", "competition"})


class Refusal(NamedTuple):
    """One refused line of a language 2 check: fixed codes and names, never source text."""

    line: int | None
    code: str
    rule: str | None
    name: str | None


class SandboxError(ValueError):
    """Unsupported source or invalid bounded sandbox protocol.

    ``str(error)`` is exactly the fixed code; ``line`` (and ``col``, for a syntax
    error) is the authored source position when known. A static refusal also names
    its ``rule`` (a ``sandbox_language.RULES`` key) and, for some rules, ``name``: one
    of the language's own identifiers (a builtin, a method, a reserved name, a key of
    the binding) or a key of ``MODULES`` or ``ALTERNATIVES``. None of them ever carries
    other source text, model text or a path.

    A language 2 check keeps going after a refusal: the error raised is the first
    refusal by line, and ``refusals`` lists every one the check found, in line order.
    """

    def __init__(
        self,
        code: str,
        *,
        line: int | None = None,
        rule: str | None = None,
        name: str | None = None,
        col: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.line = line
        self.rule = rule
        self.name = name
        self.col = col
        self.found: tuple[Refusal, ...] = ()

    @property
    def refusals(self) -> tuple[Refusal, ...]:
        """Every refusal of the check, by line; this error alone when it found one."""
        return self.found or (Refusal(self.line, self.code, self.rule, self.name),)


class SourceStats(NamedTuple):
    language: Literal[1, 2]
    nodes: int
    depth: int
    helpers: int
    constants: int
    loops: int
    await_sites: int
    bytes: int


def _json(value: object, depth: int = 0) -> JsonValue:
    """Reject non-JSON objects before serialization and bound nesting."""
    if depth > DEPTH_LIMIT:
        raise SandboxError("sandbox_value_limit")
    if value is None or type(value) in (bool, int, str):
        return cast(JsonValue, value)
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json(item, depth + 1) for item in cast(list[object], value)]
    if type(value) is dict:
        result: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise SandboxError("sandbox_invalid_json")
            result[key] = _json(item, depth + 1)
        return result
    raise SandboxError("sandbox_invalid_json")


def _encode(value: object) -> bytes:
    try:
        text = json.dumps(_json(value), sort_keys=True, ensure_ascii=True, allow_nan=False)
        data = text.encode("utf-8") + b"\n"
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise SandboxError("sandbox_invalid_json") from exc
    if len(data) > MESSAGE_LIMIT:
        raise SandboxError("sandbox_message_limit")
    return data


def _decode(data: bytes) -> JsonValue:
    if not data or len(data) > MESSAGE_LIMIT or not data.endswith(b"\n"):
        raise SandboxError("sandbox_invalid_message")
    try:
        return _json(json.loads(data))
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise SandboxError("sandbox_invalid_message") from exc


def _utf8_size(text: str) -> int:
    """UTF-8 length, counting a lone surrogate from JSON input instead of failing."""
    return len(text.encode("utf-8", "surrogatepass"))


def _literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SandboxError("sandbox_literal_required") from exc


def _same(left: object, right: object) -> bool:
    # Unlike Python equality, this keeps booleans distinct from numeric values.
    return _encode(left) == _encode(right)


def _name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _annotation(node: ast.expr | None, name: str) -> bool:
    return node is None or _name(node, name)


class _Grammar:
    def __init__(self, artifact: HarnessArtifact) -> None:
        self.artifact = artifact
        self.locals: set[str] = {"task"}
        self.calls = 0

    def block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, ast.Return):
                if statement.value is not None:
                    self.expr(statement.value)
            elif isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise SandboxError("sandbox_assignment")
                name = statement.targets[0].id
                if name in {"task", "context", "execute_preset", "len", "str"} or name.startswith(
                    "_"
                ):
                    raise SandboxError("sandbox_assignment")
                self.expr(statement.value)
                self.locals.add(name)
            elif isinstance(statement, ast.If):
                self.expr(statement.test)
                before = self.locals.copy()
                self.block(statement.body)
                after = self.locals.copy()
                self.locals = before
                self.block(statement.orelse)
                self.locals &= after
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Await):
                self.expr(statement.value)
            else:
                raise SandboxError("sandbox_statement")

    def expr(self, node: ast.expr) -> None:
        if isinstance(node, ast.Constant):
            _encode(node.value)
        elif isinstance(node, ast.Name) and node.id in self.locals:
            return
        elif isinstance(node, ast.List):
            for item in node.elts:
                self.expr(item)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant) or type(key.value) is not str:
                    raise SandboxError("sandbox_dictionary")
                self.expr(value)
        elif isinstance(node, ast.Subscript):
            self.expr(node.value)
            self.expr(node.slice)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub)):
            self.expr(node.operand)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            self.expr(node.left)
            self.expr(node.right)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            for item in node.values:
                self.expr(item)
        elif isinstance(node, ast.Compare):
            if not all(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops):
                raise SandboxError("sandbox_comparison")
            self.expr(node.left)
            for item in node.comparators:
                self.expr(item)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in {"len", "str"} or len(node.args) != 1 or node.keywords:
                raise SandboxError("sandbox_call")
            self.expr(node.args[0])
        elif isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            self.capability(node.value)
        else:
            raise SandboxError("sandbox_expression")

    def capability(self, call: ast.Call) -> None:
        self.calls += 1
        if self.calls > CALL_LIMIT:
            raise SandboxError("sandbox_call_limit")
        if (
            isinstance(call.func, ast.Attribute)
            and _name(call.func.value, "context")
            and call.func.attr == "call"
        ):
            if len(call.args) not in (1, 2):
                raise SandboxError("sandbox_call")
            if (
                len(call.keywords) > 1
                or any(k.arg != "max_output_tokens" for k in call.keywords)
                or (len(call.args) == 2 and call.keywords)
            ):
                raise SandboxError("sandbox_call")
            for item in call.args:
                self.expr(item)
            for keyword in call.keywords:
                self.expr(keyword.value)
        elif _name(call.func, "execute_preset"):
            if call.keywords or len(call.args) != 6 or not _name(call.args[4], "context"):
                raise SandboxError("sandbox_preset_call")
            name, params, components, spec = [_literal(item) for item in call.args[:4]]
            artifact = self.artifact
            if (
                not _same(name, artifact.preset)
                or not _same(params, artifact.params)
                or not isinstance(components, tuple)
                or components != artifact.components
                or not _same(spec, artifact.spec.model_dump(mode="json"))
            ):
                raise SandboxError("sandbox_binding_mismatch")
            self.expr(call.args[5])
        else:
            raise SandboxError("sandbox_capability")


_IMPORTS = frozenset(
    {
        ("pydantic", "JsonValue"),
        ("architect.presets", "execute_preset"),
        ("architect.runtime", "RunContext"),
        ("__future__", "annotations"),
    }
)


def _parse_bounded(source: str, node_limit: int) -> tuple[ast.Module, int, int]:
    """Parse, then bound node count and depth; return (tree, nodes, depth)."""
    if len(source.encode("utf-8")) > SOURCE_LIMIT:
        raise SandboxError("sandbox_source_limit", rule="source_limit")
    tree = ast.parse(source)
    pending: list[tuple[ast.AST, int]] = [(tree, 0)]
    count = deepest = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        deepest = max(deepest, depth)
        if count > node_limit or depth > DEPTH_LIMIT:
            raise SandboxError(
                "sandbox_ast_limit", line=getattr(node, "lineno", None), rule="ast_limit"
            )
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree, count, deepest


def _syntax_rule(source: str, exc: SyntaxError) -> str:
    """The fixed rule for a parse failure. The parser's message only picks the rule; no
    part of it, or of the source, is ever shown."""
    message = exc.msg if isinstance(exc.msg, str) else ""
    if isinstance(exc, IndentationError):  # TabError included
        return "syntax_indent"
    if "null bytes" in message:
        return "syntax_null"
    if "line continuation" in message:
        if source.count("\n") <= 1 and "\\n" in source:
            return "syntax_newlines"
        return "syntax_backslash"
    if "f-string" in message:
        return "syntax_fstring"
    if "unterminated" in message or "EOL while" in message or "EOF while" in message:
        return "syntax_string"
    if "never closed" in message or "unmatched" in message or "does not match" in message:
        return "syntax_bracket"
    if "expected ':'" in message:
        return "syntax_colon"
    if "invalid character" in message or "non-printable" in message:
        return "syntax_character"
    return "syntax"


def _module_key(module: str | None) -> str | None:
    """The top-level package an import names: ``a`` of ``a.b.c``."""
    return module.split(".", 1)[0] if module else None


def _import_refusal(node: ast.Import | ast.ImportFrom, rule: str = "import") -> SandboxError:
    """A refused import. One of the 4 fixed imports in the wrong place or form keeps the
    rule that lists them; another module is named when ``MODULES`` knows what replaces it."""
    if isinstance(node, ast.ImportFrom):
        modules = [node.module] if node.level == 0 and node.module else []
        fixed = any((node.module, alias.name) in _IMPORTS for alias in node.names)
    else:
        modules = [alias.name for alias in node.names]
        fixed = False
    if not fixed:
        for module in modules:
            key = _module_key(module)
            if key in language.MODULES:
                return SandboxError(
                    "sandbox_import", line=node.lineno, rule="import_module", name=key
                )
    code = "sandbox_import" if rule == "import" else "sandbox_statement"
    return SandboxError(code, line=node.lineno, rule=rule)


def _header(tree: ast.Module, errors: list[SandboxError]) -> list[ast.stmt]:
    """Strip the optional docstring and the imports at the top; return the rest.

    Each of the 4 fixed imports is taken once; every other import there is refused into
    ``errors`` and dropped, so the check goes on with the module.
    """
    statements = list(tree.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements.pop(0)
    seen: set[tuple[str | None, str]] = set()
    while statements and isinstance(statements[0], (ast.Import, ast.ImportFrom)):
        imported = cast(ast.Import | ast.ImportFrom, statements.pop(0))
        if isinstance(imported, ast.ImportFrom) and not imported.level and len(imported.names) == 1:
            alias = imported.names[0]
            pair = (imported.module, alias.name)
            if alias.asname is None and pair in _IMPORTS and pair not in seen:
                seen.add(pair)
                continue
        errors.append(_import_refusal(imported))
    return statements


def _main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` with no else (either side of the ==): a script's
    entry point, which never runs here, so a check drops it whole, unread."""
    if not isinstance(node, ast.If) or node.orelse:
        return False
    test = node.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    ):
        return False
    left, right = test.left, test.comparators[0]

    def main(side: ast.expr) -> bool:
        return isinstance(side, ast.Constant) and side.value == "__main__"

    return (_name(left, "__name__") and main(right)) or (main(left) and _name(right, "__name__"))


def _dotted(node: ast.expr) -> bool:
    """A plain or dotted name: str, typing.Any."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _type_shaped(node: ast.expr) -> bool:
    """Whether an annotation names a type: names and dotted names, subscripts of them,
    ``a | b``, tuples and lists inside subscripts, and the literals a type can hold
    (a str forward reference, None, ``...``, an int or bool, a negative int)."""
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _dotted(node)
    if isinstance(node, ast.Constant):
        value = node.value
        return value is None or value is Ellipsis or type(value) in (str, int, bool)
    if isinstance(node, ast.Subscript):
        return _dotted(node.value) and _type_shaped(node.slice)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_type_shaped(item) for item in node.elts)
    if isinstance(node, ast.BinOp):
        return (
            isinstance(node.op, ast.BitOr)
            and _type_shaped(node.left)
            and _type_shaped(node.right)
        )
    if isinstance(node, ast.UnaryOp):
        return (
            isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) is int
        )
    return False


def _annotation_shape(node: ast.expr | None) -> None:
    """Annotations are accepted and ignored, never evaluated; each must name a type."""
    if node is not None and not _type_shaped(node):
        raise SandboxError("sandbox_expression", line=node.lineno, rule="annotation_form")


def _signature_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    for arg in node.args.args:
        _annotation_shape(arg.annotation)
    _annotation_shape(node.returns)


def _code_nodes(statements: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node a body can evaluate: annotations (never evaluated) are left out."""
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        yield node
        for field, value in ast.iter_fields(node):
            if field in ("annotation", "returns"):
                continue
            if isinstance(value, ast.AST):
                pending.append(value)
            elif isinstance(value, list):
                pending.extend(item for item in value if isinstance(item, ast.AST))


def _run_signature(run: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    """``async def run(context, task)``, annotated or not; anything else is refused with
    the one line to write."""
    args = run.args
    if (
        run.name != "run"
        or not isinstance(run, ast.AsyncFunctionDef)
        or run.decorator_list
        or run.type_params
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg
        or args.kwarg
        or args.defaults
        or args.kw_defaults
        or [arg.arg for arg in args.args] != ["context", "task"]
    ):
        raise SandboxError("sandbox_signature", line=run.lineno, rule="run_signature")
    _signature_annotations(run)


def _validated_v1(
    source: str, artifact: HarnessArtifact
) -> tuple[ast.AsyncFunctionDef, SourceStats]:
    """The frozen language 1 gate: the exact pre-Phase 9 checks, plus statistics."""
    try:
        if len(source.encode("utf-8")) > SOURCE_LIMIT:
            raise SandboxError("sandbox_source_limit")
        tree = ast.parse(source)
        pending: list[tuple[ast.AST, int]] = [(tree, 0)]
        count = deepest = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            deepest = max(deepest, depth)
            if count > NODE_LIMIT or depth > DEPTH_LIMIT:
                raise SandboxError("sandbox_ast_limit")
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        # Reject malformed artifact metadata, even for a direct guarded-call program.
        preset = get_preset(artifact.preset)
        preset.build(artifact.spec, artifact.params, artifact.components)
        statements = list(tree.body)
        if (
            statements
            and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)
        ):
            statements.pop(0)
        seen: set[tuple[str | None, str]] = set()
        while statements and isinstance(statements[0], ast.ImportFrom):
            imported = cast(ast.ImportFrom, statements.pop(0))
            if imported.level or len(imported.names) != 1:
                raise SandboxError("sandbox_import")
            alias = imported.names[0]
            pair = (imported.module, alias.name)
            if alias.asname is not None or pair not in _IMPORTS or pair in seen:
                raise SandboxError("sandbox_import")
            seen.add(pair)
        if len(statements) != 1 or not isinstance(statements[0], ast.AsyncFunctionDef):
            raise SandboxError("sandbox_entrypoint")
        run = statements[0]
        args = run.args
        if (
            run.name != "run"
            or run.decorator_list
            or run.type_params
            or args.posonlyargs
            or args.kwonlyargs
            or args.vararg
            or args.kwarg
            or args.defaults
            or args.kw_defaults
            or [arg.arg for arg in args.args] != ["context", "task"]
            or not _annotation(args.args[0].annotation, "RunContext")
            or not _annotation(args.args[1].annotation, "JsonValue")
            or not _annotation(run.returns, "JsonValue")
        ):
            raise SandboxError("sandbox_signature")
        grammar = _Grammar(artifact)
        grammar.block(run.body)
        stats = SourceStats(
            language=1,
            nodes=count,
            depth=deepest,
            helpers=0,
            constants=0,
            loops=0,
            await_sites=grammar.calls,
            bytes=len(source.encode("utf-8")),
        )
        return run, stats
    except SandboxError:
        raise
    except (ValueError, TypeError, SyntaxError, RecursionError, UnicodeError, MemoryError) as exc:
        raise SandboxError("sandbox_invalid_source") from exc


# ------------------------------------------------------------------ language 2

_V2_ONLY_STATEMENTS = (
    ast.For, ast.While, ast.Break, ast.Continue, ast.Pass, ast.Try, ast.AugAssign, ast.AnnAssign
)
_V2_ONLY_EXPRESSIONS = (ast.IfExp, ast.JoinedStr, ast.Slice, ast.Tuple)
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot)
_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
_AUGMENTED = (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod)
_CONTEXT_CAPABILITIES = frozenset({"call", "observe", "step", "harness"})
_CALL_KEYWORDS = frozenset({"max_output_tokens", "system", "images"})
_RESERVED = language.RESERVED
_LOCAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CONSTANT_NAME = re.compile(language.CONSTANT_NAME)
_HELPER_NAME = re.compile(language.HELPER_NAME)
# Statements with a rule of their own; anything else is the general "statement" rule.
_STATEMENT_RULES: dict[type, str] = {
    ast.Raise: "raise",
    ast.Assert: "assert",
    ast.With: "with",
    ast.Delete: "del",
    ast.Global: "global",
    ast.Nonlocal: "global",
    ast.ClassDef: "class",
    ast.AsyncFor: "async_block",
    ast.AsyncWith: "async_block",
    ast.Match: "match",
}
_EXPRESSION_RULES: dict[type, str] = {
    ast.Lambda: "lambda",
    ast.GeneratorExp: "generator",
    ast.Set: "set_literal",
    ast.NamedExpr: "walrus",
    ast.Attribute: "attribute",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
    ast.Starred: "starred",
}
# Refused statements whose body is still checked: code a fix keeps, once dedented.
_BODY_KEPT = (ast.With, ast.AsyncWith, ast.AsyncFor)
_Comprehensions = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp


def _method_call(node: ast.AST) -> bool:
    """``value.method(...)`` spelling a builtin, on anything but ``context``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in language.METHODS
        and not _name(node.func.value, "context")
    )


def _v2_construct(tree: ast.Module) -> ast.AST | None:
    """The first language-2-only construct in ``tree`` (by line), or None."""
    found: list[ast.AST] = []
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.Assign, ast.AnnAssign)) or (
            isinstance(statement, ast.AsyncFunctionDef) and statement.name != "run"
        ):
            found.append(statement)
    for node in ast.walk(tree):
        if isinstance(node, (*_V2_ONLY_STATEMENTS, *_V2_ONLY_EXPRESSIONS)):
            found.append(node)
        elif isinstance(node, ast.BinOp) and not isinstance(node.op, ast.Add):
            found.append(node)
        elif isinstance(node, ast.Compare) and any(isinstance(op, _ORDERING) for op in node.ops):
            found.append(node)
        elif isinstance(node, ast.Assign) and not all(
            isinstance(target, ast.Name) for target in node.targets
        ):
            found.append(node)
        elif isinstance(node, ast.Dict) and not all(
            isinstance(key, ast.Constant) for key in node.keys
        ):
            found.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and (
                (node.func.id in language.BUILTINS and node.func.id not in {"len", "str"})
                or node.func.id == "isinstance"
            )
        ) or (
            isinstance(node, ast.Attribute)
            and _name(node.value, "context")
            and node.attr in {"observe", "step", "harness"}
        ):
            found.append(node)
        elif _method_call(node):
            found.append(node)
    return min(
        found,
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
        default=None,
    )


class _ProgramV2(NamedTuple):
    run: ast.AsyncFunctionDef
    helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    constants: dict[str, JsonValue]
    stats: SourceStats
    # helper name -> {parameter: default value}, for the parameters that have one.
    defaults: dict[str, dict[str, JsonValue]]


def _constant_value(node: ast.expr) -> JsonValue:
    """A literal module constant: JSON constants and List/Dict/Tuple of literals.

    The source, node and depth limits already bound its size and nesting.
    """
    if isinstance(node, ast.Constant):
        literal = node.value
        if literal is None or type(literal) in (bool, str):
            return cast(JsonValue, literal)
        if type(literal) is int and abs(literal) < language.INT_BOUND:
            return literal
        if type(literal) is float and math.isfinite(literal):
            return literal
    elif (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in (int, float)
    ):
        return cast(JsonValue, -_constant_value(node.operand))  # type: ignore[operator]
    elif isinstance(node, (ast.List, ast.Tuple)):
        return [_constant_value(item) for item in node.elts]
    elif isinstance(node, ast.Dict):
        result: dict[str, JsonValue] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant) or type(key.value) is not str:
                raise SandboxError("sandbox_constant", line=node.lineno, rule="constant_value")
            result[key.value] = _constant_value(value)
        return result
    raise SandboxError("sandbox_constant", line=node.lineno, rule="constant_value")


def _default_value(node: ast.expr, constants: dict[str, JsonValue]) -> JsonValue:
    """A helper default: a module CONSTANT's value, or a literal as constants allow."""
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    try:
        return _constant_value(node)
    except SandboxError:
        raise SandboxError(
            "sandbox_helper", line=node.lineno, rule="helper_default_value"
        ) from None


def _target_names(target: ast.expr) -> list[str]:
    """Every name a binding target holds, nested ones included, in order.

    A valid target is one name or a flat tuple of names; a refused shape still binds its
    names here, so the code that reads them is not refused a second time.
    """
    names: list[str] = []
    pending: list[ast.expr] = [target]
    while pending:
        node = pending.pop(0)
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            pending[:0] = node.elts
        elif isinstance(node, ast.Starred):
            pending.insert(0, node.value)
    return names


def _pair_target(target: ast.expr) -> bool:
    """A for loop or a comprehension binds one name, or a tuple of two or three names (three
    for ``zip`` of three lists)."""
    if isinstance(target, ast.Tuple):
        return 2 <= len(target.elts) <= language.LOOP_NAMES_LIMIT and all(
            isinstance(item, ast.Name) for item in target.elts
        )
    return isinstance(target, ast.Name)


def _exception_types(node: ast.expr | None) -> bool:
    """An ``except`` clause's type: none, one of Python's exception names, or a tuple of
    them. It is checked here and never evaluated."""
    if node is None:
        return True
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return bool(names) and all(
        isinstance(item, ast.Name) and item.id in language.EXCEPTION_NAMES for item in names
    )


def _assigned(statements: list[ast.stmt], names: dict[str, int]) -> None:
    """Collect every name a function body binds (targets, loop variables) and its line.

    Comprehension names are not collected: they live only inside their comprehension.
    """
    for statement in statements:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, (ast.AugAssign, ast.For, ast.AsyncFor)):
            targets = [statement.target]
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            targets = [item.optional_vars for item in statement.items if item.optional_vars]
        for target in targets:
            for name in _target_names(target):
                names.setdefault(name, statement.lineno)
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(statement, field, None)
            if isinstance(nested, list):
                _assigned(cast(list[ast.stmt], nested), names)
        if isinstance(statement, ast.Try):
            for handler in statement.handlers:
                if handler.name is not None:
                    names.setdefault(handler.name, handler.lineno)
                _assigned(handler.body, names)


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Every parameter a def names, of every kind (only plain ones validate)."""
    args = node.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    every += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return [arg.arg for arg in every]


def _first_difference(source: object, bound: dict[str, JsonValue]) -> str | None:
    """The first key of the binding (sorted) that the source's dict lacks or changes.

    Only a key of the binding itself is ever named, never one the source added.
    """
    if not isinstance(source, dict):
        return None
    for key in sorted(bound):
        if key not in source or not _same(source[key], bound[key]):
            return key
    return None


def _generator_slot(call: ast.Call, name: str, args: list[ast.expr], index: int) -> bool:
    """Whether ``args[index]`` of builtin ``name`` may be a generator expression.

    Only the list argument of a ``GENERATOR_CALLEES`` builtin, and only when it is the
    last thing the call evaluates, so its loop runs where Python's would: the one argument
    of most, the second of ``join(sep, values)`` (``sep.join(values)`` too), and the first
    of ``sorted`` when ``reverse=`` is a literal.
    """
    if name not in language.GENERATOR_CALLEES or index != len(args) - 1:
        return False
    if name == "join":
        return len(args) == 2 and not call.keywords
    if len(args) != 1:
        return False
    if name == "sorted":
        return all(
            keyword.arg == "reverse" and isinstance(keyword.value, ast.Constant)
            for keyword in call.keywords
        )
    return not call.keywords


class _GrammarV2:
    """Static gate of language 2. Every refusal carries the authored line and a rule.

    A refusal is recorded and the check goes on, so one check finds every refused line:
    a statement's own refusal leaves the statements after it (and its nested blocks)
    still checked, and a refused part of an expression leaves its other parts checked.
    """

    def __init__(
        self,
        artifact: HarnessArtifact,
        helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
        constants: dict[str, JsonValue],
        defaults: dict[str, dict[str, JsonValue]],
        errors: list[SandboxError],
        *,
        run: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
        lenient: frozenset[str] = frozenset(),
        modules: dict[str, str] | None = None,
        imported: dict[str, str] | None = None,
        classes: frozenset[str] = frozenset(),
        known: frozenset[str] = frozenset(),
    ) -> None:
        self.artifact = artifact
        self.helpers = helpers
        self.constants = constants
        self.defaults = defaults
        self.errors = errors
        self.run = run
        # Helpers whose signature was refused: calls to them are not counted against it.
        self.lenient = lenient
        # Names a refused import binds (to its module's key), class names, and refused
        # module-level names: a read of one is refused for what it is, not as unknown.
        self.modules = modules or {}
        self.imported = imported or {}
        self.classes = classes
        self.known = known
        self.game = artifact.spec.execution_config.get("kind") in _GAME_KINDS
        self.bound = set(artifact.forge.subharnesses) if artifact.forge is not None else set()
        self.edges: dict[str, list[tuple[str, int]]] = {}
        # Per-function state, reset by function().
        self.current = ""
        self.is_async = True
        self.names: set[str] = set()
        self.loop_depth = 0

    # -------------------------------------------------------------- recording

    def record(
        self, error: SandboxError, node: ast.AST | None = None, at: int | None = None
    ) -> None:
        """Keep a refusal. ``at`` places it before the refusals its own parts recorded
        meanwhile, so within a line the outer refusal comes first."""
        if error.line is None and node is not None:
            error.line = getattr(node, "lineno", None)
        self.errors.insert(len(self.errors) if at is None else at, error)

    def sub(self, node: ast.expr) -> None:
        """Check one part of an expression; its refusal is recorded, not raised."""
        mark = len(self.errors)
        try:
            self.expr(node)
        except SandboxError as exc:
            self.record(exc, node, mark)

    # -------------------------------------------------------------- functions

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.current = node.name
        # A run refused for being a plain def is still checked as the async run it must be.
        self.is_async = isinstance(node, ast.AsyncFunctionDef) or node is self.run
        self.loop_depth = 0
        self.edges.setdefault(node.name, [])
        params = _parameter_names(node)
        bound: dict[str, int] = {}
        _assigned(node.body, bound)
        for name, line in sorted(bound.items(), key=lambda item: item[1]):
            if name not in params or name in {"context", "task"}:
                try:
                    self.assignable(name, line)
                except SandboxError as exc:
                    self.record(exc)
        self.names = set(params) | set(bound)
        body = list(node.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and type(body[0].value.value) is str
            and len(body) > 1
        ):
            body.pop(0)  # A docstring has no effect.
        self.block(body)

    def assignable(self, name: str, line: int) -> None:
        """A variable may reuse a builtin's name (calls by that name still reach the
        builtin); it may not be task, context, another reserved name, a constant or a
        helper."""
        rule, named = None, None
        if not _LOCAL_NAME.fullmatch(name) or name.startswith("__"):
            rule = "assign_name"
        elif name in {"task", "context"}:
            rule = f"assign_{name}"
        elif name in _RESERVED:
            rule, named = "assign_reserved", name
        elif name in self.constants:
            rule = "assign_constant"
        elif name in self.helpers:
            rule = "assign_helper"
        if rule is not None:
            raise SandboxError("sandbox_assignment", line=line, rule=rule, name=named)

    def required(self, helper: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
        """(fewest, most) arguments a call passes, ``context`` included."""
        total = len(helper.args.args)
        return total - len(self.defaults.get(helper.name, {})), total

    # -------------------------------------------------------------- statements

    def block(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            mark = len(self.errors)
            try:
                self.statement(statement)
            except SandboxError as exc:
                self.record(exc, statement, mark)

    def loop(self, line: int, body: list[ast.stmt]) -> None:
        """A loop's body, one level deeper; the first level past the limit is refused once."""
        self.loop_depth += 1
        try:
            if self.loop_depth == language.LOOP_DEPTH_LIMIT + 1:
                self.record(SandboxError("sandbox_loop_depth", line=line, rule="loop_depth"))
            self.block(body)
        finally:
            self.loop_depth -= 1

    def statement(self, node: ast.stmt) -> None:
        line = node.lineno
        if isinstance(node, ast.Return):
            if node.value is not None:
                self.sub(node.value)
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise SandboxError("sandbox_assignment", line=line, rule="assign_chained")
            self.sub(node.value)
            self.target(node.targets[0], unpack=True)
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                raise SandboxError("sandbox_statement", line=line, rule="annotation_no_value")
            try:
                _annotation_shape(node.annotation)
            except SandboxError as exc:
                self.record(exc)
            self.sub(node.value)
            self.target(node.target, unpack=False)
        elif isinstance(node, ast.AugAssign):
            self.sub(node.value)
            if not isinstance(node.op, _AUGMENTED):
                raise SandboxError("sandbox_assignment", line=line, rule="augmented_op")
            self.target(node.target, unpack=False)
        elif isinstance(node, ast.If):
            self.sub(node.test)
            self.block(node.body)
            self.block(node.orelse)
        elif isinstance(node, (ast.For, ast.While)):
            if node.orelse:
                self.record(SandboxError("sandbox_statement", line=line, rule="loop_else"))
            if isinstance(node, ast.For):
                if not _pair_target(node.target):
                    self.record(SandboxError("sandbox_assignment", line=line, rule="for_target"))
                self.sub(node.iter)
            else:
                self.sub(node.test)
            self.loop(line, node.body)
            self.block(node.orelse)
        elif isinstance(node, (ast.Break, ast.Continue)):
            if self.loop_depth == 0:
                raise SandboxError("sandbox_statement", line=line, rule="break_outside_loop")
        elif isinstance(node, ast.Pass):
            return
        elif isinstance(node, ast.Try):
            if len(node.handlers) != 1 or node.orelse or node.finalbody:
                self.record(SandboxError("sandbox_statement", line=line, rule="try_shape"))
            for handler in node.handlers:
                # Its as name was checked with the function's other bindings.
                if not _exception_types(handler.type):
                    self.record(
                        SandboxError("sandbox_statement", line=handler.lineno, rule="except_shape")
                    )
            self.block(node.body)
            for handler in node.handlers:
                self.block(handler.body)
            self.block(node.orelse)
            self.block(node.finalbody)
        elif isinstance(node, ast.Expr):
            value = node.value
            if isinstance(value, ast.Await):
                self.sub(value)
            elif isinstance(value, ast.Call):
                before = len(self.errors)
                self.sub(value)  # an invalid call is refused with its own rule
                if len(self.errors) == before and not self.acts(value):
                    raise SandboxError("sandbox_statement", line=line, rule="expression_statement")
            else:
                raise SandboxError("sandbox_statement", line=line, rule="expression_statement")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise SandboxError("sandbox_helper", line=line, rule="nested_def")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            raise _import_refusal(node, "import_inside")
        else:
            rule = _STATEMENT_RULES.get(type(node), "statement")
            self.record(SandboxError("sandbox_statement", line=line, rule=rule))
            if isinstance(node, _BODY_KEPT):
                if isinstance(node, ast.AsyncFor):
                    self.sub(node.iter)
                    self.loop(line, node.body)
                else:
                    self.block(node.body)

    def acts(self, call: ast.Call) -> bool:
        """A call worth a statement of its own: a mutating builtin (or its method
        spelling) or a sync helper."""
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr in language.MUTATING
        return isinstance(func, ast.Name) and (
            func.id in language.MUTATING
            or isinstance(self.helpers.get(func.id), ast.FunctionDef)
        )

    def target(self, node: ast.expr, *, unpack: bool) -> None:
        line = node.lineno
        if isinstance(node, ast.Name):
            # Every bound name was collected and checked by function(); task and
            # context are reserved, so neither can be rebound.
            return
        if unpack and isinstance(node, (ast.Tuple, ast.List)):
            if not 2 <= len(node.elts) <= language.PARAMETER_LIMIT or not all(
                isinstance(item, ast.Name) for item in node.elts
            ):
                raise SandboxError("sandbox_assignment", line=line, rule="assign_unpack")
            for item in node.elts:
                self.target(item, unpack=False)
            return
        if isinstance(node, ast.Subscript):
            root: ast.expr = node
            while isinstance(root, ast.Subscript):
                if isinstance(root.slice, ast.Slice):
                    raise SandboxError("sandbox_assignment", line=line, rule="assign_slice")
                self.sub(root.slice)
                root = root.value
            if not isinstance(root, ast.Name) or not self.mutable(root.id):
                raise SandboxError("sandbox_assignment", line=line, rule="assign_root")
            return
        raise SandboxError("sandbox_assignment", line=line, rule="assign_target")

    def mutable(self, name: str) -> bool:
        """A local name that is not ``task``, ``context`` or a constant."""
        return name in self.names and name not in {"task", "context"}

    # -------------------------------------------------------------- expressions

    def foreign(self, name: str, line: int | None) -> SandboxError | None:
        """The refusal for reading a name no local binds that a refused import or class
        bound, or None. A module is named when ``MODULES`` knows what replaces it."""
        if name in self.names:
            return None
        module = self.modules.get(name) or self.imported.get(name)
        if module is not None:
            known = module if module in language.MODULES else None
            return SandboxError("sandbox_expression", line=line, rule="module_use", name=known)
        if name in self.classes:
            return SandboxError("sandbox_expression", line=line, rule="class_use")
        return None

    def attribute(self, node: ast.Attribute) -> SandboxError:
        """``value.name`` outside a method spelling: refused, by what its root is."""
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            found = self.foreign(root.id, node.lineno)
            if found is not None:
                return found
        return SandboxError("sandbox_expression", line=node.lineno, rule="attribute")

    def expr(self, node: ast.expr) -> None:
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Constant):
            literal = node.value
            if not (
                literal is None
                or type(literal) in (bool, str)
                or (type(literal) is int and abs(literal) < language.INT_BOUND)
                or (type(literal) is float and math.isfinite(literal))
            ):
                raise SandboxError("sandbox_expression", line=line, rule="literal")
        elif isinstance(node, ast.Name):
            if node.id == "context":
                raise SandboxError("sandbox_expression", line=line, rule="context_value")
            if (
                node.id not in self.names
                and node.id not in self.constants
                and node.id not in self.known
            ):
                raise self.foreign(node.id, line) or SandboxError(
                    "sandbox_expression", line=line, rule="unknown_name"
                )
        elif isinstance(node, (ast.List, ast.Tuple)):
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    starred = SandboxError("sandbox_expression", line=item.lineno, rule="starred")
                    self.record(starred)
                else:
                    self.sub(item)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    self.record(SandboxError("sandbox_dictionary", line=line, rule="dict_unpack"))
                else:
                    self.sub(key)
                self.sub(value)
        elif isinstance(node, ast.Subscript):
            self.sub(node.value)
            if isinstance(node.slice, ast.Slice):
                for bound in (node.slice.lower, node.slice.upper):
                    if bound is not None:
                        self.sub(bound)
                step = node.slice.step
                if step is not None and not (
                    (isinstance(step, ast.Constant) and step.value == 1 and type(step.value) is int)
                    or (
                        isinstance(step, ast.UnaryOp)
                        and isinstance(step.op, ast.USub)
                        and isinstance(step.operand, ast.Constant)
                        and type(step.operand.value) is int
                        and step.operand.value == 1
                    )
                ):
                    raise SandboxError("sandbox_expression", line=line, rule="slice_step")
            else:
                self.sub(node.slice)
        elif isinstance(node, ast.UnaryOp):
            self.sub(node.operand)
            if not isinstance(node.op, (ast.Not, ast.USub)):
                raise SandboxError("sandbox_expression", line=line, rule="operator")
        elif isinstance(node, ast.BinOp):
            self.sub(node.left)
            self.sub(node.right)
            if not isinstance(node.op, _ARITHMETIC):
                raise SandboxError("sandbox_expression", line=line, rule="operator")
        elif isinstance(node, ast.BoolOp):
            for item in node.values:
                self.sub(item)
        elif isinstance(node, ast.Compare):
            self.sub(node.left)
            for item in node.comparators:
                self.sub(item)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.Is, ast.IsNot)):
                    if not (isinstance(comparator, ast.Constant) and comparator.value is None):
                        raise SandboxError(
                            "sandbox_comparison", line=line, rule="is_none_only"
                        )
                elif not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn, *_ORDERING)):
                    raise SandboxError(  # pragma: no cover - no other cmpop exists
                        "sandbox_comparison", line=line, rule="is_none_only"
                    )
        elif isinstance(node, ast.IfExp):
            self.sub(node.test)
            self.sub(node.body)
            self.sub(node.orelse)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    if part.conversion != -1 or part.format_spec is not None:
                        self.record(
                            SandboxError("sandbox_expression", line=line, rule="fstring_format")
                        )
                    else:
                        self.sub(part.value)
                elif not (isinstance(part, ast.Constant) and type(part.value) is str):
                    raise SandboxError(  # pragma: no cover - parser only emits these
                        "sandbox_expression", line=line, rule="expression"
                    )
        elif isinstance(node, ast.Call):
            self.call(node)
        elif isinstance(node, ast.Await):
            self.awaited(node)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            self.comprehension(node)
        elif isinstance(node, ast.Attribute):
            raise self.attribute(node)
        else:
            rule = _EXPRESSION_RULES.get(type(node), "expression")
            raise SandboxError("sandbox_expression", line=line, rule=rule)

    def comprehension(self, node: _Comprehensions) -> None:
        """A comprehension, checked as the loop nest it runs as, in a scope of its own.

        The first iterable is read in the enclosing scope, as Python reads it. Every
        loop name is the comprehension's own, so a part that reads one before its for
        clause binds it is refused as unknown, as Python would fail it; each for clause
        is a loop level, and none of its names outlives the comprehension.
        """
        line = node.lineno
        generators = node.generators
        bound = [_target_names(generator.target) for generator in generators]
        own = {name for names in bound for name in names}
        self.sub(generators[0].iter)
        names, depth = self.names, self.loop_depth
        self.names = names - own
        try:
            for index, generator in enumerate(generators):
                if generator.is_async:
                    self.record(SandboxError("sandbox_statement", line=line, rule="async_block"))
                if index:
                    self.sub(generator.iter)
                if not _pair_target(generator.target):
                    self.record(SandboxError("sandbox_assignment", line=line, rule="for_target"))
                for name in bound[index]:
                    try:
                        self.assignable(name, line)
                    except SandboxError as exc:
                        self.record(exc)
                    self.names.add(name)
                self.loop_depth += 1
                if self.loop_depth == language.LOOP_DEPTH_LIMIT + 1:
                    self.record(SandboxError("sandbox_loop_depth", line=line, rule="loop_depth"))
                for condition in generator.ifs:
                    self.sub(condition)
            if isinstance(node, ast.DictComp):
                self.sub(node.key)
                self.sub(node.value)
            else:
                self.sub(node.elt)
        finally:
            self.names, self.loop_depth = names, depth

    def positional(self, call: ast.Call) -> None:
        if any(isinstance(item, ast.Starred) for item in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        ):
            raise SandboxError("sandbox_call", line=call.lineno, rule="call_starred")

    def arguments(self, call: ast.Call) -> None:
        """Check the arguments of a call that is itself refused."""
        for item in call.args:
            if not isinstance(item, ast.Starred):
                self.sub(item)
        for keyword in call.keywords:
            self.sub(keyword.value)

    def call(self, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Attribute):
            self.method(call, func)
            return
        if not isinstance(func, ast.Name):
            # Computed callees do not exist: only builtins and helpers, by name.
            self.arguments(call)
            raise SandboxError("sandbox_expression", line=call.lineno, rule="callee")
        name = func.id
        self.positional(call)
        if name == "isinstance":
            self.isinstance_call(call)
            return
        if name in language.BUILTINS:
            self.builtin(call, name, call.args)
            return
        helper = self.helpers.get(name)
        if helper is None:
            self.arguments(call)
            if name == "execute_preset":
                raise SandboxError(
                    "sandbox_capability", line=call.lineno, rule="await_capability"
                )
            found = self.foreign(name, call.lineno)
            if found is not None:
                raise found
            known = name if name in language.ALTERNATIVES else None
            raise SandboxError(
                "sandbox_call", line=call.lineno, rule="missing_function", name=known
            )
        if isinstance(helper, ast.AsyncFunctionDef):
            passed = call.args[1:] if call.args and _name(call.args[0], "context") else call.args
            for item in passed:
                self.sub(item)
            raise SandboxError("sandbox_helper", line=call.lineno, rule="async_helper_await")
        self.edges[self.current].append((name, call.lineno))
        for item in call.args:
            self.sub(item)
        self.helper_arguments(call, helper)

    def builtin(self, call: ast.Call, name: str, args: list[ast.expr]) -> None:
        """A builtin call; ``args`` holds a method's receiver first."""
        for index, item in enumerate(args):
            if isinstance(item, ast.GeneratorExp) and _generator_slot(call, name, args, index):
                self.comprehension(item)  # records its own refusals
            else:
                self.sub(item)
        for keyword in call.keywords:
            self.sub(keyword.value)
        low, high = language.ARITY[name]
        keywords = [keyword.arg for keyword in call.keywords]
        if keywords and (keywords != ["reverse"] or name != "sorted"):
            raise SandboxError(
                "sandbox_call", line=call.lineno, rule="builtin_keyword", name=name
            )
        if not low <= len(args) <= high:
            rule = "method_arity" if isinstance(call.func, ast.Attribute) else "builtin_arity"
            raise SandboxError("sandbox_call", line=call.lineno, rule=rule, name=name)
        if name in language.MUTATING:
            first = args[0]
            if not isinstance(first, ast.Name) or not self.mutable(first.id):
                raise SandboxError(
                    "sandbox_assignment", line=call.lineno, rule="mutate_root", name=name
                )

    def method(self, call: ast.Call, func: ast.Attribute) -> None:
        """``value.name(args)``: exactly the builtin ``name(value, args)``."""
        if _name(func.value, "context"):
            self.arguments(call)
            rule = "await_capability" if func.attr in _CONTEXT_CAPABILITIES else (
                "capability_unknown"
            )
            raise SandboxError("sandbox_capability", line=call.lineno, rule=rule)
        if isinstance(func.value, ast.Name):
            found = self.foreign(func.value.id, call.lineno)
            if found is not None:
                self.arguments(call)
                raise found
        if func.attr not in language.METHODS:
            self.sub(func.value)
            self.arguments(call)
            known = func.attr if func.attr in language.ALTERNATIVES else None
            raise SandboxError(
                "sandbox_expression", line=call.lineno, rule="method_unknown", name=known
            )
        self.positional(call)
        self.builtin(call, func.attr, [func.value, *call.args])

    def isinstance_call(self, call: ast.Call) -> None:
        """``isinstance(x, T)``: T names types; it is checked here and never evaluated."""
        names: list[ast.expr] = []
        if call.args:
            self.sub(call.args[0])
        if len(call.args) == 2 and not call.keywords:
            kinds = call.args[1]
            names = list(kinds.elts) if isinstance(kinds, (ast.Tuple, ast.List)) else [kinds]
        if not names or not all(
            isinstance(item, ast.Name) and item.id in language.TYPE_NAMES for item in names
        ):
            raise SandboxError("sandbox_call", line=call.lineno, rule="isinstance_form")

    def helper_arguments(
        self, call: ast.Call, helper: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        if call.keywords:
            raise SandboxError("sandbox_call", line=call.lineno, rule="helper_keyword")
        if helper.name in self.lenient:
            return  # its own signature is refused; the calls wait for the fix
        fewest, most = self.required(helper)
        if not fewest <= len(call.args) <= most:
            raise SandboxError("sandbox_call", line=call.lineno, rule="helper_arity")

    def awaited(self, node: ast.Await) -> None:
        if not self.is_async:
            raise SandboxError("sandbox_helper", line=node.lineno, rule="helper_sync_await")
        call = node.value
        if not isinstance(call, ast.Call):
            self.sub(call)
            raise SandboxError("sandbox_capability", line=node.lineno, rule="await_target")
        self.positional(call)
        func = call.func
        if isinstance(func, ast.Attribute):
            if not _name(func.value, "context"):
                self.arguments(call)
                found = (
                    self.foreign(func.value.id, call.lineno)
                    if isinstance(func.value, ast.Name)
                    else None
                )
                raise found or SandboxError(
                    "sandbox_capability", line=call.lineno, rule="await_target"
                )
            if func.attr not in _CONTEXT_CAPABILITIES:
                self.arguments(call)
                raise SandboxError(
                    "sandbox_capability", line=call.lineno, rule="capability_unknown"
                )
            self.context_capability(func.attr, call)
        elif _name(func, "execute_preset"):
            self.preset(call)
        elif isinstance(func, ast.Name) and isinstance(
            self.helpers.get(func.id), ast.AsyncFunctionDef
        ):
            helper = cast(ast.AsyncFunctionDef, self.helpers[func.id])
            self.edges[self.current].append((func.id, call.lineno))
            for item in call.args[1:]:
                self.sub(item)
            self.helper_arguments(call, helper)
            if not call.args or not _name(call.args[0], "context"):
                raise SandboxError(
                    "sandbox_helper", line=call.lineno, rule="async_helper_context_arg"
                )
        else:
            self.arguments(call)
            raise SandboxError("sandbox_capability", line=call.lineno, rule="await_target")

    def context_capability(self, name: str, call: ast.Call) -> None:
        args, keywords = call.args, call.keywords
        if name == "harness":
            label = args[0] if args else None
            args = args[1:]
        for item in args:
            self.sub(item)
        for keyword in keywords:
            self.sub(keyword.value)
        if name == "call":
            names = [keyword.arg for keyword in keywords]
            if (
                len(call.args) not in (1, 2)
                or len(set(names)) != len(names)
                or not set(cast(list[str], names)) <= _CALL_KEYWORDS
                or (len(call.args) == 2 and "max_output_tokens" in names)
            ):
                raise SandboxError("sandbox_call", line=call.lineno, rule="call_args")
        elif name in {"observe", "step"}:
            if not self.game:
                raise SandboxError("sandbox_capability", line=call.lineno, rule="game_only")
            if keywords or len(call.args) != (0 if name == "observe" else 1):
                raise SandboxError("sandbox_call", line=call.lineno, rule="game_args")
        else:
            if keywords or len(call.args) != 2:
                raise SandboxError("sandbox_call", line=call.lineno, rule="harness_args")
            if not (
                isinstance(label, ast.Constant)
                and type(label.value) is str
                and label.value in self.bound
            ):
                raise SandboxError(
                    "sandbox_binding_mismatch", line=call.lineno, rule="harness_unbound"
                )

    def binding_value(self, node: ast.expr, index: int) -> object:
        """One of execute_preset's first four arguments: a literal, or a module CONSTANT
        holding one (a list constant stands for the components tuple)."""
        if isinstance(node, ast.Name) and node.id in self.constants:
            value: object = self.constants[node.id]
            return tuple(value) if index == 2 and isinstance(value, list) else value
        try:
            return _literal(node)
        except SandboxError as exc:
            exc.rule = "preset_literal"
            raise

    def preset(self, call: ast.Call) -> None:
        if call.keywords or len(call.args) != 6 or not _name(call.args[4], "context"):
            self.arguments(call)
            raise SandboxError("sandbox_preset_call", line=call.lineno, rule="preset_shape")
        self.sub(call.args[5])
        try:
            name, params, components, spec = [
                self.binding_value(item, index) for index, item in enumerate(call.args[:4])
            ]
        except SandboxError as exc:
            exc.line = call.lineno
            raise
        artifact = self.artifact
        bound_spec = artifact.spec.model_dump(mode="json")
        rule, key = None, None
        if not _same(name, artifact.preset):
            rule = "binding_preset"
        elif not _same(params, artifact.params):
            rule, key = "binding_params", _first_difference(params, artifact.params)
        elif not isinstance(components, tuple) or components != artifact.components:
            rule = "binding_components"
        elif not _same(spec, bound_spec):
            rule, key = "binding_spec", _first_difference(spec, bound_spec)
        if rule is not None:
            raise SandboxError(
                "sandbox_binding_mismatch", line=call.lineno, rule=rule, name=key
            )

    # -------------------------------------------------------------- call graph

    def acyclic(self) -> None:
        """Refuse every call that closes a cycle of helpers (a depth-first walk with an
        explicit stack, so no helper count can exhaust the host's own recursion)."""
        state: dict[str, int] = {}
        for root in sorted(self.edges):
            if state.get(root, 0):
                continue
            state[root] = 1
            stack = [(root, iter(self.edges.get(root, [])))]
            while stack:
                name, pending = stack[-1]
                for callee, line in pending:
                    mark = state.get(callee, 0)
                    if mark == 1:
                        self.record(SandboxError("sandbox_recursion", line=line, rule="recursion"))
                    elif mark == 0:
                        state[callee] = 1
                        stack.append((callee, iter(self.edges.get(callee, []))))
                        break
                else:
                    state[name] = 2
                    stack.pop()


def _helper_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    constants: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Check a helper's name and parameters; return its parameters' default values.

    Annotations are allowed and ignored. A parameter may reuse a builtin's name.
    """
    args = node.args
    params = [arg.arg for arg in args.args]
    line = node.lineno

    def refuse(rule: str, name: str | None = None, at: int = line) -> SandboxError:
        return SandboxError("sandbox_helper", line=at, rule=rule, name=name)

    if not _HELPER_NAME.fullmatch(node.name) or node.name in constants:
        raise refuse("helper_name")
    if node.name in _RESERVED:
        raise refuse("helper_reserved", node.name)
    if node.name in language.BUILTINS:
        raise refuse("helper_builtin_name", node.name)
    if node.decorator_list:
        raise refuse("helper_decorator")
    if node.type_params:
        raise refuse("helper_type_params")
    if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.kw_defaults:
        raise refuse("helper_parameter_kind")
    if len(params) > language.PARAMETER_LIMIT:
        raise refuse("helper_parameter_count")
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if is_async and (params[:1] != ["context"] or len(args.defaults) == len(params)):
        raise refuse("helper_async_context")
    for param in params[1:] if is_async else params:
        if (
            not _LOCAL_NAME.fullmatch(param)
            or param.startswith("__")
            or param in _RESERVED
            or param in constants
        ):
            raise refuse("helper_parameter_name")
    _signature_annotations(node)
    defaulted = args.args[len(args.args) - len(args.defaults) :]
    defaults = {
        arg.arg: _default_value(default, constants)
        for arg, default in zip(defaulted, args.defaults, strict=True)
    }
    if not is_async:
        lines = [
            getattr(inner, "lineno", line)
            for inner in _code_nodes(node.body)
            if isinstance(inner, ast.Await) or _name(inner, "context")
        ]
        if lines:
            raise refuse("helper_sync_await", at=min(lines))
    return defaults


def _bound_by_imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """What refused imports bind, anywhere in the file, each to its module's key: module
    names (``import re`` binds re) and names taken from a module (``from json import
    loads`` binds loads). The 4 fixed imports bind nothing here."""
    modules: dict[str, str] = {}
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = _module_key(alias.name)
                if key is not None:
                    modules[alias.asname or key] = key
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            key = _module_key(node.module)
            for alias in node.names:
                if key is not None and (node.module, alias.name) not in _IMPORTS:
                    names[alias.asname or alias.name] = key
    return modules, names


def _module_in(node: ast.expr, modules: dict[str, str], names: dict[str, str]) -> str | None:
    """The key of a refused import an expression reads, if it reads one."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            module = modules.get(inner.id) or names.get(inner.id)
            if module is not None:
                return module
    return None


def _entrypoint(
    statements: list[ast.stmt], errors: list[SandboxError]
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, list[ast.stmt]]:
    """The run definition (the last one, as Python keeps) and the other statements.

    Refused, and recorded: no run, a run that is not last, and a second run.
    """
    runs = [
        statement
        for statement in statements
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "run"
    ]
    if not runs:
        line = statements[-1].lineno if statements else None
        errors.append(SandboxError("sandbox_entrypoint", line=line, rule="run_last"))
        return None, statements
    run = runs[-1]
    for extra in runs[:-1]:
        errors.append(SandboxError("sandbox_entrypoint", line=extra.lineno, rule="run_twice"))
    if statements[-1] is not run:
        after = statements[[id(item) for item in statements].index(id(run)) + 1]
        errors.append(SandboxError("sandbox_entrypoint", line=after.lineno, rule="run_last"))
    return run, [statement for statement in statements if not any(statement is r for r in runs)]


def _module_constant(
    statement: ast.Assign | ast.AnnAssign,
    constants: dict[str, JsonValue],
    known: set[str],
    imports: tuple[dict[str, str], dict[str, str]],
    errors: list[SandboxError],
) -> None:
    """One module-level assignment: an UPPER_CASE constant holding a literal. A refused
    one still binds its name (to None), so the code that reads it is not refused again."""
    line = statement.lineno
    value = statement.value
    if isinstance(statement, ast.AnnAssign):
        if value is None:
            errors.append(SandboxError("sandbox_constant", line=line, rule="constant_no_value"))
            if isinstance(statement.target, ast.Name):
                known.add(statement.target.id)
            return
        try:
            _annotation_shape(statement.annotation)
        except SandboxError as exc:
            errors.append(exc)
        target: ast.expr | None = statement.target
    else:
        target = statement.targets[0] if len(statement.targets) == 1 else None
    if not isinstance(target, ast.Name) or not _CONSTANT_NAME.fullmatch(target.id):
        errors.append(SandboxError("sandbox_constant", line=line, rule="constant_name"))
        for item in statement.targets if isinstance(statement, ast.Assign) else [target]:
            if item is not None:
                known.update(_target_names(item))
        target = None
    elif target.id in constants:
        errors.append(SandboxError("sandbox_constant", line=line, rule="constant_duplicate"))
        return
    elif len(constants) >= language.CONSTANT_LIMIT:
        errors.append(SandboxError("sandbox_constant", line=line, rule="constant_limit"))
    try:
        literal = _constant_value(cast(ast.expr, value))
    except SandboxError as refused:
        literal = None
        module = _module_in(cast(ast.expr, value), *imports)
        named = module if module in language.MODULES else None
        errors.append(
            refused
            if module is None
            else SandboxError("sandbox_expression", line=line, rule="module_use", name=named)
        )
    if target is not None:
        constants[target.id] = literal


def _first_refusal(errors: list[SandboxError]) -> SandboxError:
    """The refusals by line (the order found within a line), each kept once; the first is
    raised and carries them all."""
    ordered = sorted(errors, key=lambda error: error.line if error.line is not None else 0)
    unique: dict[Refusal, None] = {}
    for error in ordered:
        unique.setdefault(Refusal(error.line, error.code, error.rule, error.name), None)
    first = ordered[0]
    first.found = tuple(unique)
    return first


def _validated_v2(source: str, artifact: HarnessArtifact) -> _ProgramV2:
    """The language 2 gate; see ``sandbox_language`` for the table it enforces.

    Past parsing, the check records each refusal and goes on, so the error it raises
    (the first refusal by line) lists every refused line in ``refusals``.
    """
    try:
        tree, count, deepest = _parse_bounded(source, language.NODE_LIMIT_V2)
        preset = get_preset(artifact.preset)
        preset.build(artifact.spec, artifact.params, artifact.components)
        errors: list[SandboxError] = []
        statements = [
            statement for statement in _header(tree, errors) if not _main_guard(statement)
        ]
        run, others = _entrypoint(statements, errors)
        if run is not None:
            try:
                _run_signature(run)
            except SandboxError as exc:
                errors.append(exc)
        imports = _bound_by_imports(tree)
        constants: dict[str, JsonValue] = {}
        helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        classes: set[str] = set()
        known: set[str] = set()
        for statement in others:
            line = statement.lineno
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                _module_constant(statement, constants, known, imports, errors)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name in helpers:
                    errors.append(
                        SandboxError("sandbox_helper", line=line, rule="helper_duplicate")
                    )
                    continue
                helpers[statement.name] = statement
                if len(helpers) > language.HELPER_LIMIT:
                    errors.append(SandboxError("sandbox_helper", line=line, rule="helper_limit"))
            elif isinstance(statement, ast.AugAssign):
                errors.append(SandboxError("sandbox_constant", line=line, rule="constant_change"))
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                errors.append(_import_refusal(statement))
            elif isinstance(statement, ast.ClassDef):
                classes.add(statement.name)
                errors.append(SandboxError("sandbox_statement", line=line, rule="class"))
            else:
                errors.append(
                    SandboxError("sandbox_entrypoint", line=line, rule="module_statement")
                )
        defaults: dict[str, dict[str, JsonValue]] = {}
        lenient: set[str] = set()
        for name, helper in helpers.items():
            try:
                defaults[name] = _helper_signature(helper, constants)
            except SandboxError as exc:
                errors.append(exc)
                defaults[name] = {}
                lenient.add(name)
        grammar = _GrammarV2(
            artifact,
            helpers,
            constants,
            defaults,
            errors,
            run=run,
            lenient=frozenset(lenient),
            modules=imports[0],
            imported=imports[1],
            classes=frozenset(classes),
            known=frozenset(known),
        )
        for helper in helpers.values():
            grammar.function(helper)
        if run is not None:
            grammar.function(run)
        grammar.acyclic()
        if errors:
            raise _first_refusal(errors)
        run = cast(ast.AsyncFunctionDef, run)
        walked = list(ast.walk(tree))
        stats = SourceStats(
            language=2,
            nodes=count,
            depth=deepest,
            helpers=len(helpers),
            constants=len(constants),
            loops=sum(isinstance(node, (ast.For, ast.While)) for node in walked),
            await_sites=sum(isinstance(node, ast.Await) for node in walked),
            bytes=len(source.encode("utf-8")),
        )
        program = _ProgramV2(run, helpers, constants, stats, defaults)
        _desugar(program)
        return program
    except SandboxError:
        raise
    except SyntaxError as exc:
        raise SandboxError(
            "sandbox_invalid_source",
            line=exc.lineno,
            rule=_syntax_rule(source, exc),
            col=exc.offset if type(exc.offset) is int else None,
        ) from exc
    except (ValueError, TypeError, RecursionError, UnicodeError, MemoryError) as exc:
        rule = "syntax_null" if "\x00" in source else None
        raise SandboxError("sandbox_invalid_source", rule=rule) from exc


# ------------------------------------------------------------------ comprehensions


class _Comprehension(ast.expr):
    """A comprehension after the check desugared it (``_Desugar``).

    ``body`` is the loop nest of ordinary statements that the comprehension stands for,
    built from its own validated parts: for clauses become for statements, conditions
    become one if, and the element is appended to (or set into) ``result``, a name no
    source can spell. The interpreter runs ``body`` in place, as an expression, with the
    fuel those statements cost, then restores ``names`` (the loop names and ``result``)
    to what they held, so nothing a comprehension binds outlives it. For ``any`` and
    ``all`` the body returns the answer and ``result`` is None.
    """

    _fields = ("body",)

    def __init__(self, body: list[ast.stmt], names: tuple[str, ...], result: str | None) -> None:
        super().__init__()
        self.body = body
        self.names = names
        self.result = result


_Node = TypeVar("_Node", bound=ast.AST)


class _Desugar(ast.NodeTransformer):
    """Replace each comprehension of a checked function by a ``_Comprehension``.

    Only a checked program is desugared, so every generator expression left in it is the
    list argument of a builtin that takes one. ``any(...)`` and ``all(...)`` of a
    generator become loops that return at the first item that decides them; every other
    generator expression builds the list a list comprehension would.
    """

    def __init__(self) -> None:
        self.serial = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id in ("any", "all")
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.GeneratorExp)
        ):
            return self.loops(node.args[0], func.id, node)
        self.generic_visit(node)
        return node

    def visit_ListComp(self, node: ast.ListComp) -> ast.AST:
        return self.loops(node, "list", node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> ast.AST:
        return self.loops(node, "list", node)

    def visit_SetComp(self, node: ast.SetComp) -> ast.AST:
        return self.loops(node, "set", node)

    def visit_DictComp(self, node: ast.DictComp) -> ast.AST:
        return self.loops(node, "dict", node)

    def loops(self, node: _Comprehensions, kind: str, where: ast.expr) -> _Comprehension:
        self.generic_visit(node)  # nested comprehensions first
        self.serial += 1
        result = f"<comprehension {self.serial}>"

        def at(new: _Node) -> _Node:
            return ast.copy_location(new, where)

        def load(name: str) -> ast.Name:
            return at(ast.Name(id=name, ctx=ast.Load()))

        def call(builtin: str, *args: ast.expr) -> ast.stmt:
            value = at(ast.Call(func=load(builtin), args=list(args), keywords=[]))
            return at(ast.Expr(value=value))

        def returns(answer: bool) -> list[ast.stmt]:
            return [at(ast.Return(value=at(ast.Constant(value=answer))))]

        names = [result]
        inner: list[ast.stmt]
        if isinstance(node, ast.DictComp):
            inner = [call("set_key", load(result), node.key, node.value)]
        elif kind == "set":
            item = f"<comprehension {self.serial} item>"
            names.append(item)
            seen = at(ast.Compare(left=load(item), ops=[ast.NotIn()], comparators=[load(result)]))
            inner = [
                at(ast.Assign(targets=[at(ast.Name(id=item, ctx=ast.Store()))], value=node.elt)),
                at(ast.If(test=seen, body=[call("append", load(result), load(item))], orelse=[])),
            ]
        elif kind == "any":
            inner = [at(ast.If(test=node.elt, body=returns(True), orelse=[]))]
        elif kind == "all":
            inner = [at(ast.If(test=node.elt, body=[], orelse=returns(False)))]
        else:
            inner = [call("append", load(result), node.elt)]
        body = inner
        for generator in reversed(node.generators):
            if generator.ifs:
                test = (
                    generator.ifs[0]
                    if len(generator.ifs) == 1
                    else at(ast.BoolOp(op=ast.And(), values=list(generator.ifs)))
                )
                body = [at(ast.If(test=test, body=body, orelse=[]))]
            body = [at(ast.For(target=generator.target, iter=generator.iter, body=body, orelse=[]))]
        for generator in node.generators:
            names.extend(_target_names(generator.target))
        if kind in ("any", "all"):
            body = [*body, *returns(kind == "all")]
            return at(_Comprehension(body, tuple(dict.fromkeys(names[1:])), None))
        empty: ast.expr = (
            at(ast.Dict(keys=[], values=[]))
            if isinstance(node, ast.DictComp)
            else at(ast.List(elts=[], ctx=ast.Load()))
        )
        start = at(ast.Assign(targets=[at(ast.Name(id=result, ctx=ast.Store()))], value=empty))
        return at(_Comprehension([start, *body], tuple(dict.fromkeys(names)), result))


def _desugar(program: _ProgramV2) -> None:
    """Desugar the comprehensions of a checked program's functions, in place."""
    transformer = _Desugar()
    for function in (*program.helpers.values(), program.run):
        function.body = [
            cast(ast.stmt, transformer.visit(statement)) for statement in function.body
        ]


def _valid_v2(source: str, artifact: HarnessArtifact) -> bool:
    probe = artifact.model_copy(
        update={"forge": ForgeBinding(call_limit=CALL_LIMIT, fuel=language.DEFAULT_FUEL)}
    )
    try:
        _validated_v2(source, probe)
    except SandboxError:
        return False
    return True


def _v1_or_language_error(
    source: str, artifact: HarnessArtifact
) -> tuple[ast.AsyncFunctionDef, SourceStats]:
    """Run the frozen v1 gate; name a v2 construct in a v1 binding ``sandbox_language``.

    The remap happens only when language 1 rejects the source, language 2 would
    accept it, and it contains a language-2-only construct; every other v1
    rejection keeps its exact v1 code.
    """
    try:
        return _validated_v1(source, artifact)
    except SandboxError as exc:
        if exc.code in {"sandbox_source_limit", "sandbox_invalid_source", "sandbox_ast_limit"}:
            raise
        if not _valid_v2(source, artifact):
            raise
        construct = _v2_construct(ast.parse(source))
        if construct is None:
            raise
        raise SandboxError("sandbox_language", line=getattr(construct, "lineno", None)) from None


def _validated_tree(source: str, artifact: HarnessArtifact) -> ast.AsyncFunctionDef:
    """The validated ``run`` definition of either language version."""
    if artifact.forge is None:
        return _v1_or_language_error(source, artifact)[0]
    return _validated_program(source, artifact).run


def _validated_program(source: str, artifact: HarnessArtifact) -> _ProgramV2:
    """The validated language 2 module: constants, helpers and ``run``."""
    binding = artifact.forge
    if binding is None or binding.language != 2:
        raise SandboxError("sandbox_language")
    return _validated_v2(source, artifact)


def validate_source(source: str, artifact: HarnessArtifact) -> SourceStats:
    """Validate without importing authored code or executing its annotations.

    ``artifact.forge is None`` selects the frozen language 1 gate; a Forge binding
    with ``language == 2`` selects ``_GrammarV2``. Raises ``SandboxError(code,
    line=...)``.
    """
    if artifact.forge is None:
        return _v1_or_language_error(source, artifact)[1]
    return _validated_program(source, artifact).stats


def call_limit(artifact: HarnessArtifact) -> int:
    """The dynamic per-run op cap: 64 for language 1, the binding's cap for language 2."""
    return CALL_LIMIT if artifact.forge is None else artifact.forge.call_limit


def rebind_source(source: str, old: HarnessArtifact, new_spec: Spec) -> str:
    """Replace only preset spec literals; keep custom guarded-call control flow."""
    if old.forge is not None:
        _validated_program(source, old)
        module = ast.parse(source)
        found = False
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _name(node.func, "execute_preset"):
                node.args[3] = ast.parse(repr(new_spec.model_dump(mode="json")), mode="eval").body
                found = True
        rebound = ast.unparse(module) + "\n" if found else source
        new = old.model_copy(update={"spec": new_spec, "report": None, "execution_mode": "sandbox"})
        validate_source(rebound, new)
        return rebound
    tree = _validated_tree(source, old)
    changed = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func, "execute_preset"):
            node.args[3] = ast.parse(repr(new_spec.model_dump(mode="json")), mode="eval").body
            changed = True
    rebound = ast.unparse(tree) + "\n" if changed else source
    new = old.model_copy(update={"spec": new_spec, "report": None, "execution_mode": "sandbox"})
    validate_source(rebound, new)
    return rebound


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    waiter = asyncio.create_task(process.wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    waiter.result()
    if cancelled:
        raise asyncio.CancelledError


def _image_stubs(context: RunContext, names: JsonValue) -> tuple[ImageStub, ...]:
    """Resolve worker sha256 names against this run's attached images only.

    The worker omits ``images`` on text-only calls, so an empty list, a
    non-hex name, more than the request cap, or a name the run never attached
    is a protocol violation.
    """
    if (
        not isinstance(names, list)
        or not names
        or len(names) > MAX_IMAGES_PER_REQUEST
        or not all(isinstance(name, str) and _SHA256.fullmatch(name) for name in names)
    ):
        raise SandboxError("sandbox_invalid_call")
    stubs: list[ImageStub] = []
    for index, name in enumerate(names, start=1):
        image = context._attached_image(str(name))
        if image is None:
            raise SandboxError("sandbox_invalid_call")
        stubs.append(image.stub(index))
    return tuple(stubs)


class _Nothing:
    def close(self) -> None:
        return None


def _contain(process: asyncio.subprocess.Process, *, required: bool) -> Closeable:
    """Cap the worker's memory and tie its lifetime to the host (a cap, not a jail).

    A language 2 worker (``required``) has loops, so its space is bounded only
    together with the OS cap: when the cap cannot be applied the run fails closed
    with ``sandbox_worker_failed`` instead of running uncapped. That path is total:
    ANY exception from ``contain`` (not only ``OSError``) becomes
    ``sandbox_worker_failed``, so the caller always reaps the worker. Language 1
    keeps its Phase 4 behavior (an ``OSError`` runs uncapped). Test doubles
    without a pid have no process to contain.
    """
    if not isinstance(getattr(process, "pid", None), int):
        return _Nothing()
    try:
        return contain(process, memory_bytes=language.WORKER_MEMORY_WINDOWS)
    except Exception as exc:
        if required:
            raise SandboxError("sandbox_worker_failed") from None
        if isinstance(exc, OSError):
            return _Nothing()
        raise


async def _memory_killed(process: asyncio.subprocess.Process, job: Closeable) -> bool:
    """Whether a worker that closed its pipe without a frame was stopped by a memory cap."""
    exceeded = getattr(job, "memory_exceeded", None)
    if callable(exceeded) and exceeded():
        return True
    try:
        code = await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, OSError):
        return False
    names = ("SIGKILL", "SIGSEGV", "SIGABRT", "SIGBUS")
    killed = {-int(getattr(signal, name)) for name in names if hasattr(signal, name)}
    return os.name != "nt" and code in killed


def _error_frame(message: dict[str, JsonValue]) -> SandboxError:
    """A language 2 ``{op: error, code, line}`` frame, or ``sandbox_worker_failed``."""
    code, line = message.get("code"), message.get("line")
    if (
        set(message) == {"op", "code", "line"}
        and isinstance(code, str)
        and code in language.RUNTIME_ERROR_CODES
        and (line is None or (type(line) is int and line >= 1))
    ):
        return SandboxError(code, line=line)
    return SandboxError("sandbox_worker_failed")


def sandbox_harness(artifact: HarnessArtifact) -> Harness:
    """Load only data in the host; create and reap one interpreter for each run."""
    try:
        path = artifact.directory / "harness.py"
        with path.open("rb") as stream:
            raw = stream.read(SOURCE_LIMIT + 1)
        if len(raw) > SOURCE_LIMIT:
            raise SandboxError("sandbox_source_limit")
        source = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SandboxError("sandbox_source_unreadable") from exc
    stats = validate_source(source, artifact)
    version = stats.language
    fields = {"spec", "preset", "params", "components", "model"}
    # Send no directory, report, expected label, adapter, wallet, or dataset object.
    binding = artifact.model_dump(
        mode="json", include=fields | ({"forge", "execution_mode"} if version == 2 else set())
    )
    limit = call_limit(artifact)
    game = artifact.spec.execution_config.get("kind") in _GAME_KINDS
    bound = set(artifact.forge.subharnesses) if artifact.forge is not None else set()
    fuel = artifact.forge.fuel if artifact.forge is not None else 0

    async def run(context: RunContext, task: JsonValue) -> JsonValue:
        context.limit_calls(limit)
        # Pages stay in the host: only stubs cross, so no page nears MESSAGE_LIMIT.
        lifted, refs = lift_images(task)
        context.attach_images([ref for ref in refs if isinstance(ref, ImageInput)])
        initial = _encode(
            {"source": source, "binding": binding, "task": lifted, "language": version}
        )

        async def exchange() -> JsonValue:
            with TemporaryDirectory(prefix="architect-sandbox-") as temporary:
                environment = {"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
                if os.name == "nt" and "SystemRoot" in os.environ:
                    environment["SystemRoot"] = os.environ["SystemRoot"]
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-u",
                        "-m",
                        "architect.runtime._sandbox_worker",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=temporary,
                        env=environment,
                        limit=MESSAGE_LIMIT,
                    )
                )
                try:
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    # A second cancellation must not abandon an in-flight spawn.
                    while not spawning.done():
                        try:
                            await asyncio.shield(spawning)
                        except asyncio.CancelledError:
                            pass
                    process = spawning.result()
                    await _reap(process)
                    raise
                try:
                    job = _contain(process, required=version == 2)
                except BaseException:
                    # Never leave an uncontained worker running, whatever failed.
                    await _reap(process)
                    raise
                try:
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.write(initial)
                    await process.stdin.drain()
                    calls = 0

                    async def reply(value: JsonValue) -> None:
                        assert process.stdin is not None
                        process.stdin.write(_encode({"value": value}))
                        await process.stdin.drain()

                    def count() -> None:
                        nonlocal calls
                        calls += 1
                        if calls > limit:
                            raise SandboxError("sandbox_invalid_call")

                    while True:
                        data = await process.stdout.readline()
                        if not data and version == 2 and await _memory_killed(process, job):
                            raise SandboxError("sandbox_memory")
                        message = _decode(data)
                        if not isinstance(message, dict):
                            raise SandboxError("sandbox_invalid_message")
                        op = message.get("op")
                        if op == "result" and set(message) == {"op", "value"}:
                            return message["value"]
                        if version == 2:
                            if op == "result" and set(message) == {"op", "value", "fuel"}:
                                used = message["fuel"]
                                if type(used) is not int or not 0 <= used <= fuel:
                                    raise SandboxError("sandbox_invalid_message")
                                context.fuel_used = used
                                return message["value"]
                            if op == "error":
                                error = _error_frame(message)
                                if error.code == "sandbox_fuel":
                                    context.fuel_used = fuel
                                raise error
                            if op == "observe" and set(message) == {"op"}:
                                if not game:
                                    raise SandboxError("sandbox_invalid_call")
                                await reply(await context.game_observe())
                                continue
                            if op == "step" and set(message) == {"op", "action"}:
                                if not game or not isinstance(message["action"], dict):
                                    raise SandboxError("sandbox_invalid_call")
                                if calls >= limit:
                                    raise SandboxError("sandbox_invalid_call")
                                observation = await context.game_step(message["action"])
                                # A refused step sent nothing and is not counted.
                                if not (
                                    isinstance(observation, dict)
                                    and observation.get("refused") is True
                                ):
                                    count()
                                await reply(observation)
                                continue
                            if op == "harness" and set(message) == {"op", "name", "input"}:
                                name = message["name"]
                                if not isinstance(name, str) or name not in bound:
                                    raise SandboxError("sandbox_invalid_call")
                                count()
                                await reply(
                                    await context.subharness(artifact, name, message["input"])
                                )
                                continue
                        if op == "preset":
                            if set(message) != {"op", "task"} or not (
                                artifact.preset == "coder"
                                or artifact.spec.pipeline
                                or artifact.spec.execution_config.get("kind")
                                in {"game", "competition"}
                            ):
                                raise SandboxError("sandbox_invalid_preset")
                            calls += 1
                            if calls > limit:
                                raise SandboxError("sandbox_invalid_call")
                            from architect.presets import execute_preset

                            value = await execute_preset(
                                artifact.preset,
                                artifact.params,
                                artifact.components,
                                artifact.spec.model_dump(mode="json"),
                                context,
                                message["task"],
                            )
                            await reply(value)
                            continue
                        keys = set(message)
                        # A bound preset whose spec asks for ``reply_json`` "schema" sends
                        # its reply schema with each call (Phase 14, Section 9.2). Under
                        # any other mode the key is unknown, as before.
                        schema = message.get("schema")
                        if "schema" in keys and artifact.spec.reply_json == "schema":
                            keys.discard("schema")
                        if op != "call" or not (
                            keys in (_CALL_KEYS, _IMAGE_CALL_KEYS)
                            or (version == 2 and _CALL_KEYS <= keys <= _SYSTEM_CALL_KEYS)
                        ):
                            raise SandboxError("sandbox_worker_failed")
                        calls += 1
                        prompt, tokens = message["prompt"], message["tokens"]
                        system = message.get("system")
                        if (
                            calls > limit
                            or not isinstance(prompt, str)
                            or (tokens is not None and (type(tokens) is not int or tokens < 1))
                            or ("system" in message and not isinstance(system, str))
                            or (
                                isinstance(system, str)
                                and _utf8_size(system) > language.SYSTEM_LIMIT
                            )
                            or ("schema" in message and not isinstance(schema, dict))
                        ):
                            raise SandboxError("sandbox_invalid_call")
                        stubs = (
                            _image_stubs(context, message["images"]) if "images" in message else ()
                        )
                        response = await context.call(
                            prompt,
                            max_output_tokens=tokens,
                            system=system if isinstance(system, str) else None,
                            images=stubs,
                            response_schema=schema if isinstance(schema, dict) else None,
                        )
                        await reply(response)
                except (OSError, ValueError) as exc:
                    if isinstance(exc, SandboxError):
                        raise
                    raise SandboxError("sandbox_protocol_failed") from exc
                finally:
                    try:
                        await _reap(process)
                    finally:
                        job.close()

        return await context._within_deadline(exchange())

    return run
