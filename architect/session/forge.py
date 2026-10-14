"""The session verb ``forge`` and the ``forge report`` table (Phase 9 Section 12).

``forge on | off | status | budget <usd> <minutes> | model <name> | mode agent|template``
edits this session's ``CompileConfig.forge`` only: never the Spec (its hash is
unchanged) and never the profile file. ``forge on`` shows the summed compile +
Forge cap and asks with the session's usual confirmation; a Forge compile then
raises the outer wallet by exactly that Forge cap (``Compiler._outer_budget``).

Everything printed is counts, dollars, seconds, codes, digests and sizes. No
source, prompt, case, reply or note text ever reaches this module: ``forge.json``
is content-free by contract, and its values are re-checked here before they are
drawn (anything unexpected prints ``-``). Tuning counts are evidence, never a
license; children are always shown with their ``case_origin``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError

from architect.compiler.types import ForgeSettings
from architect.profiles import Profile, forge_settings_of

from . import render
from .codes import SessionError
from .commands import Command

if TYPE_CHECKING:
    from .session import Session

__all__ = [
    "ENABLE_PROMPT",
    "REPORT_HEADER",
    "cap_line",
    "effective",
    "forge_report_table",
    "handle_forge",
    "read_forge_record",
    "status_lines",
    "spec_verifications",
]

ENABLE_PROMPT = "confirm> enable Forge for compiles in this session? [y/N] "
REPORT_HEADER = "tuning evidence, not a license"
WIDTH = 100
_FORGE_JSON_LIMIT = 1_048_576
_MODES = ("agent", "template")


# ------------------------------------------------------------------ settings


def effective(session: Session) -> ForgeSettings:
    """This session's Forge settings: its own override, else the profile's."""
    override = session.forge_override
    if override is not None:
        return override
    return forge_settings_of(session._profile().compile)


def _known_model(profile: Profile, name: str) -> bool:
    agent = profile.forge_agent
    return name in profile.models or (agent is not None and agent.model == name)


def _money(value: float) -> str:
    return f"${value:.2f}"


def _minutes(seconds: float) -> str:
    return f"{seconds / 60.0:g}"


def spec_verifications(session: Session) -> int | None:
    """The per-spec verification count (Section 10.7) for the Spec under review."""
    draft = session.draft
    if draft is None:
        return None
    counter = getattr(session._services().compiler.library, "verification_count", None)
    if not callable(counter):
        return 0
    try:
        value = counter(draft.spec.spec_hash)
    except (OSError, ValueError):
        return None
    return value if type(value) is int and value >= 0 else None


def cap_line(compile_usd: float, settings: ForgeSettings, verifications: int | None) -> str:
    """The summed-cap line of Section 12.2 (the CLI prints the same text)."""
    count = "-" if verifications is None else str(verifications)
    return (
        f"forge: compile cap {_money(compile_usd)} + forge cap {_money(settings.max_usd)} = "
        f"{_money(compile_usd + settings.max_usd)} (API-equivalent, not a bill); "
        f"forge minutes {_minutes(settings.max_s)}; verifications for this spec so far: "
        f"{count}/{settings.max_verifications_per_spec}"
    )


def status_lines(settings: ForgeSettings, compile_usd: float, verifications: int | None) -> str:
    agent = settings.agent_model or "(ladder model)"
    lines = [
        f"forge {'on' if settings.enabled else 'off'}  mode {settings.mode}  "
        f"trigger {settings.trigger}  agent {agent if settings.mode == 'agent' else '-'}",
        f"  caps: {_money(settings.max_usd)}, {_minutes(settings.max_s)} min, "
        f"measures {settings.max_measures}, submissions {settings.max_submissions}, "
        f"spawns {settings.max_spawns}",
    ]
    if settings.enabled:
        lines.append("  " + cap_line(compile_usd, settings, verifications))
    return "".join(render.wrap(line) + "\n" for line in lines)


def _update(session: Session, **values: object) -> ForgeSettings:
    try:
        return ForgeSettings.model_validate({**effective(session).model_dump(), **values})
    except ValidationError:
        raise SessionError("forge_budget_invalid") from None


def _finite_positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise SessionError("forge_budget_invalid") from None
    if not math.isfinite(value) or value <= 0:
        raise SessionError("forge_budget_invalid")
    return value


async def handle_forge(session: Session, cmd: Command) -> None:
    """Dispatch ``forge <sub> ...``; every failure is a fixed ``forge_*`` code."""
    args = cmd.args
    sub = args[0].lower() if args else "status"
    profile = session._profile()
    compile_usd = profile.compile.compile_budget.cost_usd
    if sub == "status" and len(args) <= 1:
        count = spec_verifications(session)
        session._write(status_lines(effective(session), compile_usd, count))
    elif sub == "off" and len(args) == 1:
        session.forge_override = _update(session, enabled=False)
        session.forge_confirmed = False
        session._line("forge off: Stage 3 is the ordinary rewrite path")
    elif sub == "on" and len(args) == 1:
        if session._services().compiler.smoke_factory is None:
            # Forge runs only with a smoke floor (Section 8.2); it would silently not run.
            raise SessionError("forge_disabled")
        settings = _update(session, enabled=True)
        session._line(cap_line(compile_usd, settings, spec_verifications(session)))
        if await session._confirm(ENABLE_PROMPT, default=False):
            session.forge_override = settings
            session.forge_confirmed = True
            session._line(
                f"forge on: mode {settings.mode}, trigger {settings.trigger}; "
                "the Spec and its hash are unchanged"
            )
        else:
            session._line("forge not enabled")
    elif sub == "budget" and len(args) == 3:
        usd = _finite_positive(args[1])
        minutes = _finite_positive(args[2])
        settings = _update(session, max_usd=usd, max_s=minutes * 60.0)
        session.forge_override = settings
        session._line(
            f"forge budget {_money(settings.max_usd)} / {_minutes(settings.max_s)} min "
            "(API-equivalent, not a bill)"
        )
        if settings.enabled:
            session._line(cap_line(compile_usd, settings, spec_verifications(session)))
    elif sub == "model" and len(args) == 2:
        if not _known_model(profile, args[1]):
            raise SessionError("forge_model_unknown")
        session.forge_override = _update(session, agent_model=args[1])
        session._line(f"forge model {args[1]}")
    elif sub == "mode" and len(args) == 2 and args[1].lower() in _MODES:
        session.forge_override = _update(session, mode=args[1].lower())
        session._line(f"forge mode {args[1].lower()}")
    elif sub == "run" and len(args) >= 2:
        from .delivery import forge_run_verb

        await forge_run_verb(session, cmd)
    elif sub == "integrate":
        from .delivery import forge_integrate_verb

        await forge_integrate_verb(session, cmd)
    else:
        session._fail(
            "not_a_command",
            hint="forge on | forge off | forge status | forge budget <usd> <minutes> | "
            "forge model <name> | forge mode agent|template | forge run {json} | "
            "forge integrate <repo> <into>",
        )


# ------------------------------------------------------------------ forge.json


def read_forge_record(directory: Path) -> dict[str, JsonValue] | None:
    """The artifact's ``forge.json`` as a JSON object, or None when missing or invalid."""
    path = Path(directory) / "forge.json"
    try:
        if not path.is_file() or path.stat().st_size > _FORGE_JSON_LIMIT:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe(value: object, width: int = 24) -> str:
    """Only numbers, booleans and short code-like strings are drawn; anything else is ``-``."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
    if isinstance(value, str) and value and all(
        ch.isascii() and (ch.isalnum() or ch in "._:-") for ch in value
    ):
        return value if len(value) <= width else value[: width - 1] + "~"
    return "-"


def _pairs(record: Mapping[str, object], names: tuple[str, ...]) -> list[str]:
    return [f"{name} {_safe(record.get(name))}" for name in names]


def _pack(cells: list[str], indent: str = "  ") -> list[str]:
    lines: list[str] = []
    current = ""
    for cell in cells:
        candidate = f"{current}  {cell}" if current else cell
        if current and len(indent) + len(candidate) > WIDTH:
            lines.append(indent + current)
            current = cell
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def _total(rows: list[dict[str, object]], name: str) -> int:
    return sum(
        value
        for value in (row.get(name) for row in rows)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def _turn_lines(rows: list[dict[str, object]]) -> list[str]:
    """One line over the per-turn rows: token totals, cache and reasoning shares, cuts."""
    prompt, completion = _total(rows, "prompt_tokens"), _total(rows, "completion_tokens")
    cached, reasoning = _total(rows, "cached_tokens"), _total(rows, "reasoning_tokens")
    cut = sum(1 for row in rows if row.get("stop") == "max_tokens")
    failed = sum(1 for row in rows if row.get("ok") is False)
    return _pack(
        [
            f"turns {len(rows)}",
            f"prompt {prompt} (cached {_share(cached, prompt)})",
            f"completion {completion} (reasoning {_share(reasoning, completion)})",
            f"cut {cut}",
            f"failed {failed}",
        ]
    )


def forge_report_table(record: Mapping[str, object]) -> str:
    """``forge.json`` as an ASCII report of at most 100 columns (Section 12.2)."""
    lines = [
        f"forge report  language {_safe(record.get('language'))}  mode "
        f"{_safe(record.get('mode'))}  agent {_safe(record.get('agent_model'), 40)}  play "
        f"{_safe(record.get('play_model'), 40)}"
    ]
    lines.append(REPORT_HEADER)
    lines.extend(
        _pack(
            _pairs(
                record, ("checks", "dryruns", "measures", "submissions", "accepted", "spawns")
            )
        )
    )
    lines.append("build-time agent (API-equivalent, not a bill; never part of play cost)")
    cost = record.get("agent_cost_usd")
    wall = record.get("wall_s")
    lines.extend(
        _pack(
            [
                f"agent ${_safe(cost)}",
                *_pairs(record, ("agent_tokens_in", "agent_tokens_out")),
                f"wall_s {_safe(wall)}",
            ]
        )
    )
    turns = record.get("turns")
    if isinstance(turns, list) and turns:
        lines.extend(_turn_lines([row for row in turns if isinstance(row, dict)]))
    lines.append("harness and verification")
    lines.extend(
        _pack(
            _pairs(
                record,
                ("harness_nodes", "harness_bytes", "stage4_calls", "verifications_for_spec"),
            )
        )
    )
    pack = _safe(record.get("pack_sha256"), 64)
    if pack != "-":
        lines.append(f"forge pack {pack}")
    notes = record.get("notes")
    if isinstance(notes, list):
        lines.append(f"notes {len(notes)} (sizes and digests only)")
        for note in notes:
            if isinstance(note, dict):
                digest = _safe(note.get("sha256"), 64)
                lines.append(f"  chars {_safe(note.get('chars'))}  sha256 {digest[:16]}")
    children = record.get("children")
    if isinstance(children, list) and children:
        lines.append(
            f"children {len(children)} (a child license is weaker than a parent license)"
        )
        for child in children:
            if isinstance(child, dict):
                digest = _safe(child.get("artifact_digest"), 64)
                lines.append(
                    f"  {_safe(child.get('name'), 32)}  case_origin "
                    f"{_safe(child.get('case_origin'))}  licensed "
                    f"{_safe(child.get('licensed'))}  digest {digest[:16]}"
                )
    lines.append("only Stage 4 of the parent is a license; tuning counts above are evidence")
    return "\n".join(line[:WIDTH] for line in lines) + "\n"
