from pathlib import Path

from langgraph.graph import END, StateGraph

from .nodes import bootstrap_node, create_worker_node, planner_node, requirement_clarifier_node, supervisor
from .state import DesignState

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CHECKPOINTS_DIR = BASE_DIR / "projects" / ".orchestrator"
CHECKPOINT_DB_PATH = CHECKPOINTS_DIR / "langgraph-checkpoints.sqlite"


def _get_agents_from_registry() -> list[str]:
    """Dynamically get agent list from ExpertRegistry.
    
    This enables hot-pluggable experts - new experts are automatically
    included in the workflow without code changes.
    """
    builtin_agents = {"validator", "ir-assembler"}
    
    try:
        from registry.expert_registry import ExpertRegistry
        registry = ExpertRegistry.get_instance()
        return list(set(registry.get_capabilities()) | builtin_agents)
    except RuntimeError:
        # Fallback for when registry is not initialized (e.g., during tests)
        return list(builtin_agents)


def create_design_graph(checkpointer=None):
    workflow = StateGraph(DesignState)

    workflow.add_node("bootstrap", bootstrap_node)
    workflow.add_node("requirement_clarifier", requirement_clarifier_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("supervisor", supervisor)

    # Dynamically get agents from registry (hot-pluggable)
    agents = _get_agents_from_registry()

    for agent in agents:
        workflow.add_node(agent, create_worker_node(agent))

    workflow.set_entry_point("bootstrap")

    def route_bootstrap(state: DesignState):
        if state.get("resume_target_node"):
            return state["resume_target_node"]
        if state.get("resume_action") == "approve":
            return "supervisor"
        return "requirement_clarifier"

    def route_requirement_clarifier(state: DesignState):
        if state.get("human_intervention_required"):
            return END
        return "planner"

    def route_planner(state: DesignState):
        if state.get("human_intervention_required"):
            return END
        return "supervisor"

    def route_supervisor(state: DesignState):
        return resolve_supervisor_route(state)

    workflow.add_conditional_edges("bootstrap", route_bootstrap)
    workflow.add_conditional_edges("requirement_clarifier", route_requirement_clarifier)
    workflow.add_conditional_edges("planner", route_planner)
    workflow.add_conditional_edges("supervisor", route_supervisor)

    for agent in agents:
        workflow.add_conditional_edges(agent, resolve_worker_completion_route)

    return workflow.compile(checkpointer=checkpointer)


def resolve_supervisor_route(state: DesignState):
    dispatched = state.get("dispatched_tasks") or []
    if state.get("last_worker") == "supervisor" and dispatched:
        agent_names = [task.get("agent_type") for task in dispatched if task.get("agent_type")]
        if not agent_names:
            return END
        return agent_names if len(agent_names) > 1 else agent_names[0]

    if state.get("last_worker") == "supervisor" and state.get("current_task_ids"):
        current_ids = state.get("current_task_ids") or []
        running_lookup = {
            task.get("id"): task.get("agent_type")
            for task in state.get("task_queue", [])
            if task.get("status") == "running" and task.get("agent_type")
        }
        agent_names = [running_lookup[task_id] for task_id in current_ids if task_id in running_lookup]
        if agent_names:
            return agent_names if len(agent_names) > 1 else agent_names[0]

    if state.get("last_worker") == "supervisor" and state.get("current_task_id") and state.get("current_node"):
        running_task = next(
            (
                task
                for task in state.get("task_queue", [])
                if task.get("id") == state.get("current_task_id") and task.get("status") == "running"
            ),
            None,
        )
        if running_task:
            return state["current_node"]

    decision = supervisor(state)
    next_step = decision["next"]

    if isinstance(next_step, list):
        return next_step if next_step else END
    if next_step in {"END", "human_review"}:
        return END
    if next_step == "supervisor_advance":
        return "supervisor"
    return next_step


def resolve_worker_completion_route(state: DesignState):
    last_worker = state.get("last_worker")
    if last_worker in {None, "", "bootstrap", "planner", "supervisor"}:
        return "supervisor"

    running_peers = [
        task
        for task in state.get("task_queue", [])
        if task.get("status") == "running" and task.get("agent_type") != last_worker
    ]
    if running_peers:
        return END
    return "supervisor"
