import asyncio
from contextlib import asynccontextmanager, contextmanager
import datetime
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from graphs.builder import CHECKPOINT_DB_PATH, CHECKPOINTS_DIR, create_design_graph
from graphs.state import merge_artifacts
from models.events import dump_event, validate_event_payload
from services.log_service import format_run_log_entry, get_run_log, run_log_dedupe_key, save_run_log
from services.db_service import JSON_UNSET, metadata_db
from services.artifact_governance_runtime import finalize_expert_artifact_outputs
from services.design_artifact_service import sync_artifacts_from_disk
from services.llm_service import resolve_runtime_llm_settings, test_llm_connectivity
from registry.expert_registry import ExpertRegistry

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"
EXPERTS_DIR = BASE_DIR / "experts"
LEGACY_SUBAGENTS_DIR = BASE_DIR / "subagents"
SKILLS_DIR = BASE_DIR / "skills"
EXPERT_CENTER_VERSIONS_DIR = BASE_DIR / ".expert-center-versions"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_HUMAN = "waiting_human"
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_SCHEDULED = "scheduled"
PLANNER_EXPERT_SELECTION_INTERACTION = "expert_selection"
PLANNER_EXPERT_SELECTION_QUESTION = (
    "Review the planner's expert selection. "
    "You can add experts or remove selected experts before execution starts."
)
PLANNER_EXPERT_SELECTION_WAIT_LOG_MARKERS = (
    "规划器已给出专家推荐，等待人工确认",
    "Planner has finished the initial expert recommendation",
)
STALE_RUNNING_TIMEOUT_SECONDS = int(os.getenv("ORCHESTRATOR_STALE_TIMEOUT_SECONDS", "180"))

jobs = {}
runtime_registry = {}
runtime_tasks = {}
scheduled_runtime_tasks = {}
RESERVED_PROJECT_DIR_NAMES = {"cloned_repos"}


def _extract_localized_expert_names(config: dict) -> tuple[str, str]:
    name = str(config.get("name") or "").strip()
    name_zh = str(config.get("name_zh") or "").strip()
    name_en = str(config.get("name_en") or name or "").strip()
    return name_zh, name_en


def _normalize_expert_profile_yaml(content: str, *, expert_id: str, existing_profile_path: Path | None = None) -> str:
    """Normalize expert profile YAML so bilingual name fields stay present."""
    try:
        profile = yaml.safe_load(content) or {}
    except Exception as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc

    if not isinstance(profile, dict):
        raise ValueError("Expert profile YAML must be a mapping object.")

    existing_profile: dict = {}
    if existing_profile_path and existing_profile_path.exists():
        try:
            existing_profile = yaml.safe_load(existing_profile_path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing_profile = {}
        if not isinstance(existing_profile, dict):
            existing_profile = {}

    existing_name = str(existing_profile.get("name") or "").strip()
    existing_name_en = str(existing_profile.get("name_en") or existing_name or "").strip()
    existing_name_zh = str(existing_profile.get("name_zh") or "").strip()

    name = str(profile.get("name") or profile.get("name_en") or existing_name_en or expert_id).strip()
    name_en = str(profile.get("name_en") or name or existing_name_en or expert_id).strip()
    name_zh = str(profile.get("name_zh") or existing_name_zh or "").strip()
    capability = str(profile.get("capability") or existing_profile.get("capability") or expert_id).strip()

    if not capability:
        capability = expert_id
    if not name:
        name = name_en or capability
    if not name_en:
        name_en = name or capability

    profile["name"] = name
    profile["name_en"] = name_en
    profile["name_zh"] = name_zh
    profile["capability"] = capability

    return yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)


def _resolve_localized_expert_names(expert_id: str, config: dict) -> tuple[str, str]:
    name_zh, name_en = _extract_localized_expert_names(config)
    try:
        manifest = ExpertRegistry.get_instance().get_manifest(expert_id)
    except RuntimeError:
        manifest = None
    if manifest:
        name_zh = name_zh or manifest.name_zh or ""
        name_en = name_en or manifest.name_en or manifest.name or expert_id
    return name_zh, name_en


def _is_project_internal_dir_name(name: str) -> bool:
    return name.startswith(".") or name in RESERVED_PROJECT_DIR_NAMES


@contextmanager
def _graph_for_state():
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(CHECKPOINT_DB_PATH)) as checkpointer:
            yield create_design_graph(checkpointer=checkpointer)
            return
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver

        yield create_design_graph(checkpointer=MemorySaver())


@asynccontextmanager
async def _graph_for_run():
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        saver_cm = AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB_PATH))
        checkpointer = await saver_cm.__aenter__()
    except Exception:
        from langgraph.checkpoint.memory import MemorySaver
        yield create_design_graph(checkpointer=MemorySaver())
        return

    try:
        yield create_design_graph(checkpointer=checkpointer)
    finally:
        await saver_cm.__aexit__(None, None, None)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize_string_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if item:
            normalized.append(item)
    return normalized


def _is_planner_expert_selection_wait_log(message: Any) -> bool:
    text = str(message or "")
    return any(marker in text for marker in PLANNER_EXPERT_SELECTION_WAIT_LOG_MARKERS)


def _is_planner_expert_selection_pending(state: Dict[str, Any] | None) -> bool:
    pending_interrupt = (state or {}).get("pending_interrupt") or {}
    if not isinstance(pending_interrupt, dict):
        return False
    context = pending_interrupt.get("context") if isinstance(pending_interrupt.get("context"), dict) else {}
    return (
        str((state or {}).get("run_status") or "").strip() == RUN_STATUS_WAITING_HUMAN
        and str(pending_interrupt.get("node_type") or "").strip() == "planner"
        and (
            str(pending_interrupt.get("interrupt_kind") or "").strip() == PLANNER_EXPERT_SELECTION_INTERACTION
            or str(context.get("interaction_type") or "").strip() == PLANNER_EXPERT_SELECTION_INTERACTION
            or isinstance(context.get("available_experts"), list)
        )
    )


def _filter_stale_planner_expert_selection_wait_logs(logs: List[Any], state: Dict[str, Any] | None) -> List[str]:
    normalized_logs = [str(log) for log in logs if str(log or "").strip()]
    if _is_planner_expert_selection_pending(state):
        return normalized_logs
    return [
        log
        for log in normalized_logs
        if not _is_planner_expert_selection_wait_log(log)
    ]


def _infer_interaction_scope(pending_interrupt: Dict[str, Any]) -> str:
    node_type = str(pending_interrupt.get("node_type") or "").strip()
    interrupt_kind = str(pending_interrupt.get("interrupt_kind") or "").strip()
    context = pending_interrupt.get("context") or {}
    interaction_type = str(context.get("interaction_type") or "").strip()
    if node_type == "requirement_clarifier":
        return "requirement_clarification"
    if node_type == "planner" and interaction_type == PLANNER_EXPERT_SELECTION_INTERACTION:
        return "planner_review"
    if node_type == "planner":
        return "requirement_clarification" if interrupt_kind == "ask_human" else "planner_review"
    return "expert_clarification" if interrupt_kind == "ask_human" else "expert_review"


