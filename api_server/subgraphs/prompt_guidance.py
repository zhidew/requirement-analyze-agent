"""Prompt guidance helpers for dynamic subagents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registry.expert_runtime_profile import ExpertRuntimeProfile, GENERIC_BOUNDARY_NOTE


_GENERIC_GUIDANCE_BY_SUFFIX = {
    ".md": "覆盖需求结论、边界、约束、风险、待确认项与引用证据。",
    ".json": "保持结构化、可被 IR 聚合和后续 IT 设计稳定消费，字段命名一致。",
    ".yaml": "输出可落地的结构化需求数据，不只给概念性描述。",
    ".yml": "输出可落地的结构化需求数据，不只给概念性描述。",
}

_CAPABILITY_HINTS = {
    "requirement-clarification": "重点回答 RR 目标、范围、角色、术语、假设和待确认问题。",
    "rules-management": "重点回答业务规则、判定条件、计算口径、优先级、例外和参数。",
    "document-operation": "重点回答单据、字段、作业动作、状态、校验和权限。",
    "process-control": "重点回答主流程、分支、状态流转、异常路径和业务时限。",
    "integration-requirements": "重点回答上下游系统、业务事件、数据交换、对账和失败处理。",
    "ir-assembler": "重点回答 IR 聚合、追踪关系、验收标准和待确认问题。",
    "validator": "重点回答 IR 完整性、一致性、可追踪性和可验收性。",
}


def _normalize_relative_path(raw_path: str) -> str:
    return str(raw_path or "").strip().replace("\\", "/").lstrip("./")


def render_boundary_note(profile: ExpertRuntimeProfile, capability: str) -> str:
    if profile.boundary_note:
        return profile.boundary_note
    return GENERIC_BOUNDARY_NOTE if not capability else profile.boundary_note or GENERIC_BOUNDARY_NOTE


def resolve_guidance_for_target(
    profile: ExpertRuntimeProfile,
    target_file: str,
) -> str:
    normalized_target = _normalize_relative_path(target_file)
    basename = Path(normalized_target).name
    prompt_hints = profile.prompt_hints or {}
    file_guidance = dict(prompt_hints.get("file_guidance") or {})

    if normalized_target in file_guidance:
        return str(file_guidance[normalized_target]).strip()

    for configured_path, guidance in file_guidance.items():
        if Path(configured_path).name == basename:
            return str(guidance).strip()

    default_guidance = str(prompt_hints.get("default_file_guidance") or "").strip()
    if default_guidance:
        return default_guidance

    capability_hint = _CAPABILITY_HINTS.get(
        profile.capability,
        "重点回答该专家负责的核心需求分析问题，并确保内容可落地。",
    )
    return f"{_GENERIC_GUIDANCE_BY_SUFFIX.get(Path(normalized_target).suffix.lower(), '输出需完整、结构化、可直接交付。')} {capability_hint}"


def resolve_file_guidance(
    profile: ExpertRuntimeProfile,
    expected_files: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for file_name in expected_files:
        normalized = _normalize_relative_path(file_name)
        rows.append(
            {
                "path": normalized,
                "guidance": resolve_guidance_for_target(profile, normalized),
            }
        )
    return rows
