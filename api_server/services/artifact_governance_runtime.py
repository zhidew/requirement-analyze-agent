from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from services import artifact_dependency_service
from services.design_artifact_service import sync_file_artifact
from services.db_service import metadata_db


BLOCKING_ARTIFACT_STATUSES = {
    "reflection_failed",
    "system_check_failed",
    "blocked_pending_decision",
    "content_missing",
    "content_drifted",
}
WARNING_ARTIFACT_STATUSES = {
    "auto_accepted",
    "reflection_warning",
    "needs_revalidation",
    "needs_regeneration",
    "user_disputed",
    "revision_requested",
}


def _changed_artifact_names(before: Dict[str, Any] | None, after: Dict[str, Any] | None) -> List[str]:
    before = before or {}
    after = after or {}
    changed = []
    for artifact_name in sorted(after):
        if artifact_name not in before or before.get(artifact_name) != after.get(artifact_name):
            changed.append(artifact_name)
    return changed


def _count_by_status(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        status = str(item.get(key) or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _summarize_artifact(artifact: Dict[str, Any], graph: Dict[str, Any]) -> Dict[str, Any]:
    from services import context_consistency_service

    artifact_id = artifact["artifact_id"]
    reflection = artifact.get("reflection") or metadata_db.get_reflection_report_for_artifact(artifact_id)
    consistency = artifact.get("consistency") or context_consistency_service.get_consistency_report_for_artifact(artifact_id) or {}
    conflicts = consistency.get("conflicts") or []
    incoming_edges = [
        edge for edge in graph.get("edges", []) if edge.get("downstream_artifact_id") == artifact_id
    ]
    outgoing_edges = [
        edge for edge in graph.get("edges", []) if edge.get("upstream_artifact_id") == artifact_id
    ]
    outgoing_impacts = artifact.get("impact_records") or metadata_db.list_artifact_impact_records(
        project_id=artifact["project_id"],
        version_id=artifact["version_id"],
        source_artifact_id=artifact_id,
    )
    incoming_impacts = artifact.get("incoming_impacts") or metadata_db.list_artifact_impact_records(
        project_id=artifact["project_id"],
        version_id=artifact["version_id"],
        impacted_artifact_id=artifact_id,
    )

    conflict_counts = _count_by_status(conflicts, "severity")
    impact_counts = _count_by_status(outgoing_impacts, "impact_status")
    artifact_status = str(artifact.get("status") or "unknown")
    reflection_status = str((reflection or {}).get("status") or "missing")
    consistency_status = str((consistency or {}).get("status") or "missing")

    review_status = "ready_for_review"
    if artifact_status in BLOCKING_ARTIFACT_STATUSES or consistency_status == "failed" or reflection_status == "blocking":
        review_status = "blocked"
    elif (
        artifact_status in WARNING_ARTIFACT_STATUSES
        or consistency_status in {"warning", "missing"}
        or reflection_status in {"warning", "missing"}
        or conflict_counts.get("warning", 0)
        or incoming_impacts
        or outgoing_impacts
    ):
        review_status = "needs_review"

    return {
        "artifact_id": artifact_id,
        "file_name": artifact.get("file_name"),
        "file_path": artifact.get("file_path"),
        "expert_id": artifact.get("expert_id"),
        "artifact_version": artifact.get("artifact_version"),
        "status": artifact_status,
        "review_status": review_status,
        "summary": artifact.get("summary") or "",
        "reflection": {
            "report_id": (reflection or {}).get("report_id"),
            "status": reflection_status,
            "confidence": (reflection or {}).get("confidence"),
            "blocks_downstream": bool((reflection or {}).get("blocks_downstream")),
            "issue_count": len((reflection or {}).get("issues") or []),
            "required_action_count": len((reflection or {}).get("required_actions") or []),
        },
        "consistency": {
            "report_id": (consistency or {}).get("report_id"),
            "status": consistency_status,
            "conflict_count": len(conflicts),
            "blocking_conflict_count": conflict_counts.get("blocking", 0),
            "warning_conflict_count": conflict_counts.get("warning", 0),
        },
        "dependencies": {
            "upstream_count": len(incoming_edges),
            "downstream_count": len(outgoing_edges),
        },
        "impacts": {
            "outgoing_count": len(outgoing_impacts),
            "incoming_count": len(incoming_impacts),
            "outgoing_status_counts": impact_counts,
        },
    }


def _runtime_status(items: List[Dict[str, Any]], errors: List[Dict[str, str]]) -> str:
    if errors:
        return "needs_review"
    return "auto_accepted"


def finalize_expert_artifact_outputs(
    *,
    project_id: str,
    version_id: str,
    run_id: Optional[str],
    expert_id: str,
    before: Dict[str, Any] | None,
    after: Dict[str, Any] | None,
) -> Dict[str, Any]:
    changed_artifact_names = _changed_artifact_names(before, after)
    artifacts: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for artifact_name in changed_artifact_names:
        try:
            artifact = sync_file_artifact(
                project_id=project_id,
                version_id=version_id,
                run_id=run_id,
                expert_id=expert_id,
                file_name=artifact_name,
            )
            if artifact:
                artifacts.append(artifact)
        except Exception as exc:
            errors.append({"file_name": artifact_name, "error": str(exc)})

    graph = artifact_dependency_service.build_artifact_dependency_graph(project_id, version_id)
    items = [_summarize_artifact(artifact, graph) for artifact in artifacts]
    status = _runtime_status(items, errors)

    for artifact in artifacts:
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=artifact["artifact_id"],
            event_type="runtime_governance_finalized",
            payload={
                "run_id": run_id,
                "expert_id": expert_id,
                "runtime_status": status,
                "dependency_edge_count": len(graph.get("edges") or []),
            },
        )

    return {
        "project_id": project_id,
        "version_id": version_id,
        "run_id": run_id,
        "expert_id": expert_id,
        "status": status,
        "changed_artifact_names": changed_artifact_names,
        "artifact_count": len(items),
        "items": items,
        "errors": errors,
        "dependency_graph": {
            "refreshed": True,
            "node_count": len(graph.get("nodes") or []),
            "edge_count": len(graph.get("edges") or []),
        },
    }
