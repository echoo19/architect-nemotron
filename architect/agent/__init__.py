"""Architect as a terminal agent that builds agents (Phase 12).

``Conductor`` is the agent the user talks to; ``events`` is its front-end interface;
``tools`` are what it can do (it never writes files itself; the coder subagent does).
"""

from .conductor import Conductor, ConductorSettings, Mode, TurnResult, project_instructions
from .events import UI, RecordingUI
from .tools import Tool, ToolContext, ToolError, default_tools
from .transcript import Transcript

__all__ = [
    "UI",
    "Conductor",
    "ConductorSettings",
    "Mode",
    "RecordingUI",
    "Tool",
    "ToolContext",
    "ToolError",
    "Transcript",
    "TurnResult",
    "default_tools",
    "project_instructions",
]
