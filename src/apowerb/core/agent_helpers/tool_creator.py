"""``create_tool`` — lets any agent build a new tool for its user.

The tool is a workflow whose leading trigger is ``kind:"agent_tool"``: it is
written, then published at once, so it resolves as ``workflow:<tool_name>``
for the agents of the SAME user (see ``core.workflow_agent_tools``). Nothing
new is executed here: the workflow engine runs the graph with its existing
guards (graph validation, SSRF check on ``http`` nodes, forbidden auth
headers, 120 s cap, anti-recursion chain).

The owner is the user of the current invocation (``invocation_context``),
never the process-global ``AGENT_OWNER``: under load, that variable can
name the owner of another agent.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from apowerb.core import workflow_main
from apowerb.core.invocation_context import get_current_invoker
from apowerb.core.workflow_engine import WorkflowUserError

logger = getLogger(__name__)

_TRIGGER_ID = "start"


def _invalid(*errors: str) -> dict:
    return {"error": "invalid_tool", "errors": list(errors)}


def create_tool(
    tool_name: str,
    description: str,
    input_schema: list[dict],
    nodes: list[dict],
    edges: list[dict],
) -> dict:
    """Create a new reusable tool, published immediately for the current user.

    The tool is a workflow. Its entry node is added for you with id "start";
    read the tool's arguments in node configs as {{start.<argument_name>}} and
    the output of another node as {{<node_id>}} or {{<node_id>.<field>}}.
    Do not add a node of type "trigger".

    Useful node types (config keys):
    - http: method (GET|POST|PUT|PATCH|DELETE), url, headers [{key, value}],
      body, timeout_s. Private network URLs and auth headers are refused.
    - agent: agent_id, input (message sent to that agent).
    - rag: agent_id, input (query), top_k.
    - set, convert, condition, router, loop, extract, merge.
    - output: value (what the tool returns).

    Args:
        tool_name: lowercase snake_case, 3 to 41 characters, unique for the user.
        description: what the tool does, shown to agents that use it.
        input_schema: the tool's arguments, e.g.
            [{"name": "city", "type": "string", "required": true}];
            type is string, number, boolean, array or object.
        nodes: [{"id": "call", "type": "http", "config": {...}}, ...]
        edges: [{"source": "start", "target": "call"}, ...]

    Returns:
        {"status": "created", "tool": "workflow:<tool_name>", ...} or
        {"error": ..., "errors": [...]} explaining what to fix.
    """
    owner_id = get_current_invoker()
    if not owner_id or "@" not in owner_id:
        return {
            "error": "no_user",
            "message": "No signed-in user for this conversation: tools can "
            "only be created from a user's chat.",
        }

    nodes = list(nodes or [])
    if any(isinstance(n, dict) and n.get("type") == "trigger" for n in nodes):
        return _invalid(
            'Do not add a "trigger" node: the entry node "start" is added for you.'
        )
    if any(isinstance(n, dict) and n.get("id") == _TRIGGER_ID for n in nodes):
        return _invalid(f'The node id "{_TRIGGER_ID}" is reserved for the entry node.')

    graph: dict[str, Any] = {
        "version": 1,
        "nodes": [
            {
                "id": _TRIGGER_ID,
                "type": "trigger",
                "config": {
                    "kind": "agent_tool",
                    "tool_name": tool_name,
                    "description": description,
                    "input_schema": list(input_schema or []),
                },
            },
            *nodes,
        ],
        "edges": list(edges or []),
    }

    report = workflow_main.check_workflow(graph, owner_id=owner_id)
    if not report["valid"]:
        return _invalid(*report["errors"])

    workflow = workflow_main.create_workflow(
        owner_id=owner_id,
        name=f"Tool {tool_name}",
        graph=graph,
        description=description,
    )
    try:
        workflow_main.update_workflow(
            workflow["workflow_id"],
            owner_id=owner_id,
            expected_version=workflow["version"],
            status="published",
        )
    except (workflow_main.InvalidWorkflow, WorkflowUserError) as exc:
        # Checked just above, so only a concurrent write gets here: leave no
        # half-created draft holding the tool name.
        workflow_main.delete_workflow(workflow["workflow_id"], owner_id=owner_id)
        return _invalid(str(exc))

    logger.info(
        "[create_tool] %s published workflow:%s (workflow_id=%s)",
        owner_id,
        tool_name,
        workflow["workflow_id"],
    )
    return {
        "status": "created",
        "tool": f"workflow:{tool_name}",
        "workflow_id": workflow["workflow_id"],
        "message": f"Published. Agents of this user can use it by adding "
        f"workflow:{tool_name} to their tools; it can be edited in the "
        f"Workflow Studio.",
    }
