from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from services.db_service import metadata_db


def _make_evidence_from_conflict(conflict: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = []
    for item in conflict.get("evidence_refs") or []:
        if isinstance(item, dict):
            evidence.append(item)
    return evidence


def create_decision_log(
    *,
    project_id: str,
    version_id: str,
    scope: str,
    conflict_ids: List[str],
    decision: str,
    basis: str,
    authority: str,
    applies_to: Optional[List[str]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    evidence_refs: List[Dict[str, Any]] = []
    for conflict_id in conflict_ids:
        conflict = metadata_db.get_context_conflict(conflict_id)
        if conflict:
            evidence_refs.append(
                {
                    "evidence_source": "context_conflict",
                    "source_id": conflict["conflict_id"],
                    "source_version": conflict.get("version_id"),
                    "retrieved_at": conflict.get("updated_at") or conflict.get("created_at"),
                    "evidence_excerpt": conflict.get("summary"),
                    "confidence": 0.9 if conflict.get("severity") == "blocking" else 0.7,
                    "as_is_or_to_be": conflict.get("semantic"),
                    "context_conflict": conflict,
                }
            )
            evidence_refs.extend(_make_evidence_from_conflict(conflict))

    decision_log = metadata_db.create_decision_log(
        decision_id=str(uuid.uuid4()),
        project_id=project_id,
        version_id=version_id,
        scope=scope,
        conflict_ids=conflict_ids,
        decision=decision,
        basis=basis,
        authority=authority,
        applies_to=applies_to or [],
        evidence_refs=evidence_refs,
        created_by=created_by,
    )

    for conflict_id in conflict_ids:
        metadata_db.update_context_conflict(conflict_id, status="resolved", decision_id=decision_log["decision_id"])

    sessions = metadata_db.list_revision_sessions(project_id=project_id, version_id=version_id)
    for session in sessions:
        if session.get("conflict_report_id") in conflict_ids:
            metadata_db.update_revision_session(
                session["revision_session_id"],
                status="decision_recorded",
                decision_id=decision_log["decision_id"],
            )

    for artifact_id in applies_to or []:
        artifact = metadata_db.get_design_artifact(artifact_id)
        if not artifact:
            continue
        decision_refs = list(artifact.get("decision_refs") or [])
        decision_refs.append(
            {
                "decision_id": decision_log["decision_id"],
                "decision": decision,
                "basis": basis,
                "authority": authority,
                "scope": scope,
            }
        )
        metadata_db.update_design_artifact(artifact_id, decision_refs=decision_refs)
        try:
            from services import impact_analysis_service

            impact_analysis_service.analyze_revision_impact(
                artifact_id,
                {"change_type": "decision_change", "decision_id": decision_log["decision_id"]},
                trigger_type="decision_recorded",
                trigger_ref_id=decision_log["decision_id"],
                has_blocking_conflict=decision == "pending_confirmation",
            )
        except Exception:
            pass

    return decision_log


def get_decision_log(decision_id: str) -> Optional[Dict[str, Any]]:
    return metadata_db.get_decision_log(decision_id)


def list_decision_logs(project_id: str, version_id: str, scope: Optional[str] = None) -> List[Dict[str, Any]]:
    return metadata_db.list_decision_logs(project_id=project_id, version_id=version_id, scope=scope)


def resolve_conflict_with_decision(
    *,
    conflict_id: str,
    decision: str,
    basis: str,
    authority: str,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    conflict = metadata_db.get_context_conflict(conflict_id)
    if not conflict:
        raise ValueError("Context conflict not found.")
    return create_decision_log(
        project_id=conflict["project_id"],
        version_id=conflict["version_id"],
        scope=conflict.get("conflict_type") or "artifact",
        conflict_ids=[conflict_id],
        decision=decision,
        basis=basis,
        authority=authority,
        applies_to=[conflict.get("artifact_id")] if conflict.get("artifact_id") else [],
        created_by=created_by,
    )
