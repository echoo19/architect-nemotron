"""Strict JSON action decoding shared by the local game environments."""

from collections.abc import Collection


class ActionError(ValueError):
    """Invalid model-authored action data; no input content is included."""


def parse_actions(value: object, allowed: Collection[str], maximum: int) -> tuple[str, ...]:
    if type(maximum) is not int or maximum < 1:
        raise ValueError("invalid_action_limit")
    if not isinstance(value, dict) or set(value) != {"actions"}:
        raise ActionError("invalid_actions")
    actions = value["actions"]
    if not isinstance(actions, list) or len(actions) > maximum:
        raise ActionError("invalid_actions")
    choices = frozenset(allowed)
    if any(not isinstance(action, str) or action not in choices for action in actions):
        raise ActionError("invalid_actions")
    return tuple(actions)
