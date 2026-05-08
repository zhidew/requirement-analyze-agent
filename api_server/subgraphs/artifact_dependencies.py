"""Artifact dependency helpers for dynamic subagents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registry.expert_runtime_profile import ExpertRuntimeProfile


UPSTREAM_ARTIFACT_MAPPING_FALLBACK: dict[str, dict[str, list[str]]] = {
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
        "process-control": ["process-requirements.md", "state-transition.md", "exception-handling.md", "workflow-configuration.yaml"],
        "business-form-operation": ["field-requirements.yaml", "form-data-analysis.md"],
    },
    "validator": {
        "ir-assembler": ["it-requirements.md", "requirement-traceability.json", "acceptance-criteria.md", "open-questions.md"],
    },
}


def get_upstream_artifact_mapping(
    profiles: dict[str, ExpertRuntimeProfile] | None = None,
) -> dict[str, dict[str, list[str]]]:
    merged: dict[str, dict[str, list[str]]] = {
        capability: dict(mapping)
        for capability, mapping in UPSTREAM_ARTIFACT_MAPPING_FALLBACK.items()
    }

    if profiles:
        for capability, profile in profiles.items():
            if profile.upstream_artifacts:
                merged[capability] = dict(profile.upstream_artifacts)
        if merged:
            return merged

    try:
        from registry.expert_registry import ExpertRegistry

        registry = ExpertRegistry.get_instance()
        result: dict[str, dict[str, list[str]]] = {}
        for manifest in registry.get_all_manifests():
            if manifest.upstream_artifacts:
                result[manifest.capability] = manifest.upstream_artifacts
        return {**merged, **result} if result else merged
    except RuntimeError:
        return merged


def discover_upstream_artifacts(
    capability: str,
    artifacts_dir: Path,
    mapping: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, list[str]]:
    upstream_map = (mapping or get_upstream_artifact_mapping()).get(capability, {})
    if not upstream_map or not artifacts_dir.exists():
        return {}

    existing_files = {item.name for item in artifacts_dir.iterdir() if item.is_file()}
    discovered: dict[str, list[str]] = {}
    for upstream_agent, expected_files in upstream_map.items():
        found = [file_name for file_name in expected_files if file_name in existing_files]
        if found:
            discovered[upstream_agent] = found
    return discovered

