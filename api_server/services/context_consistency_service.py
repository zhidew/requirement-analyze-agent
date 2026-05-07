from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.db_service import metadata_db
from subgraphs.context_conflict_checker import (
    build_conflict,
    classify_upstream_status,
    classify_revision_feedback,
    extract_schema_mentions,
    extract_sql_schema_objects,
    extract_requirement_terms,
    find_unresolved_markers,
)


BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _project_version_root(project_id: str, version_id: str) -> Path:
    return PROJECTS_DIR / project_id / version_id


def _read_artifact_file(artifact: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    path = (_project_version_root(artifact["project_id"], artifact["version_id"]) / artifact["file_path"]).resolve()
    root = _project_version_root(artifact["project_id"], artifact["version_id"]).resolve()
    if root not in path.parents and path != root:
        return None, "Artifact path escapes project version root."
    if not path.exists():
        return None, "Artifact file is missing."
    try:
        return path.read_text(encoding="utf-8"), None
    except Exception as exc:
        return None, f"Artifact file could not be read: {exc}"


def _load_requirement_text(project_id: str, version_id: str) -> str:
    root = _project_version_root(project_id, version_id)
    candidates = [
        root / "baseline" / "raw-requirements.md",
        root / "baseline" / "original-requirements.md",
        root / "baseline" / "input-requirements.md",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return ""
    return ""


def _make_check(check_id: str, status: str, message: str, evidence_refs: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "message": message,
        "evidence_refs": evidence_refs or [],
    }


def _read_artifact_text_if_available(artifact: Dict[str, Any]) -> str:
    content, error = _read_artifact_file(artifact)
    return "" if error else (content or "")


def _schema_artifacts_for_version(project_id: str, version_id: str) -> List[Dict[str, Any]]:
    artifacts = metadata_db.list_design_artifacts(project_id, version_id)
    return [
        item
        for item in artifacts
        if item.get("artifact_type") == "sql" or str(item.get("file_name") or "").lower().endswith(".sql")
    ]


def _single_address_schema_conflicts(
    *,
    feedback: str,
    feedback_classification: Dict[str, str],
    target_artifact: Dict[str, Any],
    sql_artifacts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    lowered = feedback.lower()
    mentions_multiple_addresses = (
        ("address" in lowered or "地址" in feedback)
        and ("multiple" in lowered or "many" in lowered or "多个" in feedback or "多地址" in feedback or "多个收货地址" in feedback)
    )
    mentions_order = "order" in lowered or "订单" in feedback or "orders" in lowered
    if not (mentions_order and mentions_multiple_addresses):
        return []

    conflicts: List[Dict[str, Any]] = []
    for sql_artifact in sql_artifacts:
        content = _read_artifact_text_if_available(sql_artifact)
        schema = extract_sql_schema_objects(content)
        orders = schema["tables"].get("orders") or schema["tables"].get("order")
        if not orders:
            continue
        columns = orders.get("columns") or {}
        if "address_id" not in columns:
            continue
        semantic = feedback_classification["semantic"]
        severity = "warning" if semantic in {"to_be_change", "missing_context"} else "blocking"
        conflicts.append(
            build_conflict(
                project_id=target_artifact["project_id"],
                version_id=target_artifact["version_id"],
                artifact_id=target_artifact["artifact_id"],
                conflict_type="user_vs_database",
                semantic=semantic,
                severity=severity,
                status="open",
                summary="用户反馈提到订单支持多个地址，但当前 SQL Artifact 中 orders 表表现为单个 address_id 字段。",
                evidence_refs=[
                    {"type": "user_feedback", "excerpt": feedback[:500]},
                    {
                        "type": "artifact",
                        "artifact_id": sql_artifact["artifact_id"],
                        "file_path": sql_artifact.get("file_path"),
                        "sql_object": "orders.address_id",
                    },
                ],
                suggested_actions=[
                    "作为 To-Be 目标继续，并生成数据模型变更建议。",
                    "按当前数据库 As-Is 调整专家产出。",
                    "标记待确认，并在用户修订后提示相关下游重新校验。",
                ],
            )
        )
    return conflicts[:1]


def _explicit_schema_mention_conflicts(
    *,
    feedback: str,
    feedback_classification: Dict[str, str],
    target_artifact: Dict[str, Any],
    sql_artifacts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    mentions = extract_schema_mentions(feedback)
    if not mentions:
        return []

    conflicts: List[Dict[str, Any]] = []
    schema_sources: List[Dict[str, Any]] = []
    for sql_artifact in sql_artifacts:
        content = _read_artifact_text_if_available(sql_artifact)
        schema_sources.append({"artifact": sql_artifact, "schema": extract_sql_schema_objects(content)})

    for mention in mentions:
        table_key = mention["table"].lower()
        column_key = mention["column"].lower()
        matched_table_source = None
        for source in schema_sources:
            table = (source["schema"].get("tables") or {}).get(table_key)
            if not table:
                continue
            matched_table_source = source
            if column_key in (table.get("columns") or {}):
                matched_table_source = None
                break
        if not matched_table_source:
            continue
        semantic = feedback_classification["semantic"]
        severity = "warning" if semantic in {"to_be_change", "missing_context"} else "blocking"
        sql_artifact = matched_table_source["artifact"]
        conflicts.append(
            build_conflict(
                project_id=target_artifact["project_id"],
                version_id=target_artifact["version_id"],
                artifact_id=target_artifact["artifact_id"],
                conflict_type="user_vs_database",
                semantic=semantic,
                severity=severity,
                status="open",
                summary=f"用户反馈提到 {mention['raw']}，但当前 SQL Artifact 中 {mention['table']} 表没有该字段。",
                evidence_refs=[
                    {"type": "user_feedback", "excerpt": feedback[:500], "schema_mention": mention},
                    {
                        "type": "artifact",
                        "artifact_id": sql_artifact["artifact_id"],
                        "file_path": sql_artifact.get("file_path"),
                        "sql_object": mention["table"],
                    },
                ],
                suggested_actions=[
                    "确认用户反馈是当前事实还是目标设计。",
                    "如为 To-Be，生成数据库迁移和下游影响建议。",
                    "如以当前数据库为准，调整专家产出避免引用不存在字段。",
                ],
            )
        )
    return conflicts


def detect_revision_feedback_conflicts(revision_session_id: str) -> Dict[str, Any]:
    session = metadata_db.get_revision_session(revision_session_id)
    if not session:
        raise ValueError("Revision session not found.")
    target_artifact = metadata_db.get_design_artifact(session["target_artifact_id"])
    if not target_artifact:
        raise ValueError("Target artifact not found.")

    feedback = str(session.get("user_feedback") or "")
    classification = classify_revision_feedback(feedback)
    sql_artifacts = _schema_artifacts_for_version(session["project_id"], session["version_id"])
    candidate_conflicts: List[Dict[str, Any]] = []

    if classification["revision_type"] != "preference":
        candidate_conflicts.extend(
            _explicit_schema_mention_conflicts(
                feedback=feedback,
                feedback_classification=classification,
                target_artifact=target_artifact,
                sql_artifacts=sql_artifacts,
            )
        )
        candidate_conflicts.extend(
            _single_address_schema_conflicts(
                feedback=feedback,
                feedback_classification=classification,
                target_artifact=target_artifact,
                sql_artifacts=sql_artifacts,
            )
        )

    conflict_ids: List[str] = []
    for conflict in candidate_conflicts:
        created = metadata_db.create_context_conflict(
            conflict_id=str(uuid.uuid4()),
            report_id=None,
            **conflict,
        )
        conflict_ids.append(created["conflict_id"])

    decision_required = any(conflict.get("severity") == "blocking" for conflict in candidate_conflicts)
    return {
        **classification,
        "candidate_conflicts": conflict_ids,
        "decision_required": decision_required,
        "conflict_count": len(conflict_ids),
    }


def run_consistency_check(artifact_id: str) -> Dict[str, Any]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")

    project_id = artifact["project_id"]
    version_id = artifact["version_id"]
    reflection = metadata_db.get_reflection_report_for_artifact(artifact_id)
    content, read_error = _read_artifact_file(artifact)
    checks: List[Dict[str, Any]] = []
    candidate_conflicts: List[Dict[str, Any]] = []

    if read_error:
        checks.append(_make_check("artifact_content_integrity", "failed", read_error))
        candidate_conflicts.append(
            build_conflict(
                project_id=project_id,
                version_id=version_id,
                artifact_id=artifact_id,
                conflict_type="artifact_vs_context",
                semantic="as_is_conflict",
                severity="blocking",
                status="open",
                summary=read_error,
                evidence_refs=[{"type": "artifact", "artifact_id": artifact_id, "file_path": artifact.get("file_path")}],
                suggested_actions=["重新同步 Artifact 指向的文件快照。"],
            )
        )
    else:
        content_hash = _sha256_text(content or "")
        if content_hash != artifact.get("content_hash"):
            message = "Artifact file content hash does not match the registered snapshot."
            checks.append(_make_check("artifact_content_integrity", "failed", message))
            candidate_conflicts.append(
                build_conflict(
                    project_id=project_id,
                    version_id=version_id,
                    artifact_id=artifact_id,
                    conflict_type="artifact_vs_context",
                    semantic="as_is_conflict",
                    severity="blocking",
                    status="open",
                    summary=message,
                    evidence_refs=[
                        {"type": "artifact", "artifact_id": artifact_id, "registered_hash": artifact.get("content_hash")},
                        {"type": "file", "path": artifact.get("file_path"), "actual_hash": content_hash},
                    ],
                    suggested_actions=["生成新的 Artifact 版本，或恢复旧文件快照。"],
                )
            )
        else:
            checks.append(_make_check("artifact_content_integrity", "passed", "Artifact file hash matches the registered snapshot."))

    dependency_refs = artifact.get("dependency_refs") or []
    upstream_checks = []
    for ref in dependency_refs:
        upstream_id = ref.get("artifact_id") if isinstance(ref, dict) else str(ref or "")
        if not upstream_id:
            continue
        upstream = metadata_db.get_design_artifact(upstream_id)
        if not upstream:
            upstream_checks.append(_make_check("artifact_vs_upstream", "unknown", f"Upstream artifact '{upstream_id}' was not found."))
            continue
        classification = classify_upstream_status(upstream)
        upstream_checks.append(_make_check("artifact_vs_upstream", classification["status"], classification["summary"]))
        if classification["severity"] == "blocking":
            candidate_conflicts.append(
                build_conflict(
                    project_id=project_id,
                    version_id=version_id,
                    artifact_id=artifact_id,
                    conflict_type="artifact_vs_artifact",
                    semantic="as_is_conflict",
                    severity="blocking",
                    status="open",
                    summary=classification["summary"],
                    evidence_refs=[
                        {"type": "artifact", "artifact_id": artifact_id},
                        {"type": "artifact", "artifact_id": upstream_id, "status": upstream.get("status")},
                    ],
                    suggested_actions=["先处理上游 Artifact 的反思或系统一致性问题。"],
                )
            )
    if upstream_checks:
        checks.extend(upstream_checks)
    else:
        checks.append(_make_check("artifact_vs_upstream", "unknown", "No explicit upstream Artifact dependency references were registered."))

    requirement_text = _load_requirement_text(project_id, version_id)
    if requirement_text and content:
        terms = extract_requirement_terms(requirement_text)
        matched_terms = [term for term in terms if term in content.lower()]
        if terms and not matched_terms:
            checks.append(
                _make_check(
                    "artifact_vs_confirmed_requirements",
                    "warning",
                    "No prominent requirement keywords were found in the artifact; this may be normal for narrow technical outputs.",
                    [{"type": "baseline", "path": "baseline/raw-requirements.md", "sample_terms": terms[:6]}],
                )
            )
        else:
            checks.append(_make_check("artifact_vs_confirmed_requirements", "passed", "Artifact content overlaps with confirmed requirement context."))
    else:
        checks.append(_make_check("artifact_vs_confirmed_requirements", "unknown", "No baseline requirement text was available for comparison."))

    database_configs = metadata_db.list_databases(project_id)
    if content and artifact.get("artifact_type") == "sql":
        markers = find_unresolved_markers(content)
        if markers:
            message = f"SQL artifact contains unresolved markers: {', '.join(markers)}."
            checks.append(_make_check("artifact_vs_database_schema", "warning", message))
            candidate_conflicts.append(
                build_conflict(
                    project_id=project_id,
                    version_id=version_id,
                    artifact_id=artifact_id,
                    conflict_type="artifact_vs_context",
                    semantic="missing_context",
                    severity="warning",
                    status="open",
                    summary=message,
                    evidence_refs=[{"type": "artifact", "artifact_id": artifact_id, "markers": markers}],
                    suggested_actions=["补齐字段/表的上下文，或在修订会话中标记为待确认。"],
                )
            )
        elif database_configs:
            checks.append(_make_check("artifact_vs_database_schema", "unknown", "Database configs exist, but no live schema snapshot is registered for deterministic comparison."))
        else:
            checks.append(_make_check("artifact_vs_database_schema", "unknown", "No database config or schema snapshot is available."))
    else:
        checks.append(_make_check("artifact_vs_database_schema", "unknown", "Artifact is not a SQL schema artifact; database comparison was not applicable."))

    knowledge_bases = metadata_db.list_knowledge_bases(project_id)
    if knowledge_bases:
        checks.append(_make_check("artifact_vs_knowledge_base", "unknown", "Knowledge base configs exist, but no retrieved evidence snapshot is attached to this artifact."))
    else:
        checks.append(_make_check("artifact_vs_knowledge_base", "unknown", "No knowledge base config is available for comparison."))

    if reflection and reflection.get("blocks_downstream"):
        message = "Reflection report blocks downstream consumption; system check keeps the artifact from clean review."
        checks.append(_make_check("reflection_gate", "failed", message, [{"type": "reflection_report", "report_id": reflection.get("report_id")}]))
        candidate_conflicts.append(
            build_conflict(
                project_id=project_id,
                version_id=version_id,
                artifact_id=artifact_id,
                conflict_type="artifact_vs_context",
                semantic="missing_context",
                severity="blocking",
                status="open",
                summary=message,
                evidence_refs=[{"type": "reflection_report", "report_id": reflection.get("report_id")}],
                suggested_actions=["先完成 Reflection Report 中的 required actions。"],
            )
        )

    blocking_count = sum(1 for item in candidate_conflicts if item["severity"] == "blocking")
    warning_count = sum(1 for item in candidate_conflicts if item["severity"] == "warning")
    if blocking_count:
        status = "failed"
    elif warning_count or any(check["status"] == "warning" for check in checks):
        status = "warning"
    else:
        status = "passed"

    report = metadata_db.create_system_consistency_report(
        report_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        project_id=project_id,
        version_id=version_id,
        status=status,
        checks=checks,
        conflict_ids=[],
        suggested_actions=_suggest_actions(status, checks),
    )
    conflict_ids: List[str] = []
    for conflict in candidate_conflicts:
        created = metadata_db.create_context_conflict(
            conflict_id=str(uuid.uuid4()),
            report_id=report["report_id"],
            **conflict,
        )
        conflict_ids.append(created["conflict_id"])

    if conflict_ids:
        report = metadata_db.update_system_consistency_report(report["report_id"], conflict_ids=conflict_ids) or report

    content_integrity_failed = any(
        check.get("check_id") == "artifact_content_integrity" and check.get("status") == "failed"
        for check in checks
    )
    update_payload: Dict[str, Any] = {"consistency_report_id": report["report_id"]}
    if status == "failed" and content_integrity_failed:
        update_payload["status"] = "system_check_failed"
    metadata_db.update_design_artifact(artifact_id, **update_payload)
    return get_consistency_report_for_artifact(artifact_id) or report


def _suggest_actions(status: str, checks: List[Dict[str, Any]]) -> List[str]:
    if status == "failed":
        return ["处理 blocking context conflict 后再进入下游消费。"]
    if status == "warning":
        return ["允许进入审阅，但需要向用户展示 warning 与证据。"]
    if all(check["status"] == "unknown" for check in checks):
        return ["补充代码仓、数据库或知识库证据快照以提升系统校验置信度。"]
    return []


def get_consistency_report_for_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    report = metadata_db.get_system_consistency_report_for_artifact(artifact_id)
    if not report:
        return None
    return {
        **report,
        "conflicts": metadata_db.list_context_conflicts(report_id=report["report_id"]),
    }


def list_context_conflicts(project_id: str, version_id: str, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    return metadata_db.list_context_conflicts(project_id=project_id, version_id=version_id, status=status)
