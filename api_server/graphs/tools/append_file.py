from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .standards import resolve_path_within_root


def append_file(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    file_path_str = tool_input.get("path")
    content = tool_input.get("content")

    if not file_path_str or not isinstance(file_path_str, str):
        raise ValueError("`path` is required and must be a string.")
    if content is None or not isinstance(content, str):
        raise ValueError("`content` is required and must be a string.")

    target_path, normalized_path = resolve_path_within_root(root_dir, file_path_str, expected_kind="file")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    previous_size = target_path.stat().st_size if target_path.exists() else 0
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(content)

    return {
        "path": normalized_path,
        "size_bytes": target_path.stat().st_size,
        "appended_bytes": target_path.stat().st_size - previous_size,
        "message": f"Successfully appended to {normalized_path}",
    }
