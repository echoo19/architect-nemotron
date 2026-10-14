"""Template ``extract_verify``: the bound preset answers, then one cheap self-check call.

On a "no" verdict the preset answers once more. ``$PRESET_BINDING`` and
``$TASK_TEXT`` are substituted by ``instantiate``; the rest is fixed v2 source.
"""

from typing import Final

SOURCE: Final = r'''"""Forge template extract_verify: preset answer, self-check, one retry."""

from pydantic import JsonValue

from architect.presets import execute_preset
from architect.runtime import RunContext

TASK = $TASK_TEXT
CHECK = "Answer only yes or no. Is the proposed output a correct and complete answer?"


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    answer = await execute_preset($PRESET_BINDING, context, task)
    prompt = CHECK + "\nTask: " + TASK + "\nInput: " + to_json(task)
    verdict = await context.call(prompt + "\nProposed output: " + to_json(answer), 8)
    if startswith(lower(strip(verdict)), "no"):
        answer = await execute_preset($PRESET_BINDING, context, task)
    return answer
'''
