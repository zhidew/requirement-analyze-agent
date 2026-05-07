from __future__ import annotations

import re
from typing import Any, Dict, List


UNRESOLVED_MARKERS = ("TODO", "TBD", "待确认", "待补充", "未确认", "unknown", "to be confirmed")
UNTRUSTED_ARTIFACT_STATUSES = {
    "reflection_failed",
    "system_check_failed",
    "user_disputed",
    "revision_requested",
    "content_missing",
    "content_drifted",
}

TO_BE_TOKENS = (
    "希望",
    "未来",
    "目标",
    "应该支持",
    "需要支持",
    "想要",
    "计划",
    "to-be",
    "to be",
    "should support",
    "need to support",
    "target",
    "future",
)
AS_IS_TOKENS = (
    "现在",
    "当前",
    "已经",
    "实际",
    "事实",
    "线上",
    "目前",
    "as-is",
    "as is",
    "currently",
    "already",
    "actually",
    "in production",
)
PREFERENCE_TOKENS = ("prefer", "preference", "偏好", "倾向", "更喜欢", "风格", "命名", "展示为", "文案")
CORRECTION_TOKENS = ("wrong", "incorrect", "错误", "不对", "修正", "纠正", "改正", "不是这样", "事实不符", "correct")
CONFLICT_TOKENS = ("conflict", "冲突", "矛盾", "不一致", "instead", "不是", "但 schema", "但数据库")
SUPPLEMENT_TOKENS = ("补充", "新增", "另外", "还有", "also", "additional", "add context", "new information")


def find_unresolved_markers(content: str) -> List[str]:
    found: List[str] = []
    lowered = content.lower()
    for marker in UNRESOLVED_MARKERS:
        if marker.lower() in lowered:
            found.append(marker)
    return found


def classify_revision_feedback(feedback: str) -> Dict[str, str]:
    value = feedback or ""
    lowered = value.lower()

    if any(token in lowered or token in value for token in PREFERENCE_TOKENS):
        revision_type = "preference"
    elif any(token in lowered or token in value for token in CORRECTION_TOKENS):
        revision_type = "correction"
    elif any(token in lowered or token in value for token in CONFLICT_TOKENS):
        revision_type = "conflict"
    elif any(token in lowered or token in value for token in SUPPLEMENT_TOKENS):
        revision_type = "supplement"
    else:
        revision_type = "supplement"

    if any(token in lowered or token in value for token in TO_BE_TOKENS):
        as_is_or_to_be = "to_be"
        semantic = "to_be_change"
    elif revision_type == "preference":
        as_is_or_to_be = "unknown"
        semantic = "preference_override"
    elif any(token in lowered or token in value for token in AS_IS_TOKENS):
        as_is_or_to_be = "as_is"
        semantic = "as_is_conflict" if revision_type in {"correction", "conflict"} else "missing_context"
    elif revision_type in {"correction", "conflict"}:
        as_is_or_to_be = "as_is"
        semantic = "as_is_conflict"
    else:
        as_is_or_to_be = "unknown"
        semantic = "missing_context"

    return {
        "revision_type": revision_type,
        "as_is_or_to_be": as_is_or_to_be,
        "semantic": semantic,
    }


def extract_schema_mentions(feedback: str) -> List[Dict[str, str]]:
    mentions: List[Dict[str, str]] = []
    seen = set()
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", feedback or ""):
        table = match.group(1)
        column = match.group(2)
        key = (table.lower(), column.lower())
        if key in seen:
            continue
        seen.add(key)
        mentions.append({"table": table, "column": column, "raw": match.group(0)})
    return mentions


def extract_sql_schema_objects(sql_content: str) -> Dict[str, Any]:
    tables: Dict[str, Dict[str, Any]] = {}
    create_table_pattern = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([A-Za-z_][A-Za-z0-9_]*)[`\"\]]?\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    for match in create_table_pattern.finditer(sql_content or ""):
        table_name = match.group(1)
        table_key = table_name.lower()
        body = match.group(2)
        columns: Dict[str, str] = {}
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            first = line.split(None, 1)[0].strip("`\"[]").lower()
            if first in {"primary", "foreign", "constraint", "unique", "check", "key", "index"}:
                continue
            column_name = line.split(None, 1)[0].strip("`\"[],")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", column_name):
                columns[column_name.lower()] = column_name
        tables[table_key] = {"name": table_name, "columns": columns}
    return {"tables": tables}


def extract_requirement_terms(requirement_text: str) -> List[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", requirement_text or "")
    stop_words = {
        "with",
        "that",
        "this",
        "from",
        "have",
        "will",
        "must",
        "should",
        "design",
        "system",
        "project",
        "version",
    }
    normalized: List[str] = []
    for term in terms:
        lower = term.lower()
        if lower in stop_words or lower in normalized:
            continue
        normalized.append(lower)
    return normalized[:12]


def build_conflict(
    *,
    project_id: str,
    version_id: str,
    artifact_id: str,
    conflict_type: str,
    semantic: str,
    severity: str,
    status: str,
    summary: str,
    evidence_refs: List[Dict[str, Any]] | None = None,
    suggested_actions: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "version_id": version_id,
        "artifact_id": artifact_id,
        "conflict_type": conflict_type,
        "semantic": semantic,
        "severity": severity,
        "status": status,
        "summary": summary,
        "evidence_refs": evidence_refs or [],
        "suggested_actions": suggested_actions or [],
    }


def classify_upstream_status(upstream_artifact: Dict[str, Any]) -> Dict[str, Any]:
    status = str(upstream_artifact.get("status") or "unknown")
    if status in UNTRUSTED_ARTIFACT_STATUSES:
        return {
            "status": "failed",
            "severity": "blocking",
            "summary": f"Upstream artifact '{upstream_artifact.get('title') or upstream_artifact.get('artifact_id')}' is not in a trusted state: {status}.",
        }
    if status in {"reflection_warning", "ready_for_review"}:
        return {
            "status": "warning",
            "severity": "warning",
            "summary": f"Upstream artifact '{upstream_artifact.get('title') or upstream_artifact.get('artifact_id')}' has status {status}; downstream should keep this visible.",
        }
    return {
        "status": "passed",
        "severity": "info",
        "summary": "Upstream artifact is in a consumable state.",
    }
