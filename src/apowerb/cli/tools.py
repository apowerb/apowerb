import typer
from apowerb.tools_store.tool_manager import get_tools_store

app = typer.Typer()


@app.command("list")
def list_tools():
    """List all available tools."""
    tools_store = get_tools_store()
    tools = tools_store.get_all_tools()

    if not tools:
        typer.echo("No tools found.")
        return

    typer.echo("Available Tools:")
    typer.echo("-" * 50)
    for tool in tools:
        typer.echo(f"Name: {tool.get('name', 'N/A')}")
        typer.echo(f"Description: {tool.get('description', 'N/A')}")
        typer.echo(f"Category: {tool.get('category', 'N/A')}")
        typer.echo("-" * 50)
