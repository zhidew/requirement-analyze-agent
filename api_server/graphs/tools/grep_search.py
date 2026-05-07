from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .list_files import _resolve_search_roots


def grep_search(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("`pattern` must be a non-empty string.")

    matches = []
    search_roots = _resolve_search_roots(root_dir, tool_input)
    for search_root in search_roots:
        base_path = search_root["path"]
        for file_path in sorted(path for path in base_path.rglob("*") if path.is_file()):
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(content.splitlines(), start=1):
                if pattern.lower() in line.lower():
                    matches.append(
                        {
                            "path": file_path.relative_to(base_path).as_posix(),
                            "search_root": search_root["label"],
                            "line_number": line_number,
                            "line": line.strip(),
                        }
                    )

    return {
        "root_dir": str(root_dir),
        "pattern": pattern,
        "search_roots": [item["label"] for item in search_roots],
        "matches": matches[:50],
    }
