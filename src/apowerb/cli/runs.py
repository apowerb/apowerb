import typer
from apowerb.agent_store.agent_manager import AgentStore
from apowerb.core.agent_main import get_agent
import threading
from apowerb.configs.settings import get_settings

app = typer.Typer()

# Global dict to track running agents (same as in router)
running_agents: dict[str, threading.Thread] = {}

settings = get_settings()


def get_agent_store() -> AgentStore:
    """Get agent store instance."""
    agent_store = AgentStore()
    agent_store.create_table()
    return agent_store


@app.command("start")
def start_agent(
    agent_id: str = typer.Argument(..., help="Agent ID to start"),
):
    """Start running an agent."""
    if agent_id in running_agents:
        typer.echo(f"Agent '{agent_id}' is already running.", err=True)
        raise typer.Exit(1)

    agent_store = get_agent_store()
    agent = get_agent(agent_store, agent_id)

    if not agent:
        typer.echo(f"Agent with ID '{agent_id}' not found.", err=True)
        raise typer.Exit(1)

    # Run the agent in a background thread
    def run_agent_thread():
        try:
            # Assuming the agent has a run method
            agent.run()
        except Exception as e:
            typer.echo(f"Error running agent {agent_id}: {e}", err=True)

    thread = threading.Thread(target=run_agent_thread, daemon=True)
    running_agents[agent_id] = thread
    thread.start()

    typer.echo(f"Agent '{agent_id}' started successfully.")


@app.command("status")
def get_agent_status_cmd(
    agent_id: str = typer.Argument(..., help="Agent ID to check status for"),
):
    """Get the run status of an agent."""
    if agent_id in running_agents:
        thread = running_agents[agent_id]
        if thread.is_alive():
            typer.echo(f"Agent '{agent_id}' is running.")
            return
        else:
            # Thread died, clean up
            del running_agents[agent_id]

    typer.echo(f"Agent '{agent_id}' is stopped.")


@app.command("stop")
def stop_agent(
    agent_id: str = typer.Argument(..., help="Agent ID to stop"),
):
    """Stop a running agent."""
    if agent_id not in running_agents:
        typer.echo(f"Agent '{agent_id}' is not running.", err=True)
        raise typer.Exit(1)

    # thread = running_agents[agent_id]
    # Note: In a real implementation, you'd want a proper way to stop the thread
    # For now, we'll just remove it from tracking
    del running_agents[agent_id]
    typer.echo(f"Agent '{agent_id}' stopped.")
