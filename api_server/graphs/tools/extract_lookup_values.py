from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .standards import resolve_path_within_root


def extract_lookup_values(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    requested_files = tool_input.get("files")
    candidate_paths = []
    if isinstance(requested_files, list) and requested_files:
        for relative_path in requested_files:
            if not isinstance(relative_path, str) or not relative_path.strip():
                continue
            resolved_path, _ = resolve_path_within_root(root_dir, relative_path, expected_kind="file")
            candidate_paths.append(resolved_path)
    else:
        candidate_paths = [path for path in root_dir.rglob("*") if path.is_file()]

    lookup_files = []
    entries: List[Dict[str, Any]] = []
    warnings = []
    for file_path in sorted(candidate_paths):
        relative_path = file_path.relative_to(root_dir).as_posix()
        if not _is_lookup_candidate(file_path):
            continue
        lookup_files.append(relative_path)
        try:
            parsed_entries = _extract_entries_from_file(file_path, relative_path)
            entries.extend(parsed_entries)
        except Exception as exc:
            warnings.append(f"{relative_path}: {exc}")

    return {
        "root_dir": str(root_dir),
        "lookup_files": lookup_files,
        "entries": entries,
        "warnings": warnings,
    }


def _is_lookup_candidate(file_path: Path) -> bool:
    lowered_name = file_path.name.lower()
    return any(token in lowered_name for token in ("lookup", "dict", "dictionary", "enum"))


def _extract_entries_from_file(file_path: Path, relative_path: str) -> List[Dict[str, Any]]:
    suffix = file_path.suffix.lower()
    content = file_path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        return _extract_entries_from_json(content, relative_path, file_path.stem)
    return _extract_entries_from_text(content, relative_path, file_path.stem)


def _extract_entries_from_json(content: str, relative_path: str, fallback_name: str) -> List[Dict[str, Any]]:
    parsed = json.loads(content)
    if isinstance(parsed, dict):
        entries = []
        for key, value in parsed.items():
            extracted_values = _coerce_values(value)
            if extracted_values:
                entries.append({"name": key, "values": extracted_values, "source_path": relative_path})
        return entries
    if isinstance(parsed, list):
        extracted_values = _coerce_values(parsed)
        return [{"name": fallback_name, "values": extracted_values, "source_path": relative_path}] if extracted_values else []
    return []


def _extract_entries_from_text(content: str, relative_path: str, fallback_name: str) -> List[Dict[str, Any]]:
    entries = []
    pattern = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*[:：]\s*([A-Za-z0-9_\-,\s/]+)$")
    for line in content.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        name = match.group(1).strip().lower().replace("-", "_")
        values = [item.strip() for item in re.split(r"[,/]", match.group(2)) if item.strip()]
        if values:
            entries.append({"name": name, "values": values, "source_path": relative_path})
    if entries:
        return entries

    uppercase_tokens = re.findall(r"\b[A-Z][A-Z0-9_]{1,}\b", content)
    unique_tokens = []
    for token in uppercase_tokens:
        if token not in unique_tokens:
            unique_tokens.append(token)
    return [{"name": fallback_name, "values": unique_tokens[:20], "source_path": relative_path}] if unique_tokens else []


def _coerce_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(key) for key in value.keys() if str(key).strip()]
    return []
