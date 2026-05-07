from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

RunStatus = Literal["queued", "running", "waiting_human", "success", "failed"]
NodeStatus = Literal["todo", "running", "waiting_human", "success", "failed", "skipped"]


def merge_messages(
    current: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    return [*(current or []), *(incoming or [])]


def merge_artifacts(
    current: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(current or {})
    merged.update(incoming or {})
    return merged


def _node_status_rank(status: Optional[str]) -> int:
    order = {
        "todo": 0,
        "running": 1,
        "waiting_human": 2,
        "success": 3,
        "skipped": 3,
        "failed": 4,
    }
    return order.get(str(status or "").lower(), -1)


def _merge_node_status(current_status: Optional[str], incoming_status: Optional[str]) -> Optional[str]:
    if incoming_status in (None, ""):
        return current_status
    if current_status in (None, ""):
        return incoming_status
    if _node_status_rank(incoming_status) >= _node_status_rank(current_status):
        return incoming_status
    return current_status


def merge_task_queue(
    current: Optional[List["Task"]],
    incoming: Optional[List["Task"]],
) -> List["Task"]:
    current = current or []
    incoming = incoming or []
    if any(task.get("agent_type") == "planner" for task in incoming):
        # Planner outputs represent the authoritative execution plan for the run.
        # Replace the entire queue so stale expert nodes from an earlier plan cannot survive.
        return [dict(task) for task in incoming]
    merged_by_id: Dict[str, Task] = {task["id"]: dict(task) for task in current}
    order = [task["id"] for task in current]

    for task in incoming:
        task_id = task["id"]
        if task_id not in merged_by_id:
            order.append(task_id)
            merged_by_id[task_id] = dict(task)
            continue
        merged_task = {**merged_by_id[task_id], **task}
        merged_task["status"] = _merge_node_status(
            merged_by_id[task_id].get("status"),
            task.get("status"),
        )
        merged_by_id[task_id] = merged_task

    return [merged_by_id[task_id] for task_id in order]


def merge_history(
    current: Optional[List[str]],
    incoming: Optional[List[str]],
) -> List[str]:
    return [*(current or []), *(incoming or [])]


def merge_tool_results(
    current: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    return [*(current or []), *(incoming or [])]


def merge_bool_or(current: bool, incoming: bool) -> bool:
    return current or incoming


def merge_optional_str(current: Optional[str], incoming: Optional[str]) -> Optional[str]:
    return incoming or current


def merge_optional_task_id(current: Optional[str], incoming: Optional[str]) -> Optional[str]:
    if incoming is None:
        return None
    if incoming == "":
        return None
    return incoming


def merge_task_id_list(current: Optional[List[str]], incoming: Optional[List[str]]) -> List[str]:
    if incoming is None:
        return list(current or [])
    return list(incoming)


def merge_dispatch_list(
    current: Optional[List[Dict[str, Any]]],
    incoming: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if incoming is None:
        return list(current or [])
    return [dict(item) for item in incoming]


def merge_run_status(current: RunStatus, incoming: RunStatus) -> RunStatus:
    # Logic: failed > waiting_human > running > queued > success
    severity = {"failed": 4, "waiting_human": 3, "running": 2, "queued": 1, "success": 0}
    if severity.get(incoming, 0) > severity.get(current, 0):
        return incoming
    return current


class Task(TypedDict, total=False):
    id: str
    agent_type: str
    stage: int
    phase: str
    priority: int
    input_keys: List[str]
    status: NodeStatus
    dependencies: List[str]
    metadata: Dict[str, Any]


class DesignState(TypedDict, total=False):
    design_context: Dict[str, Any]
    task_queue: Annotated[List[Task], merge_task_queue]
    workflow_phase: str
    history: Annotated[List[str], merge_history]
    messages: Annotated[List[Dict[str, Any]], merge_messages]
    artifacts: Annotated[Dict[str, Any], merge_artifacts]
    tool_results: Annotated[List[Dict[str, Any]], merge_tool_results]
    human_intervention_required: Annotated[bool, merge_bool_or]
    waiting_reason: Annotated[Optional[str], merge_optional_str]
    last_worker: Annotated[Optional[str], merge_optional_str]
    current_node: Annotated[Optional[str], merge_optional_str]
    run_status: Annotated[RunStatus, merge_run_status]
    run_id: str
    resume_action: str
    human_feedback: str
    human_answers: Dict[str, List[Dict[str, Any]]]
    pending_interrupt: Dict[str, Any] | None
    resume_target_node: str | None
    current_task_id: Annotated[Optional[str], merge_optional_task_id]
    current_task_ids: Annotated[List[str], merge_task_id_list]
    dispatched_tasks: Annotated[List[Dict[str, Any]], merge_dispatch_list]
    updated_at: Annotated[str, merge_optional_str]
    project_id: str
    version: str
    requirement: str
