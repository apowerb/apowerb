import os

# adk libs
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPServerParams

# th2agent libs
from apowerb.configs.th2logger import setup_logging


logger = setup_logging(__name__)


def mcp_tool_set_tavily():
    """Factory: creates a fresh McpToolset with current API key."""
    api_key = os.getenv("TAVILY_API_KEY")
    return McpToolset(
        connection_params=StreamableHTTPServerParams(
            url=f"https://mcp.tavily.com/mcp/?tavilyApiKey={api_key}",
            headers={},
        ),
    )


def tool_web_search(provider: str = "tavily", query: str = "") -> dict:
    """
    Search the web using the configured provider.
    Returns search results or a safe fallback dict on any error.
    The TAVILY_API_KEY is loaded automatically from tool_config params
    """
    if provider == "tavily":
        try:
            from tavily import TavilyClient

            TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
            if not TAVILY_API_KEY:
                logger.warning("[tool_web_search] TAVILY_API_KEY not set — check tool config params")
                return {"results": [], "error": "TAVILY_API_KEY not configured", "fallback": True}

            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(query=query, search_depth="basic")
            logger.info(f"[tool_web_search] Search OK for query: {query[:80]}")
            return response

        except Exception as e:
            logger.warning(f"[tool_web_search] Search failed (quota or error): {e}")
            return {
                "results": [],
                "error": str(e),
                "fallback": True,
            }
    else:
        logger.warning(f"[tool_web_search] Unsupported provider: {provider}")
        return {"results": [], "error": f"Unsupported provider: {provider}", "fallback": True}
