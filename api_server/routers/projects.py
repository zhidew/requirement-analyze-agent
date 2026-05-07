from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
import shutil
from typing import List
from models.project import (
    ArtifactAcceptRequest,
    ConflictDecisionRequest,
    CancelRequest,
    ClarifiedRequirementsResponse,
    ContinueRequest,
    DecisionCreateRequest,
    ArtifactAnchorCreateRequest,
    ImpactStatusUpdateRequest,
    InteractionDetailResponse,
    InteractionListResponse,
    InteractionResponseRequest,
    JobResponse,
    ManualArtifactRevisionRequest,
    NodeRetryRequest,
    ProjectCreateRequest,
    ProjectResponse,
    RevisionMessageRequest,
    RevisionReplacementSuggestionRequest,
    RevisionPatchPreviewRequest,
    RevisionSessionCreateRequest,
    ResumeRequest,
    ScheduleRunRequest,
    ScheduleRunResponse,
    SectionReviewRequest,
    VersionRunRequest,
)
from models.management import VersionListResponse
import services.orchestrator_service as orch
from services import design_artifact_service as artifacts_service
from services import context_consistency_service
from services import decision_log_service
from services import artifact_dependency_service
from services import impact_analysis_service

router = APIRouter(
    prefix="/api/v1/projects",
    tags=["Projects"],
)

@router.post("/{project_id}/versions/{version}/upload")
async def upload_baseline_files(
    project_id: str, 
    version: str, 
    files: List[UploadFile] = File(...)
):
    """上传基线输入文件（需求、模型、字典等）"""
    project_path = orch.PROJECTS_DIR / project_id / version
    baseline_dir = project_path / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    
    saved_files = []
    for file in files:
        file_path = baseline_dir / file.filename
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_files.append(file.filename)
        
    return {"status": "success", "files": saved_files}

@router.get("", response_model=List[ProjectResponse])
async def get_projects():
    projects = orch.list_projects()
    return projects

@router.post("", response_model=ProjectResponse)
async def create_project(req: ProjectCreateRequest):
    project_id = req.name.strip().replace(" ", "-").lower()
    orch.create_project(project_id)
    return {"id": project_id, "name": req.name, "description": req.description}

@router.get("/{project_id}/assets-summary")
async def get_project_assets_summary(project_id: str):
    summary = orch.get_project_assets_summary(project_id)
    return summary

@router.delete("/{project_id}")
async def delete_project(project_id: str):
    success = orch.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=409, detail="Project cannot be deleted while it has running versions.")
    return {"success": True, "project_id": project_id}

@router.get("/{project_id}/versions", response_model=VersionListResponse)
async def get_project_versions(project_id: str, page: int = 1, page_size: int = 10):
    versions_data = orch.list_versions(project_id, page, page_size)
    return versions_data

@router.delete("/{project_id}/versions/{version}")
async def delete_project_version(project_id: str, version: str):
    deleted = orch.delete_version(project_id, version)
    if not deleted:
        raise HTTPException(status_code=409, detail="Version cannot be deleted while it is running, or it does not exist.")
    return {"success": True, "project_id": project_id, "version": version}

@router.post("/{project_id}/versions/{version}/run", response_model=JobResponse)
async def run_design_orchestrator(project_id: str, version: str, req: VersionRunRequest):
    job_id = orch.trigger_orchestrator(
        project_id,
        version,
        req.requirement_text,
        req.model,
    )
    return {"job_id": job_id, "status": "queued", "message": "Orchestrator job queued."}


@router.post("/{project_id}/versions/{version}/schedule-run", response_model=ScheduleRunResponse)
async def schedule_design_orchestrator(project_id: str, version: str, req: ScheduleRunRequest):
    try:
        schedule = await orch.schedule_orchestrator_run(
            project_id,
            version,
            req.requirement_text,
            req.scheduled_for,
            req.model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "schedule_id": schedule["schedule_id"],
        "status": "scheduled",
        "message": "Orchestrator run scheduled.",
        "scheduled_for": schedule["scheduled_for"],
    }

@router.get("/{project_id}/versions/{version}/artifacts")
async def get_artifacts(project_id: str, version: str):
    tree = orch.get_artifacts_tree(project_id, version)
    return tree


