from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.db_service import metadata_db
from subgraphs.context_conflict_checker import classify_revision_feedback
from subgraphs.expert_reflection import record_reflection_observation


BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"
CONTENT_DIRS = ("artifacts", "release", "evidence", "logs", "baseline")
PLANNER_EXPERT_IDS = {"planner"}
PLANNER_ARTIFACT_NAMES = {
    "requirements.json",
    "input-requirements.md",
    "original-requirements.md",
    "raw-requirements.md",
    "clarified-requirements.md",
    "planner-reasoning.md",
    "planner-output.md",
}
SYSTEM_REVIEW_EXPERT_IDS = {"planner", "validator"}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_anchor_text(value: str) -> str:
    return " ".join(value.split())


def _has_meaningful_text_change(left: str, right: str) -> bool:
    return left != right


def _is_mutable_revision_artifact(artifact: Dict[str, Any]) -> bool:
    return bool(artifact.get("parent_artifact_id")) and artifact.get("status") in {
        "ready_for_review",
        "reflection_warning",
        "revision_requested",
        "user_disputed",
    }


def _record_downstream_regeneration_plan(
    *,
    accepted_artifact: Dict[str, Any],
    impact_records: List[Dict[str, Any]],
    reviewer_note: str,
) -> List[Dict[str, Any]]:
    plans: List[Dict[str, Any]] = []
    for record in impact_records:
        if record.get("impact_status") != "needs_regeneration":
            continue
        impacted = metadata_db.get_design_artifact(record["impacted_artifact_id"])
        if not impacted:
            plans.append(
                {
                    "impact_id": record.get("impact_id"),
                    "artifact_id": record.get("impacted_artifact_id"),
                    "status": "skipped",
                    "reason": "Impacted artifact not found.",
                }
            )
            continue
        try:
            from services import orchestrator_service

            plan = orchestrator_service.trigger_downstream_regeneration(
                project_id=accepted_artifact["project_id"],
                version=accepted_artifact["version_id"],
                target_expert_id=impacted["expert_id"],
                source_artifact_id=record["source_artifact_id"],
                accepted_artifact_id=accepted_artifact["artifact_id"],
                impacted_artifact_id=impacted["artifact_id"],
                impact_id=record["impact_id"],
                feedback=reviewer_note or "Accepted upstream artifact revision; regenerate impacted downstream design artifact.",
            )
        except Exception as exc:
            plan = {
                "status": "failed",
                "impact_id": record.get("impact_id"),
                "artifact_id": impacted["artifact_id"],
                "target_expert_id": impacted["expert_id"],
                "reason": str(exc),
            }
        plans.append(plan)
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=impacted["artifact_id"],
            event_type="downstream_regeneration_requested",
            payload={
                **plan,
                "source_artifact_id": record["source_artifact_id"],
                "accepted_artifact_id": accepted_artifact["artifact_id"],
            },
        )
    if plans:
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=accepted_artifact["artifact_id"],
            event_type="downstream_regeneration_planned",
            payload={"plans": plans},
        )
    return plans


def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-") or "artifact"


def _project_version_root(project_id: str, version_id: str) -> Path:
    return PROJECTS_DIR / project_id / version_id


def _resolve_artifact_path(project_id: str, version_id: str, file_name: str) -> Optional[Path]:
    root = _project_version_root(project_id, version_id)
    for dirname in CONTENT_DIRS:
        candidate = root / dirname / file_name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _relative_to_version(project_id: str, version_id: str, path: Path) -> str:
    return str(path.relative_to(_project_version_root(project_id, version_id))).replace("\\", "/")


def _read_artifact_content(project_id: str, version_id: str, file_path: str) -> str:
    path = (_project_version_root(project_id, version_id) / file_path).resolve()
    root = _project_version_root(project_id, version_id).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Artifact path escapes project version root.")
    return path.read_text(encoding="utf-8")


