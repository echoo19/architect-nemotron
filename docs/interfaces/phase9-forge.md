# Phase 9 "Forge": a harness-engineering agent on Pi, sandbox language v2, licensed sub-harnesses and per-turn game play

Status: implemented (2026-09-27); live Stages B and C pending operator approval; see PROGRESS.md. It extends `docs/interfaces/phase4.md` (sandbox), `phase6-games.md`, `phase6-runtime.md`, `phase7-session.md` and `phase8-bench.md`. It explicitly supersedes two sentences of `phase4.md` (Section 4.12) and nothing else. Where this text and the code differ after implementation, the code, `docs/usage.md` and `docs/bench.md` are authoritative. Line references are to the checkout at commit `fdd7c41`.

This document is the contract for four implementers working in parallel on disjoint files (Section 18), plus one integration package. Every public signature, file path, closed label, error code, JSON shape and operator-facing string below is fixed. Anything not listed is an implementation detail owned by the file's author.

This contract starts from the judges' winning design ("a Pi agent loop in the Stage 3 slot, sandbox language v2, and three trusted tools") and grafts in the following:
- the free `forge_check` / `forge_dryrun` tools;
- native grid builtins and a shared `games/grids.py`;
- a template library and a `template` bench arm;
- a submit-time literal-leak lint;
- the budget line in tool results;
- v2 grammar gated by the binding, with v1 frozen;
- a narrowly scoped `try/except`;
- `per_case_unavailable` and the `aggregate` feedback mode;
- raising the outer wallet by the Forge budget, with operator consent;
- a game call cap that counts steps and does not count observes;
- a real Job Object memory limit;
- a guard that `forge.measures > 0`.

Two alternatives were considered and rejected:
- **Held-out-inheriting projection floors for children.** A child license bit would then leak parent held-out information.
- **Gating on `is_evaluation`.** It is `False` at delivery (`runtime/__init__.py:743`), so it cannot keep the Forge tools away from play time.

## 1. Goal and fixed properties

Today Stage 3 (`compiler/__init__.py:204-232` → `compiler/rewriting.py:217`) is one blind JSON-diff call (`rewriting.py:258-292`). It is accepted only by the cost-only `_cheaper` rule (`rewriting.py:175`). The emitted `harness.py` is usually a one-line `execute_preset(...)` wrapper. On ARC-AGI-3 the model then has to carry game state in its prompt and oscillates: PROGRESS.md:116 records gemma4:26b, 80 actions, 724 calls, score 0.0.

Phase 9 delivers:

1. **Sandbox language v2.** It is a strict, versioned superset of v1: helpers, fuel-bounded loops, arithmetic and ordering, mutation, f-strings, DataError-only `try/except`, and pure native builtins, including ARC grid helpers. It also adds three host-mediated capabilities: per-turn `observe`/`step`, licensed sub-harness calls, and `images=`/`system=` on `context.call`.
2. **Forge.** An opt-in, build-time session on the real pinned Pi agent (`@mariozechner/pi-coding-agent` 0.73.1, `architect/backends/pi.py`), placed in the Stage 3 slot. It writes harness code in a throwaway workspace and reaches trusted code only through five tools: `forge_check`, `forge_dryrun`, `forge_measure`, `forge_submit` and `forge_spawn`.
3. **Licensed sub-harnesses.** Each child is compiled through the normal Compiler path (Stages 1, 2 and 4), bound into the parent by digest, and called at play time through the host.
4. **A template library** of v2 reference harnesses. It is used as Forge's starting point and as an agent-free `template` mode.
5. **Bench arms** `default | template | forge`, with `--pair-variants` compare and ARC process metrics for every arm. The ARC experiment has pre-registered pass criteria.

Fixed properties. These are inherited and never weakened:

- **Stage 4 is the only licensing authority.** Forge never constructs, reads, edits or signs a `TestReport` for the parent. `verify()` (`compiler/__init__.py:234`) runs exactly once per ladder model, on the promoted best candidate, after the Pi process tree has been reaped. Its result, score, failures and `verification_id` never reach any tool result, workspace file or prompt of the session that produced the candidate.
- **Tuning only.** Forge code touches only `FloorEvaluator.tuning` (public by design, `floors/base.py:144`) and the tuning partition of games. `held_out` (`base.py:148`) and the verifier capability (`floors/_access.py`) stay reachable only from `compiler/verification.py`.
- **Money and time only.** Budgets are dollars and seconds. Tokens are metered into telemetry and never capped. No token gate is added.
- **Interpreted allowlist.** Sandbox code is never passed to `compile()` or `exec()`. There is no `import`, no attribute traversal (except `context.<capability>` directly under `await`), and no Python object reachable from a name. The only side effects are the capability RPCs, and the host re-validates each one.
- **v1 is frozen.** Every artifact with `forge is None` validates under the unchanged v1 `_Grammar`, runs under unchanged v1 interpreter semantics, and keeps a byte-identical `artifact_digest`.
- **Opt-in.** With `ForgeSettings.enabled = False`, the default, no Forge code runs, no new CLI output appears, and every existing test passes unchanged.
- **Content-free telemetry, rows and `forge.json`.** Only counts, dollars, seconds, codes, digests and sizes. No source, prompt, case, reply or note text.
- **No live providers and no Ollama in any test.** Nothing in the Lectern checkout is read or referenced.
- **Rendering** is ASCII only, with no ANSI, at most 100 columns, and deterministic. Money from CLI providers is labelled "API-equivalent, not a bill".

## 2. Vocabulary

| Term | Meaning |
|---|---|
| agent model | The model driving the Pi session at build time (`ForgeSettings.agent_model`). Any profile model: cloud API, CLI or Ollama. |
| play model | The ladder model the harness will run with. It is fixed for the session, and every measurement uses it through `adapter_factory`. |
| draft | The current contents of `<workspace>/harness.py`. Never persisted. |
| candidate | A draft that `forge_submit` wrote to the forge-private candidate store and measured on all tuning (plus smoke). |
| accepted | A candidate that passed the acceptance rule (Section 8.8). |
| promoted | The single best accepted candidate, copied into `artifacts/<spec_hash>/<n>/` after the session and handed to `verify()`. |
| child | A sub-harness compiled and licensed by `forge_spawn`. |
| build context | A `RunContext` with `is_build=True`, created only by `Runtime.build`. |

## 3. File list, responsibilities, owners

**Day-0 rule.** Each WP lands its public model definitions verbatim from this contract as its first commit on day 0, so the other WPs import real types instead of stubs:
- WP3: `ForgeBinding`, `SubharnessRef`, `HarnessArtifact.forge`
- WP4: `ForgeSettings`
- WP2: `TrustedTool`, `ForgeFields`, `Variant`

| File | Responsibility | Owner |
|---|---|---|
| `architect/runtime/sandbox_language.py` (new) | The single table of v2 statements, expressions, builtins, capabilities, error codes and limits; `language_markdown()` | WP1 |
| `architect/runtime/sandbox.py` (edit) | `validate_source` dispatch v1/v2; `_GrammarV2`; `SourceStats`; dynamic call cap; host op loop gains `observe`, `step`, `harness` and `system`; worker spawned under `_proc_limits` | WP1 |
| `architect/runtime/_sandbox_worker.py` (edit) | v2 `_Interpreter` (helpers, loops, fuel, value semantics, try/except, construction bounds, `{op:error,code,line}`); `_RPC` gains `observe`, `step`, `harness` and `system` | WP1 |
| `architect/runtime/_sandbox_builtins.py` (new) | Native pure builtins with fuel charging | WP1 |
| `architect/runtime/_proc_limits.py` (new) | Windows Job Object with memory limit plus kill-on-close; POSIX `RLIMIT_AS` | WP1 |
| `architect/games/grids.py` (new) | Shared grid functions: `grid_diff` (moved verbatim from `games/execution.py:176`), `grid_text`, `objects`, `count_colors`, `find_cells`, `crop`, `grid_equal`, `validate_grid` | WP1 |
| `architect/spec/__init__.py` (edit) | `SubharnessRef`, `ForgeBinding`, `HarnessArtifact.forge`; `_config` omits `forge` when `None` | WP3 |
| `architect/runtime/__init__.py` (edit) | `RunContext.is_build`; `Runtime.build`; `RunContext.game_observe`, `game_step`, `subharness` | WP3 |
| `architect/runtime/subharness.py` (new) | Play-time execution of a bound licensed child | WP3 |
| `architect/runtime/evaluation.py` (edit) | `measure_cases`, `CaseDetail`, `GameDetail`, `dry_run_case`, `DryRun`; `evaluate_cases` unchanged | WP3 |
| `architect/models/fake.py` (edit) | `ScriptedAdapter`: zero-priced and deterministic, used by `forge_dryrun` | WP3 |
| `architect/games/execution.py` (edit) | `_Play` refactor of `GameBackend.run` (`:292-460`); `open_turns`; process counters; imports `grid_diff` from `games/grids.py` | WP3 |
| `architect/games/factory.py` (edit) | `GameFactoryConfig.allow_forge` | WP3 |
| `architect/library/__init__.py` (edit) | `store`/`snapshot` copy `subharnesses/` recursively; `verification_count`; `record_forge_verification` | WP3 |
| `architect/backends/pi.py` (edit) | `TrustedTool`; `PiBackend(capabilities=)`; `capability` RPC kind; `is_build` gate; soft errors; result bounding; per-tool caps; `compile_agent` re-expressed as one `TrustedTool` | WP2 |
| `architect/backends/pi-node/extension.ts`, `bridge.mjs` (edit) | Register `init.capabilities` via `pi.registerTool` with `Type.Unsafe(parameters)`; forward `capability` RPC | WP2 |
| `architect/profiles.py` (edit) | `Profile.forge_agent`; wires `Compiler(forge_adapter=, pi_config=)` | WP2 |
| `architect/cli.py` (edit) | `--forge*` flags on compile/do/create/recompile; `--yes`; `architect forge report`; bench `--variant forge/template`, `--forge-*`, `compare --pair-variants` | WP2 |
| `architect/session/forge.py` (new) + `state.py`, `commands.py`, `codes.py`, `render.py`, `session.py` (edit) | Session verb `forge`; codes; progress line | WP2 |
| `architect/bench/rows.py`, `suite.py`, `runner.py`, `compare.py`, `render.py`, `oracle.py` (edit) | Variants `forge` and `template`; `ForgeFields`; ARC process metrics; `pair_variants`; `ForgeScript` | WP2 |
| `architect/compiler/forge_settings.py` (new) | `ForgeSettings` (re-exported from `compiler/types.py`) | WP4 |
| `architect/compiler/types.py`, `compiler/__init__.py` (edit) | `CompileConfig.forge`; `Compiler(forge_adapter=, pi_config=)`; Stage 3 dispatch; outer-wallet raise | WP4 |
| `architect/compiler/forge.py` (new) | Driver: wallets, workspace, Pi session via `Runtime.build`, candidate store, acceptance, promotion, `forge.json` | WP4 |
| `architect/compiler/forge_tools.py` (new) | The five `TrustedTool` factories; JSON schemas; leakage-safe views | WP4 |
| `architect/compiler/forge_workspace.py` (new) | Writes `harness.py`, `LANGUAGE.md`, `SPEC.md`, `TUNING.md`, `BASELINE.json`, `templates/`, `NOTES.md` | WP4 |
| `architect/compiler/forge_lint.py` (new) | Literal-leak lint | WP4 |
| `architect/compiler/forge_templates/__init__.py` + `*.py` (new) | Template library: `prompt_chain`, `extract_verify`, `vision_two_pass`, `game_explorer`, `game_hypothesis`; `instantiate` | WP4 |
| `examples/forge/` (new) | `arc_explorer.py` (committed v2 reference), `scripts/*.json` (ForgeScript tool-call scripts), `README.md` | WP4 |
| `examples/local/profile-forge.json` (new) | ARC experiment profile (Section 14) | WP4 |
| Tests | Section 15 | per WP |
| `docs/interfaces/phase9-forge.md`, `DECISIONS.md`, `docs/usage.md`, `docs/bench.md`, `docs/limitations.md`, `PROGRESS.md`, `.github/workflows/ci.yml`, `examples/bench/suite-v1/smoke-baseline.jsonl`, `examples/telemetry/arc-forge-evidence.json` | Docs, decisions, CI, baselines, evidence | WP5 |

Package boundary (added to `tests/test_package_boundary.py` in WP5):
- `architect/compiler/forge*.py`, `forge_templates/` and `architect/runtime/subharness.py` do not import `architect.floors._access`, `architect.compiler.verification` (other than the name `verify` in `compiler/__init__.py`), or any `FloorEvaluator` private attribute.
- Only `architect/compiler/forge.py` calls `Runtime.build`.
- Only `architect/compiler/forge_tools.py` constructs `TrustedTool` objects named `forge_*`.
- `architect.bench` stays importable only from `architect/bench/`, `cli.py` and `session/bench.py`.

## 4. Sandbox language v2 (WP1)

### 4.1 Gating and versioning

```python
# architect/runtime/sandbox.py
class SourceStats(NamedTuple):
    language: Literal[1, 2]
    nodes: int
    depth: int
    helpers: int
    constants: int
    loops: int
    await_sites: int
    bytes: int

def validate_source(source: str, artifact: HarnessArtifact) -> SourceStats: ...
    # artifact.forge is None -> the existing v1 _Grammar, byte-for-byte the current code path
    # artifact.forge is not None and artifact.forge.language == 2 -> _GrammarV2
    # raises SandboxError(code, line=...) exactly as today (new optional .line)
def sandbox_harness(artifact: HarnessArtifact) -> Harness: ...
def call_limit(artifact: HarnessArtifact) -> int: ...      # Section 4.9
class SandboxError(ValueError):
    code: str
    line: int | None
```

The init message to the worker gains `"language": 1 | 2`. With language 1 the interpreter keeps v1 semantics exactly: `int + int` still raises `sandbox_add`, the static `CALL_LIMIT = 64` (`sandbox.py:38`) still applies, and per-intermediate `_encode` still applies. v1 existing callers that ignore the return value of `validate_source` (previously `None`) keep working.

### 4.2 Module shape (v2)

```
["""docstring"""]                       # optional, as v1
<the 4 exact ImportFrom lines of v1>    # unchanged
CONSTANT = <literal>                    # 0..64 module constants
def helper(a, b): ...                   # 0..32 helpers, sync or async, any order
async def helper2(context, a): ...
async def run(context, task): ...       # exactly one, last
```

