import os

from google.adk.tools.toolbox_toolset import ToolboxToolset

from apowerb.configs.th2logger import setup_logging


logger = setup_logging(__name__)


def mcp_tool_set_database_toolbox():
    """Factory: creates a ToolboxToolset connected to MCP Toolbox for Databases.

    Requires MCP_TOOLBOX_URL env var pointing to the running Toolbox server.
    Optionally set MCP_TOOLBOX_TOOLSET to load a specific toolset (default: all tools).
    """
    server_url = os.getenv("MCP_TOOLBOX_URL", "http://localhost:5000")
    toolset_name = os.getenv("MCP_TOOLBOX_TOOLSET", "")

    logger.info(f"Connecting to MCP Toolbox for Databases at {server_url}")

    kwargs = {"server_url": server_url}
    if toolset_name:
        kwargs["toolset_name"] = toolset_name

    return ToolboxToolset(**kwargs)