def _artifact_type_for_file(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower().lstrip(".")
    return suffix or "text"


def _status_from_reflection(reflection: Dict[str, Any], *, default_status: str = "auto_accepted") -> str:
    if default_status == "auto_accepted":
        return default_status
    status = reflection.get("status")
    if status == "blocking":
        return "reflection_failed"
    if status == "warning" and default_status != "auto_accepted":
        return "reflection_warning"
    return default_status


def _is_planner_artifact(expert_id: str, file_name: str) -> bool:
    lower = file_name.lower()
    return expert_id in PLANNER_EXPERT_IDS or lower in PLANNER_ARTIFACT_NAMES or lower.startswith("planner-")


def _is_system_review_artifact(expert_id: str, file_name: str) -> bool:
    lower = file_name.lower()
    return (
        expert_id in SYSTEM_REVIEW_EXPERT_IDS
        or _is_planner_artifact(expert_id, file_name)
        or lower.startswith("validator")
        or lower.startswith("validation")
    )


def _build_summary(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().strip("#").strip()
        if stripped:
            return stripped[:180]
    return "Empty artifact content."


def sync_file_artifact(
    *,
    project_id: str,
    version_id: str,
    run_id: Optional[str],
    expert_id: str,
    file_name: str,
    dependency_refs: Optional[List[str]] = None,
    source_refs: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    if _is_system_review_artifact(expert_id, file_name):
        return None
    path = _resolve_artifact_path(project_id, version_id, file_name)
    if not path:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        content = "[Binary]"

    relative_path = _relative_to_version(project_id, version_id, path)
    content_hash = _sha256_text(content)
    existing = metadata_db.get_latest_design_artifact_by_file(project_id, version_id, relative_path)
    if existing and existing.get("content_hash") == content_hash:
        return existing

    parent_artifact_id = existing.get("artifact_id") if existing else None
    artifact_version = int((existing or {}).get("artifact_version") or 0) + 1
    reflection = record_reflection_observation(content, dependency_refs=dependency_refs or [])
    artifact = metadata_db.create_design_artifact(
        artifact_id=str(uuid.uuid4()),
        project_id=project_id,
        version_id=version_id,
        run_id=run_id,
        expert_id=expert_id,
        artifact_type=_artifact_type_for_file(file_name),
        artifact_version=artifact_version,
        parent_artifact_id=parent_artifact_id,
        status=_status_from_reflection(reflection),
        title=file_name,
        file_name=file_name,
        file_path=relative_path,
        content_hash=content_hash,
        summary=_build_summary(content),
        source_refs=source_refs or [{"type": "file", "path": relative_path, "content_hash": content_hash}],
        dependency_refs=dependency_refs or [],
    )
    report = metadata_db.create_expert_reflection_report(
        report_id=str(uuid.uuid4()),
        artifact_id=artifact["artifact_id"],
        expert_id=expert_id,
        status=reflection["status"],
        confidence=reflection["confidence"],
        checks=reflection["checks"],
        issues=reflection["issues"],
        assumptions=reflection["assumptions"],
        open_questions=reflection["open_questions"],
        required_actions=reflection["required_actions"],
        blocks_downstream=reflection["blocks_downstream"],
    )
    metadata_db.update_design_artifact(
        artifact["artifact_id"],
        reflection_report_id=report["report_id"],
        status=_status_from_reflection(reflection),
    )
    metadata_db.append_design_artifact_event(
        event_id=str(uuid.uuid4()),
        artifact_id=artifact["artifact_id"],
        event_type="registered",
        payload={"file_path": relative_path, "content_hash": content_hash, "run_id": run_id},
    )
    if parent_artifact_id:
        metadata_db.update_design_artifact(parent_artifact_id, status="superseded")
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=parent_artifact_id,
            event_type="superseded",
            payload={"superseded_by": artifact["artifact_id"]},
        )
    try:
        from services import context_consistency_service

        context_consistency_service.run_consistency_check(artifact["artifact_id"])
    except Exception as exc:
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=artifact["artifact_id"],
            event_type="consistency_check_failed",
            payload={"error": str(exc)},
        )
    return get_design_artifact(artifact["artifact_id"])


def sync_artifacts_from_disk(project_id: str, version_id: str, artifacts: Dict[str, str], *, run_id: Optional[str] = None) -> None:
    for file_name in sorted(artifacts):
        expert_id = infer_expert_id_for_file(file_name)
        sync_file_artifact(
            project_id=project_id,
            version_id=version_id,
            run_id=run_id,
            expert_id=expert_id,
            file_name=file_name,
        )


def infer_expert_id_for_file(file_name: str) -> str:
    lower = file_name.lower()
    if lower in PLANNER_ARTIFACT_NAMES or lower.startswith("planner-"):
        return "planner"
    if lower.startswith("requirement-clarification") or lower.startswith("scope-and-assumptions") or lower.startswith("glossary"):
        return "requirement-clarification"
    if lower.startswith("business-rules") or lower.startswith("decision-tables") or lower.startswith("rule-parameters"):
        return "rules-management"
    if lower.startswith("document-operations") or lower.startswith("field-requirements") or lower.startswith("operation-permissions"):
        return "document-operation"
    if lower.startswith("process-requirements") or lower.startswith("state-transition") or lower.startswith("exception-handling"):
        return "process-control"
    if lower.startswith("integration-requirements") or lower.startswith("external-system-matrix") or lower.startswith("data-exchange-events"):
        return "integration-requirements"
    if lower.startswith("it-requirements") or lower.startswith("requirement-traceability") or lower.startswith("acceptance-criteria") or lower.startswith("open-questions"):
        return "ir-assembler"
    if lower.startswith("validator") or lower.startswith("validation"):
        return "validator"
    return "unknown"


def list_design_artifacts(project_id: str, version_id: str, expert_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return [hydrate_artifact(row) for row in metadata_db.list_design_artifacts(project_id, version_id, expert_id=expert_id)]


def get_design_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    row = metadata_db.get_design_artifact(artifact_id)
    return hydrate_artifact(row) if row else None


def accept_design_artifact(artifact_id: str, *, reviewer_note: str = "", accepted_by: str = "user") -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")
    should_propagate_revision = (
        artifact.get("status") != "accepted"
        and bool(artifact.get("parent_artifact_id"))
        and artifact.get("status") in {"ready_for_review", "reflection_warning"}
    )
    updated = metadata_db.update_design_artifact(artifact_id, status="accepted")
    metadata_db.append_design_artifact_event(
        event_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        event_type="accepted",
        payload={"reviewer_note": reviewer_note or "", "accepted_by": accepted_by or "user"},
    )
    if should_propagate_revision:
        _propagate_accepted_revision_impact(artifact, reviewer_note=reviewer_note, accepted_by=accepted_by)
    return hydrate_artifact(updated or artifact)


def _propagate_accepted_revision_impact(artifact: Dict[str, Any], *, reviewer_note: str = "", accepted_by: str = "user") -> None:
    parent_artifact_id = artifact.get("parent_artifact_id")
    if not parent_artifact_id:
        return
    try:
        from services import impact_analysis_service

        impact_result = impact_analysis_service.analyze_revision_impact(
            parent_artifact_id,
            {
                "change_type": "schema_change" if artifact.get("artifact_type") == "sql" else "artifact_revision",
                "accepted_artifact_id": artifact["artifact_id"],
                "reviewer_note": reviewer_note or "",
                "accepted_by": accepted_by or "user",
            },
            trigger_type="revision_accepted",
            trigger_ref_id=artifact["artifact_id"],
        )
        sessions = metadata_db.list_revision_sessions(
            project_id=artifact["project_id"],
            version_id=artifact["version_id"],
        )
        affected_artifacts = [
            {
                "artifact_id": record["impacted_artifact_id"],
                "impact_status": record["impact_status"],
                "impact_id": record["impact_id"],
            }
            for record in impact_result.get("impact_records", [])
        ]
        regeneration_plans = _record_downstream_regeneration_plan(
            accepted_artifact=artifact,
            impact_records=impact_result.get("impact_records", []),
            reviewer_note=reviewer_note,
        )
        for session in sessions:
            if session.get("created_artifact_id") == artifact["artifact_id"]:
                metadata_db.update_revision_session(
                    session["revision_session_id"],
                    status="revision_accepted",
                    affected_artifacts=affected_artifacts,
                )
                metadata_db.append_revision_session_event(
                    event_id=str(uuid.uuid4()),
                    revision_session_id=session["revision_session_id"],
                    event_type="revision_accepted",
                    payload={
                        "accepted_artifact_id": artifact["artifact_id"],
                        "affected_artifacts": affected_artifacts,
                        "regeneration_plans": regeneration_plans,
                    },
                )
    except Exception as exc:
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=artifact["artifact_id"],
            event_type="impact_analysis_failed",
            payload={"error": str(exc), "trigger": "revision_accepted"},
        )


def mark_artifact_section_review(
    *,
    artifact_id: str,
    status: str,
    anchor_id: Optional[str] = None,
    reviewer_note: str = "",
    revision_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if status not in {"accepted", "disputed", "revision_pending", "blocked_by_conflict", "outdated"}:
        raise ValueError("Unsupported section review status.")
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")
    if anchor_id:
        anchor = metadata_db.get_artifact_anchor(anchor_id)
        if not anchor or anchor["artifact_id"] != artifact_id:
            raise ValueError("Artifact anchor not found.")
    review = metadata_db.upsert_artifact_section_review(
        section_review_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        anchor_id=anchor_id,
        status=status,
        reviewer_note=reviewer_note,
        revision_session_id=revision_session_id,
    )
    metadata_db.append_design_artifact_event(
        event_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        event_type="section_review_marked",
        payload={
            "section_review_id": review["section_review_id"],
            "anchor_id": anchor_id,
            "status": status,
            "reviewer_note": reviewer_note,
            "revision_session_id": revision_session_id,
        },
    )
    if status in {"disputed", "revision_pending", "blocked_by_conflict"} and artifact.get("status") in {"accepted", "auto_accepted"}:
        metadata_db.update_design_artifact(artifact_id, status="user_disputed")
    return review


def list_artifact_section_reviews(artifact_id: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")
    return metadata_db.list_artifact_section_reviews(artifact_id, status=status)


def hydrate_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    reflection = metadata_db.get_reflection_report_for_artifact(artifact["artifact_id"])
    from services import context_consistency_service
    from services import decision_log_service
    from services import impact_analysis_service

    consistency = context_consistency_service.get_consistency_report_for_artifact(artifact["artifact_id"])
    decision_logs = []
    for decision in artifact.get("decision_refs") or []:
        decision_id = decision.get("decision_id") if isinstance(decision, dict) else None
        if not decision_id:
            continue
        log = decision_log_service.get_decision_log(decision_id)
        if log:
            decision_logs.append(log)
    if not decision_logs:
        decision_logs = decision_log_service.list_decision_logs(artifact["project_id"], artifact["version_id"])
    outgoing_impacts = impact_analysis_service.list_impact_records(
        artifact["project_id"],
        artifact["version_id"],
        source_artifact_id=artifact["artifact_id"],
    )
    incoming_impacts = impact_analysis_service.list_impact_records(
        artifact["project_id"],
        artifact["version_id"],
        impacted_artifact_id=artifact["artifact_id"],
    )
    return {
        **artifact,
        "reflection": reflection,
        "consistency": consistency,
        "decision_logs": decision_logs[:8],
        "impact_records": outgoing_impacts[:20],
        "incoming_impacts": incoming_impacts[:20],
        "section_reviews": metadata_db.list_artifact_section_reviews(artifact["artifact_id"])[:20],
    }


def create_revision_session(
    *,
    project_id: str,
    version_id: str,
    target_artifact_id: str,
    user_feedback: str = "",
) -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(target_artifact_id)
    if not artifact:
        raise ValueError("Target artifact not found.")
    session = metadata_db.create_revision_session(
        revision_session_id=str(uuid.uuid4()),
        project_id=project_id,
        version_id=version_id,
        target_artifact_id=target_artifact_id,
        target_expert_id=artifact["expert_id"],
        status="discussion_open",
        user_feedback=user_feedback,
    )
    metadata_db.update_design_artifact(target_artifact_id, status="user_disputed")
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=session["revision_session_id"],
        event_type="session_created",
        payload={"user_feedback": user_feedback},
    )
    return session


def list_revision_sessions(
    *,
    project_id: str,
    version_id: str,
    target_artifact_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return metadata_db.list_revision_sessions(
        project_id=project_id,
        version_id=version_id,
        target_artifact_id=target_artifact_id,
        status=status,
    )


def add_revision_message(revision_session_id: str, role: str, content: str) -> Dict[str, Any]:
    session = metadata_db.get_revision_session(revision_session_id)
    if not session:
        raise ValueError("Revision session not found.")
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=revision_session_id,
        event_type="message",
        payload={"role": role, "content": content},
    )
    if role == "user" and content.strip():
        merged = "\n".join(item for item in [session.get("user_feedback"), content.strip()] if item)
        session = metadata_db.update_revision_session(revision_session_id, user_feedback=merged) or session
    return session


def finalize_revision_session(revision_session_id: str) -> Dict[str, Any]:
    session = metadata_db.get_revision_session(revision_session_id)
    if not session:
        raise ValueError("Revision session not found.")
    feedback = str(session.get("user_feedback") or "")
    classification = classify_revision_feedback(feedback)
    normalized = {
        "target_expert": session["target_expert_id"],
        "target_artifact_id": session["target_artifact_id"],
        "revision_reason": feedback[:500],
        "user_new_information": feedback,
        "revision_type": classification["revision_type"],
        "as_is_or_to_be": classification["as_is_or_to_be"],
        "semantic": classification["semantic"],
        "candidate_conflicts": [],
        "affected_artifacts": [],
        "decision_required": False,
    }
    from services import context_consistency_service

    detection = context_consistency_service.detect_revision_feedback_conflicts(revision_session_id)
    normalized["revision_type"] = detection["revision_type"]
    normalized["as_is_or_to_be"] = detection["as_is_or_to_be"]
    normalized["semantic"] = detection["semantic"]
    normalized["candidate_conflicts"] = detection["candidate_conflicts"]
    normalized["decision_required"] = detection["decision_required"]
    status = "blocked_pending_decision" if detection["decision_required"] else "revision_intent_created"
    session = metadata_db.update_revision_session(
        revision_session_id,
        status=status,
        normalized_revision_request=normalized,
        conflict_report_id=detection["candidate_conflicts"][0] if detection["candidate_conflicts"] else None,
    ) or session
    metadata_db.update_design_artifact(session["target_artifact_id"], status="revision_requested")
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=revision_session_id,
        event_type="system_summary",
        payload=normalized,
    )
    return session


def suggest_revision_replacement(
    *,
    revision_session_id: str,
    artifact_id: str,
    anchor_id: str,
    user_feedback: str = "",
) -> Dict[str, Any]:
    session = metadata_db.get_revision_session(revision_session_id)
    artifact = metadata_db.get_design_artifact(artifact_id)
    anchor = metadata_db.get_artifact_anchor(anchor_id)
    if not session or not artifact or not anchor:
        raise ValueError("Revision session, artifact, or anchor not found.")
    if session["target_artifact_id"] != artifact_id or anchor["artifact_id"] != artifact_id:
        raise ValueError("Revision suggestion target does not match the selected artifact.")

    content = _read_artifact_content(artifact["project_id"], artifact["version_id"], artifact["file_path"])
    start_offset = int(anchor["start_offset"])
    end_offset = int(anchor["end_offset"])
    if start_offset < 0 or end_offset < start_offset or end_offset > len(content):
        raise ValueError("Anchor offsets are outside the artifact content.")
    original = content[start_offset:end_offset]
    feedback = (user_feedback or session.get("user_feedback") or "").strip()
    if not feedback:
        raise ValueError("User feedback is required to suggest a revision.")

    system_prompt = (
        "You revise a selected range of an IR requirement artifact. "
        "Return JSON only. Preserve all correct details unless the user's feedback explicitly changes them. "
        "Do not include markdown fences unless they are part of the replacement text."
    )
    surrounding_start = max(0, start_offset - 1200)
    surrounding_end = min(len(content), end_offset + 1200)
    user_prompt = json.dumps(
        {
            "task": "rewrite_selected_artifact_range",
            "artifact": {
                "file_name": artifact.get("file_name"),
                "expert_id": artifact.get("expert_id"),
                "artifact_type": artifact.get("artifact_type"),
            },
            "selected_range": original,
            "user_feedback": feedback,
            "surrounding_context": content[surrounding_start:surrounding_end],
            "required_output": {
                "replacement_text": "Complete replacement text for the selected range only.",
                "rationale": "Short rationale for the proposed edit.",
            },
        },
        ensure_ascii=False,
    )
    try:
        from services import llm_service

        output = llm_service.generate_with_llm(
            system_prompt,
            user_prompt,
            ["replacement_text", "rationale"],
            max_retries=1,
            project_id=artifact["project_id"],
            version=artifact["version_id"],
            node_id="artifact-revision-suggestion",
        )
        replacement_text = str(output.artifacts.get("replacement_text") or "")
        rationale = str(output.artifacts.get("rationale") or output.reasoning or "").strip()
    except Exception as exc:
        replacement_text = original
        rationale = f"LLM suggestion failed; preserved original selection. Error: {exc}"

    normalized = dict(session.get("normalized_revision_request") or {})
    normalized["suggested_replacement_text"] = replacement_text
    normalized["suggested_replacement_rationale"] = rationale
    session = metadata_db.update_revision_session(
        revision_session_id,
        normalized_revision_request=normalized,
    ) or session
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=revision_session_id,
        event_type="replacement_suggested",
        payload={
            "artifact_id": artifact_id,
            "anchor_id": anchor_id,
            "original_text": original,
            "replacement_text": replacement_text,
            "rationale": rationale,
            "has_changes": _has_meaningful_text_change(original, replacement_text),
        },
    )
    return {
        "project_id": artifact["project_id"],
        "version_id": artifact["version_id"],
        "revision_session_id": revision_session_id,
        "artifact_id": artifact_id,
        "anchor_id": anchor_id,
        "original_text": original,
        "replacement_text": replacement_text,
        "rationale": rationale,
        "has_changes": _has_meaningful_text_change(original, replacement_text),
        "session": session,
    }


