import pkgutil
import importlib
import inspect
import re
import th2agent.tools_store.portfolio as ts
from pydantic import BaseModel
from logging import getLogger

logger = getLogger(__name__)

# System-level env vars that are injected by the runtime, not user-configurable.
# This includes both agent-runtime vars and integration-managed vars — any env var
# that is loaded automatically from the integrations table via _ensure_integration_tokens()
# must be listed here so it is never surfaced as a user-fillable parameter in the UI.
_SYSTEM_ENV_VARS = frozenset(
    {
        # ── Agent runtime ────────────────────────────────────────────────────
        "AGENT_OWNER",
        "ROOT_AGENT_ID",
        "AGENT_FOLDER",
        "AGENT_ID",
        # ── Microsoft Outlook (microsoft_outlook integration row) ─────────────
        "OUTLOOK_REFRESH_TOKEN",
        "OUTLOOK_CLIENT_ID",
        "OUTLOOK_CLIENT_SECRET",
        "OUTLOOK_TENANT_ID",
        # ── Microsoft Teams (microsoft_teams integration row) ─────────────────
        "TEAMS_REFRESH_TOKEN",
        "TEAMS_CLIENT_ID",
        "TEAMS_CLIENT_SECRET",
        "TEAMS_TENANT_ID",
        # ── Microsoft OneDrive (microsoft_onedrive integration row) ───────────
        "ONEDRIVE_REFRESH_TOKEN",
        "ONEDRIVE_CLIENT_ID",
        "ONEDRIVE_CLIENT_SECRET",
        "ONEDRIVE_TENANT_ID",
        # ── Microsoft SharePoint (microsoft_sharepoint integration row) ───────
        "SHAREPOINT_REFRESH_TOKEN",
        "SHAREPOINT_CLIENT_ID",
        "SHAREPOINT_CLIENT_SECRET",
        "SHAREPOINT_TENANT_ID",
    }
)


def get_tools_store():
    """Get the tools store instance."""
    return ToolsStore()


class ToolsStore(BaseModel):
    """Class to manage the tool store."""

    tool_store: str = "thaink2_tools_store"
    categories: list[str] = []

    def get_categories(self):
        """Return the list of tool categories."""
        self.categories = [
            name
            for _, name, _ in pkgutil.iter_modules(ts.__path__)
            if name != "tool_manager"
        ]
        return self.categories

    def get_tools_in_category(self, category: str):
        """Return the list of functions (tools) available in a specific category."""
        try:
            module = importlib.import_module(
                f"th2agent.tools_store.portfolio.{category}"
            )
            functions = [
                f"{category}.{name}"
                for name, obj in inspect.getmembers(module, inspect.isfunction)
                if name.startswith("tool_")
            ]
            mcp_toolsets = [
                f"{category}.{name}"
                for name, obj in inspect.getmembers(module)
                if name.startswith("mcp_tool_set_") and not inspect.isfunction(obj)
            ]
            mcp_factories = [
                f"{category}.{name}"
                for name, obj in inspect.getmembers(module, inspect.isfunction)
                if name.startswith("mcp_tool_set_")
            ]
            return functions + mcp_toolsets + mcp_factories
        except ImportError:
            return []

    def get_all_tools(self):
        """Return a dictionary with categories as keys and lists of tools as values."""
        all_tools = {}
        for category in self.get_categories():
            all_tools[category] = self.get_tools_in_category(category)
        # Overlay tools (e.g. SCEI) live outside th2agent.tools_store.portfolio;
        # surface them so the resolver can find them by name.
        try:
            from th2agent.core.extensions.registry import registry as _ext_registry
            _ovl = list(_ext_registry.overlay_tools().keys())
            if _ovl:
                all_tools["overlay"] = _ovl
        except Exception:
            pass
        return all_tools

    def get_tool_expected_params(self, tool_name: str) -> list[dict]:
        """Extract expected configuration parameters (env vars) from a tool module.

        Parses the module source code for os.getenv() calls and returns a
        deduplicated list of parameter descriptors, filtering out system-level
        env vars that are injected by the runtime.

        Args:
            tool_name: Dotted tool name like "database.tool_run_sql".

        Returns:
            List of dicts with "key" and "default" fields, e.g.:
            [{"key": "DB_HOST", "default": "localhost"}, {"key": "DB_NAME", "default": None}]
        """
        # tool_name format: "category.tool_function_name" — we only need the category
        category = tool_name.split(".")[0]
        try:
            module = importlib.import_module(
                f"th2agent.tools_store.portfolio.{category}"
            )
        except ImportError:
            logger.warning("Could not import tool module for category: %s", category)
            return []

        try:
            source = inspect.getsource(module)
        except (OSError, TypeError):
            logger.warning("Could not read source for module: %s", category)
            return []

        # Regex to match os.getenv("KEY") and os.getenv("KEY", "default")
        # Handles single/double quotes and optional whitespace (including newlines)
        pattern = re.compile(
            r"""os\.getenv\(\s*"""
            r"""(['"])(\w+)\1"""  # group 1: quote char, group 2: env var key
            r"""(?:\s*,\s*"""  # optional comma + default value
            r"""(['"])(.*?)\3)?"""  # group 3: quote char, group 4: default value
            r"""\s*\)""",
            re.DOTALL,
        )

        seen: set[str] = set()
        params: list[dict] = []

        for match in pattern.finditer(source):
            key = match.group(2)
            default_value = match.group(4)  # None if no default group matched

            if key in _SYSTEM_ENV_VARS:
                continue
            if key in seen:
                continue

            seen.add(key)
            params.append({"key": key, "default": default_value})

        return params

    def get_all_tools_docs(self) -> dict:
        """Return documentation for all tools: name, docstring, parameters, expected env vars."""
        docs = {}
        for category in self.get_categories():
            try:
                module = importlib.import_module(
                    f"th2agent.tools_store.portfolio.{category}"
                )
            except ImportError:
                continue

            # Get module-level docstring as category description
            category_doc = inspect.getdoc(module) or ""

            tools_list = []
            for name, func in inspect.getmembers(module, inspect.isfunction):
                if not name.startswith("tool_"):
                    continue

                doc = inspect.getdoc(func) or "No description available."
                # Parse the docstring to extract description before Args:/Returns:
                description = doc.split("\nArgs:")[0].split("\nargs:")[0].strip()

                # Extract function signature
                sig = inspect.signature(func)
                params = []
                for param_name, param in sig.parameters.items():
                    if param.annotation is inspect.Parameter.empty:
                        type_str = "str"
                    else:
                        ann = param.annotation
                        # Classes expose __name__; generics/strings/Unions don't.
                        type_str = getattr(ann, "__name__", None) or str(ann)
                    p = {
                        "name": param_name,
                        "type": type_str,
                    }
                    if param.default != inspect.Parameter.empty:
                        p["default"] = (
                            str(param.default) if param.default is not None else None
                        )
                    params.append(p)

                tools_list.append(
                    {
                        "name": f"{category}.{name}",
                        "function_name": name,
                        "description": description,
                        "full_docstring": doc,
                        "parameters": params,
                        "env_vars": self.get_tool_expected_params(
                            f"{category}.{name}"
                        ),
                    }
                )

            if tools_list:
                docs[category] = {
                    "category": category,
                    "description": category_doc,
                    "tools": tools_list,
                }

        return docs