from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

from .standards import resolve_path_within_root


DEFAULT_SIMILARITY_THRESHOLD = 0.9


def upsert_markdown_sections(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    file_path_str = tool_input.get("path")
    raw_sections = tool_input.get("sections")
    dedupe_strategy = str(tool_input.get("dedupe_strategy") or "heading_or_similar").strip().lower()
    similarity_threshold = float(tool_input.get("similarity_threshold") or DEFAULT_SIMILARITY_THRESHOLD)

    if not file_path_str or not isinstance(file_path_str, str):
        raise ValueError("`path` is required and must be a string.")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("`sections` is required and must be a non-empty list.")
    if dedupe_strategy not in {"heading", "heading_or_similar"}:
        raise ValueError("`dedupe_strategy` must be `heading` or `heading_or_similar`.")

    target_path, normalized_path = resolve_path_within_root(root_dir, file_path_str, expected_kind="file")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    current_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    document = _parse_markdown_document(current_content)

    inserted_sections = 0
    replaced_sections = 0
    skipped_sections = 0

    for index, raw_section in enumerate(raw_sections):
        if not isinstance(raw_section, dict):
            raise ValueError(f"`sections[{index}]` must be an object.")
        heading = str(raw_section.get("heading") or "").strip()
        content = raw_section.get("content")
        mode = str(raw_section.get("mode") or "replace_by_heading").strip()
        if not heading:
            raise ValueError(f"`sections[{index}].heading` is required and must be a non-empty string.")
        if content is None or not isinstance(content, str):
            raise ValueError(f"`sections[{index}].content` is required and must be a string.")
        if mode not in {"replace_by_heading", "append_if_missing", "skip_if_similar"}:
            raise ValueError(
                "`sections[].mode` must be one of `replace_by_heading`, `append_if_missing`, or `skip_if_similar`."
            )

        new_section = _build_section(heading, content)
        existing_index = _find_section_by_heading(document["sections"], heading)

        if existing_index is not None:
            if mode == "append_if_missing":
                skipped_sections += 1
                continue
            if mode == "skip_if_similar":
                similarity = _section_similarity(document["sections"][existing_index]["content"], new_section["content"])
                if similarity >= similarity_threshold:
                    skipped_sections += 1
                    continue
            document["sections"][existing_index] = new_section
            replaced_sections += 1
            continue

        if dedupe_strategy == "heading_or_similar":
            if any(
                _section_similarity(section["content"], new_section["content"]) >= similarity_threshold
                for section in document["sections"]
            ):
                skipped_sections += 1
                continue

        document["sections"].append(new_section)
        inserted_sections += 1

    final_content = _render_markdown_document(document)
    target_path.write_text(final_content, encoding="utf-8")

    return {
        "path": normalized_path,
        "size_bytes": target_path.stat().st_size,
        "inserted_sections": inserted_sections,
        "replaced_sections": replaced_sections,
        "skipped_sections": skipped_sections,
        "message": f"Successfully upserted markdown sections in {normalized_path}",
    }


def _parse_markdown_document(content: str) -> Dict[str, Any]:
    lines = str(content or "").splitlines()
    preamble: List[str] = []
    sections: List[Dict[str, Any]] = []
    current_section: Optional[Dict[str, Any]] = None

    for line in lines:
        match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if current_section is not None:
                current_section["content"] = "\n".join(current_section["lines"]).strip()
                sections.append(current_section)
            current_section = {
                "heading_level": len(match.group(1)),
                "heading": match.group(2).strip(),
                "lines": [],
                "content": "",
            }
            continue
        if current_section is None:
            preamble.append(line)
        else:
            current_section["lines"].append(line)

    if current_section is not None:
        current_section["content"] = "\n".join(current_section["lines"]).strip()
        sections.append(current_section)

    return {
        "preamble": "\n".join(preamble).strip(),
        "sections": sections,
    }


def _render_markdown_document(document: Dict[str, Any]) -> str:
    blocks: List[str] = []
    preamble = str(document.get("preamble") or "").strip()
    if preamble:
        blocks.append(preamble)

    for section in document.get("sections") or []:
        heading_level = int(section.get("heading_level") or 2)
        heading = str(section.get("heading") or "").strip()
        content = str(section.get("content") or "").strip()
        header = f"{'#' * max(1, min(6, heading_level))} {heading}".strip()
        section_block = header if not content else f"{header}\n\n{content}"
        blocks.append(section_block.strip())

    rendered = "\n\n".join(block for block in blocks if block)
    if rendered:
        rendered += "\n"
    return rendered


def _build_section(heading: str, content: str, heading_level: int = 2) -> Dict[str, Any]:
    return {
        "heading_level": heading_level,
        "heading": heading,
        "content": str(content).strip(),
    }


def _find_section_by_heading(sections: List[Dict[str, Any]], heading: str) -> Optional[int]:
    target_key = _normalize_heading_key(heading)
    for index, section in enumerate(sections):
        if _normalize_heading_key(section.get("heading") or "") == target_key:
            return index
    return None


def _normalize_heading_key(heading: str) -> str:
    text = str(heading or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_section_body(content: str) -> str:
    text = re.sub(r"`[^`]+`", " ", str(content or ""))
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _section_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_section_body(left)
    normalized_right = _normalize_section_body(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    shorter_length = min(len(normalized_left), len(normalized_right))
    if shorter_length >= 80 and (normalized_left in normalized_right or normalized_right in normalized_left):
        return 0.99
    return SequenceMatcher(None, normalized_left[:4000], normalized_right[:4000]).ratio()
