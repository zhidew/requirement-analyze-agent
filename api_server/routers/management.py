from functools import partial
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import List

import services.orchestrator_service as orch
from models.management import (
    ExpertDependencyValidationResponse,
    ExpertCenterFileNode,
    PhaseOrchestrationResponse,
    ExpertMetadata,
    FileContentResponse,
    SkillMetadata,
    TemplateMetadata,
)
from registry.expert_registry import ExpertRegistry


management_router = APIRouter(
    prefix="/api/v1/management",
    tags=["Management"],
)

expert_center_router = APIRouter(
    prefix="/api/v1/expert-center",
    tags=["Expert Center"],
)


class TemplateUpdateRequest(BaseModel):
    content: str


class AgentUpdateRequest(BaseModel):
    config_yaml: str


class ExpertUpdateRequest(BaseModel):
    profile_yaml: str


class ExpertCreateRequest(BaseModel):
    expert_id: str
    name: str = ""
    name_zh: str = ""
    name_en: str = ""
    description: str = ""
    phase: str = ""  # Target execution phase, e.g. "ARCHITECTURE"


class FileContentUpdateRequest(BaseModel):
    content: str


class PhaseOrchestrationItemRequest(BaseModel):
    id: str
    order: int
    experts: List[str] = []


class PhaseOrchestrationUpdateRequest(BaseModel):
    phases: List[PhaseOrchestrationItemRequest]


@management_router.get("/agents")
async def list_agents():
    return orch.list_agents()


@management_router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = orch.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@management_router.post("/agents/{agent_id}")
async def update_agent(agent_id: str, req: AgentUpdateRequest):
    success = orch.update_agent(agent_id, req.config_yaml)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update agent")
    return {"status": "success", "message": f"Agent {agent_id} updated."}


@management_router.get("/skills", response_model=List[SkillMetadata])
async def list_skills():
    return orch.list_skills()


@management_router.get("/skills/{skill_id}/templates/{template_name}", response_model=TemplateMetadata)
async def get_template(skill_id: str, template_name: str):
    template = orch.get_template(skill_id, template_name)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@management_router.post("/skills/{skill_id}/templates/{template_name}")
async def update_template(skill_id: str, template_name: str, req: TemplateUpdateRequest):
    success = orch.update_template(skill_id, template_name, req.content)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update template")
    return {"status": "success", "message": f"Template {template_name} updated."}


@expert_center_router.get("/experts", response_model=List[ExpertMetadata])
async def list_experts():
    return orch.list_experts()


@expert_center_router.get("/phases")
async def list_phases(executable_only: bool = True):
    payload = orch.get_phase_orchestration()
    phases = payload["phases"]
    if executable_only:
        phases = [phase for phase in phases if phase.get("executable")]
    return phases


@expert_center_router.get("/phase-orchestration", response_model=PhaseOrchestrationResponse)
async def get_phase_orchestration():
    return orch.get_phase_orchestration()


@expert_center_router.put("/phase-orchestration", response_model=PhaseOrchestrationResponse)
async def update_phase_orchestration(req: PhaseOrchestrationUpdateRequest):
    try:
        return orch.update_phase_orchestration([item.model_dump() for item in req.phases])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@expert_center_router.get("/experts/validate-dependencies", response_model=ExpertDependencyValidationResponse)
async def validate_expert_dependencies():
    return orch.validate_expert_dependencies()


