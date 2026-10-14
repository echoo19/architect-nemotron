"""Scoped measurement identity; trusted callers only, never licensing authority."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(eq=False)
class EvaluationScope:
    candidate_digest: str
    measurement_id: str = field(default_factory=lambda: uuid4().hex)
    active: bool = True
    _cleanup: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def register_cleanup(self, callback: Callable[[], None]) -> None:
        if not self.active:
            raise ValueError("evaluation_scope_expired")
        self._cleanup.append(callback)


_CURRENT: ContextVar[EvaluationScope | None] = ContextVar("evaluation_identity", default=None)


def current_evaluation() -> EvaluationScope | None:
    return _CURRENT.get()


@contextmanager
def scoped_evaluation(candidate_digest: str) -> Iterator[EvaluationScope]:
    scope = EvaluationScope(candidate_digest)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        scope.active = False
        _CURRENT.reset(token)
        callbacks, scope._cleanup = scope._cleanup, []
        first_error: BaseException | None = None
        for callback in reversed(callbacks):
            try:
                callback()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
