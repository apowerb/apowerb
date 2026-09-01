from pydantic import BaseModel, field_validator

# Credential-bearing keys: pasted by hand, so the same whitespace accident
# applies to them as to the model name. Module level on purpose — a
# leading-underscore name inside a pydantic v2 model body becomes a private
# attribute, not a plain tuple.
_CREDENTIAL_KEYS = ("model_api_key", "model_api_base")


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server attached to an agent.

    Supports two transports:
    - 'http': Streamable HTTP (url + headers + params injected as query string)
    - 'stdio': Local process (command + args + env vars)

    The optional ``mcp_type`` field carries semantic info propagated from
    the saved MCP config:
    - "toolbox-db": MCP Toolbox for Databases (uses ToolboxToolset, not the
      generic McpToolset). ``toolset`` selects a named toolset from the
      toolbox (empty = all tools).
    - "" / unset: standard MCP HTTP/SSE/Stdio server.
    """

    name: str = ""
    transport: str = "http"  # "http" or "stdio"
    # HTTP transport fields
    url: str = ""
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    # Stdio transport fields
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    # Semantic type — controls which ADK Toolset class is instantiated
    mcp_type: str | None = None
    toolset: str | None = None
    # Optional DB metadata (used when mcp_type == "toolbox-db") — surfaced in
    # the system prompt preamble so the LLM refers to the database by its
    # actual name (e.g. "PMI") instead of the wrapper config name.
    db_type: str | None = None
    db_database: str | None = None


class AgentCreateSchema(BaseModel):
    """Schema for creating a new agent.
    Attributes:
        name: str - The name of the agent.
        model: str - The model used by the agent.
        description: str - A brief description of the agent.
        instruction: str - Instructions for the agent's behavior.
        tools: list[str] - A list of tool paths used by the agent.
        input_schema: dict | None - The input schema for the agent (optional).
        output_schema: dict | None - The output schema for the agent (optional).
        organization_id: str - The ID of the organization the agent belongs to.
        project_id: str - The ID of the project the agent is associated with.
        owner_id: str - The ID of the owner of the agent.
        tags: list[str] | None - A list of tags associated with the agent (optional).
    """

    agent_name: str
    agent_model: str
    agent_model_params: dict | None = None

    @field_validator("agent_model", mode="before")
    @classmethod
    def _strip_agent_model(cls, value):
        """Normalise the model name at the boundary: what is validated must be
        what is stored.

        Measured on 2026-09-01: an agent saved as ``"ovhcloud/Qwen3.5-9B "``
        passed ``validate_agent_model`` and then failed *every* call with
        ``model_not_found: The model `Qwen3.5-9B ` does not exist``. The guard
        checks ``agent_model.strip()`` (llm_model_builder.py) while the raw
        value is the one persisted — a guard that normalises to verify and lets
        the original through guards nothing. Nothing warned at save time: from
        the guard's point of view the model was valid.
        """
        return value.strip() if isinstance(value, str) else value

    @field_validator("agent_model_params", mode="before")
    @classmethod
    def _strip_credentials(cls, value):
        """Same accident, one field over.

        An API key or an endpoint copied with a trailing newline is never
        intentional, and it fails the same silent way — an authentication
        error that names neither the cause nor the field. Only the two
        credential keys are touched; every other parameter is left verbatim.
        """
        if not isinstance(value, dict):
            return value
        return {
            key: (item.strip() if key in _CREDENTIAL_KEYS and isinstance(item, str) else item)
            for key, item in value.items()
        }
    agent_description: str
    agent_instruction: str
    agent_tools: list[str] | None = []
    input_schema: dict | None = None
    output_schema: dict | None = None
    output_key: str | None = None
    output_schema_name: str | None = None
    skip_when_upstream: str | None = None
    sub_agents: list[str] | None = None
    agent_type: str
    # organization_id: Optional[str]
    # owner_id: Optional[EmailStr]
    project_id: str | None = "thaink2"
    code_executor: str | None = None
    tags: list[str] | None = None
    guardrails_config: dict | None = None
    memory_enabled: bool = False
    artifacts_enabled: bool = False
    superagent_template_id: str | None = None
    loop_max_iterations: int | None = None
    loop_exit_instruction: str | None = None
    mcp_servers: list[McpServerConfig] | None = None
    agent_skills: list[str] | None = None
    hub_origin_id: str | None = None
    propagate_api_key: bool = False