@expert_center_router.post("/experts", response_model=ExpertMetadata)
async def create_expert(req: ExpertCreateRequest):
    request_id = uuid4().hex[:8]

    # Validate names for duplicates / similarity
    name_zh = (req.name_zh or "").strip()
    name_en = (req.name_en or "").strip()
    name = (req.name or name_en or name_zh).strip()
    print(
        f"[ExpertCreate:{request_id}] Received create request "
        f"expert_id='{req.expert_id}' name_en='{name_en}' name_zh='{name_zh}' phase='{req.phase or ''}'."
    )

    # Validate phase if provided
    phase = (req.phase or "").strip().upper()
    if phase:
        from config import get_phase_config
        _pcfg = get_phase_config()
        if not _pcfg.is_executable_phase(phase):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid phase '{phase}'. Must be one of: {', '.join(_pcfg.execution_phases)}",
            )

    experts = orch.list_experts()
    for existing in experts:
        existing_names = {
            str(existing.get("name", "")).strip().lower(),
            str(existing.get("name_zh", "")).strip().lower(),
            str(existing.get("name_en", "")).strip().lower(),
        }
        if name_zh and name_zh.lower() in existing_names:
            raise HTTPException(status_code=409, detail=f"Expert name (zh) '{name_zh}' already exists as '{existing['id']}'.")
        if name_en and name_en.lower() in existing_names:
            raise HTTPException(status_code=409, detail=f"Expert name (en) '{name_en}' already exists as '{existing['id']}'.")

    # Similarity check: normalize whitespace/punctuation
    import re as _re
    def _normalize(s: str) -> str:
        return _re.sub(r"[\s\-_.]+", "", s).lower()

    for existing in experts:
        existing_name_norm = _normalize(existing.get("name_en") or existing.get("name", ""))
        if name_en and _normalize(name_en) == existing_name_norm and existing_name_norm:
            raise HTTPException(status_code=409, detail=f"Expert name '{name_en}' is too similar to existing expert '{existing['id']}' (name: '{existing['name']}').")

    # Expert generation performs long-running sync LLM/file work. Run it in the
    # threadpool so one slow create request does not block unrelated API calls.
    print(f"[ExpertCreate:{request_id}] Dispatching expert generation to threadpool.")
    try:
        expert = await run_in_threadpool(
            partial(
                orch.create_expert,
                req.expert_id,
                name,
                req.description,
                name_zh=name_zh,
                name_en=name_en,
                phase=phase,
                request_id=request_id,
            )
        )
    except ValueError as exc:
        print(f"[ExpertCreate:{request_id}] Validation error: {exc}")
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        print(f"[ExpertCreate:{request_id}] Unexpected error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    if not expert:
        print(f"[ExpertCreate:{request_id}] Expert generation returned no result.")
        raise HTTPException(status_code=400, detail="Failed to create expert")
    print(f"[ExpertCreate:{request_id}] Expert generation completed with id='{expert['id']}'.")
    return expert


@expert_center_router.get("/experts/{expert_id}", response_model=ExpertMetadata)
async def get_expert(expert_id: str):
    expert = orch.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail="Expert not found")
    return expert


@expert_center_router.put("/experts/{expert_id}")
async def update_expert(expert_id: str, req: ExpertUpdateRequest):
    success = orch.update_expert(expert_id, req.profile_yaml)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update expert profile")
    return {"status": "success", "message": f"Expert {expert_id} updated."}


@expert_center_router.delete("/experts/{expert_id}")
async def delete_expert(expert_id: str):
    success = orch.delete_expert(expert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Expert not found")
    return {"status": "success", "message": f"Expert {expert_id} deleted."}


@expert_center_router.get("/file-tree", response_model=List[ExpertCenterFileNode])
async def get_file_tree():
    return orch.get_expert_center_tree()


@expert_center_router.get("/files/{path:path}/content", response_model=FileContentResponse)
async def get_file_content(path: str):
    payload = orch.get_file_content(path)
    if not payload:
        raise HTTPException(status_code=404, detail="File not found")
    return payload


@expert_center_router.put("/files/{path:path}/content")
async def update_file_content(path: str, req: FileContentUpdateRequest):
    success = orch.update_file_content(path, req.content)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update file content")
    return {"status": "success", "message": f"File {path} updated."}


@expert_center_router.delete("/files/{path:path}")
async def delete_file(path: str):
    """Delete a file from the expert center.
    
    Only allows deleting files in templates, references, and scripts directories.
    Profile and SKILL.md files cannot be deleted.
    """
    success = orch.delete_file(path)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete file. File may be protected or not found.")
    return {"status": "success", "message": f"File {path} deleted."}


@expert_center_router.post("/reload")
async def reload_experts():
    """Hot-reload all experts from the experts/ directory.
    
    This enables adding new experts without restarting the server:
    1. Add new *.expert.yaml file to experts/ directory
    2. Call POST /api/v1/expert-center/reload
    3. New expert is automatically available in the workflow
    """
    try:
        registry = ExpertRegistry.get_instance()
        registry.reload()
        stats = registry.get_stats()
        return {
            "status": "success",
            "message": f"Reloaded {stats['total_experts']} experts",
            "experts": stats['capabilities'],
            "errors": stats['load_errors'],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload experts: {e}")


@expert_center_router.get("/tools")
async def list_tools():
    """List all system built-in tools."""
    tools = orch.list_system_tools()
    return tools


@expert_center_router.get("/tools/{tool_name}/code")
async def get_tool_code(tool_name: str):
    """Get the implementation code of a specific tool."""
    code = orch.get_tool_code(tool_name)
    if code is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return {"tool_name": tool_name, "code": code}


router = management_router
