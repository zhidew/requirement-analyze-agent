"""Delivery contract helpers for dynamic subagents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registry.expert_runtime_profile import ExpertRuntimeProfile


_LEGACY_DELIVERY_CHECKLIST_MAP = {
    "requirement-clarification": {
        "must_answer": [
            "RR 的业务目标、范围边界、角色、术语和待确认问题是否清楚。",
            "竞品参考中的可借鉴能力、差异点和不适用点是否被标出。",
        ],
        "evidence_expectations": [
            "澄清结论必须回指 RR、竞品参考、上传材料或人工回答。",
            "推断内容必须标注为假设或待确认。",
        ],
    },
    "rules-management": {
        "must_answer": [
            "核心业务规则、计算口径、例外和优先级是否可被实现和验收。",
            "规则冲突、默认处理和可配置参数是否明确。",
        ],
        "evidence_expectations": ["规则编号必须能追踪到 RR、澄清产物或竞品参考。"],
    },
    "business-form-operation": {
        "must_answer": [
            "业务表单对象、字段、CRUD 作业、状态、校验、权限和作业数据分析用途是否清楚。",
            "字段、动作、状态、规则、流程、角色和分析指标之间是否有可追踪关系。",
        ],
        "evidence_expectations": ["字段、动作、权限和分析指标必须引用 RR、规则产物、澄清产物、竞品参考、数据库、代码仓或知识库线索。"],
    },
    "process-control": {
        "must_answer": [
            "主流程、分支、状态流转和异常路径是否完整。",
            "每个流程节点的角色、触发条件和输出是否明确。",
            "哪些流程节点、分支条件、状态迁移、SLA、超时和补偿策略应该作为可配置项。",
        ],
        "evidence_expectations": [
            "流程节点和分支条件应引用 RR、规则或单据产物。",
            "如果项目配置了代码仓、数据库或知识库，现有系统线索必须标注为候选解释或已确认复用依据。",
        ],
    },
    "integration-requirements": {
        "must_answer": [
            "上下游系统、业务责任、触发时机、数据交换和失败处理是否明确。",
            "哪些内容需要后续 IT 接口设计确认。",
        ],
        "evidence_expectations": ["集成场景必须引用 RR、流程、单据字段或竞品参考。"],
    },
    "ir-assembler": {
        "must_answer": [
            "最终 IR 是否能让 SE 开始 IT 设计，并能追踪到 RR 或专家产物。",
            "冲突、缺口、残余假设、验收标准和待确认问题是否清楚。",
        ],
        "evidence_expectations": [
            "每个 IR 条款需要引用 RR、竞品参考、人工澄清或上游专家产物。",
            "缺失、冲突或低置信度内容必须保留为待确认项。",
        ],
    },
    "validator": {
        "must_answer": [
            "IR 是否完整、一致、可追踪且可验收。",
            "哪些问题会阻塞进入 IT 设计。",
        ],
        "evidence_expectations": ["每个问题都要给出文件、条款或证据来源。"],
    },
}


def _normalize_relative_path(raw_path: str) -> str:
    return str(raw_path or "").strip().replace("\\", "/").lstrip("./")


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def _match_review_target_path(configured_path: str, existing_paths: list[str]) -> str:
    normalized = _normalize_relative_path(configured_path)
    if normalized in existing_paths:
        return normalized

    basename = Path(normalized).name
    basename_matches = [path for path in existing_paths if Path(path).name == basename]
    if len(basename_matches) == 1:
        return basename_matches[0]
    return normalized


def build_generic_artifact_review(
    expected_files: list[str],
) -> dict[str, list[str]]:
    artifact_review_checklist: dict[str, list[str]] = {}
    for file_name in expected_files:
        normalized = _normalize_relative_path(file_name)
        suffix = Path(normalized).suffix.lower()
        review_items = [
            "内容不能为空，且结构完整。",
            "命名与其他 IR 产物保持一致。",
            "能回指 RR、竞品参考、人工澄清或已收集证据，而不是纯推测。",
        ]
        if suffix in {".yaml", ".yml", ".json"}:
            review_items.extend(
                [
                    "结构化字段完整，便于 IR 聚合、追踪和后续 IT 设计消费。",
                    "字段语义、确认状态和来源不冲突。",
                ]
            )
        else:
            review_items.extend(
                [
                    "文档章节覆盖目标问题、约束、风险、结论和待确认事项。",
                    "不是只有概念说明，而是包含 BA 可确认、SE 可承接的细节。",
                ]
            )
        artifact_review_checklist[normalized] = review_items
    return artifact_review_checklist


def merge_artifact_review(
    generic_review: dict[str, list[str]],
    configured_review: dict[str, Any],
) -> dict[str, list[str]]:
    merged = {path: list(items) for path, items in (generic_review or {}).items()}
    for raw_path, items in (configured_review or {}).items():
        target_path = _match_review_target_path(str(raw_path or ""), list(merged.keys()))
        configured_items = [str(item).strip() for item in (items or []) if str(item).strip()]
        merged[target_path] = _dedupe_preserve_order(list(merged.get(target_path, [])) + configured_items)
    return merged


def build_delivery_checklist(
    profile: ExpertRuntimeProfile,
    capability: str,
    expected_files: list[str],
) -> dict[str, Any]:
    configured = profile.delivery_contract or {}
    legacy = _LEGACY_DELIVERY_CHECKLIST_MAP.get(
        capability,
        {
            "must_answer": ["该专家负责的核心需求分析问题是否被完整回答。"],
            "evidence_expectations": ["输出必须结构化、可交付，并与证据一致。"],
        },
    )
    generic_review = build_generic_artifact_review(expected_files)
    configured_review = configured.get("artifact_review_checklist") or {}
    return {
        "must_answer": list(configured.get("must_answer") or legacy["must_answer"]),
        "evidence_expectations": list(configured.get("evidence_expectations") or legacy["evidence_expectations"]),
        "artifact_review_checklist": merge_artifact_review(generic_review, configured_review),
    }
