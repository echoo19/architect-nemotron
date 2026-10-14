"""A Model Context Protocol client (Phase 13, Package D).

Servers configured in ``config.toml`` (``[mcp.servers.<name>]``) or ``.mcp.json`` run over
stdio (spawned) or streamable HTTP; their tools become conductor tools named
``mcp__<server>__<tool>``. The CLI calls ``mcp_tools(workspace, cwd)`` once to bridge them
and ``close_mcp()`` on exit.
"""

from .client import CallResult, MCPClient, ServerInfo, ToolInfo
from .config import (
    ConfigError,
    McpConfig,
    ServerConfig,
    add_server,
    config_paths,
    load_config,
    read_toml,
    remove_server,
    set_enabled,
)
from .manager import (
    MCPManager,
    ServerState,
    close_mcp,
    close_mcp_now,
    manager_for,
    mcp_tools,
    tool_name,
)
from .protocol import PROTOCOL_VERSION, SUPPORTED_VERSIONS, MCPError, RPCError
from .transport import HttpTransport, StdioTransport

__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "CallResult",
    "ConfigError",
    "HttpTransport",
    "MCPClient",
    "MCPError",
    "MCPManager",
    "McpConfig",
    "RPCError",
    "ServerConfig",
    "ServerInfo",
    "ServerState",
    "StdioTransport",
    "ToolInfo",
    "add_server",
    "close_mcp",
    "close_mcp_now",
    "config_paths",
    "load_config",
    "manager_for",
    "mcp_tools",
    "read_toml",
    "remove_server",
    "set_enabled",
    "tool_name",
]