- **Constants.** The name matches `^[A-Z][A-Z0-9_]{0,63}$`. The value is a literal: a JSON constant, or List/Dict/Tuple of literals (a Tuple becomes a list). No calls and no f-strings. Constants are visible in every function body and are read-only: assigning to them, subscript-assigning into them or passing them as the first argument of a mutating builtin raises `sandbox_assignment` (static).
- **Helpers.** `def name(p1, ..., pk):` (sync) or `async def name(context, p2, ..., pk):` (async), with k ≤ 8. The name matches `^[a-z][a-z0-9_]{0,63}$` and must not shadow a builtin, capability, constant, `run`, `context` or `task`.
- Helpers have no defaults, `*args`, `**kwargs`, keyword-only arguments, annotations, decorators, nested `def`, `lambda`, `global`/`nonlocal` or closures. A body sees only its parameters, its locals and the constants.
- **Sync helpers** contain no `await`, never mention `context`, and call only builtins and sync helpers.
- **Async helpers** have `context` as their literal first parameter and are called only as `await name(context, ...)`.
- **The helper call graph must be acyclic** (checked statically, code `sandbox_recursion`), so call depth is at most 33.
- Helper calls are bare-name and positional only.
- **Why this is safe:** helpers are named AST blocks the interpreter walks with a fresh locals dict. No function object is ever created, and no first-class call exists.

### 4.3 Statements added (v2 only)

| Construct | Rule | Safety argument |
|---|---|---|
| `for T in E:` (no `else`) | `T` is a Name, or a Tuple of exactly 2 Names (unpacks `[a, b]` pairs from `enumerate`/`items`/`zip`). At run time `E` must be a list (iterated over a snapshot copy), a dict (keys in insertion order), a str (characters) or a range object; otherwise `sandbox_type`. | Every iteration charges fuel, and the iterable is finite. |
| `while E:` (no `else`) | — | Every test evaluation charges fuel. Fuel and the host deadline bound it. |
| `break`, `continue`, `pass` | `break`/`continue` only lexically inside a loop (`sandbox_statement` otherwise). | Control flow only. |
| Loop nesting | At most 6 (`sandbox_loop_depth`). | Bounds validator and interpreter recursion. |
| Assign to a Subscript | `s[k1][k2]... = v`, rooted at a local Name that is not `task`, `context` or a constant. | Mutation reaches only the interpreter's own JSON containers. |
| Assign `a, b = E` | At run time `E` must be a list of exactly that length (`sandbox_value` otherwise). | No tuple type exists at run time. |
| AugAssign `+= -= *= //= %=` | On Name or Subscript targets; same type rules as BinOp. | Same as BinOp. |
| `try:` / `except:` or `except Exception:` | Exactly one handler, no `as`, no `else`, no `finally`. The handler catches only the DataError codes: `sandbox_key`, `sandbox_index`, `sandbox_type`, `sandbox_value`, `sandbox_dictionary`, `sandbox_add`. It never catches `sandbox_fuel`, `sandbox_memory`, `sandbox_value_limit`, `sandbox_unbound`, `sandbox_invalid_call`, capability or RPC failures, `BudgetExceeded`, the deadline or cancellation. The interpreter raises those as a separate Fatal class that no `except` matches. | Only pure data failures become recoverable. Every effect and resource failure stays fatal. |
| Unbound local | v1's "defined in one `if` branch only" static rule is relaxed in v2. Reading an unbound local is the runtime Fatal `sandbox_unbound`. | Matches Python. Local models write this pattern constantly. |
| Expr statements | Allowed: `await <capability or async helper>`, calls to mutating builtins (`append`, `extend`, `insert`, `pop`, `set_key`), and calls to sync helpers. | No other expression statement has an effect. |

Not added in v2, and still rejected: `raise`, `assert`, `with`, `match`, `del`, `global`, `nonlocal`, `lambda`, `yield`, `class`, `import`, walrus, starred, decorators and `async for`/`async with`. Comprehensions and generator expressions were refused here too until Forge pack v3, which accepts them by desugaring (4.15).

### 4.4 Expressions added (v2 only)

- **BinOp.**
  - `+` accepts str+str, list+list and num+num.
  - `- * / // %` accept int or float operands only. The check is `type(x) in (int, float)`, so bool is excluded.
  - `str * int` and `list * int` raise `sandbox_type`, which keeps the existing `'x' * 1e9` test meaningful.
  - Division or modulo by zero raises `sandbox_value`.
  - Float results must be finite (`sandbox_value`).
  - Int results must satisfy `|x| < 2**63` (`sandbox_value`).
- **Compare.** `< <= > >=` on two numbers (int or float, not bool) or two strs, otherwise `sandbox_type`. Chains are allowed. `is` and `is not` only with a literal `None` comparator (static `sandbox_comparison`). `== != in not in` work as in v1.
- **IfExp.** The ternary.
- **JoinedStr (f-strings).** No conversion (`!r` etc.) and no `format_spec`; both are rejected statically with `sandbox_expression`. A str value is inserted raw. Any other value is inserted as the `str()` builtin would render it (sorted-key compact JSON).
- **Slices** on list and str: bounds are int or None; step is None, 1 or -1.
- **Dict literals** with computed keys. A key must evaluate to str (`sandbox_dictionary`).
- **Tuple literal** in expression position evaluates to a list.
- **Calls** to a bare Name that is a builtin or helper, positional only. Keyword arguments are allowed only on capabilities (Section 4.8) and on `sorted(l, reverse=b)`.
- **Still rejected.** Attribute access except `context.<cap>` as the direct func of an awaited call; method calls other than the builtins' listed method spellings (added in 4.14); Lambda; a generator expression anywhere but the list argument of the builtins that take one (4.15); Starred; NamedExpr; Set literals.

### 4.5 Value model and bounds

- **Values** are JSON values: null, bool, int, float, str, list, dict with str keys. There is also one opaque internal range object, which only `for`, `len`, `list` and `at` accept.
- **Value semantics on store.** Storing a container into another container (subscript assign, `append`, `extend`, `insert`, `set_key`, dict literal) deep-copies it, charging 1 fuel per copied element. Binding a name to an existing value aliases it, as in Python. Therefore no container can contain itself, `append(l, l)` terminates, and encoding never loops.
- **Construction-time bounds** replace v1's per-intermediate `_encode`:
  - str at most 131,072 characters;
  - list or dict at most 65,536 entries;
  - nesting depth at most 40;
  - range at most 100,000 items.
  
  A violation raises the Fatal `sandbox_value_limit`.
- **At every RPC boundary and at return**, full `_encode` applies, with `MESSAGE_LIMIT` = 128 KiB (`sandbox.py:36`) unchanged.
- **Measured cost of the old rule:** 4,096 `_encode` calls on one 64x64 grid take about 1.6 s. The native grid builtins (Section 4.7) keep per-cell loops out of the interpreter.

### 4.6 Builtins

All builtins are native Python in `_sandbox_builtins.py`. Each is pure, deterministic and total over its documented domain. Each takes and returns JSON values and charges `fuel = 1 + ceil(work / 256)`, where `work` is the column below. A wrong argument type raises `sandbox_type`, and a wrong argument count raises `sandbox_call` (static when the call is visible).

| Builtin | Signature → result | work |
|---|---|---|
| `len` | `(str\|list\|dict\|range) → int` | 1 |
| `str` | `(x) → str`. A str is returned as-is; anything else is rendered as sorted-key compact JSON. | output chars |
| `int`, `float`, `bool` | Numeric and str parse. Non-parsable input raises `sandbox_value`. A float must be finite. | input chars |
| `abs`, `round(x[, n])`, `min(list)`, `max(list)`, `sum(list)` | Numbers only; `min`/`max` also accept a list of str. An empty list raises `sandbox_value`. | items |
| `sorted(list, reverse=False)` | The list must be homogeneous (numbers or strs). | n·⌈log2 n⌉ |
| `range(a[, b[, c]])` | Ints; at most 100,000 items; lazy. | 1 plus 1 per iteration |
| `enumerate(list) → [[i, v]]`, `zip(l1, l2) → [[a, b]]` | — | items |
| `list(x)` | From a range, str (characters) or dict (keys). | items |
| `kind(x)` | → `'null'`, `'bool'`, `'int'`, `'float'`, `'str'`, `'list'` or `'dict'` | 1 |
| `keys(d)`, `values(d)`, `items(d) → [[k, v]]` | — | items |
| `get(d, k, default)`, `at(l, i, default)` | Never raise on a missing key or index; return `default`. | 1 |
| `append(l, v)`, `extend(l, l2)`, `insert(l, i, v)`, `pop(l) → last or None`, `set_key(d, k, v)` | Mutating; value semantics. The first argument must be a local Name, not `task`, a constant or a parameter of `run`. | copied elements |
| `split(s, sep)`, `join(sep, list_of_str)`, `strip(s)`, `lower(s)`, `upper(s)`, `startswith(s, p)`, `endswith(s, p)`, `replace(s, a, b)`, `find(s, sub)`, `count(s, sub)`, `splitlines(s)` | `sep` must be a non-empty str. `replace`'s result must be at most 131,072 characters. | input chars |
| `parse_json(s) → JsonValue \| None` | Returns None when the input is over 128 KiB, deeper than 40, contains a non-finite number or has a duplicate key. Never raises. | input chars |
| `extract_json(s) → JsonValue \| None` | Takes the first balanced `{...}` or `[...]`, fenced (a json-tagged or untagged code fence) or bare, and parses it as `parse_json`. Never raises. Local models wrap JSON in prose. | input chars |
| `to_json(x) → str` | Sorted-key compact JSON. | output chars |
| `digest(x) → str` | 64-hex sha256 of canonical sorted-key JSON. Independent of `PYTHONHASHSEED`. | canonical bytes |

### 4.7 Grid builtins and `architect/games/grids.py`

`games/grids.py` is the single implementation for the host (strategy v2 in `games/execution.py`) and for the sandbox builtins. `grid_diff` moves verbatim from `games/execution.py:176`, and WP3 switches the host import. Every grid argument is validated by `validate_grid`: a rectangular list of lists of int 0..15, at most 64x64, the same rule as `games.types.Grid`. Otherwise it raises `sandbox_type`. For grid builtins, `work` equals the number of cells.

```python
def validate_grid(value: object) -> list[list[int]]: ...
def grid_diff(a, b, limit: int = 4096) -> dict   # existing _grid_diff shape: changed cells [[r, c, old, new]] (<= limit), size-mismatch marker
def grid_text(grid) -> str                       # one hex digit per cell, rows joined by "\n" (~4.2 KB for 64x64 vs ~13.7 KB JSON)
def objects(grid, background: int = 0) -> list   # 4-connected same-colour components, <= 256, each
                                                 # {"color","cells","x0","y0","x1","y1","cx","cy"}; cx/cy rounded to 2 decimals
def count_colors(grid) -> dict                   # {"<color>": n}, keys as decimal str
def find_cells(grid, color: int) -> list         # [[x, y]], <= 4096
def crop(grid, x0, y0, x1, y1) -> list           # inclusive bounds, clipped; empty crop -> []
def grid_equal(a, b) -> bool
```

The sandbox names are `grid_diff`, `grid_text`, `objects`, `count_colors`, `find_cells`, `crop`, `grid_equal` and `diff_cells`. `diff_cells(a, b, limit)` is an alias of `grid_diff` with `limit ≤ 4096`.

### 4.8 Capabilities

Capabilities are valid only as the direct operand of `await`, inside `run` or an async helper. Each one becomes one RPC, and the host re-validates it.

| Source form | Wire op (worker→host) | Host checks | Result |
|---|---|---|---|
| `await context.call(prompt[, tokens], system=S, images=L)` | `{"op":"call","prompt","tokens","system"?,"images"?}` | v1 checks. `system` is a str of at most 64 KiB. `images` must be a list of exact `'$image'` stub dicts taken from `task`. Only their sha256 names are sent, and the host resolves them through the existing `_image_stubs` against this run's attached images only; an unknown sha raises `sandbox_invalid_call`. | str reply |
| `await execute_preset(<exact binding>, context, x)` | unchanged | unchanged | unchanged |
| `await context.observe()` | `{"op":"observe"}` | Static: `artifact.spec.execution_config.kind in {"game","competition"}`, else `sandbox_capability`. Runtime: the scoped `_Play` (Section 6.2). | Observation JSON (Section 6.2) |
| `await context.step(action)` | `{"op":"step","action":{...}}` | Same static rule. `decode_action` against the host-offered actions. | Observation JSON |
| `await context.harness("<str literal>", value)` | `{"op":"harness","name","input"}` | Static: the literal must be a key of `artifact.forge.subharnesses` (`sandbox_binding_mismatch`), mirroring the `execute_preset` exact-binding rule. Runtime: Section 6.3. | Child output JSON |

Scoring, `finish`/finalize, datasets, floors and tools are not reachable from code.

### 4.9 Limits and caps

| Limit | v1 (unchanged) | v2 |
|---|---|---|
| Source bytes | 64 KiB | 64 KiB |
| AST nodes | 2,000 | 8,000 (measured: the ARC explorer draft is 2,565 bytes, 564 nodes, depth 11; a 5-helper variant is 3,492 bytes, 811 nodes) |
| AST depth | 40 | 40 |
| Helpers, constants, loop nesting | — | 32, 64, 6 |
| Static await sites | 64 (`sandbox_call_limit`) | none (loops make a static count meaningless) |
| Dynamic op cap | 64 (`sandbox.py:184, 367, 425`) | `call_limit(artifact)` = `artifact.forge.call_limit` |
| Fuel per run | — | `artifact.forge.fuel` (default 10,000,000; at most 100,000,000) |
| Worker memory | — | 512 MiB on Windows (Job Object); 1 GiB `RLIMIT_AS` on POSIX |
| Wall clock | `_within_deadline` | unchanged |

**Fuel.** Fuel charges 1 unit per evaluated AST node, plus builtin work and copy charges. Exhaustion is the Fatal `sandbox_fuel`. A fuel budget of 10M is roughly 5-10 s of interpreter time on the CI runner. The measured ARC explorer uses about 300 nodes per turn, which is far below that.