def create_anchor(
    *,
    artifact_id: str,
    file_name: str,
    anchor_type: str,
    text_excerpt: str,
    start_offset: Optional[int] = None,
    end_offset: Optional[int] = None,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Artifact not found.")
    content = _read_artifact_content(artifact["project_id"], artifact["version_id"], artifact["file_path"])
    has_offsets = start_offset is not None and end_offset is not None
    if not has_offsets:
        index = content.find(text_excerpt)
        if index < 0:
            raise ValueError(
                "Selected text could not be located in the artifact source. "
                "Please reselect it in source view so exact offsets can be captured."
            )
        start_offset = index
        end_offset = index + len(text_excerpt)
    if start_offset < 0 or end_offset < start_offset or end_offset > len(content):
        raise ValueError("Anchor offsets are outside the artifact content.")
    anchor_content = content[start_offset:end_offset]
    if has_offsets and text_excerpt.strip():
        normalized_excerpt = _normalize_anchor_text(text_excerpt)
        normalized_anchor = _normalize_anchor_text(anchor_content)
        if normalized_excerpt and normalized_excerpt not in normalized_anchor and normalized_anchor not in normalized_excerpt:
            raise ValueError("Anchor offsets do not match the selected artifact text.")
    return metadata_db.create_artifact_anchor(
        anchor_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        file_name=file_name,
        anchor_type=anchor_type if anchor_type in {"heading_section", "text_range", "code_block"} else "text_range",
        label=label or text_excerpt[:80],
        text_excerpt=text_excerpt,
        start_offset=start_offset,
        end_offset=end_offset,
        structural_path={},
        content_hash=_sha256_text(anchor_content),
    )


def create_patch_preview(
    *,
    revision_session_id: str,
    artifact_id: str,
    anchor_id: str,
    replacement_text: str,
    rationale: str = "",
    preserve_policy: str = "preserve_unselected_content",
    scope: str = "selection",
) -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    anchor = metadata_db.get_artifact_anchor(anchor_id)
    if not artifact or not anchor:
        raise ValueError("Artifact or anchor not found.")
    content = _read_artifact_content(artifact["project_id"], artifact["version_id"], artifact["file_path"])
    source_hash = _sha256_text(content)
    start_offset = int(anchor["start_offset"])
    end_offset = int(anchor["end_offset"])
    original = content[start_offset:end_offset]
    if not _has_meaningful_text_change(original, replacement_text):
        raise ValueError("No content changes detected in the selected range.")
    if _sha256_text(original) != anchor["content_hash"]:
        validation = {"status": "stale_anchor", "message": "Anchor content hash no longer matches the artifact file."}
    else:
        validation = {
            "status": "valid",
            "message": "Patch replaces the full artifact." if scope == "artifact" else "Patch is limited to the selected range.",
        }
    diff = list(
        difflib.unified_diff(
            original.splitlines(),
            replacement_text.splitlines(),
            fromfile=artifact["file_name"],
            tofile=f"{artifact['file_name']} (preview)",
            lineterm="",
        )
    )
    patch = metadata_db.create_revision_patch(
        patch_id=str(uuid.uuid4()),
        revision_session_id=revision_session_id,
        artifact_id=artifact_id,
        anchor_id=anchor_id,
        scope=scope if scope in {"selection", "artifact"} else "selection",
        preserve_policy=preserve_policy,
        patch_status="preview_created",
        source_content_hash=source_hash,
        allowed_range={"start_offset": start_offset, "end_offset": end_offset},
        diff={
            "format": "replace_range",
            "original_text": original,
            "replacement_text": replacement_text,
            "unified_diff": diff,
        },
        rationale=rationale,
        predicted_impact={"current_artifact": "needs_revalidation", "downstream": []},
        apply_result={},
        post_apply_validation=validation,
    )
    metadata_db.update_revision_session(revision_session_id, status="patch_preview_created")
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=revision_session_id,
        event_type="patch_preview",
        payload={"patch_id": patch["patch_id"], "validation": validation},
    )
    return patch


