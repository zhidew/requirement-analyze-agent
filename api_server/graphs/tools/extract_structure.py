from __future__ import annotations

import json
import re
from ast import AsyncFunctionDef, ClassDef, FunctionDef, parse
from pathlib import Path
from typing import Any, Dict, List

from .standards import resolve_path_within_root


def extract_structure(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    file_paths = tool_input.get("files")
    if not isinstance(file_paths, list):
        raise ValueError("`files` must be a list of relative paths.")

    summaries = []
    missing_files = []

    for relative_path in file_paths:
        if not isinstance(relative_path, str) or not relative_path.strip():
            continue
        try:
            file_path, normalized_path = resolve_path_within_root(root_dir, relative_path, expected_kind="file")
        except ValueError as exc:
            raise ValueError(f"File path escapes root: {relative_path}") from exc

        if not file_path.exists() or not file_path.is_file():
            missing_files.append(normalized_path)
            continue

        summaries.append(_summarize_file(root_dir, file_path))

    return {
        "root_dir": str(root_dir),
        "files": summaries,
        "missing_files": missing_files,
    }


def _summarize_file(root_dir: Path, file_path: Path) -> Dict[str, Any]:
    suffix = file_path.suffix.lower()
    content = file_path.read_text(encoding="utf-8", errors="replace")
    summary: Dict[str, Any] = {
        "path": file_path.relative_to(root_dir).as_posix(),
        "name": file_path.name,
        "extension": suffix,
        "size_bytes": file_path.stat().st_size,
        "line_count": len(content.splitlines()),
    }

    if suffix in {".md", ".txt"}:
        headings = [
            match.group(1).strip()
            for line in content.splitlines()
            if (match := re.match(r"^#{1,6}\s+(.*)$", line.strip()))
        ]
        summary["summary_type"] = "document_outline"
        summary["headings"] = headings[:12]
        summary["heading_count"] = len(headings)
        return summary

    if suffix == ".json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            summary["summary_type"] = "plain_text"
            return summary
        summary["summary_type"] = "json_keys"
        summary["top_level_keys"] = _json_keys(parsed, max_depth=2)
        return summary

    if suffix == ".py":
        summary["summary_type"] = "python_symbols"
        summary["symbols"] = _python_symbols(content)
        return summary

    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        summary["summary_type"] = "code_symbols"
        summary["symbols"] = _pattern_symbols(content)
        return summary

    summary["summary_type"] = "plain_text"
    return summary


def _json_keys(value: Any, prefix: str = "", max_depth: int = 2, depth: int = 0) -> List[str]:
    if depth >= max_depth or not isinstance(value, dict):
        return []

    keys: List[str] = []
    for key, nested in value.items():
        current_key = f"{prefix}.{key}" if prefix else str(key)
        keys.append(current_key)
        keys.extend(_json_keys(nested, current_key, max_depth=max_depth, depth=depth + 1))
    return keys[:20]


def _python_symbols(content: str) -> List[str]:
    try:
        tree = parse(content)
    except SyntaxError:
        return []

    symbols = []
    for node in tree.body:
        if isinstance(node, ClassDef):
            symbols.append(f"class:{node.name}")
        elif isinstance(node, (FunctionDef, AsyncFunctionDef)):
            symbols.append(f"function:{node.name}")
    return symbols[:20]


def _pattern_symbols(content: str) -> List[str]:
    symbols = []
    patterns = [
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\binterface\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\btype\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
        r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"\bconst\s+([A-Za-z_][A-Za-z0-9_]*)\s*=",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            symbols.append(match.group(1))
    unique_symbols = []
    for symbol in symbols:
        if symbol not in unique_symbols:
            unique_symbols.append(symbol)
    return unique_symbols[:20]
