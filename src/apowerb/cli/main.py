import importlib.metadata
from typing import Optional

import typer
import uvicorn
from apowerb.cli.agents import app as agents_app
from apowerb.cli.tools import app as tools_app
from apowerb.cli.runs import app as runs_app
from apowerb.cli.config import app as config_app

app = typer.Typer()

api_host = "127.0.0.1"
api_port = 8000


def version_callback(value: bool) -> None:
    if value:
        typer.echo(importlib.metadata.version("apowerb"))
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        help="Print installed apowerb version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """apowerb CLI."""
    pass



@app.command()
def serve(
    host: str = typer.Option(
        api_host, "--host", "-h", help="Host to bind the server to"
    ),
    port: int = typer.Option(
        api_port, "--port", "-p", help="Port to bind the server to"
    ),
    reload: bool = typer.Option(
        True, "--reload/--no-reload", help="Enable auto-reload"
    ),
):
    """Start the th2agent FastAPI server."""
    typer.echo(f"Starting th2agent server on {host}:{port}")
    uvicorn.run("apowerb.main:app", host=host, port=port, reload=reload)


# Add subcommands
app.add_typer(agents_app, name="agents", help="Manage agents")
app.add_typer(tools_app, name="tools", help="Manage tools")
app.add_typer(runs_app, name="runs", help="Manage agent runs")
app.add_typer(config_app, name="config", help="Configuration set from the admin screen")

if __name__ == "__main__":
    app()
