from __future__ import annotations

from typing import Any, Dict, Literal, Union

from pydantic import BaseModel, Field


class EventModel(BaseModel):
    event_id: str = Field(description="Unique event identifier for de-duplication and replay.")
    event_type: str = Field(description="Discriminator for the structured SSE event.")
    run_id: str = Field(description="Stable run identifier shared by all events in a single execution.")
    timestamp: str = Field(description="UTC ISO-8601 timestamp when the event was produced.")


class NodeStartedEvent(EventModel):
    event_type: Literal["node_started"] = "node_started"
    node_id: str = Field(description="Stable node identifier within the run.")
    node_type: str = Field(description="Node type or agent type, such as planner or api-design.")


class NodeCompletedEvent(EventModel):
    event_type: Literal["node_completed"] = "node_completed"
    node_id: str = Field(description="Stable node identifier within the run.")
    node_type: str = Field(description="Node type or agent type, such as planner or api-design.")
    status: Literal["success", "failed", "skipped"] = Field(description="Final node execution result.")


class TextDeltaEvent(EventModel):
    event_type: Literal["text_delta"] = "text_delta"
    node_id: str = Field(description="Stable node identifier associated with the text stream.")
    node_type: str = Field(description="Node type or agent type emitting the text delta.")
    stream_name: Literal["history", "stdout", "stderr"] = Field(description="Named text stream channel.")
    delta: str = Field(description="Incremental text payload for the stream.")


class ArtifactUpdatedEvent(EventModel):
    event_type: Literal["artifact_updated"] = "artifact_updated"
    node_id: str = Field(description="Stable node identifier associated with the artifact update.")
    node_type: str = Field(description="Node type or agent type that produced the artifact.")
    artifact_name: str = Field(description="Artifact file name.")
    artifact_status: Literal["created", "updated"] = Field(description="Artifact change mode.")


class ArtifactGovernanceReviewableEvent(EventModel):
    event_type: Literal["artifact_governance_reviewable"] = "artifact_governance_reviewable"
    node_id: str = Field(description="Stable node identifier associated with the governed artifact output.")
    node_type: str = Field(description="Node type or agent type that produced the governed artifact output.")
    status: Literal["auto_accepted", "ready_for_review", "needs_review", "blocked"] = Field(description="Governance review status for this output batch.")
    artifacts: list[Dict[str, Any]] = Field(default_factory=list, description="Reviewable artifact summaries.")
    errors: list[Dict[str, Any]] = Field(default_factory=list, description="Non-fatal governance finalization errors.")
    dependency_graph: Dict[str, Any] = Field(default_factory=dict, description="Dependency graph refresh summary.")


class ToolEvent(EventModel):
    event_type: Literal["tool_event"] = "tool_event"
    node_id: str = Field(description="Stable node identifier associated with the tool execution.")
    node_type: str = Field(description="Node type or agent type that invoked the tool.")
    tool_name: str = Field(description="Registered tool name.")
    status: Literal["success", "error"] = Field(description="Normalized tool execution result.")
    error_code: str = Field(description="Stable machine-readable error code.")
    duration_ms: int = Field(description="Tool runtime in milliseconds.")
    tool_input: Dict[str, Any] = Field(description="Normalized tool invocation input.")
    tool_output: Dict[str, Any] = Field(description="Normalized tool invocation output.")


class WaitingHumanEvent(EventModel):
    event_type: Literal["waiting_human"] = "waiting_human"
    node_id: str = Field(description="Stable node identifier that is waiting for human input.")
    node_type: str = Field(description="Node type or agent type requesting human input.")
    interrupt_id: str | None = Field(default=None, description="Stable interrupt identifier for precise resume.")
    interaction_id: str | None = Field(default=None, description="Stable interaction identifier for loading structured history and answer forms.")
    interrupt_kind: str | None = Field(default=None, description="Semantic interrupt type, such as ask_human or expert_selection.")
    question: str = Field(description="Human-readable prompt explaining what decision is needed.")
    context: Dict[str, Any] = Field(default_factory=dict, description="Structured context needed to answer the question.")
    resume_target: str = Field(description="Target node or run handle used when resuming execution.")


class RunCompletedEvent(EventModel):
    event_type: Literal["run_completed"] = "run_completed"
    status: Literal["success"] = Field(description="Terminal run status.")


class RunFailedEvent(EventModel):
    event_type: Literal["run_failed"] = "run_failed"
    status: Literal["failed"] = Field(description="Terminal run status.")
    error_message: str = Field(description="Failure reason for the run.")


StructuredEvent = Union[
    NodeStartedEvent,
    NodeCompletedEvent,
    TextDeltaEvent,
    ArtifactUpdatedEvent,
    ArtifactGovernanceReviewableEvent,
    ToolEvent,
    WaitingHumanEvent,
    RunCompletedEvent,
    RunFailedEvent,
]


EVENT_MODEL_BY_TYPE = {
    "node_started": NodeStartedEvent,
    "node_completed": NodeCompletedEvent,
    "text_delta": TextDeltaEvent,
    "artifact_updated": ArtifactUpdatedEvent,
    "artifact_governance_reviewable": ArtifactGovernanceReviewableEvent,
    "tool_event": ToolEvent,
    "waiting_human": WaitingHumanEvent,
    "run_completed": RunCompletedEvent,
    "run_failed": RunFailedEvent,
}


def validate_event_payload(payload: Dict[str, Any]) -> StructuredEvent:
    event_type = payload.get("event_type")
    model_cls = EVENT_MODEL_BY_TYPE.get(event_type)
    if model_cls is None:
        raise ValueError(f"Unsupported event_type: {event_type}")

    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(payload)
    return model_cls.parse_obj(payload)


def dump_event(event: StructuredEvent) -> Dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump()
    return event.dict()
