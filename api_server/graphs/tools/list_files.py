from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .standards import resolve_search_roots


def _resolve_search_roots(root_dir: Path, tool_input: Dict[str, Any]):
    return resolve_search_roots(root_dir, tool_input.get("repos_dir"))


def list_files(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    files = []
    search_roots = _resolve_search_roots(root_dir, tool_input)
    for search_root in search_roots:
        base_path = search_root["path"]
        for file_path in sorted(path for path in base_path.rglob("*") if path.is_file()):
            files.append(
                {
                    "name": file_path.name,
                    "path": file_path.relative_to(base_path).as_posix(),
                    "search_root": search_root["label"],
                    "extension": file_path.suffix.lower(),
                    "size_bytes": file_path.stat().st_size,
                }
            )
    return {
        "root_dir": str(root_dir),
        "search_roots": [item["label"] for item in search_roots],
        "files": files,
    }