def create_manual_artifact_revision(
    *,
    artifact_id: str,
    content: str,
    reviewer_note: str = "",
    edited_by: str = "user",
) -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")
    if not str(content or "").strip():
        raise ValueError("Manual revision content cannot be empty.")
    original = _read_artifact_content(artifact["project_id"], artifact["version_id"], artifact["file_path"])
    if not _has_meaningful_text_change(original, content):
        raise ValueError("No content changes detected in the artifact.")

    session = create_revision_session(
        project_id=artifact["project_id"],
        version_id=artifact["version_id"],
        target_artifact_id=artifact_id,
        user_feedback=reviewer_note or "Manual artifact revision.",
    )
    normalized = {
        "target_expert": artifact["expert_id"],
        "target_artifact_id": artifact_id,
        "revision_reason": (reviewer_note or "Manual artifact revision.")[:500],
        "user_new_information": reviewer_note or "",
        "revision_type": "manual_edit",
        "as_is_or_to_be": "to_be",
        "semantic": "manual_artifact_revision",
        "candidate_conflicts": [],
        "affected_artifacts": [],
        "decision_required": False,
        "edited_by": edited_by or "user",
    }
    metadata_db.update_revision_session(
        session["revision_session_id"],
        status="manual_revision_created",
        normalized_revision_request=normalized,
    )
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=session["revision_session_id"],
        event_type="manual_revision_created",
        payload=normalized,
    )
    anchor = create_anchor(
        artifact_id=artifact_id,
        file_name=artifact["file_name"],
        anchor_type="text_range",
        text_excerpt=original[:5000],
        start_offset=0,
        end_offset=len(original),
        label="Full artifact",
    )
    patch = create_patch_preview(
        revision_session_id=session["revision_session_id"],
        artifact_id=artifact_id,
        anchor_id=anchor["anchor_id"],
        replacement_text=content,
        rationale=reviewer_note or "Manual artifact revision.",
        preserve_policy="replace_full_artifact",
        scope="artifact",
    )
    applied = apply_revision_patch(patch["patch_id"])
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=session["revision_session_id"],
        event_type="manual_revision_applied",
        payload={
            "patch_id": applied["patch_id"],
            "created_artifact_id": applied.get("created_artifact_id"),
            "edited_by": edited_by or "user",
        },
    )
    return applied


