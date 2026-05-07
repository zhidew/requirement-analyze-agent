"""Topic ownership payload helpers for planner and subagents."""

from __future__ import annotations

from typing import Any

from registry.expert_runtime_profile import (
    ExpertRuntimeProfile,
    LEGACY_DEFAULT_CAPABILITY_TOPICS,
    LEGACY_SHARED_CONTEXT_OWNER_CAPABILITIES,
    build_runtime_profiles,
)


GENERIC_SHARED_CONTEXT_SECTION_EXAMPLES = "业务背景 / RR概述 / 竞品参考摘要 / 范围说明 / 目标结果"
DEFAULT_SHARED_CONTEXT_OWNER_CAPABILITIES = ["requirement-clarification", "ir-assembler"]
DEFAULT_SHARED_CONTEXT_TOPICS = [
    "business_background",
    "raw_requirement_overview",
    "competitor_reference_summary",
    "scope",
    "target_outcomes",
]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def build_default_topic_ownership(active_agents: list[str]) -> dict[str, Any]:
    normalized_agents = _dedupe_preserve_order([str(agent).strip() for agent in active_agents if str(agent).strip()])
    shared_context_owners = [capability for capability in normalized_agents if capability in LEGACY_SHARED_CONTEXT_OWNER_CAPABILITIES]
    if not shared_context_owners:
        if normalized_agents:
            shared_context_owners = [normalized_agents[0]]
        else:
            shared_context_owners = list(DEFAULT_SHARED_CONTEXT_OWNER_CAPABILITIES)

    capability_topics = {
        capability: list(LEGACY_DEFAULT_CAPABILITY_TOPICS.get(capability, [capability.replace("-", "_")]))
        for capability in normalized_agents
    }

    return {
        "shared_context_owner_capabilities": shared_context_owners,
        "shared_context_topics": list(DEFAULT_SHARED_CONTEXT_TOPICS),
        "capability_topics": capability_topics,
        "generic_shared_context_section_examples": GENERIC_SHARED_CONTEXT_SECTION_EXAMPLES,
    }


def build_topic_ownership_from_profiles(
    profiles: dict[str, ExpertRuntimeProfile],
    active_agents: list[str],
) -> dict[str, Any]:
    normalized_agents = _dedupe_preserve_order([str(agent).strip() for agent in active_agents if str(agent).strip()])
    if not profiles:
        return build_default_topic_ownership(normalized_agents)

    payload = build_default_topic_ownership(normalized_agents)
    shared_context_owners = [
        capability
        for capability in normalized_agents
        if profiles.get(capability) is not None and profiles[capability].owns_shared_context is True
    ]
    if shared_context_owners:
        payload["shared_context_owner_capabilities"] = shared_context_owners

    capability_topics: dict[str, list[str]] = {}
    for capability in normalized_agents:
        profile = profiles.get(capability)
        if profile is None:
            capability_topics[capability] = list(LEGACY_DEFAULT_CAPABILITY_TOPICS.get(capability, [capability.replace("-", "_")]))
            continue
        capability_topics[capability] = list(profile.topics or LEGACY_DEFAULT_CAPABILITY_TOPICS.get(capability, [capability.replace("-", "_")]))
        if profile.shared_context_topics:
            payload["shared_context_topics"] = list(profile.shared_context_topics)
        if profile.generic_shared_context_section_examples:
            payload["generic_shared_context_section_examples"] = profile.generic_shared_context_section_examples
    payload["capability_topics"] = capability_topics
    return resolve_topic_ownership(payload)




def build_topic_ownership_payload(active_agents: list[str]) -> dict[str, Any]:
    normalized_agents = _dedupe_preserve_order([str(agent).strip() for agent in active_agents if str(agent).strip()])
    if not normalized_agents:
        return build_default_topic_ownership([])

    try:
        from registry.expert_registry import ExpertRegistry

        registry = ExpertRegistry.get_instance()
    except RuntimeError:
        return build_default_topic_ownership(normalized_agents)
    except Exception:
        return build_default_topic_ownership(normalized_agents)

    expert_configs: list[Any] = []
    for capability in normalized_agents:
        try:
            expert_configs.append(registry.load_full_config(capability))
        except Exception:
            continue

    if not expert_configs:
        return build_default_topic_ownership(normalized_agents)

    return build_topic_ownership_from_profiles(build_runtime_profiles(expert_configs), normalized_agents)
def resolve_topic_ownership(
    topic_ownership: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(topic_ownership, dict):
        return build_default_topic_ownership([])

    shared_context_owners = _dedupe_preserve_order(
        [str(item).strip() for item in topic_ownership.get("shared_context_owner_capabilities") or [] if str(item).strip()]
    )
    if not shared_context_owners:
        shared_context_owners = build_default_topic_ownership([])["shared_context_owner_capabilities"]

    shared_context_topics = _dedupe_preserve_order(
        [str(item).strip() for item in topic_ownership.get("shared_context_topics") or [] if str(item).strip()]
    ) or list(DEFAULT_SHARED_CONTEXT_TOPICS)

    raw_capability_topics = topic_ownership.get("capability_topics") or {}
    capability_topics: dict[str, list[str]] = {}
    if isinstance(raw_capability_topics, dict):
        for capability, topics in raw_capability_topics.items():
            normalized_capability = str(capability).strip()
            if not normalized_capability:
                continue
            capability_topics[normalized_capability] = _dedupe_preserve_order(
                [str(item).strip() for item in topics or [] if str(item).strip()]
            )

    return {
        "shared_context_owner_capabilities": shared_context_owners,
        "shared_context_topics": shared_context_topics,
        "capability_topics": capability_topics,
        "generic_shared_context_section_examples": str(
            topic_ownership.get("generic_shared_context_section_examples") or GENERIC_SHARED_CONTEXT_SECTION_EXAMPLES
        ).strip(),
    }


