"""What the conductor tells a front end, and what it asks of one (Phase 12, Section 8).

The conductor never prints. It emits ``Event`` values to a ``UI`` and asks the UI for
answers (interview questions, confirmations, approvals). The terminal front end, the line
fallback, ``-p`` mode and tests are all ``UI`` implementations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

__all__ = [
    "AssistantText",
    "Approval",
    "Event",
    "Notice",
    "PlanCard",
    "Progress",
    "RecordingUI",
    "SubagentActivity",
    "TextDelta",
    "Thinking",
    "ToolFinished",
    "ToolStarted",
    "UI",
]

Approval = Literal["yes", "session", "no"]


@dataclass(frozen=True)
class Thinking:
    """A model call started (``active``) or ended; ``label`` names who is thinking."""

    active: bool
    label: str = "architect"


@dataclass(frozen=True)
class TextDelta:
    """A streamed piece of the answer (Phase 13 native providers); AssistantText still ends it."""

    text: str


@dataclass(frozen=True)
class AssistantText:
    """The conductor's final answer for this turn (markdown)."""

    text: str


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    name: str
    summary: str


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class SubagentActivity:
    """One line of what a subagent is doing (a coder edit, a compile stage, ...)."""

    agent: str
    text: str


@dataclass(frozen=True)
class Progress:
    """A replaceable status line for long work (compile stage, calls, spend)."""

    text: str


@dataclass(frozen=True)
class Notice:
    text: str
    level: Literal["info", "warn", "error"] = "info"


@dataclass(frozen=True)
class PlanCard:
    """Build mode's plan before compiling: ordered (label, value) rows.

    ``data`` holds the same card as values for a front end that draws it (the desktop's
    hash lines, bar and ladder, and after the compile each rung's signed exam counts):
    numbers, model ids, hashes and fixed codes, never a case. Terminals show the rows.
    """

    title: str
    rows: tuple[tuple[str, str], ...]
    footer: str = ""
    data: Mapping[str, object] | None = field(default=None, compare=False, hash=False)


Event = (
    Thinking
    | TextDelta
    | AssistantText
    | ToolStarted
    | ToolFinished
    | SubagentActivity
    | Progress
    | Notice
    | PlanCard
)


class UI(Protocol):
    def emit(self, event: Event) -> None: ...

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        """One answer; ``None`` means the user cancelled (Esc, Ctrl-C, end of input)."""
        ...

    async def approve(self, action: str, detail: str) -> Approval:
        """Permission for one subagent action; anything unclear is ``no``."""
        ...


@dataclass
class RecordingUI:
    """A scripted UI for tests and ``-p`` mode: records events, answers from lists."""

    answers: list[str | None] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    default_approval: Approval = "no"
    events: list[Event] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def emit(self, event: Event) -> None:
        self.events.append(event)

    async def ask(
        self, question: str, *, default: str | None = None, choices: Sequence[str] = ()
    ) -> str | None:
        self.questions.append(question)
        if self.answers:
            answer = self.answers.pop(0)
            return default if answer == "" else answer
        return default

    async def approve(self, action: str, detail: str) -> Approval:
        if self.approvals:
            return self.approvals.pop(0)
        return self.default_approval

    def texts(self) -> list[str]:
        return [event.text for event in self.events if isinstance(event, AssistantText)]
