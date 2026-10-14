"""How conductor events look in the terminal (Phase 12 Section 8, Phase 13 Section 5).

Two renderers share one vocabulary: ``PlainRenderer`` writes plain text through any
``write`` function (the line fallback, pipes, CI, tests) and ``RichRenderer`` prints with
rich (markdown answers, colored notices, a plan card table, a spinner while thinking).

Streaming (``TextDelta``): the plain renderer writes each piece as it arrives. The rich
renderer prints every finished markdown block (a paragraph, a list, a closed code fence)
permanently and shows the block still being written in the live region above the spinner,
so the answer appears as it streams and still ends up rendered as markdown. The final
``AssistantText`` that closes a streamed answer is not printed a second time.

The live region (spinner with elapsed time, tokens and spend; the streaming block; any
messages typed and queued during the turn) is rich's ``Live``, used only while no prompt
is active: callers stop it before a question. Neither renderer writes the bare erase-line
sequence.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TextIO

from .events import (
    AssistantText,
    Event,
    Notice,
    PlanCard,
    Progress,
    SubagentActivity,
    TextDelta,
    Thinking,
    ToolFinished,
    ToolStarted,
)

if TYPE_CHECKING:
    from rich.console import Console as RichConsole
    from rich.console import RenderableType
    from rich.live import Live

__all__ = [
    "Glyphs",
    "PlainRenderer",
    "RichRenderer",
    "card_rows",
    "context_style",
    "glyphs_for",
    "mode_label",
    "money",
    "plain_rows",
    "split_blocks",
    "toolbar_fragments",
    "toolbar_text",
    "unicode_ok",
]


@dataclass(frozen=True)
class Glyphs:
    tool: str
    result: str
    sub: str
    sep: str
    ellipsis: str
    ok: str = "ok"
    fail: str = "x"
    warn: str = "!"
    answer: str = "*"
    up: str = "^"
    down: str = "v"


ASCII = Glyphs(tool="*", result="->", sub="-", sep=" | ", ellipsis="...", ok="+", fail="x",
               warn="!", answer="*", up="in", down="out")
UNICODE = Glyphs(tool="*", result="⎿", sub="·", sep=" · ", ellipsis="…", ok="✓", fail="✗",
                 warn="!", answer="●", up="↑", down="↓")

# Colours used everywhere (rich style names), so every surface agrees.
STYLE = {
    "accent": "cyan",
    "tool": "bold cyan",
    "dim": "dim",
    "ok": "green",
    "warn": "yellow",
    "error": "bold red",
    "agent": "magenta",
    "build": "bold magenta",
    "plan": "bold blue",
}


def unicode_ok(stream: TextIO | None = None) -> bool:
    """UTF-8 output on a terminal known to draw box glyphs; ASCII everywhere else."""
    stream = stream if stream is not None else sys.stdout
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    if encoding not in ("utf8", "utf8sig"):
        return False
    if sys.platform != "win32":
        return True
    return any(key in os.environ for key in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI"))


def glyphs_for(unicode: bool) -> Glyphs:
    return UNICODE if unicode else ASCII


def money(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def mode_label(mode: str, permission: str = "") -> str:
    """What the user calls a mode: normal, build or plan (the read-only permission).

    Build mode was shown as "forge" before Forge became the optional harness engineer;
    ``/mode forge`` still reaches it (``commands.MODE_NAMES``)."""
    if permission == "plan" and mode != "build":
        return "plan"
    return mode


def context_style(percent: float) -> str:
    """Green below 50%, yellow below 80%, red from 80% (auto-compact is near 85%)."""
    if percent < 50:
        return "green"
    if percent < 80:
        return "yellow"
    return "red"


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"


def _toolbar_parts(
    *,
    mode: str,
    permission: str,
    model: str,
    spent_usd: float,
    budget_usd: float,
    elapsed_s: float,
    budget_s: float,
    running: str,
    width: int,
    context_percent: float | None,
    queued: int,
    unpriced: bool = False,
) -> list[tuple[int, str, str]]:
    """(drop priority, text, style): low priorities are dropped first on a narrow screen."""
    label = mode_label(mode, permission)
    style = {"build": STYLE["build"], "plan": STYLE["plan"]}.get(label, "bold")
    parts: list[tuple[int, str, str]] = [
        (9, f"{label} mode" + (" (shift+tab)" if width >= 100 else ""), style),
    ]
    if permission != "plan" or label != "plan":
        parts.append((3, permission, ""))
    parts.append((2, model, ""))
    if context_percent is not None:
        parts.append((5, f"ctx {context_percent:.0f}%", context_style(context_percent)))
    if unpriced:
        parts.append((4, "cost unknown", "yellow"))
    else:
        parts.append((4, f"{money(spent_usd)}/{money(budget_usd)}", ""))
    if budget_s:
        parts.append((1, f"{_minutes(elapsed_s)}/{_minutes(budget_s)}", ""))
    if queued:
        parts.append((6, f"{queued} queued", "yellow"))
    if running:
        parts.append((0, running, ""))
    return parts


def _fit(parts: list[tuple[int, str, str]], width: int, sep: str) -> list[tuple[int, str, str]]:
    limit = max(10, width - 2)
    parts = list(parts)
    while len(parts) > 1 and len(sep.join(text for _, text, _ in parts)) > limit:
        parts.remove(min(parts, key=lambda part: part[0]))
    return parts


def toolbar_text(
    *,
    mode: str,
    permission: str,
    model: str,
    spent_usd: float,
    budget_usd: float,
    elapsed_s: float = 0.0,
    budget_s: float = 0.0,
    running: str = "",
    width: int = 120,
    glyphs: Glyphs = ASCII,
    context_percent: float | None = None,
    queued: int = 0,
    unpriced: bool = False,
) -> str:
    """The bottom toolbar as text; the least important parts go first when it is narrow."""
    parts = _toolbar_parts(
        mode=mode, permission=permission, model=model, spent_usd=spent_usd,
        budget_usd=budget_usd, elapsed_s=elapsed_s, budget_s=budget_s, running=running,
        width=width, context_percent=context_percent, queued=queued, unpriced=unpriced,
    )
    kept = _fit(parts, width, glyphs.sep)
    return glyphs.sep.join(text for _, text, _ in kept)[: max(10, width - 2)]


_PT_STYLES = {
    "green": "fg:ansigreen",
    "yellow": "fg:ansiyellow",
    "red": "fg:ansired bold",
    "bold": "bold",
    STYLE["build"]: "fg:ansimagenta bold",
    STYLE["plan"]: "fg:ansiblue bold",
    "": "",
}


def toolbar_fragments(glyphs: Glyphs = ASCII, **kwargs: Any) -> list[tuple[str, str]]:
    """The toolbar as prompt_toolkit formatted text (style, text) with colours."""
    width = int(kwargs.get("width", 120))
    kwargs.setdefault("elapsed_s", 0.0)
    kwargs.setdefault("budget_s", 0.0)
    kwargs.setdefault("running", "")
    kwargs.setdefault("context_percent", None)
    kwargs.setdefault("queued", 0)
    kwargs["width"] = width
    kept = _fit(_toolbar_parts(**kwargs), width, glyphs.sep)
    fragments: list[tuple[str, str]] = [("", " ")]
    for index, (_, text, style) in enumerate(kept):
        if index:
            fragments.append(("", glyphs.sep))
        fragments.append((_PT_STYLES.get(style, ""), text))
    return fragments


# Card rows whose value is a sentence the build writes in lower case ("counts as a miss,
# ...", "run it here: ..."): shown, it starts with a capital, as on the desktop's cards
# (SENTENCE_ROWS in desktop/src/lib/vocab.ts). Field lists, names, the task and figures
# keep their case, and so does a sentence that opens with a path or a field name.
SENTENCE_ROWS = frozenset(
    {"engineer", "invalid output", "forge budget", "destination", "note", "next"}
)
_SENTENCE = re.compile(r"(^|\. )([a-z])(?=[a-z-]*[ ,;])")


def _sentence(text: str) -> str:
    return _SENTENCE.sub(lambda match: match.group(1) + match.group(2).upper(), text)


def card_rows(rows: Sequence[tuple[str, str]], glyphs: Glyphs) -> list[tuple[str, str]]:
    """Plan-card rows as a terminal shows them: the " · " separator drawn as this glyph
    set's ``sep`` (an ASCII terminal shows "CORD v2 | CC BY 4.0 | 200 receipts"; Unicode
    output is unchanged), and a sentence row (``SENTENCE_ROWS``) with a capital. The card's
    own rows are unchanged."""
    shown: list[tuple[str, str]] = []
    for label, value in rows:
        text = str(value).replace(" · ", glyphs.sep)
        shown.append((label, _sentence(text) if label in SENTENCE_ROWS else text))
    return shown


def plain_rows(rows: Sequence[tuple[str, str]], indent: str = "  ") -> str:
    width = max((len(label) for label, _ in rows), default=0)
    lines = []
    for label, value in rows:
        pad = " " * (width + 2)
        body = str(value).replace("\n", "\n" + indent + pad)
        lines.append(f"{indent}{label.ljust(width)}  {body}")
    return "\n".join(lines) + "\n"


def split_blocks(text: str) -> tuple[str, str]:
    """``(finished, pending)``: markdown blocks that are complete, and the open tail.

    A block ends at a blank line outside a code fence, or at a closing fence.
    """
    cut = 0
    position = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        position += len(line)
        if not line.endswith("\n"):
            break  # the last, unfinished line
        stripped = line.strip()
        if fence is None and stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
        elif fence is not None and stripped.startswith(fence):
            fence = None
            cut = position
        elif fence is None and not stripped:
            cut = position
    return text[:cut], text[cut:]


class _Stream:
    """What has been streamed for the answer being written."""

    def __init__(self) -> None:
        self.text = ""  # every delta since the last model call started
        self.open = False  # a streamed line is still open (plain) / a block is pending

    def remainder(self, final: str) -> str | None:
        """What of ``final`` still needs printing: None when it was streamed already."""
        if not self.text.strip():
            return final
        shown, wanted = self.text.strip(), final.strip()
        if wanted == shown:
            return None
        if wanted.startswith(shown):
            return wanted[len(shown):]
        return final


class PlainRenderer:
    """Plain text for the line UI; streamed text is written as it arrives."""

    def __init__(self, write: Callable[[str], None], glyphs: Glyphs = ASCII) -> None:
        self.write = write
        self.glyphs = glyphs
        self._stream = _Stream()

    def _end_stream(self) -> None:
        if self._stream.open:
            if not self._stream.text.endswith("\n"):
                self.write("\n")
            self._stream.open = False

    def show(self, event: Event) -> None:
        g = self.glyphs
        if isinstance(event, TextDelta):
            if event.text:
                self.write(event.text)
                self._stream.text += event.text
                self._stream.open = True
            return
        if isinstance(event, Thinking):
            if event.active:
                self._end_stream()
                self._stream = _Stream()
            return  # no spinner in plain text
        self._end_stream()
        if isinstance(event, AssistantText):
            rest = self._stream.remainder(event.text)
            self._stream = _Stream()
            if rest is event.text:
                self.write(event.text.rstrip() + "\n")
            elif rest is not None and rest.strip():
                self.write(rest.strip("\n") + "\n")  # the streamed text missed an ending
            return
        if isinstance(event, ToolStarted):
            self._stream = _Stream()
            self.write(f"{g.tool} {event.summary}\n")
        elif isinstance(event, ToolFinished):
            state = "" if event.ok else "failed: "
            self.write(f"  {g.result} {state}{event.summary}\n")
        elif isinstance(event, SubagentActivity):
            self.write(f"  [{event.agent}] {event.text}\n")
        elif isinstance(event, Progress):
            self.write(f"  ... {event.text}\n")
        elif isinstance(event, Notice):
            prefix = {"info": "", "warn": "warning: ", "error": "error: "}[event.level]
            self.write(f"{prefix}{event.text}\n")
        elif isinstance(event, PlanCard):
            self.write(f"== {event.title} ==\n")
            self.write(plain_rows(card_rows(event.rows, g)))
            if event.footer:
                self.write(event.footer.rstrip() + "\n")

    def rows(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        self._end_stream()
        if title:
            self.write(title + "\n")
        self.write(plain_rows(rows) if rows else "  (none)\n")

    def text(self, text: str, style: str = "") -> None:
        self._end_stream()
        self.write(text if text.endswith("\n") else text + "\n")

    def diff(self, text: str) -> None:
        self.text(text)


class RichRenderer:
    """Rich output: markdown answers, compact tool lines, colored notices, a live region."""

    def __init__(self, console: RichConsole, glyphs: Glyphs = ASCII) -> None:
        self.console = console
        self.glyphs = glyphs
        self._live: Live | None = None
        self._spinning = False
        self._status_label = ""
        self._since = 0.0
        self._stream = _Stream()
        self._pending = ""  # the markdown block still being streamed
        self.status_suffix: Callable[[], str] = lambda: ""
        self.queue_lines: Callable[[], Sequence[str]] = lambda: ()

    # -- the live region --------------------------------------------------------------

    def _renderable(self) -> RenderableType:
        from rich.console import Group
        from rich.markdown import Markdown
        from rich.spinner import Spinner
        from rich.text import Text

        parts: list[RenderableType] = []
        if self._pending.strip():
            height = max(4, self.console.size.height - 6)
            lines = self._pending.splitlines()
            tail = "\n".join(lines[-height:])
            parts.append(Markdown(tail) if len(lines) <= height else Text(tail))
        if self._spinning:
            name = "dots" if self.glyphs is UNICODE else "line"
            parts.append(Spinner(name, text=Text(self._status_text(), style=STYLE["dim"]),
                                 style=STYLE["accent"]))
        for line in self.queue_lines():
            parts.append(Text(line, style=STYLE["warn"]))
        return Group(*parts)

    def _refresh(self) -> None:
        from rich.live import Live

        wanted = self._spinning or bool(self._pending.strip()) or bool(self.queue_lines())
        if not wanted:
            self._stop_live()
            return
        if self._live is None:
            self._live = Live(self._renderable(), console=self.console, transient=True,
                              refresh_per_second=8, auto_refresh=True)
            self._live.start()
        else:
            self._live.update(self._renderable(), refresh=True)

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def start_status(self, label: str) -> None:
        self._status_label = label
        if not self._spinning:
            self._since = time.monotonic()
            self._spinning = True
        self._refresh()

    def tick(self) -> None:
        if self._live is not None or self.queue_lines():
            self._refresh()

    def stop_status(self) -> None:
        """Stop the whole live region (before a question takes the screen)."""
        self._flush_pending()
        self._spinning = False
        self._stop_live()

    def _status_text(self) -> str:
        elapsed = int(time.monotonic() - self._since)
        suffix = self.status_suffix()
        extra = f"  {suffix}" if suffix else ""
        return f"{self._status_label}{self.glyphs.ellipsis} {elapsed}s{extra}  (esc to interrupt)"

    # -- streaming --------------------------------------------------------------------

    def _print_block(self, block: str) -> None:
        from rich.markdown import Markdown

        if block.strip():
            self.console.print(Markdown(block.strip("\n")))

    def _flush_pending(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self._print_block(pending)
        self._stream.open = False

    def _delta(self, text: str) -> None:
        if not text:
            return
        if not self._stream.text:
            self._spinning = False  # the answer itself is the progress now
        self._stream.text += text
        self._stream.open = True
        finished, self._pending = split_blocks(self._pending + text)
        if finished:
            self._print_block(finished)
        self._refresh()

    # -- events -----------------------------------------------------------------------

    def show(self, event: Event) -> None:
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        g = self.glyphs
        console = self.console
        if isinstance(event, TextDelta):
            self._delta(event.text)
            return
        if isinstance(event, Thinking):
            if event.active:
                self._flush_pending()
                self._stream = _Stream()
                self.start_status(f"{event.label} thinking")
            else:
                self._flush_pending()
                self._spinning = False
                self._refresh()
            return
        self._flush_pending()
        if isinstance(event, Progress):
            if self._spinning:
                self._status_label = event.text
                self._refresh()
            else:
                console.print(Text(f"  {g.ellipsis} {event.text}", style=STYLE["dim"]))
            return
        if isinstance(event, AssistantText):
            rest = self._stream.remainder(event.text)
            self._stream = _Stream()
            if rest is not None and rest.strip():
                console.print(Markdown(rest.strip("\n")))
        elif isinstance(event, ToolStarted):
            self._stream = _Stream()
            line = Text()
            line.append(f"{g.tool} ", style=STYLE["tool"])
            line.append(event.summary)
            console.print(line)
        elif isinstance(event, ToolFinished):
            style = STYLE["dim"] if event.ok else "red"
            state = "" if event.ok else "failed: "
            console.print(Text(f"  {g.result} {state}{event.summary}", style=style))
        elif isinstance(event, SubagentActivity):
            line = Text("  ")
            line.append(f"[{event.agent}] ", style=STYLE["agent"])
            line.append(event.text, style=STYLE["dim"])
            console.print(line)
        elif isinstance(event, Notice):
            style = {"info": STYLE["accent"], "warn": STYLE["warn"],
                     "error": STYLE["error"]}[event.level]
            prefix = {"info": "", "warn": f"{g.warn} ", "error": f"{g.fail} "}[event.level]
            console.print(Text(prefix + event.text, style=style))
        elif isinstance(event, PlanCard):
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold")
            table.add_column(overflow="fold")  # a long path wraps whole, never cut short
            for label, value in card_rows(event.rows, g):
                table.add_row(Text(label), Text(value))
            subtitle = Text(event.footer, style=STYLE["dim"]) if event.footer else None
            console.print(Panel(table, title=Text(event.title), subtitle=subtitle,
                                expand=False, border_style=STYLE["accent"]))

    def rows(self, title: str, rows: Sequence[tuple[str, str]]) -> None:
        from rich.table import Table
        from rich.text import Text

        self._flush_pending()
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column(overflow="fold")
        for label, value in rows:
            table.add_row(Text(label), Text(str(value)))
        if title:
            self.console.print(Text(title, style="bold"))
        if rows:
            self.console.print(table)
        else:
            self.console.print(Text("  (none)", style=STYLE["dim"]))

    def text(self, text: str, style: str = "") -> None:
        from rich.text import Text

        self._flush_pending()
        self.console.print(Text(text.rstrip("\n"), style=style))

    def diff(self, text: str) -> None:
        from rich.text import Text

        self._flush_pending()
        out = Text()
        for line in text.rstrip("\n").splitlines():
            if line.startswith(("+++", "---", "diff ", "index ")):
                style = "bold"
            elif line.startswith("+"):
                style = STYLE["ok"]
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = STYLE["accent"]
            else:
                style = ""
            out.append(line + "\n", style=style)
        self.console.print(out, end="")
