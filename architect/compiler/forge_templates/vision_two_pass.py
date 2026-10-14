"""Template ``vision_two_pass``: a layout pass with ``images=``, then an answer pass.

Pass 2 re-attaches only the first page stub. ``$TASK_TEXT``, ``$OUTPUT_SCHEMA``
and ``$PRESET_BINDING`` are substituted by ``instantiate``.
"""

from typing import Final

SOURCE: Final = r'''"""Forge template vision_two_pass: layout pass, then one answer pass."""

from pydantic import JsonValue

from architect.presets import execute_preset
from architect.runtime import RunContext

TASK = $TASK_TEXT
SCHEMA = $OUTPUT_SCHEMA
LAYOUT = "Describe the layout of the attached pages briefly: regions, tables, labelled fields."
MAX_PAGES = 8


def image_stubs(value):
    found = []
    stack = [value]
    while len(stack) > 0:
        item = pop(stack)
        if kind(item) == "dict":
            if "$image" in item:
                if len(found) < MAX_PAGES:
                    append(found, item)
            else:
                for key in keys(item):
                    append(stack, item[key])
        elif kind(item) == "list":
            for entry in item:
                append(stack, entry)
    return found


def parse(reply):
    value = parse_json(strip(reply))
    if value is None:
        value = extract_json(reply)
    return value


async def run(context: RunContext, task: JsonValue) -> JsonValue:
    pages = image_stubs(task)
    layout = ""
    if len(pages) > 0:
        layout = await context.call(LAYOUT, 400, images=pages)
    prompt = "Task: " + TASK + "\nLayout notes: " + layout + "\nInput: " + to_json(task)
    prompt = prompt + "\nOutput JSON schema: " + to_json(SCHEMA) + "\nReply with only JSON."
    if len(pages) > 0:
        reply = await context.call(prompt, images=pages[0:1])
    else:
        reply = await context.call(prompt)
    value = parse(reply)
    if value is None:
        value = await execute_preset($PRESET_BINDING, context, task)
    return value
'''