def apply_revision_patch(patch_id: str) -> Dict[str, Any]:
    patch = metadata_db.get_revision_patch(patch_id)
    if not patch:
        raise ValueError("Revision patch not found.")
    if patch["patch_status"] not in {"preview_created", "apply_failed"}:
        raise ValueError("Patch is not in an applyable state.")
    artifact = metadata_db.get_design_artifact(patch["artifact_id"])
    if not artifact:
        raise ValueError("Source artifact not found.")
    content = _read_artifact_content(artifact["project_id"], artifact["version_id"], artifact["file_path"])
    if _sha256_text(content) != patch["source_content_hash"]:
        failed = metadata_db.update_revision_patch(
            patch_id,
            patch_status="apply_failed",
            apply_result={"status": "source_hash_mismatch"},
            post_apply_validation={"status": "failed", "message": "Source artifact hash changed."},
        )
        return failed or patch

    allowed = patch.get("allowed_range") or {}
    start_offset = int(allowed.get("start_offset"))
    end_offset = int(allowed.get("end_offset"))
    replacement = (patch.get("diff") or {}).get("replacement_text", "")
    new_content = content[:start_offset] + replacement + content[end_offset:]
    if not _has_meaningful_text_change(content, new_content):
        failed = metadata_db.update_revision_patch(
            patch_id,
            patch_status="apply_failed",
            apply_result={"status": "no_changes_detected"},
            post_apply_validation={"status": "failed", "message": "No content changes detected."},
        )
        return failed or patch
    source_path = (_project_version_root(artifact["project_id"], artifact["version_id"]) / artifact["file_path"]).resolve()
    source_hash = artifact["content_hash"]
    update_existing_revision = _is_mutable_revision_artifact(artifact)
    parent_artifact_id = artifact.get("parent_artifact_id") if update_existing_revision else artifact["artifact_id"]
    version_number = int(artifact.get("artifact_version") or 1) if update_existing_revision else int(artifact.get("artifact_version") or 1) + 1
    new_file_name = artifact["file_name"] if update_existing_revision else f"{source_path.name}.v{version_number}"
    target_path = source_path if update_existing_revision else source_path.parent / new_file_name
    target_path.write_text(new_content, encoding="utf-8")
    new_relative = _relative_to_version(artifact["project_id"], artifact["version_id"], target_path)
    new_hash = _sha256_text(new_content)

    if update_existing_revision:
        new_artifact = metadata_db.update_design_artifact(
            artifact["artifact_id"],
            status="ready_for_review",
            content_hash=new_hash,
            summary=_build_summary(new_content),
            source_refs=[
                {"type": "artifact_revision_previous", "artifact_id": artifact["artifact_id"], "content_hash": source_hash},
                {"type": "artifact", "artifact_id": parent_artifact_id, "content_hash": (artifact.get("source_refs") or [{}])[0].get("content_hash")},
                {"type": "file", "path": new_relative, "content_hash": new_hash},
            ],
        ) or artifact
    else:
        new_artifact = metadata_db.create_design_artifact(
            artifact_id=str(uuid.uuid4()),
            project_id=artifact["project_id"],
            version_id=artifact["version_id"],
            run_id=artifact.get("run_id"),
            expert_id=artifact["expert_id"],
            artifact_type=artifact["artifact_type"],
            artifact_version=version_number,
            parent_artifact_id=parent_artifact_id,
            status="ready_for_review",
            title=artifact["title"],
            file_name=new_file_name,
            file_path=new_relative,
            content_hash=new_hash,
            summary=_build_summary(new_content),
            source_refs=[
                {"type": "artifact", "artifact_id": artifact["artifact_id"], "content_hash": source_hash},
                {"type": "file", "path": new_relative, "content_hash": new_hash},
            ],
            dependency_refs=artifact.get("dependency_refs") or [],
            decision_refs=[],
        )
    reflection = record_reflection_observation(new_content, dependency_refs=artifact.get("dependency_refs") or [])
    report = metadata_db.create_expert_reflection_report(
        report_id=str(uuid.uuid4()),
        artifact_id=new_artifact["artifact_id"],
        expert_id=new_artifact["expert_id"],
        status=reflection["status"],
        confidence=reflection["confidence"],
        checks=reflection["checks"],
        issues=reflection["issues"],
        assumptions=reflection["assumptions"],
        open_questions=reflection["open_questions"],
        required_actions=reflection["required_actions"],
        blocks_downstream=reflection["blocks_downstream"],
    )
    metadata_db.update_design_artifact(
        new_artifact["artifact_id"],
        reflection_report_id=report["report_id"],
        status=_status_from_reflection(reflection, default_status="ready_for_review"),
    )
    try:
        from services import context_consistency_service

        context_consistency_service.run_consistency_check(new_artifact["artifact_id"])
    except Exception as exc:
        metadata_db.append_design_artifact_event(
            event_id=str(uuid.uuid4()),
            artifact_id=new_artifact["artifact_id"],
            event_type="consistency_check_failed",
            payload={"error": str(exc)},
        )
    if not update_existing_revision:
        metadata_db.update_design_artifact(artifact["artifact_id"], status="superseded")
    updated_patch = metadata_db.update_revision_patch(
        patch_id,
        patch_status="applied",
        created_artifact_id=new_artifact["artifact_id"],
        apply_result={
            "status": "applied",
            "file_path": new_relative,
            "revision_mode": "updated_existing_revision" if update_existing_revision else "created_new_version",
        },
        post_apply_content_hash=new_hash,
        post_apply_validation={
            "status": "valid",
            "message": "Patch applied to existing revision artifact." if update_existing_revision else "Patch applied and new artifact version created.",
        },
        applied=True,
    )
    session_id = patch["revision_session_id"]
    metadata_db.update_revision_session(
        session_id,
        status="awaiting_revision_acceptance",
        created_artifact_id=new_artifact["artifact_id"],
        affected_artifacts=[],
    )
    metadata_db.append_revision_session_event(
        event_id=str(uuid.uuid4()),
        revision_session_id=session_id,
        event_type="patch_applied",
        payload={
            "patch_id": patch_id,
            "created_artifact_id": new_artifact["artifact_id"],
            "next_step": "accept_revision",
        },
    )
    return updated_patch or patch
