"""Deterministic scripts for offline evaluations and smoke tests.

Images are priced: ``quote`` adds the fake model's per-image ceiling
(``pricing.image_reservation``) at the fake input rate on top of the scripted
usage, and ``complete`` returns the scripted response with ``tokens_in`` and
``cost_usd`` raised by exactly that amount, so the fake "measures" the ceiling
and ``quote == measured`` stays true. A fake priced without ``image`` refuses
images in ``quote`` like every other adapter. Every completed request leaves a
content-free ``RequestShape`` in ``seen``, including the sha256 of the
request's canonical ``response_schema`` (``None`` without one).
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .pricing import image_reservation, load_prices, price_for
from .types import AdapterError, ModelRequest, ModelResponse, UsageQuote


class RequestShape(BaseModel):
    """Content-free record of one completed request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_bytes: int
    image_count: int
    image_bytes: int
    sha256s: tuple[str, ...]
    response_schema_sha256: str | None = None


class FakeAdapter:
    def __init__(
        self,
        model: str = "fake",
        responses: Sequence[ModelResponse] | None = None,
        fixture: Path | None = None,
        *,
        prices_path: Path | None = None,
    ) -> None:
        if responses is not None and fixture is not None:
            raise AdapterError("Specify responses or fixture, not both")
        self.model = model
        self.calls = 0
        self.seen: list[RequestShape] = []
        prices = load_prices(prices_path)
        price = price_for(prices, model)
        self._price = price
        self._default = ModelResponse(
            text="ok",
            tokens_in=prices.fake_usage.tokens_in,
            tokens_out=prices.fake_usage.tokens_out,
            cost_usd=price.cost(prices.fake_usage.tokens_in, prices.fake_usage.tokens_out),
            model=model,
        )
        if fixture is not None:
            try:
                responses = TypeAdapter(list[ModelResponse]).validate_json(
                    fixture.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, ValidationError) as exc:
                raise AdapterError("Invalid or unreadable response fixture") from exc
        self._responses = tuple(responses) if responses is not None else None
        if self._responses is not None and any(item.model != model for item in self._responses):
            raise AdapterError("Scripted response model does not match adapter")

    @classmethod
    def from_fixture(
        cls, fixture: Path, *, model: str = "fake", prices_path: Path | None = None
    ) -> "FakeAdapter":
        return cls(model=model, fixture=fixture, prices_path=prices_path)

    def _next(self, index: int) -> ModelResponse:
        if self._responses is None:
            return self._default
        if index >= len(self._responses):
            raise AdapterError("Scripted responses exhausted")
        return self._responses[index]

    def _priced(self, response: ModelResponse, request: ModelRequest) -> ModelResponse:
        """The scripted response plus the image ceiling at the fake input rate."""
        image_tokens = image_reservation(self._price, request.images)
        if not image_tokens:
            return response
        return response.model_copy(
            update={
                "tokens_in": response.tokens_in + image_tokens,
                "cost_usd": response.cost_usd + self._price.cost(image_tokens, 0),
            }
        )

    def quote(self, request: ModelRequest) -> UsageQuote:
        response = self._priced(self._next(self.calls), request)
        return UsageQuote(
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # Refuse images before counting the call, so a refusal neither records
        # a phantom call nor consumes a scripted response.
        image_reservation(self._price, request.images)
        index = self.calls
        self.calls += 1
        response = self._priced(self._next(index), request)
        self.seen.append(
            RequestShape(
                prompt_bytes=len(request.prompt.encode("utf-8")),
                image_count=len(request.images),
                image_bytes=sum(len(image.data) for image in request.images),
                sha256s=tuple(
                    hashlib.sha256(image.data).hexdigest() for image in request.images
                ),
                response_schema_sha256=request.response_schema_sha256(),
            )
        )
        return response


class ScriptedCall(NamedTuple):
    """One recorded request shape of a ScriptedAdapter (the text after ``system``)."""

    prompt: str
    system_chars: int
    images: int
    tokens: int | None


class ScriptedAdapter:
    """Zero-priced, deterministic replies for ``forge_dryrun``; never a measurement.

    Every request is quoted and billed at $0 and 0 tokens, so a wallet
    reservation moves nothing. Replies cycle in order. The first
    ``max_recorded`` requests are kept as ``ScriptedCall`` shapes; ``calls``
    counts all of them.
    """

    def __init__(
        self, replies: Sequence[str], *, model: str = "fake", max_recorded: int = 8
    ) -> None:
        if isinstance(replies, str) or not all(isinstance(item, str) for item in replies):
            raise AdapterError("Scripted replies must be a sequence of strings")
        if type(max_recorded) is not int or max_recorded < 0:
            raise AdapterError("Invalid scripted record limit")
        self.model = model
        self._replies = tuple(replies) or ("{}",)
        self._max_recorded = max_recorded
        self.calls = 0
        self.requests: list[ScriptedCall] = []

    def quote(self, request: ModelRequest) -> UsageQuote:
        return UsageQuote(tokens_in=0, tokens_out=0, cost_usd=0.0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        index = self.calls
        self.calls += 1
        if len(self.requests) < self._max_recorded:
            system, prompt = request.split()
            self.requests.append(
                ScriptedCall(
                    prompt=prompt,
                    system_chars=len(system),
                    images=len(request.images),
                    tokens=request.max_output_tokens,
                )
            )
        return ModelResponse(
            text=self._replies[index % len(self._replies)],
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            model=self.model,
        )