def _build_question_schema(question: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    context = context or {}
    raw_schema = context.get("question_schema")
    if isinstance(raw_schema, dict) and raw_schema:
        schema = dict(raw_schema)
    elif isinstance(context.get("options"), list) and context.get("options"):
        schema = {
            "type": "single_select",
            "options": list(context.get("options") or []),
        }
    elif str(context.get("interaction_type") or "").strip() == PLANNER_EXPERT_SELECTION_INTERACTION:
        schema = {
            "type": "expert_multi_select",
            "selection_mode": context.get("selection_mode") or "multi_select",
            "available_experts": list(context.get("available_experts") or []),
            "recommended_experts": list(context.get("recommended_experts") or []),
            "selected_experts": list(context.get("selected_experts") or []),
        }
    else:
        schema = {"type": "long_text"}

    schema.setdefault("title", question)
    schema.setdefault("required", True)
    schema.setdefault("allow_free_text", bool(context.get("allow_free_text", True)))
    return schema


def _build_interaction_summary_from_payload(human_input: Dict[str, Any], pending_interrupt: Dict[str, Any]) -> str:
    action = str((human_input or {}).get("action") or "").strip()
    response = (human_input or {}).get("response") or {}
    response_type = str(response.get("type") or "").strip().lower()
    answer = str((human_input or {}).get("answer") or response.get("text") or "").strip()
    feedback = str((human_input or {}).get("feedback") or response.get("feedback") or "").strip()
    selected_option = str((human_input or {}).get("selected_option") or response.get("value") or "").strip()
    selected_experts = _normalize_string_list(
        (human_input or {}).get("selected_experts") if isinstance((human_input or {}).get("selected_experts"), list)
        else response.get("selected_experts") or (response.get("values") if response_type == "expert_multi_select" else None)
    )
    selected_options = _normalize_string_list(
        (human_input or {}).get("selected_options") if isinstance((human_input or {}).get("selected_options"), list)
        else response.get("selected_options") or (response.get("values") if response_type == "multi_select" else None)
    )
    if action == "approve":
        return "Approved to continue."
    if action == "revise":
        return feedback or "Requested revision before retry."
    if selected_experts:
        summary = f"Selected experts: {', '.join(selected_experts)}"
        return f"{summary}. {answer}".strip(". ") if answer else summary
    if selected_options:
        summary = f"Selected options: {', '.join(selected_options)}"
        return f"{summary}. {answer}".strip(". ") if answer else summary
    if selected_option:
        summary = f"Selected option: {selected_option}"
        return f"{summary}. {answer}".strip(". ") if answer else summary
    return answer or feedback or "Submitted human response."


def _ensure_human_interaction_record(
    project_id: str,
    version: str,
    run_id: str | None,
    pending_interrupt: Dict[str, Any] | None,
) -> Dict[str, Any]:
    pending_interrupt = dict(pending_interrupt or {})
    if not pending_interrupt:
        return pending_interrupt

    interaction_id = str(pending_interrupt.get("interaction_id") or "").strip()
    question = str(pending_interrupt.get("question") or "").strip() or "Human input required to continue."
    context = pending_interrupt.get("context") if isinstance(pending_interrupt.get("context"), dict) else {}
    question_schema = _build_question_schema(question, context)
    pending_interrupt["question_schema"] = question_schema
    pending_interrupt.setdefault("owner_node", pending_interrupt.get("node_type") or pending_interrupt.get("resume_target"))
    pending_interrupt.setdefault("scope", _infer_interaction_scope(pending_interrupt))

    existing = metadata_db.get_human_interaction(interaction_id) if interaction_id else None
    owner_node = str(pending_interrupt.get("owner_node") or pending_interrupt.get("node_type") or "").strip() or "planner"
    owner_expert_id = owner_node if owner_node not in {"planner", "supervisor", "bootstrap"} else None
    knowledge_refs = _normalize_string_list(context.get("knowledge_refs"))
    affected_artifacts = _normalize_string_list(context.get("related_artifacts"))
    if existing:
        metadata_db.update_human_interaction(
            interaction_id,
            run_id=run_id,
            status="waiting_user",
            question_text=question,
            question_schema=question_schema,
            context=context,
            knowledge_refs=knowledge_refs,
            affected_artifacts=affected_artifacts,
        )
    else:
        interaction_id = interaction_id or str(uuid.uuid4())
        pending_interrupt["interaction_id"] = interaction_id
        metadata_db.create_human_interaction(
            interaction_id=interaction_id,
            project_id=project_id,
            version_id=version,
            run_id=run_id,
            scope=str(pending_interrupt.get("scope") or "expert_clarification"),
            owner_node=owner_node,
            owner_expert_id=owner_expert_id,
            status="waiting_user",
            question_text=question,
            question_schema=question_schema,
            context=context,
            knowledge_refs=knowledge_refs,
            affected_artifacts=affected_artifacts,
        )
        metadata_db.append_human_interaction_event(
            event_id=str(uuid.uuid4()),
            interaction_id=interaction_id,
            event_type="waiting_user",
            payload={
                "interrupt_id": pending_interrupt.get("interrupt_id"),
                "node_id": pending_interrupt.get("node_id"),
                "node_type": pending_interrupt.get("node_type"),
                "resume_target": pending_interrupt.get("resume_target"),
            },
        )
    return pending_interrupt


def _hydrate_human_interaction(record: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not record:
        return None
    return {
        **record,
        "events": metadata_db.list_human_interaction_events(record["interaction_id"]),
    }


def _pending_interrupt_from_interaction_record(record: Dict[str, Any] | None) -> Dict[str, Any]:
    if not record:
        return {}
    context = record.get("context") if isinstance(record.get("context"), dict) else {}
    question = str(record.get("question_text") or "").strip() or "Human input required to continue."
    owner_node = str(record.get("owner_node") or "").strip() or "planner"
    scope = str(record.get("scope") or "").strip()
    interaction_type = str(context.get("interaction_type") or "").strip()
    is_expert_selection = scope == "planner_review" and (
        interaction_type == PLANNER_EXPERT_SELECTION_INTERACTION
        or isinstance(context.get("available_experts"), list)
        or isinstance((record.get("question_schema") or {}).get("available_experts"), list)
    )
    interrupt_kind = PLANNER_EXPERT_SELECTION_INTERACTION if is_expert_selection else ("ask_human" if scope.endswith("clarification") else "review")
    node_id = "planner" if is_expert_selection and owner_node == "planner" else owner_node
    pending_interrupt = {
        "interaction_id": record["interaction_id"],
        "interrupt_id": f"interaction:{record['interaction_id']}",
        "node_id": node_id,
        "node_type": owner_node,
        "owner_node": owner_node,
        "question": question,
        "context": context,
        "resume_target": owner_node,
        "interrupt_kind": interrupt_kind,
        "scope": scope or _infer_interaction_scope({"node_type": owner_node, "interrupt_kind": interrupt_kind, "context": context}),
        "question_schema": record.get("question_schema") or _build_question_schema(question, context),
    }
    return pending_interrupt


def _build_clarification_log(project_id: str, version: str) -> List[Dict[str, Any]]:
    interactions = metadata_db.list_human_interactions(project_id, version)
    log_entries: List[Dict[str, Any]] = []
    for interaction in reversed(interactions):
        if not interaction.get("answer"):
            continue
        merge_targets = _normalize_string_list(
            ((interaction.get("context") or {}).get("answer_merge_targets"))
            or ((interaction.get("answer") or {}).get("answer_merge_targets"))
        )
        log_entries.append(
            {
                "interaction_id": interaction["interaction_id"],
                "scope": interaction.get("scope"),
                "owner_node": interaction.get("owner_node"),
                "question": interaction.get("question_text"),
                "answer": interaction.get("answer") or {},
                "summary": interaction.get("summary") or "",
                "status": interaction.get("status"),
                "merge_targets": merge_targets,
                "created_at": interaction.get("created_at"),
                "updated_at": interaction.get("updated_at"),
                "completed_at": interaction.get("completed_at"),
            }
        )
    return log_entries


def _build_clarified_requirements_payload(project_id: str, version: str, state: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = state or get_workflow_state(project_id, version) or {}
    version_record = metadata_db.get_version(project_id, version) or {}
    clarification_log = _build_clarification_log(project_id, version)
    summary_lines = [
        entry["summary"]
        for entry in clarification_log
        if entry.get("summary") and ("clarified_requirements" in (entry.get("merge_targets") or []))
    ]
    decision_log = [
        {
            "interaction_id": entry.get("interaction_id"),
            "owner_node": entry.get("owner_node"),
            "summary": entry.get("summary") or "",
            "question": entry.get("question") or "",
            "created_at": entry.get("created_at"),
        }
        for entry in clarification_log
        if "decision_log" in (entry.get("merge_targets") or [])
    ]
    summary = "\n".join(f"- {line}" for line in summary_lines[-10:])
    return {
        "project_id": project_id,
        "version": version,
        "original_requirement": version_record.get("requirement") or "",
        "summary": summary,
        "human_answers": state.get("human_answers") or {},
        "clarification_log": clarification_log,
        "decision_log": decision_log,
        "pending_interrupt": state.get("pending_interrupt") or None,
        "updated_at": _now_iso(),
    }


def _render_clarified_requirements_markdown(payload: Dict[str, Any]) -> str:
    lines = [
        "# Clarified Requirements",
        "",
        f"- Project: `{payload.get('project_id', '')}`",
        f"- Version: `{payload.get('version', '')}`",
        f"- Updated At: `{payload.get('updated_at', '')}`",
        "",
        "## Summary",
        "",
    ]
    summary = str(payload.get("summary") or "").strip()
    lines.append(summary or "No clarified decisions have been recorded yet.")
    lines.extend(["", "## Original Requirement", ""])
    original_requirement = str(payload.get("original_requirement") or "").strip()
    lines.append(original_requirement or "No original requirement text recorded.")
    lines.extend(["", "## Clarification Log", ""])
    log_entries = payload.get("clarification_log") or []
    if not log_entries:
        lines.append("No clarification rounds have been completed yet.")
    else:
        for index, entry in enumerate(log_entries, start=1):
            lines.extend(
                [
                    f"### Round {index}",
                    "",
                    f"- Scope: `{entry.get('scope') or 'unknown'}`",
                    f"- Owner: `{entry.get('owner_node') or 'unknown'}`",
                    f"- Status: `{entry.get('status') or 'unknown'}`",
                    "",
                    f"**Question**: {entry.get('question') or ''}",
                    "",
                    f"**Summary**: {entry.get('summary') or 'No summary.'}",
                    "",
                    "```json",
                    json.dumps(entry.get("answer") or {}, ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            )
    lines.extend(["", "## Decision Log", ""])
    decision_entries = payload.get("decision_log") or []
    if not decision_entries:
        lines.append("No decision log entries have been recorded yet.")
    else:
        for index, entry in enumerate(decision_entries, start=1):
            lines.extend(
                [
                    f"- Decision {index}: {entry.get('summary') or 'No summary.'}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def _merge_clarified_requirements_payload(
    payload: Dict[str, Any],
    existing_requirements_payload: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        **existing_requirements_payload,
        "original_requirement": payload.get("original_requirement") or existing_requirements_payload.get("original_requirement") or "",
        "human_answers": payload.get("human_answers") or existing_requirements_payload.get("human_answers") or {},
        "clarification_log": payload.get("clarification_log") or [],
        "decision_log": payload.get("decision_log") or [],
        "clarified_requirements_summary": payload.get("summary") or "",
        "clarified_requirements_markdown_path": "baseline/clarified-requirements.md",
        "updated_at": payload.get("updated_at"),
    }


def _get_clarified_requirements_snapshot(project_id: str, version: str, state: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = _build_clarified_requirements_payload(project_id, version, state=state)
    project_path = PROJECTS_DIR / project_id / version
    baseline_dir = project_path / "baseline"
    requirements_json_path = baseline_dir / "requirements.json"
    existing_requirements_payload: Dict[str, Any] = {}
    if requirements_json_path.exists():
        try:
            loaded = json.loads(requirements_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_requirements_payload = loaded
        except Exception:
            existing_requirements_payload = {}
    clarified_markdown = _render_clarified_requirements_markdown(payload)
    merged_requirements_payload = _merge_clarified_requirements_payload(payload, existing_requirements_payload)
    return {
        "summary": payload.get("summary") or "",
        "clarified_requirements_markdown": clarified_markdown,
        "requirements": merged_requirements_payload,
        "clarification_log": payload.get("clarification_log") or [],
    }


def _persist_clarification_artifacts(project_id: str, version: str, state: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = _build_clarified_requirements_payload(project_id, version, state=state)
    project_path = PROJECTS_DIR / project_id / version
    baseline_dir = project_path / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    clarification_log_path = baseline_dir / "clarification-log.json"
    clarified_requirements_path = baseline_dir / "clarified-requirements.md"
    requirements_json_path = baseline_dir / "requirements.json"

    clarification_log_path.write_text(
        json.dumps(payload.get("clarification_log") or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    clarified_markdown = _render_clarified_requirements_markdown(payload)
    clarified_requirements_path.write_text(clarified_markdown, encoding="utf-8")
    existing_requirements_payload: Dict[str, Any] = {}
    if requirements_json_path.exists():
        try:
            loaded = json.loads(requirements_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_requirements_payload = loaded
        except Exception:
            existing_requirements_payload = {}
    merged_requirements_payload = _merge_clarified_requirements_payload(payload, existing_requirements_payload)
    requirements_json_path.write_text(
        json.dumps(merged_requirements_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "summary": payload.get("summary") or "",
        "clarified_requirements_markdown": clarified_markdown,
        "requirements": merged_requirements_payload,
        "clarification_log": payload.get("clarification_log") or [],
    }


def _thread_id(project_id: str, version: str) -> str:
    return f"{project_id}_{version}"


def _graph_config(project_id: str, version: str, run_id: str | None = None) -> dict:
    config = {
        "configurable": {
            "thread_id": _thread_id(project_id, version), 
            "version": version
        },
        "recursion_limit": 100,
    }
    if run_id:
        config["configurable"]["run_id"] = run_id
    return config


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _ensure_job(job_id: str) -> dict:
    existing = jobs.get(job_id)
    if existing:
        existing.setdefault("logs", [])
        existing.setdefault("events", [])
        existing.setdefault("subscribers", set())
        existing.setdefault("status", RUN_STATUS_QUEUED)
        return existing

    jobs[job_id] = {"status": RUN_STATUS_QUEUED, "logs": [], "events": [], "subscribers": set()}
    return jobs[job_id]


def _set_runtime_state(
    project_id: str,
    version: str,
    *,
    run_status: str,
    current_node: str | None = None,
    waiting_reason: str | None = None,
    pending_interrupt: dict | None = None,
    can_resume: bool | None = None,
    job_id: str | None = None,
):
    thread_id = _thread_id(project_id, version)
    previous = runtime_registry.get(thread_id, {})

    # Also update persistent metadata DB
    metadata_db.upsert_version(project_id, version, previous.get("requirement", ""), run_status)

    runtime_registry[thread_id] = {
        **previous,
        "project_id": project_id,
        "version": version,
        "job_id": job_id or previous.get("job_id"),
        "run_status": run_status,
        "current_node": current_node,
        "waiting_reason": waiting_reason,
        "pending_interrupt": pending_interrupt if run_status == RUN_STATUS_WAITING_HUMAN else None,
        "can_resume": (
            can_resume
            if can_resume is not None
            else run_status in {RUN_STATUS_WAITING_HUMAN, RUN_STATUS_FAILED}
        ),
        "updated_at": _now_iso(),
    }

    existing_projection = metadata_db.get_workflow_run(project_id, version) or {}
    current_phase = existing_projection.get("current_phase")
    started_at = existing_projection.get("started_at") or runtime_registry[thread_id]["updated_at"]
    finished_at = runtime_registry[thread_id]["updated_at"] if run_status in {RUN_STATUS_SUCCESS, RUN_STATUS_FAILED} else None
    metadata_db.upsert_workflow_run(
        project_id,
        version,
        run_id=job_id or previous.get("job_id"),
        status=run_status,
        current_phase=current_phase,
        current_node=current_node,
        waiting_reason=waiting_reason,
        pending_interrupt=pending_interrupt if run_status == RUN_STATUS_WAITING_HUMAN else None,
        started_at=started_at,
        finished_at=finished_at,
    )


def _mark_workflow_failed(
    project_id: str,
    version: str,
    run_id: str,
    *,
    reason: str,
    job_id: str | None = None,
    current_node: str | None = None,
    task_queue: list[dict] | None = None,
):
    job_key = job_id or run_id
    queue = task_queue if task_queue is not None else metadata_db.list_workflow_tasks(project_id, version)
    failed_queue = []
    for task in queue or []:
        next_status = "failed"
        failed_task = {**task, "status": next_status}
        failed_queue.append(failed_task)
        node_type = task.get("agent_type") or task.get("node_type")
        if node_type and node_type not in {"bootstrap", "supervisor"}:
            _publish_event(
                job_key,
                {
                    "event_id": _new_event_id(),
                    "event_type": "node_completed",
                    "run_id": run_id,
                    "node_id": task.get("id") or task.get("task_id") or node_type,
                    "node_type": node_type,
                    "status": next_status,
                    "timestamp": _now_iso(),
                },
            )

    if failed_queue:
        metadata_db.replace_workflow_tasks(project_id, version, run_id=run_id, tasks=failed_queue)

    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_FAILED,
        current_node=current_node,
        waiting_reason=reason,
        can_resume=True,
        job_id=job_key,
    )
    _finalize_waiting_interactions_for_version(
        project_id,
        version,
        new_status="superseded",
        event_type="workflow_failed",
        payload={"run_id": run_id, "reason": reason},
    )
    _ensure_job(job_key)["status"] = RUN_STATUS_FAILED
    _append_job_log(job_key, f"[ERROR] {reason}", project_id=project_id, version=version)
    _emit_run_failed(job_key, run_id, reason)


def _run_llm_connectivity_preflight(
    project_id: str,
    version: str,
    state: dict | None,
    model: str | None = None,
) -> dict:
    probe_state = state or {}
    if model:
        probe_state = _build_graph_input_state(
            "llm-preflight",
            project_id,
            version,
            probe_state.get("requirement", ""),
            probe_state,
            model=model,
        )
    runtime_settings = resolve_runtime_llm_settings((probe_state.get("design_context") or {}))
    if runtime_settings:
        llm_settings = {
            "api_key": runtime_settings.get("openai_api_key"),
            "base_url": runtime_settings.get("openai_base_url"),
            "model_name": runtime_settings.get("openai_model_name"),
            "headers": runtime_settings.get("openai_headers") or {},
        }
    else:
        llm_settings = {}
    result = test_llm_connectivity(llm_settings)
    if result.get("success"):
        return result
    message = str(result.get("message") or "LLM connectivity check failed.").strip()
    raise RuntimeError(f"LLM 连接性验证失败：{message}")


def _sync_workflow_projection_from_payload(
    project_id: str,
    version: str,
    payload: dict,
    *,
    run_id: str | None,
    authoritative_tasks: bool = False,
):
    task_queue = payload.get("task_queue") or []
    workflow_phase = payload.get("workflow_phase")
    current_node = payload.get("current_node")
    run_status = payload.get("run_status")
    waiting_reason = payload.get("waiting_reason")
    pending_interrupt = (
        payload.get("pending_interrupt")
        if "pending_interrupt" in payload
        else (None if run_status and run_status != RUN_STATUS_WAITING_HUMAN else JSON_UNSET)
    )

    if workflow_phase or current_node or run_status or waiting_reason or pending_interrupt is not JSON_UNSET:
        existing = metadata_db.get_workflow_run(project_id, version) or {}
        metadata_db.upsert_workflow_run(
            project_id,
            version,
            run_id=run_id,
            status=run_status or existing.get("status") or RUN_STATUS_QUEUED,
            current_phase=workflow_phase or existing.get("current_phase"),
            current_node=current_node if current_node is not None else existing.get("current_node"),
            waiting_reason=waiting_reason if waiting_reason is not None else existing.get("waiting_reason"),
            pending_interrupt=pending_interrupt,
            started_at=existing.get("started_at"),
            finished_at=existing.get("finished_at"),
        )

    if not task_queue:
        return

    if authoritative_tasks:
        metadata_db.replace_workflow_tasks(project_id, version, run_id=run_id, tasks=task_queue)
        return

    for task in task_queue:
        metadata = dict(task.get("metadata") or {})
        phase = task.get("phase") or metadata.get("workflow_phase")
        metadata_db.upsert_workflow_task(
            project_id,
            version,
            node_type=task.get("agent_type"),
            task_id=task.get("id"),
            run_id=run_id,
            status=task.get("status", "todo"),
            phase=phase,
            priority=task.get("priority"),
            dependencies=task.get("dependencies") or [],
            metadata=metadata,
            authoritative=False,
        )


def _has_active_runtime_task(thread_id: str) -> bool:
    task = runtime_tasks.get(thread_id)
    return bool(task and not task.done())


def _launch_runtime_task(thread_id: str, coro):
    # Ensure any previous task for this thread is stopped first
    existing_task = runtime_tasks.get(thread_id)
    if existing_task and not existing_task.done():
        print(f"[DEBUG] Cancelling existing task for thread {thread_id}")
        existing_task.cancel()

    task = asyncio.create_task(coro)
    runtime_tasks[thread_id] = task

    def _cleanup(completed_task):
        if runtime_tasks.get(thread_id) is completed_task:
            runtime_tasks.pop(thread_id, None)

    task.add_done_callback(_cleanup)
    return task


def _latest_project_timestamp(project_id: str, version: str) -> str:
    project_root = PROJECTS_DIR / project_id / version
    if not project_root.exists():
        return _now_iso()

    latest_mtime = None
    for path in project_root.rglob("*"):
        try:
            if path.is_file():
                path_mtime = path.stat().st_mtime
                latest_mtime = path_mtime if latest_mtime is None else max(latest_mtime, path_mtime)
        except OSError:
            continue

    if latest_mtime is None:
        return _now_iso()
    return datetime.datetime.fromtimestamp(latest_mtime, tz=datetime.timezone.utc).isoformat()


def _load_artifacts_from_disk(project_id: str, version: str) -> dict:
    artifacts = {}
    project_root = PROJECTS_DIR / project_id / version
    for dirname in ("baseline", "artifacts", "logs", "evidence", "release"):
        dir_path = project_root / dirname
        if not dir_path.exists():
            continue
        for item in dir_path.iterdir():
            if not item.is_file():
                continue
            try:
                artifacts[item.name] = item.read_text(encoding="utf-8")
            except Exception:
                artifacts[item.name] = "[Binary]"
    return artifacts


def _check_success(project_root: Path, file_patterns: list[str]) -> bool:
    for dirname in ("artifacts", "release"):
        target_dir = project_root / dirname
        if not target_dir.exists():
            continue
        for pattern in file_patterns:
            if any(target_dir.glob(pattern)):
                return True
    return False


def _get_registry_expert_outputs() -> dict[str, list[str]]:
    """Get expert outputs mapping from registry. Returns {capability: [expected_outputs]}."""
    try:
        from registry.expert_registry import ExpertRegistry
        registry = ExpertRegistry.get_instance()
        return {
            manifest.capability: manifest.expected_outputs
            for manifest in registry.get_all_manifests()
        }
    except RuntimeError:
        return {}


def _build_legacy_task_queue(project_id: str, version: str) -> list[dict]:
    project_root = PROJECTS_DIR / project_id / version
    logs_dir = project_root / "logs"
    baseline_file = project_root / "baseline" / "requirements.json"

    active_agents = set()
    if baseline_file.exists():
        try:
            base_data = json.loads(baseline_file.read_text(encoding="utf-8"))
            active_agents = {str(agent) for agent in base_data.get("active_agents", [])}
        except Exception:
            active_agents = set()

    # Dynamic default from registry
    if not active_agents:
        try:
            from registry.expert_registry import ExpertRegistry
            registry = ExpertRegistry.get_instance()
            active_agents = {"planner"} | set(registry.get_capabilities())
        except RuntimeError:
            active_agents = {"planner", "requirement-clarification", "ir-assembler", "validator"}

    validator_status = "todo"
    val_log_path = logs_dir / "validator.log"
    validator_report_path = project_root / "artifacts" / "validation-report.md"
    validator_evidence_path = project_root / "evidence" / "validator.json"
    validator_reasoning_path = logs_dir / "validator-reasoning.md"
    if val_log_path.exists():
        content = val_log_path.read_text(encoding="utf-8")
        validator_status = "success" if "[SUCCESS]" in content else "failed"
    elif validator_report_path.exists() and validator_evidence_path.exists():
        validator_status = "success"
    elif validator_evidence_path.exists() or validator_reasoning_path.exists():
        try:
            evidence = json.loads(validator_evidence_path.read_text(encoding="utf-8")) if validator_evidence_path.exists() else {}
            validator_status = "failed" if evidence.get("failure_reason") else "success"
        except Exception:
            validator_status = "failed"

    # Build task map dynamically from registry
    expert_outputs = _get_registry_expert_outputs()
    full_map = [{"id": "0", "agent_type": "planner", "status": "success"}]

    task_id = 1
    for capability in active_agents:
        if capability == "planner":
            continue
        if capability == "validator":
            full_map.append({"id": str(task_id), "agent_type": capability, "status": validator_status})
        else:
            outputs = expert_outputs.get(capability, [])
            status = "success" if _check_success(project_root, outputs) else "todo"
            full_map.append({"id": str(task_id), "agent_type": capability, "status": status})
        task_id += 1

    return [task for task in full_map if task["agent_type"] in active_agents or task["agent_type"] == "planner"]


def _derive_run_status(task_queue: list[dict], human_intervention_required: bool) -> str:
    statuses = {task.get("status", "todo") for task in task_queue}
    if human_intervention_required or "waiting_human" in statuses:
        return RUN_STATUS_WAITING_HUMAN
    if "running" in statuses:
        return RUN_STATUS_RUNNING
    if "failed" in statuses:
        return RUN_STATUS_FAILED
    if task_queue and statuses.issubset({"success", "skipped"}):
        return RUN_STATUS_SUCCESS
    if task_queue and "todo" in statuses:
        return RUN_STATUS_QUEUED
    return RUN_STATUS_QUEUED


def _derive_current_node(task_queue: list[dict], raw_state: dict | None) -> str | None:
    if raw_state and raw_state.get("human_intervention_required") and raw_state.get("last_worker"):
        return raw_state.get("last_worker")
    if raw_state and raw_state.get("current_node"):
        return raw_state.get("current_node")
    running_task = next((task for task in task_queue if task.get("status") == "running"), None)
    if running_task:
        return running_task.get("agent_type")
    return raw_state.get("last_worker") if raw_state else None


def _normalize_string_list(raw_items) -> list[str]:
    if not isinstance(raw_items, list):
        return []

    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _parse_planner_recommended_experts(reasoning_content: str | None) -> list[str]:
    if not isinstance(reasoning_content, str) or not reasoning_content.strip():
        return []

    marker = "**Planner Recommended Experts:**"
    for line in reasoning_content.splitlines():
        if marker not in line:
            continue
        raw_experts = line.split(marker, 1)[1].strip()
        if not raw_experts or raw_experts == "(none)":
            return []
        return _normalize_string_list([item.strip() for item in raw_experts.split(",")])
    return []


def _infer_legacy_pending_interrupt(
    project_id: str,
    version: str,
    state: dict,
    *,
    task_queue: list[dict],
    run_status: str | None,
    current_node: str | None,
    waiting_reason: str | None,
) -> dict | None:
    if run_status != RUN_STATUS_WAITING_HUMAN:
        return None

    planner_waiting = any(
        str(task.get("agent_type") or "").strip() == "planner"
        and str(task.get("status") or "").strip() == "waiting_human"
        for task in task_queue
    )
    if current_node != "planner" and not planner_waiting:
        return None

    reason_text = str(waiting_reason or "").casefold()
    if "expert selection" not in reason_text and "planner" not in reason_text:
        return None

    artifacts = state.get("artifacts") or {}
    requirements_payload = {}
    raw_requirements = artifacts.get("requirements.json")
    if isinstance(raw_requirements, str):
        try:
            requirements_payload = json.loads(raw_requirements) or {}
        except Exception:
            requirements_payload = {}

    recommended_experts = _normalize_string_list(requirements_payload.get("active_agents"))
    if not recommended_experts:
        recommended_experts = _parse_planner_recommended_experts(artifacts.get("planner-reasoning.md"))

    enabled_expert_ids = _normalize_string_list(metadata_db.list_enabled_expert_ids(project_id))
    if not enabled_expert_ids:
        enabled_expert_ids = list(recommended_experts)

    available_expert_map = {
        expert["id"]: expert
        for expert in list_experts()
        if expert.get("id") not in SYSTEM_EXPERTS
    }
    recommended_set = set(recommended_experts)
    available_experts = []
    available_ids = set()

    for expert_id in enabled_expert_ids:
        expert = available_expert_map.get(expert_id) or {}
        available_experts.append(
            {
                "id": expert_id,
                "name": expert.get("name") or expert_id,
                "name_zh": expert.get("name_zh") or None,
                "name_en": expert.get("name_en") or expert.get("name") or expert_id,
                "description": expert.get("description") or "",
                "phase": expert.get("phase") or "",
                "recommended": expert_id in recommended_set,
                "auto_selected": expert_id in recommended_set,
            }
        )
        available_ids.add(expert_id)

    for expert_id in recommended_experts:
        if expert_id in available_ids:
            continue
        expert = available_expert_map.get(expert_id) or {}
        available_experts.append(
            {
                "id": expert_id,
                "name": expert.get("name") or expert_id,
                "name_zh": expert.get("name_zh") or None,
                "name_en": expert.get("name_en") or expert.get("name") or expert_id,
                "description": expert.get("description") or "",
                "phase": expert.get("phase") or "",
                "recommended": True,
                "auto_selected": True,
            }
        )

    if not available_experts and not recommended_experts:
        return None

    return {
        "node_id": "planner",
        "node_type": "planner",
        "interrupt_id": f"legacy-planner-expert-selection:{project_id}:{version}",
        "question": PLANNER_EXPERT_SELECTION_QUESTION,
        "context": {
            "interaction_type": PLANNER_EXPERT_SELECTION_INTERACTION,
            "selection_mode": "multi_select",
            "why_needed": (
                "Planner has finished the initial expert recommendation. "
                "Please confirm the final experts before execution starts."
            ),
            "recommended_experts": recommended_experts,
            "selected_experts": recommended_experts,
            "available_experts": available_experts,
            "allow_free_text": True,
        },
        "resume_target": "planner",
        "interrupt_kind": PLANNER_EXPERT_SELECTION_INTERACTION,
    }


def _parse_iso_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        normalized_value = value.replace("Z", "+00:00")
        parsed = datetime.datetime.fromisoformat(normalized_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _format_scheduled_waiting_reason(scheduled_for: str | None) -> str | None:
    scheduled_at = _parse_iso_timestamp(scheduled_for)
    if scheduled_at is None:
        return None
    return f"Scheduled to start at {scheduled_at.isoformat()}"


def _cancel_scheduled_task(schedule_id: str) -> None:
    task = scheduled_runtime_tasks.pop(schedule_id, None)
    if task and not task.done():
        task.cancel()


def _cancel_scheduled_tasks_for_version(project_id: str, version: str) -> None:
    for schedule in metadata_db.list_scheduled_runs_for_version(project_id, version, statuses=["scheduled"]):
        _cancel_scheduled_task(schedule["schedule_id"])


def _normalize_llm_log_node_id(node_id: str | None) -> str | None:
    if not node_id:
        return None
    if node_id.endswith("-final"):
        return node_id[:-6]
    if "-react-step-" in node_id:
        return node_id.split("-react-step-", 1)[0]
    return node_id


def _build_node_llm_map(project_id: str, version: str, state: dict, task_queue: list[dict]) -> dict[str, dict]:
    design_context = state.get("design_context") or {}
    model_config = design_context.get("model_config") or {}
    fallback_model = (
        str(model_config.get("model_name") or "").strip()
        or str(design_context.get("model") or "").strip()
        or None
    )
    fallback_provider = str(model_config.get("provider") or "").strip().lower() or None

    latest_by_node: dict[str, dict] = {}
    log_file = BASE_DIR / "projects" / project_id / version / "logs" / "llm_interactions.jsonl"
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    normalized_node_id = _normalize_llm_log_node_id(entry.get("node_id"))
                    if not normalized_node_id:
                        continue
                    timestamp = _parse_iso_timestamp(entry.get("timestamp"))
                    current = latest_by_node.get(normalized_node_id)
                    current_timestamp = _parse_iso_timestamp((current or {}).get("timestamp"))
                    if current is None or (timestamp and (current_timestamp is None or timestamp >= current_timestamp)):
                        latest_by_node[normalized_node_id] = entry
        except OSError:
            pass

    node_types = {
        task.get("agent_type")
        for task in task_queue
        if task.get("agent_type")
    }
    if "planner" not in node_types and (latest_by_node.get("planner") or state.get("current_node") == "planner"):
        node_types.add("planner")

    node_llm_map: dict[str, dict] = {}
    for node_type in node_types:
        latest = latest_by_node.get(node_type) or {}
        model_name = str(latest.get("model") or fallback_model or "").strip()
        provider = str(latest.get("provider") or fallback_provider or "").strip().lower()
        if not model_name and not provider:
            continue
        node_llm_map[node_type] = {
            "provider": provider or None,
            "model": model_name or None,
            "label": " · ".join(part for part in [provider or "", model_name or ""] if part),
        }

    return node_llm_map


def _normalize_state(project_id: str, version: str, raw_state: dict | None, runtime: dict | None = None) -> dict | None:
    runtime = runtime if runtime is not None else runtime_registry.get(_thread_id(project_id, version), {})
    workflow_run = metadata_db.get_workflow_run(project_id, version) or {}
    version_record = metadata_db.get_version(project_id, version) or {}
    scheduled_run = metadata_db.get_latest_scheduled_run_for_version(
        project_id,
        version,
        statuses=["scheduled"],
    )
    if not raw_state and not runtime and not workflow_run and not version_record and not scheduled_run:
        return None

    state = dict(raw_state or {})
    workflow_tasks = metadata_db.list_workflow_tasks(project_id, version)
    initial_status = (
        runtime.get("run_status")
        or workflow_run.get("status")
        or version_record.get("run_status")
        or (RUN_STATUS_SCHEDULED if scheduled_run else None)
    )
    task_queue = workflow_tasks or state.get("task_queue") or []
    if not task_queue and initial_status != RUN_STATUS_SCHEDULED:
        task_queue = _build_legacy_task_queue(project_id, version)
    history = state.get("history") or []
    messages = state.get("messages") or []
    artifacts = merge_artifacts(_load_artifacts_from_disk(project_id, version), state.get("artifacts") or {})
    state["artifacts"] = artifacts
    human_intervention_required = bool(state.get("human_intervention_required", False))

    derived_run_status = (
        RUN_STATUS_SCHEDULED
        if initial_status == RUN_STATUS_SCHEDULED and not task_queue
        else _derive_run_status(task_queue, human_intervention_required)
    )
    run_status = runtime.get("run_status") or workflow_run.get("status") or version_record.get("run_status") or derived_run_status
    current_node = runtime.get("current_node") or workflow_run.get("current_node")

    # When runtime is absent, prefer the task projection over a stale workflow_run row
    # so a missing runtime task does not continue to masquerade as "running".
    if not runtime and derived_run_status != RUN_STATUS_RUNNING:
        if run_status in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING} or workflow_run.get("status") in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
            run_status = derived_run_status

    if current_node is None:
        current_node = _derive_current_node(task_queue, state)

    waiting_reason = runtime.get("waiting_reason") or workflow_run.get("waiting_reason")
    if waiting_reason is None:
        waiting_reason = state.get("waiting_reason")

    pending_interrupt = (
        state.get("pending_interrupt")
        or runtime.get("pending_interrupt")
        or workflow_run.get("pending_interrupt")
    )
    if not pending_interrupt:
        pending_interrupt = _infer_legacy_pending_interrupt(
            project_id,
            version,
            state,
            task_queue=task_queue,
            run_status=run_status,
            current_node=current_node,
            waiting_reason=waiting_reason,
        )
    if waiting_reason is None and pending_interrupt:
        waiting_reason = pending_interrupt.get("question")
    if waiting_reason is None and run_status == RUN_STATUS_WAITING_HUMAN:
        waiting_reason = "human_intervention_required"
    if waiting_reason is None and run_status == RUN_STATUS_SCHEDULED:
        waiting_reason = _format_scheduled_waiting_reason(scheduled_run.get("scheduled_for") if scheduled_run else None)

    if run_status == RUN_STATUS_SUCCESS:
        current_node = None
        waiting_reason = None
    elif run_status == RUN_STATUS_SCHEDULED:
        current_node = None

    normalized_updated_at = (
        runtime.get("updated_at")
        or workflow_run.get("updated_at")
        or version_record.get("updated_at")
        or (scheduled_run.get("updated_at") if scheduled_run else None)
        or state.get("updated_at")
        or _latest_project_timestamp(project_id, version)
    )
    stale_running_detected = False
    runtime_thread_id = _thread_id(project_id, version)
    runtime_missing_or_inactive = not runtime or not _has_active_runtime_task(runtime_thread_id)
    if runtime_missing_or_inactive and run_status == RUN_STATUS_RUNNING:
        updated_at_dt = _parse_iso_timestamp(normalized_updated_at)
        if updated_at_dt is not None:
            age_seconds = (datetime.datetime.now(datetime.timezone.utc) - updated_at_dt).total_seconds()
            if age_seconds >= STALE_RUNNING_TIMEOUT_SECONDS:
                stale_running_detected = True

    normalized_task_queue = list(task_queue)
    if stale_running_detected:
        failed_queue = []
        for task in normalized_task_queue:
            if task.get("status") == "running":
                failed_queue.append({**task, "status": "failed"})
            else:
                failed_queue.append(task)
        normalized_task_queue = failed_queue
        run_status = RUN_STATUS_FAILED
        human_intervention_required = False
        current_node = current_node or _derive_current_node(normalized_task_queue, state)
        stale_node = current_node or "current node"
        waiting_reason = (
            f"Execution appears stalled at {stale_node}. "
            "No active orchestrator runtime task was found for this running state. "
            "You can retry the node or review the latest logs and tool output."
        )

    can_resume = runtime.get("can_resume")
    if can_resume is None:
        can_resume = run_status in {RUN_STATUS_WAITING_HUMAN, RUN_STATUS_FAILED}
    
    # FORCE: If status is queued or running, it cannot be 'resumed' via the resume/answer API
    if run_status in {RUN_STATUS_SCHEDULED, RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
        can_resume = False

    node_llm_map = _build_node_llm_map(project_id, version, state, normalized_task_queue)

    return {
        **state,
        "project_id": project_id,
        "version": version,
        "run_id": runtime.get("job_id") or workflow_run.get("run_id") or state.get("run_id"),
        "task_queue": normalized_task_queue,
        "history": history,
        "messages": messages,
        "artifacts": artifacts,
        "run_status": run_status,
        "current_node": current_node,
        "workflow_phase": workflow_run.get("current_phase") or state.get("workflow_phase"),
        "can_resume": can_resume,
        "waiting_reason": waiting_reason,
        "pending_interrupt": pending_interrupt,
        "human_answers": state.get("human_answers") or {},
        "updated_at": normalized_updated_at,
        "stale_execution_detected": stale_running_detected,
        "node_llm_map": node_llm_map,
        "schedule_id": scheduled_run.get("schedule_id") if scheduled_run else None,
        "scheduled_for": scheduled_run.get("scheduled_for") if scheduled_run else None,
    }


def _coerce_event_output(output) -> dict:
    return output if isinstance(output, dict) else {}


def _append_job_log(job_id: str, message: str, project_id: str | None = None, version: str | None = None):
    job = _ensure_job(job_id)
    log_entry = format_run_log_entry(message)
    job["logs"].append(log_entry)
    
    # Try to persist log incrementally if we have project context
    try:
        pid = project_id
        ver = version
        if not pid or not ver:
            if ":" in job_id:
                parts = job_id.split(":")
                if len(parts) >= 2:
                    pid, ver = parts[0], parts[1]
        if pid and ver:
            log_dir = BASE_DIR / "projects" / pid / ver / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "orchestrator_run.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
    except Exception as e:
        # Ignore errors during incremental logging to avoid crashing the worker
        print(f"[Orchestrator] Failed to append log to disk: {e}")


def _publish_event(job_id: str, payload: dict) -> dict:
    event = validate_event_payload(payload)
    serialized = dump_event(event)
    job = _ensure_job(job_id)
    job["events"].append(serialized)

    stale_subscribers = []
    for subscriber in job["subscribers"]:
        try:
            subscriber.put_nowait(serialized)
        except Exception:
            stale_subscribers.append(subscriber)

    for subscriber in stale_subscribers:
        job["subscribers"].discard(subscriber)

    return serialized


def _emit_node_started(
    job_id: str,
    run_id: str,
    node_id: str,
    node_type: str,
    *,
    project_id: str | None = None,
    version_id: str | None = None,
):
    payload = {
        "event_id": _new_event_id(),
        "event_type": "node_started",
        "run_id": run_id,
        "node_id": node_id,
        "node_type": node_type,
        "timestamp": _now_iso(),
    }
    if project_id and version_id and node_type not in {"bootstrap", "supervisor"}:
        existing_task = metadata_db.get_workflow_task(project_id, version_id, node_type) or {}
        metadata_db.upsert_workflow_task(
            project_id,
            version_id,
            node_type=node_type,
            task_id=node_id,
            run_id=run_id,
            status="running",
            phase=existing_task.get("phase"),
            priority=existing_task.get("priority"),
            dependencies=existing_task.get("dependencies") or [],
            metadata=existing_task.get("metadata") or {},
            authoritative=False,
        )
        metadata_db.append_workflow_task_event(
            event_id=payload["event_id"],
            project_id=project_id,
            version_id=version_id,
            run_id=run_id,
            task_id=node_id,
            node_type=node_type,
            event_type="node_started",
            status="running",
            payload=payload,
            created_at=payload["timestamp"],
        )
    _publish_event(job_id, payload)


def _emit_node_completed(
    job_id: str,
    run_id: str,
    node_id: str,
    node_type: str,
    status: str,
    *,
    project_id: str | None = None,
    version_id: str | None = None,
):
    if status not in {"success", "failed", "skipped"}:
        return
    payload = {
        "event_id": _new_event_id(),
        "event_type": "node_completed",
        "run_id": run_id,
        "node_id": node_id,
        "node_type": node_type,
        "status": status,
        "timestamp": _now_iso(),
    }
    if project_id and version_id and node_type not in {"bootstrap", "supervisor"}:
        existing_task = metadata_db.get_workflow_task(project_id, version_id, node_type) or {}
        metadata_db.upsert_workflow_task(
            project_id,
            version_id,
            node_type=node_type,
            task_id=node_id,
            run_id=run_id,
            status=status,
            phase=existing_task.get("phase"),
            priority=existing_task.get("priority"),
            dependencies=existing_task.get("dependencies") or [],
            metadata=existing_task.get("metadata") or {},
            authoritative=False,
        )
        metadata_db.append_workflow_task_event(
            event_id=payload["event_id"],
            project_id=project_id,
            version_id=version_id,
            run_id=run_id,
            task_id=node_id,
            node_type=node_type,
            event_type="node_completed",
            status=status,
            payload=payload,
            created_at=payload["timestamp"],
        )
    _publish_event(job_id, payload)


def _emit_text_delta(job_id: str, run_id: str, node_id: str, node_type: str, delta: str, stream_name: str = "history"):
    _publish_event(
        job_id,
        {
            "event_id": _new_event_id(),
            "event_type": "text_delta",
            "run_id": run_id,
            "node_id": node_id,
            "node_type": node_type,
            "stream_name": stream_name,
            "delta": delta,
            "timestamp": _now_iso(),
        },
    )


def _emit_artifact_updates(job_id: str, run_id: str, node_id: str, node_type: str, before: dict, after: dict):
    before = before or {}
    after = after or {}
    for artifact_name, content in after.items():
        if artifact_name not in before:
            artifact_status = "created"
        elif before[artifact_name] != content:
            artifact_status = "updated"
        else:
            continue

        _publish_event(
            job_id,
            {
                "event_id": _new_event_id(),
                "event_type": "artifact_updated",
                "run_id": run_id,
                "node_id": node_id,
                "node_type": node_type,
                "artifact_name": artifact_name,
                "artifact_status": artifact_status,
                "timestamp": _now_iso(),
            },
        )


def _register_artifact_updates(
    project_id: str,
    version: str,
    run_id: str,
    node_type: str,
    before: dict,
    after: dict,
) -> dict:
    try:
        result = finalize_expert_artifact_outputs(
            project_id=project_id,
            version_id=version,
            run_id=run_id,
            expert_id=node_type,
            before=before,
            after=after,
        )
    except Exception as exc:
        print(f"[WARN] Failed to finalize artifact governance for '{node_type}': {exc}")
        return {
            "project_id": project_id,
            "version_id": version,
            "run_id": run_id,
            "expert_id": node_type,
            "status": "blocked",
            "changed_artifact_names": [],
            "artifact_count": 0,
            "items": [],
            "errors": [{"file_name": "*", "error": str(exc)}],
            "dependency_graph": {"refreshed": False, "node_count": 0, "edge_count": 0},
        }
    for error in result.get("errors") or []:
        print(f"[WARN] Failed to register design artifact '{error.get('file_name')}': {error.get('error')}")
    return result


def _emit_artifact_governance_reviewable(
    job_id: str,
    run_id: str,
    node_id: str,
    node_type: str,
    governance: dict | None,
):
    if not governance:
        return
    if not governance.get("artifact_count") and not governance.get("errors"):
        return
    _publish_event(
        job_id,
        {
            "event_id": _new_event_id(),
            "event_type": "artifact_governance_reviewable",
            "run_id": run_id,
            "node_id": node_id,
            "node_type": node_type,
            "status": governance.get("status", "needs_review"),
            "artifacts": governance.get("items") or [],
            "errors": governance.get("errors") or [],
            "dependency_graph": governance.get("dependency_graph") or {},
            "timestamp": _now_iso(),
        },
    )


def _record_artifact_governance_summary(
    project_id: str,
    version_id: str,
    run_id: str,
    node_id: str,
    node_type: str,
    governance: dict | None,
):
    if not governance:
        return
    if node_type in {"bootstrap", "supervisor"}:
        return
    if not governance.get("artifact_count") and not governance.get("errors"):
        return
    timestamp = _now_iso()
    payload = {
        "event_id": _new_event_id(),
        "event_type": "artifact_governance_reviewable",
        "run_id": run_id,
        "node_id": node_id,
        "node_type": node_type,
        "status": governance.get("status", "needs_review"),
        "artifacts": governance.get("items") or [],
        "errors": governance.get("errors") or [],
        "dependency_graph": governance.get("dependency_graph") or {},
        "timestamp": timestamp,
    }
    existing_task = metadata_db.get_workflow_task(project_id, version_id, node_type) or {}
    existing_metadata = existing_task.get("metadata") or {}
    metadata_db.upsert_workflow_task(
        project_id,
        version_id,
        node_type=node_type,
        task_id=node_id,
        run_id=run_id,
        status=existing_task.get("status") or "running",
        phase=existing_task.get("phase"),
        priority=existing_task.get("priority"),
        dependencies=existing_task.get("dependencies") or [],
        metadata={**existing_metadata, "artifact_governance": payload},
        authoritative=False,
    )
    metadata_db.append_workflow_task_event(
        event_id=payload["event_id"],
        project_id=project_id,
        version_id=version_id,
        run_id=run_id,
        task_id=node_id,
        node_type=node_type,
        event_type="artifact_governance_reviewable",
        status=payload["status"],
        payload=payload,
        created_at=timestamp,
    )


def _emit_tool_events(job_id: str, run_id: str, node_id: str, node_type: str, tool_results: list[dict] | None):
    for tool_result in tool_results or []:
        _publish_event(
            job_id,
            {
                "event_id": _new_event_id(),
                "event_type": "tool_event",
                "run_id": run_id,
                "node_id": node_id,
                "node_type": node_type,
                "tool_name": tool_result.get("tool_name", "unknown"),
                "status": tool_result.get("status", "error"),
                "error_code": tool_result.get("error_code", "UNKNOWN"),
                "duration_ms": int(tool_result.get("duration_ms", 0) or 0),
                "tool_input": tool_result.get("input") or {},
                "tool_output": tool_result.get("output") or {},
                "timestamp": _now_iso(),
            },
        )


def _emit_waiting_human(
    job_id: str,
    run_id: str,
    node_id: str,
    node_type: str,
    question: str,
    resume_target: str,
    *,
    interrupt_id: str | None = None,
    interaction_id: str | None = None,
    context: dict | None = None,
):
    _publish_event(
        job_id,
        {
            "event_id": _new_event_id(),
            "event_type": "waiting_human",
            "run_id": run_id,
            "node_id": node_id,
            "node_type": node_type,
            "interrupt_id": interrupt_id,
            "interaction_id": interaction_id,
            "question": question,
            "context": context or {},
            "resume_target": resume_target,
            "timestamp": _now_iso(),
        },
    )


def _emit_run_completed(job_id: str, run_id: str):
    _publish_event(
        job_id,
        {
            "event_id": _new_event_id(),
            "event_type": "run_completed",
            "run_id": run_id,
            "status": "success",
            "timestamp": _now_iso(),
        },
    )


def _emit_run_failed(job_id: str, run_id: str, error_message: str):
    _publish_event(
        job_id,
        {
            "event_id": _new_event_id(),
            "event_type": "run_failed",
            "run_id": run_id,
            "status": "failed",
            "error_message": error_message,
            "timestamp": _now_iso(),
        },
    )


def _resolve_node_id(node_name: str, payload: dict) -> str:
    if payload.get("current_task_id"):
        return payload["current_task_id"]

    for task in payload.get("task_queue", []) or []:
        if task.get("agent_type") == node_name:
            return task.get("id", node_name)

    if node_name == "planner":
        return "0"
    return node_name


def _record_graph_event(
    project_id: str,
    version: str,
    node_name: str,
    output,
    *,
    job_id: str | None = None,
):
    payload = _coerce_event_output(output)
    if payload.get("human_intervention_required"):
        payload["pending_interrupt"] = _ensure_human_interaction_record(
            project_id,
            version,
            job_id,
            payload.get("pending_interrupt") or {},
        )
    node_run_status = RUN_STATUS_WAITING_HUMAN if payload.get("human_intervention_required") else RUN_STATUS_RUNNING
    current_node = payload.get("current_node") or node_name
    _set_runtime_state(
        project_id,
        version,
        run_status=node_run_status,
        current_node=current_node,
        waiting_reason=payload.get("waiting_reason"),
        pending_interrupt=payload.get("pending_interrupt") if payload.get("human_intervention_required") else None,
        job_id=job_id,
    )
    _sync_workflow_projection_from_payload(
        project_id,
        version,
        payload,
        run_id=job_id,
        authoritative_tasks=node_name in {"planner", "bootstrap"},
    )
    return payload


def _finalize_waiting_interactions_for_version(
    project_id: str,
    version: str,
    *,
    new_status: str,
    event_type: str,
    payload: Dict[str, Any] | None = None,
) -> None:
    active_records = metadata_db.list_human_interactions(
        project_id,
        version,
        statuses=["waiting_user", "answered", "resumed"],
    )
    completed_at = _now_iso() if new_status in {"completed", "cancelled", "superseded"} else None
    for record in active_records:
        metadata_db.update_human_interaction(
            record["interaction_id"],
            status=new_status,
            completed_at=completed_at,
        )
        metadata_db.append_human_interaction_event(
            event_id=str(uuid.uuid4()),
            interaction_id=record["interaction_id"],
            event_type=event_type,
            payload=payload or {},
        )


def _handle_structured_graph_event(
    job_id: str,
    project_id: str,
    version: str,
    node_name: str,
    payload: dict,
    previous_artifacts: dict,
) -> dict:
    run_id = job_id
    node_id = _resolve_node_id(node_name, payload)
    node_type = node_name

    completed_status = "success"
    if node_name not in {"bootstrap", "supervisor"}:
        matched_task = next((task for task in payload.get("task_queue", []) if task.get("agent_type") == node_name), None)
        if matched_task:
            completed_status = matched_task.get("status", "success")
        elif payload.get("human_intervention_required"):
            completed_status = "skipped"

    for history_entry in payload.get("history", []):
        _append_job_log(job_id, history_entry, project_id=project_id, version=version)
        _emit_text_delta(job_id, run_id, node_id, node_type, history_entry, "history")

    _emit_tool_events(job_id, run_id, node_id, node_type, payload.get("tool_results"))

    current_artifacts = _load_artifacts_from_disk(project_id, version)
    _emit_artifact_updates(job_id, run_id, node_id, node_type, previous_artifacts, current_artifacts)
    artifact_governance = _register_artifact_updates(project_id, version, run_id, node_type, previous_artifacts, current_artifacts)
    _record_artifact_governance_summary(project_id, version, run_id, node_id, node_type, artifact_governance)
    _emit_artifact_governance_reviewable(job_id, run_id, node_id, node_type, artifact_governance)

    if payload.get("human_intervention_required"):
        pending_interrupt = payload.get("pending_interrupt") or {}
        question = pending_interrupt.get("question") or payload.get("waiting_reason") or "Human input required to continue."
        _emit_waiting_human(
            job_id,
            run_id,
            node_id,
            node_type,
            question,
            resume_target=pending_interrupt.get("resume_target", node_type),
            interrupt_id=pending_interrupt.get("interrupt_id"),
            interaction_id=pending_interrupt.get("interaction_id"),
            context=pending_interrupt.get("context") or {},
        )

    _emit_node_completed(
        job_id,
        run_id,
        node_id,
        node_type,
        completed_status,
        project_id=project_id,
        version_id=version,
    )

    if node_name == "bootstrap":
        resume_target_node = payload.get("resume_target_node")
        if resume_target_node:
            resume_task = next(
                (task for task in payload.get("task_queue", []) if task.get("agent_type") == resume_target_node),
                None,
            )
            _emit_node_started(
                job_id,
                run_id,
                (resume_task or {}).get("id", "0" if resume_target_node == "planner" else resume_target_node),
                resume_target_node,
                project_id=project_id,
                version_id=version,
            )
        elif payload.get("resume_action") != "approve":
            _emit_node_started(job_id, run_id, "0", "planner", project_id=project_id, version_id=version)
    elif node_name == "supervisor":
        next_node = payload.get("next")
        if isinstance(next_node, list):
            dispatched_tasks = payload.get("dispatched_tasks") or []
            task_id_by_agent = {
                task.get("agent_type"): task.get("id")
                for task in dispatched_tasks
                if task.get("agent_type") and task.get("id")
            }
            for index, node_type in enumerate(next_node):
                if node_type in {"END", "human_review", "supervisor_advance"}:
                    continue
                current_task_ids = payload.get("current_task_ids") or []
                next_node_id = task_id_by_agent.get(node_type) or (
                    current_task_ids[index] if index < len(current_task_ids) else node_type
                )
                _emit_node_started(job_id, run_id, next_node_id, node_type, project_id=project_id, version_id=version)
        elif next_node and next_node not in {"END", "human_review", "supervisor_advance"}:
            next_node_id = payload.get("current_task_id") or next_node
            _emit_node_started(job_id, run_id, next_node_id, next_node, project_id=project_id, version_id=version)

    return current_artifacts


def _initial_history(project_id: str, history: list[str]) -> list[str]:
    if history:
        return history
    return [f"[SYSTEM] Initializing design session for {project_id}..."]


def _build_resume_task_queue(current_state: dict, resume_action: str, resume_target_node: str | None = None) -> list[dict]:
    if resume_action != "revise":
        if not resume_target_node or resume_target_node == "supervisor":
            return current_state.get("task_queue", [])

        updated_queue = []
        target_found = False
        for task in current_state.get("task_queue", []):
            if task.get("agent_type") == resume_target_node:
                updated_queue.append({**task, "status": "running"})
                target_found = True
            else:
                updated_queue.append(dict(task))
        if target_found:
            return updated_queue
        if resume_target_node == "planner":
            return [{"id": "0", "agent_type": "planner", "status": "running", "dependencies": [], "priority": 100}]
        return current_state.get("task_queue", [])
    if resume_target_node and resume_target_node not in {"planner", "supervisor"}:
        reset_queue = _reset_retry_branch(current_state.get("task_queue", []), resume_target_node)
        return [
            {**task, "status": "running"} if task.get("agent_type") == resume_target_node else task
            for task in reset_queue
        ]
    return [{"id": "0", "agent_type": "planner", "status": "running", "dependencies": [], "priority": 100}]


def _resolve_resume_workflow_phase(
    task_queue: list[dict],
    resume_action: str | None,
    resume_target_node: str | None,
    fallback_phase: str | None,
) -> str | None:
    if resume_target_node and resume_target_node not in {"planner", "supervisor"}:
        target_task = next((task for task in task_queue if task.get("agent_type") == resume_target_node), None)
        if target_task:
            metadata = target_task.get("metadata") or {}
            return target_task.get("phase") or metadata.get("workflow_phase") or fallback_phase

    if resume_action == "revise":
        return "ANALYSIS"
    return fallback_phase


def _reset_retry_branch(task_queue: list[dict], target_node_type: str) -> list[dict]:
    tasks_by_id = {task["id"]: dict(task) for task in task_queue}
    target_task = next((task for task in task_queue if task.get("agent_type") == target_node_type), None)
    if not target_task:
        return task_queue

    to_reset = {target_task["id"]}
    changed = True
    while changed:
        changed = False
        for task in task_queue:
            deps = set(task.get("dependencies", []))
            if deps & to_reset and task["id"] not in to_reset:
                to_reset.add(task["id"])
                changed = True

    reset_queue = []
    for task in task_queue:
        if task["id"] in to_reset:
            reset_queue.append({**task, "status": "todo"})
        else:
            reset_queue.append(dict(task))
    return reset_queue


def _build_graph_input_state(
    job_id: str,
    project_id: str,
    version: str,
    requirement_text: str,
    persisted_state: dict | None,
    *,
    resume_action: str | None = None,
    feedback: str = "",
    model: str | None = None,
) -> dict:
    messages = list((persisted_state or {}).get("messages", []))
    history = list((persisted_state or {}).get("history", []))
    resume_target_node = (persisted_state or {}).get("resume_target_node")
    if resume_action:
        human_message = {
            "role": "human",
            "action": resume_action,
            "content": feedback,
            "timestamp": _now_iso(),
        }
        messages.append(human_message)
        history.append(
            f"[HUMAN] Action: {resume_action}. Feedback: {feedback or 'None'}"
        )

    design_context = dict((persisted_state or {}).get("design_context", {}) or {})
    orchestrator_context = dict((design_context or {}).get("orchestrator", {}) or {})
    if "max_react_steps" in orchestrator_context:
        design_context["orchestrator"] = {
            "max_react_steps": orchestrator_context["max_react_steps"],
        }
    else:
        design_context.pop("orchestrator", None)

    if model:
        design_context["model"] = model
        # Try to lookup specific config for this model ID in the project
        try:
            from services.db_service import metadata_db
            project_models = metadata_db.list_project_models(project_id, include_secrets=True)
            # Find the model by ID (passed from frontend selectedModel)
            model_config = next((m for m in project_models if m["id"] == model), None)
            if model_config:
                design_context["model_config"] = {
                    "provider": model_config["provider"],
                    "api_key": model_config["api_key"],
                    "base_url": model_config["base_url"],
                    "headers": model_config.get("headers"),
                    "model_name": model_config["model_name"]
                }
                # Also set the model_name for easy access
                design_context["model"] = model_config["model_name"]
        except Exception as e:
            print(f"[ERROR] Failed to lookup model config for {model}: {e}")

    resume_task_queue = _build_resume_task_queue(persisted_state or {}, resume_action or "", resume_target_node)
    workflow_phase = _resolve_resume_workflow_phase(
        resume_task_queue,
        resume_action,
        resume_target_node,
        (persisted_state or {}).get("workflow_phase", "INIT"),
    )

    state = {
        "project_id": project_id,
        "version": version,
        "run_id": job_id,
        "requirement": requirement_text or (persisted_state or {}).get("requirement", ""),
        "design_context": design_context,
        "task_queue": resume_task_queue,
        "workflow_phase": workflow_phase,
        "history": _initial_history(project_id, history),
        "messages": messages,
        "artifacts": (persisted_state or {}).get("artifacts", {}),
        "human_intervention_required": False,
        "waiting_reason": None,
        "pending_interrupt": (persisted_state or {}).get("pending_interrupt"),
        "human_answers": (persisted_state or {}).get("human_answers", {}),
        "last_worker": (persisted_state or {}).get("last_worker"),
        "current_node": "bootstrap",
        "resume_target_node": resume_target_node,
        "run_status": RUN_STATUS_RUNNING,
        "updated_at": _now_iso(),
        "resume_action": resume_action,
        "human_feedback": feedback,
    }
    return state


async def run_orchestrator_task(
    job_id: str,
    project_id: str,
    version: str,
    requirement_text: str,
    *,
    resume_action: str | None = None,
    feedback: str = "",
    persisted_state_override: dict | None = None,
    model: str | None = None,
    preflight_checked: bool = False,
):
    thread_id = _thread_id(project_id, version)
    print(f"\n[DEBUG] Starting/Resuming Job: {job_id} for Thread: {thread_id}")
    _ensure_job(job_id)["status"] = RUN_STATUS_RUNNING
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_RUNNING,
        current_node="bootstrap",
        can_resume=False,
        job_id=job_id,
    )
    _emit_node_started(job_id, job_id, "bootstrap", "bootstrap", project_id=project_id, version_id=version)

    try:
        project_path = PROJECTS_DIR / project_id / version
        baseline_path = project_path / "baseline"
        baseline_path.mkdir(parents=True, exist_ok=True)
        (project_path / "logs").mkdir(parents=True, exist_ok=True)

        if requirement_text:
            (baseline_path / "raw-requirements.md").write_text(requirement_text, encoding="utf-8")

        persisted_state = persisted_state_override if persisted_state_override is not None else get_workflow_state(project_id, version)
        initial_state = _build_graph_input_state(
            job_id,
            project_id,
            version,
            requirement_text,
            persisted_state,
            resume_action=resume_action,
            feedback=feedback,
            model=model,
        )
        if not preflight_checked:
            preflight_result = await asyncio.to_thread(
                _run_llm_connectivity_preflight,
                project_id,
                version,
                initial_state,
                None,
            )
            _append_job_log(
                job_id,
                f"[SYSTEM] LLM connectivity preflight passed: {preflight_result.get('message') or 'ok'}",
                project_id=project_id,
                version=version,
            )

        config = _graph_config(project_id, version, job_id)
        known_artifacts = _load_artifacts_from_disk(project_id, version)
        paused_for_human = False
        pause_node = None
        pause_reason = None
        async with _graph_for_run() as design_graph:
            async for event in design_graph.astream(initial_state, config=config, stream_mode="updates"):
                for node_name, output in event.items():
                    print(f"[DEBUG] Node {node_name} yielded an update event.")
                    payload = _record_graph_event(project_id, version, node_name, output, job_id=job_id)
                    if payload.get("human_intervention_required"):
                        paused_for_human = True
                        pause_node = node_name
                        pause_reason = payload.get("waiting_reason")
                    known_artifacts = _handle_structured_graph_event(job_id, project_id, version, node_name, payload, known_artifacts)

        # FINAL STATE GUARD: if the graph exits with unfinished tasks, mark the run failed
        # instead of leaving the workflow projection stuck in a pseudo-running state.
        if not paused_for_human:
            final_state = get_workflow_state(project_id, version, include_runtime=False)
            if final_state:
                queue = final_state.get("task_queue", [])
                running_tasks = [t for t in queue if t["status"] == "running"]
                todo_tasks = [t for t in queue if t["status"] == "todo"]
                if running_tasks or todo_tasks:
                    unfinished_tasks = running_tasks or todo_tasks
                    if running_tasks:
                        print(f"[ERROR] Graph execution ended but {len(running_tasks)} tasks are still 'running'. Marking as failed.")
                        waiting_reason = "Execution stalled: Background task ended unexpectedly."
                    else:
                        print(f"[ERROR] Graph execution ended but {len(todo_tasks)} tasks remain 'todo'. Marking as failed.")
                        waiting_reason = "Execution stalled: Graph ended with unfinished tasks remaining."

                    # Force failed status to avoid frontend spinning
                    for task in unfinished_tasks:
                        _emit_node_completed(
                            job_id,
                            job_id,
                            task["id"],
                            task["agent_type"],
                            "failed",
                            project_id=project_id,
                            version_id=version,
                        )
                    
                    # Update persisted state to reflect failure
                    _set_runtime_state(
                        project_id,
                        version,
                        run_status=RUN_STATUS_FAILED,
                        current_node=unfinished_tasks[0]["agent_type"],
                        waiting_reason=waiting_reason,
                        can_resume=True,
                        job_id=job_id,
                    )
                    _finalize_waiting_interactions_for_version(
                        project_id,
                        version,
                        new_status="superseded",
                        event_type="workflow_failed",
                        payload={"run_id": job_id, "reason": waiting_reason},
                    )
                    _ensure_job(job_id)["status"] = RUN_STATUS_FAILED
                    _emit_run_failed(job_id, job_id, waiting_reason)
                    return

        if paused_for_human:
            _ensure_job(job_id)["status"] = RUN_STATUS_WAITING_HUMAN
            _set_runtime_state(
                project_id,
                version,
                run_status=RUN_STATUS_WAITING_HUMAN,
                current_node=pause_node,
                waiting_reason=pause_reason,
                can_resume=True,
                job_id=job_id,
            )
            return

        latest_state = get_workflow_state(project_id, version, include_runtime=False)
        latest_status = latest_state.get("run_status") if latest_state else RUN_STATUS_SUCCESS
        if latest_status == RUN_STATUS_SUCCESS:
            _ensure_job(job_id)["status"] = RUN_STATUS_SUCCESS
            _finalize_waiting_interactions_for_version(
                project_id,
                version,
                new_status="completed",
                event_type="workflow_completed",
                payload={"run_id": job_id},
            )
            _set_runtime_state(
                project_id,
                version,
                run_status=RUN_STATUS_SUCCESS,
                current_node=None,
                waiting_reason=None,
                can_resume=False,
                job_id=job_id,
            )
            _emit_run_completed(job_id, job_id)
        else:
            _ensure_job(job_id)["status"] = latest_status
            _set_runtime_state(
                project_id,
                version,
                run_status=latest_status,
                current_node=latest_state.get("current_node") if latest_state else None,
                waiting_reason=latest_state.get("waiting_reason") if latest_state else None,
                can_resume=latest_state.get("can_resume") if latest_state else False,
                job_id=job_id,
            )
    except Exception as exc:
        import traceback

        error_msg = f"[ERROR] LangGraph execution error: {exc}\n{traceback.format_exc()}"
        print(error_msg)
        _append_job_log(job_id, error_msg, project_id=project_id, version=version)
        _emit_text_delta(job_id, job_id, runtime_registry.get(thread_id, {}).get("current_node") or "run", runtime_registry.get(thread_id, {}).get("current_node") or "run", error_msg, "stderr")
        current_state = get_workflow_state(project_id, version, include_runtime=False) or {}
        current_queue = current_state.get("task_queue") or []
        if not current_queue:
            current_queue = [
                {"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "failed", "dependencies": [], "priority": 100}
            ]
        _mark_workflow_failed(
            project_id,
            version,
            job_id=job_id,
            run_id=job_id,
            reason=str(exc),
            current_node=current_state.get("current_node"),
            task_queue=current_queue,
        )
    finally:
        # Final log flush: combine memory logs with state history for maximum durability
        job = jobs.get(job_id)
        latest_state = get_workflow_state(project_id, version)
        
        history = latest_state.get("history", []) if latest_state else []
        mem_logs = job.get("logs", []) if job else []
        
        # Merge memory logs and history, maintaining order but avoiding duplicates
        # Priority: Memory logs are more detailed (real-time), History are milestone-based
        combined_logs = list(mem_logs)
        seen = {run_log_dedupe_key(log) for log in combined_logs}
        for h in history:
            key = run_log_dedupe_key(h)
            if key not in seen:
                seen.add(key)
                combined_logs.append(h)
        
        combined_logs = _filter_stale_planner_expert_selection_wait_logs(combined_logs, latest_state)
        if combined_logs:
            save_run_log(project_id, version, BASE_DIR, combined_logs)


def get_workflow_state(project_id: str, version: str, include_runtime: bool = True):
    config = _graph_config(project_id, version)
    runtime = runtime_registry.get(_thread_id(project_id, version), {}) if include_runtime else {}
    try:
        with _graph_for_state() as design_graph:
            try:
                state = design_graph.get_state(config)
                if state and state.values:
                    return _normalize_state(project_id, version, state.values, runtime=runtime)
            except Exception as e:
                print(f"[Orchestrator] Error getting graph state for {project_id}/{version}: {e}")

        persisted_logs = get_run_log(project_id, version, BASE_DIR)
        fallback_state = None
        if persisted_logs:
            fallback_state = {
                "project_id": project_id,
                "version": version,
                "workflow_phase": "ARCHIVED",
                "task_queue": _build_legacy_task_queue(project_id, version),
                "history": persisted_logs,
            }
        return _normalize_state(project_id, version, fallback_state, runtime=runtime)
    except Exception as e:
        print(f"[Orchestrator] Critical error in get_workflow_state for {project_id}/{version}: {e}")
        return _normalize_state(project_id, version, None, runtime=runtime)


def list_interactions(project_id: str, version: str) -> List[Dict[str, Any]]:
    return [
        _hydrate_human_interaction(record) or {}
        for record in metadata_db.list_human_interactions(project_id, version)
    ]


def get_current_interaction(project_id: str, version: str) -> Dict[str, Any] | None:
    current_state = get_workflow_state(project_id, version)
    pending_interrupt = (current_state or {}).get("pending_interrupt") or {}
    interaction_id = str(pending_interrupt.get("interaction_id") or "").strip()
    if interaction_id:
        return _hydrate_human_interaction(metadata_db.get_human_interaction(interaction_id))
    latest = metadata_db.get_latest_human_interaction_for_version(
        project_id,
        version,
        statuses=["waiting_user", "answered"],
    )
    return _hydrate_human_interaction(latest)


def get_interaction_detail(project_id: str, version: str, interaction_id: str) -> Dict[str, Any] | None:
    record = metadata_db.get_human_interaction(interaction_id)
    if not record or record.get("project_id") != project_id or record.get("version_id") != version:
        return None
    return _hydrate_human_interaction(record)


def get_clarified_requirements(project_id: str, version: str) -> Dict[str, Any]:
    return _get_clarified_requirements_snapshot(project_id, version)


def _translate_interaction_response_payload(
    interaction_id: str,
    payload: Dict[str, Any] | None,
) -> Dict[str, Any]:
    payload = dict(payload or {})
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    action = str(payload.get("action") or response.get("action") or "").strip().lower()
    response_type = str(response.get("type") or "").strip().lower()
    response_text = str(
        response.get("text")
        or response.get("feedback")
        or response.get("answer")
        or ""
    ).strip()
    values = response.get("values")
    translated = {
        "interaction_id": interaction_id,
        "response": response,
    }
    if action not in {"approve", "revise", "answer"}:
        if response_type in {"approval", "review"}:
            value = response.get("value")
            action = "approve" if value in {True, "approve", "approved"} else "revise"
        else:
            action = "answer"
    translated["action"] = action
    if response_type == "expert_multi_select":
        expert_values = values if isinstance(values, list) else response.get("selected_experts")
        translated["selected_experts"] = _normalize_string_list(expert_values)
        translated["answer"] = response_text
    elif isinstance(response.get("selected_experts"), list):
        translated["selected_experts"] = _normalize_string_list(response.get("selected_experts"))
        translated["answer"] = response_text
    elif response_type == "multi_select":
        option_values = values if isinstance(values, list) else response.get("selected_options")
        translated["selected_options"] = _normalize_string_list(option_values)
        translated["answer"] = response_text
    elif isinstance(response.get("selected_options"), list):
        translated["selected_options"] = _normalize_string_list(response.get("selected_options"))
        translated["answer"] = response_text
    elif "value" in response and response.get("value") not in (None, ""):
        translated["selected_option"] = str(response.get("value")).strip()
        translated["answer"] = response_text
    elif action == "revise":
        translated["feedback"] = response_text
    else:
        translated["answer"] = response_text
    return translated


async def submit_interaction_response(project_id: str, version: str, interaction_id: str, payload: Dict[str, Any]) -> bool:
    translated = _translate_interaction_response_payload(interaction_id, payload)
    return await resume_workflow(project_id, version, translated)


async def resume_workflow(project_id: str, version: str, human_input: dict):
    action = (human_input or {}).get("action")
    feedback = (human_input or {}).get("feedback", "")
    answer = (human_input or {}).get("answer", "")
    selected_option = ((human_input or {}).get("selected_option") or "").strip()
    selected_options = _normalize_string_list((human_input or {}).get("selected_options"))
    if action not in {"approve", "revise", "answer"}:
        return False

    current_state = get_workflow_state(project_id, version)
    if not current_state or not current_state.get("can_resume"):
        return False

    run_id = current_state.get("run_id")
    if not run_id:
        return False

    interaction_id = (human_input or {}).get("interaction_id")
    interaction_record = metadata_db.get_human_interaction(interaction_id) if interaction_id else None
    pending_interrupt = current_state.get("pending_interrupt") or _pending_interrupt_from_interaction_record(interaction_record)
    requested_node_id = (human_input or {}).get("node_id") or pending_interrupt.get("node_id") or current_state.get("current_node")
    requested_interrupt_id = (human_input or {}).get("interrupt_id") or pending_interrupt.get("interrupt_id")
    interaction_id = interaction_id or pending_interrupt.get("interaction_id")

    if pending_interrupt:
        if requested_node_id != pending_interrupt.get("node_id"):
            return False
        if (
            (human_input or {}).get("interrupt_id")
            and pending_interrupt.get("interrupt_id")
            and requested_interrupt_id != pending_interrupt.get("interrupt_id")
        ):
            return False
        if pending_interrupt.get("interaction_id") and interaction_id != pending_interrupt.get("interaction_id"):
            return False

    normalized_feedback = feedback
    pending_resume_target = str(
        pending_interrupt.get("resume_target")
        or pending_interrupt.get("node_type")
        or requested_node_id
        or ""
    ).strip()
    if action in {"approve", "revise"} and pending_resume_target:
        resume_target_node = pending_resume_target
    else:
        resume_target_node = "supervisor" if action == "approve" else "planner"
    if action == "approve" and resume_target_node == "planner":
        resume_target_node = "supervisor"
    human_answers = dict(current_state.get("human_answers") or {})
    interaction_answer_payload: Dict[str, Any] = {
        "action": action,
        "response": (human_input or {}).get("response") or {},
        "answer": str(answer or "").strip(),
        "feedback": str(feedback or "").strip(),
        "selected_option": selected_option or None,
        "selected_options": selected_options or None,
        "answer_merge_targets": _normalize_string_list(
            (((human_input or {}).get("response") or {}).get("answer_merge_targets"))
            or ((pending_interrupt.get("context") or {}).get("answer_merge_targets"))
        ),
    }

    if action == "answer":
        normalized_answer = answer.strip()
        normalized_feedback = normalized_answer
        has_selected_experts_payload = isinstance((human_input or {}).get("selected_experts"), list)
        selected_experts = _normalize_string_list((human_input or {}).get("selected_experts"))
        has_selected_options_payload = isinstance((human_input or {}).get("selected_options"), list)
        interrupt_context = pending_interrupt.get("context") or {}
        interaction_type = str(interrupt_context.get("interaction_type") or "").strip()
        if not normalized_answer and not selected_option and not has_selected_experts_payload and not has_selected_options_payload:
            return False
        resume_target_node = pending_interrupt.get("resume_target") or requested_node_id or "planner"
        target_key = requested_node_id or "planner"
        answer_entries = list(human_answers.get(target_key, []))
        summary = normalized_answer
        if selected_option:
            summary = f"Selected option: {selected_option}"
            if normalized_answer:
                summary = f"{summary}. {normalized_answer}"
        if has_selected_experts_payload:
            summary = f"Selected experts: {', '.join(selected_experts) if selected_experts else '(none)'}"
            if normalized_answer:
                summary = f"{summary}. {normalized_answer}"
        if has_selected_options_payload:
            summary = f"Selected options: {', '.join(selected_options) if selected_options else '(none)'}"
            if normalized_answer:
                summary = f"{summary}. {normalized_answer}"
        answer_entries.append(
            {
                "interrupt_id": requested_interrupt_id,
                "question": pending_interrupt.get("question") or current_state.get("waiting_reason"),
                "answer": normalized_answer,
                "selected_option": selected_option or None,
                "selected_options": selected_options if has_selected_options_payload else None,
                "selected_experts": selected_experts if has_selected_experts_payload else None,
                "recommended_experts": (
                    _normalize_string_list(interrupt_context.get("recommended_experts"))
                    if has_selected_experts_payload
                    else None
                ),
                "selection_type": interaction_type if has_selected_experts_payload else None,
                "summary": summary,
            }
        )
        human_answers[target_key] = answer_entries
        interaction_answer_payload["selected_options"] = selected_options if has_selected_options_payload else None
        interaction_answer_payload["selected_experts"] = selected_experts if has_selected_experts_payload else None
        interaction_answer_payload["summary"] = summary
    elif action == "approve":
        interaction_answer_payload["summary"] = "Approved to continue."
    elif action == "revise":
        interaction_answer_payload["summary"] = feedback.strip() or "Requested revision before retry."

    resume_task_queue = _build_resume_task_queue(current_state, action, resume_target_node)
    resumed_workflow_phase = _resolve_resume_workflow_phase(
        resume_task_queue,
        action,
        resume_target_node,
        current_state.get("workflow_phase") or "INIT",
    )
    resumed_state = {
        **current_state,
        "task_queue": resume_task_queue,
        "pending_interrupt": None,
        "human_answers": human_answers,
        "resume_target_node": resume_target_node,
        "human_intervention_required": False,
        "waiting_reason": None,
        "run_status": RUN_STATUS_RUNNING,
        "current_node": "bootstrap",
        "workflow_phase": resumed_workflow_phase,
    }
    if interaction_id:
        metadata_db.update_human_interaction(
            interaction_id,
            run_id=run_id,
            status="answered",
            answer=interaction_answer_payload,
            summary=_build_interaction_summary_from_payload(human_input or {}, pending_interrupt),
        )
        metadata_db.append_human_interaction_event(
            event_id=str(uuid.uuid4()),
            interaction_id=interaction_id,
            event_type="response_submitted",
            payload=interaction_answer_payload,
        )
    _persist_clarification_artifacts(project_id, version, state=resumed_state)

    _delete_checkpoint_state(project_id, version)
    _sync_workflow_projection_from_payload(
        project_id,
        version,
        resumed_state,
        run_id=run_id,
        authoritative_tasks=True,
    )
    job = _ensure_job(run_id)
    job["logs"] = _filter_stale_planner_expert_selection_wait_logs(job.get("logs", []), resumed_state)
    existing_persisted_logs = get_run_log(project_id, version, BASE_DIR)
    persisted_logs = _filter_stale_planner_expert_selection_wait_logs(existing_persisted_logs, resumed_state)
    if existing_persisted_logs != persisted_logs:
        save_run_log(project_id, version, BASE_DIR, persisted_logs)
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_RUNNING,
        current_node="bootstrap",
        waiting_reason=None,
        can_resume=False,
        job_id=run_id,
    )
    if interaction_id:
        metadata_db.update_human_interaction(interaction_id, status="resumed")
        metadata_db.append_human_interaction_event(
            event_id=str(uuid.uuid4()),
            interaction_id=interaction_id,
            event_type="workflow_resumed",
            payload={
                "run_id": run_id,
                "resume_target_node": resume_target_node,
                "action": action,
            },
        )
    _launch_runtime_task(
        _thread_id(project_id, version),
        run_orchestrator_task(
            run_id,
            project_id,
            version,
            current_state.get("requirement", ""),
            resume_action=action,
            feedback=normalized_feedback,
            persisted_state_override=resumed_state,
            # The run already reached a human interrupt through real LLM calls.
            # Avoid a second probe on resume that can diverge from the runtime model path.
            preflight_checked=True,
        )
    )
    return True


async def retry_workflow_node(
    project_id: str,
    version: str,
    node_type: str,
    model: str | None = None,
):
    current_state = get_workflow_state(project_id, version)
    if not current_state:
        return False

    target_task = next((task for task in current_state.get("task_queue", []) if task.get("agent_type") == node_type), None)
    if not target_task or target_task.get("status") != "failed":
        return False

    run_id = current_state.get("run_id")
    if not run_id:
        return False

    thread_id = _thread_id(project_id, version)
    # FORCE CANCEL for Retry specifically to clear DB locks
    existing_task = runtime_tasks.get(thread_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        try:
            await asyncio.wait_for(existing_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    reset_queue = _reset_retry_branch(current_state.get("task_queue", []), node_type)
    retry_task_queue = _build_resume_task_queue({"task_queue": reset_queue}, "approve", node_type)
    retry_workflow_phase = _resolve_resume_workflow_phase(
        retry_task_queue,
        "approve",
        node_type,
        current_state.get("workflow_phase") or "INIT",
    )
    retry_state = {
        **current_state,
        "task_queue": retry_task_queue,
        "history": [
            *(current_state.get("history") or []),
            f"[HUMAN] Retry node: {node_type}",
        ],
        "run_status": RUN_STATUS_RUNNING,
        "current_node": "bootstrap",
        "human_intervention_required": False,
        "waiting_reason": None,
        "resume_action": "approve",
        "resume_target_node": node_type,
        "workflow_phase": retry_workflow_phase,
    }

    _delete_checkpoint_state(project_id, version)
    _ensure_job(run_id)
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_RUNNING,
        current_node="bootstrap",
        waiting_reason=None,
        can_resume=False,
        job_id=run_id,
    )
    _sync_workflow_projection_from_payload(
        project_id,
        version,
        retry_state,
        run_id=run_id,
        authoritative_tasks=True,
    )
    _finalize_waiting_interactions_for_version(
        project_id,
        version,
        new_status="superseded",
        event_type="node_retry_requested",
        payload={"run_id": run_id, "node_type": node_type},
    )
    # Launch new task cleanly
    _launch_runtime_task(
        thread_id,
        run_orchestrator_task(
            run_id,
            project_id,
            version,
            current_state.get("requirement", ""),
            resume_action="approve",
            feedback="",
            persisted_state_override=retry_state,
            model=model,
        )
    )
    return True


def trigger_downstream_regeneration(
    *,
    project_id: str,
    version: str,
    target_expert_id: str,
    source_artifact_id: str,
    accepted_artifact_id: str,
    impacted_artifact_id: str,
    impact_id: str,
    feedback: str = "",
) -> dict:
    current_state = get_workflow_state(project_id, version)
    if not current_state:
        return {
            "status": "skipped",
            "reason": "Workflow state not found.",
            "impact_id": impact_id,
            "artifact_id": impacted_artifact_id,
            "target_expert_id": target_expert_id,
        }

    if current_state.get("run_status") in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING, RUN_STATUS_SCHEDULED}:
        return {
            "status": "skipped",
            "reason": f"Workflow is already {current_state.get('run_status')}.",
            "impact_id": impact_id,
            "artifact_id": impacted_artifact_id,
            "target_expert_id": target_expert_id,
        }

    target_task = next(
        (task for task in current_state.get("task_queue", []) if task.get("agent_type") == target_expert_id),
        None,
    )
    if not target_task:
        return {
            "status": "skipped",
            "reason": "Target expert task not found in workflow queue.",
            "impact_id": impact_id,
            "artifact_id": impacted_artifact_id,
            "target_expert_id": target_expert_id,
        }

    run_id = current_state.get("run_id") or str(uuid.uuid4())
    reset_queue = _reset_retry_branch(current_state.get("task_queue", []), target_expert_id)
    regeneration_queue = _build_resume_task_queue({"task_queue": reset_queue}, "approve", target_expert_id)
    regeneration_phase = _resolve_resume_workflow_phase(
        regeneration_queue,
        "approve",
        target_expert_id,
        current_state.get("workflow_phase") or "INIT",
    )
    normalized_feedback = feedback or (
        f"Accepted revision {accepted_artifact_id} impacts {impacted_artifact_id}; regenerate {target_expert_id}."
    )
    regeneration_state = {
        **current_state,
        "task_queue": regeneration_queue,
        "history": [
            *(current_state.get("history") or []),
            f"[SYSTEM] Downstream regeneration requested for {target_expert_id} after artifact revision acceptance.",
        ],
        "run_status": RUN_STATUS_RUNNING,
        "current_node": "bootstrap",
        "human_intervention_required": False,
        "waiting_reason": None,
        "pending_interrupt": None,
        "resume_action": "revise",
        "resume_target_node": target_expert_id,
        "workflow_phase": regeneration_phase,
        "human_feedback": normalized_feedback,
        "regeneration_context": {
            "source_artifact_id": source_artifact_id,
            "accepted_artifact_id": accepted_artifact_id,
            "impacted_artifact_id": impacted_artifact_id,
            "impact_id": impact_id,
            "target_expert_id": target_expert_id,
        },
    }

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return {
            "status": "skipped",
            "reason": "No running event loop available to launch regeneration.",
            "impact_id": impact_id,
            "artifact_id": impacted_artifact_id,
            "target_expert_id": target_expert_id,
        }

    _delete_checkpoint_state(project_id, version)
    _ensure_job(run_id)
    jobs[run_id]["status"] = RUN_STATUS_RUNNING
    _sync_workflow_projection_from_payload(
        project_id,
        version,
        regeneration_state,
        run_id=run_id,
        authoritative_tasks=True,
    )
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_RUNNING,
        current_node="bootstrap",
        waiting_reason=None,
        can_resume=False,
        job_id=run_id,
    )
    _finalize_waiting_interactions_for_version(
        project_id,
        version,
        new_status="superseded",
        event_type="downstream_regeneration_requested",
        payload={
            "run_id": run_id,
            "target_expert_id": target_expert_id,
            "impact_id": impact_id,
            "accepted_artifact_id": accepted_artifact_id,
        },
    )
    _launch_runtime_task(
        _thread_id(project_id, version),
        run_orchestrator_task(
            run_id,
            project_id,
            version,
            current_state.get("requirement", ""),
            resume_action="revise",
            feedback=normalized_feedback,
            persisted_state_override=regeneration_state,
        )
    )
    return {
        "status": "queued",
        "run_id": run_id,
        "impact_id": impact_id,
        "artifact_id": impacted_artifact_id,
        "target_expert_id": target_expert_id,
    }


async def continue_workflow(
    project_id: str,
    version: str,
    model: str | None = None,
):
    current_state = get_workflow_state(project_id, version)
    if not current_state:
        print(f"[DEBUG] continue_workflow: No state found for {project_id}/{version}")
        return False

    run_status = current_state.get("run_status")
    can_resume = current_state.get("can_resume")
    waiting_reason = current_state.get("waiting_reason")
    print(f"[DEBUG] continue_workflow: {project_id}/{version} status={run_status}, can_resume={can_resume}")

    # Support continuing from:
    # 1. queued status (normal continuation)
    # 2. waiting_human status (after user cancelled, need to retry with new parameters)
    is_cancelled = waiting_reason and "[CANCELLED]" in waiting_reason

    if run_status == RUN_STATUS_RUNNING:
        print(f"[DEBUG] continue_workflow: Status is running, cannot continue directly")
        return False

    if run_status != RUN_STATUS_QUEUED and run_status != RUN_STATUS_WAITING_HUMAN:
        print(f"[DEBUG] continue_workflow: Status is not queued or waiting_human ({run_status})")
        return False

    # If status is waiting_human but not cancelled, and can_resume is True, treat as normal resume
    # Only allow if can_resume is True or if it's a cancelled state
    if not can_resume and not is_cancelled:
        print(f"[DEBUG] continue_workflow: can_resume is False and not a cancelled state")
        return False

    has_todo_tasks = any(task.get("status") == "todo" for task in current_state.get("task_queue", []))
    if not has_todo_tasks:
        print(f"[DEBUG] continue_workflow: No todo tasks found")
        return False

    run_id = current_state.get("run_id")
    if not run_id:
        # Generate new run_id if not exists (e.g., after cancel)
        run_id = str(uuid.uuid4())

    try:
        preflight_result = await asyncio.to_thread(
            _run_llm_connectivity_preflight,
            project_id,
            version,
            current_state,
            model,
        )
        print(f"[DEBUG] continue_workflow: LLM connectivity preflight passed: {preflight_result.get('message')}")
    except Exception as exc:
        reason = str(exc)
        _mark_workflow_failed(
            project_id,
            version,
            run_id,
            reason=reason,
            current_node=current_state.get("current_node"),
            task_queue=current_state.get("task_queue") or [],
        )
        return True

    # Build history message based on continuation type
    if is_cancelled:
        history_msg = f"[HUMAN] Retry workflow with model={model or 'default'}"
    else:
        history_msg = "[HUMAN] Continue workflow from queued state"

    continue_state = {
        **current_state,
        "run_status": RUN_STATUS_RUNNING,
        "current_node": "bootstrap",
        "human_intervention_required": False,
        "waiting_reason": None,
        "can_resume": False,
        "resume_action": "approve",
        "history": [
            *(current_state.get("history") or []),
            history_msg,
        ],
    }

    _delete_checkpoint_state(project_id, version)
    _ensure_job(run_id)
    jobs[run_id]["status"] = RUN_STATUS_RUNNING

    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_RUNNING,
        current_node="bootstrap",
        waiting_reason=None,
        can_resume=False,
        job_id=run_id,
    )
    _finalize_waiting_interactions_for_version(
        project_id,
        version,
        new_status="resumed",
        event_type="workflow_continued",
        payload={"run_id": run_id},
    )

    # Cancel existing task before starting new one
    thread_id = _thread_id(project_id, version)
    existing_task = runtime_tasks.get(thread_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        try:
            await asyncio.wait_for(existing_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    _launch_runtime_task(
        thread_id,
        run_orchestrator_task(
            run_id,
            project_id,
            version,
            current_state.get("requirement", ""),
            resume_action="approve",
            feedback="",
            persisted_state_override=continue_state,
            model=model,
            preflight_checked=True,
        )
    )
    return True


async def cancel_workflow(
    project_id: str,
    version: str,
    reason: str | None = None,
) -> bool:
    """Cancel a running workflow and set it to a cancellable state for user to retry with new parameters."""
    current_state = get_workflow_state(project_id, version)
    if not current_state:
        print(f"[DEBUG] cancel_workflow: No state found for {project_id}/{version}")
        return False

    run_status = current_state.get("run_status")
    thread_id = _thread_id(project_id, version)

    # Cancel the running task if exists
    existing_task = runtime_tasks.get(thread_id)
    if existing_task and not existing_task.done():
        print(f"[DEBUG] Cancelling running task for thread {thread_id}")
        existing_task.cancel()
        try:
            await asyncio.wait_for(existing_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Determine which tasks to mark as todo based on current state
    task_queue = current_state.get("task_queue", [])
    current_node = current_state.get("current_node")

    # Mark the currently running task as todo, keep completed tasks as is
    updated_task_queue = []
    for task in task_queue:
        if task.get("status") == "running":
            # Set the running task back to todo so it can be retried
            updated_task_queue.append({
                **task,
                "status": "todo"
            })
        else:
            updated_task_queue.append(task)

    # Update job status
    run_id = current_state.get("run_id")
    if run_id and run_id in jobs:
        jobs[run_id]["status"] = RUN_STATUS_FAILED

    # Update runtime state to indicate cancelled and can be resumed
    cancel_reason = reason or "Cancelled by user"
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_WAITING_HUMAN,
        current_node=current_node,
        waiting_reason=f"[CANCELLED] {cancel_reason}. You can now retry with a different LLM.",
        can_resume=True,
        job_id=None,
    )
    _finalize_waiting_interactions_for_version(
        project_id,
        version,
        new_status="cancelled",
        event_type="workflow_cancelled",
        payload={"run_id": run_id, "reason": cancel_reason},
    )

    # Update the persisted state
    from services.db_service import metadata_db
    metadata_db.upsert_version(
        project_id,
        version,
        current_state.get("requirement", ""),
        RUN_STATUS_WAITING_HUMAN
    )

    # Add to history
    history = current_state.get("history", [])
    history.append(f"[HUMAN] Workflow cancelled: {cancel_reason}")

    print(f"[DEBUG] cancel_workflow: Workflow {project_id}/{version} cancelled successfully")
    return True


async def _scheduled_run_worker(schedule_id: str):
    schedule = metadata_db.get_scheduled_run(schedule_id)
    if not schedule or schedule.get("status") != "scheduled":
        scheduled_runtime_tasks.pop(schedule_id, None)
        return

    try:
        scheduled_at = _parse_iso_timestamp(schedule.get("scheduled_for"))
        if scheduled_at is None:
            raise ValueError("Invalid scheduled_for timestamp")

        delay_seconds = (scheduled_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        schedule = metadata_db.get_scheduled_run(schedule_id)
        if not schedule or schedule.get("status") != "scheduled":
            return

        project_id = schedule["project_id"]
        version = schedule["version_id"]
        job_id = trigger_orchestrator(
            project_id,
            version,
            schedule.get("requirement") or "",
            schedule.get("model"),
        )
        metadata_db.update_scheduled_run(
            schedule_id,
            status="triggered",
            error=None,
            triggered_job_id=job_id,
            triggered_at=_now_iso(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        schedule = metadata_db.get_scheduled_run(schedule_id)
        metadata_db.update_scheduled_run(schedule_id, status="failed", error=str(exc))
        if schedule:
            metadata_db.upsert_version(
                schedule["project_id"],
                schedule["version_id"],
                schedule.get("requirement") or "",
                RUN_STATUS_FAILED,
            )
            metadata_db.upsert_workflow_run(
                schedule["project_id"],
                schedule["version_id"],
                run_id=None,
                status=RUN_STATUS_FAILED,
                current_phase=None,
                current_node=None,
                waiting_reason=f"Scheduled run failed to start: {exc}",
                started_at=None,
                finished_at=_now_iso(),
            )
    finally:
        scheduled_runtime_tasks.pop(schedule_id, None)


def _schedule_pending_run(schedule: dict) -> None:
    schedule_id = schedule.get("schedule_id")
    if not schedule_id or schedule.get("status") != "scheduled":
        return

    existing_task = scheduled_runtime_tasks.get(schedule_id)
    if existing_task and not existing_task.done():
        return

    task = asyncio.create_task(_scheduled_run_worker(schedule_id))
    scheduled_runtime_tasks[schedule_id] = task

    def _cleanup(completed_task):
        if scheduled_runtime_tasks.get(schedule_id) is completed_task:
            scheduled_runtime_tasks.pop(schedule_id, None)

    task.add_done_callback(_cleanup)


async def restore_scheduled_runs() -> None:
    for schedule in metadata_db.list_pending_scheduled_runs():
        _schedule_pending_run(schedule)


async def schedule_orchestrator_run(
    project_id: str,
    version: str,
    requirement_text: str,
    scheduled_for: str,
    model: str | None = None,
) -> dict:
    scheduled_at = _parse_iso_timestamp(scheduled_for)
    if scheduled_at is None:
        raise ValueError("Invalid scheduled time.")

    if scheduled_at <= datetime.datetime.now(datetime.timezone.utc):
        raise ValueError("Scheduled time must be in the future.")

    project_root = PROJECTS_DIR / project_id / version
    project_root.mkdir(parents=True, exist_ok=True)

    existing_state = get_workflow_state(project_id, version)
    if existing_state and existing_state.get("run_status") in {
        RUN_STATUS_SCHEDULED,
        RUN_STATUS_QUEUED,
        RUN_STATUS_RUNNING,
    }:
        raise ValueError("This version already has an active or scheduled run.")

    _cancel_scheduled_tasks_for_version(project_id, version)
    metadata_db.cancel_scheduled_runs_for_version(
        project_id,
        version,
        statuses=["scheduled"],
        error="Superseded by a newer schedule.",
    )

    _delete_checkpoint_state(project_id, version)
    metadata_db.replace_workflow_tasks(project_id, version, run_id=None, tasks=[])
    runtime_registry.pop(_thread_id(project_id, version), None)

    waiting_reason = _format_scheduled_waiting_reason(scheduled_for)
    metadata_db.upsert_version(project_id, version, requirement_text, RUN_STATUS_SCHEDULED)
    metadata_db.upsert_workflow_run(
        project_id,
        version,
        run_id=None,
        status=RUN_STATUS_SCHEDULED,
        current_phase=None,
        current_node=None,
        waiting_reason=waiting_reason,
        started_at=None,
        finished_at=None,
    )

    schedule = metadata_db.create_scheduled_run(
        schedule_id=str(uuid.uuid4()),
        project_id=project_id,
        version_id=version,
        requirement=requirement_text,
        scheduled_for=scheduled_at.isoformat(),
        model=model,
        status="scheduled",
    )
    _schedule_pending_run(schedule)
    return schedule


def trigger_orchestrator(
    project_id: str,
    version: str,
    requirement_text: str,
    model: str | None = None,
) -> str:
    _cancel_scheduled_tasks_for_version(project_id, version)
    metadata_db.cancel_scheduled_runs_for_version(
        project_id,
        version,
        statuses=["scheduled"],
        error="Triggered immediately before scheduled start.",
    )
    job_id = str(uuid.uuid4())
    _ensure_job(job_id)
    jobs[job_id]["status"] = RUN_STATUS_QUEUED
    _set_runtime_state(
        project_id,
        version,
        run_status=RUN_STATUS_QUEUED,
        current_node="bootstrap",
        can_resume=False,
        job_id=job_id,
    )
    preflight_state = {
        "project_id": project_id,
        "version": version,
        "requirement": requirement_text,
        "design_context": {},
        "task_queue": [
            {"id": "0", "agent_type": "planner", "stage": 0, "phase": "ANALYSIS", "status": "todo", "dependencies": [], "priority": 100}
        ],
    }
    try:
        _run_llm_connectivity_preflight(project_id, version, preflight_state, model)
    except Exception as exc:
        _mark_workflow_failed(
            project_id,
            version,
            job_id,
            reason=str(exc),
            job_id=job_id,
            current_node="planner",
            task_queue=preflight_state["task_queue"],
        )
        return job_id
    _launch_runtime_task(
        _thread_id(project_id, version),
        run_orchestrator_task(
            job_id,
            project_id,
            version,
            requirement_text,
            model=model,
            preflight_checked=True,
        ),
    )
    return job_id


def get_job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found", "logs": [], "events": []})


def get_job_events(job_id: str) -> list[dict]:
    return list(_ensure_job(job_id)["events"])


def subscribe_job_events(job_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _ensure_job(job_id)["subscribers"].add(queue)
    return queue


def unsubscribe_job_events(job_id: str, queue: asyncio.Queue):
    if job_id in jobs:
        jobs[job_id]["subscribers"].discard(queue)


def list_projects():
    # Sync from disk to DB for projects that might have been created manually
    if PROJECTS_DIR.exists():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                metadata_db.upsert_project(d.name, d.name)
    
    # Pass runtime states to merge with DB states
    return metadata_db.list_projects(runtime_states=runtime_registry)


def create_project(project_id: str, name: Optional[str] = None, description: Optional[str] = None):
    (PROJECTS_DIR / project_id).mkdir(parents=True, exist_ok=True)
    metadata_db.upsert_project(project_id, name or project_id, description)
    
    # Initialize all experts as enabled by default (for backward compatibility)
    try:
        from registry.expert_registry import ExpertRegistry
        registry = ExpertRegistry.get_instance()
        for manifest in registry.get_all_manifests():
            metadata_db.upsert_project_expert(project_id, {
                "id": manifest.capability,
                "enabled": True,
                "description": manifest.description
            })
        print(f"[Project] Initialized {len(registry.get_all_manifests())} experts for project {project_id}")
    except RuntimeError:
        print(f"[Project] Warning: ExpertRegistry not initialized, cannot setup default experts")


def list_versions(project_id: str, page: int = 1, page_size: int = 10):
    # Sync versions from disk to DB if they don't exist
    proj_dir = PROJECTS_DIR / project_id
    if proj_dir.exists():
        for d in proj_dir.iterdir():
            if d.is_dir():
                if _is_project_internal_dir_name(d.name):
                    if metadata_db.get_version(project_id, d.name):
                        metadata_db.delete_version(project_id, d.name)
                    continue
                # We only sync if not in DB to avoid overwriting with generic data
                if not metadata_db.get_version(project_id, d.name):
                    metadata_db.upsert_version(project_id, d.name, "", "unknown")
    
    return metadata_db.list_versions(project_id, page, page_size)


def _delete_checkpoint_state(project_id: str, version: str):
    if not CHECKPOINT_DB_PATH.exists():
        return

    thread_id = _thread_id(project_id, version)
    conn = sqlite3.connect(CHECKPOINT_DB_PATH)
    try:
        conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        conn.commit()
    finally:
        conn.close()


def delete_version(project_id: str, version: str) -> bool:
    state = get_workflow_state(project_id, version)
    thread_id = _thread_id(project_id, version)
    has_active_runtime = thread_id in runtime_registry or bool(state and state.get("run_id"))
    if has_active_runtime and state and state.get("run_status") in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
        return False

    project_version_dir = PROJECTS_DIR / project_id / version
    if not project_version_dir.exists():
        return False

    if state and state.get("run_id"):
        jobs.pop(state["run_id"], None)
    _cancel_scheduled_tasks_for_version(project_id, version)
    metadata_db.cancel_scheduled_runs_for_version(
        project_id,
        version,
        statuses=["scheduled"],
        error="Version deleted before scheduled start.",
    )
    runtime_registry.pop(thread_id, None)
    runtime_tasks.pop(thread_id, None)
    _delete_checkpoint_state(project_id, version)
    metadata_db.delete_version(project_id, version)
    shutil.rmtree(project_version_dir, ignore_errors=True)
    return True


def get_artifacts_tree(project_id: str, version: str):
    artifacts = _load_artifacts_from_disk(project_id, version)
    try:
        sync_artifacts_from_disk(project_id, version, artifacts)
    except Exception as exc:
        print(f"[WARN] Failed to sync design artifact registry: {exc}")
    return artifacts


def get_version_logs(project_id: str, version: str) -> list:
    persisted_logs = get_run_log(project_id, version, BASE_DIR)
    current_state = get_workflow_state(project_id, version)
    run_id = (current_state or {}).get("run_id")
    live_logs = jobs.get(run_id, {}).get("logs", []) if run_id else []

    combined_logs = list(persisted_logs)
    seen = {run_log_dedupe_key(log) for log in combined_logs}
    for log in live_logs:
        key = run_log_dedupe_key(log)
        if key not in seen:
            seen.add(key)
            combined_logs.append(log)
    return _filter_stale_planner_expert_selection_wait_logs(combined_logs, current_state)


def _resolve_experts_dir() -> Path:
    if EXPERTS_DIR.exists():
        return EXPERTS_DIR
    return LEGACY_SUBAGENTS_DIR


def _resolve_expert_profile_path(expert_id: str) -> Path:
    experts_dir = _resolve_experts_dir()
    candidate_paths = [
        experts_dir / f"{expert_id}.expert.yaml",
        experts_dir / f"{expert_id}.agent.yaml",
    ]
    for candidate in candidate_paths:
        if candidate.exists():
            return candidate
    return candidate_paths[0]


def _get_version_bucket(file_path: Path) -> Path:
    relative = file_path.relative_to(BASE_DIR)
    bucket_name = "__".join(relative.parts)
    return EXPERT_CENTER_VERSIONS_DIR / bucket_name


def _list_file_versions(file_path: Path) -> list[dict]:
    versions = []
    versions_dir = _get_version_bucket(file_path)
    if not versions_dir.exists():
        return versions

    version_files = sorted(list(versions_dir.glob("*.v*")), key=os.path.getmtime, reverse=True)
    for version_file in version_files:
        try:
            name_parts = version_file.name.split(".v")
            versions.append(
                {
                    "version_id": name_parts[1],
                    "timestamp": name_parts[0],
                    "content": version_file.read_text(encoding="utf-8"),
                }
            )
        except Exception:
            pass
    return versions


def _write_versioned_file(file_path: Path, content: str, *, validate_yaml: bool = False) -> bool:
    if not file_path.exists():
        return False
    if validate_yaml:
        try:
            yaml.safe_load(content)
        except Exception:
            return False

    old_content = file_path.read_text(encoding="utf-8")
    versions_dir = _get_version_bucket(file_path)
    versions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    version_count = len(list(versions_dir.glob("*.v*"))) + 1
    (versions_dir / f"{timestamp}.v{version_count}").write_text(old_content, encoding="utf-8")
    file_path.write_text(content, encoding="utf-8")
    return True


def _reload_phase_and_registry_state() -> None:
    from config import get_phase_config

    get_phase_config().reload()
    try:
        ExpertRegistry.get_instance().reload()
    except RuntimeError:
        ExpertRegistry.initialize(BASE_DIR)


def get_phase_orchestration():
    from config import get_phase_config

    phase_config = get_phase_config()
    experts = []
    phase_map = phase_config.get_expert_phase_map()

    for expert in list_experts():
        if expert["id"] in SYSTEM_EXPERTS:
            continue
        experts.append(
            {
                "id": expert["id"],
                "name": expert["name"],
                "name_zh": expert.get("name_zh"),
                "name_en": expert.get("name_en"),
                "description": expert.get("description"),
                "phase": phase_map.get(expert["id"], ""),
            }
        )

    experts.sort(key=lambda item: item["id"])

    return {
        "phases": phase_config.get_phase_labels(lang="zh", executable_only=False),
        "experts": experts,
        "validation_errors": phase_config.validation_errors,
    }


def update_phase_orchestration(phases: list[dict]):
    from config import get_phase_config

    phase_config = get_phase_config()
    valid_experts = {expert["id"] for expert in list_experts() if expert["id"] not in SYSTEM_EXPERTS}
    phase_updates: list[dict] = []

    for phase in phases:
        phase_id = str(phase.get("id") or "").strip().upper()
        current_phase = phase_config.get_phase(phase_id)
        if current_phase is None:
            raise ValueError(f"Unknown phase '{phase_id}'.")
        order = int(phase.get("order", current_phase.order))
        experts = [str(item).strip() for item in (phase.get("experts") or []) if str(item).strip()]
        unknown = sorted({expert_id for expert_id in experts if expert_id not in valid_experts})
        if unknown:
            raise ValueError(
                f"Unknown experts for phase '{phase_id}': {', '.join(unknown)}",
            )
        phase_updates.append({"id": phase_id, "order": order, "experts": experts})

    phase_config.update_phase_configuration(phase_updates)
    _reload_phase_and_registry_state()
    return get_phase_orchestration()


def list_experts():
    experts_dir = _resolve_experts_dir()
    if not experts_dir.exists():
        return []

    experts = []
    for item in sorted(list(experts_dir.glob("*.expert.yaml")) + list(experts_dir.glob("*.agent.yaml"))):
        try:
            with open(item, "r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            expert_id = item.stem.replace(".expert", "").replace(".agent", "")
            name_zh, name_en = _resolve_localized_expert_names(expert_id, config)
            experts.append(
                {
                    "id": expert_id,
                    "name": config.get("name", expert_id),
                    "name_zh": name_zh or None,
                    "name_en": name_en or config.get("name", expert_id),
                    "description": config.get("description", ""),
                    "expertise": config.get("keywords", []),
                    "profile_path": str(item.relative_to(BASE_DIR)),
                    "skill_path": str((SKILLS_DIR / expert_id).relative_to(BASE_DIR)) if (SKILLS_DIR / expert_id).exists() else None,
                    "current_profile": item.read_text(encoding="utf-8"),
                    "versions": _list_file_versions(item),
                }
            )
        except Exception:
            pass
    return experts


def validate_expert_dependencies():
    try:
        registry = ExpertRegistry.get_instance()
    except RuntimeError:
        registry = ExpertRegistry.initialize(BASE_DIR)
    return registry.validate_dependency_graph(exclude_capabilities=SYSTEM_EXPERTS)


def get_expert(expert_id: str):
    profile_path = _resolve_expert_profile_path(expert_id)
    if not profile_path.exists():
        return None

    content = profile_path.read_text(encoding="utf-8")
    config = yaml.safe_load(content) or {}
    name_zh, name_en = _resolve_localized_expert_names(expert_id, config)
    return {
        "id": expert_id,
        "name": config.get("name", expert_id),
        "name_zh": name_zh or None,
        "name_en": name_en or config.get("name", expert_id),
        "description": config.get("description", ""),
        "expertise": config.get("keywords", []),
        "profile_path": str(profile_path.relative_to(BASE_DIR)),
        "skill_path": str((SKILLS_DIR / expert_id).relative_to(BASE_DIR)) if (SKILLS_DIR / expert_id).exists() else None,
        "current_profile": content,
        "versions": _list_file_versions(profile_path),
    }


def update_expert(expert_id: str, new_profile_yaml: str):
    profile_path = _resolve_expert_profile_path(expert_id)
    try:
        normalized_profile_yaml = _normalize_expert_profile_yaml(
            new_profile_yaml,
            expert_id=expert_id,
            existing_profile_path=profile_path,
        )
    except ValueError:
        return False
    return _write_versioned_file(profile_path, normalized_profile_yaml, validate_yaml=True)


# System experts that cannot be deleted
SYSTEM_EXPERTS = {"expert-creator"}


def create_expert(
    expert_id: str,
    name: str,
    description: str = "",
    *,
    name_zh: str = "",
    name_en: str = "",
    phase: str = "",
    request_id: str = "",
):
    """Create a new expert using the Expert Generator script.
    
    This function delegates to the expert-creator skill's generate_expert.py script
    for intelligent expert generation with LLM support.
    
    Args:
        phase: Target execution phase (e.g. "RULES"). Written to config/phases.yaml.
    """
    request_tag = request_id or uuid.uuid4().hex[:8]
    display_name = name_en or name_zh or name
    target_phase = phase or "ANALYSIS"
    print(
        f"[ExpertCreate:{request_tag}] Starting generation flow "
        f"expert_id='{expert_id}' display_name='{display_name}' target_phase='{target_phase}'."
    )
    result = None
    try:
        from skills.expert_creator.scripts.generate_expert import create_expert as generate_expert
        result = generate_expert(
            BASE_DIR,
            expert_id,
            display_name,
            description,
            use_llm=True,
            name_zh=name_zh,
            name_en=name_en,
            phase=target_phase,
            request_id=request_tag,
        )
    except Exception as e:
        print(f"[ExpertCreate:{request_tag}] Expert generation script failed: {e}. Using inline fallback.")

    if result:
        print(
            f"[ExpertCreate:{request_tag}] Expert asset generation succeeded with generated_id='{result['id']}'. "
            f"Updating phase orchestration."
        )
        update_phase_orchestration(
            [
                {
                    "id": item["id"],
                    "order": item.get("order"),
                    "experts": list(item.get("experts") or []) + ([result["id"]] if item["id"] == target_phase else []),
                }
                for item in get_phase_orchestration()["phases"]
            ]
        )
        print(f"[ExpertCreate:{request_tag}] Phase orchestration updated for expert '{result['id']}'.")
        return get_expert(result["id"])
    
    # Fallback: inline generation with rich structure
    print(f"[ExpertCreate:{request_tag}] Entering inline fallback generation path.")
    initial_id = "".join(ch for ch in expert_id if ch.isalnum() or ch == "-").strip("-").lower()
    if not initial_id:
        initial_id = "expert-" + str(uuid.uuid4())[:8]

    normalized_name = (name or name_en or name_zh or initial_id).strip()
    
    final_id = initial_id
    profile_path = _resolve_expert_profile_path(final_id)
    if profile_path.exists():
        final_id = f"{final_id}-{str(uuid.uuid4())[:4]}"
        profile_path = _resolve_expert_profile_path(final_id)

    experts_dir = _resolve_experts_dir()
    experts_dir.mkdir(parents=True, exist_ok=True)
    skill_dir = SKILLS_DIR / final_id
    (skill_dir / "assets" / "templates").mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)

    yaml_name = name_en or normalized_name or final_id.replace("-", " ").title()
    profile_content = f"""name: {json.dumps(yaml_name, ensure_ascii=False)}
name_en: {json.dumps(yaml_name, ensure_ascii=False)}
name_zh: {json.dumps(name_zh, ensure_ascii=False)}
capability: {final_id}
description: {json.dumps(description or normalized_name, ensure_ascii=False)}
version: 0.1.0
skills:
  - {final_id}
inputs:
  required:
    - requirements
    - existing_assets
    - output_root
  optional:
    - constraints
    - context
scheduling:
  phase: {target_phase}
  priority: 50
  dependencies: []
upstream_artifacts: {{}}
keywords: []
tools:
  allowed: ["list_files", "read_file_chunk", "grep_search", "write_file", "patch_file"]
outputs:
  expected: ["{final_id}-design.md"]
  evidence: ["{final_id}.json"]
metadata:
  boundary_contract:
    owns:
      - {final_id} domain design artifacts
    excludes:
      - full IR package
      - unrelated technical implementation design
    upstream_inputs: []
policies:
  asset_baseline_required: true
  evidence_required: true
  output_must_be_structured: true
  manual_override_forbidden: true
  descriptions_prefer_chinese: true
error_handling:
  on_missing_required_input: fail
  on_validation_failure: fail
  on_partial_generation: emit_evidence_and_fail
"""
    skill_content = f"""---
name: {normalized_name}
description: "{description or normalized_name}"
keywords: []
---

# {normalized_name}

## 工作流 (Workflow)

1. **需求分析**：读取需求基线，识别业务场景和设计需求。
2. **上下文收集**：使用读取工具从现有资产和参考文档中收集必要信息。
3. **设计生成**：基于收集到的证据生成领域设计产物。
4. **验证与修正**：回读已写入的内容，检查完整性和一致性，必要时修补。
5. **证据沉淀**：将设计依据写入 evidence 文件。

## 输出产物 (Output Artifacts)

| 产物路径 | 说明 |
|----------|------|
| `artifacts/{final_id}-design.md` | 主要设计文档 |
| `evidence/{final_id}.json` | 设计依据和决策证据 |

# ReAct 执行策略 (ReAct Strategy)

1. **研究 (Research)**：使用读取工具（list_files, read_file_chunk, grep_search）从需求文件中收集证据。
2. **编写 (Write)**：使用 `write_file` 生成草稿产物。
3. **验证 (Verify)**：使用 `read_file_chunk` 回读已写入的内容进行验证。
4. **修补 (Patch)**：基于验证结果或新发现，使用 `patch_file` 进行微调。
5. **完成 (Finalize)**：仅当所有预期产物正确写入并验证后，设置 done=true。

## ReAct 规则

1. 默认每次只输出一个下一步动作；只有在收集独立、低风险的读取证据时，才可使用 `actions` 返回最多 2 个只读动作。
2. 仅当收集到足够证据且已写入所有预期文件时才停止。
3. 保持 tool_input 简洁且为机器可读的 JSON 格式。
4. `actions` 只可包含 `read_file_chunk`、`extract_structure`、`grep_search` 等只读工具。
"""

    profile_path.write_text(profile_content, encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
    print(f"[ExpertCreate:{request_tag}] Applying fallback expert '{final_id}' to phase '{target_phase}'.")
    update_phase_orchestration(
        [
            {
                "id": item["id"],
                "order": item.get("order"),
                "experts": list(item.get("experts") or []) + ([final_id] if item["id"] == target_phase else []),
            }
            for item in get_phase_orchestration()["phases"]
        ]
    )
    print(f"[ExpertCreate:{request_tag}] Inline fallback completed with id='{final_id}'.")
    return get_expert(final_id)


def delete_expert(expert_id: str) -> bool:
    """Delete an expert by ID. System experts cannot be deleted."""
    # Protect system experts
    if expert_id in SYSTEM_EXPERTS:
        return False
    
    profile_path = _resolve_expert_profile_path(expert_id)
    skill_dir = SKILLS_DIR / expert_id

    if not profile_path.exists() and not skill_dir.exists():
        return False

    if profile_path.exists():
        profile_path.unlink()
    if skill_dir.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)
    update_phase_orchestration(
        [
            {
                "id": item["id"],
                "order": item.get("order"),
                "experts": [item_id for item_id in (item.get("experts") or []) if item_id != expert_id],
            }
            for item in get_phase_orchestration()["phases"]
        ]
    )
    return True


def _build_skill_children(expert_id: str, path: Path) -> list[dict]:
    nodes = []
    for child in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
        relative_path = str(child.relative_to(BASE_DIR)).replace("\\", "/")
        node_type = "file" if child.is_file() else "folder"
        children = _build_skill_children(expert_id, child) if child.is_dir() else []
        nodes.append(
            {
                "id": relative_path,
                "name": child.name,
                "path": relative_path,
                "node_type": node_type,
                "expert_id": expert_id,
                "children": children,
            }
        )
    return nodes


def get_expert_center_tree():
    tree = []
    for expert in list_experts():
        expert_id = expert["id"]
        children = [
            {
                "id": f"{expert_id}:profile",
                "name": "Expert Profile",
                "path": expert["profile_path"].replace("\\", "/"),
                "node_type": "file",
                "expert_id": expert_id,
                "children": [],
            }
        ]

        skill_dir = SKILLS_DIR / expert_id
        if skill_dir.exists():
            children.append(
                {
                    "id": f"{expert_id}:skills",
                    "name": "Skill Files",
                    "path": str(skill_dir.relative_to(BASE_DIR)).replace("\\", "/"),
                    "node_type": "folder",
                    "expert_id": expert_id,
                    "children": _build_skill_children(expert_id, skill_dir),
                }
            )

        tree.append(
            {
                "id": expert_id,
                "name": expert["name"],
                "path": expert_id,
                "node_type": "expert",
                "expert_id": expert_id,
                "children": children,
            }
        )
    return tree


def get_file_content(relative_path: str):
    normalized_path = (relative_path or "").replace("\\", "/").lstrip("/")
    file_path = (BASE_DIR / normalized_path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return None
    if BASE_DIR.resolve() not in file_path.parents and file_path != BASE_DIR.resolve():
        return None

    return {
        "path": normalized_path,
        "name": file_path.name,
        "content": file_path.read_text(encoding="utf-8"),
        "versions": _list_file_versions(file_path),
    }


def update_file_content(relative_path: str, content: str):
    normalized_path = (relative_path or "").replace("\\", "/").lstrip("/")
    file_path = (BASE_DIR / normalized_path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return False
    if BASE_DIR.resolve() not in file_path.parents and file_path != BASE_DIR.resolve():
        return False

    validate_yaml = file_path.suffix.lower() in {".yaml", ".yml"}
    return _write_versioned_file(file_path, content, validate_yaml=validate_yaml)


def delete_file(relative_path: str) -> bool:
    """Delete a file from the expert center.

    Only allows deleting files in:
    - skills/*/assets/templates/
    - skills/*/references/
    - skills/*/scripts/

    Protected files (cannot be deleted):
    - experts/*.expert.yaml (profile files)
    - skills/*/SKILL.md
    """
    normalized_path = (relative_path or "").replace("\\", "/").lstrip("/")
    file_path = (BASE_DIR / normalized_path).resolve()

    # Security check: must be within BASE_DIR
    if BASE_DIR.resolve() not in file_path.parents:
        return False

    if not file_path.exists() or not file_path.is_file():
        return False

    # Check if path is in allowed directories
    path_str = normalized_path.lower()
    allowed_patterns = [
        "/assets/templates/",
        "/references/",
        "/scripts/",
    ]

    is_allowed = any(pattern in path_str for pattern in allowed_patterns)

    # Protect profile and SKILL.md files
    protected_patterns = [
        ".expert.yaml",
        ".agent.yaml",
        "/skill.md",
    ]
    is_protected = any(pattern in path_str for pattern in protected_patterns)

    if not is_allowed or is_protected:
        return False

    try:
        file_path.unlink()
        return True
    except Exception:
        return False


def list_agents():
    experts = list_experts()
    return [
        {
            "id": expert["id"],
            "name": expert["name"],
            "description": expert["description"],
            "config_path": expert["profile_path"],
            "skills": [expert["id"]],
            "current_config": expert["current_profile"],
            "versions": expert["versions"],
        }
        for expert in experts
    ]


def get_agent(agent_id: str):
    expert = get_expert(agent_id)
    if not expert:
        return None
    return {
        "id": expert["id"],
        "name": expert["name"],
        "description": expert["description"],
        "config_path": expert["profile_path"],
        "current_config": expert["current_profile"],
        "versions": expert["versions"],
        "skills": [expert["id"]],
    }


def update_agent(agent_id: str, new_config_yaml: str):
    return update_expert(agent_id, new_config_yaml)


def list_skills():
    if not SKILLS_DIR.exists():
        return []
    skills = []
    for item in SKILLS_DIR.iterdir():
        if item.is_dir() and (item / "SKILL.md").exists():
            name = item.name
            try:
                content = (item / "SKILL.md").read_text(encoding="utf-8")
                if content.startswith("---"):
                    fm = yaml.safe_load(content.split("---")[1])
                    name = fm.get("name", name)
            except Exception:
                pass
            skills.append(
                {
                    "id": item.name,
                    "name": name,
                    "path": str(item.relative_to(BASE_DIR)),
                    "templates": [t.name for t in (item / "assets" / "templates").iterdir() if t.is_file()]
                    if (item / "assets" / "templates").exists()
                    else [],
                }
            )
    return skills


def get_template(skill_id: str, template_name: str):
    tpl_path = SKILLS_DIR / skill_id / "assets" / "templates" / template_name
    if not tpl_path.exists():
        return None
    content = tpl_path.read_text(encoding="utf-8")
    versions = []
    versions_dir = tpl_path.parent / ".versions" / template_name
    if versions_dir.exists():
        v_files = sorted(list(versions_dir.glob("*.v*")), key=os.path.getmtime, reverse=True)
        for v_file in v_files:
            try:
                name_parts = v_file.name.split(".v")
                versions.append(
                    {
                        "version_id": name_parts[1],
                        "timestamp": name_parts[0],
                        "content": v_file.read_text(encoding="utf-8"),
                    }
                )
            except Exception:
                pass
    return {"id": template_name, "name": template_name, "skill_id": skill_id, "current_content": content, "versions": versions}


def update_template(skill_id: str, template_name: str, new_content: str):
    tpl_path = SKILLS_DIR / skill_id / "assets" / "templates" / template_name
    if not tpl_path.exists():
        tpl_path.parent.mkdir(parents=True, exist_ok=True)
        old_content = ""
    else:
        old_content = tpl_path.read_text(encoding="utf-8")
    if old_content:
        versions_dir = tpl_path.parent / ".versions" / template_name
        versions_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        v_count = len(list(versions_dir.glob("*.v*"))) + 1
        (versions_dir / f"{timestamp}.v{v_count}").write_text(old_content, encoding="utf-8")
    tpl_path.write_text(new_content, encoding="utf-8")
    return True


def list_system_tools() -> list:
    """List all system built-in tools from the tool registry."""
    registry_path = BASE_DIR / "skills" / "expert-creator" / "assets" / "TOOL_REGISTRY.yaml"
    if not registry_path.exists():
        return []
    
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        tools = data.get("tools", [])
        
        # Add script path for each tool
        tools_dir = BASE_DIR / "api_server" / "graphs" / "tools"
        for tool in tools:
            tool_name = tool.get("name", "")
            script_file = tools_dir / f"{tool_name}.py"
            if script_file.exists():
                tool["script_path"] = f"api_server/graphs/tools/{tool_name}.py"

        if not any((tool or {}).get("name") == "validate_artifacts" for tool in tools):
            fallback_tool = {
                "name": "validate_artifacts",
                "category": "validation",
                "description_zh": "校验本次运行产物的完整性、Markdown 结构、Mermaid 烟测和结构化文件合法性",
                "description_en": "Validate generated artifacts for completeness, markdown structure, Mermaid smoke checks, and structured file validity",
                "input_schema": {
                    "root_dir": {"type": "string", "required": True},
                    "target_files": {"type": "array", "required": False},
                },
                "output_schema": {
                    "findings_path": {"type": "string"},
                    "summary": {"type": "object"},
                    "findings": {"type": "array"},
                },
                "recommended_for": ["validator"],
            }
            script_file = tools_dir / "validate_artifacts.py"
            if script_file.exists():
                fallback_tool["script_path"] = "api_server/graphs/tools/validate_artifacts.py"
            tools.append(fallback_tool)

        if not any((tool or {}).get("name") == "append_file" for tool in tools):
            fallback_tool = {
                "name": "append_file",
                "category": "file_operations",
                "description_zh": "向文件末尾追加内容，不覆盖已有内容",
                "description_en": "Append content to the end of a file without overwriting existing text",
                "input_schema": {
                    "root_dir": {"type": "string", "required": True},
                    "path": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True},
                },
                "output_schema": {
                    "path": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "appended_bytes": {"type": "integer"},
                },
            }
            script_file = tools_dir / "append_file.py"
            if script_file.exists():
                fallback_tool["script_path"] = "api_server/graphs/tools/append_file.py"
            tools.append(fallback_tool)

        if not any((tool or {}).get("name") == "upsert_markdown_sections" for tool in tools):
            fallback_tool = {
                "name": "upsert_markdown_sections",
                "category": "file_operations",
                "description_zh": "按 Markdown 标题增量更新章节，支持替换、缺失追加和近似重复跳过",
                "description_en": "Upsert markdown sections by heading with replace, append-if-missing, and near-duplicate skip modes",
                "input_schema": {
                    "root_dir": {"type": "string", "required": True},
                    "path": {"type": "string", "required": True},
                    "sections": {"type": "array", "required": True},
                    "dedupe_strategy": {"type": "string", "required": False},
                    "similarity_threshold": {"type": "number", "required": False},
                },
                "output_schema": {
                    "path": {"type": "string"},
                    "inserted_sections": {"type": "integer"},
                    "replaced_sections": {"type": "integer"},
                    "skipped_sections": {"type": "integer"},
                },
            }
            script_file = tools_dir / "upsert_markdown_sections.py"
            if script_file.exists():
                fallback_tool["script_path"] = "api_server/graphs/tools/upsert_markdown_sections.py"
            tools.append(fallback_tool)
        
        return tools
    except Exception as e:
        print(f"Error loading tools: {e}")
        return []


def get_project_assets_summary(project_id: str) -> dict:
    """Get a high-level summary of assets for a project before deletion."""
    project_dir = PROJECTS_DIR / project_id
    versions_summary = []
    total_files = 0
    total_size = 0

    if project_dir.exists():
        for v_dir in project_dir.iterdir():
            if v_dir.is_dir() and not _is_project_internal_dir_name(v_dir.name):
                v_files = 0
                v_size = 0
                for p in v_dir.rglob("*"):
                    if p.is_file():
                        v_files += 1
                        v_size += p.stat().st_size
                
                versions_summary.append({
                    "version": v_dir.name,
                    "file_count": v_files,
                    "size_mb": round(v_size / (1024 * 1024), 2),
                    "has_baseline": (v_dir / "baseline").exists(),
                    "has_artifacts": (v_dir / "artifacts").exists(),
                    "has_logs": (v_dir / "logs").exists(),
                })
                total_files += v_files
                total_size += v_size

    # Also check DB configs
    configs = {
        "repositories": metadata_db.list_repositories(project_id),
        "databases": metadata_db.list_databases(project_id),
        "knowledge_bases": metadata_db.list_knowledge_bases(project_id),
        "models": metadata_db.list_project_models(project_id),
    }

    return {
        "exists": project_dir.exists(),
        "project_id": project_id,
        "versions": versions_summary,
        "total_versions": len(versions_summary),
        "total_files": total_files,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "configs": {
            "repositories_count": len(configs["repositories"]),
            "databases_count": len(configs["databases"]),
            "knowledge_bases_count": len(configs["knowledge_bases"]),
            "models_count": len(configs["models"]),
        }
    }


def delete_project(project_id: str) -> bool:
    """Delete an entire project and all its assets."""
    # 1. Check if any version is running
    versions_data = list_versions(project_id)
    versions = versions_data.get("versions", [])
    
    for v in versions:
        version_id = v["version_id"]
        state = get_workflow_state(project_id, version_id)
        if state and state.get("run_status") in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
            # Cannot delete project if any version is active
            return False
            
    # 2. Clean up runtime state for all versions
    for v in versions:
        version_id = v["version_id"]
        thread_id = _thread_id(project_id, version_id)
        state = get_workflow_state(project_id, version_id)
        if state and state.get("run_id"):
            jobs.pop(state["run_id"], None)
        runtime_registry.pop(thread_id, None)
        runtime_tasks.pop(thread_id, None)
        _delete_checkpoint_state(project_id, version_id)

    # 3. Delete from database
    metadata_db.delete_project(project_id)
    
    # 4. Delete project directory
    project_dir = PROJECTS_DIR / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)
        
    return True


def get_tool_code(tool_name: str) -> str | None:
    """Get the implementation code of a specific tool."""
    tools_dir = BASE_DIR / "api_server" / "graphs" / "tools"
    script_file = tools_dir / f"{tool_name}.py"
    
    if not script_file.exists():
        return None
    
    try:
        return script_file.read_text(encoding="utf-8")
    except Exception:
        return None
