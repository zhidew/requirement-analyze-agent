"""Runtime profile normalization for expert configuration.

This module is the boundary between raw expert YAML/SKILL metadata and
subagent prompt/runtime helpers. It keeps parsing, defaults, validation
warnings, and source tracking out of ``dynamic_subagent.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


GENERIC_BOUNDARY_NOTE = "Keep each artifact concise and limited to this BA requirement-analysis expert's primary responsibility."

LEGACY_CAPABILITY_SCOPE_NOTES: dict[str, str] = {}

LEGACY_SHARED_CONTEXT_OWNER_CAPABILITIES = {"requirement-clarification", "ir-assembler"}

LEGACY_DEFAULT_CAPABILITY_TOPICS: dict[str, list[str]] = {}

LEGACY_CAPABILITY_KEYWORDS: dict[str, list[str]] = {}

KNOWN_METADATA_KEYS = {
    "boundary_contract",
    "topic_ownership",
    "routing",
    "prompt_hints",
    "delivery_contract",
    "interaction",
    "execution",
    "expected_outputs",
    "upstream_artifacts",
}


@dataclass
class ExpertRuntimeProfile:
    capability: str
    boundary_note: str
    topics: list[str]
    owns_shared_context: bool | None
    routing_keywords: list[str]
    prompt_hints: dict[str, Any]
    delivery_contract: dict[str, Any]
    upstream_artifacts: dict[str, list[str]]
    source_map: dict[str, str]
    validation_warnings: list[str]
    boundary_contract: dict[str, Any] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    shared_context_topics: list[str] = field(default_factory=list)
    generic_shared_context_section_examples: str = ""
    interaction: dict[str, Any] = field(default_factory=dict)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _ensure_string_list(value: Any, *, field_name: str, warnings: list[str], limit: int = 64) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        warnings.append(f"{field_name}: expected list[str] or str, got {type(value).__name__}; ignored.")
        return []

    normalized: list[str] = []
    for item in values:
        if not isinstance(item, (str, int, float)):
            warnings.append(f"{field_name}: ignored non-scalar item of type {type(item).__name__}.")
            continue
        text = str(item).strip()
        if text:
            normalized.append(text)
    return _dedupe_preserve_order(normalized)[:limit]


def _normalize_relative_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raw_path = str(raw_path or "")
    return raw_path.strip().replace("\\", "/").lstrip("./")


def _normalize_artifact_mapping(value: Any, *, field_name: str, warnings: list[str]) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{field_name}: expected mapping, got {type(value).__name__}; ignored.")
        return {}

    normalized: dict[str, list[str]] = {}
    for upstream, outputs in value.items():
        upstream_id = str(upstream).strip()
        if not upstream_id:
            warnings.append(f"{field_name}: ignored empty upstream capability key.")
            continue
        files = [_normalize_relative_path(item) for item in _ensure_string_list(outputs, field_name=f"{field_name}.{upstream_id}", warnings=warnings)]
        normalized[upstream_id] = [item for item in files if item]
    return normalized


def _normalize_string_map(value: Any, *, field_name: str, warnings: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{field_name}: expected mapping, got {type(value).__name__}; ignored.")
        return {}

    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _normalize_relative_path(raw_key)
        if not key:
            warnings.append(f"{field_name}: ignored empty key.")
            continue
        if not isinstance(raw_value, (str, int, float)):
            warnings.append(f"{field_name}.{key}: expected scalar guidance, got {type(raw_value).__name__}; ignored.")
            continue
        text = str(raw_value).strip()
        if text:
            normalized[key] = text
    return normalized


def _normalize_review_checklist(value: Any, *, field_name: str, warnings: list[str]) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{field_name}: expected mapping, got {type(value).__name__}; ignored.")
        return {}

    normalized: dict[str, list[str]] = {}
    for raw_path, items in value.items():
        path = _normalize_relative_path(raw_path)
        if not path:
            warnings.append(f"{field_name}: ignored empty path key.")
            continue
        normalized[path] = _ensure_string_list(items, field_name=f"{field_name}.{path}", warnings=warnings)
    return normalized


def _manifest_list(manifest: Any, attr_name: str) -> list[str]:
    value = getattr(manifest, attr_name, []) if manifest is not None else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _manifest_mapping(manifest: Any, attr_name: str) -> dict[str, list[str]]:
    value = getattr(manifest, attr_name, {}) if manifest is not None else {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): [str(item).strip() for item in items if str(item).strip()]
        for key, items in value.items()
        if str(key).strip() and isinstance(items, list)
    }


def _render_boundary_note(boundary_contract: dict[str, Any], capability: str) -> str:
    owns = boundary_contract.get("owns") or []
    excludes = boundary_contract.get("excludes") or []
    upstream_inputs = boundary_contract.get("upstream_inputs") or []
    parts: list[str] = []
    if owns:
        parts.append(f"Own {', '.join(owns)}.")
    if excludes:
        parts.append(f"Do not expand into {', '.join(excludes)}.")
    if upstream_inputs:
        parts.append(f"Use upstream inputs from {', '.join(upstream_inputs)} when relevant.")
    if parts:
        return " ".join(parts)
    return LEGACY_CAPABILITY_SCOPE_NOTES.get(capability, GENERIC_BOUNDARY_NOTE)


def normalize_expert_metadata(
    raw_metadata: dict[str, Any] | None,
    manifest: Any | None,
) -> dict[str, Any]:
    warnings: list[str] = []
    raw = raw_metadata if isinstance(raw_metadata, dict) else {}
    if raw_metadata is not None and not isinstance(raw_metadata, dict):
        warnings.append(f"metadata: expected mapping, got {type(raw_metadata).__name__}; ignored.")

    unknown_keys = sorted(str(key) for key in raw.keys() if str(key) not in KNOWN_METADATA_KEYS)
    for key in unknown_keys:
        warnings.append(f"metadata.{key}: unknown metadata key; preserved by registry but ignored by runtime profile.")

    boundary_raw = raw.get("boundary_contract") or {}
    if boundary_raw and not isinstance(boundary_raw, dict):
        warnings.append(f"metadata.boundary_contract: expected mapping, got {type(boundary_raw).__name__}; ignored.")
        boundary_raw = {}
    boundary_contract = {
        "owns": _ensure_string_list(boundary_raw.get("owns"), field_name="metadata.boundary_contract.owns", warnings=warnings),
        "excludes": _ensure_string_list(boundary_raw.get("excludes"), field_name="metadata.boundary_contract.excludes", warnings=warnings),
        "upstream_inputs": _ensure_string_list(boundary_raw.get("upstream_inputs"), field_name="metadata.boundary_contract.upstream_inputs", warnings=warnings),
    }

    topic_raw = raw.get("topic_ownership") or {}
    if topic_raw and not isinstance(topic_raw, dict):
        warnings.append(f"metadata.topic_ownership: expected mapping, got {type(topic_raw).__name__}; ignored.")
        topic_raw = {}
    owns_shared_context = topic_raw.get("owns_shared_context")
    if owns_shared_context is not None and not isinstance(owns_shared_context, bool):
        warnings.append("metadata.topic_ownership.owns_shared_context: expected bool; ignored.")
        owns_shared_context = None
    topic_ownership = {
        "topics": _ensure_string_list(topic_raw.get("topics"), field_name="metadata.topic_ownership.topics", warnings=warnings),
        "owns_shared_context": owns_shared_context,
        "shared_context_topics": _ensure_string_list(topic_raw.get("shared_context_topics"), field_name="metadata.topic_ownership.shared_context_topics", warnings=warnings),
        "generic_shared_context_section_examples": str(topic_raw.get("generic_shared_context_section_examples") or "").strip(),
    }

    routing_raw = raw.get("routing") or {}
    if routing_raw and not isinstance(routing_raw, dict):
        warnings.append(f"metadata.routing: expected mapping, got {type(routing_raw).__name__}; ignored.")
        routing_raw = {}
    routing = {
        "keywords": _ensure_string_list(routing_raw.get("keywords"), field_name="metadata.routing.keywords", warnings=warnings),
    }

    prompt_raw = raw.get("prompt_hints") or {}
    if prompt_raw and not isinstance(prompt_raw, dict):
        warnings.append(f"metadata.prompt_hints: expected mapping, got {type(prompt_raw).__name__}; ignored.")
        prompt_raw = {}
    prompt_hints = {
        "file_guidance": _normalize_string_map(prompt_raw.get("file_guidance"), field_name="metadata.prompt_hints.file_guidance", warnings=warnings),
        "default_file_guidance": str(prompt_raw.get("default_file_guidance") or "").strip(),
    }

    delivery_raw = raw.get("delivery_contract") or {}
    if delivery_raw and not isinstance(delivery_raw, dict):
        warnings.append(f"metadata.delivery_contract: expected mapping, got {type(delivery_raw).__name__}; ignored.")
        delivery_raw = {}
    delivery_contract = {
        "must_answer": _ensure_string_list(delivery_raw.get("must_answer"), field_name="metadata.delivery_contract.must_answer", warnings=warnings),
        "evidence_expectations": _ensure_string_list(delivery_raw.get("evidence_expectations"), field_name="metadata.delivery_contract.evidence_expectations", warnings=warnings),
        "artifact_review_checklist": _normalize_review_checklist(
            delivery_raw.get("artifact_review_checklist"),
            field_name="metadata.delivery_contract.artifact_review_checklist",
            warnings=warnings,
        ),
    }

    interaction_raw = raw.get("interaction") or {}
    if interaction_raw and not isinstance(interaction_raw, dict):
        warnings.append(f"metadata.interaction: expected mapping, got {type(interaction_raw).__name__}; ignored.")
        interaction_raw = {}
    clarification_raw = interaction_raw.get("clarification") or {}
    if clarification_raw and not isinstance(clarification_raw, dict):
        warnings.append(
            f"metadata.interaction.clarification: expected mapping, got {type(clarification_raw).__name__}; ignored."
        )
        clarification_raw = {}
    interaction = {
        "clarification": {
            "supported_question_types": _ensure_string_list(
                clarification_raw.get("supported_question_types"),
                field_name="metadata.interaction.clarification.supported_question_types",
                warnings=warnings,
            ),
            "default_topics": _ensure_string_list(
                clarification_raw.get("default_topics"),
                field_name="metadata.interaction.clarification.default_topics",
                warnings=warnings,
            ),
            "answer_merge_targets": _ensure_string_list(
                clarification_raw.get("answer_merge_targets"),
                field_name="metadata.interaction.clarification.answer_merge_targets",
                warnings=warnings,
            ),
        }
    }

    expected_outputs = _ensure_string_list(raw.get("expected_outputs"), field_name="metadata.expected_outputs", warnings=warnings)
    if not expected_outputs:
        expected_outputs = _manifest_list(manifest, "expected_outputs")

    upstream_artifacts = _normalize_artifact_mapping(raw.get("upstream_artifacts"), field_name="metadata.upstream_artifacts", warnings=warnings)
    if not upstream_artifacts:
        upstream_artifacts = _manifest_mapping(manifest, "upstream_artifacts")

    return {
        "boundary_contract": boundary_contract,
        "topic_ownership": topic_ownership,
        "routing": routing,
        "prompt_hints": prompt_hints,
        "delivery_contract": delivery_contract,
        "interaction": interaction,
        "expected_outputs": expected_outputs,
        "upstream_artifacts": upstream_artifacts,
        "_warnings": warnings,
        "_raw_keys": set(raw.keys()),
    }


def validate_expert_metadata(normalized_metadata: dict[str, Any]) -> list[str]:
    warnings = list(normalized_metadata.get("_warnings") or [])
    expected_outputs = normalized_metadata.get("expected_outputs") or []
    upstream_artifacts = normalized_metadata.get("upstream_artifacts") or {}
    if expected_outputs and not all(isinstance(item, str) and item.strip() for item in expected_outputs):
        warnings.append("metadata.expected_outputs: normalized output list contains invalid entries.")
    if upstream_artifacts and not isinstance(upstream_artifacts, dict):
        warnings.append("metadata.upstream_artifacts: normalized value must be a mapping.")
    return warnings


def _source(raw_keys: set[str], metadata_key: str, *, manifest_has_value: bool = False, legacy_has_value: bool = False) -> str:
    if metadata_key in raw_keys:
        return "configured"
    if manifest_has_value:
        return "manifest"
    if legacy_has_value:
        return "legacy_fallback"
    return "generic_fallback"


def resolve_expert_runtime_profile(
    capability: str,
    agent_config: Any | None,
) -> ExpertRuntimeProfile:
    manifest = getattr(agent_config, "manifest", None) if agent_config is not None else None
    raw_metadata = getattr(agent_config, "metadata", None) if agent_config is not None else None
    normalized = normalize_expert_metadata(raw_metadata, manifest)
    warnings = validate_expert_metadata(normalized)
    raw_keys = normalized.get("_raw_keys") or set()

    boundary_contract = normalized["boundary_contract"]
    topic_ownership = normalized["topic_ownership"]
    routing = normalized["routing"]
    prompt_hints = normalized["prompt_hints"]
    delivery_contract = normalized["delivery_contract"]
    interaction = normalized["interaction"]
    clarification_cfg = dict(interaction.get("clarification") or {})
    if not clarification_cfg.get("supported_question_types"):
        clarification_cfg["supported_question_types"] = ["single_select", "long_text"]
    if not clarification_cfg.get("answer_merge_targets"):
        clarification_cfg["answer_merge_targets"] = ["clarified_requirements", "decision_log"]
    interaction = {"clarification": clarification_cfg}

    topics = topic_ownership.get("topics") or LEGACY_DEFAULT_CAPABILITY_TOPICS.get(capability) or [capability.replace("-", "_")]
    owns_shared_context = topic_ownership.get("owns_shared_context")
    if owns_shared_context is None and capability in LEGACY_SHARED_CONTEXT_OWNER_CAPABILITIES:
        owns_shared_context = True

    manifest_keywords = _dedupe_preserve_order(_manifest_list(manifest, "keywords"))
    base_keywords = capability.replace("-", " ").split()
    routing_keywords = routing.get("keywords") or manifest_keywords or _dedupe_preserve_order(base_keywords + LEGACY_CAPABILITY_KEYWORDS.get(capability, []))

    source_map = {
        "boundary_note": _source(raw_keys, "boundary_contract", legacy_has_value=capability in LEGACY_CAPABILITY_SCOPE_NOTES),
        "topics": _source(raw_keys, "topic_ownership", legacy_has_value=capability in LEGACY_DEFAULT_CAPABILITY_TOPICS),
        "owns_shared_context": _source(raw_keys, "topic_ownership", legacy_has_value=capability in LEGACY_SHARED_CONTEXT_OWNER_CAPABILITIES),
        "routing_keywords": _source(raw_keys, "routing", manifest_has_value=bool(manifest_keywords), legacy_has_value=capability in LEGACY_CAPABILITY_KEYWORDS),
        "prompt_hints": _source(raw_keys, "prompt_hints"),
        "delivery_contract": _source(raw_keys, "delivery_contract"),
        "interaction": _source(raw_keys, "interaction"),
        "upstream_artifacts": _source(raw_keys, "upstream_artifacts", manifest_has_value=bool(_manifest_mapping(manifest, "upstream_artifacts"))),
        "expected_outputs": _source(raw_keys, "expected_outputs", manifest_has_value=bool(_manifest_list(manifest, "expected_outputs"))),
    }

    return ExpertRuntimeProfile(
        capability=capability,
        boundary_note=_render_boundary_note(boundary_contract, capability),
        topics=list(topics),
        owns_shared_context=owns_shared_context,
        routing_keywords=list(routing_keywords),
        prompt_hints=dict(prompt_hints),
        delivery_contract=dict(delivery_contract),
        upstream_artifacts=dict(normalized["upstream_artifacts"]),
        source_map=source_map,
        validation_warnings=warnings,
        boundary_contract=boundary_contract,
        expected_outputs=list(normalized["expected_outputs"]),
        shared_context_topics=list(topic_ownership.get("shared_context_topics") or []),
        generic_shared_context_section_examples=str(topic_ownership.get("generic_shared_context_section_examples") or ""),
        interaction=dict(interaction),
    )


def build_runtime_profiles(
    agent_configs: list[Any],
) -> dict[str, ExpertRuntimeProfile]:
    profiles: dict[str, ExpertRuntimeProfile] = {}
    for agent_config in agent_configs:
        manifest = getattr(agent_config, "manifest", None)
        capability = str(getattr(manifest, "capability", "") or "").strip()
        if not capability:
            continue
        profiles[capability] = resolve_expert_runtime_profile(capability, agent_config)
    return profiles


