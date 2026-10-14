"""Template ``game_hypothesis``: frame summaries and one rule hypothesis every few turns.

The model returns a short plan of listed options, which the code executes
deterministically. Game templates carry no placeholders.
"""

from typing import Final

SOURCE: Final = r'''"""Forge template game_hypothesis: summarise, ask for a plan, execute it."""
# ruff: noqa: F821  (sandbox builtins are not Python names)

from pydantic import JsonValue

from architect.runtime import RunContext

EVERY = 6
MAX_OPTIONS = 12
PROMPT = (
    "You play a grid game. Guess one rule from the objects and colours below, then reply "
    'with only JSON {"rule": "<short rule>", "plan": ["<option>", ...]} using listed options.'
)


def options(obs):
    result = []
    for name in obs["available_actions"]:
        if name == "ACTION6":
            for item in objects(obs["frame"], 0):
                if len(result) < MAX_OPTIONS:
                    append(result, "ACTION6:" + str(int(item["cx"])) + ":" + str(int(item["cy"])))
        elif name != "RESET":
            if len(result) < MAX_OPTIONS:
                append(result, name)
    return result


def action_of(option):
    parts = split(option, ":")
    if len(parts) == 3:
        return {"name": parts[0], "x": int(parts[1]), "y": int(parts[2])}
    return {"name": option}


def summary(obs):
    shapes = []
    for item in objects(obs["frame"], 0):
        if len(shapes) < 16:
            append(shapes, [item["color"], item["x0"], item["y0"], item["x1"], item["y1"]])
    return {"objects": shapes, "colors": count_colors(obs["frame"]), "state": obs["state"]}


def playing(obs):
    if obs["refused"] or obs["actions_left"] <= 0:
        return False
    return obs["state"] != "WIN" and obs["state"] != "GAME_OVER"


async def hypothesis(context, obs, choices):
    prompt = PROMPT + "\nSummary: " + to_json(summary(obs)) + "\nOptions: " + join(", ", choices)
    data = extract_json(await context.call(prompt, 160))
    plan = []
    if kind(data) == "dict" and kind(get(data, "plan", None)) == "list":
        for item in data["plan"]:
            if kind(item) == "str" and item in choices and len(plan) < EVERY:
                append(plan, item)
    return plan


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    obs = await context.observe()
    plan = []
    turn = 0
    while playing(obs):
        choices = options(obs)
        if len(choices) == 0:
            break
        if len(plan) == 0 and turn % EVERY == 0:
            plan = await hypothesis(context, obs, choices)
        if len(plan) == 0:
            plan = [choices[turn % len(choices)]]
        pick = plan[0]
        plan = plan[1:]
        obs = await context.step(action_of(pick))
        turn += 1
    return {"turns": turn}
'''
