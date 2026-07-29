import typer
from typing import Optional
from th2agent.agent_store.agent_manager import AgentStore
from th2agent.core.agent_main import (
    delete_agent,
    register_agent,
    fetch_agents,
    get_agent,
)
from th2agent.schema.agent_schema import AgentCreateSchema
from th2agent.core.agent_helpers.llm_model_builder import validate_agent_model
import json

app = typer.Typer()


def get_agent_store() -> AgentStore:
    """Get agent store instance."""
    agent_store = AgentStore()
    agent_store.create_table()
    return agent_store


@app.command("list")
def list_agents(
    owner: Optional[str] = typer.Option(
        None,
        "--owner",
        "-o",
        help=(
            "Filter by owner email. If omitted, lists ALL agents across "
            "every owner (admin mode — intended for CLI/ops only)."
        ),
    ),
):
    """List registered agents.

    By default (no ``--owner``), every agent in the store is listed —
    use this on the VM to inspect what is deployed across tenants.
    Pass ``--owner user@example.com`` to scope to a single owner.
    """
    # Touch the store once so the table is created if the CLI is the
    # very first thing run against a fresh database.
    get_agent_store()
    agents = fetch_agents(user_id=owner)

    if not agents:
        scope = f"for owner '{owner}'" if owner else "in the store"
        typer.echo(f"No agents found {scope}.")
        return

    header = (
        f"Registered Agents (owner='{owner}'):"
        if owner
        else "Registered Agents (all owners):"
    )
    typer.echo(header)
    typer.echo("-" * 50)
    for agent in agents:
        typer.echo(f"ID:          {agent.get('agent_id', 'N/A')}")
        typer.echo(f"Name:        {agent.get('agent_name', 'N/A')}")
        typer.echo(f"Owner:       {agent.get('owner_id', 'N/A')}")
        typer.echo(f"Model:       {agent.get('agent_model', 'N/A')}")
        typer.echo(f"Template:    {agent.get('superagent_template_id', 'N/A')}")
        typer.echo(f"Description: {agent.get('agent_description', 'N/A')}")
        typer.echo("-" * 50)


@app.command("create")
def create_agent(
    name: str = typer.Option(..., "--name", "-n", help="Agent name"),
    description: str = typer.Option(
        "", "--description", "-d", help="Agent description"
    ),
    model: str = typer.Option("gpt-4", "--model", "-m", help="Model to use"),
    system_prompt: str = typer.Option(
        ..., "--system-prompt", "-s", help="System prompt"
    ),
    tools: Optional[str] = typer.Option(
        None, "--tools", "-t", help="Comma-separated list of tool names"
    ),
):
    """Create a new agent."""
    tool_list = []
    if tools:
        tool_list = [tool.strip() for tool in tools.split(",")]

    agent_data = AgentCreateSchema(
        agent_name=name,
        agent_description=description,
        agent_model=model,
        agent_instruction=system_prompt,
        agent_tools=tool_list,
        organization_id="default",
        project_id="default",
        owner_id="default",
    )

    try:
        validate_agent_model(
            agent_data.agent_model, agent_data.agent_model_params, agent_data.agent_type
        )
        register_agent(agent_data)
        typer.echo(f"Agent '{name}' created successfully.")
    except Exception as e:
        typer.echo(f"Error creating agent: {e}", err=True)
        raise typer.Exit(1)


@app.command("get")
def get_agent_info(
    agent_id: str = typer.Argument(..., help="Agent ID to retrieve"),
):
    """Get information about a specific agent."""
    agent = get_agent(int(agent_id.replace("agent", "")))

    if not agent:
        typer.echo(f"Agent with ID '{agent_id}' not found.", err=True)
        raise typer.Exit(1)

    typer.echo("Agent Information:")
    typer.echo("-" * 50)
    typer.echo(json.dumps(agent, indent=2))


@app.command("delete")
def delete_agent_cmd(
    agent_id: str = typer.Argument(..., help="Agent ID to delete"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force deletion without confirmation"
    ),
):
    """Delete an agent."""
    if not force:
        confirm = typer.confirm(f"Are you sure you want to delete agent '{agent_id}'?")
        if not confirm:
            typer.echo("Deletion cancelled.")
            return

    try:
        delete_agent(agent_id)
        typer.echo(f"Agent '{agent_id}' deleted successfully.")
    except Exception as e:
        typer.echo(f"Error deleting agent: {e}", err=True)
        raise typer.Exit(1)


@app.command("export")
def export_agents_cmd(
    owner: str = typer.Option(..., "--owner", "-o", help="Owner email to export agents for"),
    s3_prefix: str = typer.Option("seeds/", "--prefix", "-p", help="S3 prefix (default: seeds/)"),
):
    """Export all agents for a user to YAML seed files on S3."""
    from th2agent.core.agent_seeds import export_agents

    try:
        written = export_agents(owner_id=owner, s3_prefix=s3_prefix)
        if not written:
            typer.echo(f"No agents found for '{owner}'.")
            return
        typer.echo(f"Exported {len(written)} file(s) to S3:")
        for key in written:
            typer.echo(f"  {key}")
    except Exception as e:
        typer.echo(f"Error exporting agents: {e}", err=True)
        raise typer.Exit(1)


@app.command("import")
def import_agents_cmd(
    s3_prefix: str = typer.Option("seeds/", "--prefix", "-p", help="S3 prefix to import from"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without creating anything"),
):
    """Import agents from YAML seed files stored on S3."""
    from th2agent.core.agent_seeds import import_agents

    try:
        result = import_agents(s3_prefix=s3_prefix, dry_run=dry_run)

        if result["created"]:
            typer.echo(f"Created {len(result['created'])} agent(s):")
            for name in result["created"]:
                typer.echo(f"  + {name}")

        if result["skipped"]:
            typer.echo(f"Skipped {len(result['skipped'])} agent(s) (already exist):")
            for name in result["skipped"]:
                typer.echo(f"  = {name}")

        if result["errors"]:
            typer.echo(f"Errors ({len(result['errors'])}):", err=True)
            for err in result["errors"]:
                typer.echo(f"  ! {err}", err=True)
            raise typer.Exit(1)

        if not result["created"] and not result["skipped"]:
            typer.echo("No seed files found on S3.")
    except SystemExit:
        raise
    except Exception as e:
        typer.echo(f"Error importing agents: {e}", err=True)
        raise typer.Exit(1)
