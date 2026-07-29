import os
from typing import List
import ast
from th2agent.configs.paths import agents_pool_dir
from th2agent.core.agent_helpers import get_agent_details
from th2agent.agent_store.agent_manager import AgentStore

agent_store = AgentStore()


def generate_subagents_code(agent_id: str):
    """ """
    agent_details = get_agent_details(
        agent_id=int(agent_id), items_to_select="sub_agents"
    )
    from th2agent.core.agent_main import _parse_string_list
    sub_agents = _parse_string_list(agent_details.get("sub_agents"))
    if not sub_agents:
        return [], None
    # sub_agents_code = []
    sub_agents_list = "['" + "','".join(sub_agents) + "']"
    sub_agents_list = f"sub_agents_list = {sub_agents_list}"
    sub_agents_code = f"{sub_agents_list} \nsub_agents = [] \nfor sub_agent in sub_agents_list: \n   sub_agents.append(to_agent(agent_name = sub_agent,sub_agents = []))"
    sub_agents_names = [f"sub_{sub_agent}" for sub_agent in sub_agents]
    return sub_agents_names, sub_agents_code


def create_agent_module(
    agent_name: str,
    description: str,
    instruction: str,
    tools: List[str],
    model: str = "claude-3-5-sonnet-20241022",
    agents_pool_path: str | None = None,
) -> None:
    """
    Create a new agent module in the agents_pool directory.

    Args:
        agent_name: Name of the agent (used for folder and agent name).
        description: Description of the agent.
        instruction: Instruction for the agent.
        tools: List of tool functions.
        model: Model to use (default Anthropic model).
        agents_pool_path: Path to the agents pool directory.
    """
    agents_pool_path = agents_pool_path or str(agents_pool_dir())

    # Create the agent directory
    agent_adk_name = f"agent{agent_name}"
    agent_dir = os.path.join(agents_pool_path, agent_adk_name)
    os.makedirs(agent_dir, exist_ok=True)

    # Create __init__.py
    init_file = os.path.join(agent_dir, "__init__.py")
    with open(init_file, "w") as f:
        f.write("# Agent module\n")
    # # Create .env file
    # env_file = os.path.join(agent_dir, ".env")
    # with open(env_file, "w") as f:
    #     f.write("# Environment variables\n")

    # create adk agent module file"
    agent_code = """
# th2agent modules
from th2agent.core.agent_helpers import to_agent

# declare env variables from stored in agent_model_params
root_agent = to_agent(agent_name = '{agent_name}')
# Create the agent

""".format(
        agent_name=agent_adk_name,
    )

    # Write agent.py
    agent_file = os.path.join(agent_dir, "agent.py")
    with open(agent_file, "w") as f:
        f.write(agent_code)

    print(f"Agent module '{agent_name}' created successfully in {agent_dir}")


def ensure_agent_modules(agents_pool_path: str | None = None) -> None:
    """Auto-repair: regenerate missing agent.py files for all agents in the DB.

    Called at startup to fix agents whose pool directory exists (with .adk session data)
    but whose agent.py was lost (e.g. different environment, partial sync, manual deletion).
    """
    agents_pool_path = agents_pool_path or str(agents_pool_dir())
    try:
        result = agent_store.get_list_agents(agent_store.agent_table.select())
        agents = [u._asdict() for u in result]
    except Exception as e:
        print(f"[ensure_agent_modules] Could not query agents: {e}")
        return

    repaired = []
    for agent in agents:
        agent_id = agent.get("agent_id")
        if not agent_id:
            continue
        folder_name = f"agent{agent_id}"
        agent_dir = os.path.join(agents_pool_path, folder_name)
        agent_file = os.path.join(agent_dir, "agent.py")

        if not os.path.exists(agent_file):
            os.makedirs(agent_dir, exist_ok=True)
            # Write __init__.py
            init_file = os.path.join(agent_dir, "__init__.py")
            if not os.path.exists(init_file):
                with open(init_file, "w") as f:
                    f.write("# Agent module\n")
            # Write agent.py
            agent_code = (
                "\n# th2agent modules\n"
                "from th2agent.core.agent_helpers import to_agent\n\n"
                f"# declare env variables from stored in agent_model_params\n"
                f"root_agent = to_agent(agent_name = '{folder_name}')\n"
                "# Create the agent\n\n"
            )
            with open(agent_file, "w") as f:
                f.write(agent_code)
            repaired.append(folder_name)

    if repaired:
        print(f"[ensure_agent_modules] Repaired {len(repaired)} agent(s): {repaired}")
    else:
        print("[ensure_agent_modules] All agent modules are intact.")


def delete_agent_module(
    agent_name: str = "", agents_pool_path: str | None = None
) -> None:
    import shutil
    agents_pool_path = agents_pool_path or str(agents_pool_dir())
    agent_adk_name = f"agent{agent_name}"
    agent_dir = os.path.join(agents_pool_path, agent_adk_name)
    if os.path.exists(agent_dir):
        shutil.rmtree(agent_dir)
        print(f"Agent module '{agent_adk_name}' deleted successfully from {agent_dir}")
    else:
        print(f"Agent module directory '{agent_dir}' not found, skipping file deletion")
