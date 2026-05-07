from __future__ import annotations

from typing import Iterable, List


DEFAULT_READ_TOOLS = (
    "list_files",
    "extract_structure",
    "grep_search",
    "read_file_chunk",
    "extract_lookup_values",
    "clone_repository",
    "query_database",
    "query_knowledge_base",
)

DEFAULT_WRITE_TOOLS = (
    "write_file",
    "append_file",
    "upsert_markdown_sections",
    "patch_file",
)

WRITE_COMPANION_SOURCES = {
    "append_file": ("write_file", "patch_file"),
    "upsert_markdown_sections": ("write_file", "patch_file", "append_file"),
}

KNOWN_RUNTIME_TOOLS = (
    "list_files",
    "clone_repository",
    "extract_structure",
    "grep_search",
    "read_file_chunk",
    "extract_lookup_values",
    "query_database",
    "query_knowledge_base",
    "write_file",
    "append_file",
    "upsert_markdown_sections",
    "patch_file",
    "run_command",
    "validate_artifacts",
)


def normalize_explicit_tools(tools: Iterable[str] | None) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for raw_tool in tools or []:
        tool_name = str(raw_tool or "").strip()
        if not tool_name or tool_name in seen:
            continue
        normalized.append(tool_name)
        seen.add(tool_name)
    return normalized


def build_effective_tools(explicit_tools: Iterable[str] | None) -> List[str]:
    normalized_explicit = normalize_explicit_tools(explicit_tools)
    if "*" in normalized_explicit:
        return list(KNOWN_RUNTIME_TOOLS)

    effective_tools: List[str] = []
    seen: set[str] = set()

    for tool_name in DEFAULT_READ_TOOLS:
        effective_tools.append(tool_name)
        seen.add(tool_name)

    for tool_name in normalized_explicit:
        if tool_name in seen:
            continue
        effective_tools.append(tool_name)
        seen.add(tool_name)

    changed = True
    while changed:
        changed = False
        for tool_name, sources in WRITE_COMPANION_SOURCES.items():
            if tool_name in seen:
                continue
            if any(source in seen for source in sources):
                effective_tools.append(tool_name)
                seen.add(tool_name)
                changed = True

    return effective_tools


def has_effective_tool_permission(tool_name: str, explicit_tools: Iterable[str] | None) -> bool:
    return tool_name in set(build_effective_tools(explicit_tools))
