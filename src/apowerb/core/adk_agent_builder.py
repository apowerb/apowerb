import os
from typing import List
import ast
from apowerb.configs.paths import agents_pool_dir
from apowerb.core.agent_helpers import get_agent_details
from apowerb.agent_store.agent_manager import AgentStore

agent_store = AgentStore()


def generate_subagents_code(agent_id: str):
    """ """
    agent_details = get_agent_details(
        agent_id=int(agent_id), items_to_select="sub_agents"
    )
    from apowerb.core.agent_main import _parse_string_list
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
from apowerb.core.agent_helpers import to_agent

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


# The stub below is entirely derived from the agent id: no business logic ever
# lives in it. Model, instruction and tools are resolved at runtime by
# ``to_agent`` from the database. That is precisely what makes rewriting a
# stale one safe -- there is nothing in the file to lose.
_AGENT_STUB_IMPORT = "from apowerb.core.agent_helpers import to_agent"


def _agent_stub_source(folder_name: str) -> str:
    """The canonical content of an agent module."""
    return (
        "\n# apowerb modules\n"
        f"{_AGENT_STUB_IMPORT}\n\n"
        "# declare env variables from stored in agent_model_params\n"
        f"root_agent = to_agent(agent_name = '{folder_name}')\n"
        "# Create the agent\n\n"
    )


def _stub_cannot_import_the_core(agent_file: str) -> bool:
    """A stub that cannot import the core is as good as a missing one.

    The package was renamed ``th2agent`` -> ``apowerb`` on 2026-07-31. The
    generator here was updated; the files already written were not, and a stub
    is only rewritten when its agent is saved again. On 2026-08-03 that left
    124 of 128 agents on production importing a module that no longer exists --
    each one a ``ModuleNotFoundError`` at its first run. Nobody had run an
    agent since the rename, so nothing had surfaced it.

    Deliberately narrow: only a stub that cannot import the core is rewritten.
    A file someone has genuinely customised, which still imports it, is left
    alone.
    """
    try:
        with open(agent_file, encoding="utf-8") as f:
            return _AGENT_STUB_IMPORT not in f.read()
    except OSError:
        return True


def ensure_agent_modules(agents_pool_path: str | None = None) -> None:
    """Auto-repair: regenerate missing *or stale* agent.py files for every agent.

    Called at startup, so an environment heals itself on its next restart
    rather than waiting for someone to save each agent by hand.

    Missing: the pool directory survived (with its .adk session data) but the
    module was lost -- another environment, a partial sync, a manual deletion.

    Stale: the module is there but can no longer import the core, which is what
    a package rename leaves behind. Both cases end the same way, as a
    ModuleNotFoundError at the agent's first run, so both are repaired.
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

        missing = not os.path.exists(agent_file)
        stale = not missing and _stub_cannot_import_the_core(agent_file)

        if missing or stale:
            os.makedirs(agent_dir, exist_ok=True)
            # Write __init__.py
            init_file = os.path.join(agent_dir, "__init__.py")
            if not os.path.exists(init_file):
                with open(init_file, "w") as f:
                    f.write("# Agent module\n")
            # Write agent.py
            with open(agent_file, "w") as f:
                f.write(_agent_stub_source(folder_name))
            repaired.append(f"{folder_name} ({'missing' if missing else 'stale'})")

    if repaired:
        print(f"[ensure_agent_modules] Repaired {len(repaired)} agent(s): {repaired}")
    else:
        print("[ensure_agent_modules] All agent modules are intact.")


def delete_agent_module(
    agent_name: str = "", agents_pool_path: str | None = None
) -> None:
    import re
    import shutil

    # agent_name is always meant to be a bare numeric agent ID. Reject
    # anything else instead of concatenating it into a path: this is a
    # destructive shutil.rmtree() and callers have historically passed the
    # raw agent_id straight through from a request (see delete_agent in
    # core/agent_main.py) without a format check.
    if not re.fullmatch(r"\d+", str(agent_name)):
        print(f"[delete_agent_module] Refusing invalid agent_name: {agent_name!r}")
        return

    agents_pool_path = agents_pool_path or str(agents_pool_dir())
    agent_adk_name = f"agent{agent_name}"
    agent_dir = os.path.join(agents_pool_path, agent_adk_name)
    if os.path.exists(agent_dir):
        shutil.rmtree(agent_dir)
        print(f"Agent module '{agent_adk_name}' deleted successfully from {agent_dir}")
    else:
        print(f"Agent module directory '{agent_dir}' not found, skipping file deletion")