**Dynamic op cap.** The host counter counts `call`, `preset`, `harness` and `step` ops, 1 each. `observe` is free. A child's own model calls count against the child's own `limit_calls` and the shared wallet, not against the parent's cap. `sandbox_harness` calls `context.limit_calls(call_limit(artifact))` in place of the constant, which fixes the latent clash between 64 calls and 256-turn games. `ForgeBinding.call_limit` is computed at submit time:
- text specs: `ForgeSettings.call_limit` (default 128, at most 1024);
- game specs: `policy.limits.max_turns * ForgeSettings.calls_per_turn` (default 2, at most 4), capped at 100,000.

Independently, the host enforces `GameLimits.max_actions` on steps.

### 4.10 Error codes

**New validation codes** (static, raised by `validate_source`, each with `line`): `sandbox_recursion`, `sandbox_loop_depth`, `sandbox_helper`, `sandbox_constant`, `sandbox_language`. The last one means a v2 construct appeared in a v1-bound artifact.

**Existing codes are reused unchanged** (for example `sandbox_statement`, `sandbox_expression`, `sandbox_assignment`, `sandbox_capability`, `sandbox_binding_mismatch`, `sandbox_ast_limit`). v1 keeps its exact code set.

**Runtime codes**, a closed set: `sandbox_key`, `sandbox_index`, `sandbox_type`, `sandbox_value`, `sandbox_dictionary`, `sandbox_add` (the DataError class), plus the Fatal codes `sandbox_unbound`, `sandbox_fuel`, `sandbox_memory`, `sandbox_value_limit`, `sandbox_invalid_call`.

**Error frame.** The worker sends `{"op":"error","code":<code>,"line":<int authored line>}` with no message text and no traceback. The host raises `SandboxError(code, line=line)`.
- At play time and in Stage 2/3/4, the run fails exactly as `sandbox_worker_failed` does today: the case is failed, and telemetry keeps `code` and `line`.
- Forge tools return `code` and `line` to the agent. The source is the agent's own, so this discloses nothing.
- A `MemoryError` in the worker, or a Job Object or `RLIMIT_AS` kill, becomes `sandbox_memory`.

### 4.11 `sandbox_language.py` and `LANGUAGE.md`

```python
LANGUAGE_VERSION: Final = 2
NODE_LIMIT_V2: Final = 8000
DEFAULT_FUEL: Final = 10_000_000
MAX_FUEL: Final = 100_000_000
class BuiltinSpec(NamedTuple): name: str; signature: str; returns: str; work: str; mutating: bool; hint: str
class CapabilitySpec(NamedTuple): name: str; source_form: str; op: str; game_only: bool
BUILTINS: Mapping[str, BuiltinSpec]
CAPABILITIES: Mapping[str, CapabilitySpec]
STATEMENTS: Mapping[str, str]          # ast class name -> one-line rule
EXPRESSIONS: Mapping[str, str]
RUNTIME_ERROR_CODES: frozenset[str]
DATA_ERROR_CODES: frozenset[str]
HINTS: Mapping[str, str]               # every validation/runtime code -> one fixed ASCII hint (<= 160 chars)
def language_markdown() -> str: ...    # deterministic; the Forge LANGUAGE.md
```

`_GrammarV2`, `_Interpreter` (v2 paths) and `_sandbox_builtins.BUILTIN_FUNCTIONS` read these tables, so the document the model sees cannot drift from the validator. A golden-file test pins `language_markdown()`.

