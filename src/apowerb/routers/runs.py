from fastapi import APIRouter, Depends, HTTPException
from apowerb.core.agent_main import get_agent
from apowerb.agent_store.agent_manager import AgentStore
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from typing import Dict

# import asyncio
# from concurrent.futures import ThreadPoolExecutor
import threading

# Global dict to track running agents
running_agents: Dict[str, threading.Thread] = {}


def get_agent_store() -> AgentStore:
    agent_store = AgentStore()
    agent_store.create_table()
    return agent_store


router = APIRouter()


@router.post("/agents/{agent_id}/run")
async def run_adk_agent(agent_id: str, agent_store=Depends(get_agent_store), _current_user: user_schemas.User = Depends(get_current_user)):
    """Start running an agent in the background."""
    if agent_id in running_agents:
        raise HTTPException(status_code=400, detail="Agent is already running")

    agent = get_agent(agent_store, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Run the agent in a background thread
    def run_agent_thread():
        try:
            # Assuming the agent has a run method
            agent.run()
        except Exception as e:
            print(f"Error running agent {agent_id}: {e}")

    thread = threading.Thread(target=run_agent_thread, daemon=True)
    running_agents[agent_id] = thread
    thread.start()

    return {"message": f"Agent {agent_id} started"}


@router.get("/agents/{agent_id}/status")
async def get_agent_status(agent_id: str, _current_user: user_schemas.User = Depends(get_current_user)):
    """Get the run status of an agent."""
    if agent_id in running_agents:
        thread = running_agents[agent_id]
        if thread.is_alive():
            return {"status": "running"}
        else:
            # Thread died, clean up
            del running_agents[agent_id]
            return {"status": "stopped"}
    else:
        return {"status": "stopped"}
