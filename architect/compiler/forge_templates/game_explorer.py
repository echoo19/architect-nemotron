"""Template ``game_explorer``: digest visited set, untried options first, model when stuck.

The source of truth is ``examples/forge/arc_explorer.py``; a test pins this copy
to that file byte for byte. Game templates carry no placeholders.
"""

from typing import Final

SOURCE: Final = r'''"""ARC explorer: the committed Forge sandbox language v2 reference harness.

Exploration state lives in code, not in the prompt: a digest-keyed visited set,
an untried-option-per-state policy, frame-change (no-op) detection and a memory
of the model's advice per state. The first steps (one per option, at most a
quarter of the action budget) probe untried options with no model call. After
that the play model is asked for one option per new state, at most once per
state; its answer is reused on revisits, and code overrides it with an untried
live option when it is a known no-op. The host produces the game output; the
return is ignored.

Many public games draw a step or energy bar along an edge that advances one cell
per action. Edge rows and columns that change on at least 80% of steps (after
five) are treated as that clock: states are keyed, and no-ops detected, on the
frame with the clock cropped away, and click targets on the clock are dropped.
Without this every frame is new, so every step would cost a model call.
"""
# ruff: noqa: F821  (sandbox builtins such as digest and grid_text are not Python names)

from pydantic import JsonValue

from architect.runtime import RunContext

MAX_OPTIONS = 12
BAND = 3
CLOCK_MIN_STEPS = 5
HINT = (
    "You play a grid game. "
    'Reply with only JSON {"option": "<one listed option>"} naming the option most likely '
    "to make progress toward winning."
)


def full_box(frame):
    return [0, 0, len(frame[0]) - 1, len(frame) - 1]


def inside(box, x, y):
    return x >= box[0] and y >= box[1] and x <= box[2] and y <= box[3]


def options(obs, box):
    result = []
    for name in obs["available_actions"]:
        if name == "ACTION6":
            for item in objects(obs["frame"], 0):
                x = int(item["cx"])
                y = int(item["cy"])
                if len(result) < MAX_OPTIONS and inside(box, x, y):
                    append(result, "ACTION6:" + str(x) + ":" + str(y))
        elif name != "RESET":
            if len(result) < MAX_OPTIONS:
                append(result, name)
    return result


def action_of(option):
    parts = split(option, ":")
    if len(parts) == 3:
        return {"name": parts[0], "x": int(parts[1]), "y": int(parts[2])}
    return {"name": option}


def first_untried(choices, done):
    for option in choices:
        if option not in done:
            return option
    return None


def playing(obs):
    if obs["refused"] or obs["actions_left"] <= 0:
        return False
    return obs["state"] != "WIN" and obs["state"] != "GAME_OVER"


def edges(size):
    if size < 4 * BAND:
        return []
    result = []
    for i in range(BAND):
        append(result, i)
        append(result, size - 1 - i)
    return result


def is_clock(clock, hits):
    return clock["steps"] >= CLOCK_MIN_STEPS and hits * 5 >= clock["steps"] * 4


def clock_step(clock, before, after):
    height = len(after)
    width = len(after[0])
    if len(before) != height or len(before[0]) != width:
        return clock
    rows = clock["rows"]
    cols = clock["cols"]
    for y in edges(height):
        line = crop(after, 0, y, width - 1, y)
        if not grid_equal(crop(before, 0, y, width - 1, y), line):
            set_key(rows, str(y), get(rows, str(y), 0) + 1)
    for x in edges(width):
        line = crop(after, x, 0, x, height - 1)
        if not grid_equal(crop(before, x, 0, x, height - 1), line):
            set_key(cols, str(x), get(cols, str(x), 0) + 1)
    result = {"steps": clock["steps"] + 1, "rows": rows, "cols": cols}
    top = 0
    bottom = height - 1
    left = 0
    right = width - 1
    for i in range(BAND):
        if is_clock(result, get(rows, str(i), 0)):
            top = i + 1
        if is_clock(result, get(rows, str(height - 1 - i), 0)):
            bottom = min([bottom, height - 2 - i])
        if is_clock(result, get(cols, str(i), 0)):
            left = i + 1
        if is_clock(result, get(cols, str(width - 1 - i), 0)):
            right = min([right, width - 2 - i])
    set_key(result, "box", [left, top, right, bottom])
    return result


def view(frame, box):
    return crop(frame, box[0], box[1], box[2], box[3])


async def ask(context, obs, choices):
    prompt = HINT + "\nFrame (one hex digit per cell):\n" + grid_text(obs["frame"])
    prompt = prompt + "\nOptions: " + join(", ", choices)
    reply = await context.call(prompt, 64)
    data = extract_json(reply)
    if kind(data) == "dict":
        option = get(data, "option", None)
        if kind(option) == "str" and option in choices:
            return option
    return None


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    obs = await context.observe()
    clock = {"steps": 0, "rows": {}, "cols": {}, "box": full_box(obs["frame"])}
    tried = {}
    dead = {}
    advice = {}
    visits = {}
    probes = min([len(options(obs, clock["box"])), obs["actions_left"] // 4])
    steps = 0
    while playing(obs):
        box = clock["box"]
        choices = options(obs, box)
        if len(choices) == 0:
            break
        key = digest([obs["levels_completed"], view(obs["frame"], box)])
        set_key(visits, key, get(visits, key, 0) + 1)
        done = get(tried, key, [])
        pick = None
        if steps < probes:
            pick = first_untried(choices, done)
        if pick is None:
            if key not in advice:
                answer = await ask(context, obs, choices)
                set_key(advice, key, answer)
            pick = get(advice, key, None)
            if pick is None or pick in get(dead, key, []) or pick not in choices:
                live = []
                for option in choices:
                    if option not in get(dead, key, []):
                        append(live, option)
                if len(live) == 0:
                    live = choices
                pick = first_untried(live, done)
                if pick is None:
                    pick = live[get(visits, key, 0) % len(live)]
        set_key(tried, key, done + [pick])
        before = obs["frame"]
        obs = await context.step(action_of(pick))
        steps += 1
        clock = clock_step(clock, before, obs["frame"])
        if grid_equal(view(before, box), view(obs["frame"], box)):
            set_key(dead, key, get(dead, key, []) + [pick])
    return {"steps": steps}
'''
