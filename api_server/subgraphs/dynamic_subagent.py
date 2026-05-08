"""
Dynamic Subagent Execution Module.

This module provides a unified interface for executing subagents based on their
configuration from AgentRegistry. It replaces hardcoded subgraph files with
a configuration-driven approach.

Usage:
    from subgraphs.dynamic_subagent import run_dynamic_subagent
    
    result = await run_dynamic_subagent(
        capability="rules-management",
        state=state,
        base_dir=base_dir,
        ...
    )
"""
from __future__ import annotations

import json
import importlib.util
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from graphs.tools.permissions import DEFAULT_READ_TOOLS, DEFAULT_WRITE_TOOLS, build_effective_tools
from services.llm_service import SubagentOutput, resolve_runtime_llm_settings
from registry.expert_runtime_profile import ExpertRuntimeProfile, resolve_expert_runtime_profile
from subgraphs.artifact_dependencies import (
    discover_upstream_artifacts as _discover_upstream_artifacts_from_profiles,
    get_upstream_artifact_mapping as _get_upstream_artifact_mapping_from_profiles,
)
from subgraphs.delivery_contract import build_delivery_checklist as _build_delivery_checklist_from_profile
from subgraphs.prompt_guidance import (
    render_boundary_note as _render_boundary_note_from_profile,
    resolve_file_guidance as _resolve_file_guidance_from_profile,
)
from subgraphs.topic_ownership import (
    build_default_topic_ownership as _build_default_topic_ownership_from_profiles,
    resolve_topic_ownership as _resolve_topic_ownership_from_profile,
)

if TYPE_CHECKING:
    from registry.agent_registry import AgentFullConfig


MAX_REACT_STEPS = int(os.getenv("AGENT_MAX_REACT_STEPS", "99"))
MAX_ACTIONS_PER_STEP = int(os.getenv("AGENT_MAX_ACTIONS_PER_STEP", "2"))
MAX_FINALIZATION_STEPS = int(os.getenv("AGENT_MAX_FINALIZATION_STEPS", "16"))
REACT_PLATEAU_WINDOW = int(os.getenv("AGENT_REACT_PLATEAU_WINDOW", "4"))
REACT_MIN_STEPS_BEFORE_PLATEAU = int(os.getenv("AGENT_REACT_MIN_STEPS_BEFORE_PLATEAU", "8"))
PATH_NOT_FOUND_REPEAT_LIMIT = int(os.getenv("AGENT_PATH_NOT_FOUND_REPEAT_LIMIT", "2"))
MARKDOWN_BUDGET_TRUNCATION_NOTE = "\n\n> [内容已按控制器字符预算截断；如需更多细节，请重试当前节点并补充范围。]\n"
SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT = (
    "语言要求：所有自然语言输出、推理说明、问题、说明文字、planning_notes、thought、evidence_note、"
    "以及生成的文档正文默认必须使用简体中文。"
    "JSON 键、工具名、文件路径、专家 ID、阶段 ID、标准缩写、代码标识符和协议字段名保持原样，不要翻译。"
)

OUTPUT_CHAR_BUDGET_BY_SUFFIX = {
    ".md": 18000,
    ".json": 12000,
    ".yaml": 14000,
    ".yml": 14000,
    ".sql": 18000,
    ".mmd": 8000,
}

OUTPUT_CHAR_BUDGET_BY_FILE = {
    ("requirement-clarification", "requirement-clarification.md"): 22000,
    ("rules-management", "business-rules.md"): 22000,
    ("business-form-operation", "field-requirements.yaml"): 18000,
    ("process-control", "process-requirements.md"): 22000,
    ("integration-requirements", "integration-requirements.md"): 20000,
    ("ir-assembler", "it-requirements.md"): 30000,
    ("ir-assembler", "requirement-traceability.json"): 20000,
}

OUTPUT_MUST_COVER_LIMIT_BY_SUFFIX = {
    ".md": 5,
    ".json": 4,
    ".yaml": 4,
    ".yml": 4,
    ".sql": 5,
}

OUTPUT_MUST_COVER_LIMIT_BY_FILE = {
    ("ir-assembler", "it-requirements.md"): 7,
    ("ir-assembler", "requirement-traceability.json"): 5,
}

TEXT_ARTIFACT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
    ".csv",
    ".tsv",
}
_SKILL_RUNTIME_MODULE_CACHE: Dict[str, ModuleType] = {}

SHARED_CONTEXT_OWNER_CAPABILITIES = {"requirement-clarification", "ir-assembler"}
GENERIC_SHARED_CONTEXT_HEADING_MARKERS = (
    "背景",
    "概述",
    "概要",
    "目标",
    "范围",
    "总体",
    "综述",
    "overview",
    "summary",
    "scope",
)
GENERIC_SHARED_CONTEXT_SECTION_EXAMPLES = (
    "业务背景 / RR概述 / 竞品参考摘要 / 范围说明 / 目标结果"
)
DEFAULT_SHARED_CONTEXT_TOPICS = [
    "business_background",
    "raw_requirement_overview",
    "competitor_reference_summary",
    "scope",
    "target_outcomes",
]
DEFAULT_CAPABILITY_TOPICS = {
    "requirement-clarification": ["shared_context", "business_goal", "scope_boundary", "glossary", "assumptions", "open_questions"],
    "rules-management": ["business_rules", "decision_conditions", "calculations", "rule_priority", "exceptions", "configurable_parameters"],
    "business-form-operation": ["business_form_objects", "field_requirements", "crud_operations", "operation_actions", "validation_rules", "permissions", "statuses", "operation_data_analysis"],
    "process-control": ["process_flows", "workflow_nodes", "state_transitions", "branch_conditions", "exception_paths", "business_timing"],
    "integration-requirements": ["external_systems", "integration_scenarios", "data_exchange", "business_events", "reconciliation", "failure_handling"],
    "validator": ["consistency_checks", "gap_analysis", "traceability_checks", "acceptance_readiness", "residual_risks"],
    "ir-assembler": ["shared_context", "cross_requirement_alignment", "traceability", "final_ir_package", "acceptance_criteria", "open_questions"],
}

ARCHITECTURE_SCOPE_EXCLUSION_RE = re.compile(
    r"(asyncapi|event\s+contract|event\s+payload|message\s+payload|topic|idempoten|retry\s+policy|"
    r"compensation|sql|ddl|schema|table|index|migration|字段|索引|表结构|迁移|"
    r"config\s+matrix|env\s+var|feature\s+flag|配置矩阵|环境变量|"
    r"deployment|runbook|monitor|alert|sla|部署|运维|监控|告警|"
    r"test\s+case|coverage|chaos|压测|测试用例|覆盖率|混沌)",
    re.IGNORECASE,
)


def build_default_topic_ownership(active_agents: List[str]) -> Dict[str, Any]:
    return _build_default_topic_ownership_from_profiles(active_agents)


