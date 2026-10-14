"""Image literals in task JSON: lift the bytes out, leave content-free stubs behind.

Task convention: an image literal anywhere in a task is exactly
``{"$image": {"media_type": "<MediaType>", "base64": "<...>"}}``; its stub form
is ``{"$image": {"media_type", "sha256", "width", "height", "attached"}}`` with a
1-based ``attached`` index. Input schemas that accept a page require only
``media_type`` under ``$image`` so both forms validate.

``lift_images`` runs before any prompt is built, so base64 never reaches a
prompt, the context policy, the per-byte quote, telemetry or the sandbox pipe.
Any object carrying a ``$image`` key must be exactly one of the two forms;
anything else fails closed rather than letting bytes through as text.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

from pydantic import JsonValue, ValidationError

from architect.models.images import MAX_IMAGES_PER_REQUEST, ImageInput, ImageRef, ImageStub
from architect.spec import Spec

from . import PresetError

__all__ = ["IMAGE_KEY", "attach_stages", "lift_images", "stage_attach", "stage_attaches"]

IMAGE_KEY = "$image"
_LITERAL_KEYS = frozenset({"media_type", "base64"})
_STUB_KEYS = frozenset({"media_type", "sha256", "width", "height", "attached"})
# Set by the pipeline around each stage harness: whether that stage sends images.
_STAGE_ATTACH: ContextVar[bool] = ContextVar("stage_attach_images", default=True)


def _invalid() -> PresetError:
    return PresetError("Invalid image reference in task")


def _literal(body: dict[str, JsonValue]) -> ImageInput:
    encoded = body["base64"]
    if not isinstance(encoded, str):
        raise _invalid()
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
        image = ImageInput.from_bytes(data)
    except (ValueError, UnicodeError, binascii.Error, ValidationError) as exc:
        raise _invalid() from exc
    if image.media_type != body["media_type"]:
        raise _invalid()
    return image


def _stub(body: dict[str, JsonValue]) -> ImageStub:
    try:
        return ImageStub.model_validate(body)
    except ValidationError as exc:
        raise _invalid() from exc


def _stub_json(stub: ImageStub) -> JsonValue:
    return {IMAGE_KEY: cast(JsonValue, stub.model_dump(mode="json"))}


def lift_images(task: JsonValue) -> tuple[JsonValue, tuple[ImageRef, ...]]:
    """Depth-first walk; each literal becomes an ImageInput and is replaced by its stub;
    each existing stub becomes an ImageStub. Order = attached index. Bytes never remain.

    Object keys are visited in sorted order (JSON key order is not significant and
    the sandbox pipe sorts keys), while the returned task keeps its key order. A
    literal's ``attached`` index is its 1-based position in the walk; an existing
    stub keeps its own index, and a repeated identical stub is listed once.
    """
    refs: list[ImageRef] = []

    def convert(value: dict[str, JsonValue]) -> JsonValue:
        body = value[IMAGE_KEY]
        if len(value) != 1 or not isinstance(body, dict):
            raise _invalid()
        keys = set(body)
        if keys == _LITERAL_KEYS:
            image = _literal(body)
            stub = image.stub(len(refs) + 1)
            refs.append(image)
            return _stub_json(stub)
        if keys == _STUB_KEYS:
            stub = _stub(body)
            if stub not in refs:
                refs.append(stub)
            return _stub_json(stub)
        raise _invalid()

    def walk(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            if IMAGE_KEY in value:
                return convert(value)
            converted = {key: walk(value[key]) for key in sorted(value)}
            return {key: converted[key] for key in value}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    lifted = walk(task)
    if len(refs) > MAX_IMAGES_PER_REQUEST:
        raise PresetError("Task carries too many images")
    return lifted, tuple(refs)


@contextmanager
def stage_attach(attach: bool) -> Iterator[None]:
    """Bind ``attach_images`` for the preset execution of one pipeline stage."""
    token = _STAGE_ATTACH.set(attach)
    try:
        yield
    finally:
        _STAGE_ATTACH.reset(token)


def stage_attaches() -> bool:
    """False only inside a pipeline stage whose policy does not name it."""
    return _STAGE_ATTACH.get()


def attach_stages(spec: Spec) -> frozenset[str]:
    """spec.execution_config['images']['stages'] if present and a list of step names
    (validated: every name is a pipeline step), else {first step name}; empty set for
    single-agent specs means 'attach on every call'."""
    if not spec.pipeline:
        return frozenset()
    names = [step.name for step in spec.pipeline]
    config = spec.execution_config.get("images")
    if config is None:
        return frozenset({names[0]})
    if not isinstance(config, dict) or not set(config) <= {"stages"}:
        raise PresetError("Image attach policy must be an object with only 'stages'")
    stages = config.get("stages")
    if stages is None:
        return frozenset({names[0]})
    if (
        not isinstance(stages, list)
        or not all(isinstance(name, str) for name in stages)
        or not set(cast(list[str], stages)) <= set(names)
    ):
        raise PresetError("Image attach stages must name pipeline steps")
    return frozenset(cast(list[str], stages))
