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

## Statements

| Form | Example |
|---|---|
| assignment | x = 1; a, b = pair; d["k"] = v; l[0] = v |
| annotated assignment | n: int = 0 (the annotation is ignored) |
| augmented assignment | n += 1, also -=, *=, //= and %= |
| if | if a: ... elif b: ... else: ... |
| for | for x in l:, for k, v in items(d): or for a, b, c in zip(l1, l2, l3): over a list, dict keys, str or range |
| while | while n < 10: |
| break, continue, pass | break and continue only inside a loop |
| try | try: ... except: ... catches data errors only; so do except ValueError: and except (KeyError, TypeError) as e:, with e set to the error's code |
| return | return value |
| call statement | append(out, x); out.append(x); clean(x); await context.call(p) |

## Expressions

| Form | Example |
|---|---|
| arithmetic | a + b, a - b, a * b, a / b, a // b, a % b, -a (+ also joins strs, lists) |
| comparison | ==, !=, <, <=, >, >=, in, not in, x is None, x is not None |
| logic | a and b, a or b, not a |
| conditional | a if ok else b |
| f-string | f"{name}: {n}" |
| containers | [1, 2], (1, 2) (a list), {"k": v} (str keys) |
| index and slice | x[0], d["k"], s[1:3], l[::-1] (step 1 or -1) |
| call | len(x), clean(x), sorted(l, reverse=True), isinstance(x, (int, float)) |
| method | s.split(","), l.append(x), d.get("k") |
| comprehension | [f(x) for x in l if x], {k: v for k, v in items(d)}, {x for x in l} |
| generator | any(x > 0 for x in l), also in all, sum, min, max, sorted, list and join |

## Builtins

- Values: len(x), str(x), int(x), float(x), bool(x), abs(x), round(x[, n]),
  min(list) or min(a, b), max(list) or max(a, b), sum(list), any(list), all(list),
  sorted(list, reverse=False), range(a[, b[, c]]), enumerate(list),
  zip(l1, l2[, l3]), list(x), kind(x).
- Lists and dicts: keys(d), values(d), items(d), get(d, k[, default]),
  at(l, i[, default]), append(l, v), extend(l, l2), insert(l, i, v), pop(l),
  set_key(d, k, v), copy(x).
- Strings: split(s[, sep]), join(sep, list_of_str), strip(s[, chars]),
  lstrip(s[, chars]), rstrip(s[, chars]), lower(s), upper(s), startswith(s, p),
  endswith(s, p), replace(s, a, b), find(s, sub), rfind(s, sub), count(s, sub),
  splitlines(s), isdigit(s), isalpha(s), isalnum(s), isspace(s), isupper(s),
  islower(s), chr(i), ord(c).
- JSON: parse_json(s), extract_json(s), to_json(x), digest(x).
- Grids, for games: grid_diff(a, b[, limit]), diff_cells(a, b[, limit]),
  grid_text(grid), objects(grid[, background]), count_colors(grid),
  find_cells(grid, color), crop(grid, x0, y0, x1, y1), grid_equal(a, b).

min and max take one list or two values: max(l) or max(a, b). split(s) with no sep
splits on runs of whitespace; strip, lstrip and rstrip remove whitespace, or the
characters given. copy(x) copies a list or dict whole. get and at never raise; their
default is None when left out. append, extend, insert, pop and set_key change their
first argument, which must be a local variable. isinstance(x, T) takes bool, dict,
float, int, list, str or a tuple of them; as in Python, True is an int.

Methods are the same builtins written after their first argument: s.split(sep),
s.split(), sep.join(parts), s.strip(), s.rstrip(chars), s.lower(), s.upper(),
s.startswith(p), s.endswith(p), s.replace(a, b), s.find(sub), s.rfind(sub),
s.count(sub), s.splitlines(), s.isdigit(), d.get(k[, default]), d.keys(),
d.values(), d.items(), d.copy(), l.append(v), l.extend(l2), l.insert(i, v), l.pop(),
l.copy(). No other method or attribute exists.

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

## Errors and limits

- Data errors, caught by try/except: sandbox_add, sandbox_dictionary, sandbox_index,
  sandbox_key, sandbox_type, sandbox_value.
- Fatal errors, which end the run: sandbox_fuel, sandbox_invalid_call,
  sandbox_memory, sandbox_unbound, sandbox_value_limit.
- Limits: 64 KiB of source, 8,000 syntax nodes, 32 helpers, 64 constants, 8
  parameters, loops 6 deep, strs of 131,072 chars, lists and dicts of 65,536
  entries, values 40 deep, ranges of 100,000 items.
- Fuel: the binding sets it (default 10,000,000). Every evaluated node costs 1, a
  builtin 1 more per 256 units of work, and a capability call 64 plus 1 per 8 bytes
  sent and received. Loops must end.