def _resolve_topic_ownership(topic_ownership: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _resolve_topic_ownership_from_profile(topic_ownership)


def _owns_shared_context(capability: str, topic_ownership: Optional[Dict[str, Any]] = None) -> bool:
    ownership = _resolve_topic_ownership(topic_ownership)
    return capability in set(ownership["shared_context_owner_capabilities"])


def _shared_context_owner_text(topic_ownership: Optional[Dict[str, Any]] = None) -> str:
    ownership = _resolve_topic_ownership(topic_ownership)
    return ", ".join(ownership["shared_context_owner_capabilities"])


def _capability_topic_text(capability: str, topic_ownership: Optional[Dict[str, Any]] = None) -> str:
    ownership = _resolve_topic_ownership(topic_ownership)
    capability_topics = ownership.get("capability_topics") or {}
    topics = capability_topics.get(capability) or DEFAULT_CAPABILITY_TOPICS.get(capability) or []
    if not topics:
        return ""
    return ", ".join(topics)


def _is_generic_shared_context_heading(heading: str) -> bool:
    normalized = str(heading or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in GENERIC_SHARED_CONTEXT_HEADING_MARKERS)


def _build_shared_context_prompt_block(capability: str, topic_ownership: Optional[Dict[str, Any]] = None) -> str:
    owners = _shared_context_owner_text(topic_ownership)
    owned_topics = _capability_topic_text(capability, topic_ownership)
    owned_topics_line = f"\n- Your owned topics: {owned_topics}." if owned_topics else ""
    if _owns_shared_context(capability, topic_ownership):
        return (
            "Shared Context Ownership:\n"
            f"- You are one of the shared-context owners ({owners}).\n"
            "- Keep shared background, scope, and overall narrative concise.\n"
            "- Write shared context only when it directly helps the current file, and avoid repeating the same background in multiple sections."
            f"{owned_topics_line}"
        )

    generic_examples = _resolve_topic_ownership(topic_ownership)["generic_shared_context_section_examples"]
    return (
        "Shared Context Ownership:\n"
        f"- Shared background, scope, overall requirement overview, and cross-cutting narrative are owned by {owners}.\n"
        "- Your job is to write only the expert-specific delta, decision, contract, constraint, or verification content.\n"
        f"- Do not create standalone sections such as {generic_examples} unless this file explicitly owns them.\n"
        "- If shared context is needed, compress it into one or two bullets and cite the upstream artifact or requirement section instead of restating it.\n"
        "- Do not restate the requirement digest, coverage brief, or upstream artifact text verbatim."
        f"{owned_topics_line}"
    )


def _build_shared_context_digest_section(capability: str, topic_ownership: Optional[Dict[str, Any]] = None) -> List[str]:
    owners = _shared_context_owner_text(topic_ownership)
    owned_topics = _capability_topic_text(capability, topic_ownership)
    owned_topics_line = f"- Owned topics: {owned_topics}." if owned_topics else None
    if _owns_shared_context(capability, topic_ownership):
        return [
            "## Shared Context Handling",
            f"- This capability is allowed to own shared context together with {owners}.",
            "- Keep project background, scope, and overall narrative concise, and avoid repeating them across multiple sections or files.",
            *([owned_topics_line] if owned_topics_line else []),
        ]

    generic_examples = _resolve_topic_ownership(topic_ownership)["generic_shared_context_section_examples"]
    return [
        "## Shared Context Handling",
        f"- Shared background, scope, and overall narrative are owned by {owners}.",
        "- This brief is intentionally trimmed to the expert-specific delta.",
        f"- Do not create standalone sections such as {generic_examples}.",
        "- If you need more context, cite upstream artifacts or read the baseline files directly instead of rewriting the full requirement summary.",
        *([owned_topics_line] if owned_topics_line else []),
    ]

# Default tools available to all subagents
USE_MARKDOWN_UPSERT_TOOL = os.getenv("USE_MARKDOWN_UPSERT_TOOL", "true").lower() in ("true", "1", "yes")


def _tool_is_available(tool_name: str, tools_allowed: List[str]) -> bool:
    return tool_name in set(build_effective_tools(tools_allowed))


def _is_read_tool(tool_name: str) -> bool:
    return tool_name in DEFAULT_READ_TOOLS


def _resolve_effective_tools(
    agent_config: Optional["AgentFullConfig"],
    *,
    allow_unsafe_default: bool = False,
) -> List[str]:
    if not agent_config:
        if allow_unsafe_default:
            return build_effective_tools(["write_file", "patch_file", "run_command", "validate_artifacts"])
        return build_effective_tools([])

    effective_tools = getattr(agent_config, "effective_tools", None)
    if isinstance(effective_tools, list):
        return effective_tools

    return build_effective_tools(getattr(agent_config, "tools_allowed", []))


def _coerce_positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_relative_path(raw_path: str) -> str:
    return raw_path.strip().replace("\\", "/").lstrip("./")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_skill_runtime_module(capability: str) -> Optional[ModuleType]:
    """Load optional deterministic runtime hooks shipped with a skill."""
    safe_capability = _normalize_relative_path(capability)
    if not safe_capability or "/" in safe_capability or "\\" in safe_capability:
        return None
    skill_dir = _repo_root() / "skills" / safe_capability
    candidates = [
        skill_dir / "runtime.py",
        skill_dir / "runtime" / "skill_runtime.py",
        skill_dir / "runtime" / "design_assembly.py",
    ]
    runtime_path = next((path for path in candidates if path.exists() and path.is_file()), None)
    if not runtime_path:
        return None

    cache_key = str(runtime_path)
    cached = _SKILL_RUNTIME_MODULE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    module_name = f"_skill_runtime_{re.sub(r'[^a-zA-Z0-9_]', '_', safe_capability)}"
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SKILL_RUNTIME_MODULE_CACHE[cache_key] = module
    return module


def _call_skill_runtime_hook(
    capability: str,
    hook_name: str,
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    module = _load_skill_runtime_module(capability)
    if module is None:
        return default
    hook = getattr(module, hook_name, None)
    if not callable(hook):
        return default
    return hook(*args, **kwargs)


def _normalize_signature_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"`[^`]+`", "<ref>", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _decision_focus_signature(decision: Dict[str, Any]) -> str:
    thought = _normalize_signature_text(decision.get("thought"))
    note = _normalize_signature_text(decision.get("evidence_note"))
    return " | ".join(part for part in [thought, note] if part)


def _compact_tool_signature_value(value: Any) -> Any:
    if isinstance(value, dict):
        compact: Dict[str, Any] = {}
        for key in sorted(value.keys()):
            if key in {"root_dir", "content"}:
                continue
            compact[key] = _compact_tool_signature_value(value[key])
        return compact
    if isinstance(value, list):
        return [_compact_tool_signature_value(item) for item in value[:4]]
    if isinstance(value, str):
        return value[:160]
    return value


def _tool_execution_signature(
    tool_name: str,
    tool_input: Dict[str, Any],
    tool_result: Dict[str, Any],
) -> str:
    output = dict(tool_result.get("output") or {})
    compact_output = {
        "status": tool_result.get("status"),
        "error_code": tool_result.get("error_code"),
        "path": output.get("path"),
        "project_relative_path": output.get("project_relative_path"),
        "search_hint": output.get("search_hint"),
        "match_count": len(output.get("matches") or []) if isinstance(output.get("matches"), list) else None,
        "files_count": len(output.get("files") or []) if isinstance(output.get("files"), list) else None,
        "error": _compact_tool_signature_value(output.get("error") or {}),
    }
    payload = {
        "tool_name": tool_name,
        "tool_input": _compact_tool_signature_value(tool_input),
        "tool_result": _compact_tool_signature_value(compact_output),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _normalize_expected_output_paths(expected_files: List[str]) -> set[str]:
    normalized: set[str] = set()
    for item in expected_files:
        if isinstance(item, str) and item.strip():
            normalized.add(_normalize_relative_path(item))
    return normalized


def _normalize_output_candidate_list(items: List[str]) -> List[str]:
    normalized: List[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            normalized.append(_normalize_relative_path(item))
    return _dedupe_preserve_order(normalized)


def _match_output_candidate(raw_value: Any, candidate_outputs: List[str]) -> Optional[str]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None

    normalized = _normalize_relative_path(raw_value)
    candidate_set = set(candidate_outputs)
    if normalized in candidate_set:
        return normalized

    basename = Path(normalized).name
    for candidate in candidate_outputs:
        if Path(candidate).name == basename:
            return candidate
    return None


def _default_must_cover_items_for_output(capability: str, target_file: str) -> List[str]:
    runtime_default = _call_skill_runtime_hook(
        capability,
        "default_must_cover_items",
        target_file=target_file,
    )
    if isinstance(runtime_default, list):
        items = [str(item).strip() for item in runtime_default if str(item).strip()]
        if items:
            return items

    basename = Path(_normalize_relative_path(target_file)).name
    suffix = Path(basename).suffix.lower()
    if suffix == ".md":
        return [
            "Key decisions with constraints, risks, and implementation boundaries for this artifact.",
        ]
    if suffix in {".yaml", ".yml", ".json"}:
        return [
            "Required schema/field contract and validation constraints.",
            "Downstream implementation and integration expectations.",
        ]
    if suffix == ".sql":
        return [
            "DDL coverage for required entities/fields/constraints.",
            "Indexing, migration compatibility, and rollback considerations.",
        ]
    return ["Key content that this artifact must answer."]


def _normalize_coverage_contract_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _missing_required_must_cover_groups(
    capability: str,
    target_file: str,
    must_cover_items: List[str],
) -> List[str]:
    basename = Path(_normalize_relative_path(target_file)).name
    rules = _call_skill_runtime_hook(
        capability,
        "required_must_cover_groups",
        target_file=basename,
        default=[],
    )
    if not isinstance(rules, list) or not rules:
        return []

    normalized_items = [_normalize_coverage_contract_text(item) for item in must_cover_items if str(item).strip()]
    missing_labels: List[str] = []
    for rule in rules:
        label = str(rule.get("label") or "required coverage").strip()
        keywords = [
            _normalize_coverage_contract_text(keyword)
            for keyword in (rule.get("keywords") or [])
            if _normalize_coverage_contract_text(keyword)
        ]
        if not keywords:
            continue
        matched = any(keyword in item for keyword in keywords for item in normalized_items)
        if not matched:
            missing_labels.append(label)
    return missing_labels


def _validate_output_plan_coverage(
    *,
    capability: str,
    output_plan: Dict[str, Any],
    candidate_outputs: List[str],
) -> None:
    selected_outputs = _normalize_output_candidate_list(output_plan.get("selected_outputs") or [])
    candidate_outputs = _normalize_output_candidate_list(candidate_outputs)
    must_cover_by_file = output_plan.get("must_cover_by_file")

    if candidate_outputs and not selected_outputs:
        raise ValueError(
            "Output planning coverage contract violated: selected_outputs cannot be empty when candidate outputs exist."
        )

    if not isinstance(must_cover_by_file, dict):
        raise ValueError(
            "Output planning coverage contract violated: must_cover_by_file must be a mapping keyed by selected output path."
        )

    files_without_coverage: List[str] = []
    for target_file in selected_outputs:
        items = must_cover_by_file.get(target_file)
        if not isinstance(items, list):
            files_without_coverage.append(target_file)
            continue

        normalized_items = [str(item).strip() for item in items if str(item).strip()]
        if not normalized_items:
            files_without_coverage.append(target_file)
            continue

        missing_groups = _missing_required_must_cover_groups(capability, target_file, normalized_items)
        if missing_groups:
            missing_text = ", ".join(missing_groups)
            raise ValueError(
                f"Output planning coverage contract violated for `{target_file}`: missing required must_cover dimensions: {missing_text}."
            )

    if files_without_coverage:
        missing_text = ", ".join(sorted(set(files_without_coverage)))
        raise ValueError(
            f"Output planning coverage contract violated: selected outputs must provide non-empty must_cover items. Missing coverage for: {missing_text}."
        )


def _default_output_plan(
    capability: str,
    candidate_outputs: List[str],
    *,
    selected_outputs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    selected = _normalize_output_candidate_list(selected_outputs or candidate_outputs)
    if not selected and candidate_outputs:
        selected = [candidate_outputs[0]]

    skipped = [
        {
            "path": candidate,
            "reason": "Not selected for the current requirement scope.",
        }
        for candidate in candidate_outputs
        if candidate not in set(selected)
    ]
    return {
        "capability": capability,
        "candidate_outputs": list(candidate_outputs),
        "selected_outputs": selected,
        "skipped_outputs": skipped,
        "file_order": list(selected),
        "must_cover_by_file": {
            path: _default_must_cover_items_for_output(capability, path)
            for path in selected
        },
        "evidence_focus": [],
        "planning_notes": "",
    }


def _canonical_legacy_capability(capability: str) -> str:
    return capability


def _resolve_output_char_budget(
    state: Dict[str, Any],
    capability: str,
    target_file: str,
) -> int:
    orchestrator_config = ((state.get("design_context") or {}).get("orchestrator") or {})
    overrides = orchestrator_config.get("output_char_budgets") or {}
    normalized_target = _normalize_relative_path(target_file)
    basename = Path(normalized_target).name
    if isinstance(overrides, dict):
        for key in (normalized_target, basename):
            explicit = _coerce_positive_int(overrides.get(key))
            if explicit is not None:
                return explicit

    explicit = OUTPUT_CHAR_BUDGET_BY_FILE.get((_canonical_legacy_capability(capability), basename))
    if explicit is not None:
        return explicit
    return OUTPUT_CHAR_BUDGET_BY_SUFFIX.get(Path(normalized_target).suffix.lower(), 12000)


def _resolve_must_cover_limit(capability: str, target_file: str) -> int:
    normalized_target = _normalize_relative_path(target_file)
    basename = Path(normalized_target).name
    explicit = OUTPUT_MUST_COVER_LIMIT_BY_FILE.get((_canonical_legacy_capability(capability), basename))
    if explicit is not None:
        return explicit
    return OUTPUT_MUST_COVER_LIMIT_BY_SUFFIX.get(Path(normalized_target).suffix.lower(), 4)


def _scope_boundary_note(
    capability: str,
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    return _render_boundary_note_from_profile(profile, capability)


def _filter_scope_items_for_capability(capability: str, items: List[str]) -> List[str]:
    normalized_items = [str(item).strip() for item in items if str(item).strip()]
    return normalized_items


def _constrain_output_plan(
    capability: str,
    output_plan: Dict[str, Any],
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> Dict[str, Any]:
    normalized = dict(output_plan)
    selected_outputs = _normalize_output_candidate_list(normalized.get("selected_outputs") or [])
    must_cover_by_file = dict(normalized.get("must_cover_by_file") or {})
    constrained_must_cover: Dict[str, List[str]] = {}
    for target_file in selected_outputs:
        items = _filter_scope_items_for_capability(capability, must_cover_by_file.get(target_file) or [])
        constrained_must_cover[target_file] = items[: _resolve_must_cover_limit(capability, target_file)]

    evidence_focus = _filter_scope_items_for_capability(
        capability,
        list(normalized.get("evidence_focus") or []),
    )[:6]

    planning_notes = str(normalized.get("planning_notes") or "").strip()
    boundary_note = _scope_boundary_note(capability, agent_config=agent_config, runtime_profile=runtime_profile)
    if boundary_note not in planning_notes:
        planning_notes = f"{planning_notes} {boundary_note}".strip()

    normalized["selected_outputs"] = selected_outputs
    normalized["file_order"] = [item for item in (normalized.get("file_order") or []) if item in set(selected_outputs)] or list(selected_outputs)
    normalized["must_cover_by_file"] = constrained_must_cover
    normalized["evidence_focus"] = evidence_focus
    normalized["planning_notes"] = planning_notes
    return normalized


def _enforce_markdown_budget(content: str, total_budget: int) -> tuple[str, bool]:
    if total_budget <= 0 or len(content) <= total_budget:
        return content, False

    note = MARKDOWN_BUDGET_TRUNCATION_NOTE
    base_content = content
    if base_content.endswith(note):
        base_content = base_content[: -len(note)].rstrip()

    allowed = max(200, total_budget - len(note))
    trimmed = base_content[:allowed].rstrip()
    return f"{trimmed}{note}", True


def _normalize_markdown_heading_key(heading: str) -> str:
    text = str(heading or "").strip().lower()
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_markdown_heading_titles(content: str) -> List[str]:
    titles: List[str] = []
    for raw_line in str(content or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", raw_line)
        if match:
            title = match.group(1).strip()
            if title:
                titles.append(title)
    return titles


def _normalize_markdown_section_body_text(section_lines: List[str]) -> str:
    if not section_lines:
        return ""
    body_text = "\n".join(section_lines[1:] if len(section_lines) > 1 else section_lines)
    body_text = re.sub(r"^\s*[-*]\s+", "", body_text, flags=re.MULTILINE)
    body_text = re.sub(r"^\s*\d+[.)]\s*", "", body_text, flags=re.MULTILINE)
    body_text = re.sub(r"`[^`]+`", " ", body_text)
    body_text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", body_text)
    return re.sub(r"\s+", " ", body_text).strip().lower()


def _markdown_similarity_ratio(text_a: str, text_b: str) -> float:
    normalized_a = str(text_a or "").strip()
    normalized_b = str(text_b or "").strip()
    if not normalized_a or not normalized_b:
        return 0.0
    if normalized_a == normalized_b:
        return 1.0
    shorter_length = min(len(normalized_a), len(normalized_b))
    if shorter_length >= 80 and (normalized_a in normalized_b or normalized_b in normalized_a):
        return 0.99
    return SequenceMatcher(None, normalized_a[:4000], normalized_b[:4000]).ratio()


def _summarize_markdown_sections_for_prompt(content: str, limit: int = 8) -> List[Dict[str, Any]]:
    section_summaries: List[Dict[str, Any]] = []
    current_heading: Optional[str] = None
    current_lines: List[str] = []

    for raw_line in str(content or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", raw_line)
        if match:
            if current_heading is not None:
                body = _normalize_markdown_section_body_text([f"## {current_heading}", *current_lines])
                section_summaries.append(
                    {
                        "heading": current_heading,
                        "body_summary": _summarize_value_for_prompt(body, max_string=160),
                    }
                )
            current_heading = match.group(1).strip()
            current_lines = []
            continue
        if current_heading is not None:
            current_lines.append(raw_line)

    if current_heading is not None:
        body = _normalize_markdown_section_body_text([f"## {current_heading}", *current_lines])
        section_summaries.append(
            {
                "heading": current_heading,
                "body_summary": _summarize_value_for_prompt(body, max_string=160),
            }
        )

    return section_summaries[:limit]


def _markdown_content_to_upsert_sections(content: str) -> List[Dict[str, Any]]:
    stripped = str(content or "").strip()
    if not stripped or not stripped.startswith("#"):
        return []

    sections: List[Dict[str, Any]] = []
    current_heading: Optional[str] = None
    current_level = 0
    current_lines: List[str] = []

    for raw_line in stripped.splitlines():
        match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", raw_line)
        if match:
            if current_heading is not None:
                sections.append(
                    {
                        "heading": current_heading,
                        "content": "\n".join(current_lines).strip(),
                        "mode": "skip_if_similar",
                        "heading_level": current_level,
                    }
                )
            current_heading = match.group(2).strip()
            current_level = len(match.group(1))
            current_lines = []
            continue
        current_lines.append(raw_line)

    if current_heading is not None:
        sections.append(
            {
                "heading": current_heading,
                "content": "\n".join(current_lines).strip(),
                "mode": "skip_if_similar",
                "heading_level": current_level,
            }
        )

    return [section for section in sections if str(section.get("heading") or "").strip()]


def _should_use_markdown_upsert(state: Dict[str, Any]) -> bool:
    orchestrator_config = ((state.get("design_context") or {}).get("orchestrator") or {})
    explicit_value = orchestrator_config.get("use_markdown_upsert_tool")
    if explicit_value is None:
        return USE_MARKDOWN_UPSERT_TOOL
    if isinstance(explicit_value, bool):
        return explicit_value
    return str(explicit_value).strip().lower() in {"true", "1", "yes"}


def _dedupe_markdown_sections(content: str, existing_content: str = "") -> tuple[str, int]:
    raw_content = str(content or "")
    if not raw_content.strip():
        return raw_content, 0

    lines = raw_content.splitlines()
    preamble: List[str] = []
    sections: List[List[str]] = []
    current_section: List[str] = []
    in_section = False

    for line in lines:
        if re.match(r"^\s*#{1,6}\s+.+$", line):
            if current_section:
                sections.append(current_section)
            current_section = [line]
            in_section = True
            continue
        if in_section:
            current_section.append(line)
        else:
            preamble.append(line)

    if current_section:
        sections.append(current_section)

    if not sections:
        return raw_content, 0

    seen_heading_keys = {_normalize_markdown_heading_key(title) for title in _extract_markdown_heading_titles(existing_content)}
    seen_section_bodies: List[str] = []
    current_section_lines: List[str] = []
    for line in str(existing_content or "").splitlines():
        if re.match(r"^\s*#{1,6}\s+.+$", line):
            if current_section_lines:
                body_text = _normalize_markdown_section_body_text(current_section_lines)
                if body_text:
                    seen_section_bodies.append(body_text)
            current_section_lines = [line]
            continue
        if current_section_lines:
            current_section_lines.append(line)
    if current_section_lines:
        body_text = _normalize_markdown_section_body_text(current_section_lines)
        if body_text:
            seen_section_bodies.append(body_text)

    kept_sections: List[List[str]] = []
    removed_count = 0

    for section_lines in sections:
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", section_lines[0])
        heading_key = _normalize_markdown_heading_key(match.group(1) if match else "")
        body_text = _normalize_markdown_section_body_text(section_lines)
        is_duplicate_heading = bool(heading_key and heading_key in seen_heading_keys)
        is_near_duplicate_body = any(
            _markdown_similarity_ratio(body_text, existing_body) >= 0.9
            for existing_body in seen_section_bodies
            if body_text and existing_body
        )
        if is_duplicate_heading or is_near_duplicate_body:
            removed_count += 1
            continue
        if heading_key:
            seen_heading_keys.add(heading_key)
        if body_text:
            seen_section_bodies.append(body_text)
        kept_sections.append(section_lines)

    rebuilt: List[str] = []
    if preamble and not existing_content.strip():
        rebuilt.extend(preamble)

    for section_lines in kept_sections:
        if rebuilt and rebuilt[-1].strip():
            rebuilt.append("")
        rebuilt.extend(section_lines)

    rebuilt_text = "\n".join(rebuilt).strip()
    if rebuilt_text:
        rebuilt_text += "\n"
    return rebuilt_text, removed_count


def _normalize_output_plan(
    raw_plan: Any,
    *,
    capability: str,
    candidate_outputs: List[str],
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> Dict[str, Any]:
    default_plan = _default_output_plan(capability, candidate_outputs)
    if not isinstance(raw_plan, dict):
        return default_plan

    selected_outputs: List[str] = []
    raw_selected = raw_plan.get("selected_outputs") or []
    if isinstance(raw_selected, list):
        for item in raw_selected:
            if isinstance(item, dict):
                item = item.get("path")
            matched = _match_output_candidate(item, candidate_outputs)
            if matched:
                selected_outputs.append(matched)
    selected_outputs = _normalize_output_candidate_list(selected_outputs)
    if not selected_outputs and candidate_outputs:
        selected_outputs = list(candidate_outputs)

    selected_set = set(selected_outputs)
    skipped_by_path: Dict[str, str] = {}
    raw_skipped = raw_plan.get("skipped_outputs") or []
    if isinstance(raw_skipped, list):
        for item in raw_skipped:
            reason = ""
            path_value: Any = item
            if isinstance(item, dict):
                path_value = item.get("path")
                reason = str(item.get("reason") or "").strip()
            matched = _match_output_candidate(path_value, candidate_outputs)
            if matched and matched not in selected_set:
                skipped_by_path[matched] = reason or "Not selected for the current requirement scope."

    for candidate in candidate_outputs:
        if candidate not in selected_set and candidate not in skipped_by_path:
            skipped_by_path[candidate] = "Not selected for the current requirement scope."

    file_order: List[str] = []
    raw_file_order = raw_plan.get("file_order") or []
    if isinstance(raw_file_order, list):
        for item in raw_file_order:
            matched = _match_output_candidate(item, selected_outputs)
            if matched and matched not in file_order:
                file_order.append(matched)
    for selected in selected_outputs:
        if selected not in file_order:
            file_order.append(selected)

    must_cover_by_file: Dict[str, List[str]] = {}
    raw_must_cover = raw_plan.get("must_cover_by_file") or {}
    if isinstance(raw_must_cover, dict):
        for raw_path, items in raw_must_cover.items():
            matched = _match_output_candidate(raw_path, selected_outputs)
            if not matched:
                continue
            if isinstance(items, list):
                must_cover_by_file[matched] = [
                    str(item).strip()
                    for item in items
                    if str(item).strip()
                ][:12]
    for selected in selected_outputs:
        must_cover_by_file.setdefault(selected, [])

    evidence_focus: List[str] = []
    raw_focus = raw_plan.get("evidence_focus") or []
    if isinstance(raw_focus, list):
        evidence_focus = [str(item).strip() for item in raw_focus if str(item).strip()][:16]

    normalized = {
        "capability": capability,
        "candidate_outputs": list(candidate_outputs),
        "selected_outputs": selected_outputs,
        "skipped_outputs": [
            {"path": path, "reason": reason}
            for path, reason in skipped_by_path.items()
        ],
        "file_order": file_order,
        "must_cover_by_file": must_cover_by_file,
        "evidence_focus": evidence_focus,
        "planning_notes": str(raw_plan.get("planning_notes") or "").strip(),
    }
    return _constrain_output_plan(
        capability,
        normalized,
        agent_config=agent_config,
        runtime_profile=runtime_profile,
    )


def _normalize_react_action(action: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(action, dict):
        return None

    tool_name = str(action.get("tool_name") or "none").strip() or "none"
    tool_input = action.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    return {
        "tool_name": tool_name,
        "tool_input": dict(tool_input),
    }


def _normalize_react_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(decision)
    normalized_actions: List[Dict[str, Any]] = []
    raw_actions = normalized.get("actions")

    if isinstance(raw_actions, list):
        for raw_action in raw_actions[:MAX_ACTIONS_PER_STEP]:
            action = _normalize_react_action(raw_action)
            if action is not None:
                normalized_actions.append(action)

    if not normalized_actions:
        single_action = _normalize_react_action(
            {
                "tool_name": normalized.get("tool_name"),
                "tool_input": normalized.get("tool_input"),
            }
        )
        if single_action is not None and single_action["tool_name"] != "none":
            normalized_actions.append(single_action)

    normalized["actions"] = normalized_actions
    if normalized_actions:
        normalized["tool_name"] = normalized_actions[0]["tool_name"]
        normalized["tool_input"] = dict(normalized_actions[0]["tool_input"])
    else:
        normalized["tool_name"] = "none"
        normalized["tool_input"] = {}

    if isinstance(raw_actions, list) and len(raw_actions) > MAX_ACTIONS_PER_STEP:
        normalized["actions_truncated"] = len(raw_actions) - MAX_ACTIONS_PER_STEP

    if len(normalized_actions) > 1 and any(not _is_read_tool(action["tool_name"]) for action in normalized_actions):
        normalized["actions_restricted_to_single"] = True
        normalized_actions = normalized_actions[:1]
        normalized["actions"] = normalized_actions
        normalized["tool_name"] = normalized_actions[0]["tool_name"]
        normalized["tool_input"] = dict(normalized_actions[0]["tool_input"])

    return normalized


def _normalize_human_option(option: Any, fallback_value: str) -> Optional[Dict[str, str]]:
    if isinstance(option, dict):
        value = str(option.get("value") or option.get("label") or fallback_value).strip()
        label = str(option.get("label") or value).strip()
        description = str(option.get("description") or "").strip()
    else:
        value = str(option).strip()
        label = value
        description = ""

    if not value:
        return None
    return {
        "value": value,
        "label": label or value,
        "description": description,
    }


def _question_type_supports_options(question_type: str) -> bool:
    return question_type in {"single_select", "multi_select"}


def _select_default_question_type(supported_question_types: List[str]) -> str:
    for candidate in ("single_select", "multi_select"):
        if candidate in supported_question_types:
            return candidate
    return supported_question_types[0] if supported_question_types else "single_select"


def _normalize_human_options_with_other(raw_options: Any) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    if isinstance(raw_options, list):
        for index, raw_option in enumerate(raw_options):
            option = _normalize_human_option(raw_option, f"option_{index + 1}")
            if option is not None:
                options.append(option)

    if not any(option["value"] == "other" or option["label"] == "其他" for option in options):
        options.append(
            {
                "value": "other",
                "label": "其他",
                "description": "选择此项后，可在补充说明中填写未覆盖的情况。",
            }
        )
    return options


def _action_targets_final_artifact(
    action: Dict[str, Any],
    expected_files: List[str],
) -> Optional[str]:
    return _decision_targets_final_artifact(action, expected_files)


def _decision_targets_final_artifact(
    decision: Dict[str, Any],
    expected_files: List[str],
) -> Optional[str]:
    tool_name = str(decision.get("tool_name") or "").strip()
    if tool_name not in DEFAULT_WRITE_TOOLS:
        return None

    tool_input = dict(decision.get("tool_input") or {})
    raw_path = tool_input.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    normalized_path = _normalize_relative_path(raw_path)
    expected_paths = _normalize_expected_output_paths(expected_files)
    expected_basenames = {Path(path).name for path in expected_paths}
    if normalized_path in expected_paths or Path(normalized_path).name in expected_basenames:
        return normalized_path
    return None


def _resolve_candidate_files(payload: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    project_layout = payload.get("project_layout") or {}
    baseline_dir = str(project_layout.get("baseline_dir") or "baseline").strip("/\\") or "baseline"

    explicit_candidates = payload.get("candidate_files") or []
    for raw_path in explicit_candidates:
        if isinstance(raw_path, str) and raw_path.strip():
            candidates.append(_normalize_relative_path(raw_path))

    tool_context = payload.get("tool_context") or {}
    list_files_output = tool_context.get("list_files") or {}
    list_files_root = str(list_files_output.get("root_dir") or "")
    use_baseline_prefix = (
        list_files_root == baseline_dir
        or list_files_root.replace("\\", "/").endswith(f"/{baseline_dir}")
    )
    for file_info in list_files_output.get("files") or []:
        raw_path = file_info.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        normalized = _normalize_relative_path(raw_path)
        if use_baseline_prefix and not normalized.startswith(f"{baseline_dir}/"):
            normalized = f"{baseline_dir}/{normalized}"
        candidates.append(normalized)

    for raw_path in payload.get("uploaded_files") or []:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        normalized = _normalize_relative_path(raw_path)
        if "/" not in normalized and not normalized.startswith(f"{baseline_dir}/"):
            normalized = f"{baseline_dir}/{normalized}"
        candidates.append(normalized)

    filtered = [
        path for path in _dedupe_preserve_order(candidates)
        if path.endswith((".md", ".txt", ".json", ".yaml", ".yml"))
    ]
    if filtered:
        return filtered
    return [f"{baseline_dir}/raw-requirements.md"]


def _get_runtime_project_root(payload: Dict[str, Any]) -> Optional[Path]:
    raw_value = payload.get("_runtime_project_root")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    return Path(raw_value)


def _resolve_explicit_max_react_steps(state: Dict[str, Any]) -> Optional[int]:
    orchestrator_config = ((state.get("design_context") or {}).get("orchestrator") or {})
    return _coerce_positive_int(orchestrator_config.get("max_react_steps"))


def _resolve_explicit_max_finalization_steps(state: Dict[str, Any]) -> Optional[int]:
    orchestrator_config = ((state.get("design_context") or {}).get("orchestrator") or {})
    return _coerce_positive_int(orchestrator_config.get("max_finalization_steps"))


def _estimate_react_budget(
    *,
    state: Dict[str, Any],
    payload: Dict[str, Any],
    expected_files: List[str],
    agent_config: Optional["AgentFullConfig"],
    upstream_artifacts: Dict[str, List[str]],
    default_value: int,
) -> int:
    if default_value != MAX_REACT_STEPS:
        return max(1, int(default_value))

    explicit_override = _resolve_explicit_max_react_steps(state)
    if explicit_override is not None:
        return explicit_override

    # ReAct budget is now a single global cap instead of per-expert tuning.
    return MAX_REACT_STEPS


def _estimate_finalization_budget(
    *,
    state: Dict[str, Any],
    expected_files: List[str],
    default_value: int = MAX_FINALIZATION_STEPS,
) -> int:
    explicit_override = _resolve_explicit_max_finalization_steps(state)
    if explicit_override is not None:
        return explicit_override

    return max(1, max(default_value, len(expected_files) * 3))


def _relativize_path_for_prompt(raw_value: str, project_root: Optional[Path]) -> str:
    normalized = raw_value.strip()
    if not normalized:
        return normalized
    if project_root is None:
        return normalized
    try:
        candidate = Path(normalized).expanduser()
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(project_root.resolve()).as_posix() or "."
            except ValueError:
                return normalized
    except (OSError, RuntimeError, ValueError):
        return normalized
    return normalized


def _sanitize_prompt_payload(value: Any, project_root: Optional[Path], *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_prompt_payload(item_value, project_root, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_prompt_payload(item, project_root) for item in value]
    if isinstance(value, str):
        if key == "root_dir":
            return "."
        return _relativize_path_for_prompt(value, project_root)
    return value


def _workspace_relative_dir(capability: str) -> str:
    return f"_work/{capability}"


def _extract_markdown_sections(requirement_text: str) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    current_heading = "Overview"
    current_level = 1
    current_lines: List[str] = []

    for raw_line in requirement_text.splitlines():
        line = raw_line.rstrip()
        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
        if heading_match:
            body = "\n".join(current_lines).strip()
            if body:
                sections.append(
                    {
                        "heading": current_heading,
                        "level": current_level,
                        "body": body,
                    }
                )
            current_heading = heading_match.group(2).strip()
            current_level = len(heading_match.group(1))
            current_lines = []
            continue
        current_lines.append(line)

    body = "\n".join(current_lines).strip()
    if body:
        sections.append(
            {
                "heading": current_heading,
                "level": current_level,
                "body": body,
            }
        )
    return sections


def _extract_bullet_items(section_body: str, max_items: int = 8) -> List[str]:
    bullet_items: List[str] = []
    for raw_line in section_body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[-*]\s+", line) or re.match(r"^\d+[.)、]\s*", line):
            bullet_items.append(line)
        if len(bullet_items) >= max_items:
            break
    return bullet_items


def _capability_keywords(
    capability: str,
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> List[str]:
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    return list(profile.routing_keywords)


def _matched_routing_keywords(
    text: str,
    capability: str,
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> List[str]:
    haystack = str(text or "").lower()
    matches: List[str] = []
    for keyword in _capability_keywords(
        capability,
        agent_config=agent_config,
        runtime_profile=runtime_profile,
    ):
        normalized = str(keyword or "").strip()
        if normalized and normalized.lower() in haystack and normalized not in matches:
            matches.append(normalized)
    return matches


def _score_section_for_capability(
    section: Dict[str, Any],
    capability: str,
    expected_files: List[str],
    topic_ownership: Optional[Dict[str, Any]] = None,
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> int:
    heading = str(section.get("heading") or "")
    body = str(section.get("body") or "")
    combined = f"{heading}\n{body}"
    score = 0

    priority_headings = ["强制设计约束", "非功能要求", "风险关注点", "期望设计结论", "指定设计输出要求"]
    for marker in priority_headings:
        if marker in heading:
            score += 4

    if not _owns_shared_context(capability, topic_ownership) and _is_generic_shared_context_heading(heading):
        score -= 3

    for keyword in _capability_keywords(
        capability,
        agent_config=agent_config,
        runtime_profile=runtime_profile,
    ):
        if keyword and keyword.lower() in combined.lower():
            score += 2

    for file_name in expected_files:
        stem = Path(file_name).stem
        if stem and stem.lower() in combined.lower():
            score += 1

    return score


def _select_focus_sections(
    requirement_text: str,
    capability: str,
    expected_files: List[str],
    *,
    max_sections: int = 6,
    topic_ownership: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> List[Dict[str, Any]]:
    sections = _extract_markdown_sections(requirement_text)
    if not sections:
        return []

    scored_sections = sorted(
        (
            {
                **section,
                "score": _score_section_for_capability(
                    section,
                    capability,
                    expected_files,
                    topic_ownership,
                    agent_config=agent_config,
                    runtime_profile=runtime_profile,
                ),
                "matched_keywords": _matched_routing_keywords(
                    f"{section.get('heading') or ''}\n{section.get('body') or ''}",
                    capability,
                    agent_config=agent_config,
                    runtime_profile=runtime_profile,
                ),
            }
            for section in sections
        ),
        key=lambda item: item["score"],
        reverse=True,
    )

    selected = [section for section in scored_sections if section["score"] > 0][:max_sections]
    if not selected:
        selected = sections[:max_sections]
    return selected


def _build_expected_file_guidance(
    capability: str,
    expected_files: List[str],
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> List[Dict[str, str]]:
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    return _resolve_file_guidance_from_profile(profile, expected_files)


def _build_capability_delivery_checklist(
    capability: str,
    expected_files: List[str],
    *,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> Dict[str, Any]:
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    return _build_delivery_checklist_from_profile(profile, capability, expected_files)


def _build_coverage_brief(
    payload: Dict[str, Any],
    capability: str,
    candidate_files: List[str],
    expected_files: List[str],
    *,
    candidate_output_files: Optional[List[str]] = None,
    output_plan: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> Dict[str, Any]:
    requirement_text = str(payload.get("requirement") or "").strip()
    topic_ownership = payload.get("topic_ownership") if isinstance(payload.get("topic_ownership"), dict) else None
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    focus_sections = _select_focus_sections(
        requirement_text,
        capability,
        expected_files,
        topic_ownership=topic_ownership,
        agent_config=agent_config,
        runtime_profile=profile,
    )
    candidate_output_files = _normalize_output_candidate_list(candidate_output_files or expected_files)
    output_plan = output_plan or _default_output_plan(
        capability,
        candidate_output_files,
        selected_outputs=expected_files,
    )

    def _find_section_items(marker: str, max_items: int = 6) -> List[str]:
        for section in _extract_markdown_sections(requirement_text):
            if marker in str(section.get("heading") or ""):
                items = _extract_bullet_items(str(section.get("body") or ""), max_items=max_items)
                if items:
                    return items
        return []

    return {
        "capability": capability,
        "candidate_files": candidate_files,
        "candidate_output_files": candidate_output_files,
        "expected_files": expected_files,
        "selected_outputs": list(output_plan.get("selected_outputs") or expected_files),
        "skipped_outputs": list(output_plan.get("skipped_outputs") or []),
        "file_order": list(output_plan.get("file_order") or expected_files),
        "must_cover_by_file": dict(output_plan.get("must_cover_by_file") or {}),
        "evidence_focus": list(output_plan.get("evidence_focus") or []),
        "planning_notes": str(output_plan.get("planning_notes") or ""),
        "expected_file_guidance": _build_expected_file_guidance(
            capability,
            expected_files,
            agent_config=agent_config,
            runtime_profile=profile,
        ),
        "delivery_checklist": _build_capability_delivery_checklist(
            capability,
            expected_files,
            agent_config=agent_config,
            runtime_profile=profile,
        ),
        "routing_debug": {
            "source": str((profile.source_map or {}).get("routing_keywords") or ""),
            "keywords": list(profile.routing_keywords),
            "matched_keywords": _matched_routing_keywords(
                requirement_text,
                capability,
                agent_config=agent_config,
                runtime_profile=profile,
            ),
        },
        "focus_sections": [
            {
                "heading": section.get("heading"),
                "score": section.get("score"),
                "matched_keywords": list(section.get("matched_keywords") or []),
                "must_cover_points": _extract_bullet_items(str(section.get("body") or ""), max_items=6),
                "excerpt": _summarize_value_for_prompt(str(section.get("body") or ""), max_string=600),
            }
            for section in focus_sections
        ],
        "hard_constraints": _find_section_items("强制设计约束", max_items=8),
        "non_functional_requirements": _find_section_items("非功能要求", max_items=8),
        "risks": _find_section_items("风险关注点", max_items=8),
        "target_outcomes": _find_section_items("期望设计结论", max_items=8),
    }


def _build_requirement_digest(
    payload: Dict[str, Any],
    candidate_files: List[str],
    capability: str,
    expected_files: List[str],
    *,
    candidate_output_files: Optional[List[str]] = None,
    output_plan: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    requirement_text = str(payload.get("requirement") or "").strip()
    topic_ownership = payload.get("topic_ownership") if isinstance(payload.get("topic_ownership"), dict) else None
    is_shared_context_owner = _owns_shared_context(capability, topic_ownership)
    coverage_brief = _build_coverage_brief(
        payload,
        capability,
        candidate_files,
        expected_files,
        candidate_output_files=candidate_output_files,
        output_plan=output_plan,
        agent_config=agent_config,
        runtime_profile=runtime_profile,
    )
    structure_entries = ((payload.get("tool_context") or {}).get("extract_structure") or {}).get("files") or []
    headings: List[str] = []
    for entry in structure_entries:
        if not isinstance(entry, dict):
            continue
        for heading in entry.get("headings") or []:
            if isinstance(heading, str) and heading.strip():
                headings.append(heading.strip())

    lines = [
        "# Requirement Digest" if is_shared_context_owner else "# Expert Delta Brief",
        "",
        f"- Project: {payload.get('project_id') or payload.get('project_name') or 'unknown'}",
        f"- Version: {payload.get('version') or 'unknown'}",
    ]

    active_agents = [agent for agent in payload.get("active_agents") or [] if isinstance(agent, str) and agent.strip()]
    if active_agents:
        lines.append(f"- Active agents: {', '.join(active_agents)}")
    if candidate_files:
        lines.append(f"- Baseline files: {', '.join(candidate_files[:10])}")
    candidate_output_files = coverage_brief.get("candidate_output_files") or []
    if candidate_output_files:
        lines.append(f"- Candidate outputs: {', '.join(candidate_output_files)}")
    if expected_files:
        lines.append(f"- Selected outputs: {', '.join(expected_files)}")

    lines.extend(["", *_build_shared_context_digest_section(capability, topic_ownership)])

    skipped_outputs = coverage_brief.get("skipped_outputs") or []
    if skipped_outputs:
        lines.extend(["", "## Skipped Candidate Outputs"])
        for row in skipped_outputs:
            if isinstance(row, dict) and row.get("path"):
                lines.append(f"- {row['path']}: {row.get('reason') or 'Not selected for this run.'}")

    file_guidance = coverage_brief.get("expected_file_guidance") or []
    if file_guidance:
        lines.extend(
            [
                "",
                "## Output Targets",
                *[
                    f"- {row['path']}: {row['guidance']}"
                    for row in file_guidance
                    if isinstance(row, dict) and row.get("path") and row.get("guidance")
                ],
            ]
        )

    file_order = coverage_brief.get("file_order") or []
    must_cover_by_file = coverage_brief.get("must_cover_by_file") or {}
    evidence_focus = coverage_brief.get("evidence_focus") or []
    planning_notes = str(coverage_brief.get("planning_notes") or "").strip()
    if file_order or must_cover_by_file or evidence_focus or planning_notes:
        lines.append("")
        lines.append("## Output Plan")
        if file_order:
            lines.append(f"- File order: {', '.join(file_order)}")
        if evidence_focus:
            lines.extend(f"- Evidence focus: {item}" for item in evidence_focus)
        if planning_notes:
            lines.append(f"- Planning notes: {planning_notes}")
        for file_name in file_order:
            items = must_cover_by_file.get(file_name) or []
            if not items:
                continue
            lines.append(f"### {file_name}")
            lines.extend(f"- {item}" for item in items[:10])

    delivery_checklist = coverage_brief.get("delivery_checklist") or {}
    if delivery_checklist:
        must_answer = delivery_checklist.get("must_answer") or []
        evidence_expectations = delivery_checklist.get("evidence_expectations") or []
        artifact_review_checklist = delivery_checklist.get("artifact_review_checklist") or {}
        if must_answer:
            lines.extend(["", "## Expert Must Answer", *[f"- {item}" for item in must_answer]])
        if evidence_expectations:
            lines.extend(["", "## Evidence Expectations", *[f"- {item}" for item in evidence_expectations]])
        if artifact_review_checklist:
            lines.append("")
            lines.append("## Artifact Review Checklist")
            for file_name, items in artifact_review_checklist.items():
                lines.append(f"### {file_name}")
                lines.extend(f"- {item}" for item in items[:8])

    if headings and is_shared_context_owner:
        lines.extend(
            [
                "",
                "## Outline",
                *[f"- {heading}" for heading in headings[:25]],
            ]
        )
        if len(headings) > 25:
            lines.append(f"- ... ({len(headings) - 25} more headings omitted)")

    focus_sections = (coverage_brief.get("focus_sections") or [])[: (6 if is_shared_context_owner else 3)]
    if focus_sections:
        lines.extend(["", "## Must-Cover Sections"])
        for section in focus_sections:
            heading = section.get("heading") or "Unknown Section"
            lines.append(f"### {heading}")
            matched_keywords = [str(item).strip() for item in (section.get("matched_keywords") or []) if str(item).strip()]
            if matched_keywords:
                lines.append(f"- Routing keyword hits: {', '.join(matched_keywords)}")
            points = section.get("must_cover_points") or []
            if points:
                lines.extend(f"- {point}" for point in points)
            excerpt = str(section.get("excerpt") or "").strip()
            if excerpt and is_shared_context_owner:
                lines.append(excerpt)

    for title, key in (
        ("Hard Constraints", "hard_constraints"),
        ("Non-Functional Requirements", "non_functional_requirements"),
        ("Risks", "risks"),
        ("Target Outcomes", "target_outcomes"),
    ):
        items = coverage_brief.get(key) or []
        if items:
            lines.extend(["", f"## {title}", *[f"- {item}" for item in items]])

    routing_debug = coverage_brief.get("routing_debug") or {}
    routing_keywords = [str(item).strip() for item in (routing_debug.get("keywords") or []) if str(item).strip()]
    if routing_keywords:
        matched_keywords = [str(item).strip() for item in (routing_debug.get("matched_keywords") or []) if str(item).strip()]
        lines.extend(
            [
                "",
                "## Routing Debug",
                f"- Keyword source: {routing_debug.get('source') or 'unknown'}",
                f"- Configured keywords: {', '.join(routing_keywords)}",
                f"- Matched keywords in requirement: {', '.join(matched_keywords) if matched_keywords else '(none)'}",
            ]
        )

    if requirement_text and is_shared_context_owner:
        excerpt_limit = 2400
        excerpt = requirement_text[:excerpt_limit]
        if len(requirement_text) > excerpt_limit:
            excerpt = f"{excerpt}\n...[truncated {len(requirement_text) - excerpt_limit} chars]"
        lines.extend(
            [
                "",
                "## Requirement Excerpt",
                excerpt,
            ]
        )
    elif is_shared_context_owner:
        lines.extend(
            [
                "",
                "## Requirement Excerpt",
                "(No inline requirement text found. Read the baseline files if more detail is needed.)",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def _collect_artifact_status(
    artifacts_dir: Path,
    expected_files: List[str],
) -> List[Dict[str, Any]]:
    status_rows: List[Dict[str, Any]] = []
    for file_name in expected_files:
        artifact_path = artifacts_dir / file_name
        exists = artifact_path.exists() and artifact_path.is_file()
        size_bytes = artifact_path.stat().st_size if exists else 0
        status_rows.append(
            {
                "path": _normalize_relative_path(file_name),
                "exists": exists,
                "size_bytes": size_bytes,
            }
        )
    return status_rows


def _ordered_selected_outputs(output_plan: Dict[str, Any], expected_files: List[str]) -> List[str]:
    ordered: List[str] = []
    for item in output_plan.get("file_order") or []:
        matched = _match_output_candidate(item, expected_files)
        if matched and matched not in ordered:
            ordered.append(matched)
    for item in expected_files:
        if item not in ordered:
            ordered.append(item)
    return ordered


def _all_expected_artifacts_complete(
    artifacts_dir: Path,
    expected_files: List[str],
) -> bool:
    for file_name in expected_files:
        artifact_path = artifacts_dir / file_name
        if not artifact_path.exists() or not artifact_path.is_file():
            return False
        if not artifact_path.read_text(encoding="utf-8").strip():
            return False
    return True


def _persist_workspace_snapshot(
    *,
    project_path: Path,
    payload: Dict[str, Any],
    capability: str,
    candidate_files: List[str],
    candidate_output_files: List[str],
    expected_files: List[str],
    output_plan: Dict[str, Any],
    observations: List[Dict[str, Any]],
    react_trace: List[Dict[str, Any]],
    upstream_artifacts: Dict[str, List[str]],
    artifacts_dir: Path,
    work_dir: Path,
    final_trace: Optional[List[Dict[str, Any]]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
) -> Dict[str, str]:
    final_trace = final_trace or []
    project_root = project_path
    work_dir.mkdir(parents=True, exist_ok=True)

    requirement_digest_path = work_dir / "requirement-digest.md"
    coverage_brief_path = work_dir / "coverage-brief.json"
    output_plan_path = work_dir / "output-plan.json"
    observations_path = work_dir / "grounded-observations.jsonl"
    observations_summary_path = work_dir / "grounded-observations-summary.json"
    react_trace_path = work_dir / "react-trace.json"
    final_trace_path = work_dir / "finalization-trace.json"
    assembly_plan_path = work_dir / "assembly-plan.json"
    workspace_index_path = work_dir / "workspace-index.json"

    requirement_digest_path.write_text(
        _build_requirement_digest(
            payload,
            candidate_files,
            capability,
            expected_files,
            candidate_output_files=candidate_output_files,
            output_plan=output_plan,
            agent_config=agent_config,
        ),
        encoding="utf-8",
    )
    coverage_brief = _build_coverage_brief(
        payload,
        capability,
        candidate_files,
        expected_files,
        candidate_output_files=candidate_output_files,
        output_plan=output_plan,
        agent_config=agent_config,
    )
    coverage_brief_path.write_text(
        json.dumps(
            coverage_brief,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    output_plan_path.write_text(
        json.dumps(_sanitize_prompt_payload(output_plan, project_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with observations_path.open("w", encoding="utf-8") as handle:
        for observation in observations:
            sanitized = _sanitize_prompt_payload(observation, project_root)
            handle.write(json.dumps(sanitized, ensure_ascii=False) + "\n")

    observations_summary = _compact_observations_for_prompt(
        observations,
        capability,
        "final",
        project_root=project_root,
    )
    observations_summary_path.write_text(
        json.dumps(observations_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    react_trace_path.write_text(
        json.dumps(_sanitize_prompt_payload(react_trace, project_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    final_trace_path.write_text(
        json.dumps(_sanitize_prompt_payload(final_trace, project_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    actual_workspace_artifacts = _build_actual_workspace_artifact_summaries(
        artifacts_dir,
        upstream_artifacts,
        expected_files,
    )
    assembly_plan = _build_skill_workspace_plan(
        capability=capability,
        actual_workspace_artifacts=actual_workspace_artifacts,
        output_plan=output_plan,
        coverage_brief=coverage_brief,
    )
    assembly_plan_rel_path: Optional[str] = None
    if assembly_plan.get("status") != "not_applicable":
        assembly_plan_path.write_text(
            json.dumps(_sanitize_prompt_payload(assembly_plan, project_root), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        assembly_plan_rel_path = _normalize_relative_path(str(assembly_plan_path.relative_to(artifacts_dir)))

    workspace_index = {
        "capability": capability,
        "candidate_files": candidate_files,
        "candidate_output_files": candidate_output_files,
        "expected_files": expected_files,
        "selected_outputs": list(output_plan.get("selected_outputs") or expected_files),
        "skipped_outputs": list(output_plan.get("skipped_outputs") or []),
        "requirement_digest_path": _normalize_relative_path(str(requirement_digest_path.relative_to(artifacts_dir))),
        "coverage_brief_path": _normalize_relative_path(str(coverage_brief_path.relative_to(artifacts_dir))),
        "output_plan_path": _normalize_relative_path(str(output_plan_path.relative_to(artifacts_dir))),
        "grounded_observations_path": _normalize_relative_path(str(observations_path.relative_to(artifacts_dir))),
        "grounded_observations_summary_path": _normalize_relative_path(str(observations_summary_path.relative_to(artifacts_dir))),
        "react_trace_path": _normalize_relative_path(str(react_trace_path.relative_to(artifacts_dir))),
        "finalization_trace_path": _normalize_relative_path(str(final_trace_path.relative_to(artifacts_dir))),
        "upstream_artifacts": upstream_artifacts,
        "actual_workspace_artifacts": actual_workspace_artifacts,
        "current_expected_artifacts": _collect_artifact_status(artifacts_dir, expected_files),
        "observation_count": len(observations),
        "react_step_count": len(react_trace),
        "finalization_step_count": len(final_trace),
    }
    if assembly_plan_rel_path:
        workspace_index["assembly_plan_path"] = assembly_plan_rel_path
    workspace_index_path.write_text(
        json.dumps(_sanitize_prompt_payload(workspace_index, project_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    workspace_paths = {
        "requirement_digest": _normalize_relative_path(str(requirement_digest_path.relative_to(artifacts_dir))),
        "coverage_brief": _normalize_relative_path(str(coverage_brief_path.relative_to(artifacts_dir))),
        "output_plan": _normalize_relative_path(str(output_plan_path.relative_to(artifacts_dir))),
        "grounded_observations": _normalize_relative_path(str(observations_path.relative_to(artifacts_dir))),
        "grounded_observations_summary": _normalize_relative_path(str(observations_summary_path.relative_to(artifacts_dir))),
        "workspace_index": _normalize_relative_path(str(workspace_index_path.relative_to(artifacts_dir))),
        "react_trace": _normalize_relative_path(str(react_trace_path.relative_to(artifacts_dir))),
        "finalization_trace": _normalize_relative_path(str(final_trace_path.relative_to(artifacts_dir))),
    }
    if assembly_plan_rel_path:
        workspace_paths["assembly_plan"] = assembly_plan_rel_path
    return workspace_paths


def _write_finalization_step_log(
    *,
    logs_dir: Path,
    capability: str,
    step: int,
    decision: Dict[str, Any],
    artifact_status: List[Dict[str, Any]],
    workspace_paths: Dict[str, str],
    project_root: Path,
    tool_results: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    finalization_dir = logs_dir / "finalization" / capability
    finalization_dir.mkdir(parents=True, exist_ok=True)
    log_path = finalization_dir / f"step-{step:02d}.json"
    payload = {
        "step": step,
        "decision": _sanitize_prompt_payload(decision, project_root),
        "artifact_status": _sanitize_prompt_payload(artifact_status, project_root),
        "workspace_paths": _sanitize_prompt_payload(workspace_paths, project_root),
        "tool_results": _sanitize_prompt_payload(tool_results or [], project_root),
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return log_path


CORE_TOOL_DESCRIPTIONS = {
    "list_files": "Inspect project directories and files from the current project root.",
    "read_file_chunk": "Read a file slice by path and line range.",
    "grep_search": "Search grounded text across project files.",
    "extract_structure": "Summarize document or file structure before deeper reads.",
    "extract_lookup_values": "Extract enumerations or repeated structured values from files.",
    "write_file": "Create or fully overwrite an artifact under `artifacts/`.",
    "append_file": "Append raw content to the end of an artifact under `artifacts/`.",
    "upsert_markdown_sections": "Insert or replace markdown sections by heading while deduping similar content.",
    "patch_file": "Apply a bounded replacement to an existing artifact under `artifacts/`.",
    "run_command": "Run a shell command from project root when explicitly permitted.",
    "validate_artifacts": "Run deterministic validation checks against generated files under `artifacts/`.",
}


def _build_available_tool_section(tools_allowed: List[str]) -> str:
    ordered_tools = [
        "list_files",
        "read_file_chunk",
        "grep_search",
        "extract_structure",
        "extract_lookup_values",
        "write_file",
        "append_file",
        "upsert_markdown_sections",
        "patch_file",
        "run_command",
        "validate_artifacts",
    ]
    tool_lines = [
        f"- {tool_name} ({CORE_TOOL_DESCRIPTIONS[tool_name]})"
        for tool_name in ordered_tools
        if _tool_is_available(tool_name, tools_allowed)
    ]
    return "Available tools:\n" + "\n".join(tool_lines)


def _build_asset_tool_section(tools_allowed: List[str], configured_assets: Dict[str, Any] | None) -> str:
    configured_assets = configured_assets or {}
    asset_lines: List[str] = []

    if configured_assets.get("repositories") and _tool_is_available("clone_repository", tools_allowed):
        asset_lines.append("- clone_repository (Clone or update a project-shared repository cache for grounded code inspection; reuse the returned `project_relative_path`/`search_hint` in later `repos_dir` lookups)")
    if configured_assets.get("databases") and _tool_is_available("query_database", tools_allowed):
        asset_lines.append("- query_database (Inspect configured database schemas or run read-only SQL)")
    if configured_assets.get("knowledge_bases") and _tool_is_available("query_knowledge_base", tools_allowed):
        asset_lines.append("- query_knowledge_base (Search configured knowledge bases for terms, feature trees, and design docs)")

    if not asset_lines:
        return ""

    return f"""
Asset-aware tools:
{chr(10).join(asset_lines)}
"""


def _get_configured_asset_items(configured_assets: Dict[str, Any] | None, asset_kind: str) -> List[Dict[str, Any]]:
    configured_assets = configured_assets or {}
    bucket = configured_assets.get(asset_kind) or {}
    items = bucket.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _format_asset_examples(configured_assets: Dict[str, Any] | None) -> str:
    configured_assets = configured_assets or {}
    sections: List[str] = []

    repositories = _get_configured_asset_items(configured_assets, "repositories")
    if repositories:
        repo_lines = [
            f'  - `{item.get("id")}`: {item.get("name") or item.get("description") or "repository"}'
            for item in repositories
        ]
        repo_example = repositories[0].get("id")
        sections.append(
            "\n".join(
                [
                    "Configured repositories:",
                    *repo_lines,
                    f'  Example: `{{"tool_name":"clone_repository","tool_input":{{"repo_id":"{repo_example}"}}}}`',
                ]
            )
        )

    databases = _get_configured_asset_items(configured_assets, "databases")
    if databases:
        db_lines = [
            f'  - `{item.get("id")}`: {item.get("name") or item.get("description") or "database"}'
            for item in databases
        ]
        db_example = databases[0].get("id")
        sections.append(
            "\n".join(
                [
                    "Configured databases:",
                    *db_lines,
                    f'  Example: `{{"tool_name":"query_database","tool_input":{{"db_id":"{db_example}","query_type":"list_tables"}}}}`',
                ]
            )
        )

    knowledge_bases = _get_configured_asset_items(configured_assets, "knowledge_bases")
    if knowledge_bases:
        kb_lines = [
            f'  - `{item.get("id")}` ({item.get("type") or "local"}): {item.get("name") or item.get("description") or "knowledge base"}'
            for item in knowledge_bases
        ]
        kb_example = knowledge_bases[0].get("id")
        sections.append(
            "\n".join(
                [
                    "Configured knowledge bases:",
                    *kb_lines,
                    f'  Example: `{{"tool_name":"query_knowledge_base","tool_input":{{"kb_id":"{kb_example}","query_type":"search_design_docs","keyword":"补发"}}}}`',
                ]
            )
        )

    if not sections:
        return ""
    return "\n\nKnown configured asset IDs:\n" + "\n\n".join(sections)


def _resolve_single_asset_id(
    configured_assets: Dict[str, Any] | None,
    asset_kind: str,
    requested_id: Any,
) -> tuple[Optional[str], Optional[str]]:
    items = _get_configured_asset_items(configured_assets, asset_kind)
    asset_ids = [str(item.get("id")) for item in items if item.get("id")]
    bucket = (configured_assets or {}).get(asset_kind) or {}
    is_complete_catalog = len(asset_ids) == int(bucket.get("count") or len(asset_ids))

    if isinstance(requested_id, str) and requested_id.strip():
        requested_id = requested_id.strip()
        if is_complete_catalog and requested_id not in asset_ids:
            return None, (
                f"Unknown {asset_kind[:-1]} id '{requested_id}'. "
                f"Available ids: {', '.join(asset_ids) if asset_ids else '(none)'}."
            )
        return requested_id, None

    if len(asset_ids) == 1:
        return asset_ids[0], f"Auto-selected the only configured {asset_kind[:-1]} id '{asset_ids[0]}'."

    if len(asset_ids) > 1:
        return None, f"Missing {asset_kind[:-1]} id. Choose one of: {', '.join(asset_ids)}."

    return None, f"No configured {asset_kind} are available for this project."


def _preflight_asset_tool_action(
    action: Dict[str, Any],
    configured_assets: Dict[str, Any] | None,
) -> tuple[Dict[str, Any], Optional[str], Optional[str]]:
    tool_name = str(action.get("tool_name") or "").strip()
    tool_input = dict(action.get("tool_input") or {})

    if tool_name == "clone_repository":
        resolved_id, note = _resolve_single_asset_id(configured_assets, "repositories", tool_input.get("repo_id"))
        if resolved_id is None:
            return tool_input, None, note
        tool_input["repo_id"] = resolved_id
        return tool_input, note, None

    if tool_name == "query_database":
        resolved_id, note = _resolve_single_asset_id(configured_assets, "databases", tool_input.get("db_id"))
        if resolved_id is None:
            return tool_input, None, note
        tool_input["db_id"] = resolved_id
        return tool_input, note, None

    if tool_name == "query_knowledge_base":
        kb_id = tool_input.get("kb_id")
        if kb_id is None or (isinstance(kb_id, str) and not kb_id.strip()):
            resolved_id, note = _resolve_single_asset_id(configured_assets, "knowledge_bases", kb_id)
            if resolved_id is not None:
                tool_input["kb_id"] = resolved_id
                return tool_input, note, None
            if "Missing knowledge_base id" in str(note):
                return tool_input, None, note
        else:
            resolved_id, note = _resolve_single_asset_id(configured_assets, "knowledge_bases", kb_id)
            if resolved_id is None:
                return tool_input, None, note
            tool_input["kb_id"] = resolved_id
        return tool_input, None, None

    return tool_input, None, None


def _build_tool_name_options(tools_allowed: List[str], configured_assets: Dict[str, Any] | None) -> str:
    tool_names = ["list_files", "read_file_chunk", "grep_search", "extract_structure", "extract_lookup_values"]
    configured_assets = configured_assets or {}

    if _tool_is_available("write_file", tools_allowed):
        tool_names.append("write_file")
    if _tool_is_available("append_file", tools_allowed):
        tool_names.append("append_file")
    if _tool_is_available("patch_file", tools_allowed):
        tool_names.append("patch_file")
    if _tool_is_available("upsert_markdown_sections", tools_allowed):
        tool_names.append("upsert_markdown_sections")
    if _tool_is_available("run_command", tools_allowed):
        tool_names.append("run_command")
    if _tool_is_available("validate_artifacts", tools_allowed):
        tool_names.append("validate_artifacts")
    if configured_assets.get("repositories") and _tool_is_available("clone_repository", tools_allowed):
        tool_names.append("clone_repository")
    if configured_assets.get("databases") and _tool_is_available("query_database", tools_allowed):
        tool_names.append("query_database")
    if configured_assets.get("knowledge_bases") and _tool_is_available("query_knowledge_base", tools_allowed):
        tool_names.append("query_knowledge_base")

    tool_names.append("none")
    return " | ".join(f'"{name}"' for name in tool_names)


def _build_tool_contract_section(tools_allowed: List[str], candidate_files: List[str]) -> str:
    read_example = candidate_files[0] if candidate_files else "baseline/raw-requirements.md"
    write_examples: List[str] = []
    if _tool_is_available("write_file", tools_allowed):
        write_examples.append(
            '- `write_file`: `{"path":"it-requirements.md","content":"..."}`. `path` is relative to `artifacts/`.'
        )
    if _tool_is_available("append_file", tools_allowed):
        write_examples.append(
            '- `append_file`: `{"path":"it-requirements.md","content":"\\n\\nmore content"}`. Appends raw content to the end of the file under `artifacts/`.'
        )
    if _tool_is_available("upsert_markdown_sections", tools_allowed):
        write_examples.append(
            '- `upsert_markdown_sections`: `{"path":"it-requirements.md","sections":[{"heading":"业务规则","content":"...","mode":"replace_by_heading"}]}`. Upserts markdown sections by heading and can skip near-duplicate sections.'
        )
    if _tool_is_available("patch_file", tools_allowed):
        write_examples.append(
            '- `patch_file`: `{"path":"it-requirements.md","old_content":"...","new_content":"..."}`. `path` is relative to `artifacts/`.'
        )
    if _tool_is_available("run_command", tools_allowed):
        write_examples.append(
            '- `run_command`: `{"command":"python -m unittest","timeout":30}`. Runs from project root `.`.'
        )
    if _tool_is_available("validate_artifacts", tools_allowed):
        write_examples.append(
            '- `validate_artifacts`: `{"target_files":["it-requirements.md","requirement-traceability.json"]}`. Validates generated files under `artifacts/`; omit `target_files` to validate all selected outputs.'
        )

    write_block = "\n".join(write_examples)
    if write_block:
        write_block = f"\n{write_block}"

    return f"""
Current location:
- Project root: `.`
- Baseline directory: `baseline/`
- Artifacts directory: `artifacts/`
- Evidence directory: `evidence/`
- Candidate requirement files: {candidate_files}

Tool input contract:
- Do NOT include `root_dir`. The runtime injects it automatically.
- For read tools, all `path` values are relative to project root `.`.
- For write tools, all `path` values are relative to `artifacts/`.
- `list_files`: use `{{}}` to inspect project root, or `{{"repos_dir":"baseline"}}` to inspect a subdirectory.
- `read_file_chunk`: use `{{"path":"{read_example}","start_line":1,"end_line":120}}`. Optional: `search_root`, `repos_dir`.
- `extract_structure`: use `{{"files":["{read_example}"]}}`. Do not send `path`.
- `grep_search`: use `{{"pattern":"Kafka|Redis|callback"}}`. Optional: `repos_dir`. Do not send `file_glob`, `include`, or ad-hoc filters.{write_block}
""".strip()


def _get_prompt_budget_profile(capability: str, stage: str) -> Dict[str, int]:
    profile = {
        "max_depth": 3,
        "max_string": 500,
        "max_list_items": 6,
        "max_dict_items": 12,
        "max_observations": 6,
    }

    if stage == "react":
        profile.update(
            {
                "max_string": 300,
                "max_list_items": 5,
                "max_dict_items": 10,
                "max_observations": 5,
            }
        )

    if capability in {"ir-assembler", "validator"} and stage == "final":
        profile.update(
            {
                "max_depth": 2,
                "max_string": 180,
                "max_list_items": 3,
                "max_dict_items": 8,
                "max_observations": 4,
            }
        )

    return profile


def _summarize_value_for_prompt(
    value: Any,
    *,
    max_depth: int = 3,
    max_string: int = 500,
    max_list_items: int = 6,
    max_dict_items: int = 12,
) -> Any:
    if max_depth < 0:
        return "[Truncated]"

    if isinstance(value, str):
        if len(value) <= max_string:
            return value
        return f"{value[:max_string]}...[truncated {len(value) - max_string} chars]"

    if isinstance(value, list):
        items = [
            _summarize_value_for_prompt(
                item,
                max_depth=max_depth - 1,
                max_string=max_string,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
            for item in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            items.append(f"[{len(value) - max_list_items} more items omitted]")
        return items

    if isinstance(value, dict):
        summary: Dict[str, Any] = {}
        keys = list(value.keys())[:max_dict_items]
        for key in keys:
            summary[str(key)] = _summarize_value_for_prompt(
                value[key],
                max_depth=max_depth - 1,
                max_string=max_string,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
            )
        if len(value) > max_dict_items:
            summary["_omitted_keys"] = len(value) - max_dict_items
        return summary

    return value


def _compact_payload_for_prompt(payload: Dict[str, Any], capability: str, stage: str) -> Dict[str, Any]:
    profile = _get_prompt_budget_profile(capability, stage)
    project_root = _get_runtime_project_root(payload)
    compact: Dict[str, Any] = {}

    for key in (
        "project_name",
        "project_id",
        "version",
        "requirement",
        "active_agents",
        "project_layout",
        "candidate_output_files",
        "selected_outputs",
    ):
        if key in payload:
            compact[key] = payload[key]

    uploaded_files = payload.get("uploaded_files") or []
    if uploaded_files:
        compact["uploaded_files"] = uploaded_files[:10]
        if len(uploaded_files) > 10:
            compact["uploaded_files_omitted"] = len(uploaded_files) - 10

    candidate_files = payload.get("candidate_files") or []
    if candidate_files:
        compact["candidate_files"] = candidate_files[:10]
        if len(candidate_files) > 10:
            compact["candidate_files_omitted"] = len(candidate_files) - 10

    if "configured_assets" in payload:
        compact["configured_assets"] = _summarize_value_for_prompt(
            payload["configured_assets"],
            max_depth=min(profile["max_depth"], 3),
            max_string=min(profile["max_string"], 200),
            max_list_items=min(profile["max_list_items"], 5),
            max_dict_items=min(profile["max_dict_items"], 8),
        )

    tool_context = payload.get("tool_context") or {}
    if tool_context:
        sanitized_tool_context = _sanitize_prompt_payload(tool_context, project_root)
        compact["tool_context"] = {
            "list_files": _summarize_value_for_prompt(
                sanitized_tool_context.get("list_files") or {},
                max_depth=min(profile["max_depth"], 3),
                max_string=min(profile["max_string"], 200),
                max_list_items=min(profile["max_list_items"], 6),
                max_dict_items=min(profile["max_dict_items"], 8),
            ),
            "extract_structure": _summarize_value_for_prompt(
                (sanitized_tool_context.get("extract_structure") or {}).get("files") or [],
                max_depth=min(profile["max_depth"], 3),
                max_string=min(profile["max_string"], 200),
                max_list_items=min(profile["max_list_items"], 6),
                max_dict_items=min(profile["max_dict_items"], 8),
            ),
        }

    if payload.get("human_inputs"):
        compact["human_inputs"] = _summarize_value_for_prompt(
            payload["human_inputs"],
            max_depth=min(profile["max_depth"], 3),
            max_string=min(profile["max_string"], 300),
            max_list_items=min(profile["max_list_items"], 4),
            max_dict_items=min(profile["max_dict_items"], 8),
        )

    if payload.get("human_answers"):
        compact["human_answers"] = _summarize_value_for_prompt(
            payload["human_answers"],
            max_depth=min(profile["max_depth"], 3),
            max_string=min(profile["max_string"], 500),
            max_list_items=min(profile["max_list_items"], 6),
            max_dict_items=min(profile["max_dict_items"], 8),
        )

    if payload.get("asset_insights"):
        compact["asset_insights"] = _summarize_value_for_prompt(
            payload["asset_insights"],
            max_depth=min(profile["max_depth"], 2),
            max_string=min(profile["max_string"], 150),
            max_list_items=min(profile["max_list_items"], 3),
            max_dict_items=min(profile["max_dict_items"], 6),
        )

    if payload.get("output_plan"):
        compact["output_plan"] = _summarize_value_for_prompt(
            payload["output_plan"],
            max_depth=min(profile["max_depth"], 4),
            max_string=min(profile["max_string"], 240),
            max_list_items=min(profile["max_list_items"], 6),
            max_dict_items=min(profile["max_dict_items"], 10),
        )

    return compact


def _compact_observations_for_prompt(
    observations: List[Dict[str, Any]],
    capability: str,
    stage: str,
    *,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    profile = _get_prompt_budget_profile(capability, stage)
    compact_observations: List[Dict[str, Any]] = []

    selected_observations = observations[-profile["max_observations"] :]
    for observation in selected_observations:
        sanitized_tool_input = _sanitize_prompt_payload(observation.get("tool_input") or {}, project_root)
        sanitized_tool_output = _sanitize_prompt_payload(observation.get("tool_output") or {}, project_root)
        compact_observations.append(
            {
                "step": observation.get("step"),
                "action_index": observation.get("action_index"),
                "tool_name": observation.get("tool_name"),
                "evidence_note": observation.get("evidence_note", ""),
                "tool_input": _summarize_value_for_prompt(
                    sanitized_tool_input,
                    max_depth=max(1, profile["max_depth"] - 1),
                    max_string=min(profile["max_string"], 200),
                    max_list_items=min(profile["max_list_items"], 4),
                    max_dict_items=min(profile["max_dict_items"], 8),
                ),
                "tool_output": _summarize_value_for_prompt(
                    sanitized_tool_output,
                    max_depth=max(1, profile["max_depth"] - 1),
                    max_string=profile["max_string"],
                    max_list_items=min(profile["max_list_items"], 4),
                    max_dict_items=min(profile["max_dict_items"], 10),
                ),
            }
        )

    if len(observations) > len(selected_observations):
        compact_observations.append(
            {
                "omitted_observations": len(observations) - len(selected_observations)
            }
        )

    return compact_observations


def _compact_finalization_observations_for_prompt(
    observations: List[Dict[str, Any]],
    *,
    max_observations: int = 4,
) -> List[Dict[str, Any]]:
    compact_rows: List[Dict[str, Any]] = []
    selected = observations[-max_observations:]

    for observation in selected:
        tool_name = str(observation.get("tool_name") or "")
        tool_input = dict(observation.get("tool_input") or {})
        tool_output = dict(observation.get("tool_output") or {})
        compact_input: Dict[str, Any] = {}
        compact_output: Dict[str, Any] = {}

        if "path" in tool_input:
            compact_input["path"] = tool_input.get("path")
        if "start_line" in tool_input:
            compact_input["start_line"] = tool_input.get("start_line")
        if "end_line" in tool_input:
            compact_input["end_line"] = tool_input.get("end_line")
        if "files" in tool_input and isinstance(tool_input.get("files"), list):
            compact_input["files"] = tool_input.get("files")[:3]
        if "pattern" in tool_input:
            compact_input["pattern"] = _summarize_value_for_prompt(str(tool_input.get("pattern") or ""), max_string=120)
        if "content" in tool_input and isinstance(tool_input.get("content"), str):
            compact_input["content_summary"] = f"<omitted {len(tool_input['content'])} chars>"
        if "old_content" in tool_input and isinstance(tool_input.get("old_content"), str):
            compact_input["old_content_summary"] = f"<omitted {len(tool_input['old_content'])} chars>"
        if "new_content" in tool_input and isinstance(tool_input.get("new_content"), str):
            compact_input["new_content_summary"] = f"<omitted {len(tool_input['new_content'])} chars>"

        if "path" in tool_output:
            compact_output["path"] = tool_output.get("path")
        if "size_bytes" in tool_output:
            compact_output["size_bytes"] = tool_output.get("size_bytes")
        if "message" in tool_output:
            compact_output["message"] = _summarize_value_for_prompt(str(tool_output.get("message") or ""), max_string=120)
        if "content" in tool_output and isinstance(tool_output.get("content"), str):
            compact_output["content_summary"] = f"<omitted {len(tool_output['content'])} chars>"
        if "matches" in tool_output and isinstance(tool_output.get("matches"), list):
            compact_output["match_count"] = len(tool_output["matches"])

        compact_rows.append(
            {
                "step": observation.get("step"),
                "action_index": observation.get("action_index"),
                "tool_name": tool_name,
                "evidence_note": _summarize_value_for_prompt(str(observation.get("evidence_note") or ""), max_string=160),
                "tool_input": compact_input,
                "tool_output": compact_output,
                "stage": observation.get("stage"),
            }
        )

    if len(observations) > len(selected):
        compact_rows.append({"omitted_observations": len(observations) - len(selected)})

    return compact_rows


def _compact_payload_for_finalization_prompt(
    payload: Dict[str, Any],
    expected_files: List[str],
) -> Dict[str, Any]:
    compact = {
        "project_name": payload.get("project_name") or payload.get("project_id"),
        "project_id": payload.get("project_id"),
        "version": payload.get("version"),
        "candidate_files": (payload.get("candidate_files") or [])[:5],
        "candidate_output_files": (payload.get("candidate_output_files") or [])[:8],
        "selected_outputs": (payload.get("selected_outputs") or expected_files)[:8],
        "expected_files": expected_files,
    }

    configured_assets = payload.get("configured_assets") or {}
    if isinstance(configured_assets, dict):
        compact["configured_asset_counts"] = {
            "repositories": ((configured_assets.get("repositories") or {}).get("count") or 0),
            "databases": ((configured_assets.get("databases") or {}).get("count") or 0),
            "knowledge_bases": ((configured_assets.get("knowledge_bases") or {}).get("count") or 0),
        }

    return compact


def _compact_payload_for_output_planning_prompt(payload: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {
        "project_name": payload.get("project_name") or payload.get("project_id"),
        "project_id": payload.get("project_id"),
        "version": payload.get("version"),
        "active_agents": (payload.get("active_agents") or [])[:12],
        "candidate_files": (payload.get("candidate_files") or [])[:8],
    }
    configured_assets = payload.get("configured_assets") or {}
    if isinstance(configured_assets, dict):
        compact["configured_asset_counts"] = {
            "repositories": ((configured_assets.get("repositories") or {}).get("count") or 0),
            "databases": ((configured_assets.get("databases") or {}).get("count") or 0),
            "knowledge_bases": ((configured_assets.get("knowledge_bases") or {}).get("count") or 0),
        }
    return compact


def build_output_planning_prompt(
    capability: str,
    prompt_instructions: str,
    candidate_outputs: List[str],
) -> str:
    candidate_block = "\n".join(
        f"- {path} (target <= {_resolve_output_char_budget({}, capability, path)} chars)"
        for path in candidate_outputs
    ) or "- (none)"
    custom_section = ""
    if prompt_instructions:
        custom_section = f"""
Custom Instructions from SKILL.md:
{prompt_instructions[:1200]}
"""
    runtime_rule = _call_skill_runtime_hook(
        capability,
        "output_planning_rule",
        candidate_outputs=candidate_outputs,
        default="",
    )
    expert_rule = f"\n10. {runtime_rule}" if isinstance(runtime_rule, str) and runtime_rule.strip() else ""

    return f"""
You are the {capability} output planner.
Treat the listed outputs as candidate deliverables, not mandatory files.
Choose only the files that are actually needed for this requirement scope.

{SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT}

Candidate outputs:
{candidate_block}

{custom_section}
Rules:
1. Select the minimum useful artifact set that still lets this expert deliver grounded, actionable output.
2. If some information can be referenced inside another selected document, prefer that over producing an extra file.
3. Every selected file must have a clear purpose and must-cover points.
4. Skipped files need a short reason.
5. The downstream ReAct loop will gather evidence around the selected outputs, so make the plan concrete.
6. Keep each file concise and scoped to this expert's responsibility; avoid absorbing downstream experts' detailed design work.
7. Respect the approximate per-file char budgets shown above when choosing scope and must-cover items.
8. Avoid planning files that would all need the same background, scope, or generic requirement-overview sections; shared context should live in one concise place, not every deliverable.
9. `must_cover_by_file` is a hard contract: every selected file must contain at least one concrete must-cover item, otherwise execution fails fast.
{expert_rule}

Return JSON in artifacts.output_plan:
{{
  "selected_outputs": ["output.md"],
  "skipped_outputs": [
    {{"path": "extra.json", "reason": "Covered sufficiently inside output.md"}}
  ],
  "file_order": ["output.md"],
  "must_cover_by_file": {{
    "output.md": ["what this file must answer"]
  }},
  "evidence_focus": ["what evidence the expert should gather next"],
  "planning_notes": "optional short planning rationale"
}}
""".strip()


def default_plan_outputs(
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    capability: str,
    project_id: str,
    version: str,
    payload: Dict[str, Any],
    candidate_files: List[str],
    candidate_outputs: List[str],
    agent_config: Optional["AgentFullConfig"] = None,
) -> Dict[str, Any]:
    candidate_outputs = _normalize_output_candidate_list(candidate_outputs)
    if len(candidate_outputs) <= 1:
        return _default_output_plan(capability, candidate_outputs)

    prompt_instructions = ""
    if agent_config:
        prompt_instructions = agent_config.prompt_instructions or ""

    preview = _build_coverage_brief(
        payload,
        capability,
        candidate_files,
        candidate_outputs,
        candidate_output_files=candidate_outputs,
        output_plan=_default_output_plan(capability, candidate_outputs),
        agent_config=agent_config,
    )
    user_prompt = json.dumps(
        {
            "project": project_id,
            "version": version,
            "payload_summary": _compact_payload_for_output_planning_prompt(payload),
            "candidate_outputs": candidate_outputs,
            "coverage_preview": {
                "focus_sections": preview.get("focus_sections") or [],
                "hard_constraints": preview.get("hard_constraints") or [],
                "non_functional_requirements": preview.get("non_functional_requirements") or [],
                "risks": preview.get("risks") or [],
                "target_outcomes": preview.get("target_outcomes") or [],
                "delivery_checklist": preview.get("delivery_checklist") or {},
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    llm_output = generate_with_llm_fn(
        build_output_planning_prompt(capability, prompt_instructions, candidate_outputs),
        user_prompt,
        ["output_plan"],
        project_id=project_id,
        version=version,
        node_id=f"{capability}-output-plan",
    )
    raw_plan = llm_output.artifacts.get("output_plan", "")
    try:
        parsed = json.loads(raw_plan) if raw_plan else {}
    except json.JSONDecodeError:
        parsed = {}
    normalized = _normalize_output_plan(
        parsed,
        capability=capability,
        candidate_outputs=candidate_outputs,
        agent_config=agent_config,
    )
    if llm_output.reasoning:
        normalized["planning_notes"] = str(normalized.get("planning_notes") or llm_output.reasoning).strip()
    return normalized


def _read_workspace_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_workspace_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _summarize_workspace_artifact_file(path: Path, *, artifacts_dir: Path) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "path": _normalize_relative_path(str(path.relative_to(artifacts_dir))),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
    suffix = path.suffix.lower()
    if suffix not in TEXT_ARTIFACT_SUFFIXES:
        row["kind"] = suffix.lstrip(".") or "binary"
        return row

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        row["kind"] = "binary"
        return row

    row["kind"] = suffix.lstrip(".") or "text"
    if suffix == ".md":
        headings = _extract_markdown_heading_titles(content)
        if headings:
            row["headings"] = headings[:12]
        section_summaries = _summarize_markdown_sections_for_prompt(content, limit=4)
        if section_summaries:
            row["section_summaries"] = section_summaries
    elif suffix == ".json":
        parsed = _read_workspace_json(path)
        if isinstance(parsed, dict):
            row["top_level_keys"] = list(parsed.keys())[:16]
        elif isinstance(parsed, list):
            row["item_count"] = len(parsed)

    row["excerpt"] = _summarize_value_for_prompt(content.strip(), max_string=700)
    return row


def _build_actual_workspace_artifact_summaries(
    artifacts_dir: Path,
    upstream_artifacts: Dict[str, List[str]],
    expected_files: List[str],
    *,
    max_files: int = 18,
) -> List[Dict[str, Any]]:
    expected = {_normalize_relative_path(item) for item in expected_files}
    seen: set[str] = set()
    rows: List[Dict[str, Any]] = []

    for owner, file_names in upstream_artifacts.items():
        for file_name in file_names:
            normalized = _normalize_relative_path(file_name)
            if normalized in seen or normalized in expected:
                continue
            path = artifacts_dir / normalized
            if not path.exists() or not path.is_file():
                continue
            row = _summarize_workspace_artifact_file(path, artifacts_dir=artifacts_dir)
            row["source_owner"] = owner
            rows.append(row)
            seen.add(normalized)
            if len(rows) >= max_files:
                return rows

    return rows


def _build_skill_workspace_plan(
    *,
    capability: str,
    actual_workspace_artifacts: List[Dict[str, Any]],
    output_plan: Dict[str, Any],
    coverage_brief: Dict[str, Any],
) -> Dict[str, Any]:
    runtime_plan = _call_skill_runtime_hook(
        capability,
        "build_workspace_plan",
        actual_workspace_artifacts=actual_workspace_artifacts,
        output_plan=output_plan,
        coverage_brief=coverage_brief,
    )
    if isinstance(runtime_plan, dict):
        return runtime_plan

    return {
        "capability": capability,
        "status": "not_applicable",
        "selected_outputs": list(output_plan.get("selected_outputs") or []),
    }


def _build_design_assembly_plan(
    *,
    capability: str,
    actual_workspace_artifacts: List[Dict[str, Any]],
    output_plan: Dict[str, Any],
    coverage_brief: Dict[str, Any],
) -> Dict[str, Any]:
    return _build_skill_workspace_plan(
        capability=capability,
        actual_workspace_artifacts=actual_workspace_artifacts,
        output_plan=output_plan,
        coverage_brief=coverage_brief,
    )


def _compact_requirement_digest_for_final_prompt(requirement_digest: str) -> str:
    if not requirement_digest.strip():
        return ""

    compact_lines: List[str] = []
    include_section = False
    allowed_sections = {
        "## Expert Must Answer",
        "## Evidence Expectations",
        "## Hard Constraints",
        "## Non-Functional Requirements",
        "## Risks",
        "## Target Outcomes",
        "## Outline",
    }
    for raw_line in requirement_digest.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("# ") or stripped.startswith("- Project:") or stripped.startswith("- Version:") or stripped.startswith("- Active agents:") or stripped.startswith("- Baseline files:") or stripped.startswith("- Candidate outputs:") or stripped.startswith("- Selected outputs:"):
            compact_lines.append(line)
            continue

        if stripped.startswith("## "):
            include_section = stripped in allowed_sections
            if include_section:
                compact_lines.append(line)
            continue

        if include_section and (stripped.startswith("- ") or stripped.startswith("### ")):
            compact_lines.append(line)

    return "\n".join(compact_lines).strip()


def _compact_output_plan_for_target_file(output_plan: Dict[str, Any], target_file: str) -> Dict[str, Any]:
    must_cover = output_plan.get("must_cover_by_file") or {}
    compact: Dict[str, Any] = {
        "selected_outputs": list(output_plan.get("selected_outputs") or []),
        "file_order": list(output_plan.get("file_order") or []),
        "target_file": target_file,
        "target_must_cover": [
            _summarize_value_for_prompt(str(item), max_string=240)
            for item in (must_cover.get(target_file) or [])[:8]
        ],
        "evidence_focus": [
            _summarize_value_for_prompt(str(item), max_string=220)
            for item in (output_plan.get("evidence_focus") or [])[:6]
        ],
        "planning_notes": _summarize_value_for_prompt(
            str(output_plan.get("planning_notes") or ""),
            max_string=260,
        ),
    }
    skipped = output_plan.get("skipped_outputs") or []
    if skipped:
        compact["skipped_outputs"] = [
            {
                "path": row.get("path"),
                "reason": _summarize_value_for_prompt(str(row.get("reason") or ""), max_string=140),
            }
            for row in skipped[:6]
            if isinstance(row, dict) and row.get("path")
        ]
    return compact


def _compact_coverage_brief_for_target_file(coverage_brief: Dict[str, Any], target_file: str) -> Dict[str, Any]:
    delivery = coverage_brief.get("delivery_checklist") or {}
    target_review_items = ((delivery.get("artifact_review_checklist") or {}).get(target_file) or [])[:6]
    return {
        "hard_constraints": (coverage_brief.get("hard_constraints") or [])[:6],
        "non_functional_requirements": (coverage_brief.get("non_functional_requirements") or [])[:6],
        "risks": (coverage_brief.get("risks") or [])[:6],
        "target_outcomes": (coverage_brief.get("target_outcomes") or [])[:6],
        "must_answer": (delivery.get("must_answer") or [])[:6],
        "evidence_expectations": (delivery.get("evidence_expectations") or [])[:6],
        "target_artifact_review": target_review_items,
        "focus_sections": [
            {
                "heading": row.get("heading"),
                "must_cover_points": (row.get("must_cover_points") or [])[:4],
            }
            for row in (coverage_brief.get("focus_sections") or [])[:4]
            if isinstance(row, dict)
        ],
    }


def _compact_grounded_observations_summary_for_final_prompt(observations_summary: Any) -> Any:
    if not isinstance(observations_summary, list):
        return observations_summary

    compact_rows: List[Dict[str, Any]] = []
    for row in observations_summary[-5:]:
        if not isinstance(row, dict):
            continue
        compact_rows.append(
            {
                "step": row.get("step"),
                "action_index": row.get("action_index"),
                "tool_name": row.get("tool_name"),
                "evidence_note": _summarize_value_for_prompt(str(row.get("evidence_note") or ""), max_string=180),
                "tool_input": _summarize_value_for_prompt(row.get("tool_input") or {}, max_depth=2, max_string=120, max_list_items=4, max_dict_items=6),
                "tool_output": _summarize_value_for_prompt(row.get("tool_output") or {}, max_depth=2, max_string=120, max_list_items=4, max_dict_items=6),
            }
        )
    if len(observations_summary) > len(compact_rows):
        compact_rows.append({"omitted_observations": len(observations_summary) - len(compact_rows)})
    return compact_rows


def _build_generation_batches(target_file: str, output_plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    must_cover = [str(item).strip() for item in ((output_plan.get("must_cover_by_file") or {}).get(target_file) or []) if str(item).strip()]
    suffix = Path(target_file).suffix.lower()
    batch_size = 1 if suffix == ".md" else max(1, len(must_cover) or 1)
    batches: List[Dict[str, Any]] = []

    if must_cover:
        for index in range(0, len(must_cover), batch_size):
            batch_items = must_cover[index:index + batch_size]
            batches.append(
                {
                    "batch_index": len(batches) + 1,
                    "batch_total": max(1, (len(must_cover) + batch_size - 1) // batch_size),
                    "section_focus": batch_items,
                }
            )
    else:
        batches.append(
            {
                "batch_index": 1,
                "batch_total": 1,
                "section_focus": [],
            }
        )
    return batches


def _resolve_template_hint_for_target(capability: str, target_file: str, template_hint: str) -> str:
    resolved = _call_skill_runtime_hook(
        capability,
        "resolve_template_hint",
        target_file=target_file,
        template_hint=template_hint,
    )
    return resolved if isinstance(resolved, str) else template_hint


def _compact_template_hint_for_prompt(template_hint: str) -> str:
    """Return template hint as-is; templates carry structural meaning and must not be truncated."""
    if not template_hint.strip():
        return ""
    return template_hint


def _is_timeout_exception(exc: Exception) -> bool:
    text = str(exc).lower()
    return "timeout" in text or "504" in text or "524" in text


def _build_timeout_fallback_fragment(
    *,
    target_file: str,
    section_focus: List[str],
    batch_index: int,
    batch_total: int,
    output_plan: Dict[str, Any],
    coverage_brief: Dict[str, Any],
) -> str:
    suffix = Path(target_file).suffix.lower()
    if suffix == ".json":
        return json.dumps(
            {
                "file": target_file,
                "batch_index": batch_index,
                "batch_total": batch_total,
                "selected_outputs": output_plan.get("selected_outputs") or [],
                "module_notes": section_focus or ["Timeout fallback fragment generated by controller."],
                "must_answer": (coverage_brief.get("must_answer") or [])[:4],
            },
            ensure_ascii=False,
            indent=2,
        )

    lines = [
        f"## Batch {batch_index}/{batch_total}",
        "",
        "本段为超时保护下生成的控制器回退片段，后续可继续补充细化。",
        "",
    ]
    for item in section_focus or ["围绕当前产物目标补充结构化设计说明。"]:
        title = str(item).split("：", 1)[0].strip() or "设计点"
        lines.extend(
            [
                f"### {title}",
                f"- 目标：{item}",
                "- 依据：结合已收集的代码仓、数据库、知识库与需求约束证据进行落地。",
                "- 待补强：如需更细的类名、表名、接口名，可在后续 patch 阶段继续细化。",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def build_targeted_artifact_prompt(
    capability: str,
    prompt_instructions: str,
    target_file: str,
    output_plan: Dict[str, Any],
    template_hint: str,
    *,
    section_focus: Optional[List[str]] = None,
    batch_index: int = 1,
    batch_total: int = 1,
    total_char_budget: int = 12000,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    must_cover = section_focus or (output_plan.get("must_cover_by_file", {}).get(target_file) or [])
    evidence_focus = output_plan.get("evidence_focus") or []
    skipped_outputs = output_plan.get("skipped_outputs") or []
    topic_ownership = output_plan.get("topic_ownership") if isinstance(output_plan.get("topic_ownership"), dict) else None
    shared_context_block = _build_shared_context_prompt_block(capability, topic_ownership)
    boundary_note = _scope_boundary_note(capability, agent_config=agent_config, runtime_profile=runtime_profile)
    opening_guardrail = (
        "If you include shared context, write it once and keep it short."
        if _owns_shared_context(capability, topic_ownership)
        else "Start directly with expert-specific sections. Do not open with a long project background or requirement overview."
    )
    runtime_artifact_rule = _call_skill_runtime_hook(
        capability,
        "targeted_artifact_rule",
        target_file=target_file,
        default="",
    )
    expert_artifact_rule = (
        f"\n14. {runtime_artifact_rule}"
        if isinstance(runtime_artifact_rule, str) and runtime_artifact_rule.strip()
        else ""
    )
    batch_char_budget = total_char_budget if batch_total <= 1 else max(800, total_char_budget // max(1, batch_total))
    custom_section = ""
    if prompt_instructions:
        custom_section = f"""
Custom Instructions from SKILL.md:
{prompt_instructions[:1200]}
"""

    template_section = ""
    resolved_template_hint = _resolve_template_hint_for_target(capability, target_file, template_hint)
    if resolved_template_hint:
        template_section = f"""
Template/style hint for `{target_file}`:
{_compact_template_hint_for_prompt(resolved_template_hint)}
"""

    skipped_block = "\n".join(
        f"- {row.get('path')}: {row.get('reason') or 'Not selected'}"
        for row in skipped_outputs
        if isinstance(row, dict) and row.get("path")
    )
    if not skipped_block:
        skipped_block = "- (none)"

    must_cover_block = "\n".join(f"- {item}" for item in must_cover) or "- (use the grounded evidence to determine the exact structure)"
    evidence_focus_block = "\n".join(f"- {item}" for item in evidence_focus) or "- Ground the file in the requirement digest and gathered observations."

    return f"""
You are the {capability} artifact writer.
You are writing batch {batch_index} of {batch_total} for `{target_file}`.
Return only the fragment needed for this batch, not the whole file.
The controller will append or patch fragments into the final artifact.

{SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT}

Selected outputs for this run:
{chr(10).join(f"- {item}" for item in (output_plan.get("selected_outputs") or [target_file]))}

Skipped candidate outputs:
{skipped_block}

This file must cover:
{must_cover_block}

Evidence focus for the expert:
{evidence_focus_block}

{shared_context_block}

{template_section}
{custom_section}
Rules:
1. Use only the grounded context provided in the user prompt.
2. If information is already covered by code/KB/DB evidence, reference it inside this file instead of inventing extra artifacts.
3. Produce deliverable-ready content, not meta commentary.
4. Keep this batch within about {batch_char_budget} characters, and keep the full `{target_file}` within about {total_char_budget} characters.
5. {boundary_note}
6. {opening_guardrail}
7. Do not restate the requirement digest, coverage brief, or upstream artifacts verbatim. Convert them into expert-specific deltas and cite the source briefly when needed.
8. If shared context is needed, keep it to at most two bullets before moving to expert-specific design.
9. Return only the fragment content for `{target_file}` in the artifact payload.
10. Do not attempt to cover sections outside the current batch focus.
11. If this is not the first batch, continue the same file naturally and avoid repeating sections already covered in the current artifact.
12. Do not emit a markdown heading that already exists in `current_artifact_headings` unless you are intentionally refining that exact section in place.
13. Also avoid creating a new section whose body is semantically very close to any item in `current_artifact_section_summaries`, even if you change the heading wording.
{expert_artifact_rule}
""".strip()


def default_generate_artifact_for_output(
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    capability: str,
    project_id: str,
    version: str,
    payload: Dict[str, Any],
    workspace_paths: Dict[str, str],
    artifacts_dir: Path,
    target_file: str,
    output_plan: Dict[str, Any],
    templates: Dict[str, str],
    agent_config: Optional["AgentFullConfig"] = None,
    step: int = 1,
    section_focus: Optional[List[str]] = None,
    batch_index: int = 1,
    batch_total: int = 1,
) -> SubagentOutput:
    prompt_instructions = ""
    if agent_config:
        prompt_instructions = agent_config.prompt_instructions or ""
    total_char_budget = _resolve_output_char_budget(
        {"design_context": payload.get("design_context") or {}},
        capability,
        target_file,
    )

    requirement_digest = _read_workspace_text(artifacts_dir / workspace_paths["requirement_digest"])
    coverage_brief = _read_workspace_json(artifacts_dir / workspace_paths["coverage_brief"])
    observations_summary = _read_workspace_json(artifacts_dir / workspace_paths["grounded_observations_summary"])
    workspace_index = _read_workspace_json(artifacts_dir / workspace_paths["workspace_index"])
    assembly_plan_path = workspace_paths.get("assembly_plan")
    assembly_plan = _read_workspace_json(artifacts_dir / assembly_plan_path) if assembly_plan_path else {}
    upstream_artifacts = workspace_index.get("upstream_artifacts") if isinstance(workspace_index, dict) else {}
    if not isinstance(upstream_artifacts, dict):
        upstream_artifacts = {}
    actual_workspace_artifacts = _build_actual_workspace_artifact_summaries(
        artifacts_dir,
        {
            str(owner): [str(item) for item in files if str(item).strip()]
            for owner, files in upstream_artifacts.items()
            if isinstance(files, list)
        },
        list(output_plan.get("selected_outputs") or []),
    )
    current_artifact_path = artifacts_dir / target_file
    current_artifact = current_artifact_path.read_text(encoding="utf-8") if current_artifact_path.exists() else ""
    current_artifact_headings = _extract_markdown_heading_titles(current_artifact)[:16]
    current_artifact_section_summaries = _summarize_markdown_sections_for_prompt(current_artifact, limit=8)
    compact_output_plan = _compact_output_plan_for_target_file(output_plan, target_file)
    compact_coverage_brief = _compact_coverage_brief_for_target_file(coverage_brief, target_file)

    user_prompt = json.dumps(
        {
            "project": project_id,
            "version": version,
            "step": step,
            "batch_index": batch_index,
            "batch_total": batch_total,
            "payload_summary": _compact_payload_for_finalization_prompt(
                payload,
                list(output_plan.get("selected_outputs") or []),
            ),
            "target_file": target_file,
            "target_context": compact_output_plan,
            "section_focus": section_focus or [],
            "requirement_digest": _summarize_value_for_prompt(
                _compact_requirement_digest_for_final_prompt(requirement_digest),
                max_string=1800,
            ),
            "coverage_brief": compact_coverage_brief,
            "workspace_index": _summarize_value_for_prompt(
                workspace_index,
                max_depth=3,
                max_string=240,
                max_list_items=10,
                max_dict_items=14,
            ),
            "assembly_plan": _summarize_value_for_prompt(
                assembly_plan,
                max_depth=4,
                max_string=420,
                max_list_items=10,
                max_dict_items=14,
            ),
            "actual_workspace_artifacts": actual_workspace_artifacts,
            "grounded_observations_summary": _compact_grounded_observations_summary_for_final_prompt(observations_summary),
            "current_artifact_headings": current_artifact_headings,
            "current_artifact_section_summaries": current_artifact_section_summaries,
            "current_artifact": _summarize_value_for_prompt(current_artifact, max_string=1600) if current_artifact else "",
        },
        ensure_ascii=False,
        indent=2,
    )
    return generate_with_llm_fn(
        build_targeted_artifact_prompt(
            capability,
            prompt_instructions,
            target_file,
            output_plan,
            templates.get(target_file, ""),
            section_focus=section_focus,
            batch_index=batch_index,
            batch_total=batch_total,
            total_char_budget=total_char_budget,
            agent_config=agent_config,
        ),
        user_prompt,
        [target_file],
        max_retries=0,
        project_id=project_id,
        version=version,
        node_id=f"{capability}-final-{Path(target_file).stem}-step-{step}",
    )


def _is_requirement_content_request(question: str) -> bool:
    normalized = re.sub(r"\s+", "", str(question or "").casefold())
    if not normalized:
        return False

    chinese_markers = [
        "提供具体的ir内容",
        "提供ir内容",
        "补充ir内容",
        "填写ir内容",
        "上传ir内容",
        "提供具体需求内容",
        "提供需求内容",
        "补充需求内容",
        "请提供具体的需求",
        "请补充完整需求",
    ]
    if any(marker in normalized for marker in chinese_markers):
        return True

    english_text = str(question or "").casefold()
    provide_pattern = r"\b(provide|share|upload|paste)\b.{0,40}\b(ir|requirement|requirements)\b.{0,40}\b(content|details|text)\b"
    missing_pattern = r"\b(ir|requirement|requirements)\b.{0,40}\b(missing|not provided|absent|unavailable)\b"
    return bool(re.search(provide_pattern, english_text) or re.search(missing_pattern, english_text))


def build_react_system_prompt(
    capability: str,
    prompt_instructions: str,
    tools_allowed: List[str],
    candidate_files: List[str],
    workflow_steps: Optional[List[str]] = None,
    upstream_artifacts: Optional[Dict[str, List[str]]] = None,
    configured_assets: Optional[Dict[str, Any]] = None,
    selected_outputs: Optional[List[str]] = None,
    output_plan: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    """
    Build the ReAct system prompt from agent configuration.
    
    Args:
        capability: Agent capability identifier
        prompt_instructions: Instructions extracted from SKILL.md
        tools_allowed: List of allowed tools
        candidate_files: Files available for reading
        workflow_steps: Optional workflow steps from SKILL.md
        upstream_artifacts: Dict mapping upstream agent -> list of artifact files
        (enables cross-agent memory)
        
    Returns:
        Formatted system prompt for ReAct loop
    """
    selected_outputs = _normalize_output_candidate_list(selected_outputs or [])
    output_plan = output_plan or {}
    profile = runtime_profile or resolve_expert_runtime_profile(capability, agent_config)
    topic_ownership = output_plan.get("topic_ownership") if isinstance(output_plan.get("topic_ownership"), dict) else None
    shared_context_block = _build_shared_context_prompt_block(capability, topic_ownership)
    boundary_note = _scope_boundary_note(capability, agent_config=agent_config, runtime_profile=profile)
    interaction_cfg = ((profile.interaction or {}).get("clarification") or {}) if profile else {}
    supported_question_types = [str(item).strip() for item in (interaction_cfg.get("supported_question_types") or []) if str(item).strip()]
    default_topics = [str(item).strip() for item in (interaction_cfg.get("default_topics") or []) if str(item).strip()]
    answer_merge_targets = [str(item).strip() for item in (interaction_cfg.get("answer_merge_targets") or []) if str(item).strip()]
    interaction_lines = [
        f"- Allowed question types: {', '.join(supported_question_types) if supported_question_types else 'single_select, multi_select'}",
        f"- Default clarification topics: {', '.join(default_topics) if default_topics else capability.replace('-', '_')}",
        f"- Answer merge targets: {', '.join(answer_merge_targets) if answer_merge_targets else 'clarified_requirements, decision_log'}",
    ]
    tool_contract_section = _build_tool_contract_section(tools_allowed, candidate_files)
    available_tools_section = _build_available_tool_section(tools_allowed)
    tools_section = f"""
{available_tools_section}

{tool_contract_section}
"""
    asset_tool_section = _build_asset_tool_section(tools_allowed, configured_assets)
    asset_examples_section = _format_asset_examples(configured_assets)
    tool_name_options = _build_tool_name_options(tools_allowed, configured_assets)
    
    # Build workflow section if available
    workflow_section = ""
    if workflow_steps:
        workflow_section = f"""
Workflow Steps:
{chr(10).join(f'{i+1}. {step}' for i, step in enumerate(workflow_steps))}
"""
    
    # Build custom instructions section
    custom_section = ""
    if prompt_instructions:
        custom_section = f"""
Custom Instructions from SKILL.md:
{prompt_instructions}
"""

    output_plan_section = ""
    if selected_outputs:
        selected_block = "\n".join(f"- {item}" for item in selected_outputs)
        must_cover_by_file = output_plan.get("must_cover_by_file") or {}
        evidence_focus = output_plan.get("evidence_focus") or []
        plan_lines = [
            "Selected outputs for this run:",
            selected_block,
        ]
        for file_name in selected_outputs:
            items = must_cover_by_file.get(file_name) or []
            if items:
                plan_lines.append(f"- {file_name} must cover: {'; '.join(str(item) for item in items[:6])}")
        if evidence_focus:
            plan_lines.append("Evidence focus:")
            plan_lines.extend(f"- {item}" for item in evidence_focus[:10])
        output_plan_section = "\n".join(plan_lines) + "\n"
    
    # Build cross-agent memory section
    memory_section = ""
    if upstream_artifacts:
        memory_lines = []
        for upstream_agent, artifacts in upstream_artifacts.items():
            if artifacts:
                artifact_paths = [f"artifacts/{a}" for a in artifacts]
                memory_lines.append(f"- {upstream_agent}: {', '.join(artifact_paths)}")
        if memory_lines:
            memory_section = f"""
Cross-Agent Memory (upstream artifacts available for reading):
{chr(10).join(memory_lines)}

You can read these files using read_file_chunk with file_path like "artifacts/it-requirements.md".
"""
    
    system_prompt = f"""
You are the {capability} ReAct controller.
Choose one next action at a time to ground design artifacts.
{SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT}
{custom_section}
{tools_section}
{asset_tool_section}
{asset_examples_section}
{output_plan_section}
Scope boundary:
- {boundary_note}

{shared_context_block}

{workflow_section}{memory_section}
Strategy:
1. Ground quickly: Anchor on the candidate baseline file immediately instead of searching for it by filename.
2. Research: Use read tools to collect the minimum evidence needed from baseline/ and upstream artifacts/, always in service of the selected outputs above.
3. Draft optionally: Use write_file only when an intermediate draft materially helps, and write it under `scratch/` with a `.draft` style filename.
4. Verify: Use read_file_chunk to inspect generated drafts only when needed.
5. Finalize: Set done=true as soon as the collected evidence is sufficient for final generation to produce all expected artifacts.

Rules:
1. You may output one action or a short sequential batch in `actions`, but keep it to at most {MAX_ACTIONS_PER_STEP} actions.
2. Stop when evidence is sufficient for final generation, even if ReAct has not written any artifact files yet.
3. Keep tool_input concise and machine-readable JSON.
4. Candidate files in baseline/: {candidate_files}
4a. Do not try to gather evidence for skipped candidate outputs; focus only on the selected outputs.
5. Only use asset-aware tools when the corresponding configured assets are present in the requirements payload.
5a. For `clone_repository`, `query_database`, and most `query_knowledge_base` calls, prefer passing the concrete configured asset id shown above.
5b. After `clone_repository`, prefer the returned `project_relative_path` or `search_hint` for later `repos_dir` parameters instead of guessing the cache directory name.
6. By step 2, you should already have grounded yourself on the correct baseline requirement content.
7. Do NOT write or patch the final expected artifact paths during ReAct. If you are ready to produce the final expected artifacts, return `done=true` instead.
8. Only use `actions` for short read-only batches such as `read_file_chunk`, `extract_structure`, `grep_search`, or `extract_lookup_values`.
9. Never batch `write_file`, `patch_file`, `run_command` or other write/execution tools such as `append_file`, `upsert_markdown_sections`, `clone_repository`, `query_database`, or `query_knowledge_base`; those must be emitted as a single action step.
10. Later actions in the same batch cannot see outputs from earlier actions in that batch, so only batch independent or low-risk steps.
11. Do not gather evidence merely to recreate generic shared-context sections such as {GENERIC_SHARED_CONTEXT_SECTION_EXAMPLES} when they are already owned upstream.
12. When reading upstream artifacts, extract only the expert-specific delta you need for the selected outputs instead of planning to restate large blocks verbatim.

Return JSON in artifacts.decision:
{{
  "done": false,
  "thought": "why this step is needed",
  "tool_name": {tool_name_options},
  "tool_input": {{}},
  "actions": [
    {{"tool_name": "read_file_chunk", "tool_input": {{"path": "{candidate_files[0] if candidate_files else 'baseline/raw-requirements.md'}", "start_line": 1, "end_line": 120}}}},
    {{"tool_name": "extract_structure", "tool_input": {{"files": ["{candidate_files[0] if candidate_files else 'baseline/raw-requirements.md'}"]}}}}
  ],
  "evidence_note": "what this step should confirm or produce",
  "needs_human": false,
  "human_question": "",
  "human_context": {{}}
}}

Human-in-the-loop:
- If you encounter a critical information gap or ambiguity in the requirement that would materially affect design quality, set needs_human=true.
- Provide a focused human_question (one question at a time) and optional human_context with suggested options.
- The question must stay within these expert interaction constraints:
{chr(10).join(interaction_lines)}
- Put the chosen topic into `human_context.topic`, the preferred question type into `human_context.preferred_answer_type`, and the merge targets into `human_context.answer_merge_targets`.
- Only use this when the gap cannot be resolved by reading available files or querying configured assets.
- Never ask the human to provide the IR, requirement text, or "specific IR content" when candidate baseline files are available; read the baseline file first and continue from that context.
- If requirement granularity or scope is imperfect but the baseline content exists, record the assumption or gap in the artifact instead of pausing for a generic IR-content request.
- Do NOT set needs_human for minor uncertainties or nice-to-have details.
- When needs_human is true, set done=true as well since execution must pause.
""".strip()
    
    return system_prompt


def build_final_artifacts_prompt(
    capability: str,
    prompt_instructions: str,
    expected_files: List[str],
    templates: Dict[str, str],
    topic_ownership: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    """
    Build the final artifacts generation prompt.
    
    Args:
        capability: Agent capability identifier
        prompt_instructions: Instructions from SKILL.md
        expected_files: Files to generate
        templates: Template content for each file
        
    Returns:
        Formatted system prompt for final generation
    """
    template_sections = []
    for file_name in expected_files:
        template_content = _resolve_template_hint_for_target(capability, file_name, templates.get(file_name, ""))
        if template_content:
            template_sections.append(f"[{file_name}]\n{template_content}")
    
    templates_block = "\n\n".join(template_sections)
    
    custom_section = ""
    if prompt_instructions:
        custom_section = f"""
Additional Guidelines:
{prompt_instructions}
"""
    shared_context_block = _build_shared_context_prompt_block(capability, topic_ownership)
    boundary_note = _scope_boundary_note(capability, agent_config=agent_config, runtime_profile=runtime_profile)
    opening_guardrail = (
        "If you include shared context, write it once and keep it short."
        if _owns_shared_context(capability, topic_ownership)
        else "Start directly with expert-specific sections. Do not prepend a generic project background, goal, or scope section."
    )
    
    system_prompt = f"""
You are a senior designer for {capability}.
Generate {', '.join(expected_files)} only from grounded evidence collected during the ReAct loop.

{SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT}

{shared_context_block}

Requirements:
1. Reflect only content supported by the observations.
2. Use consistent naming conventions.
3. Include enough structure for downstream consumers.
4. Use templates only as light style references; when actual workspace artifacts are available, their content and structure take precedence over template sections.
5. {boundary_note}
6. {opening_guardrail}
7. Do not restate shared context or upstream artifacts verbatim; synthesize them and cite briefly when needed.

{templates_block}
{custom_section}
""".strip()
    
    return system_prompt


def build_finalization_system_prompt(
    capability: str,
    prompt_instructions: str,
    tools_allowed: List[str],
    expected_files: List[str],
    candidate_files: List[str],
    workspace_paths: Dict[str, str],
    topic_ownership: Optional[Dict[str, Any]] = None,
    configured_assets: Optional[Dict[str, Any]] = None,
    agent_config: Optional["AgentFullConfig"] = None,
    runtime_profile: Optional[ExpertRuntimeProfile] = None,
) -> str:
    tool_contract_section = _build_tool_contract_section(tools_allowed, candidate_files)
    available_tools_section = _build_available_tool_section(tools_allowed)
    asset_tool_section = _build_asset_tool_section(tools_allowed, configured_assets)
    tool_name_options = _build_tool_name_options(tools_allowed, configured_assets)
    expected_block = "\n".join(f"- {file_name}" for file_name in expected_files)
    shared_context_block = _build_shared_context_prompt_block(capability, topic_ownership)
    boundary_note = _scope_boundary_note(capability, agent_config=agent_config, runtime_profile=runtime_profile)
    runtime_finalization_rule = _call_skill_runtime_hook(
        capability,
        "finalization_rule",
        workspace_paths=workspace_paths,
        default="",
    )
    expert_finalization_rule = (
        f"\n8a. {runtime_finalization_rule}"
        if isinstance(runtime_finalization_rule, str) and runtime_finalization_rule.strip()
        else ""
    )
    workspace_lines = [
        f"- workspace index: artifacts/{workspace_paths['workspace_index']}",
    ]
    if workspace_paths.get("assembly_plan"):
        workspace_lines.append(f"- assembly plan: artifacts/{workspace_paths['assembly_plan']}")
    workspace_lines.extend(
        [
            f"- requirement digest: artifacts/{workspace_paths['requirement_digest']}",
            f"- coverage brief: artifacts/{workspace_paths['coverage_brief']}",
            f"- output plan: artifacts/{workspace_paths['output_plan']}",
            f"- grounded observations: artifacts/{workspace_paths['grounded_observations']}",
            f"- observations summary: artifacts/{workspace_paths['grounded_observations_summary']}",
            f"- react trace: artifacts/{workspace_paths['react_trace']}",
            f"- finalization trace: artifacts/{workspace_paths['finalization_trace']}",
        ]
    )
    workspace_block = "\n".join(workspace_lines)
    custom_section = ""
    if prompt_instructions:
        custom_section = f"""
Custom Instructions from SKILL.md:
{prompt_instructions[:1200]}
"""

    return f"""
You are the {capability} finalization controller.
Your job is to turn grounded evidence already stored on disk into the final expected artifact files.

{SIMPLIFIED_CHINESE_OUTPUT_REQUIREMENT}

Expected artifacts:
{expected_block}

Workspace files you should use first:
{workspace_block}

{shared_context_block}

Scope boundary:
- {boundary_note}

{custom_section}
{available_tools_section}

{asset_tool_section}

{tool_contract_section}

Rules:
1. The full requirement text is intentionally NOT embedded here. Start from the requirement digest and coverage brief, and only read the baseline file again if needed.
2. Prefer reading `artifacts/{workspace_paths['workspace_index']}`, `artifacts/{workspace_paths.get('assembly_plan') or workspace_paths['output_plan']}`, `artifacts/{workspace_paths['coverage_brief']}`, and `artifacts/{workspace_paths['requirement_digest']}` before writing.
2a. If a skill-owned workspace plan such as `assembly-plan.json` exists, treat it as the primary expert-specific contract and use `workspace_index.actual_workspace_artifacts` only to drill into listed source files when the plan needs more detail.
3. Write final artifacts incrementally. One file at a time is preferred.
4. Use only the write or validation tools that the runtime tool contract exposes for this expert.
5. When multiple permitted write tools exist, prefer the narrowest one that preserves grounded structure.
6. Batch only read-only actions. Never batch `write_file`, `append_file`, `upsert_markdown_sections`, or `patch_file`.
7. For non-owner artifacts, start directly with expert-specific sections. Do not recreate generic background, scope, or goal sections from the digest.
8. Do not copy large blocks from the requirement digest or upstream artifacts. Synthesize and cite them briefly.
{expert_finalization_rule}
9. Set `done=true` only when every expected artifact exists under `artifacts/` and is materially complete.

Return JSON in artifacts.decision:
{{
  "done": false,
  "thought": "why this step is needed",
  "tool_name": {tool_name_options},
  "tool_input": {{}},
  "actions": [
    {{"tool_name":"read_file_chunk","tool_input":{{"path":"artifacts/{workspace_paths['workspace_index']}","start_line":1,"end_line":200}}}},
    {{"tool_name":"read_file_chunk","tool_input":{{"path":"artifacts/{workspace_paths['requirement_digest']}","start_line":1,"end_line":200}}}}
  ],
  "evidence_note": "what this step should confirm or produce"
}}
""".strip()


def default_next_finalization_decision(
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    capability: str,
    project_id: str,
    version: str,
    payload: Dict[str, Any],
    candidate_files: List[str],
    expected_files: List[str],
    workspace_paths: Dict[str, str],
    artifact_status: List[Dict[str, Any]],
    observations: List[Dict[str, Any]],
    step: int,
    agent_config: Optional["AgentFullConfig"] = None,
) -> Dict[str, Any]:
    prompt_instructions = ""
    tools_allowed = build_effective_tools([])
    if agent_config:
        prompt_instructions = agent_config.prompt_instructions or ""
        tools_allowed = _resolve_effective_tools(agent_config)

    system_prompt = build_finalization_system_prompt(
        capability=capability,
        prompt_instructions=prompt_instructions,
        tools_allowed=tools_allowed,
        expected_files=expected_files,
        candidate_files=candidate_files,
        workspace_paths=workspace_paths,
        topic_ownership=payload.get("topic_ownership") if isinstance(payload.get("topic_ownership"), dict) else None,
        configured_assets=payload.get("configured_assets") if isinstance(payload.get("configured_assets"), dict) else None,
        agent_config=agent_config,
    )

    payload_summary = _compact_payload_for_finalization_prompt(payload, expected_files)
    user_prompt = json.dumps(
        {
            "project": project_id,
            "version": version,
            "step": step,
            "payload_summary": payload_summary,
            "expected_files": expected_files,
            "workspace_paths": workspace_paths,
            "artifact_status": artifact_status,
            "recent_finalization_observations": _compact_finalization_observations_for_prompt(observations),
        },
        ensure_ascii=False,
        indent=2,
    )

    llm_output = generate_with_llm_fn(
        system_prompt,
        user_prompt,
        ["decision"],
        project_id=project_id,
        version=version,
        node_id=f"{capability}-final-step-{step}",
    )
    raw_decision = llm_output.artifacts.get("decision", "")

    try:
        decision = json.loads(raw_decision) if raw_decision else {"done": False, "tool_name": "none", "tool_input": {}}
    except json.JSONDecodeError:
        decision = {"done": False, "tool_name": "none", "tool_input": {}}

    if not isinstance(decision, dict):
        decision = {"done": False, "tool_name": "none", "tool_input": {}}

    decision.setdefault("done", False)
    decision.setdefault("tool_name", "none")
    decision.setdefault("tool_input", {})
    decision.setdefault("thought", "")
    decision.setdefault("evidence_note", "")
    decision = _normalize_react_decision(decision)
    decision["reasoning"] = llm_output.reasoning
    return decision


def default_next_react_decision(
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    capability: str,
    project_id: str,
    version: str,
    payload: Dict[str, Any],
    candidate_files: List[str],
    observations: List[Dict[str, Any]],
    templates: Dict[str, str],
    step: int,
    agent_config: Optional["AgentFullConfig"] = None,
    upstream_artifacts: Optional[Dict[str, List[str]]] = None,
    selected_outputs: Optional[List[str]] = None,
    output_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Default implementation for ReAct decision making.
    
    Uses AgentFullConfig for prompt construction if available.
    Supports cross-agent memory via upstream_artifacts parameter.
    """
    # Get configuration
    prompt_instructions = ""
    workflow_steps = None
    tools_allowed = build_effective_tools([])
    
    if agent_config:
        prompt_instructions = agent_config.prompt_instructions or ""
        workflow_steps = agent_config.workflow_steps or None
        tools_allowed = _resolve_effective_tools(agent_config)
    
    bootstrap_decision = _build_bootstrap_decision(candidate_files, observations, step)
    if bootstrap_decision:
        bootstrap_decision["reasoning"] = "执行启动锚定步骤，先锁定规范的 baseline 主文件，再进入自由式 ReAct 证据收集。"
        return bootstrap_decision

    configured_assets = payload.get("configured_assets") if isinstance(payload.get("configured_assets"), dict) else None
    project_root = _get_runtime_project_root(payload)
    system_prompt = build_react_system_prompt(
        capability=capability,
        prompt_instructions=prompt_instructions,
        tools_allowed=tools_allowed,
        candidate_files=candidate_files,
        workflow_steps=workflow_steps,
        upstream_artifacts=upstream_artifacts,
        configured_assets=configured_assets,
        selected_outputs=selected_outputs,
        output_plan=output_plan,
        agent_config=agent_config,
    )
    
    # Build template hints — pass full content to preserve structural meaning
    template_hints = {}
    for name, content in templates.items():
        if content:
            template_hints[name.replace(".", "_")] = _resolve_template_hint_for_target(capability, name, content)
    
    user_prompt = json.dumps(
        {
            "project": project_id,
            "version": version,
            "step": step,
            "requirements_payload": _compact_payload_for_prompt(payload, capability, "react"),
            "observations": _compact_observations_for_prompt(
                observations,
                capability,
                "react",
                project_root=project_root,
            ),
            "template_hints": template_hints,
        },
        ensure_ascii=False,
        indent=2,
    )
    
    llm_output = generate_with_llm_fn(
        system_prompt, 
        user_prompt, 
        ["decision"],
        project_id=project_id,
        version=version,
        node_id=f"{capability}-react-step-{step}"
    )
    raw_decision = llm_output.artifacts.get("decision", "")
    
    try:
        decision = json.loads(raw_decision) if raw_decision else _fallback_decision(candidate_files)
    except json.JSONDecodeError:
        decision = _fallback_decision(candidate_files)
    
    if not isinstance(decision, dict):
        decision = _fallback_decision(candidate_files)

    decision.setdefault("done", False)
    decision.setdefault("tool_name", "none")
    decision.setdefault("tool_input", {})
    decision.setdefault("thought", "")
    decision.setdefault("evidence_note", "")
    decision = _normalize_react_decision(decision)
    decision["reasoning"] = llm_output.reasoning
    
    return decision


def default_generate_final_artifacts(
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    capability: str,
    project_id: str,
    version: str,
    payload: Dict[str, Any],
    observations: List[Dict[str, Any]],
    templates: Dict[str, str],
    expected_files: List[str],
    agent_config: Optional["AgentFullConfig"] = None,
) -> SubagentOutput:
    """
    Default implementation for final artifacts generation.
    
    Uses AgentFullConfig for prompt construction if available.
    """
    prompt_instructions = ""
    if agent_config:
        prompt_instructions = agent_config.prompt_instructions or ""
    
    system_prompt = build_final_artifacts_prompt(
        capability=capability,
        prompt_instructions=prompt_instructions,
        expected_files=expected_files,
        templates=templates,
        topic_ownership=payload.get("topic_ownership") if isinstance(payload.get("topic_ownership"), dict) else None,
        agent_config=agent_config,
    )
    
    project_root = _get_runtime_project_root(payload)
    user_prompt = json.dumps(
        {
            "project": project_id,
            "version": version,
            "requirements_payload": _compact_payload_for_prompt(payload, capability, "final"),
            "grounded_observations": _compact_observations_for_prompt(
                observations,
                capability,
                "final",
                project_root=project_root,
            ),
            "expected_files": expected_files,
        },
        ensure_ascii=False,
        indent=2,
    )
    
    return generate_with_llm_fn(
        system_prompt, 
        user_prompt, 
        expected_files,
        project_id=project_id,
        version=version,
        node_id=f"{capability}-final"
    )


def _fallback_decision(candidate_files: List[str]) -> Dict[str, Any]:
    """Generate a fallback decision when LLM fails."""
    if candidate_files:
        return {
            "done": False,
            "thought": "先从当前可用文件开始收集证据。",
            "tool_name": "read_file_chunk",
            "tool_input": {"path": candidate_files[0], "start_line": 1, "end_line": 100},
            "evidence_note": "先读取初始需求内容，建立后续设计判断的事实基础。",
        }
    return {
        "done": True,
        "thought": "当前没有可读取的候选文件，视为可直接进入最终生成阶段。",
        "tool_name": "none",
        "tool_input": {},
        "evidence_note": "",
    }


def _build_bootstrap_decision(
    candidate_files: List[str],
    observations: List[Dict[str, Any]],
    step: int,
) -> Optional[Dict[str, Any]]:
    if not candidate_files:
        return None

    primary_candidate = candidate_files[0]
    candidate_read_succeeded = any(
        observation.get("tool_name") == "read_file_chunk"
        and (observation.get("tool_input") or {}).get("path") == primary_candidate
        and bool((observation.get("tool_output") or {}).get("content"))
        for observation in observations
    )
    candidate_structure_succeeded = any(
        observation.get("tool_name") == "extract_structure"
        and primary_candidate in ((observation.get("tool_input") or {}).get("files") or [])
        for observation in observations
    )

    if step == 1 and not candidate_read_succeeded:
        return {
            "done": False,
            "thought": f"先读取规范的 baseline 主文件 `{primary_candidate}`，确保专家立即基于正确需求内容开始工作。",
            "tool_name": "read_file_chunk",
            "tool_input": {
                "path": primary_candidate,
                "start_line": 1,
                "end_line": 160,
            },
            "evidence_note": "在继续检索或综合之前，先确认 baseline 源文件中的真实需求内容。",
        }

    if step == 2 and candidate_read_succeeded and not candidate_structure_succeeded:
        return {
            "done": False,
            "thought": f"第 2 步提取 `{primary_candidate}` 的结构，让专家在深入分析前同时掌握精确内容和稳定大纲。",
            "tool_name": "extract_structure",
            "tool_input": {
                "files": [primary_candidate],
            },
            "evidence_note": "提取规范 baseline 文件中的标题、章节和整体文档结构。",
        }

    return None


def default_fallback_artifacts(
    capability: str,
    payload: Dict[str, Any],
    observations: List[Dict[str, Any]],
    expected_files: List[str],
) -> SubagentOutput:
    """Generate minimal fallback artifacts when LLM generation fails."""
    artifacts = {}
    reasoning = f"{capability} 在当前轮次未返回有效 LLM 结果，系统已回退为最小占位产物生成。"
    
    for file_name in expected_files:
        # Create empty placeholder
        if file_name.endswith(".sql"):
            artifacts[file_name] = "-- 回退模式生成的占位内容\n"
        elif file_name.endswith(".md"):
            artifacts[file_name] = f"# {file_name}\n\n回退模式生成的占位内容。\n"
        elif file_name.endswith(".yaml") or file_name.endswith(".yml"):
            artifacts[file_name] = "# 回退模式占位配置\n"
        elif file_name.endswith(".json"):
            artifacts[file_name] = "{}\n"
        else:
            artifacts[file_name] = ""
    
    return SubagentOutput(reasoning=reasoning, artifacts=artifacts)


def default_tool_history_entries(tool_name: str, tool_result: Dict[str, Any]) -> List[str]:
    """Generate history entries from tool execution."""
    entries = []
    status = tool_result.get("status", "unknown")
    duration = tool_result.get("duration_ms", 0)
    
    if status == "success":
        entries.append(f"[TOOL] {tool_name} completed in {duration}ms")
    else:
        error_code = tool_result.get("error_code", "UNKNOWN")
        entries.append(f"[TOOL] {tool_name} failed: {error_code}")
    
    return entries


def default_build_evidence(
    capability: str,
    payload: Dict[str, Any],
    artifacts: Dict[str, str],
    observations: List[Dict[str, Any]],
    react_trace: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
    expected_files: List[str],
    candidate_output_files: Optional[List[str]] = None,
    output_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build evidence document from execution trace."""
    return {
        "capability": capability,
        "mode": "dynamic_subagent",
        "candidate_output_files": candidate_output_files or [],
        "expected_files": expected_files,
        "selected_outputs": list((output_plan or {}).get("selected_outputs") or expected_files),
        "output_plan": output_plan or {},
        "artifacts_generated": list(artifacts.keys()),
        "observation_count": len(observations),
        "tool_calls": len(tool_results),
        "success_rate": sum(1 for r in tool_results if r.get("status") == "success") / max(len(tool_results), 1),
    }


async def run_dynamic_subagent(
    *,
    capability: str,
    state: Dict[str, Any],
    base_dir: Path,
    generate_with_llm_fn: Callable[[str, str, List[str], int, Dict[str, Any] | None, str | None, str | None, str | None], SubagentOutput],
    execute_tool_fn: Callable[[str, Dict[str, Any] | None], Dict[str, Any]],
    update_task_status_fn: Callable[[List[Dict[str, Any]], str, str], List[Dict[str, Any]]],
    agent_config: Optional["AgentFullConfig"] = None,
    max_react_steps: int = MAX_REACT_STEPS,
    enable_permission_check: bool = True,
    # Optional overrides for custom behavior
    next_decision_fn: Optional[Callable] = None,
    generate_final_artifacts_fn: Optional[Callable] = None,
    fallback_artifacts_fn: Optional[Callable] = None,
    expected_files_fn: Optional[Callable] = None,
    candidate_files_fn: Optional[Callable] = None,
    plan_outputs_fn: Optional[Callable] = None,
    execution_guard_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Execute a subagent dynamically based on its configuration.
    
    This function loads the agent configuration from AgentRegistry and uses
    it to construct prompts, validate tool permissions, and determine expected
    outputs.
    
    Args:
        capability: Agent capability identifier (e.g., "rules-management")
        state: Current workflow state
        base_dir: Project base directory
        generate_with_llm_fn: LLM generation function
        execute_tool_fn: Tool execution function
        update_task_status_fn: Task status update function
        agent_config: Pre-loaded AgentFullConfig (optional, loads from registry if not provided)
        max_react_steps: Maximum ReAct loop iterations
        enable_permission_check: Whether to check tool permissions
        next_decision_fn: Override for ReAct decision function
        generate_final_artifacts_fn: Override for final generation function
        fallback_artifacts_fn: Override for fallback generation function
        expected_files_fn: Override for expected files function
        candidate_files_fn: Override for candidate files function
        plan_outputs_fn: Override for output planning function
        execution_guard_fn: Optional callback that can abort execution when the
            owning workflow run is no longer active or a sibling branch failed
        
    Returns:
        Updated state dictionary with execution results
    """
    import asyncio
    from registry.agent_registry import AgentRegistry
    
    # Load agent config if not provided
    if agent_config is None:
        try:
            registry = AgentRegistry.get_instance()
            agent_config = registry.load_full_config(capability)
        except RuntimeError:
            # Registry not initialized, proceed without config
            pass

    effective_tools = _resolve_effective_tools(
        agent_config,
        allow_unsafe_default=not enable_permission_check,
    )
    effective_tool_set = set(effective_tools)
    
    project_id = state["project_id"]
    version = state["version"]
    project_path = base_dir / "projects" / project_id / version
    baseline_path = project_path / "baseline" / "requirements.json"
    baseline_dir = baseline_path.parent
    artifacts_dir = project_path / "artifacts"
    logs_dir = project_path / "logs"
    evidence_dir = project_path / "evidence"
    work_dir = artifacts_dir / _workspace_relative_dir(capability)

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
    payload["candidate_files"] = _resolve_candidate_files(payload)
    payload["topic_ownership"] = _resolve_topic_ownership(
        payload.get("topic_ownership")
        if isinstance(payload.get("topic_ownership"), dict)
        else build_default_topic_ownership(payload.get("active_agents") or [])
    )
    payload.setdefault(
        "project_layout",
        {
            "project_root": ".",
            "baseline_dir": "baseline",
            "artifacts_dir": "artifacts",
            "evidence_dir": "evidence",
        },
    )
    payload["_runtime_project_root"] = str(project_path)

    # Inject human answers for this capability (from human-in-the-loop resumption)
    human_answers = state.get("human_answers") or {}
    capability_answers = human_answers.get(capability) or []
    if capability_answers:
        from graphs.nodes import _summarize_human_inputs
        payload["human_answers"] = _summarize_human_inputs(capability_answers)
        print(f"[DEBUG] {capability}: injected {len(capability_answers)} human answer(s) into payload")
    # Also inject human_feedback if present
    human_feedback = state.get("human_feedback", "")
    if human_feedback and (capability_answers or state.get("resume_target_node") == capability):
        payload["human_feedback"] = human_feedback

    history_updates = []
    runtime_llm_settings = resolve_runtime_llm_settings(state.get("design_context"))
    configured_assets = payload.get("configured_assets") if isinstance(payload.get("configured_assets"), dict) else None
    execution_signatures_seen: set[str] = set()
    unavailable_read_paths: set[str] = set()
    path_not_found_counts: Dict[str, int] = {}
    repeated_action_steps = 0
    repeated_focus_steps = 0
    previous_focus_signature = ""

    def _generate_with_selected_llm(*args: Any, **kwargs: Any) -> SubagentOutput:
        if runtime_llm_settings and "llm_settings" not in kwargs:
            kwargs["llm_settings"] = runtime_llm_settings
        return generate_with_llm_fn(*args, **kwargs)
    
    # Determine candidate files
    if candidate_files_fn:
        candidate_files = candidate_files_fn(payload)
    else:
        candidate_files = payload.get("candidate_files", []) or _resolve_candidate_files(payload)
    
    # Determine candidate outputs, then plan the selected outputs for this run
    if expected_files_fn:
        candidate_output_files = expected_files_fn(payload)
    elif agent_config and agent_config.metadata.get("expected_outputs"):
        candidate_output_files = agent_config.metadata["expected_outputs"]
    else:
        candidate_output_files = _default_expected_files(capability)
    candidate_output_files = _normalize_output_candidate_list(candidate_output_files)

    if plan_outputs_fn:
        output_plan = plan_outputs_fn(
            payload,
            candidate_files,
            candidate_output_files,
            capability,
            agent_config,
        )
    elif expected_files_fn:
        output_plan = _default_output_plan(capability, candidate_output_files)
    else:
        output_plan = await asyncio.to_thread(
            default_plan_outputs,
            _generate_with_selected_llm,
            capability,
            project_id,
            version,
            payload,
            candidate_files,
            candidate_output_files,
            agent_config,
        )
    output_plan = _normalize_output_plan(
        output_plan,
        capability=capability,
        candidate_outputs=candidate_output_files,
        agent_config=agent_config,
    )
    output_plan["topic_ownership"] = payload.get("topic_ownership") or build_default_topic_ownership(
        payload.get("active_agents") or []
    )
    expected_files = _normalize_output_candidate_list(output_plan.get("selected_outputs") or [])
    try:
        _validate_output_plan_coverage(
            capability=capability,
            output_plan=output_plan,
            candidate_outputs=candidate_output_files,
        )
    except ValueError as exc:
        validation_error = str(exc).strip() or "output plan coverage contract violation"
        history_updates.append(f"[{capability}] [ERROR] {validation_error}")
        (logs_dir / f"{capability}-reasoning.md").write_text(validation_error, encoding="utf-8")
        failure_evidence = default_build_evidence(
            capability,
            payload,
            {},
            [],
            [],
            [],
            expected_files,
            candidate_output_files,
            output_plan,
        )
        failure_evidence["failure_reason"] = "output_plan_coverage_contract_violation"
        failure_evidence["output_plan_validation_error"] = validation_error
        (evidence_dir / f"{capability}.json").write_text(
            json.dumps(failure_evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        history_updates.append(f"[{capability}] Completed with status: failed")
        return {
            "history": history_updates,
            "task_queue": update_task_status_fn(state["task_queue"], capability, "failed"),
            "human_intervention_required": False,
            "last_worker": capability,
            "tool_results": [],
        }

    payload["candidate_output_files"] = candidate_output_files
    payload["selected_outputs"] = expected_files
    payload["output_plan"] = output_plan
    history_updates.append(
        f"[SYSTEM] Output planning selected {expected_files or ['(none)']} from candidate outputs {candidate_output_files or ['(none)']}."
    )
    skipped_outputs = output_plan.get("skipped_outputs") or []
    if skipped_outputs:
        skipped_summary = ", ".join(
            f"{row.get('path')} ({row.get('reason') or 'skipped'})"
            for row in skipped_outputs
            if isinstance(row, dict) and row.get("path")
        )
        if skipped_summary:
            history_updates.append(f"[SYSTEM] Skipped candidate outputs: {skipped_summary}")
    
    # Discover upstream artifacts for cross-agent memory
    upstream_artifacts = _discover_upstream_artifacts(capability, artifacts_dir)
    if upstream_artifacts:
        upstream_summary = {k: v for k, v in upstream_artifacts.items() if v}
        history_updates.append(
            f"[SYSTEM] Cross-agent memory: found upstream artifacts: {upstream_summary}"
        )
    
    # Load templates
    templates = await asyncio.to_thread(
        _load_templates_for_capability,
        base_dir,
        capability,
        agent_config,
    )

    max_react_steps = _estimate_react_budget(
        state=state,
        payload=payload,
        expected_files=expected_files,
        agent_config=agent_config,
        upstream_artifacts=upstream_artifacts,
        default_value=max_react_steps,
    )

    history_updates.append(f"[SYSTEM] Dynamic subagent '{capability}' is now running.")
    history_updates.append(f"[SYSTEM] ReAct budget resolved to {max_react_steps} step(s).")
    tool_results: List[Dict[str, Any]] = []
    react_trace: List[Dict[str, Any]] = []
    observations: List[Dict[str, Any]] = []
    react_exhausted = False

    def _build_abort_result(abort_info: Dict[str, Any]) -> Dict[str, Any]:
        reason = str(abort_info.get("reason") or "execution aborted").strip()
        status_override = abort_info.get("status")
        failure_reason = str(abort_info.get("failure_reason") or "execution_aborted").strip()
        history_updates.append(f"[{capability}] [SYSTEM] Execution stopped: {reason}")
        reasoning_sections = [entry.get("reasoning", "") for entry in react_trace if entry.get("reasoning")]
        reasoning_sections.append(f"Execution aborted: {reason}.")
        (logs_dir / f"{capability}-reasoning.md").write_text(
            "\n\n".join(section for section in reasoning_sections if section),
            encoding="utf-8",
        )
        evidence = default_build_evidence(
            capability,
            payload,
            {},
            observations,
            react_trace,
            tool_results,
            expected_files,
            candidate_output_files,
            output_plan,
        )
        evidence.setdefault("react_trace", react_trace)
        evidence["failure_reason"] = failure_reason
        evidence["abort_reason"] = reason
        (evidence_dir / f"{capability}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        next_queue = state["task_queue"]
        if isinstance(status_override, str) and status_override:
            next_queue = update_task_status_fn(state["task_queue"], capability, status_override)
        history_updates.append(f"[{capability}] Completed with status: {status_override or 'aborted'}")
        return {
            "history": history_updates,
            "task_queue": next_queue,
            "human_intervention_required": False,
            "last_worker": capability,
            "tool_results": tool_results,
        }

    # Permission-aware tool executor
    def _execute_tool_with_permission(tool_name: str, tool_input: Dict[str, Any] | None) -> Dict[str, Any]:
        if enable_permission_check:
            from api_server.graphs.tools.protocol import execute_tool_with_permission
            return execute_tool_with_permission(
                tool_name, tool_input, agent_capability=capability
            )
        return execute_tool_fn(tool_name, tool_input)

    try:
        for step in range(1, max_react_steps + 1):
            if execution_guard_fn:
                abort_info = execution_guard_fn()
                if abort_info:
                    return _build_abort_result(abort_info)
            # Use custom or default decision function
            if next_decision_fn:
                decision = await asyncio.to_thread(
                    next_decision_fn,
                    payload,
                    observations,
                    templates,
                    step,
                )
            else:
                decision = await asyncio.to_thread(
                    default_next_react_decision,
                    _generate_with_selected_llm,
                    capability,
                    project_id,
                    version,
                    payload,
                    candidate_files,
                    observations,
                    templates,
                    step,
                    agent_config,
                    upstream_artifacts,
                    expected_files,
                    output_plan,
                )

            decision = _normalize_react_decision(decision)
            final_artifact_target = None
            for action in decision.get("actions") or []:
                final_artifact_target = _action_targets_final_artifact(action, expected_files)
                if final_artifact_target:
                    break
            if final_artifact_target:
                decision = dict(decision)
                decision["done"] = True
                decision["actions"] = []
                decision["tool_name"] = "none"
                decision["tool_input"] = {}
                thought = str(decision.get("thought") or "").strip()
                if thought:
                    thought = f"{thought} Final expected artifacts must be generated in the final generation stage."
                else:
                    thought = "Evidence is sufficient; defer final artifact writing to the final generation stage."
                decision["thought"] = thought
                decision["coerced_done_from_final_artifact_write"] = final_artifact_target
            
            react_trace.append({"step": step, **decision})
            focus_signature = _decision_focus_signature(decision)
            if focus_signature and focus_signature == previous_focus_signature:
                repeated_focus_steps += 1
            else:
                repeated_focus_steps = 0
            previous_focus_signature = focus_signature
            thought = decision.get("thought", "")
            if thought:
                history_updates.append(f"[{capability}] ReAct step {step}: {thought}")

            if decision.get("actions_truncated"):
                history_updates.append(
                    f"[{capability}] ReAct step {step}: truncated actions batch by {decision['actions_truncated']} item(s) to respect the per-step cap."
                )

            if decision.get("actions_restricted_to_single"):
                history_updates.append(
                    f"[{capability}] ReAct step {step}: restricted batched actions to a single action because only read-only tools may be batched."
                )

            if final_artifact_target:
                history_updates.append(
                    f"[{capability}] ReAct step {step}: attempted to write final artifact `{final_artifact_target}` during ReAct; switching to final generation."
                )

            # Human-in-the-loop: detect needs_human from LLM decision
            expert_needs_human = bool(decision.get("needs_human"))
            expert_question = str(decision.get("human_question") or "").strip()
            expert_context = decision.get("human_context") if isinstance(decision.get("human_context"), dict) else {}
            if expert_needs_human and _is_requirement_content_request(expert_question) and candidate_files:
                baseline_path = candidate_files[0]
                history_updates.append(
                    f"[{capability}] ReAct step {step}: suppressed generic requirement-content clarification; reading `{baseline_path}` instead."
                )
                decision["needs_human"] = False
                decision["done"] = False
                decision["actions"] = [
                    {
                        "tool_name": "read_file_chunk",
                        "tool_input": {
                            "path": baseline_path,
                            "start_line": 1,
                            "end_line": 120,
                        },
                    }
                ]
                expert_needs_human = False
            if expert_needs_human and not expert_question:
                expert_question = (
                    f"[{capability}] encountered an information gap during design that requires human clarification "
                    f"before proceeding. Please review the current evidence and provide guidance."
                )
            if expert_needs_human and expert_question:
                history_updates.append(
                    f"[{capability}] ReAct step {step}: requesting human clarification - {expert_question[:200]}"
                )
                interaction_profile = resolve_expert_runtime_profile(capability, agent_config)
                clarification_cfg = ((interaction_profile.interaction or {}).get("clarification") or {})
                supported_question_types = [
                    str(item).strip()
                    for item in (clarification_cfg.get("supported_question_types") or [])
                    if str(item).strip()
                ] or ["single_select", "multi_select"]
                default_topics = [
                    str(item).strip()
                    for item in (clarification_cfg.get("default_topics") or [])
                    if str(item).strip()
                ]
                answer_merge_targets = [
                    str(item).strip()
                    for item in (clarification_cfg.get("answer_merge_targets") or [])
                    if str(item).strip()
                ] or ["clarified_requirements", "decision_log"]
                # Normalize the human_context for the interrupt
                from graphs.nodes import _normalize_interrupt_context
                normalized_ctx = _normalize_interrupt_context(expert_context)
                preferred_answer_type = str(normalized_ctx.get("preferred_answer_type") or "").strip()
                supports_select_question = any(_question_type_supports_options(item) for item in supported_question_types)
                if preferred_answer_type not in supported_question_types or (
                    supports_select_question and not _question_type_supports_options(preferred_answer_type)
                ):
                    if isinstance(normalized_ctx.get("options"), list) and normalized_ctx.get("options"):
                        preferred_answer_type = _select_default_question_type(supported_question_types)
                    else:
                        preferred_answer_type = _select_default_question_type(supported_question_types)
                if _question_type_supports_options(preferred_answer_type):
                    normalized_ctx["options"] = _normalize_human_options_with_other(normalized_ctx.get("options"))
                normalized_ctx.setdefault("topic", "clarification")
                if default_topics and normalized_ctx.get("topic") == "clarification":
                    normalized_ctx["topic"] = default_topics[0]
                normalized_ctx.setdefault(
                    "why_needed",
                    f"The {capability} expert is blocked by an unresolved requirement or boundary decision.",
                )
                normalized_ctx.setdefault(
                    "impact_if_unanswered",
                    "The expert may make incorrect assumptions or produce low-confidence design output.",
                )
                normalized_ctx.setdefault("related_artifacts", [])
                normalized_ctx["supported_question_types"] = supported_question_types
                normalized_ctx["preferred_answer_type"] = preferred_answer_type
                normalized_ctx["answer_merge_targets"] = answer_merge_targets
                question_schema = (
                    dict(normalized_ctx.get("question_schema"))
                    if isinstance(normalized_ctx.get("question_schema"), dict)
                    else {}
                )
                question_schema["type"] = preferred_answer_type
                question_schema["allow_free_text"] = True
                if _question_type_supports_options(preferred_answer_type):
                    question_schema["options"] = list(normalized_ctx.get("options") or [])
                normalized_ctx["question_schema"] = question_schema
                from graphs.nodes import _build_pending_interrupt
                pending_interrupt = _build_pending_interrupt(
                    node_id=state.get("current_task_id") or capability,
                    node_type=capability,
                    question=expert_question,
                    context=normalized_ctx,
                    resume_target=capability,
                    interrupt_kind="ask_human",
                )
                return {
                    "history": history_updates,
                    "task_queue": update_task_status_fn(state["task_queue"], capability, "waiting_human"),
                    "human_intervention_required": True,
                    "waiting_reason": expert_question,
                    "pending_interrupt": pending_interrupt,
                    "run_status": "waiting_human",
                    "last_worker": capability,
                    "current_node": capability,
                    "tool_results": tool_results,
                }

            if decision.get("done"):
                history_updates.append(f"[{capability}] ReAct step {step}: evidence is sufficient, moving to final generation.")
                break

            executed_action_summaries: List[Dict[str, Any]] = []
            step_signatures: List[str] = []
            for action_index, action in enumerate(decision.get("actions") or [], start=1):
                if execution_guard_fn:
                    abort_info = execution_guard_fn()
                    if abort_info:
                        return _build_abort_result(abort_info)
                tool_name = action.get("tool_name") or "none"
                tool_input = dict(action.get("tool_input") or {})

                if tool_name == "none":
                    continue

                tool_input, autofill_note, validation_error = _preflight_asset_tool_action(
                    action,
                    configured_assets,
                )
                if autofill_note:
                    history_updates.append(f"[{capability}] ReAct step {step}: {autofill_note}")
                if validation_error:
                    history_updates.append(
                        f"[{capability}] ReAct step {step}: asset tool input rejected before execution. {validation_error}"
                    )
                    executed_action_summaries.append(
                        {
                            "action_index": action_index,
                            "tool_name": tool_name,
                            "status": "error",
                            "error_code": "INVALID_ASSET_SELECTION",
                            "duration_ms": 0,
                        }
                    )
                    observations.append(
                        {
                            "step": step,
                            "action_index": action_index,
                            "tool_name": tool_name,
                            "tool_input": _sanitize_prompt_payload(tool_input, project_path),
                            "tool_output": {
                                "error": {
                                    "code": "INVALID_ASSET_SELECTION",
                                    "message": validation_error,
                                }
                            },
                            "evidence_note": decision.get("evidence_note", ""),
                        }
                    )
                    continue

                # Set root_dir based on tool type for cross-agent memory
                # - Write tools: write to artifacts directory
                # - Read tools: can read from project root (baseline/, artifacts/, evidence/)
                # This enables downstream agents to read upstream artifacts
                if tool_name in DEFAULT_WRITE_TOOLS:
                    tool_input["root_dir"] = str(artifacts_dir)
                else:
                    # Read tools can access project root for cross-agent memory
                    tool_input["root_dir"] = str(project_path)

                normalized_path = ""
                if tool_name == "read_file_chunk":
                    normalized_path = _normalize_relative_path(str(tool_input.get("path") or ""))
                    if normalized_path and normalized_path in unavailable_read_paths:
                        tool_result = {
                            "tool_name": tool_name,
                            "status": "error",
                            "error_code": "KNOWN_PATH_UNAVAILABLE",
                            "duration_ms": 0,
                            "input": dict(tool_input or {}),
                            "output": {
                                "error": {
                                    "code": "KNOWN_PATH_UNAVAILABLE",
                                    "message": f"Previously confirmed missing path: {normalized_path}",
                                }
                            },
                        }
                    else:
                        tool_result = await asyncio.to_thread(_execute_tool_with_permission, tool_name, tool_input)
                else:
                    tool_result = await asyncio.to_thread(_execute_tool_with_permission, tool_name, tool_input)
                tool_results.append(tool_result)
                executed_action_summaries.append(
                    {
                        "action_index": action_index,
                        "tool_name": tool_name,
                        "status": tool_result.get("status"),
                        "error_code": tool_result.get("error_code"),
                        "duration_ms": tool_result.get("duration_ms"),
                    }
                )

                if (
                    tool_name == "read_file_chunk"
                    and tool_result.get("status") != "success"
                    and tool_result.get("error_code") == "PATH_NOT_FOUND"
                    and normalized_path
                ):
                    path_not_found_counts[normalized_path] = path_not_found_counts.get(normalized_path, 0) + 1
                    if path_not_found_counts[normalized_path] >= PATH_NOT_FOUND_REPEAT_LIMIT:
                        unavailable_read_paths.add(normalized_path)

                history_updates.extend(default_tool_history_entries(tool_name, tool_result))
                observations.append(
                    {
                        "step": step,
                        "action_index": action_index,
                        "tool_name": tool_name,
                        "tool_input": _sanitize_prompt_payload(tool_result.get("input") or {}, project_path),
                        "tool_output": _sanitize_prompt_payload(tool_result.get("output") or {}, project_path),
                        "evidence_note": decision.get("evidence_note", ""),
                    }
                )
                step_signatures.append(_tool_execution_signature(tool_name, tool_input, tool_result))

            if executed_action_summaries:
                react_trace[-1]["tool_results"] = executed_action_summaries
                if step_signatures and all(signature in execution_signatures_seen for signature in step_signatures):
                    repeated_action_steps += 1
                else:
                    repeated_action_steps = 0
                execution_signatures_seen.update(step_signatures)
                highest_path_not_found_repeat = max(path_not_found_counts.values(), default=0)
                if (
                    step >= REACT_MIN_STEPS_BEFORE_PLATEAU
                    and (
                        repeated_action_steps >= REACT_PLATEAU_WINDOW
                        or (
                            repeated_focus_steps >= REACT_PLATEAU_WINDOW
                            and highest_path_not_found_repeat >= PATH_NOT_FOUND_REPEAT_LIMIT
                        )
                    )
                ):
                    plateau_reason = (
                        f"repeated_action_steps={repeated_action_steps}, "
                        f"repeated_focus_steps={repeated_focus_steps}, "
                        f"path_not_found_repeat={highest_path_not_found_repeat}"
                    )
                    react_trace[-1]["controller_forced_done"] = plateau_reason
                    history_updates.append(
                        f"[{capability}] ReAct step {step}: controller detected repeated evidence plateau ({plateau_reason}); moving to final generation."
                    )
                    break
        else:
            react_exhausted = True
            history_updates.append(
                f"[{capability}] ReAct step {max_react_steps}: reached max steps."
            )

        if react_exhausted:
            reasoning_sections = [entry.get("reasoning", "") for entry in react_trace if entry.get("reasoning")]
            reasoning_sections.append(
                f"ReAct loop exhausted {max_react_steps} steps without reaching done=true (evidence sufficient). Final artifact generation was skipped."
            )
            (logs_dir / f"{capability}-reasoning.md").write_text(
                "\n\n".join(section for section in reasoning_sections if section),
                encoding="utf-8",
            )

            evidence = default_build_evidence(
                capability,
                payload,
                {},
                observations,
                react_trace,
                tool_results,
                expected_files,
                candidate_output_files,
                output_plan,
            )
            evidence.setdefault("source_files", candidate_files)
            evidence.setdefault(
                "tool_trace",
                [
                    {
                        "tool_name": result["tool_name"],
                        "status": result["status"],
                        "error_code": result["error_code"],
                        "duration_ms": result["duration_ms"],
                    }
                    for result in tool_results
                ],
            )
            evidence.setdefault("react_trace", react_trace)
            evidence["failure_reason"] = "max_steps_exhausted"
            (evidence_dir / f"{capability}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            history_updates.append(f"[{capability}] Completed with status: failed")
            return {
                "history": history_updates,
                "task_queue": update_task_status_fn(state["task_queue"], capability, "failed"),
                "human_intervention_required": False,
                "last_worker": capability,
                "tool_results": tool_results,
            }

        final_trace: List[Dict[str, Any]] = []
        finalization_observations: List[Dict[str, Any]] = []
        workspace_paths = _persist_workspace_snapshot(
            project_path=project_path,
            payload=payload,
            capability=capability,
            candidate_files=candidate_files,
            candidate_output_files=candidate_output_files,
            expected_files=expected_files,
            output_plan=output_plan,
            observations=observations,
            react_trace=react_trace,
            upstream_artifacts=upstream_artifacts,
            artifacts_dir=artifacts_dir,
            work_dir=work_dir,
            final_trace=final_trace,
            agent_config=agent_config,
        )

        artifacts_output: Dict[str, str] = {}
        final_reasoning_sections: List[str] = []
        timeout_fallback_records: List[Dict[str, Any]] = []

        if execution_guard_fn:
            abort_info = execution_guard_fn()
            if abort_info:
                return _build_abort_result(abort_info)

        if generate_final_artifacts_fn:
            llm_output = await asyncio.to_thread(
                generate_final_artifacts_fn,
                payload,
                observations,
                templates,
                expected_files,
            )

            if any(not (llm_output.artifacts.get(name) or "").strip() for name in expected_files):
                if fallback_artifacts_fn:
                    llm_output = fallback_artifacts_fn(payload, observations, expected_files)
                else:
                    llm_output = default_fallback_artifacts(capability, payload, observations, expected_files)

            artifacts_output = dict(llm_output.artifacts)
            final_reasoning_sections.append(llm_output.reasoning)

            for artifact_name in expected_files:
                artifact_content = artifacts_output.get(artifact_name, "")
                if Path(artifact_name).suffix.lower() == ".md":
                    artifact_content, removed_sections = _dedupe_markdown_sections(artifact_content)
                    if removed_sections:
                        history_updates.append(
                            f"[{capability}] Final artifact `{artifact_name}` removed {removed_sections} duplicate markdown section(s) before persistence."
                        )
                    artifact_content, was_trimmed = _enforce_markdown_budget(
                        artifact_content,
                        _resolve_output_char_budget(state, capability, artifact_name),
                    )
                    if was_trimmed:
                        history_updates.append(
                            f"[{capability}] Final artifact `{artifact_name}` exceeded the markdown size budget and was truncated by the controller."
                        )
                        final_reasoning_sections.append(
                            f"Controller truncated `{artifact_name}` to the configured markdown size budget."
                        )
                artifacts_output[artifact_name] = artifact_content
                (artifacts_dir / artifact_name).write_text(artifact_content, encoding="utf-8")
        else:
            finalization_budget = _estimate_finalization_budget(
                state=state,
                expected_files=expected_files,
            )
            history_updates.append(
                f"[SYSTEM] Finalization budget resolved to {finalization_budget} step(s)."
            )
            finalization_completed = not expected_files
            step = 0

            for target_file in _ordered_selected_outputs(output_plan, expected_files):
                generation_batches = _build_generation_batches(target_file, output_plan)
                for batch in generation_batches:
                    if step >= finalization_budget:
                        break

                    target_path = artifacts_dir / target_file
                    step += 1
                    combined_observations = observations + finalization_observations
                    workspace_paths = _persist_workspace_snapshot(
                        project_path=project_path,
                        payload=payload,
                        capability=capability,
                        candidate_files=candidate_files,
                        candidate_output_files=candidate_output_files,
                        expected_files=expected_files,
                        output_plan=output_plan,
                        observations=combined_observations,
                        react_trace=react_trace,
                        upstream_artifacts=upstream_artifacts,
                        artifacts_dir=artifacts_dir,
                        work_dir=work_dir,
                        final_trace=final_trace,
                        agent_config=agent_config,
                    )
                    artifact_status = _collect_artifact_status(artifacts_dir, expected_files)
                    coverage_brief_for_batch = _read_workspace_json(artifacts_dir / workspace_paths["coverage_brief"])
                    artifact_char_budget = _resolve_output_char_budget(state, capability, target_file)
                    generation_exception: Optional[Exception] = None
                    try:
                        llm_output = await asyncio.to_thread(
                            default_generate_artifact_for_output,
                            _generate_with_selected_llm,
                            capability,
                            project_id,
                            version,
                            payload,
                            workspace_paths,
                            artifacts_dir,
                            target_file,
                            output_plan,
                            templates,
                            agent_config,
                            step,
                            batch.get("section_focus") or [],
                            int(batch.get("batch_index") or 1),
                            int(batch.get("batch_total") or 1),
                        )
                        generated_content = str(llm_output.artifacts.get(target_file) or "")
                        if llm_output.reasoning:
                            final_reasoning_sections.append(llm_output.reasoning)
                    except Exception as exc:
                        generation_exception = exc
                        if _is_timeout_exception(exc):
                            generated_content = _build_timeout_fallback_fragment(
                                target_file=target_file,
                                section_focus=list(batch.get("section_focus") or []),
                                batch_index=int(batch.get("batch_index") or 1),
                                batch_total=int(batch.get("batch_total") or 1),
                                output_plan=output_plan,
                                coverage_brief=coverage_brief_for_batch,
                            )
                            final_reasoning_sections.append(
                                f"Timeout fallback used for {target_file} batch {batch.get('batch_index')}/{batch.get('batch_total')}: {exc}"
                            )
                            timeout_fallback_records.append(
                                {
                                    "target_file": target_file,
                                    "batch_index": int(batch.get("batch_index") or 1),
                                    "batch_total": int(batch.get("batch_total") or 1),
                                    "error": str(exc),
                                }
                            )
                            history_updates.append(
                                f"[{capability}] Finalization step {step}: LLM timeout while generating `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')}`; writing controller fallback fragment instead."
                            )
                        else:
                            raise

                    if not generated_content.strip():
                        if fallback_artifacts_fn:
                            fallback_output = fallback_artifacts_fn(payload, combined_observations, [target_file])
                        else:
                            fallback_output = default_fallback_artifacts(capability, payload, combined_observations, [target_file])
                        generated_content = str(fallback_output.artifacts.get(target_file) or "")
                        if fallback_output.reasoning:
                            final_reasoning_sections.append(fallback_output.reasoning)

                    current_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
                    markdown_upsert_sections: List[Dict[str, Any]] = []
                    if Path(target_file).suffix.lower() == ".md":
                        generated_content, removed_sections = _dedupe_markdown_sections(
                            generated_content,
                            current_content,
                        )
                        if removed_sections:
                            history_updates.append(
                                f"[{capability}] Finalization step {step}: removed {removed_sections} duplicate markdown section(s) from `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')}."
                            )
                        if not generated_content.strip() and current_content:
                            history_updates.append(
                                f"[{capability}] Finalization step {step}: skipped `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')}` because it only repeated existing sections."
                            )
                            continue
                        generated_content, was_trimmed = _enforce_markdown_budget(generated_content, artifact_char_budget)
                        if was_trimmed:
                            history_updates.append(
                                f"[{capability}] Finalization step {step}: controller truncated `{target_file}` to the markdown size budget."
                            )
                        if _should_use_markdown_upsert(state):
                            markdown_upsert_sections = _markdown_content_to_upsert_sections(generated_content)
                    if markdown_upsert_sections and "upsert_markdown_sections" in effective_tool_set:
                        tool_name = "upsert_markdown_sections"
                        tool_input = {
                            "path": target_file,
                            "sections": markdown_upsert_sections,
                            "dedupe_strategy": "heading_or_similar",
                            "similarity_threshold": 0.9,
                            "root_dir": str(artifacts_dir),
                        }
                        decision_tool_input = {
                            "path": target_file,
                            "section_count": len(markdown_upsert_sections),
                            "dedupe_strategy": "heading_or_similar",
                            "similarity_threshold": 0.9,
                        }
                    is_append_batch = bool(current_content) and int(batch.get("batch_total") or 1) > 1 and int(batch.get("batch_index") or 1) > 1
                    if markdown_upsert_sections and "upsert_markdown_sections" not in effective_tool_set:
                        history_updates.append(
                            f"[{capability}] Finalization step {step}: markdown upsert is not permitted for `{target_file}`; falling back to the permitted file write path."
                        )

                    if not (markdown_upsert_sections and "upsert_markdown_sections" in effective_tool_set):
                        content_to_persist = generated_content
                        if is_append_batch:
                            content_to_persist = f"{current_content.rstrip()}\n\n{generated_content.lstrip()}"
                            if Path(target_file).suffix.lower() == ".md":
                                content_to_persist, removed_sections = _dedupe_markdown_sections(content_to_persist)
                                if removed_sections:
                                    history_updates.append(
                                        f"[{capability}] Finalization step {step}: removed {removed_sections} duplicate markdown section(s) after merging `{target_file}`."
                                    )
                                content_to_persist, was_trimmed = _enforce_markdown_budget(content_to_persist, artifact_char_budget)
                                if was_trimmed:
                                    history_updates.append(
                                        f"[{capability}] Finalization step {step}: controller truncated `{target_file}` to the markdown size budget."
                                    )

                        if target_path.exists() and "patch_file" in effective_tool_set:
                            tool_name = "patch_file"
                            tool_input = {
                                "path": target_file,
                                "old_content": current_content,
                                "new_content": content_to_persist,
                                "root_dir": str(artifacts_dir),
                            }
                            decision_tool_input = {
                                "path": target_file,
                                "old_content_summary": f"<omitted {len(current_content)} chars>",
                                "new_content_summary": f"<omitted {len(content_to_persist)} chars>",
                            }
                        elif "write_file" in effective_tool_set:
                            tool_name = "write_file"
                            tool_input = {
                                "path": target_file,
                                "content": content_to_persist,
                                "root_dir": str(artifacts_dir),
                            }
                            decision_tool_input = {
                                "path": target_file,
                                "content_summary": f"<omitted {len(content_to_persist)} chars>",
                            }
                        else:
                            raise RuntimeError(
                                f"Finalization cannot persist `{target_file}` because expert `{capability}` does not permit any writable finalization tool."
                            )

                    decision = {
                        "done": False,
                        "thought": f"Generate and persist `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')} based on the selected output plan and grounded workspace context.",
                        "tool_name": tool_name,
                        "tool_input": decision_tool_input,
                        "actions": [],
                        "evidence_note": f"Produce the planned final artifact `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')}.",
                        "target_file": target_file,
                        "batch_index": batch.get("batch_index"),
                        "batch_total": batch.get("batch_total"),
                        "section_focus": batch.get("section_focus") or [],
                        "reasoning": (
                            f"Timeout fallback fragment generated by controller: {generation_exception}"
                            if generation_exception and _is_timeout_exception(generation_exception)
                            else (llm_output.reasoning if 'llm_output' in locals() else "")
                        ),
                    }
                    final_trace.append({"step": step, **decision, "artifact_status": artifact_status})
                    final_step_log_path = _write_finalization_step_log(
                        logs_dir=logs_dir,
                        capability=capability,
                        step=step,
                        decision=decision,
                        artifact_status=artifact_status,
                        workspace_paths=workspace_paths,
                        project_root=project_path,
                    )
                    history_updates.append(
                        f"[{capability}] Finalization step {step}: step log written to logs/finalization/{capability}/{final_step_log_path.name}."
                    )
                    history_updates.append(
                        f"[{capability}] Finalization step {step}: writing planned artifact `{target_file}` batch {batch.get('batch_index')}/{batch.get('batch_total')}."
                    )

                    tool_result = await asyncio.to_thread(_execute_tool_with_permission, tool_name, tool_input)
                    tool_results.append(tool_result)
                    history_updates.extend(default_tool_history_entries(tool_name, tool_result))
                    executed_action_summaries = [
                        {
                            "action_index": 1,
                            "tool_name": tool_name,
                            "status": tool_result.get("status"),
                            "error_code": tool_result.get("error_code"),
                            "duration_ms": tool_result.get("duration_ms"),
                        }
                    ]
                    finalization_observations.append(
                        {
                            "step": step,
                            "action_index": 1,
                            "tool_name": tool_name,
                            "tool_input": _sanitize_prompt_payload(tool_result.get("input") or {}, project_path),
                            "tool_output": _sanitize_prompt_payload(tool_result.get("output") or {}, project_path),
                            "evidence_note": decision.get("evidence_note", ""),
                            "stage": "finalization",
                            "target_file": target_file,
                            "batch_index": batch.get("batch_index"),
                            "batch_total": batch.get("batch_total"),
                        }
                    )
                    final_trace[-1]["tool_results"] = executed_action_summaries
                    _write_finalization_step_log(
                        logs_dir=logs_dir,
                        capability=capability,
                        step=step,
                        decision=final_trace[-1],
                        artifact_status=_collect_artifact_status(artifacts_dir, expected_files),
                        workspace_paths=workspace_paths,
                        project_root=project_path,
                        tool_results=executed_action_summaries,
                    )

                if step >= finalization_budget:
                    break

            finalization_completed = _all_expected_artifacts_complete(artifacts_dir, expected_files)
            if not finalization_completed:
                combined_observations = observations + finalization_observations
                workspace_paths = _persist_workspace_snapshot(
                    project_path=project_path,
                    payload=payload,
                    capability=capability,
                    candidate_files=candidate_files,
                    candidate_output_files=candidate_output_files,
                    expected_files=expected_files,
                    output_plan=output_plan,
                    observations=combined_observations,
                    react_trace=react_trace,
                    upstream_artifacts=upstream_artifacts,
                    artifacts_dir=artifacts_dir,
                    work_dir=work_dir,
                    final_trace=final_trace,
                    agent_config=agent_config,
                )
                reasoning_sections = [entry.get("reasoning", "") for entry in react_trace if entry.get("reasoning")]
                reasoning_sections.extend(final_reasoning_sections)
                reasoning_sections.append(
                    f"Finalization loop exhausted {finalization_budget} steps without producing all expected artifacts."
                )
                (logs_dir / f"{capability}-reasoning.md").write_text(
                    "\n\n".join(section for section in reasoning_sections if section),
                    encoding="utf-8",
                )

                evidence = default_build_evidence(
                    capability,
                    payload,
                    {},
                    observations + finalization_observations,
                    react_trace + final_trace,
                    tool_results,
                    expected_files,
                    candidate_output_files,
                    output_plan,
                )
                evidence.setdefault("source_files", candidate_files)
                evidence.setdefault(
                    "tool_trace",
                    [
                        {
                            "tool_name": result["tool_name"],
                            "status": result["status"],
                            "error_code": result["error_code"],
                            "duration_ms": result["duration_ms"],
                        }
                        for result in tool_results
                    ],
                )
                evidence["failure_reason"] = "finalization_max_steps_exhausted"
                evidence["react_trace"] = react_trace
                evidence["finalization_trace"] = final_trace
                evidence["workspace_paths"] = workspace_paths
                (evidence_dir / f"{capability}.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                history_updates.append(f"[{capability}] Completed with status: failed")
                return {
                    "history": history_updates,
                    "task_queue": update_task_status_fn(state["task_queue"], capability, "failed"),
                    "human_intervention_required": False,
                    "last_worker": capability,
                    "tool_results": tool_results,
                }

            artifacts_output = {
                artifact_name: (artifacts_dir / artifact_name).read_text(encoding="utf-8")
                for artifact_name in expected_files
            }

        combined_observations = observations + finalization_observations
        workspace_paths = _persist_workspace_snapshot(
            project_path=project_path,
            payload=payload,
            capability=capability,
            candidate_files=candidate_files,
            candidate_output_files=candidate_output_files,
            expected_files=expected_files,
            output_plan=output_plan,
            observations=combined_observations,
            react_trace=react_trace,
            upstream_artifacts=upstream_artifacts,
            artifacts_dir=artifacts_dir,
            work_dir=work_dir,
            final_trace=final_trace,
            agent_config=agent_config,
        )

        # Write reasoning
        reasoning_sections = [entry.get("reasoning", "") for entry in react_trace if entry.get("reasoning")]
        reasoning_sections.extend(final_reasoning_sections)
        (logs_dir / f"{capability}-reasoning.md").write_text(
            "\n\n".join(section for section in reasoning_sections if section),
            encoding="utf-8",
        )

        # Build evidence
        evidence = default_build_evidence(
            capability,
            payload,
            artifacts_output,
            combined_observations,
            react_trace + final_trace,
            tool_results,
            expected_files,
            candidate_output_files,
            output_plan,
        )
        evidence.setdefault("source_files", candidate_files)
        evidence.setdefault(
            "tool_trace",
            [
                {
                    "tool_name": result["tool_name"],
                    "status": result["status"],
                    "error_code": result["error_code"],
                    "duration_ms": result["duration_ms"],
                }
                for result in tool_results
            ],
        )
        evidence["react_trace"] = react_trace
        evidence["finalization_trace"] = final_trace
        evidence["workspace_paths"] = workspace_paths
        if timeout_fallback_records:
            evidence["failure_reason"] = "finalization_timeout_fallback"
            evidence["timeout_fallbacks"] = timeout_fallback_records
        (evidence_dir / f"{capability}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        final_status = "failed" if timeout_fallback_records else "success"
        history_updates.append(f"[{capability}] Completed with status: {final_status}")
        return {
            "history": history_updates,
            "task_queue": update_task_status_fn(state["task_queue"], capability, final_status),
            "human_intervention_required": False,
            "last_worker": capability,
            "tool_results": tool_results,
        }
    except Exception as exc:
        history_updates.append(f"[{capability}] [ERROR] {exc}")
        return {
            "history": history_updates,
            "task_queue": update_task_status_fn(state["task_queue"], capability, "failed"),
            "human_intervention_required": False,
            "last_worker": capability,
            "tool_results": tool_results,
        }


def _load_templates_for_capability(
    base_dir: Path,
    capability: str,
    agent_config: Optional["AgentFullConfig"],
) -> Dict[str, str]:
    """Load templates for a specific capability."""
    templates = {}
    
    # First, try to use templates from agent_config
    configured_templates = getattr(agent_config, "templates", None) if agent_config else None
    if configured_templates:
        templates.update(configured_templates)
    
    # Then, load from default template directory
    template_dir = base_dir / "skills" / capability / "assets" / "templates"
    if template_dir.exists():
        for template_file in template_dir.glob("*"):
            if template_file.is_file():
                templates[template_file.name] = template_file.read_text(encoding="utf-8")
    
    # Also check global templates
    global_template_dir = base_dir / "assets" / "templates"
    if global_template_dir.exists():
        for template_file in global_template_dir.glob("*"):
            if template_file.is_file() and template_file.name not in templates:
                templates[template_file.name] = template_file.read_text(encoding="utf-8")
    
    return templates


# Cross-agent memory: upstream artifact mapping
# Defines which upstream artifacts each agent should read for cross-agent memory
# Note: This is a fallback when registry is not available; prefer registry configuration
UPSTREAM_ARTIFACT_MAPPING_FALLBACK: Dict[str, Dict[str, List[str]]] = {
    "rules-management": {
        "requirement-clarification": ["requirement-clarification.md", "scope-and-assumptions.md", "glossary.md"],
    },
    "business-form-operation": {
        "requirement-clarification": ["requirement-clarification.md", "scope-and-assumptions.md", "glossary.md"],
        "rules-management": ["business-rules.md", "decision-tables.md", "rule-parameters.yaml"],
    },
    "process-control": {
        "requirement-clarification": ["requirement-clarification.md", "scope-and-assumptions.md", "glossary.md"],
        "rules-management": ["business-rules.md", "decision-tables.md"],
        "business-form-operation": ["business-form-operations.md", "field-requirements.yaml", "operation-permissions.md", "form-data-analysis.md"],
    },
    "integration-requirements": {
        "requirement-clarification": ["requirement-clarification.md", "scope-and-assumptions.md", "glossary.md"],
        "process-control": ["process-requirements.md", "state-transition.md", "exception-handling.md"],
        "business-form-operation": ["field-requirements.yaml", "form-data-analysis.md"],
    },
    "validator": {
        "ir-assembler": ["it-requirements.md", "requirement-traceability.json", "acceptance-criteria.md", "open-questions.md"],
    },
}


def _get_expected_artifacts_by_agent() -> Dict[str, List[str]]:
    """Get expected artifacts mapping from registry. Returns {capability: [expected_outputs]}."""
    try:
        from registry.expert_registry import ExpertRegistry
        registry = ExpertRegistry.get_instance()
        return {
            manifest.capability: manifest.expected_outputs
            for manifest in registry.get_all_manifests()
            if manifest.expected_outputs
        }
    except RuntimeError:
        return {}


def _get_upstream_artifact_mapping() -> Dict[str, Dict[str, List[str]]]:
    """Get upstream artifact mapping from normalized runtime configuration."""
    return _get_upstream_artifact_mapping_from_profiles()


def _discover_upstream_artifacts(capability: str, artifacts_dir: Path) -> Dict[str, List[str]]:
    discovered = _call_skill_runtime_hook(
        capability,
        "discover_upstream_artifacts",
        artifacts_dir=artifacts_dir,
    )
    if isinstance(discovered, dict):
        return {
            str(owner): [_normalize_relative_path(str(item)) for item in files]
            for owner, files in discovered.items()
            if isinstance(files, list)
        }
    return _discover_upstream_artifacts_from_profiles(capability, artifacts_dir)


def _default_expected_files(capability: str) -> List[str]:
    """Get default expected files for a capability from registry."""
    artifacts_map = _get_expected_artifacts_by_agent()
    if capability in artifacts_map:
        return artifacts_map[capability]
    return ["output.md"]
