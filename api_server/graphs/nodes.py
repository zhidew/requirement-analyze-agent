import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

from services.llm_service import SubagentOutput, generate_with_llm, resolve_runtime_llm_settings

from .state import DesignState, Task
from .tools import execute_tool
from services.db_service import metadata_db
from subgraphs.dynamic_subagent import run_dynamic_subagent
from subgraphs.topic_ownership import build_topic_ownership_payload as build_topic_ownership_payload_from_registry

# Ensure project root is on sys.path so config module can be resolved
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
from config import get_phase_config

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PLANNER_EXPERT_SELECTION_INTERACTION = "expert_selection"
PLANNER_REASONING_TITLE = "### 编排规划推理"
REQUIREMENT_CLARIFIER_REASONING_TITLE = "### 需求澄清推理"
REQUIREMENT_CLARIFICATION_MAX_ROUNDS = int(os.getenv("REQUIREMENT_CLARIFICATION_MAX_ROUNDS", "6"))
COMBINED_INPUT_QUERY_MAX_CHARS = int(os.getenv("COMBINED_INPUT_QUERY_MAX_CHARS", "12000"))

# Agent aliases for normalization (kept for backward compatibility)
AGENT_ALIASES = {
    "clarification": "requirement-clarification",
    "requirement-clarifier": "requirement-clarification",
    "rules": "rules-management",
    "business-rules": "rules-management",
    "document": "business-form-operation",
    "documents": "business-form-operation",
    "operation": "business-form-operation",
    "form": "business-form-operation",
    "forms": "business-form-operation",
    "business-form": "business-form-operation",
    "document-operation": "business-form-operation",
    "workflow": "process-control",
    "process": "process-control",
    "integration": "integration-requirements",
    "assembler": "ir-assembler",
    "ir": "ir-assembler",
}


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for item in items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _normalize_expert_id_list(raw_ids: Any) -> List[str]:
    if not isinstance(raw_ids, list):
        return []
    return _dedupe_preserve_order([str(item).strip() for item in raw_ids if str(item).strip()])


def _format_expert_list(expert_ids: List[str]) -> str:
    normalized = _dedupe_preserve_order(expert_ids)
    return ", ".join(normalized) if normalized else "(none)"


