from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .list_files import _resolve_search_roots
from .standards import normalize_path_text, resolve_path_within_root


def read_file_chunk(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    relative_path = tool_input.get("path")
    search_root_label = tool_input.get("search_root", ".")
    start_line = int(tool_input.get("start_line", 1) or 1)
    end_line = int(tool_input.get("end_line", start_line + 19) or (start_line + 19))

    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("`path` must be a non-empty string.")
    if start_line < 1 or end_line < start_line:
        raise ValueError("Invalid line range.")

    search_roots = {
        item["label"]: item["path"]
        for item in _resolve_search_roots(root_dir, tool_input)
    }
    if search_root_label not in search_roots:
        raise ValueError(f"Unknown search_root: {search_root_label}")

    selected_root = search_roots[search_root_label]
    file_path, normalized_path = resolve_path_within_root(
        selected_root,
        relative_path,
        must_exist=True,
        expected_kind="file",
    )

    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[start_line - 1 : end_line]
    return {
        "root_dir": str(root_dir),
        "search_root": search_root_label,
        "path": normalize_path_text(normalized_path),
        "start_line": start_line,
        "end_line": min(end_line, len(lines)),
        "content": "\n".join(selected),
    }