Forge pack v2 adds `METHODS`, `TYPE_NAMES`, `RESERVED`, `RULES` (each static refusal's rule: its code and fixed sentences), `ALTERNATIVES` (Python names the language lacks, with the form that does the job) and `refusal_message(code, rule, name, line)`. `language_markdown()` now renders a compact card of about 1,200 words (allowed forms with an example each, the helper rules, the 4 imports, what is refused and why). The same card ends Forge's prompt (8.6).

### 4.12 Safety argument and contract changes

**DECISIONS.md:21 invariant** ("code runs only as an interpreted allowlist with capability RPCs"). It holds because:
- no new name resolves to a Python object;
- builtins are a closed dict of native functions over JSON values;
- there is no attribute access, so no reflection: a method spelling (4.14) is only another way to write a listed builtin call, and nothing is looked up on a value;
- annotations are never evaluated, and each must be shaped like a type, so none can hold a call;
- the only effects are the five capabilities, each re-checked by the host;
- time is bounded by fuel and the unchanged deadline;
- space is bounded by construction limits and the OS memory cap;
- aliasing cycles are impossible by value semantics;
- recursion is impossible by the acyclic call graph.

**Contract changes to `phase4.md`, recorded in DECISIONS:**
1. "no unbounded loops" becomes "loops only in language v2, and only under fuel". The comprehension ban was kept until Forge pack v3 (4.15), which desugars a comprehension into the for loops it stands for, metered as they are.
2. The static await-site count is removed for v2, and the dynamic per-artifact cap becomes the authority.

The Phase 8 rejection of `images=` in sandbox calls (`tests/test_sandbox.py:1080-1092`) is deliberately reversed for v2 only. v1 still rejects it.

### 4.13 `_proc_limits.py`

```python
class Closeable(Protocol):
    def close(self) -> None: ...
def contain(process: asyncio.subprocess.Process, *, memory_bytes: int | None) -> Closeable: ...
    # Windows: CreateJobObject + JOBOBJECT_EXTENDED_LIMIT_INFORMATION with
    #   LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000)
    #              | JOB_OBJECT_LIMIT_PROCESS_MEMORY (0x100) when memory_bytes is set,
    #   ProcessMemoryLimit = memory_bytes; AssignProcessToJobObject.
    # POSIX: returns a no-op Closeable (the worker limits itself).
def limit_self(memory_bytes: int) -> None: ...   # POSIX: resource.setrlimit(RLIMIT_AS, ...) ; Windows: no-op
```

The existing Pi job (`backends/pi.py:150-165`) sets only `KILL_ON_JOB_CLOSE` (`:156`). The memory limit is new code with its own tests, not a factor-out. The sandbox worker is spawned under `contain(..., memory_bytes=512 * 2**20)`, and worker `_main` calls `limit_self(2**30)` before reading source. WP5 switches `backends/pi.py` to `contain(..., memory_bytes=None)` with no behavior change. This is a resource cap, not isolation: safety still comes from interpretation.

### 4.14 Forge pack v2 additions (2026-10-04)

In the G1 sessions on native tool calls (`docs/feedback/probes/2026-10-04-forge-native-g1.md`), 23 of Ultra's 31 `forge_check` calls failed on `sandbox_helper`, and the ones whose source was kept failed on annotated helpers. Every construct those sessions were refused for was decided once, here. The language stays version 2: each change only accepts source that was refused before, so every artifact that validated still validates and runs the same way.

| Construct | Decision | Why |
|---|---|---|
| Type annotations on parameters, returns and assignments with a value | Accept and ignore | Never evaluated, so they cost no fuel and run nothing. An annotation must be shaped like a type (names, dotted names, subscripts, `\|`, the literals a type can hold); a call, await or lambda in one is refused (`annotation_form`). |
| Default parameter values | Accept a literal or a module CONSTANT | Fixed at check time and copied for each call, as a constant read is; anything else is refused (`helper_default_value`). A call may leave trailing defaults off. |
| A variable or parameter named like a builtin (`count = 0`) | Accept | Names are frame keys and calls resolve by name, so `count(s, sub)` still reaches the builtin. |
| The builtins' method spellings (`s.split(sep)`, `l.append(x)`, `d.get(k)`; 19 in `METHODS`) | Accept | Exactly the builtin with the receiver as its first argument: the same metering and type checks, and nothing looked up on a value. The mutating ones keep the local-variable rule. |
| `isinstance(x, T)` with T in str, int, float, bool, list, dict | Accept | T is checked statically and never evaluated; Python's rule that a bool is an int holds. |
| `get(d, k)` and `at(l, i)` with no default | Accept, default None | As `dict.get`; one more arity of an existing builtin. |
| `execute_preset` binding arguments held in UPPER_CASE constants | Accept | Constants are literals fixed at check time, and the binding is still compared exactly. |
| A helper named after a builtin (`def lower(s)`) | Refuse | A call by that name must mean one thing; the G1 wrapper `def lower(s): return lower(s)` recurses forever in Python. |
| An async helper or `run` whose first parameter is not `context` (`ctx`) | Refuse | `context` is the one capability root the checker tracks by name. |
| Comprehensions, generator expressions, `any`, `all` | Refuse (reversed in pack v3, 4.15) | A nested scope with rules of its own; a for loop says the same with metered steps. |
| `chr`, `ord`, `reversed` and other missing functions | Refuse | Not builtins; the message names the form that does the job. |
| Keyword arguments to helpers | Refuse | Not seen in the logs; positional calls cover them. |
| A binding that differs from the artifact's | Refuse | It is the licensed artifact's identity; the message names the argument and the first differing key of the binding. |
| Reassigning `task` or `context`; a helper defined twice | Refuse | Inputs and helpers stay fixed. |

Every static refusal names its rule (`SandboxError.rule`, a `RULES` key) and, for some rules, one of the language's own identifiers (`SandboxError.name`). A syntax error maps to one of a few fixed rules chosen from the parser's message (the message itself never crosses). The Forge tools turn these into the `message` of Section 9.

### 4.15 Forge pack v3 additions (2026-10-04)

In the G1 retry (`docs/feedback/probes/2026-10-04-forge-native-g1-retry.md`) Ultra wrote ordinary Python: comprehensions in 21 of its 24 parsed drafts, imports, an `if __name__ == "__main__":` block, its own `run` signature, and each check named only the first refused construct. The language stays version 2 and still only grows, with one exception: `any` and `all` are builtins now, so a helper can no longer take either name (no kept draft or template does).

| Construct | Decision | How |
|---|---|---|
| One check, every refused line | Collect | Past parsing, the check records each refusal and goes on: a statement's refusal leaves the next statements and its own nested blocks checked, a refused part of an expression leaves the other parts checked, and module-level refusals (imports, constants, classes, run placement, helper signatures) leave the functions checked. A refused name is still bound, so its readers are not refused again; calls to a helper whose signature is refused are not counted against it. `SandboxError` is the first refusal by line; `SandboxError.refusals` lists them all (`Refusal(line, code, rule, name)`), deduplicated, outer before inner on a line. |
| `[e for x in l if c]`, `{k: v for ...}`, `{e for ...}` | Accept | Checked as the loops they stand for: each `for` clause binds one name or two (`for_target`), follows the binding rules (`assignable`) and is a loop level (`loop_depth`); the first iterable is read in the enclosing scope, and a name read before its own clause binds it is `unknown_name`, as Python would fail it. After the check, `_Desugar` replaces each by a `_Comprehension` node: the loop nest of ordinary statements (`for`, one `if` per clause, `append`/`set_key` into a result no source can spell), which the interpreter runs in place as an expression. |
| A set comprehension | Accept, as a list | Values are JSON, so `{e for ...}` gives a list without repeats in first-seen order (`if v not in out: append(out, v)`, metered by `contains`). |
| A generator expression | Accept only as the list argument of `any`, `all`, `sum`, `min`, `max`, `sorted`, `list` and `join` (`GENERATOR_CALLEES`) | Only where it is the last thing the call evaluates (the one argument, the second of `join`, `sorted`'s first with a literal `reverse=`), so its loop runs where Python's would; anywhere else it is `generator`. `any`/`all` of one return at the first deciding item; the others get the list a list comprehension builds. |
| `any(list)`, `all(list)` | Accept, as builtins | Python's truth over a list; work = items. |
| `if __name__ == "__main__":` at module level, no `else` | Drop at check time | Nothing in it is checked or run. Any other module-level `if` stays `module_statement`. |
| An import of a known module (`MODULES`: json, re, typing, dataclasses, enum, asyncio, architect, forge, math, collections, string, itertools, functools, decimal, copy) | Refuse, naming the replacement | `import_module` names the module and the builtins that do the job (json.loads is parse_json, json.dumps is to_json; re has no counterpart). Every later read of a name that import bound is `module_use` with the same advice. No module is ever imported. |
| A class | Refuse (`class`); its uses are `class_use` | No objects exist; records are dicts. |
| `run`'s first line | Accept `async def run(context, task)` with any type-shaped annotations | Anything else (a plain `def`, other names, defaults, markers, decorators) is `run_signature`, whose message gives the line to write. |

**Fuel and copies.** A comprehension costs exactly the for loop it stands for (tests compare the two forms): 1 for the comprehension node, which pays for the read of the loop's result, then the loop's own statements, iterations and copies. Each element is stored with `store`, so containers are copied at 1 fuel per element, and the result is bounded by the 65,536-entry and depth-40 limits at construction. Several conditions of one clause become one `if c1 and c2`. `any` and `all` of a generator cost their loop up to the item that decides them, plus 2 for the return that ends it. The scope costs no fuel: at most two names per clause, the result and a set comprehension's item are saved and restored.

**Scope.** The names a comprehension binds (its loop names and its result) are saved when it starts and restored (or removed) when it ends, however it ends, so none outlives it. Only those names can be bound inside it, so nothing else in the enclosing scope changes; a container it reads can still be mutated through a builtin, as a Python comprehension can. The interpreter's return state, which `any` and `all` use to stop early, is restored too.

### 4.16 Forge pack v4 additions (2026-10-05)

The kept drafts of G1, its two retries and the engineer bake-off were still refused most for `except` with a type, `isdigit` and the other character tests, `chr` and `ord`, `split()` with no separator, `lstrip`, `rstrip`, `rfind`, `.copy()`, `max(a, b)` and `zip` of three lists. Each is accepted now, and nothing accepted before is refused or runs differently, with the v3 exception again: the new builtins' names (`chr`, `ord`, `copy`, `lstrip`, `rstrip`, `rfind`, `isdigit`, `isalpha`, `isalnum`, `isspace`, `isupper`, `islower`) can no longer name a helper. No kept draft or template defines one. There are still no regular expressions and no new capability.

| Construct | Decision | How |
|---|---|---|
| `except T:`, `except (T1, T2):`, `except T as e:` | Accept, one except still | Each T is a name from `EXCEPTION_NAMES` (Exception, ValueError, TypeError, KeyError, IndexError, LookupError, ArithmeticError, ZeroDivisionError, OverflowError, AttributeError, UnicodeError), checked statically and never evaluated. Any of them catches every data error, as a bare `except:` does, and never a fatal one. `e` follows the binding rules, holds the error's fixed code (a str such as `"sandbox_value"`) while the handler runs, and is unbound after it, however it ends, as Python unbinds it. Neither costs fuel. |
| `for a, b, c in ...` (also in a comprehension) | Accept three names (`LOOP_NAMES_LIMIT`) | For `zip` of three lists. Each item must be a list of exactly that many values, else `sandbox_value`; a loop costs what the same loop with two names costs. |
| `isdigit(s)`, `isalpha`, `isalnum`, `isspace`, `isupper`, `islower`, and `s.isdigit()` | Accept, as Python's `str` methods | Work = len(s), plus a native walk of len(s) (1 per 16). |
| `chr(i)`, `ord(c)` | Accept | `i` an int (not a bool) from 0 to 1,114,111, else `sandbox_type` or `sandbox_value`; `c` a str of one character, else `sandbox_type`. Work = 1. |
| `split(s)`, `s.split()` | Accept, Python's split on runs of whitespace | Work = len(s), plus native walks of len(s) and of the parts. `split(s, None)` keeps the data error it always gave. A maxsplit argument is still refused. |
| `strip(s, chars)`, `lstrip(s[, chars])`, `rstrip(s[, chars])` | Accept, chars a str or None | With no chars (or None) the whitespace form, charged as `strip(s)` always was. With chars: work = len(s), native walks of len(s) + len(chars), and len(s) * len(chars) / 1,024 for CPython's lookup of each removed character in chars, charged before the work. |
| `rfind(s, sub)`, `s.rfind(sub)` | Accept | Work = len(s), plus (len(s) - len(sub) + 1) * len(sub) / 1,024 when sub has 2 to len(s) characters: CPython's reverse search has no two-way fallback, so no window bound applies. |
| `copy(x)`, `l.copy()`, `d.copy()` | Accept | The whole value, copied as `list(l)` copies a list (value semantics: a copy shares nothing, at any depth). Exactly `list(l)`'s fuel for a list. |
| `min(a, b)`, `max(a, b)` | Accept | `max([a, b])`, at exactly its fuel for numbers and strs (1 for the list it stands for). |
| `zip(l1, l2, l3)` | Accept | Work = the shortest length, plus 1 per item taken from each list, plus the copies of what is taken. Two lists cost what they did. |

The fuel of each was measured against its CPython worst case on this host (`tests/adversarial/test_phase9_sandbox.py`, `WALKS`): at most 0.29 s per million fuel, within the contract's 6 s. A 10M-fuel loop of the worst `rfind` and of `strip` with 32,768 UCS4 characters each ends in `sandbox_fuel` in under 3 s through the worker.

## 5. Artifact binding (WP3)

```python
# architect/spec/__init__.py
SubharnessName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]

class SubharnessRef(_Schema):                 # frozen, extra="forbid"
    artifact_digest: SHA256
    spec_hash: SHA256
    input_schema_sha256: SHA256               # sha256 of canonical JSON of the child input_schema
    output_schema_sha256: SHA256
    case_origin: Literal["projection", "authored"]

class ForgeBinding(_Schema):                  # frozen, extra="forbid"
    language: Literal[2] = 2
    call_limit: Annotated[int, Field(ge=1, le=100_000, strict=True)]
    fuel: Annotated[int, Field(ge=100_000, le=100_000_000, strict=True)]
    subharnesses: dict[SubharnessName, SubharnessRef] = Field(default_factory=dict)   # <= 8

class HarnessArtifact(_Schema):               # existing fields plus:
    forge: ForgeBinding | None = None

    def _config(self) -> dict[str, JsonValue]:
        exclude = {"spec", "directory", "report"} | ({"forge"} if self.forge is None else set())
        return self.model_dump(mode="json", exclude=exclude)
```

- `forge` is in `_config` when set. It is therefore in `artifact_digest` and in the parent's signed `TestReport`.
- Changing any byte of a child changes the child digest, so the ref no longer matches and the call fails at run time (`subharness_digest_mismatch`). Changing the binding itself invalidates the parent license.
- **Golden-digest tests** pin every committed fixture's digest with `forge=None`.
- **A Forge artifact** always has `execution_mode="sandbox"` and `forge` set.
- **Directory layout** of a promoted Forge artifact: `harness.py`, `spec.json`, `config.json`, `report.json`, `forge.json`, and `subharnesses/<name>/` holding each child's full directory, including nested `subharnesses/`.
- **Library** (`library/__init__.py`): `store` and `snapshot` copy `subharnesses/` recursively.
  - `HarnessLibrary.verification_count(spec_hash: str) -> int` and `HarnessLibrary.record_forge_verification(spec_hash: str) -> None` persist a per-spec_hash counter in the library index.
  - Warm starts of Forge artifacts reuse them only for the exact model (existing keying) and always re-run Stage 4 (existing behavior).
- **Game factory** (`games/factory.py`): `GameFactoryConfig.allow_forge: bool = Field(default=False, strict=True)`.
  - When True, and only then, the delivery refusal `game_sandbox_forbidden` (`:304`) is lifted for a licensed artifact with `execution_mode == "sandbox"` and `forge is not None`.
  - `game_runtime_rewriting_forbidden` (`:101`) stays.
  - `_SafeLibrary` (`:455-462`) keeps rejecting sandbox warm starts.

## 6. Host runtime (WP3)

### 6.1 Build context

```python
# architect/runtime/__init__.py
class RunContext:
    is_build: bool                  # new constructor kwarg is_build: bool = False; copied by fork()

class Runtime:
    async def build(
        self, spec: Spec, harness: Harness, task: JsonValue, adapter: ModelAdapter,
        *, preset: str, repository: Path,
    ) -> RunResult: ...
        # compilation_wallet(spec.budget); RunContext(is_evaluation=False, is_build=True,
        # repository=repository); telemetry rows as today with `preset`; never licenses.
```

`Runtime.run` (`:656`) and `Runtime.evaluate` (`:678`) always create `is_build=False`. Only `compiler/forge.py` calls `Runtime.build` (boundary test).

### 6.2 Game turns: `_Play`

`GameBackend.run` (`games/execution.py:292-460`) is refactored into `_Play` without changing behavior, so that whole-run play and per-turn sandbox play share every check:
- policy equality;
- `task_key` equal to the original task;
- one play per `run_id`;
- a target or `evaluation_refs` grant;
- the backend expected for `is_evaluation`;
- offered actions and RESET withholding;
- `max_actions`, `max_turns` and prompt size;
- score source and metric validation at finish.

```python
class _Play:
    async def observe(self) -> JsonValue: ...
    async def step(self, action: JsonValue) -> JsonValue: ...
    async def finish(self) -> JsonValue: ...                 # exactly what GameBackend.run returns today
    counters: PlayCounters                                   # actions, distinct_states, revisits, model_calls
class PlayCounters(NamedTuple):
    actions: int; distinct_states: int; revisits: int; model_calls: int; levels_completed: int
    trace: tuple[tuple[str, int, bool], ...]                 # (action token, changed_cells, revisited), last 24

class GameBackend:
    def open_turns(self, context: RunContext, task: JsonValue, *, preset: str,
                   params: dict[str, JsonValue], components: tuple[str, ...],
                   scored: bool = True) -> _Play: ...
    async def run(...) -> JsonValue: ...                      # unchanged signature; now begin/offered/step/finish via _Play
    last_counters: PlayCounters | None                        # of the most recent play in this backend instance
```

Observation JSON, returned by both `observe` and `step`:

```json
{"frame": [[0, 3, ...], ...], "frame_count": 1, "state": "<public state label>",
 "available_actions": ["ACTION1", "..."], "levels_completed": 0, "win_levels": 3,
 "actions_taken": 0, "actions_left": 80, "refused": false}
```

- **Frame.** `frame` is the settled last frame only, at most 64x64, about 13.7 KB JSON, well under `MESSAGE_LIMIT`.
- **Action argument.** `action = {"name": <str>, "x"?: 0..63, "y"?: 0..63}` is decoded with `decode_action` against the host-offered actions. An unoffered or malformed action is the Fatal `sandbox_invalid_call`.
- **Refused steps.** A step after a terminal state, or once `actions_left == 0`, sends nothing and returns the current observation with `"refused": true`. It is counted in neither actions nor the op cap.
- **Game output.** For game specs, the output of the run is produced by the host, not the harness. After the harness returns or fails, the host calls `_Play.finish()`, which closes the session. On success the output is `finish()`'s value, identical to `GameBackend.run`. On a harness failure the case is failed as today. The harness's own return value must encode under 128 KiB and is otherwise ignored.
- **`RunContext.game_observe` / `game_step`** open the `_Play` lazily on first use against the game backend already scoped to this run: the same object `execute_preset` uses for game kinds.
- **Dry runs.** `scored=False`, used only by `forge_dryrun`, requires a local or `official_offline` backend. It never calls the score path, and its `finish()` returns `{"scored": false}`.
- **Process counters** are computed host-side for every game run, preset or sandbox. `distinct_states` counts `digest` values of settled frames. A revisit is a step whose resulting frame digest was already seen in this play.

### 6.3 Sub-harness execution (`runtime/subharness.py`)

```python
async def run_subharness(context: RunContext, parent: HarnessArtifact, name: str, value: JsonValue) -> JsonValue: ...
# RunContext.subharness(self, parent, name, value) delegates here.
```

Steps, in order. Each failure is `SandboxError(code)` and fatal for the parent run.
1. `name in parent.forge.subharnesses`, else `subharness_unknown`.
2. `child = HarnessArtifact.load(parent.directory / "subharnesses" / name)`. The child must satisfy `child.licensed`, `child.artifact_digest == ref.artifact_digest`, `child.spec.spec_hash == ref.spec_hash` and `child.model == parent.model`; otherwise `subharness_digest_mismatch` or `subharness_unlicensed`.
3. Validate `value` against the child `input_schema` (`sandbox_invalid_call`).
4. Lineage: `len(context.lineage) > 4` raises `subharness_depth`.
5. `child_ctx = context.fork(context.adapter, min(child.spec.budget, parent remaining))`. Run `load_harness(child.directory, artifact=child)(child_ctx, value)` under `context._within_deadline`, then `context.absorb(child_ctx)` (`runtime/__init__.py:269, 291`).
6. Validate the output against the child `output_schema` (`subharness_schema_error`).

Child execution mode may be native or sandbox. Children are persistent lifecycle, so there is no `.consumed` marking.

### 6.4 Tuning measurement and dry runs (`runtime/evaluation.py`)

```python
class GameDetail(BaseModel):     # frozen, extra="forbid"
    levels_completed: int; actions: int; distinct_states: int; revisit_rate: float
    final_state: str; calls_per_action: float
    trace: list[tuple[str, int, bool]] | None        # last 24 (action, changed_cells, revisited), only if requested

class CaseDetail(BaseModel):     # frozen, extra="forbid"
    index: int; passed: bool | None; score: float | None; failure: str | None
    sandbox_line: int | None; calls: int; cost_usd: float
    output_excerpt: str | None; expected_excerpt: str | None
    game: GameDetail | None

async def measure_cases(
    artifact: HarnessArtifact, floor: FloorEvaluator, indexes: Sequence[int],
    adapter_factory: AdapterFactory, runtime: Runtime, *,
    excerpt_chars: int = 600, trace: bool = False, per_case: bool = True,
) -> tuple[Measurement, tuple[CaseDetail, ...]]: ...

class DryRun(BaseModel):         # frozen, extra="forbid"
    ok: bool; code: str | None; line: int | None
    calls: list[dict[str, JsonValue]]   # <= 8: {"prompt_head","prompt_chars","system_chars","images","tokens"}
    output_head: str; schema_ok: bool; fuel_used: int; steps: int

async def dry_run_case(
    artifact: HarnessArtifact, floor: FloorEvaluator, index: int, replies: Sequence[str],
    runtime: Runtime, *, game_steps: int = 5, prompt_head_chars: int = 300, output_head_chars: int = 500,
) -> DryRun: ...
```

**`measure_cases`**
- Cases are resolved only as `floor.tuning[i]`. Scoring goes through the floor's public `score` on those tuning cases; the existing `_validate_score_inputs` (`floors/base.py:160`) already rejects any verification case with `HeldOutAccessError`.
- It never touches `held_out`, `_verification_cases` or the verifier capability.
- **Per-case scoring** calls `floor.score([case], [output])`. It is disabled, with `passed` and `score` set to None, when the floor's per-score minimum exceeds 1 (`base.py:125`, `HUMAN_EDIT_RATE`). In that case, if `len(indexes) < minimum_samples`, the function raises `ValueError("forge_measure_too_small")`.
- `per_case=False` (the `aggregate` feedback mode) returns `passed`, `score`, `output_excerpt` and `expected_excerpt` as None.
- The aggregate `Measurement` is computed exactly as `evaluate_cases` does. `evaluate_cases` (`:91`) is unchanged and is still what Stages 2, 3 and 4 and `forge_submit` use.

**`dry_run_case`**
- It uses `ScriptedAdapter(replies)` from `architect/models/fake.py`. That adapter prices every request at $0, cycles through the replies, records prompt, system and image-count shapes, and takes no wallet reservation.
- It runs the real worker through `Runtime.evaluate` with preset label `forge_dryrun`.
- For game specs it uses `open_turns(scored=False)` on `floor.tuning[index]`'s game ref and stops after `game_steps` steps.
- Gateway (online) game backends raise `ValueError("forge_dryrun_unavailable")`.

## 7. Pi trusted tools (WP2)

```python
# architect/backends/pi.py
FORGE_TOOL_NAMES: Final = frozenset({"forge_check", "forge_dryrun", "forge_measure", "forge_submit", "forge_spawn"})
# PiConfig.known_tools accepts {"read","write","edit","bash","compile_agent"} | FORGE_TOOL_NAMES

@dataclass(frozen=True)
class TrustedTool:
    name: str                                            # ^[a-z][a-z0-9_]{0,63}$ (provider tool-name charset)
    description: str                                     # <= 1024 chars, fixed text
    parameters: dict[str, JsonValue]                     # JSON Schema 2020-12, additionalProperties false
    callback: Callable[[dict[str, JsonValue]], Awaitable[JsonValue]]
    max_calls: int
    max_result_chars: int = 16384
    requires_build: bool = True

class PiBackend:
    def __init__(self, config: PiConfig | None = None, *, compiler_tool: ... = None,
                 capabilities: Mapping[str, TrustedTool] = MappingProxyType({})) -> None: ...
```

**Grant.**
- At construction, every `FORGE_TOOL_NAMES` entry in `allowed_tools` must have a `TrustedTool` in `capabilities`, and every capability name must be in `allowed_tools`; otherwise `ValueError("Unsupported or duplicate Pi tools")`.
- `run()` drops every capability tool unless the backend holds capabilities and `context.is_build is True` (for tools with `requires_build`).
- `compile_agent` keeps its existing gate (`:448-450`, `:626`) and is re-expressed as one `TrustedTool` with `requires_build=False`, so existing tests pass unchanged.

**Wire.**
- The init message gains `"capabilities": [{"name","description","parameters"}]`.
- `extension.ts` registers each one with `pi.registerTool({name, label: name, description, parameters: Type.Unsafe(parameters), execute})`, where `execute` calls `rpc("capability", {name, arguments})`. TypeBox 1.3.34 in `pi-node` exports `Unsafe`, so its validation is the first filter.
- `bridge.mjs` forwards the list. The pinned 0.73.1 checks are unchanged.

**Python handling of kind `"capability"`:**
1. `_check_parent`.
2. Name not granted: fatal `PiError("Capability denied")`, because only a forged peer can send it.
3. Re-validate the arguments with `jsonschema` Draft 2020-12 against the same schema. On failure, reply softly with `{"ok": false, "code": "invalid_arguments"}`.
4. Per-tool call count greater than `max_calls`: reply softly with `{"ok": false, "code": "forge_tool_cap"}`.
5. Await the callback. `BudgetExceeded` and `CancelledError` propagate, and the next write in the same batch is stopped (copy of `test_swallowed_compiler_money_denial_stops_same_batch_write`). Any other exception becomes the soft reply `{"ok": false, "code": "tool_failed"}` with no text.
6. Serialize. If the result exceeds `max_result_chars`, the callback's own truncation is trusted when it set `"truncated": true`; otherwise the reply is replaced by `{"ok": false, "code": "result_too_large"}`. A raw oversized result is never written, so `max_protocol_bytes` can never trip.
7. `_check_parent`.

Capability calls count toward `PiConfig.max_turns` like tool approvals.

## 8. Forge driver (WP4)

### 8.1 Settings

```python
# architect/compiler/forge_settings.py   (re-exported by compiler/types.py)
TemplateName = Literal["prompt_chain", "extract_verify", "vision_two_pass", "game_explorer", "game_hypothesis"]

class ForgeSettings(BaseModel):                      # frozen, extra="forbid", allow_inf_nan=False
    enabled: bool = False
    mode: Literal["agent", "template"] = "agent"
    trigger: Literal["failed", "always"] = "failed"
    agent_model: str | None = None                   # profile.models key; default: the ladder model
    template: TemplateName | None = None             # default: chosen by spec kind (8.4)
    feedback: Literal["per_case", "aggregate"] = "per_case"
    max_usd: Annotated[float, Field(gt=0, le=1000)] = 5.0
    max_s: Annotated[float, Field(gt=0, le=86400)] = 3600.0
    max_checks: Annotated[int, Field(ge=1, le=1000)] = 200
    max_dryruns: Annotated[int, Field(ge=0, le=1000)] = 100
    max_measures: Annotated[int, Field(ge=1, le=256)] = 24
    measure_cases: Annotated[int, Field(ge=1, le=32)] = 8
    max_submissions: Annotated[int, Field(ge=1, le=32)] = 6
    max_spawns: Annotated[int, Field(ge=0, le=8)] = 3
    max_depth: Annotated[int, Field(ge=0, le=2)] = 1
    spawn_share: Annotated[float, Field(gt=0, le=0.5)] = 0.25
    spawn_max_usd: Annotated[float, Field(gt=0, le=100)] = 2.0
    pi_max_turns: Annotated[int, Field(ge=1, le=1024)] = 200
    context_chars: Annotated[int, Field(ge=4096, le=1_000_000)] = 60000
    case_chars: Annotated[int, Field(ge=200, le=20000)] = 2000
    call_limit: Annotated[int, Field(ge=1, le=1024)] = 128
    calls_per_turn: Annotated[int, Field(ge=1, le=4)] = 2
    fuel: Annotated[int, Field(ge=100_000, le=100_000_000)] = 10_000_000
    max_verifications_per_spec: Annotated[int, Field(ge=1, le=100)] = 10

    def child_settings(self, depth: int) -> "ForgeSettings": ...
        # enabled only if depth < max_depth; trigger "failed"; money/time filled by the caller
```

`CompileConfig.forge: ForgeSettings = ForgeSettings()`. `Compiler.__init__` gains `forge_adapter: ModelAdapter | None = None` and `pi_config: PiConfig | None = None`.

### 8.2 Placement, trigger and wallets

In `Compiler._compile_spec`, at the Stage 3 gate (`compiler/__init__.py:204-232`):

```python
if self.config.forge.enabled and self.smoke_factory is not None and (
    self.config.forge.trigger == "always" or not result.passed
):
    self._enter(stages, 3)                 # stage label "forge" in progress output
    forged = await forge(best, get_preset(selection.preset), floor, self.smoke_factory(spec.floor),
                         self.adapter_factory, self.runtime, self.artifact_root,
                         self.forge_adapter, self.config.forge, pi_config=self.pi_config,
                         floor_factory=self.floor_factory, library=self.library, depth=0)
    best = forged.best
    failures.extend(forged.failures)
elif <existing rewrite() condition>:
    ...                                    # unchanged
```

**Precedence.** Forge takes the Stage 3 slot when enabled. `rewrite()` and `_cheaper` are unchanged for non-Forge compiles. `verify()` at `:234` is unchanged and receives `best.artifact` and `tuning_evidence=best.measurement`.

**Wallets.**
- When `forge.enabled`, `Compiler.compile` opens its outer wallet with `compile_budget + Budget(cost_usd=forge.max_usd, compute_s=forge.max_s)` instead of `compile_budget` (`:93`).
- `forge()` opens `compilation_wallet(Budget(cost_usd=forge.max_usd, compute_s=forge.max_s))` nested inside it, so both caps hold, and Forge cannot be starved silently by an ordinary compile budget.
- The summed cap is shown to the operator before any spend (Section 12). `BudgetExceeded` from the forge wallet is caught at the `forge()` boundary and ends the session normally with the best accepted candidate so far.

**Verification cap.** Before the session: if `library.verification_count(spec_hash) >= forge.max_verifications_per_spec`, Forge does not run and `failures` gains `forge_verification_cap`.

### 8.3 Workspace

The workspace is a `TemporaryDirectory`, created outside every denied root and outside `workspace/artifacts`, `library`, `registry` and `telemetry`. A second, forge-private `TemporaryDirectory` holds `candidates/` and `spawn_datasets/`. It is added to the session's `PiConfig.denied_paths`, on top of the `profiles.build_services` denied set (`profiles.py:234-257`: floor datasets, smoke files, artifacts, library, registry, telemetry, signing key, profile).

`prepare_workspace(root: Path, candidate: CandidateEvaluation, floor: FloorEvaluator, settings: ForgeSettings) -> Path` writes:

| File | Content |
|---|---|
| `harness.py` | The incumbent: in `template` mode, or when no sandbox source exists, the instantiated template (8.4); otherwise the Stage 2 best's sandbox source. |
| `LANGUAGE.md` | `language_markdown()` |
| `SPEC.md` | Domain, task, input/output schema, floor kind and limit, budget, execution kind, play model name. No dataset path, id or case. |
| `TUNING.md` | Tuning cases indexed `0..n-1`; the first `max_cases_shown` are shown, never more than 16,000 bytes in all (W3), and every case stays reachable by index. `input` and `expected` are each truncated to `case_chars`, or shorter when the first case alone would not fit; `expected` is omitted in `aggregate` mode. Images appear as stub metadata `{sha256[:12], media_type, width, height}`, never pixels. Games list the tuning game ids and a first-observation summary (dimensions, colour counts, available actions). Case metadata fields are never written. |
| `BASELINE.json` | `{"preset", "params", "measurement": Measurement minus failures}` of the Stage 2 best. |
| `templates/*.py` | All five templates, instantiated for this binding. |
| `NOTES.md` | Empty scratch file for the agent. |

### 8.4 Template library

The templates live in `architect/compiler/forge_templates/`. Each is a v2 source with `$PRESET_CALL`-style placeholders. `instantiate(name: TemplateName, candidate: CandidateEvaluation) -> str` substitutes the exact binding literal, so the result validates under v2 for any spec of its kind. That property is tested.

| Template | For | Shape |
|---|---|---|
| `prompt_chain` | text | Compact prompt, then `extract_json`, schema-shaped fallback, one repair call. |
| `extract_verify` | text | `execute_preset` for the answer plus a cheap self-check call; retry once on mismatch. |
| `vision_two_pass` | image inputs | Pass 1: `images=` plus a layout prompt. Pass 2: text-only, or re-attaches one chosen stub. |
| `game_explorer` | game | `digest` visited set; untried-action-per-state policy; `grid_diff` change detection; model call only when stuck. Since Phase 11, edge clock bands (step or energy bars) are cropped before keying and no-op checks. Source of truth: `examples/forge/arc_explorer.py`. |
| `game_hypothesis` | game | `objects` / `grid_text` summaries; asks the model for one rule hypothesis every k turns; executes it deterministically. |

The default template, when `settings.template` is None: game kinds use `game_explorer`; specs with image inputs use `vision_two_pass`; everything else uses `extract_verify`.

**`mode="template"`** runs no Pi session and no agent model. The driver runs the `forge_submit` gate once, in process, on the instantiated default template, then promotes and verifies as in 8.9. This is the agent-free ablation arm.

### 8.5 Pi session (mode `agent`)

1. `agent_adapter` is the `forge_adapter` built from `Profile.forge_agent`. The fallback is `Profile.rewrite`, then the ladder model's adapter.
2. `PiBackend(PiConfig(allowed_tools=("read","write","edit", *sorted(FORGE_TOOL_NAMES)), allow_shell=False, denied_paths=<build_services set> + forge-private dir, max_turns=settings.pi_max_turns, max_tool_output_chars=16384), capabilities=forge_tools(session))`.
3. The pack, built by `forge.py` and not by `pack_description`: `{"backend": "pi", "tools": [...8...], "context_policy": {"max_characters": settings.context_chars}, "prompt_fragment": forge_prompt(), "subagent_rules": []}`. `forge_prompt()` is `FORGE_INSTRUCTIONS` followed by the language card. Compaction stays off. Since W3 the parent enforces `context_policy` (`PiConfig.context_cap="parent"`): past `max_characters`, the oldest tool-result bodies are dropped and the oldest write and edit texts and `forge_dryrun` and `forge_spawn` arguments are trimmed to a short note, down to three quarters of the limit, and a removal holds for the session. The extension applies the same rule before it sends a context. The system prompt, and so the card, is never thinned. Since Forge pack v4 a trimmed write or edit text reads "(Old file text not shown here. This note is not file content: read the file for its current content.)", and with the parent cap on, a `write` or `edit` whose text holds any of the cap's notes (the old "removed to keep the conversation under its size limit" form included, in any case or wrapping) is refused with a message that says to read the file and write it again: in the engineer bake-off, Super pasted the old note into harness.py 43 times.
4. `await runtime.build(agent_spec, pi_harness, {"repo": str(workspace), "goal": "write harness.py"}, agent_adapter, preset="forge_agent", repository=workspace)`. Here `agent_spec.budget = Budget(forge.max_usd, forge.max_s)`. Agent calls are ordinary metered telemetry rows with preset `forge_agent`.
5. Pi receives text only; the bridge blocks images. Only the play-time harness attaches pixels.

### 8.6 `FORGE_INSTRUCTIONS` (fixed text in `compiler/forge.py`)

The text is fixed, about 40 lines, and byte-stable across sessions so the prefix stays cacheable. It contains no budget numbers, and the play model name is referenced as "see SPEC.md". It says:

- You write `harness.py` in the restricted language of the card that ends the prompt (Forge pack v2; `LANGUAGE.md` holds the same card, so the engineer never needs to read it, and a dropped read result never takes the rules with it). It runs at play time with the model named in SPEC.md, often a small local model with a 16k context, so keep prompts short, parse replies with `extract_json`, and always have a fallback.
- Games: keep state in variables, use `digest()` for visited states, prefer untried actions before asking the model, and use `grid_text`/`objects` to summarise frames instead of raw grids.
- The loop:
  1. Read SPEC.md, TUNING.md, BASELINE.json and `templates/`.
  2. Write `harness.py`.
  3. `forge_check` until ok (free).
  4. `forge_dryrun` on one case (free).
  5. `forge_measure` on a few tuning cases, then more.
  6. Improve.
  7. `forge_submit` when it beats BASELINE. Submit early; the best submission wins.
- Use `forge_spawn` to split off a sub-task with clear input/output schemas, and call it with `await context.harness("name", x)`.
- Tuning results are evidence, not a license. A hidden verifier you cannot query decides. Never copy expected answers or long input text into code: `forge_submit` rejects it.
- Do not read files outside this directory. Each tool result ends with a `left` object showing the remaining budget.
- Stop by replying with plain text (no tool call) when done or when `left` is exhausted.

### 8.7 Session end

The session ends on the first of:
- the agent emits final text;
- `pi_max_turns` is reached;
- the forge wallet raises `BudgetExceeded`;
- `max_submissions` submissions have been used;
- operator cancellation, which propagates as today.

Afterwards the Pi process tree is reaped (existing shielded cleanup) and the workspace is deleted.

### 8.8 Acceptance rule (Forge only)

A submission is **accepted** when all of the following hold:
- `validate_source` passes;
- the literal-leak lint passes (Section 10.3);
- every child the source references is bound and licensed, and no bound child is unreferenced;
- the full tuning measurement exists (`sample_count == len(floor.tuning)`);
- `measurement.floor_passed`;
- smoke passes when a smoke floor exists (`smoke: "absent"` otherwise);
- `forge_rank(candidate) < forge_rank(best_so_far)`.

```python
def forge_rank(m: Measurement, *, smoke_passed: bool | None, subharnesses: int, components: int, nodes: int) -> tuple:
    return (not m.floor_passed, not m.budget_passed, smoke_passed is False,
            -round(m.score, 6), round(m.mean_cost_usd, 9), subharnesses + components, nodes)
```

The initial `best_so_far` is the Stage 2 best measured the same way. A Forge result is therefore never worse on tuning than the preset. Unlike `_cheaper`, this rank accepts a quality repair that costs more. It is recorded in DECISIONS as a Forge-only rule.

### 8.9 Promotion, verification and `forge.json`

1. If no submission was accepted, `ForgeResult.best` is the unchanged Stage 2 best and `failures` gains `forge_no_submission`.
2. Otherwise the best accepted candidate is copied from `candidates/` into a new version through the existing `_allocate`, including `subharnesses/`, and saved. This is the only write into `artifacts/`.
3. `forge.json` is written next to it. `library.record_forge_verification(spec_hash)` is called.
4. `verify()` then runs as today (Section 8.2). Its outcome is never fed back to any session.

```python
class ForgeResult(BaseModel):          # frozen, extra="forbid"
    best: CandidateEvaluation
    mode: Literal["agent", "template"]
    checks: int; dryruns: int; measures: int; submissions: int; accepted: int; spawns: int
    agent_cost_usd: float; agent_tokens_in: int; agent_tokens_out: int; wall_s: float
    failures: tuple[str, ...]

async def forge(candidate: CandidateEvaluation, preset: Preset, floor: FloorEvaluator,
                smoke_floor: FloorEvaluator | None, adapter_factory: AdapterFactory, runtime: Runtime,
                artifact_root: Path, agent_adapter: ModelAdapter | None, settings: ForgeSettings, *,
                pi_config: PiConfig, floor_factory: FloorFactory, library: LibraryLookup | None,
                depth: int = 0) -> ForgeResult: ...
```

`forge.json` is content-free and has exactly these keys:

```json
{"language": 2, "mode": "agent", "agent_model": "<profile key>", "play_model": "<profile key>",
 "checks": 0, "dryruns": 0, "measures": 0, "submissions": 0, "accepted": 0, "spawns": 0,
 "agent_cost_usd": 0.0, "agent_tokens_in": 0, "agent_tokens_out": 0, "wall_s": 0.0,
 "harness_nodes": 0, "harness_bytes": 0, "stage4_calls": 1, "verifications_for_spec": 1,
 "notes": [{"chars": 0, "sha256": "<hex>"}],
 "children": [{"name": "x", "artifact_digest": "<hex>", "case_origin": "authored", "licensed": true}]}
```

## 9. Forge tools: exact contracts

**Common envelope.** Every result is a JSON object with `"ok": bool` and `"code": <Code>|null`, followed by the tool fields, then:

```json
"left": {"usd": 0.0, "seconds": 0.0, "checks": 0, "dryruns": 0, "measures": 0, "submissions": 0, "spawns": 0},
"truncated": false
```

Callbacks always read the draft from `<workspace>/harness.py`, at most 64 KiB (`forge_file_too_large` otherwise), through trusted code. The source is never taken from arguments. Per-case lists are truncated from the end so the result fits in 16,384 characters, with `"truncated": true`. A candidate artifact is built in the forge-private `candidates/` store as `HarnessArtifact(execution_mode="sandbox", forge=ForgeBinding(language=2, call_limit=<computed per 4.9>, fuel=settings.fuel, subharnesses=<session children referenced by the source>), ...)` with the Stage 2 best's spec, preset, params, components and model.

### 9.1 `forge_check` ($0)

Arguments:

```json
{"type":"object","additionalProperties":false,
 "properties":{"path":{"type":"string","pattern":"^(harness|drafts/[a-z][a-z0-9_]{0,31})\\.py$"}}}
```

`path` defaults to `harness.py`. Result fields:

```json
{"line": null, "col": null, "rule": "<RULES key or null>", "message": "<fixed sentences or null>",
 "refusals": [{"line": 3, "code": "sandbox_import", "rule": "import_module",
               "message": "<fixed sentences>"}],
 "refusals_total": 1,
 "stats": {"nodes": 0, "node_limit": 8000, "depth": 0, "helpers": 0, "constants": 0,
           "loops": 0, "await_sites": 0, "bytes": 0},
 "lint": ["forge_literal_leak"], "unbound_children": ["name"], "language": 2}
```

`lint` is advisory here and enforced at submit. The cap is `max_checks`.

A refusal names its `rule` (a key of `sandbox_language.RULES`) and a `message`: "Line N: "
and then one or two fixed sentences saying what broke and the form that is allowed, from
refusal_message in sandbox_language.py. `col` is set for a syntax error. A message is built only from fixed
tables (`RULES`, `ALTERNATIVES`, `HINTS`, `REFUSAL_TEXTS`) and the line number. It names an
identifier only when it is one of the language's own (a builtin, a listed method, a reserved
name, a key of the binding) or a key of `ALTERNATIVES` or `MODULES` that the source used
exactly, so no source text is ever echoed. Forge pack v2 replaced the earlier `hint` field
(the code's one generic line) with `rule` and `message`.

Since Forge pack v3 a refusal also lists every refused line the check found (4.15):
`refusals`, by line, each with its `line`, `code`, `rule` and `message`, and
`refusals_total`. The top-level fields are the first entry's. At most `REFUSALS_SHOWN` (20)
are listed: each kind of refusal (code, rule and name) once first, then the rest by line, and
a kind already listed says only `Line N: the same problem as line M.` A passing check returns
`refusals: []` and `refusals_total: 0`.

### 9.2 `forge_dryrun` ($0)

Arguments:

```json
{"type":"object","additionalProperties":false,"required":["case"],
 "properties":{"case":{"type":"integer","minimum":0},
  "replies":{"type":"array","maxItems":8,"items":{"type":"string","maxLength":2000}},
  "game_steps":{"type":"integer","minimum":1,"maximum":20}}}
```

`case < len(floor.tuning)` (`invalid_case`). `replies` defaults to `["{}"]`. `game_steps` defaults to 5. Result fields are the `DryRun` fields (6.4) plus `sandbox_code`, `rule` and `message`. Nothing is scored, nothing is spent, and no telemetry cost is recorded. Codes: `forge_invalid_source` (with `line`), `forge_dryrun_unavailable`, `invalid_case`, and a failed run's own code. A source refusal also carries its sandbox code, rule and the same message forge_check gives, with the same `refusals` and `refusals_total` (as do `forge_measure` and `forge_submit`); a failed run's message is its runtime code's fixed hint at its line, and a run whose output misses the output schema gets a message too. The cap is `max_dryruns`.

### 9.3 `forge_measure` (spends play-model dollars from the forge wallet)

Arguments:

```json
{"type":"object","additionalProperties":false,
 "properties":{"cases":{"type":"array","maxItems":32,"uniqueItems":true,"items":{"type":"integer","minimum":0}},
  "smoke":{"type":"boolean"},"trace":{"type":"boolean"}}}
```

`cases` defaults to the first `measure_cases` indexes. `smoke` (default false) measures smoke-floor tuning instead. `trace` (default false) applies to games only. Result fields:

```json
{"line": null, "sandbox_code": null, "rule": null, "message": null,
 "measurement": {"score": 0.0, "floor_limit": 0.0, "floor_passed": false, "budget_passed": true,
                 "mean_cost_usd": 0.0, "mean_tokens": 0.0, "mean_calls": 0.0, "max_compute_s": 0.0,
                 "sample_count": 0, "failures": ["<evaluate_cases code>"]},
 "per_case": "available",
 "cases": [{"index": 0, "passed": true, "score": 1.0, "failure": null, "sandbox_line": null,
            "calls": 2, "cost_usd": 0.0012, "output_excerpt": "...", "expected_excerpt": "...",
            "game": null}],
 "baseline": {"score": 0.0, "mean_cost_usd": 0.0, "floor_passed": false}}
```

- `per_case` is one of `"available" | "unavailable" | "aggregate"`.
- Excerpts are at most 600 characters; `game` is a `GameDetail` or null.
- A validation failure costs $0 and returns `forge_invalid_source` with `line`.
- `mean_calls` is computed from the `CaseDetail`s, not from `Measurement`.
- A partial measure never qualifies for acceptance.
- Codes: `invalid_case`, `forge_measure_too_small`, `forge_invalid_source`, `budget_exceeded` (the session then ends).
- The cap is `max_measures`.

### 9.4 `forge_submit`

Arguments:

```json
{"type":"object","additionalProperties":false,
 "properties":{"note":{"type":"string","maxLength":200}}}
```

Gate order:
1. validate;
2. lint;
3. child binding check;
4. write the candidate into `candidates/<n>/` (harness.py plus referenced children copied into `subharnesses/`);
5. `evaluate_cases` on all `floor.tuning`;
6. `evaluate_cases` on `smoke_floor.tuning` if present;
7. rank (8.8).

Result fields:

```json
{"sandbox_code": null, "line": null, "rule": null, "message": null,
 "candidate": 1, "accepted": false, "best_so_far": false,
 "measurement": {"...": "same aggregate fields as forge_measure"},
 "smoke": {"floor_passed": true, "score": 1.0},
 "submissions_left": 5}
```

- `smoke` may instead be the string `"absent"`.
- Every refusal carries a `message` (and, for `forge_invalid_source`, the `rule`).
- `code` is one of: `null`, `forge_invalid_source`, `forge_literal_leak`, `forge_unbound_child`, `forge_unused_child`, `tuning_failed`, `smoke_failed`, `not_better`, `persistence_failed`, `forge_submissions_exhausted`.
- The note is stored only as `{chars, sha256}` in `forge.json`.
- The cap is `max_submissions` attempts. No Stage 4 or held-out number is ever returned.

### 9.5 `forge_spawn`

Arguments:

```json
{"type":"object","additionalProperties":false,
 "required":["name","goal","input_schema","output_schema","cases"],
 "properties":{
  "name":{"type":"string","pattern":"^[a-z][a-z0-9_]{0,31}$"},
  "goal":{"type":"string","maxLength":2000},
  "input_schema":{"type":"object"}, "output_schema":{"type":"object"},
  "cases":{"oneOf":[
    {"type":"object","additionalProperties":false,"required":["projection"],
     "properties":{"projection":{"type":"object","additionalProperties":false,"required":["input","expected"],
       "properties":{"input":{"type":"string","maxLength":256},"expected":{"type":"string","maxLength":256}}}}},
    {"type":"object","additionalProperties":false,"required":["authored"],
     "properties":{"authored":{"type":"array","minItems":8,"maxItems":64,
       "items":{"type":"object","additionalProperties":false,"required":["input","expected"],
                "properties":{"input":{},"expected":{}}}}}}]},
  "floor_limit":{"type":"number","minimum":0,"maximum":1},
  "preset_hint":{"type":"string"},
  "max_usd":{"type":"number","exclusiveMinimum":0}}}
```

Projection values are JSON Pointers. Both schemas must be valid Draft 2020-12 and at most 16 KiB each (`invalid_schema`). The name must be unique in the session (`forge_spawn_name_taken`). Result fields:

```json
{"name": "x", "licensed": true, "preset": "<name>", "execution_mode": "native",
 "tuning": {"score": 0.0, "floor_passed": true}, "verified": {"score": 0.0, "sample_count": 0},
 "case_origin": "authored", "cost_usd": 0.0,
 "call_as": "await context.harness(\"x\", value)", "spawns_left": 2}
```

`verified` is the child's own Stage 4 on data the agent supplied or already sees (Section 11). It may be null. `code` is one of: `null`, `forge_depth`, `forge_spawn_cap`, `forge_spawn_name_taken`, `invalid_schema`, `invalid_cases`, `spawn_kind_unsupported`, `compile_failed`, `not_licensed`, `budget_exceeded`. The caps are `max_spawns` and `max_depth`.

### 9.6 Caps summary

| Tool | Cap | Money |
|---|---|---|
| `forge_check` | `max_checks` (200) | $0 |
| `forge_dryrun` | `max_dryruns` (100) | $0 |
| `forge_measure` | `max_measures` (24) | play-model spend, forge wallet |
| `forge_submit` | `max_submissions` (6) | play-model spend, forge wallet |
| `forge_spawn` | `max_spawns` (3), `max_depth` (1) | child compile, nested wallet |

Session-wide limits: results at most 16,384 characters; Pi `max_turns` 200; context thinning at 60,000 characters.

## 10. Feedback and leakage rules

1. **What the agent sees:**
   - SPEC.md;
   - tuning inputs and, in `per_case` mode, tuning expected values (truncated);
   - tuning game ids and observations;
   - its own dry-run shapes;
   - fixed codes with line numbers;
   - aggregate and per-case tuning results;
   - the remaining budget;
   - child tuning and child-own verification results.

   Showing tuning labels is a deliberate DECISIONS entry scoped to Forge. `test_prompt_omits_labels_metadata_and_failure_text` keeps holding for `rewrite()`. `feedback="aggregate"` restores Stage 3's no-labels behavior.
2. **What the agent never sees:**
   - held-out cases, ids, game ids or counts;
   - the parent's Stage 4 score, failures, report or `verification_id`;
   - dataset paths;
   - floor or adapter objects;
   - exception text;
   - case metadata fields.
3. **Literal-leak lint** (`compiler/forge_lint.py`). `def leaked_literals(source: str, tuning: Sequence[EvaluationCase]) -> tuple[int, ...]` returns the source line numbers of every string constant, and every literal part of an f-string, of 24 or more characters that equals or contains:
   - any str leaf of length 24 or more in any tuning `expected` or `input`, or
   - the canonical JSON of any tuning `expected` of length 24 or more.

   `forge_submit` rejects a non-empty result with `forge_literal_leak`. Tuning-derived few-shot examples are therefore not allowed in Phase 9 (non-goal).
4. **Tool code touches `floor.tuning` only.** `measure_cases` and `dry_run_case` go through the floor's tuning validation, so any other case raises `HeldOutAccessError`.
5. **Pi confinement** is inherited: `repository` = workspace, cwd = workspace, no shell, scrubbed environment, and `denied_paths` as in 8.3.
6. **Child datasets** are built only from parent tuning or agent-authored data (Section 11). The parent's held-out set can never reach a child floor, so a child's license bit carries no information about the parent's held-out set.
7. **Adaptive overfitting across compiles** is bounded by `max_verifications_per_spec`. The CLI and session print the per-spec verification count.

## 11. Sub-harnesses

**Spawn.** `forge_spawn` is implemented in `compiler/forge_tools.py` and uses a nested `Compiler`.

1. **Child spec.**
   - Built by trusted code without Stage 0.
   - `domain` and `model_target` come from the parent; `task = goal`; `input_schema` and `output_schema` come from the arguments.
   - `execution_config` is standard. Game children are refused with `spawn_kind_unsupported`.
   - `tools = ()`: a child never inherits the parent's tool grant.
   - `lifecycle` is persistent.
   - `floor = FloorDefinition(kind=held_out_tasks, limit=floor_limit or 0.8, dataset=<JSONL in the forge-private spawn_datasets/>)` with `minimum_samples = n // 2`.
   - `budget = min(parent.spec.budget, the spawn wallet)`.
   - The description records `case_origin`.
2. **Cases.**
   - `projection` requires a standard-kind parent. For every parent tuning case, it resolves both pointers against `case.input` and `case.expected`, drops cases where either is missing, and requires at least 8 cases (`invalid_cases`).
   - `authored` cases are validated against both schemas.
   - The child's own even/odd split holds out its odd half. The child license therefore means "meets its stated contract on unseen cases drawn from parent tuning or agent-authored data". That is weaker than a parent license, and it is recorded in `SubharnessRef.case_origin`, `forge.json` and the docs.
3. **Compile.**
   - `Compiler(config=CompileConfig(model_ladder=(parent_model,), allowed_presets=parent allowed, enable_rewriting=False, max_rewrites=0, forge=settings.child_settings(depth + 1)))`, sharing `artifact_root` (under the forge-private dir), `runtime`, `adapter_factory` and a `floor_factory` over the temp JSONL.
   - It runs inside `compilation_wallet(Budget(cost_usd=min(max_usd, spawn_max_usd, spawn_share * remaining_usd), compute_s=spawn_share * remaining_s))`, nested under the forge wallet. Every child dollar and second is also debited to the forge and outer wallets.
   - Stages 1, 2 and 4 run. Forge runs in the child only if `depth + 1 < max_depth`; with the default `max_depth = 1`, a child can neither forge nor spawn.
   - The child's Stage 4 is the normal `verify()`.
   - Child telemetry rows carry the stage-listener tag `forge_spawn`.
4. **On license,** the child is kept in the session registry `{name: artifact}`. It is not installed and not stored in the library. An unlicensed child cannot be referenced (`forge_unbound_child` at submit; `sandbox_binding_mismatch` at validation).
5. **Binding.**
   - `forge_submit` copies only the referenced children into `candidates/<n>/subharnesses/<name>/` and sets `SubharnessRef` from each child.
   - Promotion copies the tree into the version directory.
   - At play time, `runtime/subharness.py` (6.3) is the only execution path.
6. **Depth.** At build time, `max_depth` (at most 2). At run time, nesting is structurally bounded by the directory tree, and the lineage check refuses depth above 4.

## 12. Surfaces

### 12.1 Profile

- `Profile.forge_agent: NamedAdapterConfig | None = None`, mirroring `Profile.rewrite` (`profiles.py:143`). It resolves `ForgeSettings.agent_model` when set.
- `profile.compile.forge` holds `ForgeSettings`.
- `build_services` passes `Compiler(forge_adapter=..., pi_config=<effective_pi>)` and `smoke_factory` as today.
- Unknown `agent_model` raises `ProfileError("forge_model_unknown")`.

### 12.2 CLI (`architect/cli.py`)

- `compile`, `do`, `create` and `recompile` gain the flags below. They override `profile.compile.forge` for this invocation only.

  ```
  --forge  --forge-usd X  --forge-minutes M  --forge-model NAME
  --forge-trigger always|failed  --forge-mode agent|template  --yes
  ```

  With `--forge`, before any spend, the CLI prints exactly `forge: compile cap $A + forge cap $B = $C (API-equivalent, not a bill); forge minutes M; verifications for this spec so far: K/N`. It then asks `Continue? [y/N]` unless `--yes` is given. A non-interactive stdin without `--yes` gives `forge_cancelled` with nothing spent.
- `architect forge report <artifact-dir> [--json]` prints `forge.json` as an ASCII table, at most 100 columns, or the raw JSON with `--json`. A missing `forge.json` gives `forge_report_missing`.
- `architect bench run --variant default|noisy|images-all-stages|forge|template` is repeatable. It also accepts `--forge-usd X --forge-minutes M --forge-model NAME`.
- `architect bench compare A B --pair-variants BASE:CAND` treats rows of variant BASE in A and CAND in B as the same key. A and B may be the same file.
- `--max-usd` includes forge caps up front: `_size_cell` adds `forge.max_usd` per forge cell, and $0 per template cell beyond the normal compile.

### 12.3 Session verb

```
forge on | forge off | forge status | forge budget <usd> <minutes> | forge model <name> | forge mode agent|template
```

The verb edits the session's `CompileConfig.forge`, never the Spec, so the spec hash is unchanged. `forge on` shows the summed-cap line from 12.2 and uses the existing session confirmation. Compile progress shows a `forge` stage line: `forge  checks C  dryruns D  measures M/Mx  submits S/Sx  spawns P  $spent/$cap (API-equivalent)`. It never shows source, prompts or case content.

### 12.4 Fixed error codes

| Surface | Codes |
|---|---|
| Session and CLI | `forge_disabled`, `forge_budget_invalid`, `forge_model_unknown`, `forge_cancelled`, `forge_report_missing`, `forge_verification_cap`, `host_memory_low` |
| Compile failures | `forge_no_submission`, `forge_verification_cap`, `forge_failed` |
| Tools | Section 9, plus `invalid_arguments`, `forge_tool_cap`, `tool_failed`, `result_too_large`, `forge_file_too_large`, `invalid_case`, `forge_measure_too_small`, `forge_dryrun_unavailable` |
| Sandbox | Section 4.10 |
| Sub-harness | `subharness_unknown`, `subharness_digest_mismatch`, `subharness_unlicensed`, `subharness_schema_error`, `subharness_depth` |

Each code gets one fixed ASCII sentence in `session/codes.py` or `bench.CODES`.

## 13. Bench integration (WP2)

- **`rows.py`.** `Variant = Literal["default", "noisy", "images-all-stages", "forge", "template"]`.
  - `ForgeFields(_Frozen)` holds `{mode, agent_model: ModelId | None, checks, dryruns, measures, submissions, accepted, spawns, agent_cost_usd, agent_tokens_in, agent_tokens_out, agent_wall_s, harness_nodes, harness_bytes, subharnesses, language: Literal[2]}`.
  - `BenchRow.forge: ForgeFields | None = None`.
  - `ArcFields` gains `distinct_states_mean: float | None = None`, `revisit_rate_mean: float | None = None`, `levels_completed_total: int | None = None` and `calls_per_action: float | None = None`. Old rows stay readable.
  - `cost_usd` remains the play-time and delivery cost; build-time agent cost goes only in `forge.agent_cost_usd`.
- **`suite.py`.** `BenchTask.supports` returns True for `forge` and `template` on every task. Forge and template cells use the same spec and seed, so `spec_hash` is identical.
- **`runner.py`.**
  - `BenchConfig.forge: ForgeSettings | None = None`. When a forge or template variant is requested, the default is `ForgeSettings(enabled=True, trigger="always", mode=<variant>, ...)` with the `--forge-*` overrides.
  - `_text_cell`, `_local_games_cell` and `_preview_cell` pass `CompileConfig.forge` (`enable_rewriting` stays False) and set `GameFactoryConfig.allow_forge=True` only for forge and template cells.
  - ARC process metrics come from `GameBackend.last_counters` for every arm.
  - **Guard:** a forge row whose `forge.measures == 0` and a template row whose `forge.submissions == 0` are written with failure `forge_did_not_run`, and `bench run` exits non-zero. Forge must never silently not run.
- **`oracle.py`.** `class ForgeScript(ModelAdapter)`, used with `provider=fake`, replays a committed Pi `{text, tool_calls}` script from `examples/forge/scripts/<task_id>.json`: write `harness.py` (a committed v2 source), `forge_check`, `forge_measure` (with `trace` for games), `forge_submit`, final text. It is $0 and deterministic.
- **`compare.py`.** `compare(a, b, *, allow_fingerprint_mismatch=False, expect=None, pair_variants: tuple[Variant, Variant] | None = None)`. When given, `_key` (`:135`) ignores the variant, and rows are filtered to BASE in `a` and CAND in `b`.
- **`render.py`.** Adds columns `meas`, `subs`, `agent$`, `dist`, `revis`, each at most 7 characters, keeping 100 columns.
- **CI** (`bench-smoke` job): adds `--variant forge --variant template` on the smoke tier at $0. WP5 regenerates `examples/bench/suite-v1/smoke-baseline.jsonl` on the Linux runner.

## 14. ARC experiment

**Hypothesis.** The preset loses on ARC-AGI-3 because the model holds state in a prompt and oscillates. A Forge or template harness moves exploration state into code and asks the model only for hypotheses when stuck. For the same local play model, that should cut revisits and raise distinct states and levels.

**Host rules** (12 GB GPU, 31 GB RAM; PROGRESS.md:117 records the gemma4:26b at `num_ctx` 65536 memory stop):
- `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`;
- one bench cell at a time;
- play at `num_ctx` 16384;
- the `architect bench probe` preflight records Ollama `/api/ps` `size_vram` and host free RAM;
- forge and template game cells refuse with `host_memory_low` when free RAM is under 6 GB.

**Stage A: $0 CI proof** (fake provider, fixture play agent, `arc-local-3` smoke):

```
architect bench run --tier smoke --provider fake --variant default --variant template --variant forge
```

Pass criteria, all required:
- the forge and template rows are licensed by Stage 4;
- `forge.submissions >= 1` and `forge.measures >= 1`;
- the stages include 3;
- `distinct_states_mean(forge) > distinct_states_mean(default)` with the same fixture agent, which shows the code, not the model, supplies the exploration;
- the step count is at most `max_actions` on every row;
- the adversarial leak test (15.4) passes over every tool result of the run.

**Stage B: primary.** Cloud build agent, local play.
- Forge agent: the logged-in Claude CLI (Sonnet-class, API-equivalent), `--forge-usd 5 --forge-minutes 60` per cell. It uses no GPU.
- Play model: `granite4.2:8b`, `num_ctx` 16384, `think false`.
- `examples/local/profile-forge.json` sets this. `allow_online_model` is false for play.
- Arms, all with seed 7, the same games and `--arc-actions 80`: `default` (explorer preset, strategy v2), `template`, `forge`.

```
architect bench run --tier full --provider profile --profile examples/local/profile-forge.json --models granite4.2:8b --variant default --variant template --variant forge --arc-actions 80 --forge-usd 5 --forge-minutes 60 --repeats 3
architect bench run --tier arc  --provider profile --profile examples/local/profile-forge.json --models granite4.2:8b --variant default --variant template --variant forge --arc-actions 80 --arc-games 5 --forge-usd 5 --forge-minutes 60
architect bench compare <run> <run> --pair-variants default:forge
architect bench compare <run> <run> --pair-variants template:forge
```

Pre-registered metrics:
- **Primary:** `levels_completed_total` and normalised score per game, with McNemar exact on per-game "any level completed" bits (existing `compare.py`).
- **Process metrics**, required even if every arm scores 0:
  - `revisit_rate_mean(forge) <= 0.5 × default`;
  - `distinct_states_mean(forge) >= 2 × default`;
  - `calls_per_action(forge) <= 0.5` (the preset is at least 1).

**Success** requires the process targets to be met, together with either of:
- forge beats default on `levels_completed_total` for `arc-local-3` with p < 0.05 over all paired (instance, repeat) cells; or
- a strictly positive preview score where default is 0.0.

The `template:forge` comparison is reported separately as "agent contribution over the committed code". It is not part of the success rule.

**Stage C: stretch, all local.** Agent and play model are both `gemma4:26b` at `num_ctx` 32768, one loaded instance. It runs only if the probe shows the model resident with at least 6 GB of RAM free. Otherwise `granite4.2:8b` serves as both agent and play model, and `agent_model` is recorded in `ForgeFields`. Local tool-envelope compliance is reported as a measured number (checks, measures and submissions per session), not a claim.

**Failure analysis** uses only tuning-side `forge_measure` traces and bench rows. **Evidence** goes to `examples/telemetry/arc-forge-evidence.json`: content-free rows plus the compare verdicts, labelled `local-offline-preview, not a benchmark or compliance claim`. PROGRESS.md gets the run note. Stages B and C need the operator's approval to run.

**Implementation note (2026-09-28).** On the public ARC path Forge runs only on the child game compiles, never on the parent competition compile: the parent compiles the delegation planner, and each parent tuning measure is itself a batch of child compiles. Each child gets a fresh Forge agent and a smoke floor from support-tuning games only.

## 15. Tests per package

All tests are offline, using `FakeAdapter`, `ScriptedAdapter` and oracles. Pi tests use the real SDK, as `tests/test_pi.py::test_licensed_compile_is_actual_extension_tool` does.

### 15.1 WP1

Files: `tests/test_sandbox_v2.py` (new), `tests/test_grids.py` (new), `tests/test_sandbox.py` (edits), `tests/adversarial/test_phase9_sandbox.py` (new).

- **Validator matrix, accept and reject, for every construct in 4.3 and 4.4.** Rejections include:
  - `break` outside a loop;
  - a subscript assign rooted at `task` or a constant;
  - direct and mutual recursion (`sandbox_recursion`);
  - a sync helper with `await`, or mentioning `context`;
  - an async helper whose first parameter is not `context`;
  - attributes other than `context.<cap>`;
  - a method call (with its hint);
  - an f-string with a `format_spec` or conversion;
  - `is` with a non-None comparator;
  - `try` with `as`, `else` or `finally`;
  - comprehensions;
  - loop nesting 7;
  - `context.observe` on a non-game spec (`sandbox_capability`);
  - `context.harness` with an unbound name (`sandbox_binding_mismatch`);
  - a v2 construct under a v1 binding (`sandbox_language`).
- **v1 frozen.** Every fixture in `tests/fixtures/rewrites` and every preset emit validates, runs identically and keeps its `artifact_digest`. v1 `int + int` still raises `sandbox_add`, and v1 keeps the 64 static await cap.
- **Semantics.**
  - value-semantics copy (`append(l, l)` terminates, and the original is unchanged);
  - `while True:` yields `sandbox_fuel` within bounded wall time;
  - `try` catches `sandbox_key` but not `sandbox_fuel` or `sandbox_invalid_call`;
  - `sandbox_unbound`;
  - `str * int` yields `sandbox_type`;
  - 2**63 overflow;
  - division by zero;
  - error line numbers;
  - `parse_json` with duplicate keys returns None;
  - `extract_json` on fenced and prose-wrapped text;
  - `digest` is stable across `PYTHONHASHSEED` values;
  - the 64x64 `grid_diff`, `objects` and `grid_text` each run in under 50 ms;
  - `grids.grid_diff` is identical to the old `_grid_diff` on committed fixtures.
- **Capabilities.**
  - `images=` and `system=` round-trip through the real subprocess, and an unknown sha gives `sandbox_invalid_call`;
  - the dynamic cap: 129 calls in a loop with `call_limit` 128 fail; `observe` does not count; `step` does; a game artifact uses `max_turns * calls_per_turn`.
- **Memory.** Building a list of large strings gives `sandbox_memory` on both Windows (Job Object) and POSIX (`RLIMIT_AS`).
- **LANGUAGE.md** golden file.
- **Adversarial.**
  - big-dict allocation in a loop;
  - deep nesting via subscript assign;
  - a huge f-string;
  - `replace` blow-up;
  - a 100M-fuel run is killed by the host deadline;
  - a 10M-fuel run finishes in under 60 s on the CI runner.

### 15.2 WP2

Files: `tests/test_pi_capabilities.py` (new), `tests/test_pi.py` (compatibility only), `tests/test_cli_forge.py`, `tests/test_session_forge.py`, `tests/test_bench_forge.py` (new), and edits to `tests/test_bench_compare.py` and `test_bench_rows.py`.

- **Pi capabilities.**
  - A `TrustedTool` is registered through `extension.ts` and actually invoked by Pi 0.73.1 from a `FakeAdapter` `tool_calls` envelope.
  - TypeBox rejects extra keys before any Python RPC.
  - A Python schema mismatch gives a soft `invalid_arguments`.
  - An oversized result gives `result_too_large`, or a truncated result, and never a protocol-limit `PiError`.
  - `forge_tool_cap`.
  - A forged ungranted capability RPC is a fatal `PiError`.
  - Capabilities are dropped when `is_build` is False, under `Runtime.evaluate` and under `Runtime.run`.
  - `compile_agent` tests are unchanged and green.
  - A swallowed `BudgetExceeded` in a callback stops the same batch.
- **CLI and session.**
  - flag parsing and overrides;
  - the summed-cap line and `forge_cancelled` without `--yes`;
  - `forge report` table and `--json`;
  - the session verb and its codes;
  - the progress line never contains source or case text.
- **Bench.**
  - forge and template rows carry `ForgeFields`;
  - `pair_variants` alignment, including within one file;
  - the up-front `--max-usd` bound includes the forge caps;
  - the `forge_did_not_run` guard;
  - ARC process metrics on default rows;
  - the smoke tier with `--variant forge --variant template` at $0 is deterministic across two runs.

### 15.3 WP3

Files: `tests/test_forge_binding.py`, `tests/test_subharness.py`, `tests/test_game_turns.py`, `tests/test_measure_cases.py` (new), and edits to `tests/test_library.py`, `test_game_factory.py` and `test_game_execution.py`.

- **Binding.** Golden digests hold for `forge=None`. Changing the binding invalidates the license. Flipping a child byte gives `subharness_digest_mismatch` at call time.
- **Sub-harness.** Child input and output schema checks; `fork`/`absorb` debits the parent wallet; the lineage depth cap; a model mismatch is refused.
- **Game turns.** `_Play` has parity with `run()`:
  - identical offered actions, RESET withholding, `max_actions`, `task_key` mismatch and score source checks;
  - `run()` and a scripted `observe`/`step` sequence on local games give identical finish scores;
  - a step after a terminal state returns `refused: true` and nothing is sent;
  - a second play in the same `run_id` is refused;
  - `scored=False` never calls the score path;
  - counters are correct on fixtures.
- **`measure_cases`.** Refuses verification cases (`HeldOutAccessError`); returns per-case detail only for tuning; `per_case` is unavailable for `HUMAN_EDIT_RATE`; `forge_measure_too_small`.
- **`dry_run_case`.** Spends $0; its telemetry cost is 0; offline game steps are unscored.
- **Library and factory.**
  - the library snapshot copies `subharnesses/`;
  - `verification_count` persists;
  - without `allow_forge`, the factory still rejects with `game_sandbox_forbidden`; with it, a sandbox artifact must carry `forge`;
  - `_SafeLibrary` still rejects sandbox warm starts.

### 15.4 WP4

Files: `tests/test_forge.py`, `tests/test_forge_tools.py`, `tests/test_forge_templates.py`, `tests/test_forge_lint.py` (new), `tests/adversarial/test_phase9_forge.py` (new).

- **End to end** with the real Pi SDK and a scripted `FakeAdapter`:
  - a quality repair that costs more is accepted by Forge where `rewrite()` rejects it (the scratch `probe_quality_repair` fixture);
  - the promoted candidate passes Stage 4, is licensed and stored, and a warm start re-verifies;
  - `trigger="failed"` skips when tuning passed; `"always"` runs.
- **Budget.** `forge_usd` exhaustion mid-session ends cleanly with the best submission. Token counters keep growing with no cap. The outer wallet equals the compile budget plus the forge budget.
- **Leakage.** Every held-out case is fingerprinted, and every tool-result JSON, every workspace file and `forge.json` is grepped for them. The same check covers held-out game ids. The Pi workspace cannot read the floor dataset, `candidates/` or `spawn_datasets/`.
- **Spawn.** A projection child is built from tuning only (a fingerprint check shows no parent held-out case in the child JSONL). Spawning at `max_depth` gives `forge_depth`. An unlicensed child is unbindable. A referenced child is copied into the version directory.
- **Lint.** Accepts short literals and rejects literals of 24 or more characters matching tuning expected or input values.
- **Templates.** Each instantiates and validates for every spec kind it targets.
- **Template mode** runs without Pi and records `mode: "template"`.
- **Verification cap.** `forge_verification_cap` blocks the session.
- **`forge.json`.** The key set is exact and content-free.
- **Boundary.** Only `forge.py` calls `Runtime.build`, and no `forge*` module imports the verifier token.

## 16. Honesty rules

- Tuning, dry-run and child-verification numbers are never presented as a license or a held-out score. Only the parent's Stage 4 is a license. `forge report` prints `tuning evidence, not a license` above tuning columns.
- Child licenses are always shown with `case_origin`, and the docs state that they are weaker than a parent license.
- Build-time agent cost is reported separately from play-time cost and is never folded into `cost_usd`. CLI-provider dollars are API-equivalent.
- A row where Forge did not run is marked `forge_did_not_run` and never reported as a Forge result.
- ARC evidence is labelled "local-offline-preview, not a benchmark or compliance claim". 0.0-vs-0.0 results are reported with their process metrics, never omitted.
- The `template:forge` gap is reported as the agent's contribution. The `default:template` gap is attributed to committed code, not to the agent.
- Local-agent compliance (Stage C) is reported as counts, not as "works with any model", until measured.

## 17. Non-goals

- Comprehensions, method-call syntax, `raise`, recursion and closures in the sandbox (candidates for a later v2.1 with its own adversarial round).
- Tuning-derived few-shot literals in harness code.
- Game-kind children, and children with tools.
- Showing any held-out, Stage 4 or verification result to an agent. Auto-iterating compiles on Stage 4 feedback.
- Token caps.
- Installing children into the library as standalone artifacts.
- Migrating Pi off the pinned 0.73.1 package (it is marked deprecated upstream in favour of `@earendil-works/pi-coding-agent`; a migration is a separate phase).
- Online (gateway) ARC play in CI. Submitting to ARC Prize.
- Changing Stage 1/2 presets, strategy v2, or the `rewrite()` acceptance rule.

## 18. Work packages

WP1-WP4 run in parallel on disjoint files after the day-0 model commits (Section 3). Each WP codes against this contract, and where a dependency has not landed it uses a local fake confined to its own tests.

**WP1: Sandbox language v2.**
- Owns `runtime/sandbox.py`, `_sandbox_worker.py`, `_sandbox_builtins.py`, `sandbox_language.py`, `_proc_limits.py`, `games/grids.py`, and the tests in 15.1.
- Calls `RunContext.game_observe`, `game_step` and `subharness`, and reads `artifact.forge`, through the day-0 WP3 models. Until WP3's methods land it uses a stub `RunContext` in tests.
- Deliverables: grammar, interpreter, builtins, grid module, fuel, memory caps, error frames, dynamic cap, LANGUAGE.md with a golden test; v1 digests and behavior unchanged.

**WP2: Pi trusted tools and surfaces.**
- Owns `backends/pi.py` (except the `_proc_limits` switch), `pi-node/extension.ts`, `bridge.mjs`, `profiles.py`, `cli.py`, `session/forge.py` plus the session edits, `bench/{rows,suite,runner,compare,render,oracle}.py`, and the tests in 15.2.
- Reads `context.is_build` via `getattr(context, "is_build", False)` until WP3 lands.
- Uses `ForgeSettings` from day-0 WP4. Calls `forge()` only through `Compiler`.

**WP3: Host runtime, binding and games.**
- Owns `spec/__init__.py`, `runtime/__init__.py`, `runtime/subharness.py`, `runtime/evaluation.py`, `models/fake.py` (`ScriptedAdapter`), `games/execution.py` (the `_Play` refactor, `open_turns`, counters, the `grid_diff` import from `games/grids.py`), `games/factory.py`, `library/__init__.py`, and the tests in 15.3.
- The `_Play` parity tests must be green before the sandbox `observe`/`step` path is enabled.

**WP4: Forge driver.**
- Owns `compiler/forge_settings.py`, `forge.py`, `forge_tools.py`, `forge_workspace.py`, `forge_lint.py`, `forge_templates/`, `compiler/types.py`, `compiler/__init__.py`, `examples/forge/`, `examples/local/profile-forge.json`, and the tests in 15.4.
- Uses WP1's `validate_source`, `SourceStats` and `language_markdown`, WP3's `measure_cases`, `dry_run_case` and `Runtime.build`, and WP2's `TrustedTool`, all as contracted.

**WP5: Integration** (sequential, after WP1-WP4).
1. Replace stubs with real imports. Switch `backends/pi.py` to `_proc_limits.contain`. Wire `Compiler(forge_adapter, pi_config)` in `build_services` and the bench providers.
2. Extend `tests/test_package_boundary.py` (Section 3).
3. Write `docs/interfaces/phase9-forge.md` (this contract) and these DECISIONS entries:
   - language v2 supersedes the phase4 loop ban;
   - removal of the static await count for v2;
   - reversal of the `images=` rejection for v2;
   - Forge label visibility;
   - the Forge acceptance rank;
   - child license semantics (`case_origin`);
   - `allow_forge`;
   - raising the outer wallet by the Forge budget;
   - the literal-leak lint.
4. Update `docs/usage.md`, `docs/bench.md` and `docs/limitations.md`.
5. Run the full suite, `uv run ruff check .` and `uv run mypy --strict architect`, with 100% branch coverage on `sandbox.py` v2 paths, `_sandbox_builtins.py`, `subharness.py` and the authority branches of `forge_tools.py` (the Phase 4 bar).
6. Regenerate `examples/bench/suite-v1/smoke-baseline.jsonl` with the forge and template variants on the Linux CI runner. Add the forge and template steps to `bench-smoke`. Make sure the Windows job runs the memory-cap tests.
7. Run ARC Stage A in CI. Run Stages B and C locally with operator approval (one model loaded, memory preflight). Record `examples/telemetry/arc-forge-evidence.json` and the PROGRESS.md run note.

## 19. Risks bounded by this contract

| Risk | Bound |
|---|---|
| Wider language means more DoS surface | Fuel, construction limits, value semantics, the OS memory cap and the unchanged deadline; an adversarial round like Phase 4's |
| `RLIMIT_AS` counts virtual memory | 1 GiB POSIX cap; tests on both CI runners |
| Digest breakage from `HarnessArtifact.forge` | Omitted from `_config` when None; golden-digest tests |
| `_Play` refactor of the safety-critical game loop | Parity tests gate enabling `observe`/`step` |
| Tuning overfitting, since labels are shown | Literal-leak lint, Stage 4 held-out, `feedback="aggregate"` option |
| Adaptive held-out overfitting across compiles | One verify per session, per-spec counter and cap, never shown to the agent |
| Weak child licenses | `case_origin` recorded everywhere; the parent's Stage 4 is the only task claim |
| Local agent-model envelope and grammar compliance | Free check and dry-run, templates as the first draft, fixed hints; the primary ARC arm uses a cloud agent |
| Host memory | One model loaded, 16k play context, `host_memory_low` preflight |
| Pi context and token volume | `context_policy` thinning plus the dollar cap; tokens reported in `ForgeFields` |
| Interpreter speed on grids | Native grid builtins; the measured explorer uses about 300 nodes per turn |
| Forge silently not running (`trigger="failed"`, preview floor 0) | Bench and ARC use `trigger="always"`; `forge_did_not_run` guard |
| Pi 0.73.1 deprecated upstream | Pin kept; migration out of scope |

## Phase 10 amendments

`docs/interfaces/phase10-router.md` amends this contract; everything else is unchanged.

- **Section 11 (children).** With `routing.child_warm_start = true`, `forge_spawn` passes `ReadOnlyLibrary(parent library)` in place of `NullLibrary()`: a child can warm-start, but store and verification are never called. The default is `false`. Children are never routing Tier A candidates (their floors are private).
- **Section 12.2 (CLI).** `architect forge run <request_or_spec> --input JSON|@FILE [--cases FILE] [--reuse-installed]` builds an ephemeral harness with Forge forced on (`trigger=always`), lets Stage 4 license it, runs it once and expires it. `architect forge integrate <request> --repo R --into DIR [--approved-spec F] [--package NAME] [--stage-only | --package-only]` builds, licenses and installs, exports a package and places it only after four gates (approval, spend consent, license, export consent). Both print `forge agent: <model> (mode <agent|template>, turns <N>)` immediately before the unchanged Phase 9 cap line. For `compile`, `recompile`, `do` and `create` the agent line prints only under `roles.policy = "evidence"`, so the default (pinned) consent output is byte-identical to Phase 9.
- **Section 12.4 (codes).** New codes are added (`forge_run_*`, `forge_cases_*`, `integrate_*`, `export_*`, `license_unverifiable`, `registry_subharness_missing`); no existing code changed. `forge_cancelled`, `forge_verification_cap`, `forge_no_submission` and `forge_disabled` are reused.
- **Registry.** `Registry.install` now copies `subharnesses/` and `forge.json` and re-checks every bound child before the rename; a missing or tampered child is `registry_subharness_missing`.
