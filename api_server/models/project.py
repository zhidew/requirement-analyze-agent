from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class ProjectCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    total_versions: int = 0
    enabled_experts_count: int = 0
    running_versions: int = 0
    success_versions: int = 0
    failed_versions: int = 0
    waiting_versions: int = 0
    queued_versions: int = 0
    unknown_versions: int = 0
    status_counts: Dict[str, int] = {}
    has_versions: bool = False
    is_active: bool = False
    status: str = 'empty'

class VersionRunRequest(BaseModel):
    requirement_text: str
    model: Optional[str] = None


class ScheduleRunRequest(BaseModel):
    requirement_text: str
    scheduled_for: str
    model: Optional[str] = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class ScheduleRunResponse(BaseModel):
    schedule_id: str
    status: str
    message: str
    scheduled_for: str


class ResumeRequest(BaseModel):
    action: str
    node_id: Optional[str] = None
    interrupt_id: Optional[str] = None
    interaction_id: Optional[str] = None
    selected_option: Optional[str] = None
    selected_experts: Optional[List[str]] = None
    answer: Optional[str] = None
    feedback: Optional[str] = None
    response: Optional[Dict[str, Any]] = None


class InteractionResponseRequest(BaseModel):
    action: Optional[str] = None
    response: Dict[str, Any] = Field(default_factory=dict)


class InteractionEventResponse(BaseModel):
    event_id: str
    interaction_id: str
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class InteractionDetailResponse(BaseModel):
    interaction_id: str
    project_id: str
    version_id: str
    run_id: Optional[str] = None
    scope: str
    owner_node: str
    owner_expert_id: Optional[str] = None
    status: str
    turn_index: int = 0
    parent_interaction_id: Optional[str] = None
    question_text: str
    question_schema: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    answer: Dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    knowledge_refs: List[str] = Field(default_factory=list)
    affected_artifacts: List[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None
    events: List[InteractionEventResponse] = Field(default_factory=list)


class InteractionListResponse(BaseModel):
    items: List[InteractionDetailResponse] = Field(default_factory=list)


class ClarifiedRequirementsResponse(BaseModel):
    summary: str = ""
    clarified_requirements_markdown: str = ""
    requirements: Dict[str, Any] = Field(default_factory=dict)
    clarification_log: List[Dict[str, Any]] = Field(default_factory=list)


class NodeRetryRequest(BaseModel):
    node_type: str
    model: Optional[str] = None


class ContinueRequest(BaseModel):
    model: Optional[str] = None


class CancelRequest(BaseModel):
    reason: Optional[str] = None


class RevisionSessionCreateRequest(BaseModel):
    user_feedback: Optional[str] = ""


class RevisionMessageRequest(BaseModel):
    role: str = "user"
    content: str


class ArtifactAnchorCreateRequest(BaseModel):
    file_name: str
    anchor_type: str = "text_range"
    label: Optional[str] = None
    text_excerpt: str
    start_offset: Optional[int] = None
    end_offset: Optional[int] = None


class RevisionPatchPreviewRequest(BaseModel):
    artifact_id: str
    anchor_id: str
    replacement_text: str
    rationale: Optional[str] = ""
    preserve_policy: str = "preserve_unselected_content"


class RevisionReplacementSuggestionRequest(BaseModel):
    artifact_id: str
    anchor_id: str
    user_feedback: Optional[str] = ""


class ManualArtifactRevisionRequest(BaseModel):
    content: str
    reviewer_note: Optional[str] = ""
    edited_by: Optional[str] = "user"


class ArtifactAcceptRequest(BaseModel):
    reviewer_note: Optional[str] = ""
    accepted_by: Optional[str] = "user"


class SectionReviewRequest(BaseModel):
    status: str
    anchor_id: Optional[str] = None
    reviewer_note: Optional[str] = ""
    revision_session_id: Optional[str] = None


class DecisionCreateRequest(BaseModel):
    conflict_ids: List[str] = Field(default_factory=list)
    decision: str
    basis: str
    authority: str
    applies_to: List[str] = Field(default_factory=list)
    created_by: Optional[str] = None


class ConflictDecisionRequest(BaseModel):
    decision: str
    basis: str
    authority: str
    created_by: Optional[str] = None


class ImpactStatusUpdateRequest(BaseModel):
    status: str