def _extract_planner_expert_selection(answer_entries: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    for entry in reversed(answer_entries):
        if not isinstance(entry, dict):
            continue
        selection_type = str(entry.get("selection_type") or "").strip()
        if selection_type != PLANNER_EXPERT_SELECTION_INTERACTION and "selected_experts" not in entry:
            continue
        return {
            "selected_experts": _normalize_expert_id_list(entry.get("selected_experts")),
            "recommended_experts": _normalize_expert_id_list(entry.get("recommended_experts")),
        }
    return None


def _build_expert_selection_interrupt_context(
    *,
    enabled_expert_ids: List[str],
    recommended_expert_ids: List[str],
    auto_selected_expert_ids: List[str],
) -> Dict[str, Any]:
    try:
        from registry.expert_registry import ExpertRegistry

        registry = ExpertRegistry.get_instance()
    except RuntimeError:
        registry = None

    recommended_set = set(recommended_expert_ids)
    auto_selected_set = set(auto_selected_expert_ids)
    available_experts: List[Dict[str, Any]] = []

    for expert_id in _dedupe_preserve_order(enabled_expert_ids):
        manifest = registry.get_manifest(expert_id) if registry else None
        description = (manifest.description if manifest else "") or ""
        phase = (manifest.phase if manifest else "") or _get_base_phase(expert_id)
        available_experts.append(
            {
                "id": expert_id,
                "name": (manifest.name if manifest else "") or expert_id,
                "name_zh": manifest.name_zh if manifest else "",
                "name_en": manifest.name_en if manifest else expert_id,
                "description": description,
                "phase": phase,
                "recommended": expert_id in recommended_set,
                "auto_selected": expert_id in auto_selected_set,
            }
        )

    return {
        "interaction_type": PLANNER_EXPERT_SELECTION_INTERACTION,
        "selection_mode": "multi_select",
        "why_needed": (
            "规划器已完成初始专家推荐。"
            "请在执行开始前确认最终参与的专家。"
        ),
        "recommended_experts": list(recommended_expert_ids),
        "selected_experts": list(recommended_expert_ids),
        "available_experts": available_experts,
        "allow_free_text": True,
    }


def _get_planner_selection_policy(registry: Any, expert_id: str) -> Dict[str, Any]:
    try:
        config = registry.load_full_config(expert_id)
    except Exception:
        return {}
    policy = (config.policies or {}).get("planner_selection")
    return policy if isinstance(policy, dict) else {}


def _split_planner_selectable_experts(
    registry: Any,
    expert_ids: List[str],
) -> tuple[List[str], List[str]]:
    llm_selectable: List[str] = []
    default_selected: List[str] = []
    for expert_id in _dedupe_preserve_order(expert_ids):
        policy = _get_planner_selection_policy(registry, expert_id)
        if policy.get("llm_selectable", True) is not False:
            llm_selectable.append(expert_id)
        if bool(policy.get("default_selected", False)):
            default_selected.append(expert_id)
    return llm_selectable, default_selected


def _planner_failed_task() -> List[Task]:
    return [{"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "failed", "dependencies": [], "priority": 100}]

def _collect_planner_signal_text(requirement_text: str, human_inputs: Dict[str, Any]) -> str:
    candidate_texts: List[str] = [str(requirement_text or "")]

    if isinstance(human_inputs, dict):
        for value in human_inputs.values():
            if isinstance(value, list):
                candidate_texts.extend(str(item) for item in value)
            else:
                candidate_texts.append(str(value))

    return " ".join(text.casefold() for text in candidate_texts if text)


def _normalize_policy_keywords(raw_keywords: Any) -> List[str]:
    if isinstance(raw_keywords, str):
        items = [raw_keywords]
    elif isinstance(raw_keywords, list):
        items = [item for item in raw_keywords if isinstance(item, (str, int, float))]
    else:
        return []
    return [str(item).strip().casefold() for item in items if str(item).strip()]


def _apply_policy_based_auto_selection(
    *,
    active_agents: set[str],
    enabled_experts: set[str],
    requirement_text: str,
    human_inputs: Dict[str, Any],
) -> set[str]:
    """Apply planner auto-selection rules from expert YAML policies.

    Supported policy shape (per expert):
    policies:
      planner_auto_select:
        enabled: true
        trigger: keyword_any | keyword_all
        keywords: ["latency", "性能", ...]
    """
    if not enabled_experts:
        return active_agents

    signal_text = _collect_planner_signal_text(requirement_text, human_inputs)
    if not signal_text:
        return active_agents

    try:
        from registry.expert_registry import ExpertRegistry

        registry = ExpertRegistry.get_instance()
    except RuntimeError:
        return active_agents

    auto_selected: List[str] = []
    for expert_id in sorted(enabled_experts):
        if expert_id in active_agents:
            continue

        try:
            config = registry.load_full_config(expert_id)
        except Exception:
            continue

        policy = (config.policies or {}).get("planner_auto_select")
        if not isinstance(policy, dict):
            continue
        if not bool(policy.get("enabled", False)):
            continue

        trigger = str(policy.get("trigger", "keyword_any")).strip().lower()
        keywords = _normalize_policy_keywords(policy.get("keywords"))
        if not keywords:
            continue

        if trigger == "keyword_all":
            matched = all(keyword in signal_text for keyword in keywords)
        else:
            matched = any(keyword in signal_text for keyword in keywords)

        if not matched:
            continue

        active_agents.add(expert_id)
        auto_selected.append(expert_id)

    if auto_selected:
        print(f"[DEBUG] Planner: policy auto-selected experts: {sorted(auto_selected)}")

    return active_agents


def _build_project_asset_context(project_id: str) -> Dict[str, Any]:
    asset_context: Dict[str, Any] = {}

    repositories = metadata_db.list_repositories(project_id)
    if repositories:
        repo_items = [
            {
                "id": repo["id"],
                "name": repo["name"],
                "branch": repo.get("branch"),
                "description": repo.get("description"),
            }
            for repo in repositories[:5]
        ]
        asset_context["repositories"] = {
            "count": len(repositories),
            "items": repo_items,
            "omitted_count": max(len(repositories) - len(repo_items), 0),
        }

    databases = metadata_db.list_databases(project_id)
    if databases:
        db_items = [
            {
                "id": db["id"],
                "name": db["name"],
                "type": db["type"],
                "database": db["database"],
                "schema_filter": db.get("schema_filter") or [],
                "description": db.get("description"),
            }
            for db in databases[:5]
        ]
        asset_context["databases"] = {
            "count": len(databases),
            "items": db_items,
            "omitted_count": max(len(databases) - len(db_items), 0),
        }

    knowledge_bases = metadata_db.list_knowledge_bases(project_id)
    if knowledge_bases:
        kb_items = [
            {
                "id": kb["id"],
                "name": kb["name"],
                "type": kb["type"],
                "branch": kb.get("branch"),
                "repo_path": kb.get("repo_path"),
                "includes": kb.get("includes") or [],
                "description": kb.get("description"),
            }
            for kb in knowledge_bases[:5]
        ]
        asset_context["knowledge_bases"] = {
            "count": len(knowledge_bases),
            "items": kb_items,
            "omitted_count": max(len(knowledge_bases) - len(kb_items), 0),
        }

    return asset_context


def _query_asset_insights(project_id: str, requirement_text: str) -> Dict[str, Any]:
    """Actively query three repositories (database, code repo, knowledge base) to gather
    content insights during the Planner phase. Results are shared with downstream experts
    via baseline/requirements.json.

    This bridges the gap where the original system only injected asset *metadata* (names,
    types) but never probed the actual content to inform planning decisions.

    Returns a dict with:
      - database_insights / knowledge_base_insights / repository_insights: actual query results (only when queries succeed)
      - query_status: per-asset-type status ("skipped", "success", "partial_failure", "failed")
      - query_errors: list of error messages for downstream awareness
    """
    insights: Dict[str, Any] = {
        "query_status": {},  # {"database": "skipped"|"success"|"partial_failure"|"failed", ...}
        "query_errors": [],
    }
    root_dir = BASE_DIR / "projects" / project_id / "_planner_probe"
    root_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Database: probe table lists ---
    try:
        databases = metadata_db.list_databases(project_id)
        if not databases:
            insights["query_status"]["database"] = "skipped"
            insights["query_errors"].append("database: no databases configured for this project")
        else:
            db_summaries = []
            error_count = 0
            for db in databases[:5]:
                db_id = db["id"]
                try:
                    from graphs.tools.query_database import query_database as qdb
                    result = qdb(
                        root_dir,
                        {"project_id": project_id, "db_id": db_id, "query_type": "list_tables"},
                    )
                    tables = result.get("tables") or []
                    db_summaries.append({
                        "id": db_id,
                        "name": db.get("name"),
                        "type": db.get("type"),
                        "table_count": len(tables),
                        "table_names": [t.get("table_name") or t.get("name") for t in tables[:20]],
                    })
                except Exception as exc:
                    error_count += 1
                    insights["query_errors"].append(f"database:{db_id} ({db.get('name')}): {exc}")
            if db_summaries:
                insights["database_insights"] = db_summaries
            if error_count == 0:
                insights["query_status"]["database"] = "success"
            elif error_count == len(databases[:5]):
                insights["query_status"]["database"] = "failed"
            else:
                insights["query_status"]["database"] = "partial_failure"
    except Exception as exc:
        insights["query_status"]["database"] = "failed"
        insights["query_errors"].append(f"database_overview: {exc}")

    # --- 2. Knowledge Base: search for requirement-relevant terms ---
    try:
        knowledge_bases = metadata_db.list_knowledge_bases(project_id)
        if not knowledge_bases:
            insights["query_status"]["knowledge_base"] = "skipped"
            insights["query_errors"].append("knowledge_base: no knowledge bases configured for this project")
        else:
            kb_summaries = []
            # Extract meaningful keywords from requirement for KB search
            import re
            keywords = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9_\-]{2,}', requirement_text)
            search_keywords = keywords[:5] if keywords else ["设计"]
            error_count = 0
            for kb in knowledge_bases[:3]:
                kb_id = kb["id"]
                try:
                    from graphs.tools.query_knowledge_base import query_knowledge_base as qkb
                    for kw in search_keywords[:3]:
                        result = qkb(
                            root_dir,
                            {
                                "project_id": project_id,
                                "kb_id": kb_id,
                                "query_type": "search_design_docs",
                                "keyword": kw,
                                "limit": 3,
                            },
                        )
                        matches = result.get("knowledge_bases") or []
                        for kb_result in matches:
                            kb_matches = kb_result.get("matches") or []
                            if kb_matches:
                                kb_summaries.append({
                                    "kb_id": kb_id,
                                    "kb_name": kb_result.get("kb_name"),
                                    "search_keyword": kw,
                                    "match_count": len(kb_matches),
                                    "top_matches": [
                                        {
                                            "title": m.get("title") or m.get("name"),
                                            "type": m.get("type"),
                                            "feature_id": m.get("feature_id"),
                                        }
                                        for m in kb_matches[:3]
                                    ],
                                })
                                break  # One hit per KB is enough for planner context
                except Exception as exc:
                    error_count += 1
                    insights["query_errors"].append(f"knowledge_base:{kb_id} ({kb.get('name')}): {exc}")
            if kb_summaries:
                insights["knowledge_base_insights"] = kb_summaries
            if error_count == 0:
                insights["query_status"]["knowledge_base"] = "success"
            elif error_count == len(knowledge_bases[:3]):
                insights["query_status"]["knowledge_base"] = "failed"
            else:
                insights["query_status"]["knowledge_base"] = "partial_failure"
    except Exception as exc:
        insights["query_status"]["knowledge_base"] = "failed"
        insights["query_errors"].append(f"knowledge_base_overview: {exc}")

    # --- 3. Code Repository: list repo structure hints ---
    try:
        repositories = metadata_db.list_repositories(project_id)
        if not repositories:
            insights["query_status"]["repository"] = "skipped"
            insights["query_errors"].append("repository: no repositories configured for this project")
        else:
            repo_summaries = []
            error_count = 0
            for repo in repositories[:3]:
                repo_id = repo["id"]
                try:
                    from graphs.tools.clone_repository import clone_repository as cr
                    result = cr(
                        root_dir,
                        {"project_id": project_id, "repo_id": repo_id},
                    )
                    cloned_path = result.get("project_relative_path") or result.get("repo_path")
                    if cloned_path:
                        repo_abs = (BASE_DIR / "projects" / project_id / cloned_path).resolve()
                        if repo_abs.exists():
                            top_dirs = sorted(
                                [d.name for d in repo_abs.iterdir() if d.is_dir() and not d.name.startswith(".")]
                            )[:15]
                            repo_summaries.append({
                                "id": repo_id,
                                "name": repo.get("name"),
                                "branch": repo.get("branch"),
                                "top_level_dirs": top_dirs,
                            })
                        else:
                            error_count += 1
                            insights["query_errors"].append(
                                f"repository:{repo_id} ({repo.get('name')}): cloned path does not exist: {cloned_path}"
                            )
                    else:
                        error_count += 1
                        insights["query_errors"].append(
                            f"repository:{repo_id} ({repo.get('name')}): clone returned no path"
                        )
                except Exception as exc:
                    # Clone may fail due to auth, network, etc. - record but don't crash
                    error_count += 1
                    insights["query_errors"].append(f"repository:{repo_id} ({repo.get('name')}): {exc}")
            if repo_summaries:
                insights["repository_insights"] = repo_summaries
            if error_count == 0:
                insights["query_status"]["repository"] = "success"
            elif error_count == len(repositories[:3]):
                insights["query_status"]["repository"] = "failed"
            else:
                insights["query_status"]["repository"] = "partial_failure"
    except Exception as exc:
        insights["query_status"]["repository"] = "failed"
        insights["query_errors"].append(f"repository_overview: {exc}")

    # Clean up probe directory
    try:
        import shutil
        if root_dir.exists():
            shutil.rmtree(root_dir, ignore_errors=True)
    except Exception:
        pass

    return insights


def _get_supported_agent_ids() -> set:
    """Get supported agent IDs from AgentRegistry (dynamic).
    
    Includes both registry-based agents and built-in agents (validator).
    """
    # Built-in agents that may not be present in older registries.
    builtin_agents = {"ir-assembler", "validator"}
    
    try:
        from registry.agent_registry import AgentRegistry
        registry = AgentRegistry.get_instance()
        return set(registry.get_capabilities()) | builtin_agents
    except RuntimeError:
        # Fallback to hardcoded list if registry not initialized
        return {
            "requirement-clarification",
            "rules-management",
            "business-form-operation",
            "process-control",
            "integration-requirements",
            "ir-assembler",
            "validator",
        }


# Legacy constant for compatibility
SUPPORTED_AGENT_IDS = _get_supported_agent_ids()


# Enable dynamic subagent execution (can be controlled via environment variable)
USE_DYNAMIC_SUBAGENT = os.getenv("USE_DYNAMIC_SUBAGENT", "true").lower() in ("true", "1", "yes")

# Agents that have explicit hardcoded implementations (prefer those for now)
# Set to empty to use dynamic subagent for all agents
_HARDCODED_AGENTS: set = set()

# --- Phase configuration (loaded from config/phases.yaml) ---
_phase_cfg = get_phase_config()

PHASE_ORDER: List[str] = _phase_cfg.phase_order
EXECUTION_PHASES: List[str] = _phase_cfg.execution_phases

# Legacy phase map – kept as backward-compatible fallback when experts
# do not declare `scheduling.phase` in their YAML.  New experts should
# always include `scheduling.phase` so this dict can eventually be removed.
AGENT_PHASE_MAP: Dict[str, str] = {
    "planner": "PLANNING",
    "requirement-clarification": "ANALYSIS",
    "rules-management": "RULES",
    "business-form-operation": "OPERATIONS",
    "process-control": "PROCESS",
    "integration-requirements": "INTEGRATION",
    "ir-assembler": "DELIVERY",
    "validator": "DELIVERY",
}


def _should_use_dynamic_subagent(agent_type: str) -> bool:
    """
    Determine whether to use dynamic subagent execution.
    
    Returns True if:
    1. USE_DYNAMIC_SUBAGENT is enabled
    2. Agent is NOT in the hardcoded list
    
    Now defaults to True for all agents (configuration-driven approach).
    """
    if not USE_DYNAMIC_SUBAGENT:
        return False
    return agent_type not in _HARDCODED_AGENTS


def supervisor(state: DesignState) -> Dict[str, Any]:
    queue = state.get("task_queue", [])
    workflow_phase = state.get("workflow_phase", "INIT")
    cleared_routing = {
        "last_worker": "supervisor",
        "dispatched_tasks": [],
        "current_task_id": None,
        "current_task_ids": [],
    }

    if state.get("human_intervention_required"):
        return {"next": "END", **cleared_routing}

    # Check for actually running tasks based on state
    running_tasks = [task for task in queue if task["status"] == "running"]
    if running_tasks:
        # If tasks are already running (e.g. in parallel branch), we wait for them to re-enter supervisor
        return {"next": "END", **cleared_routing}

    executable_tasks = [task for task in queue if task.get("agent_type") != "planner"]
    unfinished_tasks = [task for task in executable_tasks if task.get("status") not in {"success", "skipped"}]
    if not unfinished_tasks:
        return {"next": "END", "workflow_phase": "DONE", "current_node": None, **cleared_routing}

    current_phase = _resolve_active_phase(workflow_phase, unfinished_tasks)
    limit = _resolve_parallel_limit(state)

    def build_dispatch(tasks: List[Task], phase: str) -> Dict[str, Any]:
        # Dynamic fan-out currently lacks a reliable barrier before the next
        # supervisor turn. Dispatching a single task at a time avoids the graph
        # ending early while later tasks in the same phase are still waiting.
        selected_tasks = tasks[:1] if tasks else []
        running_queue = _update_tasks_by_id(
            queue,
            [task["id"] for task in selected_tasks],
            "running",
        )
        dispatched_tasks = [{"id": task["id"], "agent_type": task["agent_type"]} for task in selected_tasks]
        if len(selected_tasks) == 1:
            task = selected_tasks[0]
            return {
                "next": task["agent_type"],
                "current_task_id": task["id"],
                "current_node": task["agent_type"],
                "task_queue": running_queue,
                "dispatched_tasks": dispatched_tasks,
                "workflow_phase": phase,
                "last_worker": "supervisor",
                "current_task_ids": [],
            }
        return {
            "next": [task["agent_type"] for task in selected_tasks],
            "current_task_ids": [task["id"] for task in selected_tasks],
            "current_node": selected_tasks[0]["agent_type"],
            "task_queue": running_queue,
            "dispatched_tasks": dispatched_tasks,
            "workflow_phase": phase,
            "last_worker": "supervisor",
            "current_task_id": None,
        }

    def ready_tasks_for_phase(phase: str) -> List[Task]:
        phase_tasks = [task for task in executable_tasks if _get_task_phase(task) == phase]
        todo_tasks = [task for task in phase_tasks if task["status"] == "todo"]
        return [
            task
            for task in sorted(todo_tasks, key=lambda item: item.get("priority", 0), reverse=True)
            if _dependencies_met(task, queue)
        ]

    current_phase_ready = ready_tasks_for_phase(current_phase)
    if current_phase_ready:
        # Mark dispatched tasks as running in the projected state so polling
        # clients can see all parallel branches immediately, without waiting
        # for each worker node to finish and emit its final update.
        return build_dispatch(current_phase_ready, current_phase)

    # If the current phase is blocked, advance to the earliest phase that still
    # has executable work instead of ending the graph and leaving the workflow in
    # a stale running state.
    for phase in EXECUTION_PHASES:
        if phase == current_phase:
            continue
        phase_ready = ready_tasks_for_phase(phase)
        if phase_ready:
            return build_dispatch(phase_ready, phase)

    blocked_tasks = [task for task in unfinished_tasks if task.get("status") == "todo"]
    blocked_agents = [task.get("agent_type") for task in blocked_tasks[:5] if task.get("agent_type")]
    waiting_reason = "Execution deadlocked: unfinished tasks remain, but no task is ready to run."
    if blocked_agents:
        waiting_reason += f" Blocked agents: {', '.join(blocked_agents)}."

    return {
        "next": "END",
        "workflow_phase": current_phase,
        "current_node": "supervisor",
        "waiting_reason": waiting_reason,
        **cleared_routing,
    }


def create_worker_node(agent_type: str):
    async def worker_node(state: DesignState) -> Dict[str, Any]:
        # Update our own status to 'running' in the queue as the very first step
        queue = state.get("task_queue", [])
        current_task_id = state.get("current_task_id")

        # If we are in a parallel branch, find our specific task by agent_type
        if not current_task_id:
            task = next((t for t in queue if t["agent_type"] == agent_type and t["status"] == "todo"), None)
            if task:
                current_task_id = task["id"]

        updated_queue = queue
        if current_task_id:
            updated_queue = _update_tasks_by_id(queue, [current_task_id], "running")

        state["task_queue"] = updated_queue
        # Inject current task id into state if missing (important for ID mapping)
        if current_task_id and not state.get("current_task_id"):
            state["current_task_id"] = current_task_id

        def _execution_guard() -> Dict[str, Any] | None:
            project_id = state.get("project_id")
            version = state.get("version")
            if not project_id or not version:
                return None

            try:
                from services import orchestrator_service as orch
            except Exception:
                return None

            thread_id = f"{project_id}_{version}"
            runtime = orch.runtime_registry.get(thread_id, {})
            active_job_id = runtime.get("job_id")
            state_run_id = state.get("run_id")
            if active_job_id and state_run_id and active_job_id != state_run_id:
                return {
                    "reason": f"workflow ownership moved to run {active_job_id}",
                    "status": None,
                    "failure_reason": "run_replaced",
                }

            runtime_status = runtime.get("run_status")
            if runtime_status in {orch.RUN_STATUS_FAILED, orch.RUN_STATUS_SUCCESS}:
                return {
                    "reason": f"workflow already {runtime_status}",
                    "status": None,
                    "failure_reason": "workflow_inactive",
                }

            projected_task = metadata_db.get_workflow_task(project_id, version, agent_type) or {}
            projected_status = projected_task.get("status")
            if projected_status in {"success", "failed", "skipped"}:
                return {
                    "reason": f"task already {projected_status} in workflow projection",
                    "status": None,
                    "failure_reason": "task_already_terminal",
                }

            peer_failed = next(
                (
                    task for task in metadata_db.list_workflow_tasks(project_id, version)
                    if task.get("node_type") != agent_type and task.get("status") == "failed"
                ),
                None,
            )
            if peer_failed:
                return {
                    "reason": f"peer expert {peer_failed.get('node_type')} already failed",
                    "status": "skipped",
                    "failure_reason": "peer_failed",
                }
            return None

        # =================================================================
        # Dynamic subagent execution (configuration-driven)
        # =================================================================
        try:
            result = await run_dynamic_subagent(
                capability=agent_type,
                state=state,
                base_dir=BASE_DIR,
                generate_with_llm_fn=generate_with_llm,
                execute_tool_fn=execute_tool,
                update_task_status_fn=_update_task_status,
                execution_guard_fn=_execution_guard,
            )
            result.setdefault("dispatched_tasks", [])
            result.setdefault("current_task_ids", [])
            result.setdefault("current_task_id", None)
            return result
        except Exception as e:
            return {
                "history": [f"[ERROR] Failed to run dynamic subagent {agent_type}: {e}"],
                "dispatched_tasks": [],
                "current_task_ids": [],
                "current_task_id": None,
            }

    return worker_node


def _update_task_status(queue: List[Task], agent_type: str, status: str) -> List[Task]:
    return [{**task, "status": status} if task["agent_type"] == agent_type else task for task in queue]


def _update_tasks_by_id(queue: List[Task], task_ids: List[str], status: str) -> List[Task]:
    task_id_set = set(task_ids)
    return [{**task, "status": status} if task["id"] in task_id_set else task for task in queue]


def _dependencies_met(task: Task, queue: List[Task]) -> bool:
    """Check if all dependencies of a task are satisfied.
    
    Implements weak dependency semantics:
    - Dependencies are only added for agents that were selected by planner
    - If a dependency task doesn't exist in queue, it means that agent was skipped
    - Only check status for dependencies that actually exist in the queue
    """
    for dep_id in task.get("dependencies", []):
        dep_task = next((queued_task for queued_task in queue if queued_task["id"] == dep_id), None)
        # Weak dependency: if task not in queue, it was skipped by planner - ignore
        if dep_task is None:
            continue
        # Strong dependency: task exists but not yet completed
        if dep_task["status"] != "success":
            return False
    return True


def _resolve_parallel_limit(state: DesignState) -> int:
    orchestrator_config = (state.get("design_context") or {}).get("orchestrator") or {}
    raw_limit = orchestrator_config.get("max_parallel_tasks", os.getenv("ORCHESTRATOR_MAX_PARALLEL", "2"))
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        return 2


def _get_base_phase(agent_type: str) -> str:
    """Resolve the execution phase for *agent_type* using a 3-tier fallback.

    1. ``config/phases.yaml`` expert assignment (surfaced via ExpertProfile.phase)
    2. Legacy ``scheduling.phase`` / ``AGENT_PHASE_MAP`` fallback
    3. Default – first executable phase
    """
    from registry.expert_registry import ExpertRegistry

    # Tier-1: check the resolved phase on the manifest, which now prefers phases.yaml.
    try:
        registry = ExpertRegistry.get_instance()
        manifest = registry.get_manifest(agent_type)
        if manifest and manifest.phase:
            if _phase_cfg.is_executable_phase(manifest.phase):
                return manifest.phase
    except RuntimeError:
        pass  # registry not initialized yet (e.g. during import-time)

    # Tier-2: legacy map
    mapped = AGENT_PHASE_MAP.get(agent_type)
    if mapped and _phase_cfg.is_executable_phase(mapped):
        return mapped

    # Tier-3: first executable phase
    return EXECUTION_PHASES[0] if EXECUTION_PHASES else "ANALYSIS"


def _phase_rank(phase: str) -> int:
    return _phase_cfg.phase_rank(phase)


def _get_task_phase(task: Task) -> str:
    if task.get("phase"):
        return task["phase"]
    metadata = task.get("metadata") or {}
    return str(metadata.get("workflow_phase") or _get_base_phase(task.get("agent_type", "")))


def _resolve_task_phases(tasks: List[Task]) -> Dict[str, str]:
    tasks_by_id: Dict[str, Task] = {task["id"]: task for task in tasks}
    non_planner_tasks = [task for task in tasks if task.get("agent_type") != "planner"]
    phase_cache: Dict[str, str] = {}

    def resolve_phase(task_id: str) -> str:
        if task_id in phase_cache:
            return phase_cache[task_id]
        task = tasks_by_id[task_id]
        resolved_phase = _get_base_phase(task.get("agent_type", ""))
        
        # We NO LONGER promote tasks to the next phase based on dependencies here.
        # Intra-phase dependencies are handled by the priority-based scheduler in the supervisor.
        # This keeps the UI consistent with the business phases defined in AGENT_PHASE_MAP.
        
        phase_cache[task_id] = resolved_phase
        return resolved_phase

    return {task["id"]: resolve_phase(task["id"]) for task in non_planner_tasks}


def _resolve_active_phase(workflow_phase: str, unfinished_tasks: List[Task]) -> str:
    unfinished_phases = {_get_task_phase(task) for task in unfinished_tasks}
    if workflow_phase in EXECUTION_PHASES and workflow_phase in unfinished_phases:
        return workflow_phase

    for phase in EXECUTION_PHASES:
        if phase in unfinished_phases:
            return phase
    return "DELIVERY"


def _annotate_execution_stages(tasks: List[Task]) -> List[Task]:
    phase_by_id = _resolve_task_phases(tasks)
    annotated_tasks: List[Task] = []

    for task in tasks:
        metadata = dict(task.get("metadata") or {})
        if task.get("agent_type") == "planner":
            metadata.setdefault("workflow_phase", "ANALYSIS")
            annotated_tasks.append({**task, "stage": 0, "phase": "ANALYSIS", "metadata": metadata})
            continue

        phase = phase_by_id.get(task["id"], _get_base_phase(task.get("agent_type", "")))
        stage = _phase_rank(phase) + 1
        metadata["execution_stage"] = stage
        metadata["workflow_phase"] = phase
        annotated_tasks.append({**task, "stage": stage, "phase": phase, "metadata": metadata})

    return annotated_tasks


def _build_task_queue(active_agents: set[str]) -> List[Task]:
    """Build task queue dynamically from expert configurations.
    
    This function now supports hot-pluggable experts by reading
    dependencies and priority from expert YAML configurations.
    
    Dependency resolution:
    - Each expert declares its dependencies in its YAML file
    - Dependencies are resolved to task IDs at runtime
    - Supports weak dependency semantics (missing deps are skipped)
    
    Built-in agents (validator, ir-assembler) have special handling:
    - ir-assembler: depends on all active requirement-analysis agents
    - validator: depends on ir-assembler
    """
    tasks: List[Task] = [
        {"id": "0", "agent_type": "planner", "status": "success", "dependencies": [], "priority": 100}
    ]
    
    # Build task ID mapping for dependency resolution
    task_id_map: Dict[str, str] = {"planner": "0"}
    task_counter = 1
    
    # Get expert configurations from registry
    try:
        from registry.expert_registry import ExpertRegistry
        registry = ExpertRegistry.get_instance()
        
        # Sort active agents by priority (higher priority first)
        expert_configs = []
        for agent in active_agents:
            if agent in {"validator", "ir-assembler"}:
                # Built-in agents handled separately
                continue
            manifest = registry.get_manifest(agent)
            if manifest:
                expert_configs.append((agent, manifest.priority, manifest.dependencies))
            else:
                # Expert not in registry, use defaults
                expert_configs.append((agent, 50, []))
        
        # Sort by priority descending for stable scheduling, but resolve dependencies in a second pass.
        # Otherwise a task can silently lose a dependency when it depends on a lower-priority expert
        # whose task id has not been assigned yet.
        expert_configs.sort(key=lambda x: x[1], reverse=True)

        for agent, _priority, _deps in expert_configs:
            task_id_map[agent] = str(task_counter)
            task_counter += 1

        # Create tasks for each expert after every selected expert has a stable task id.
        for agent, priority, deps in expert_configs:
            resolved_deps = ["0"]  # Always depend on planner
            for dep in deps:
                if dep in task_id_map:
                    resolved_deps.append(task_id_map[dep])

            tasks.append({
                "id": task_id_map[agent],
                "agent_type": agent,
                "status": "todo",
                "dependencies": resolved_deps,
                "priority": priority
            })
            
    except RuntimeError:
        # Fallback: registry not initialized, use default ordering
        default_order = [
            ("requirement-clarification", 95, []),
            ("rules-management", 85, ["requirement-clarification"]),
            ("business-form-operation", 80, ["requirement-clarification", "rules-management"]),
            ("process-control", 75, ["requirement-clarification", "rules-management", "business-form-operation"]),
            ("integration-requirements", 70, ["requirement-clarification", "process-control"]),
        ]
        
        active_defaults = [(agent, priority, deps) for agent, priority, deps in default_order if agent in active_agents]

        for agent, _priority, _deps in active_defaults:
            task_id_map[agent] = str(task_counter)
            task_counter += 1

        for agent, priority, deps in active_defaults:
            resolved_deps = ["0"]
            for dep in deps:
                if dep in task_id_map:
                    resolved_deps.append(task_id_map[dep])

            tasks.append({
                "id": task_id_map[agent],
                "agent_type": agent,
                "status": "todo",
                "dependencies": resolved_deps,
                "priority": priority
            })

    # Add ir-assembler only when it is explicitly enabled, or when validator
    # is enabled and requires it as a prerequisite.
    has_other_experts = len([t for t in tasks if t["id"] != "0"]) > 0
    should_include_assembler = (
        "ir-assembler" in active_agents or "validator" in active_agents
    )
    if should_include_assembler and has_other_experts:
        current_ids = [task["id"] for task in tasks if task["id"] != "0"]
        assembler_id = str(task_counter)
        task_counter += 1
        tasks.append({
            "id": assembler_id,
            "agent_type": "ir-assembler",
            "status": "todo",
            "dependencies": current_ids,
            "priority": 20
        })
        task_id_map["ir-assembler"] = assembler_id
        print(f"[DEBUG] _build_task_queue: Added ir-assembler, dependencies: {current_ids}")

    # Add validator (depends on ir-assembler)
    if "validator" in active_agents and has_other_experts:
        validator_id = str(task_counter)
        task_counter += 1
        task_id_map["validator"] = validator_id
        
        assembler_task = next((t for t in tasks if t["agent_type"] == "ir-assembler"), None)
        validator_deps = [assembler_task["id"]] if assembler_task else []
        tasks.append({
            "id": validator_id,
            "agent_type": "validator",
            "status": "todo",
            "dependencies": validator_deps,
            "priority": 10
        })
        print(f"[DEBUG] _build_task_queue: Added validator (in active_agents), dependencies: {validator_deps}")

    return _annotate_execution_stages(tasks)


def _format_execution_topology(tasks: List[Task]) -> str:
    """Format a readable execution plan from the resolved task queue."""
    tasks_by_id: Dict[str, Task] = {task["id"]: task for task in tasks}
    non_planner_tasks = [task for task in tasks if task.get("agent_type") != "planner"]

    if not non_planner_tasks:
        return ""

    phases: Dict[str, List[Task]] = {}
    for task in non_planner_tasks:
        phase = _get_task_phase(task)
        phases.setdefault(phase, []).append(task)

    lines = ["**Execution Topology:**", "- Stage 0: planner"]
    stage_number = 1
    for phase in EXECUTION_PHASES:
        phase_tasks = phases.get(phase, [])
        if not phase_tasks:
            continue

        has_intra_phase_dependencies = any(
            any(
                dep_id in tasks_by_id and _get_task_phase(tasks_by_id[dep_id]) == phase and tasks_by_id[dep_id].get("agent_type") != "planner"
                for dep_id in task.get("dependencies", [])
            )
            for task in phase_tasks
        )
        mode = "parallel" if len(phase_tasks) > 1 and not has_intra_phase_dependencies else "sequential"
        entries: List[str] = []
        for task in sorted(phase_tasks, key=lambda item: item.get("priority", 0), reverse=True):
            dependency_names = [
                tasks_by_id[dep_id]["agent_type"]
                for dep_id in task.get("dependencies", [])
                if dep_id in tasks_by_id and tasks_by_id[dep_id].get("agent_type") != "planner"
            ]
            if dependency_names:
                entries.append(f"{task['agent_type']} (after: {', '.join(dependency_names)})")
            else:
                entries.append(str(task["agent_type"]))
        lines.append(f"- Stage {stage_number} ({phase.lower()}, {mode}): {' | '.join(entries)}")
        stage_number += 1

    dependency_lines: List[str] = []
    for task in non_planner_tasks:
        dependency_names = [
            tasks_by_id[dep_id]["agent_type"]
            for dep_id in task.get("dependencies", [])
            if dep_id in tasks_by_id and tasks_by_id[dep_id].get("agent_type") != "planner"
        ]
        if dependency_names:
            dependency_lines.append(f"- {task['agent_type']} <- {', '.join(dependency_names)}")

    if dependency_lines:
        lines.append("")
        lines.append("**Dependency Graph:**")
        lines.extend(dependency_lines)

    return "\n".join(lines)


def _normalize_active_agents(active_agents: set[str]) -> set[str]:
    """Normalize agent IDs using aliases and validate against registry."""
    supported_ids = _get_supported_agent_ids()  # Get fresh list from registry
    normalized = set()
    for agent in active_agents:
        canonical_agent = AGENT_ALIASES.get(agent, agent)
        if canonical_agent in supported_ids:
            normalized.add(canonical_agent)
    return normalized


def _build_topic_ownership_payload(active_agents: set[str]) -> Dict[str, Any]:
    return build_topic_ownership_payload_from_registry(sorted(active_agents))


def _build_pending_interrupt(
    *,
    node_id: str,
    node_type: str,
    question: str,
    context: Dict[str, Any] | None = None,
    resume_target: str,
    interrupt_kind: str,
) -> Dict[str, Any]:
    normalized_context = context or {}
    return {
        "node_id": node_id,
        "node_type": node_type,
        "interrupt_id": str(uuid.uuid4()),
        "question": question,
        "context": normalized_context,
        "resume_target": resume_target,
        "interrupt_kind": interrupt_kind,
        "interaction_id": None,
        "owner_node": node_type,
        "question_schema": normalized_context.get("question_schema") if isinstance(normalized_context, dict) else None,
    }


def _normalize_interrupt_context(raw_context: Any) -> Dict[str, Any]:
    if not isinstance(raw_context, dict):
        return {}

    normalized_context = dict(raw_context)
    raw_options = normalized_context.get("options")
    if isinstance(raw_options, list):
        normalized_options = []
        for index, option in enumerate(raw_options):
            if isinstance(option, dict):
                value = str(option.get("value") or option.get("label") or f"option_{index + 1}").strip()
                label = str(option.get("label") or value).strip()
                description = str(option.get("description") or "").strip()
            else:
                value = str(option).strip()
                label = value
                description = ""
            if not value:
                continue
            normalized_options.append(
                {
                    "value": value,
                    "label": label or value,
                    "description": description,
                }
            )
        if normalized_options:
            normalized_context["options"] = normalized_options
        else:
            normalized_context.pop("options", None)
    else:
        normalized_context.pop("options", None)

    if "allow_free_text" not in normalized_context:
        normalized_context["allow_free_text"] = True
    return normalized_context


def _summarize_human_inputs(answer_entries: List[Dict[str, Any]], human_feedback: str = "") -> Dict[str, Any] | None:
    normalized_answers = [dict(entry) for entry in answer_entries if isinstance(entry, dict)]
    if human_feedback.strip():
        normalized_answers.append(
            {
                "interrupt_id": "manual-feedback",
                "answer": human_feedback.strip(),
                "summary": human_feedback.strip(),
            }
        )

    if not normalized_answers:
        return None

    summary_parts = []
    for entry in normalized_answers:
        summary = (entry.get("summary") or "").strip()
        answer = (entry.get("answer") or "").strip()
        selected_option = (entry.get("selected_option") or "").strip()
        selected_option_detail = entry.get("selected_option_detail") if isinstance(entry.get("selected_option_detail"), dict) else {}
        selected_options_detail = entry.get("selected_options_detail") if isinstance(entry.get("selected_options_detail"), list) else []
        answer_context = entry.get("answer_context") if isinstance(entry.get("answer_context"), dict) else {}
        if selected_option_detail:
            label = str(selected_option_detail.get("label") or selected_option_detail.get("value") or selected_option).strip()
            value = str(selected_option_detail.get("value") or selected_option).strip()
            description = str(selected_option_detail.get("description") or "").strip()
            selected_option_text = f"Selected option: {label}"
            if value and value != label:
                selected_option_text += f" ({value})"
            if description:
                selected_option_text += f" - {description}"
        elif selected_options_detail:
            option_texts = []
            for option in selected_options_detail:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or option.get("value") or "").strip()
                value = str(option.get("value") or "").strip()
                description = str(option.get("description") or "").strip()
                option_text = label
                if value and value != label:
                    option_text += f" ({value})"
                if description:
                    option_text += f" - {description}"
                if option_text:
                    option_texts.append(option_text)
            selected_option_text = f"Selected options: {'; '.join(option_texts)}" if option_texts else ""
        elif selected_option:
            selected_option_text = f"Selected option: {selected_option}"
        else:
            selected_option_text = ""
        context_parts = []
        if answer_context.get("why_needed"):
            context_parts.append(f"question rationale: {answer_context['why_needed']}")
        if answer_context.get("requirement_understanding"):
            context_parts.append(f"prior understanding: {answer_context['requirement_understanding']}")
        if answer_context.get("evidence_used"):
            context_parts.append(f"evidence used: {json.dumps(answer_context['evidence_used'], ensure_ascii=False)}")
        if selected_option_text and selected_option_text not in summary:
            summary = f"{selected_option_text}. {summary or answer}".strip()
        if context_parts:
            context_text = " | ".join(str(part) for part in context_parts if part)
            summary = f"{summary} [{context_text}]" if summary else context_text
        if summary and answer and summary != answer:
            summary_parts.append(f"{summary} 原文: {answer}")
        else:
            summary_parts.append(summary or answer)
    summary = "\n".join(f"- {part}" for part in summary_parts if part)
    return {
        "summary": summary,
        "analysis": "Human clarifications have been merged into the planning context and should be treated as authoritative supplements to the input materials.",
        "answers": normalized_answers,
    }


def _truncate_combined_input_text(value: str, max_chars: int = COMBINED_INPUT_QUERY_MAX_CHARS) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


def _build_combined_input_context(
    *,
    requirement_text: str,
    uploaded_files: List[str],
    structure_summary: List[Dict[str, Any]],
    human_inputs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    material_summaries: List[Dict[str, Any]] = []
    for entry in structure_summary:
        if not isinstance(entry, dict):
            continue
        material = {
            "path": entry.get("path") or entry.get("name"),
            "name": entry.get("name"),
            "summary_type": entry.get("summary_type"),
            "headings": entry.get("headings") or [],
            "top_level_keys": entry.get("top_level_keys") or [],
            "symbols": entry.get("symbols") or [],
            "content_excerpt": entry.get("content_excerpt") or "",
        }
        material_summaries.append({key: value for key, value in material.items() if value not in (None, "", [])})

    query_parts = []
    if str(requirement_text or "").strip():
        query_parts.append(f"Requirement Text:\n{requirement_text.strip()}")
    for material in material_summaries:
        material_lines = [f"Uploaded Material: {material.get('path') or material.get('name') or '(unknown)'}"]
        if material.get("headings"):
            material_lines.append(f"Headings: {json.dumps(material['headings'], ensure_ascii=False)}")
        if material.get("top_level_keys"):
            material_lines.append(f"Top Level Keys: {json.dumps(material['top_level_keys'], ensure_ascii=False)}")
        if material.get("symbols"):
            material_lines.append(f"Symbols: {json.dumps(material['symbols'], ensure_ascii=False)}")
        if material.get("content_excerpt"):
            material_lines.append(f"Excerpt:\n{material['content_excerpt']}")
        query_parts.append("\n".join(material_lines))
    if human_inputs:
        query_parts.append(f"Human Inputs:\n{json.dumps(human_inputs, ensure_ascii=False)}")

    query_text = _truncate_combined_input_text("\n\n".join(query_parts))
    return {
        "requirement_text": requirement_text,
        "uploaded_files": uploaded_files,
        "uploaded_materials": material_summaries,
        "human_inputs": human_inputs or {},
        "query_text": query_text,
    }


def _planner_success_task() -> List[Task]:
    return [{"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "success", "dependencies": [], "priority": 100}]


def _planner_waiting_task() -> List[Task]:
    return [{"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "waiting_human", "dependencies": [], "priority": 100}]


def _requirement_clarifier_running_task() -> List[Task]:
    return [{"id": "rq0", "agent_type": "requirement_clarifier", "stage": 0, "phase": "ANALYSIS", "status": "running", "dependencies": [], "priority": 110}]


def _requirement_clarifier_waiting_task() -> List[Task]:
    return [{"id": "rq0", "agent_type": "requirement_clarifier", "stage": 0, "phase": "ANALYSIS", "status": "waiting_human", "dependencies": [], "priority": 110}]


def _requirement_clarifier_success_task() -> List[Task]:
    return [{"id": "rq0", "agent_type": "requirement_clarifier", "stage": 0, "phase": "ANALYSIS", "status": "success", "dependencies": [], "priority": 110}]


def _requirement_clarifier_failed_task() -> List[Task]:
    return [{"id": "rq0", "agent_type": "requirement_clarifier", "stage": 0, "phase": "ANALYSIS", "status": "failed", "dependencies": [], "priority": 110}]


def _format_asset_insights_for_prompt(asset_insights: Dict[str, Any]) -> str:
    insight_sections: List[str] = []
    query_status = asset_insights.get("query_status", {}) if isinstance(asset_insights, dict) else {}
    query_errors = asset_insights.get("query_errors", []) if isinstance(asset_insights, dict) else []

    status_summary = []
    asset_label = {"database": "Database", "knowledge_base": "Knowledge Base", "repository": "Code Repository"}
    for asset_key, label in asset_label.items():
        st = query_status.get(asset_key, "skipped")
        if st == "skipped":
            status_summary.append(f"  - {label}: NOT CONFIGURED (no {asset_key.replace('_', ' ')} set up for this project)")
        elif st == "success":
            status_summary.append(f"  - {label}: queried successfully")
        elif st == "partial_failure":
            status_summary.append(f"  - {label}: PARTIAL FAILURE (some queries succeeded, some failed)")
        elif st == "failed":
            status_summary.append(f"  - {label}: QUERY FAILED (all queries errored, do NOT waste effort retrying)")
    if status_summary:
        insight_sections.append("Three-Repository Query Status:")
        insight_sections.extend(status_summary)

    if asset_insights.get("database_insights"):
        insight_sections.append("Database Insights (queried table structures):")
        for db in asset_insights["database_insights"]:
            insight_sections.append(
                f"  - DB '{db['name']}' ({db['type']}): {db['table_count']} tables: {', '.join(db['table_names'][:10])}"
            )
    if asset_insights.get("knowledge_base_insights"):
        insight_sections.append("Knowledge Base Insights (searched for RR-relevant content):")
        for kb in asset_insights["knowledge_base_insights"]:
            matches_summary = ", ".join(
                m.get("title") or m.get("feature_id") or ""
                for m in kb.get("top_matches", [])
            )[:200]
            insight_sections.append(
                f"  - KB '{kb['kb_name']}' (keyword '{kb['search_keyword']}'): {kb['match_count']} matches. Top: {matches_summary}"
            )
    if asset_insights.get("repository_insights"):
        insight_sections.append("Code Repository Insights (top-level structure):")
        for repo in asset_insights["repository_insights"]:
            insight_sections.append(
                f"  - Repo '{repo['name']}' (branch {repo['branch']}): dirs: {', '.join(repo['top_level_dirs'][:10])}"
            )
    if query_errors:
        insight_sections.append("Query Errors (do not retry unavailable resources in this clarification step):")
        for err_msg in query_errors[:6]:
            insight_sections.append(f"  - {err_msg}")

    return "\n".join(insight_sections)


def _normalize_requirement_clarification_context(raw_context: Any) -> Dict[str, Any]:
    normalized_context = _normalize_interrupt_context(raw_context)
    options = normalized_context.get("options") or []
    if len(options) < 2:
        options = [
            {
                "value": "confirm_current_understanding",
                "label": "按当前理解继续",
                "description": "确认系统基于 RR 和已配置资产形成的理解，可以作为后续 IR 分析范围。",
            },
            {
                "value": "provide_scope_adjustment",
                "label": "补充或调整边界",
                "description": "当前理解仍有偏差，需要在补充说明中修正目标、范围、约束或优先级。",
            },
        ]
    normalized_context["options"] = options[:5]
    normalized_context["allow_free_text"] = True
    normalized_context["question_schema"] = {
        "type": "single_select",
        "allow_free_text": True,
    }
    normalized_context.setdefault("topic", "requirement_clarification")
    normalized_context.setdefault("preferred_answer_type", "single_select")
    return normalized_context


def _build_requirement_clarification_question(
    requirement_text: str,
    prior_answers: List[Dict[str, Any]],
    *,
    uploaded_files: List[str],
    structure_summary: List[Dict[str, Any]],
    asset_context: Dict[str, Any],
    asset_insights: Dict[str, Any],
    llm_settings: Dict[str, Any] | None,
    project_id: str,
    version: str,
) -> Dict[str, Any] | None:
    normalized_requirement = str(requirement_text or "").strip()
    round_index = len([entry for entry in prior_answers if isinstance(entry, dict)])
    if round_index >= REQUIREMENT_CLARIFICATION_MAX_ROUNDS:
        return None

    system_prompt = f"""You are a senior BA requirement clarifier working in a ReAct style.
You must analyze the RR together with uploaded materials and configured project assets before deciding the next clarification.

Rules:
- Always use the provided observations from uploaded files, code repository, database, and knowledge base when they are available.
- On the first clarification round, you MUST ask exactly one choice-based question even if the RR text is long.
- On later rounds, ask one more question only when a remaining ambiguity materially affects IR scope, business rules, forms/actions, process, integration, roles, data, acceptance criteria, or delivery boundaries.
- Treat Prior Human Answers as authoritative. Do NOT ask a new question that is semantically equivalent to a prior answered question; instead, move to the next unresolved decision or stop asking.
- Stop asking when prior human answers are enough; unresolved minor points should be recorded as assumptions.
- The question must be concrete and business-aware. Do not use stiff boilerplate such as "为了开始需求分析，请先明确...".
- The question MUST be answerable by single choice, with 2 to 5 options. Free text is allowed for corrections.
- All natural-language output must be Simplified Chinese.

Output `clarification_decision` as a JSON object string:
{{
  "needs_human": true,
  "question": "一个具体的单选澄清问题",
  "context": {{
    "why_needed": "为什么这个问题会影响后续 IR 分析或专家选择",
    "options": [
      {{"value": "stable_option_id", "label": "选项名称", "description": "选这个选项意味着什么"}}
    ],
    "allow_free_text": true,
    "missing_information": ["scope_boundary"],
    "evidence_used": ["RR 文本", "数据库表结构", "知识库命中", "代码仓目录"]
  }},
  "requirement_understanding": "当前对目标、范围、规则、约束和资产线索的理解摘要",
  "assumptions": ["如果无需继续追问，写入可接受假设"]
}}"""

    user_prompt = (
        f"RR Text:\n{normalized_requirement or '(empty)'}\n\n"
        f"Clarification Round: {round_index + 1}/{REQUIREMENT_CLARIFICATION_MAX_ROUNDS}\n"
        f"Prior Human Answers: {json.dumps(prior_answers, ensure_ascii=False)}\n"
        f"Uploaded Files: {', '.join(uploaded_files) if uploaded_files else '(none)'}\n"
        f"Uploaded File Structures: {json.dumps(structure_summary, ensure_ascii=False)}\n"
    )
    if asset_context:
        user_prompt += f"\nConfigured Assets:\n{json.dumps(asset_context, ensure_ascii=False)}\n"
    formatted_insights = _format_asset_insights_for_prompt(asset_insights)
    if formatted_insights:
        user_prompt += f"\nThree-Repository Content Insights:\n{formatted_insights}\n"

    llm_decision = generate_with_llm(
        system_prompt,
        user_prompt,
        ["clarification_decision"],
        llm_settings=llm_settings,
        project_id=project_id,
        version=version,
        node_id="requirement_clarifier",
    )
    decision_payload = json.loads(llm_decision.artifacts.get("clarification_decision", "{}"))
    if not isinstance(decision_payload, dict):
        raise ValueError("Requirement clarifier LLM returned a non-object clarification_decision.")

    needs_human = bool(decision_payload.get("needs_human"))
    if round_index == 0:
        needs_human = True
    if not needs_human:
        return None

    context = _normalize_requirement_clarification_context(decision_payload.get("context"))
    question = str(decision_payload.get("question") or "").strip()
    if not question:
        understanding = str(decision_payload.get("requirement_understanding") or "").strip()
        question = "基于当前材料，下面哪种理解最接近本次 IR 分析的真实范围？" if understanding else "下面哪种方向最接近本次 RR 需要优先澄清的范围？"
    context.setdefault("requirement_understanding", decision_payload.get("requirement_understanding"))
    context.setdefault("assumptions", decision_payload.get("assumptions") or [])

    return {
        "question": question,
        "context": context,
        "reasoning": llm_decision.reasoning,
        "decision": decision_payload,
    }


def _append_planner_assumption_note(reasoning: str, question: str, context: Dict[str, Any] | None = None) -> str:
    normalized_reasoning = str(reasoning or "").strip()
    why_needed = ""
    if isinstance(context, dict):
        why_needed = str(context.get("why_needed") or "").strip()

    note_lines = [
        "规划阶段收到额外澄清请求，但根据当前流程约定，需求澄清应优先在 requirement_clarifier 阶段完成。",
        "本轮规划不会再次发起新的人工澄清，而是基于当前已确认信息继续生成专家推荐。",
    ]
    if question.strip():
        note_lines.append(f"未追加提问：{question.strip()}")
    if why_needed:
        note_lines.append(f"模型原始担忧：{why_needed}")

    appended_note = "\n".join(note_lines)
    if normalized_reasoning:
        return f"{normalized_reasoning}\n\n{appended_note}"
    return appended_note


async def bootstrap_node(state: DesignState) -> Dict[str, Any]:
    project_id = state["project_id"]
    version = state["version"]
    requirement_text = state.get("requirement", "")
    project_path = BASE_DIR / "projects" / project_id / version
    baseline_dir = project_path / "baseline"
    logs_dir = project_path / "logs"

    baseline_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if requirement_text:
        (baseline_dir / "raw-requirements.md").write_text(requirement_text, encoding="utf-8")

    resume_action = state.get("resume_action")
    resume_target_node = state.get("resume_target_node")
    existing_queue = state.get("task_queue") or []
    existing_planner = next((task for task in existing_queue if task.get("agent_type") == "planner"), None)
    if resume_target_node:
        return {
            "workflow_phase": state.get("workflow_phase", "ANALYSIS"),
            "history": [f"[SYSTEM] Bootstrap: resuming targeted node {resume_target_node} for {project_id}."],
            "last_worker": "bootstrap",
            "human_intervention_required": False,
            "waiting_reason": None,
            "resume_action": resume_action,
            "resume_target_node": resume_target_node,
            "current_node": resume_target_node,
        }
    if resume_action == "approve":
        return {
            "workflow_phase": state.get("workflow_phase", "ANALYSIS"),
            "history": [f"[SYSTEM] Bootstrap: resuming approved plan for {project_id}."],
            "last_worker": "bootstrap",
            "human_intervention_required": False,
            "waiting_reason": None,
            "resume_action": resume_action,
        }

    if resume_action == "revise":
        return {
            "workflow_phase": "ANALYSIS",
            "task_queue": [
                    {"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "running", "dependencies": [], "priority": 100}
            ],
            "history": [f"[SYSTEM] Bootstrap: restarting planner with human feedback for {project_id}."],
            "last_worker": "bootstrap",
            "human_intervention_required": False,
            "waiting_reason": None,
            "resume_action": resume_action,
        }

    if existing_planner and existing_planner.get("status") in {"running", "success", "failed"}:
        return {
            "workflow_phase": state.get("workflow_phase", "ANALYSIS"),
            "history": [f"[SYSTEM] Bootstrap: restored existing workflow state for {project_id}."],
            "last_worker": "bootstrap",
        }

    return {
        "workflow_phase": "ANALYSIS",
        "task_queue": [
                {"id": "rq0", "agent_type": "requirement_clarifier", "stage": 0, "phase": "ANALYSIS", "status": "running", "dependencies": [], "priority": 110}
        ],
        "history": [
            f"[SYSTEM] Bootstrap: initialized workflow context for {project_id}.",
            "[SYSTEM] Requirement clarifier started.",
        ],
        "last_worker": "bootstrap",
        "current_node": "requirement_clarifier",
    }


async def requirement_clarifier_node(state: DesignState) -> Dict[str, Any]:
    project_id = state["project_id"]
    version = state["version"]
    requirement_text = state.get("requirement", "")
    project_path = BASE_DIR / "projects" / project_id / version
    baseline_dir = project_path / "baseline"
    logs_dir = project_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    list_files_result = execute_tool("list_files", {"root_dir": str(baseline_dir)})
    uploaded_files = [file_info["name"] for file_info in list_files_result["output"].get("files", [])]
    extract_structure_result = execute_tool(
        "extract_structure",
        {
            "root_dir": str(baseline_dir),
            "files": [file_info["path"] for file_info in list_files_result["output"].get("files", [])],
        },
    )
    tool_results = [list_files_result, extract_structure_result]
    structure_summary = extract_structure_result["output"].get("files", [])
    asset_context = _build_project_asset_context(project_id)
    runtime_llm_settings = resolve_runtime_llm_settings(state.get("design_context"))
    clarifier_answers = ((state.get("human_answers") or {}).get("requirement_clarifier") or [])
    clarification_inputs = _summarize_human_inputs(clarifier_answers)
    combined_input_context = _build_combined_input_context(
        requirement_text=requirement_text,
        uploaded_files=uploaded_files,
        structure_summary=structure_summary,
        human_inputs={"requirement_clarifier": clarification_inputs} if clarification_inputs else None,
    )
    asset_insights = _query_asset_insights(project_id, combined_input_context.get("query_text") or requirement_text)
    try:
        question_payload = await asyncio.to_thread(
            _build_requirement_clarification_question,
            requirement_text,
            clarifier_answers,
            uploaded_files=uploaded_files,
            structure_summary=structure_summary,
            asset_context=asset_context,
            asset_insights=asset_insights,
            llm_settings=runtime_llm_settings,
            project_id=project_id,
            version=version,
        )
    except Exception as exc:
        error_message = f"Requirement clarifier LLM failed: {exc}"
        print(f"[ERROR] {error_message}")
        reasoning_sections = [
            REQUIREMENT_CLARIFIER_REASONING_TITLE,
            "",
            "**状态：** 需求澄清阶段调用 LLM 失败，流程已停止。",
            "",
            f"**错误：** {error_message}",
        ]
        (logs_dir / "requirement-clarifier-reasoning.md").write_text("\n".join(reasoning_sections), encoding="utf-8")
        return {
            "workflow_phase": "ANALYSIS",
            "task_queue": _requirement_clarifier_failed_task(),
            "history": [
                f"[ERROR] Requirement clarifier LLM connectivity or generation failed: {exc}",
            ],
            "human_intervention_required": False,
            "waiting_reason": error_message,
            "pending_interrupt": None,
            "run_status": "failed",
            "last_worker": "requirement_clarifier",
            "current_node": "requirement_clarifier",
            "tool_results": tool_results,
        }

    if question_payload:
        pending_interrupt = _build_pending_interrupt(
            node_id="rq0",
            node_type="requirement_clarifier",
            question=question_payload["question"],
            context=question_payload["context"],
            resume_target="requirement_clarifier",
            interrupt_kind="ask_human",
        )
        reasoning_sections = [
            REQUIREMENT_CLARIFIER_REASONING_TITLE,
            "",
            question_payload.get("reasoning") or "LLM 已结合 RR 材料和项目资产生成澄清问题。",
            "",
            f"**澄清问题：** {pending_interrupt['question']}",
        ]
        (logs_dir / "requirement-clarifier-reasoning.md").write_text("\n".join(reasoning_sections), encoding="utf-8")
        return {
            "workflow_phase": "ANALYSIS",
            "task_queue": _requirement_clarifier_waiting_task(),
            "history": [
                "[系统] 需求澄清阶段已结合 RR 材料和项目资产完成分析，正在请求人工选择确认。",
            ],
            "human_intervention_required": True,
            "waiting_reason": pending_interrupt["question"],
            "pending_interrupt": pending_interrupt,
            "run_status": "waiting_human",
            "last_worker": "requirement_clarifier",
            "current_node": "requirement_clarifier",
            "tool_results": tool_results,
        }

    clarification_summary = _summarize_human_inputs(clarifier_answers) if clarifier_answers else None
    history_lines = ["[系统] 需求澄清阶段已完成，工作流将进入规划阶段。"]
    if clarification_summary and clarification_summary.get("summary"):
        history_lines.append(f"[系统] 已记录需求澄清摘要：{clarification_summary['summary']}")
    reasoning_sections = [
        REQUIREMENT_CLARIFIER_REASONING_TITLE,
        "",
        "LLM 已结合现有 RR 材料、人工回答和项目资产判断：当前信息足以进入规划阶段。",
    ]
    if clarification_summary and clarification_summary.get("summary"):
        reasoning_sections.extend(["", f"**已确认澄清摘要：**\n{clarification_summary['summary']}"])
    (logs_dir / "requirement-clarifier-reasoning.md").write_text("\n".join(reasoning_sections), encoding="utf-8")
    return {
        "workflow_phase": "ANALYSIS",
        "task_queue": _requirement_clarifier_success_task(),
        "history": history_lines,
        "human_intervention_required": False,
        "waiting_reason": None,
        "pending_interrupt": None,
        "run_status": "running",
        "last_worker": "requirement_clarifier",
        "current_node": "requirement_clarifier",
        "tool_results": tool_results,
    }


async def planner_node(state: DesignState) -> Dict[str, Any]:
    project_id = state["project_id"]
    version = state["version"]
    requirement_text = state.get("requirement", "")
    project_path = BASE_DIR / "projects" / project_id / version
    baseline_dir = project_path / "baseline"

    list_files_result = execute_tool("list_files", {"root_dir": str(baseline_dir)})
    uploaded_files = [file_info["name"] for file_info in list_files_result["output"].get("files", [])]
    extract_structure_result = execute_tool(
        "extract_structure",
        {
            "root_dir": str(baseline_dir),
            "files": [file_info["path"] for file_info in list_files_result["output"].get("files", [])],
        },
    )
    tool_results = [list_files_result, extract_structure_result]
    structure_summary = extract_structure_result["output"].get("files", [])
    candidate_files = [f"baseline/{file_name}" for file_name in uploaded_files if isinstance(file_name, str) and file_name.strip()]

    def _sanitize_tool_context(output: Dict[str, Any], root_label: str) -> Dict[str, Any]:
        sanitized = json.loads(json.dumps(output, ensure_ascii=False))
        if isinstance(sanitized, dict):
            sanitized["root_dir"] = root_label
        return sanitized

    tool_context_payload = {
        "list_files": _sanitize_tool_context(list_files_result["output"], "baseline"),
        "extract_structure": _sanitize_tool_context(extract_structure_result["output"], "baseline"),
    }
    planner_answers = ((state.get("human_answers") or {}).get("planner") or [])
    clarifier_answers = ((state.get("human_answers") or {}).get("requirement_clarifier") or [])
    planner_selection_override = _extract_planner_expert_selection(planner_answers)
    human_feedback = state.get("human_feedback", "")
    human_inputs = _summarize_human_inputs(planner_answers, human_feedback)
    clarification_inputs = _summarize_human_inputs(clarifier_answers)
    combined_human_inputs: Dict[str, Any] = {}
    if clarification_inputs:
        combined_human_inputs["requirement_clarifier"] = clarification_inputs
    if human_inputs:
        combined_human_inputs["planner"] = human_inputs
    asset_context = _build_project_asset_context(project_id)
    combined_input_context = _build_combined_input_context(
        requirement_text=requirement_text,
        uploaded_files=uploaded_files,
        structure_summary=structure_summary,
        human_inputs=combined_human_inputs or None,
    )

    # Actively query three repositories for content insights
    asset_insights = _query_asset_insights(project_id, combined_input_context.get("query_text") or requirement_text)
    if asset_insights.get("query_errors"):
        for err in asset_insights["query_errors"][:5]:
            print(f"[DEBUG] Planner asset insight error: {err}")
    print(f"[DEBUG] Planner asset insights keys: {list(asset_insights.keys())}")

    # Get dynamic agent descriptions from AgentRegistry, filtered by project configuration
    from registry.agent_registry import AgentRegistry
    from services.db_service import metadata_db
    
    registry = AgentRegistry.get_instance()
    # Filter experts enabled for this project
    enabled_ids = _dedupe_preserve_order(metadata_db.list_enabled_expert_ids(project_id))
    # Always exclude internal system agents from requirement-analysis planning.
    analysis_expert_ids = [eid for eid in enabled_ids if eid != "expert-creator"]
    llm_selectable_expert_ids, default_selected_expert_ids = _split_planner_selectable_experts(registry, analysis_expert_ids)
    enabled_experts = set(analysis_expert_ids)
    llm_selectable_experts = set(llm_selectable_expert_ids)
    
    agent_descriptions = registry.get_planner_agent_descriptions(filter_ids=llm_selectable_expert_ids)
    if not agent_descriptions.strip():
        agent_descriptions = "(No requirement-analysis experts are currently enabled for this project. Please enable BA experts before running analysis.)"

    system_prompt = f"""You are an Expert BA Requirement Analysis Orchestrator.
Your task is to analyze RR (Raw Requirements), optional competitor references, and project context, then provide a tailored requirement-analysis pipeline that produces IR (IT Requirements).

Available Experts for this Project:
{agent_descriptions}

You MUST ONLY select from the 'Available Experts' listed above. These are the ONLY experts enabled for this project.
If a required requirement-analysis domain is NOT available in the list, explain this gap in your reasoning and proceed with available ones.
Select experts strictly based on the requirement and their documented capabilities.
Some enabled experts may be default-selected by configuration and intentionally omitted from this LLM selection list. Do not invent or select experts that are not listed above.
Evaluate the current input materials, uploaded file structure, and any prior human clarifications.
Assume downstream expert controllers default to single-step ReAct and only permit short read-only action batches for evidence gathering.
Treat this as a planning and BA expert-selection stage:
- Requirement clarification has already been handled before this node. Do NOT ask the human any new clarification questions during normal planning.
- You should still explain unresolved assumptions in reasoning, but continue planning with the best grounded expert recommendation available from the current materials.
- Only use needs_human=true as an exceptional fallback when the workflow cannot continue at all because no meaningful expert recommendation can be formed.
- Do not ask for optional nice-to-have details.
- In reasoning, explicitly explain which parts of the provided materials were sufficient and which residual assumptions remain.
- All natural-language output for `reasoning`, `question`, `why_needed`, option `label`, and option `description` MUST be written in Simplified Chinese.
- Keep JSON keys, expert ids, tool names, file paths, and phase ids unchanged in English when they are machine-readable identifiers.

Output JSON format:
{{
  "reasoning": "请用简体中文说明你基于 RR、竞品参考和上传文件进行专家选择的分析过程。",
  "artifacts": {{
    "active_agents": ["expert-id-1", "expert-id-2"],
    "needs_human": false,
    "question": "",
    "context": {{
      "missing_information": ["field_name"],
      "why_needed": "请用简体中文说明为什么这个澄清会影响 IR 质量。",
      "options": [
        {{"value": "option_value", "label": "选项名称（简体中文）", "description": "该选项适用场景的中文说明。"}}
      ],
      "allow_free_text": true
    }}
  }}
}}"""

    user_prompt = (
        f"RR Text: {requirement_text}\n"
        f"Combined Input Materials: {json.dumps(combined_input_context, ensure_ascii=False)}\n"
        f"Uploaded Files: {', '.join(uploaded_files)}\n"
        f"Uploaded File Structures: {json.dumps(structure_summary, ensure_ascii=False)}\n"
        "Evaluate whether the existing materials already provide enough information to select the BA requirement-analysis experts."
    )
    if asset_context:
        user_prompt += f"\nConfigured Assets: {json.dumps(asset_context, ensure_ascii=False)}"
    # Inject three-repository content insights for deeper analysis
    insight_sections = []
    query_status = asset_insights.get("query_status", {})
    query_errors = asset_insights.get("query_errors", [])

    # Summarize which assets were queried and their statuses
    status_summary = []
    asset_label = {"database": "Database", "knowledge_base": "Knowledge Base", "repository": "Code Repository"}
    for asset_key, label in asset_label.items():
        st = query_status.get(asset_key, "skipped")
        if st == "skipped":
            status_summary.append(f"  - {label}: NOT CONFIGURED (no {asset_key.replace('_', ' ')} set up for this project)")
        elif st == "success":
            status_summary.append(f"  - {label}: queried successfully")
        elif st == "partial_failure":
            status_summary.append(f"  - {label}: PARTIAL FAILURE (some queries succeeded, some failed)")
        elif st == "failed":
            status_summary.append(f"  - {label}: QUERY FAILED (all queries errored, do NOT waste effort retrying)")
    if status_summary:
        insight_sections.append("Three-Repository Query Status:")
        insight_sections.extend(status_summary)

    if asset_insights.get("database_insights"):
        insight_sections.append("Database Insights (queried table structures):")
        for db in asset_insights["database_insights"]:
            insight_sections.append(
                f"  - DB '{db['name']}' ({db['type']}): {db['table_count']} tables: {', '.join(db['table_names'][:10])}"
            )
    if asset_insights.get("knowledge_base_insights"):
        insight_sections.append("Knowledge Base Insights (searched for requirement-relevant content):")
        for kb in asset_insights["knowledge_base_insights"]:
            matches_summary = ", ".join(
                m.get("title") or m.get("feature_id") or ""
                for m in kb.get("top_matches", [])
            )[:200]
            insight_sections.append(
                f"  - KB '{kb['kb_name']}' (keyword '{kb['search_keyword']}'): {kb['match_count']} matches. Top: {matches_summary}"
            )
    if asset_insights.get("repository_insights"):
        insight_sections.append("Code Repository Insights (top-level structure):")
        for repo in asset_insights["repository_insights"]:
            insight_sections.append(
                f"  - Repo '{repo['name']}' (branch {repo['branch']}): dirs: {', '.join(repo['top_level_dirs'][:10])}"
            )
    # Append query errors so experts know which resources are unavailable
    if query_errors:
        insight_sections.append("Query Errors (experts should avoid retrying these):")
        for err_msg in query_errors[:6]:
            insight_sections.append(f"  - {err_msg}")
    if insight_sections:
        user_prompt += "\n\n### Three-Repository Content Insights\n" + "\n".join(insight_sections)
    if clarification_inputs:
        user_prompt += f"\nRequirement Clarifications: {json.dumps(clarification_inputs, ensure_ascii=False)}"
    if human_feedback:
        user_prompt += f"\nHuman Revision Feedback: {human_feedback}"
    if human_inputs:
        user_prompt += f"\nHuman Clarifications: {json.dumps(human_inputs, ensure_ascii=False)}"
    llm_decision = SubagentOutput(reasoning="", artifacts={"active_agents": "[]"})
    decision_data: Any = []
    needs_human = False
    ask_human_question = ""
    ask_human_context: Dict[str, Any] = {}
    active_agents: set[str] = set()
    policy_auto_selected: List[str] = []
    runtime_llm_settings = resolve_runtime_llm_settings(state.get("design_context"))

    if planner_selection_override is not None:
        selected_by_human = set(planner_selection_override.get("selected_experts") or [])
        recommended_by_planner = planner_selection_override.get("recommended_experts") or []
        active_agents = _normalize_active_agents(selected_by_human)
        print(f"[DEBUG] Planner: using human-selected experts override: {sorted(active_agents)}")
        print(f"[DEBUG] Planner: allowed design_experts for this project: {sorted(enabled_experts)}")
        if enabled_experts:
            active_agents = {agent for agent in active_agents if agent in enabled_experts}
        else:
            active_agents = set()

        added_experts = sorted(active_agents - set(recommended_by_planner))
        removed_experts = sorted(set(recommended_by_planner) - active_agents)
        override_reasoning_sections = [
            "规划器推荐结果已在执行前由人工复核。",
            f"规划器推荐专家：{_format_expert_list(recommended_by_planner)}。",
            f"人工最终确认专家：{_format_expert_list(sorted(active_agents))}。",
        ]
        if added_experts:
            override_reasoning_sections.append(f"人工新增专家：{_format_expert_list(added_experts)}。")
        if removed_experts:
            override_reasoning_sections.append(f"人工移除专家：{_format_expert_list(removed_experts)}。")
        if human_feedback.strip():
            override_reasoning_sections.append(f"人工备注：{human_feedback.strip()}")
        llm_decision = SubagentOutput(
            reasoning="\n".join(override_reasoning_sections),
            artifacts={"active_agents": json.dumps(sorted(active_agents), ensure_ascii=False)},
        )
        decision_data = {"active_agents": sorted(active_agents), "source": "human_override"}
    else:
        try:
            print("[DEBUG] Planner: Calling LLM for intent analysis...")
            llm_decision = await asyncio.to_thread(
                generate_with_llm, 
                system_prompt, 
                user_prompt, 
                ["active_agents"],
                llm_settings=runtime_llm_settings,
                project_id=project_id,
                version=version,
                node_id="planner"
            )

            decision_data = json.loads(llm_decision.artifacts.get("active_agents", "[]"))
            if isinstance(decision_data, dict):
                active_agents = set(decision_data.get("active_agents", []))
                needs_human = bool(decision_data.get("needs_human"))
                ask_human_question = (decision_data.get("question") or "").strip()
                ask_human_context = _normalize_interrupt_context(decision_data.get("context"))
                if needs_human:
                    llm_decision = SubagentOutput(
                        reasoning=_append_planner_assumption_note(
                            llm_decision.reasoning,
                            ask_human_question,
                            ask_human_context,
                        ),
                        artifacts=llm_decision.artifacts,
                    )
                    needs_human = False
                    ask_human_question = ""
                    ask_human_context = {}
            elif isinstance(decision_data, list):
                active_agents = set(decision_data)
            else:
                raise ValueError(f"Planner LLM returned unsupported active_agents payload: {type(decision_data).__name__}")
        except Exception as exc:
            error_message = f"Planner LLM failed: {exc}"
            print(f"[ERROR] {error_message}")
            reasoning_sections = [
                PLANNER_REASONING_TITLE,
                "",
                "**状态：** 规划器调用 LLM 失败，流程已停止。",
                "",
                f"**错误：** {error_message}",
            ]
            (project_path / "logs" / "planner-reasoning.md").write_text("\n".join(reasoning_sections), encoding="utf-8")
            return {
                "workflow_phase": "PLANNING",
                "task_queue": _planner_failed_task(),
                "history": [
                    f"[ERROR] Planner LLM connectivity or generation failed: {exc}",
                ],
                "human_intervention_required": False,
                "waiting_reason": error_message,
                "pending_interrupt": None,
                "run_status": "failed",
                "last_worker": "planner",
                "current_node": "planner",
                "tool_results": tool_results,
            }

        active_agents = _normalize_active_agents(active_agents)
        print(f"[DEBUG] Planner: active_agents after normalization: {sorted(active_agents)}")
        print(f"[DEBUG] Planner: allowed LLM-selectable design_experts for this project: {sorted(llm_selectable_experts)}")

        if llm_selectable_experts:
            # Only use experts that are explicitly enabled
            active_agents = {agent for agent in active_agents if agent in llm_selectable_experts}
            print(f"[DEBUG] Planner: final filtered active_agents: {sorted(active_agents)}")
        else:
            # If no experts are enabled, we MUST NOT fallback to "all"
            print(f"[DEBUG] Planner: No requirement-analysis experts are enabled for this project. Clearing selection.")
            active_agents = set()

        # Apply generic policy-driven auto-selection from expert YAML.
        pre_policy_agents = set(active_agents)
        active_agents = _apply_policy_based_auto_selection(
            active_agents=active_agents,
            enabled_experts=llm_selectable_experts,
            requirement_text=combined_input_context.get("query_text") or requirement_text,
            human_inputs=combined_human_inputs or human_inputs,
        )
        policy_auto_selected = sorted(active_agents - pre_policy_agents)

    should_apply_default_selection = planner_selection_override is None and any(
        agent in llm_selectable_experts for agent in active_agents
    )
    if should_apply_default_selection:
        active_agents.update(agent for agent in default_selected_expert_ids if agent in enabled_experts)
    configured_default_selected = sorted(set(default_selected_expert_ids) & active_agents)
    
    # Early return if human intervention is needed - don't build full task queue yet
    if needs_human:
        print(f"[DEBUG] Planner: needs_human=True, returning early without building task queue")
        pending_interrupt = _build_pending_interrupt(
            node_id="planner",
            node_type="planner",
            question=ask_human_question or "请先补充当前规划缺失的关键信息，工作流才能继续执行。",
            context=ask_human_context,
            resume_target="planner",
            interrupt_kind="ask_human",
        )
        
        # Write reasoning without pipeline info (since we don't have it yet)
        reasoning_sections = [
            PLANNER_REASONING_TITLE,
            "",
            llm_decision.reasoning,
            "",
            "**状态：** 规划器需要人工补充信息后才能继续选择执行专家。",
        ]
        reasoning_content = "\n".join(reasoning_sections)
        (project_path / "logs" / "planner-reasoning.md").write_text(reasoning_content, encoding="utf-8")
        topic_ownership = _build_topic_ownership_payload(set())
        
        baseline_payload = {
            "project_name": project_id,
            "project_id": project_id,
            "version": version,
            "requirement": requirement_text,
            "uploaded_files": uploaded_files,
            "candidate_files": candidate_files,
            "combined_input_context": combined_input_context,
            "project_layout": {
                "project_root": ".",
                "baseline_dir": "baseline",
                "artifacts_dir": "artifacts",
                "evidence_dir": "evidence",
            },
            "tool_context": tool_context_payload,
            "active_agents": [],  # Not decided yet
            "topic_ownership": topic_ownership,
            "domain_name": "Domain",
            "aggregate_root": "Entity",
            "provider": "ExternalSystem",
            "consumer": "ConsumerSystem",
        }
        if asset_context:
            baseline_payload["configured_assets"] = asset_context
        if asset_insights and any(k.endswith("_insights") for k in asset_insights):
            # Only pass down insights and query metadata, omit raw errors from payload
            payload_insights = {k: v for k, v in asset_insights.items() if k.endswith("_insights")}
            payload_insights["query_status"] = asset_insights.get("query_status", {})
            payload_insights["query_errors"] = asset_insights.get("query_errors", [])
            baseline_payload["asset_insights"] = payload_insights
        if combined_human_inputs:
            baseline_payload["human_inputs"] = combined_human_inputs
        (baseline_dir / "requirements.json").write_text(
            json.dumps(baseline_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        return {
            "workflow_phase": "PLANNING",
            "task_queue": _planner_waiting_task(),
            "history": [
                "[系统] 规划器检测到关键信息不足，正在请求人工澄清。",
            ],
            "human_intervention_required": True,
            "waiting_reason": pending_interrupt["question"],
            "pending_interrupt": pending_interrupt,
            "run_status": "waiting_human",
            "last_worker": "planner",
            "current_node": "planner",
            "tool_results": tool_results,
        }

    if enabled_experts and planner_selection_override is None:
        recommended_experts = sorted(active_agents)
        pending_interrupt = _build_pending_interrupt(
            node_id="planner",
            node_type="planner",
            question="请确认本次参与执行的专家。开始执行前，你可以补充专家或取消勾选。",
            context=_build_expert_selection_interrupt_context(
                enabled_expert_ids=analysis_expert_ids,
                recommended_expert_ids=recommended_experts,
                auto_selected_expert_ids=sorted(set(policy_auto_selected) | set(configured_default_selected)),
            ),
            resume_target="planner",
            interrupt_kind=PLANNER_EXPERT_SELECTION_INTERACTION,
        )

        reasoning_sections = [
            PLANNER_REASONING_TITLE,
            "",
            llm_decision.reasoning,
            "",
            f"**规划器推荐专家：** {_format_expert_list(recommended_experts)}",
            f"**配置默认选中专家：** {_format_expert_list(configured_default_selected)}",
            "",
            "**状态：** 等待人工确认本次执行专家。",
        ]
        reasoning_content = "\n".join(reasoning_sections)
        (project_path / "logs" / "planner-reasoning.md").write_text(reasoning_content, encoding="utf-8")
        topic_ownership = _build_topic_ownership_payload(set(recommended_experts))

        baseline_payload = {
            "project_name": project_id,
            "project_id": project_id,
            "version": version,
            "requirement": requirement_text,
            "uploaded_files": uploaded_files,
            "candidate_files": candidate_files,
            "combined_input_context": combined_input_context,
            "project_layout": {
                "project_root": ".",
                "baseline_dir": "baseline",
                "artifacts_dir": "artifacts",
                "evidence_dir": "evidence",
            },
            "tool_context": tool_context_payload,
            "active_agents": sorted(active_agents),
            "planner_recommended_experts": recommended_experts,
            "configured_default_selected_experts": configured_default_selected,
            "topic_ownership": topic_ownership,
            "domain_name": "Domain",
            "aggregate_root": "Entity",
            "provider": "ExternalSystem",
            "consumer": "ConsumerSystem",
        }
        if asset_context:
            baseline_payload["configured_assets"] = asset_context
        if asset_insights and any(k.endswith("_insights") for k in asset_insights):
            payload_insights = {k: v for k, v in asset_insights.items() if k.endswith("_insights")}
            payload_insights["query_status"] = asset_insights.get("query_status", {})
            payload_insights["query_errors"] = asset_insights.get("query_errors", [])
            baseline_payload["asset_insights"] = payload_insights
        if combined_human_inputs:
            baseline_payload["human_inputs"] = combined_human_inputs
        (baseline_dir / "requirements.json").write_text(
            json.dumps(baseline_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return {
            "workflow_phase": "PLANNING",
            "task_queue": _planner_waiting_task(),
            "history": [
                "[系统] 规划器已给出专家推荐，等待人工确认。",
            ],
            "human_intervention_required": True,
            "waiting_reason": pending_interrupt["question"],
            "pending_interrupt": pending_interrupt,
            "run_status": "waiting_human",
            "last_worker": "planner",
            "current_node": "planner",
            "tool_results": tool_results,
        }
    
    if not active_agents:
        print("[DEBUG] Planner: No active_agents selected by LLM.")
        reasoning_sections = [
            PLANNER_REASONING_TITLE,
            "",
            llm_decision.reasoning,
            "",
            "**状态：** 当前需求未选出任何可执行专家，流程无法继续。",
        ]
        reasoning_content = "\n".join(reasoning_sections)
        (project_path / "logs" / "planner-reasoning.md").write_text(reasoning_content, encoding="utf-8")
        
        return {
            "workflow_phase": "PLANNING",
            "task_queue": _planner_success_task(), # Use success task for planner itself
            "history": [
                "[系统] 规划器未识别到适合当前 RR 的需求分析专家。",
            ],
            "human_intervention_required": False,
            "waiting_reason": "当前没有选中任何需求分析专家，请补充或调整 RR 后重试。",
            "run_status": "failed",
            "last_worker": "planner",
            "current_node": "planner",
            "tool_results": tool_results,
        }

    # Build task queue only when we have a clear pipeline
    configured_default_selected = sorted(set(default_selected_expert_ids) & active_agents)
    tasks = _build_task_queue(active_agents)
    print(f"[DEBUG] Planner: task_queue built with {len(tasks)} tasks: {[t['agent_type'] for t in tasks]}")
    topic_ownership = _build_topic_ownership_payload(active_agents)

    execution_topology = _format_execution_topology(tasks)
    reasoning_sections = [
        PLANNER_REASONING_TITLE,
        "",
        llm_decision.reasoning,
        "",
        f"**最终选中专家：** {', '.join(sorted(list(active_agents)))}",
    ]
    if configured_default_selected:
        reasoning_sections.extend(["", f"**配置默认选中专家：** {_format_expert_list(configured_default_selected)}"])
    if execution_topology:
        reasoning_sections.extend(["", execution_topology])
    reasoning_content = "\n".join(reasoning_sections)
    (project_path / "logs" / "planner-reasoning.md").write_text(reasoning_content, encoding="utf-8")

    baseline_payload = {
        "project_name": project_id,
        "project_id": project_id,
        "version": version,
        "requirement": requirement_text,
        "uploaded_files": uploaded_files,
        "candidate_files": candidate_files,
        "combined_input_context": combined_input_context,
        "project_layout": {
            "project_root": ".",
            "baseline_dir": "baseline",
            "artifacts_dir": "artifacts",
            "evidence_dir": "evidence",
        },
        "tool_context": tool_context_payload,
        "active_agents": list(active_agents),
        "configured_default_selected_experts": configured_default_selected,
        "topic_ownership": topic_ownership,
        "domain_name": "Domain",
        "aggregate_root": "Entity",
        "provider": "ExternalSystem",
        "consumer": "ConsumerSystem",
    }
    if asset_context:
        baseline_payload["configured_assets"] = asset_context
    if asset_insights and any(k.endswith("_insights") for k in asset_insights):
        # Only pass down insights and query metadata, omit raw errors from payload
        payload_insights = {k: v for k, v in asset_insights.items() if k.endswith("_insights")}
        payload_insights["query_status"] = asset_insights.get("query_status", {})
        payload_insights["query_errors"] = asset_insights.get("query_errors", [])
        baseline_payload["asset_insights"] = payload_insights
    if combined_human_inputs:
        baseline_payload["human_inputs"] = combined_human_inputs
    (baseline_dir / "requirements.json").write_text(
        json.dumps(baseline_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "workflow_phase": "ANALYSIS",
        "task_queue": tasks,
        "history": [
            "[SYSTEM] Planner: LLM-driven intent analysis completed and baseline initialized.",
            "[SYSTEM] Planner finished.",
        ],
        "human_intervention_required": False,
        "waiting_reason": None,
        "pending_interrupt": None,
        "run_status": "running",
        "last_worker": "planner",
        "current_node": "planner",
        "tool_results": tool_results,
    }

