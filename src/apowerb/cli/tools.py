import typer
from apowerb.tools_store.tool_manager import get_tools_store

app = typer.Typer()


@app.command("list")
def list_tools():
    """List all available tools, grouped by category."""
    tools_store = get_tools_store()
    # {category: [tool names]} -- the names an agent's tool list refers to.
    tools_by_category = {
        category: names
        for category, names in tools_store.get_all_tools().items()
        if names
    }

    if not tools_by_category:
        typer.echo("No tools found.")
        return

    typer.echo("Available Tools:")
    typer.echo("-" * 50)
    for category, names in tools_by_category.items():
        typer.echo(f"Category: {category}")
        for name in names:
            typer.echo(f"  {name}")
        typer.echo("-" * 50)
