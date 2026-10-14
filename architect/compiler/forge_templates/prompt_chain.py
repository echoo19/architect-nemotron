"""Template ``prompt_chain``: compact prompt, ``extract_json``, one repair call, preset fallback.

``$TASK_TEXT``, ``$OUTPUT_SCHEMA`` and ``$PRESET_BINDING`` are substituted by
``instantiate``; the rest is fixed v2 source.
"""

from typing import Final

SOURCE: Final = r'''"""Forge template prompt_chain: prompt, parse, repair once, preset fallback."""

from pydantic import JsonValue

from architect.presets import execute_preset
from architect.runtime import RunContext

TASK = $TASK_TEXT
SCHEMA = $OUTPUT_SCHEMA
REPAIR = "Your previous reply was not valid JSON. Reply with only the JSON output."


def parse(reply):
    value = parse_json(strip(reply))
    if value is None:
        value = extract_json(reply)
    return value


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    prompt = "Task: " + TASK + "\nOutput JSON schema: " + to_json(SCHEMA)
    prompt = prompt + "\nInput: " + to_json(task) + "\nReply with only the JSON output."
    value = parse(await context.call(prompt))
    if value is None:
        value = parse(await context.call(prompt + "\n" + REPAIR))
    if value is None:
        value = await execute_preset($PRESET_BINDING, context, task)
    return value
'''