@router.get("/{project_id}/versions/{version}/design-artifacts")
async def list_design_artifacts(project_id: str, version: str, expert_id: str | None = None):
    orch.get_artifacts_tree(project_id, version)
    return {"items": artifacts_service.list_design_artifacts(project_id, version, expert_id=expert_id)}


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/governance")
async def get_design_artifact(project_id: str, version: str, artifact_id: str):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return artifact


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/accept")
async def accept_design_artifact(project_id: str, version: str, artifact_id: str, req: ArtifactAcceptRequest):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    try:
        return artifacts_service.accept_design_artifact(
            artifact_id,
            reviewer_note=req.reviewer_note or "",
            accepted_by=req.accepted_by or "user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/manual-revision")
async def create_manual_artifact_revision(project_id: str, version: str, artifact_id: str, req: ManualArtifactRevisionRequest):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    try:
        return artifacts_service.create_manual_artifact_revision(
            artifact_id=artifact_id,
            content=req.content,
            reviewer_note=req.reviewer_note or "",
            edited_by=req.edited_by or "user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/reflection")
async def get_artifact_reflection(project_id: str, version: str, artifact_id: str):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return artifact.get("reflection") or {}


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/consistency")
async def get_artifact_consistency(project_id: str, version: str, artifact_id: str):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return artifact.get("consistency") or {}


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/consistency/recheck")
async def recheck_artifact_consistency(project_id: str, version: str, artifact_id: str):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    try:
        return context_consistency_service.run_consistency_check(artifact_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{project_id}/versions/{version}/conflicts")
async def list_context_conflicts(project_id: str, version: str, status: str | None = None):
    return {"items": context_consistency_service.list_context_conflicts(project_id, version, status=status)}


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/section-reviews")
async def mark_artifact_section_review(project_id: str, version: str, artifact_id: str, req: SectionReviewRequest):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    try:
        return artifacts_service.mark_artifact_section_review(
            artifact_id=artifact_id,
            anchor_id=req.anchor_id,
            status=req.status,
            reviewer_note=req.reviewer_note or "",
            revision_session_id=req.revision_session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/section-reviews")
async def list_artifact_section_reviews(project_id: str, version: str, artifact_id: str, status: str | None = None):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return {"items": artifacts_service.list_artifact_section_reviews(artifact_id, status=status)}


@router.get("/{project_id}/versions/{version}/dependency-graph")
async def get_artifact_dependency_graph(project_id: str, version: str):
    return artifact_dependency_service.build_artifact_dependency_graph(project_id, version)


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/downstream")
async def list_downstream_artifacts(project_id: str, version: str, artifact_id: str):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return {"items": artifact_dependency_service.find_downstream_artifacts(artifact_id)}


@router.get("/{project_id}/versions/{version}/impact-records")
async def list_impact_records(
    project_id: str,
    version: str,
    source_artifact_id: str | None = None,
    impacted_artifact_id: str | None = None,
    impact_status: str | None = None,
):
    return {
        "items": impact_analysis_service.list_impact_records(
            project_id,
            version,
            source_artifact_id=source_artifact_id,
            impacted_artifact_id=impacted_artifact_id,
            impact_status=impact_status,
        )
    }


@router.post("/{project_id}/versions/{version}/impact-records/{impact_id}/status")
async def update_impact_record_status(project_id: str, version: str, impact_id: str, req: ImpactStatusUpdateRequest):
    try:
        record = impact_analysis_service.mark_downstream_artifact_status(impact_id, req.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record["project_id"] != project_id or record["version_id"] != version:
        raise HTTPException(status_code=404, detail="Impact record not found.")
    return record


@router.get("/{project_id}/versions/{version}/decision-logs")
async def list_decision_logs(project_id: str, version: str, scope: str | None = None):
    return {"items": decision_log_service.list_decision_logs(project_id, version, scope=scope)}


@router.get("/{project_id}/versions/{version}/decision-logs/{decision_id}")
async def get_decision_log(project_id: str, version: str, decision_id: str):
    decision = decision_log_service.get_decision_log(decision_id)
    if not decision or decision["project_id"] != project_id or decision["version_id"] != version:
        raise HTTPException(status_code=404, detail="Decision log not found.")
    return decision


@router.post("/{project_id}/versions/{version}/decision-logs")
async def create_decision_log(project_id: str, version: str, req: DecisionCreateRequest):
    try:
        return decision_log_service.create_decision_log(
            project_id=project_id,
            version_id=version,
            scope="project",
            conflict_ids=req.conflict_ids,
            decision=req.decision,
            basis=req.basis,
            authority=req.authority,
            applies_to=req.applies_to,
            created_by=req.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{project_id}/versions/{version}/conflicts/{conflict_id}/decisions")
async def resolve_context_conflict(project_id: str, version: str, conflict_id: str, req: ConflictDecisionRequest):
    conflict = context_consistency_service.metadata_db.get_context_conflict(conflict_id)
    if not conflict or conflict["project_id"] != project_id or conflict["version_id"] != version:
        raise HTTPException(status_code=404, detail="Context conflict not found.")
    try:
        decision = decision_log_service.resolve_conflict_with_decision(
            conflict_id=conflict_id,
            decision=req.decision,
            basis=req.basis,
            authority=req.authority,
            created_by=req.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return decision


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/revision-sessions")
async def create_revision_session(project_id: str, version: str, artifact_id: str, req: RevisionSessionCreateRequest):
    try:
        return artifacts_service.create_revision_session(
            project_id=project_id,
            version_id=version,
            target_artifact_id=artifact_id,
            user_feedback=req.user_feedback or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{project_id}/versions/{version}/artifacts/{artifact_id}/revision-sessions")
async def list_artifact_revision_sessions(project_id: str, version: str, artifact_id: str, status: str | None = None):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    return {
        "items": artifacts_service.list_revision_sessions(
            project_id=project_id,
            version_id=version,
            target_artifact_id=artifact_id,
            status=status,
        )
    }


@router.get("/{project_id}/versions/{version}/revision-sessions/{session_id}")
async def get_revision_session(project_id: str, version: str, session_id: str):
    from services.db_service import metadata_db

    session = metadata_db.get_revision_session(session_id)
    if not session or session["project_id"] != project_id or session["version_id"] != version:
        raise HTTPException(status_code=404, detail="Revision session not found.")
    return session


@router.post("/{project_id}/versions/{version}/revision-sessions/{session_id}/messages")
async def add_revision_message(project_id: str, version: str, session_id: str, req: RevisionMessageRequest):
    try:
        session = artifacts_service.add_revision_message(session_id, req.role, req.content)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if session["project_id"] != project_id or session["version_id"] != version:
        raise HTTPException(status_code=404, detail="Revision session not found.")
    return session


@router.post("/{project_id}/versions/{version}/revision-sessions/{session_id}/finalize")
async def finalize_revision_session(project_id: str, version: str, session_id: str):
    try:
        session = artifacts_service.finalize_revision_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if session["project_id"] != project_id or session["version_id"] != version:
        raise HTTPException(status_code=404, detail="Revision session not found.")
    return session


@router.post("/{project_id}/versions/{version}/revision-sessions/{session_id}/replacement-suggestion")
async def suggest_revision_replacement(project_id: str, version: str, session_id: str, req: RevisionReplacementSuggestionRequest):
    try:
        suggestion = artifacts_service.suggest_revision_replacement(
            revision_session_id=session_id,
            artifact_id=req.artifact_id,
            anchor_id=req.anchor_id,
            user_feedback=req.user_feedback or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if suggestion["project_id"] != project_id or suggestion["version_id"] != version:
        raise HTTPException(status_code=404, detail="Revision session not found.")
    return suggestion


@router.post("/{project_id}/versions/{version}/artifacts/{artifact_id}/anchors")
async def create_artifact_anchor(project_id: str, version: str, artifact_id: str, req: ArtifactAnchorCreateRequest):
    artifact = artifacts_service.get_design_artifact(artifact_id)
    if not artifact or artifact["project_id"] != project_id or artifact["version_id"] != version:
        raise HTTPException(status_code=404, detail="Design artifact not found.")
    try:
        return artifacts_service.create_anchor(
            artifact_id=artifact_id,
            file_name=req.file_name,
            anchor_type=req.anchor_type,
            text_excerpt=req.text_excerpt,
            start_offset=req.start_offset,
            end_offset=req.end_offset,
            label=req.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{project_id}/versions/{version}/revision-sessions/{session_id}/patch-preview")
async def create_revision_patch_preview(project_id: str, version: str, session_id: str, req: RevisionPatchPreviewRequest):
    try:
        patch = artifacts_service.create_patch_preview(
            revision_session_id=session_id,
            artifact_id=req.artifact_id,
            anchor_id=req.anchor_id,
            replacement_text=req.replacement_text,
            rationale=req.rationale or "",
            preserve_policy=req.preserve_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return patch


@router.post("/{project_id}/versions/{version}/revision-patches/{patch_id}/apply")
async def apply_revision_patch(project_id: str, version: str, patch_id: str):
    try:
        return artifacts_service.apply_revision_patch(patch_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/{project_id}/versions/{version}/state")
async def get_workflow_state(project_id: str, version: str):
    state = orch.get_workflow_state(project_id, version)
    if not state:
        # Fallback to a very minimal state instead of 404
        return {
            "project_id": project_id,
            "version": version,
            "run_status": "failed",
            "task_queue": [],
            "history": ["Error: Workflow state not found on server."],
            "artifacts": {},
        }
    return state

@router.post("/{project_id}/versions/{version}/resume")
async def resume_workflow(project_id: str, version: str, req: ResumeRequest):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    success = await orch.resume_workflow(project_id, version, payload)
    if not success:
        raise HTTPException(status_code=409, detail="Workflow is not waiting for human input.")
    return {"success": True, "status": "queued", "action": req.action}


@router.get("/{project_id}/versions/{version}/interactions/current", response_model=InteractionDetailResponse | None)
async def get_current_interaction(project_id: str, version: str):
    return orch.get_current_interaction(project_id, version)


@router.get("/{project_id}/versions/{version}/interactions", response_model=InteractionListResponse)
async def list_interactions(project_id: str, version: str):
    return {"items": orch.list_interactions(project_id, version)}


@router.get("/{project_id}/versions/{version}/interactions/{interaction_id}", response_model=InteractionDetailResponse)
async def get_interaction_detail(project_id: str, version: str, interaction_id: str):
    record = orch.get_interaction_detail(project_id, version, interaction_id)
    if not record:
        raise HTTPException(status_code=404, detail="Interaction not found.")
    return record


@router.post("/{project_id}/versions/{version}/interactions/{interaction_id}/response")
async def submit_interaction_response(
    project_id: str,
    version: str,
    interaction_id: str,
    req: InteractionResponseRequest,
):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    success = await orch.submit_interaction_response(project_id, version, interaction_id, payload)
    if not success:
        raise HTTPException(status_code=409, detail="Interaction cannot be resumed in the current workflow state.")
    return {"success": True, "status": "queued", "interaction_id": interaction_id}


@router.get("/{project_id}/versions/{version}/clarified-requirements", response_model=ClarifiedRequirementsResponse)
async def get_clarified_requirements(project_id: str, version: str):
    return orch.get_clarified_requirements(project_id, version)

@router.post("/{project_id}/versions/{version}/retry-node")
async def retry_workflow_node(project_id: str, version: str, req: NodeRetryRequest):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    success = await orch.retry_workflow_node(
        project_id,
        version,
        payload["node_type"],
        payload.get("model"),
    )
    if not success:
        raise HTTPException(status_code=409, detail="Node cannot be retried in the current workflow state.")
    return {"success": True, "status": "queued", "node_type": payload["node_type"]}

@router.post("/{project_id}/versions/{version}/continue")
async def continue_workflow(project_id: str, version: str, req: ContinueRequest):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    success = await orch.continue_workflow(
        project_id,
        version,
        payload.get("model"),
    )
    if not success:
        raise HTTPException(status_code=409, detail="Workflow cannot be continued in the current state.")
    return {"success": True, "status": "queued"}


@router.post("/{project_id}/versions/{version}/cancel")
async def cancel_workflow(project_id: str, version: str, req: CancelRequest):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    success = await orch.cancel_workflow(
        project_id,
        version,
        payload.get("reason"),
    )
    if not success:
        raise HTTPException(status_code=409, detail="Workflow cannot be cancelled in the current state.")
    return {"success": True, "status": "cancelled"}


@router.get("/{project_id}/versions/{version}/logs")
async def get_version_logs(project_id: str, version: str):
    logs = orch.get_version_logs(project_id, version)
    return {"logs": logs}
