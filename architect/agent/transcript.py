"""The conductor's conversation, kept in Python and rendered into each prompt.

Messages are plain JSON so a conversation can be saved, resumed and rendered for any
text model. Tool results are clipped when stored; ``compact`` shortens old tool results
to one line when the rendered conversation would outgrow the model's context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import JsonValue

__all__ = ["Message", "Transcript", "clip"]

Role = Literal["user", "assistant", "tool", "note"]
RESULT_CHARS = 12_000
COMPACT_RESULT_CHARS = 240


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 40)] + f"... [{len(text) - limit + 40} more chars clipped]"


@dataclass
class Message:
    role: Role
    text: str = ""
    # assistant: the tool calls it made; tool: the call it answers.
    calls: list[dict[str, JsonValue]] = field(default_factory=list)
    call_id: str | None = None
    name: str | None = None
    ok: bool = True

    def to_json(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {"role": self.role}
        if self.text:
            data["text"] = self.text
        if self.calls:
            data["tool_calls"] = list(self.calls)
        if self.call_id is not None:
            data["call_id"] = self.call_id
        if self.name is not None:
            data["name"] = self.name
        if self.role == "tool" and not self.ok:
            data["ok"] = False
        return data

    @classmethod
    def from_json(cls, data: dict[str, JsonValue]) -> Message:
        role = data.get("role")
        if role not in ("user", "assistant", "tool", "note"):
            raise ValueError("invalid_message")
        calls = data.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ValueError("invalid_message")
        text = data.get("text", "")
        call_id = data.get("call_id")
        name = data.get("name")
        return cls(
            role=cast(Role, role),
            text=text if isinstance(text, str) else "",
            calls=[call for call in calls if isinstance(call, dict)],
            call_id=call_id if isinstance(call_id, str) else None,
            name=name if isinstance(name, str) else None,
            ok=data.get("ok", True) is not False,
        )


@dataclass
class Transcript:
    messages: list[Message] = field(default_factory=list)

    def user(self, text: str) -> None:
        self.messages.append(Message("user", text))

    def assistant(self, text: str = "", calls: list[dict[str, JsonValue]] | None = None) -> None:
        self.messages.append(Message("assistant", text, calls=list(calls or [])))

    def tool(self, call_id: str, name: str, result: JsonValue, *, ok: bool = True) -> None:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        self.messages.append(
            Message("tool", clip(text, RESULT_CHARS), call_id=call_id, name=name, ok=ok)
        )

    def note(self, text: str) -> None:
        """Host-written context (an interrupted turn, a mode switch, a compaction)."""
        self.messages.append(Message("note", text))

    def render(self) -> str:
        return json.dumps([m.to_json() for m in self.messages], ensure_ascii=False)

    def compact(self, keep_recent: int = 6) -> int:
        """Shorten tool results older than the last ``keep_recent`` messages; return how many."""
        cutoff = max(0, len(self.messages) - keep_recent)
        changed = 0
        for message in self.messages[:cutoff]:
            if message.role == "tool" and len(message.text) > COMPACT_RESULT_CHARS:
                message.text = clip(message.text, COMPACT_RESULT_CHARS)
                changed += 1
        return changed

    def fit(self, limit_chars: int) -> int:
        """Compact progressively until the rendering fits ``limit_chars``; return changes."""
        changed = 0
        for keep in (8, 4, 2, 0):
            if len(self.render()) <= limit_chars:
                break
            changed += self.compact(keep)
        dropped = Message("note", "Earlier conversation was dropped to fit the context.")
        while len(self.render()) > limit_chars and len(self.messages) > 2:
            # Last resort: drop the oldest messages behind a single marker note.
            if self.messages[0].text == dropped.text:
                self.messages.pop(1)
            else:
                self.messages[0] = dropped
            changed += 1
        return changed

    def to_jsonl(self) -> str:
        return "".join(json.dumps(m.to_json(), ensure_ascii=False) + "\n" for m in self.messages)

    @classmethod
    def from_jsonl(cls, text: str) -> Transcript:
        messages = []
        for line in text.splitlines():
            if line.strip():
                data = json.loads(line)
                if isinstance(data, dict):
                    messages.append(Message.from_json(data))
        return cls(messages)
