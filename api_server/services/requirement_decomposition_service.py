import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from services import requirement_identity_service
from services.db_service import metadata_db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"

IS_REQUIREMENT_ANALYZE = BASE_DIR.name == "requirement-analyze-agent"
DECOMPOSITION_FILE = "ir-items.json" if IS_REQUIREMENT_ANALYZE else "us-items.json"
SOURCE_ITEM_TYPE = "RR" if IS_REQUIREMENT_ANALYZE else "IR"
TARGET_ITEM_TYPE = "IR" if IS_REQUIREMENT_ANALYZE else "US"
TARGET_ID_FIELDS = ("ir_id", "id", "item_id", "requirement_id") if IS_REQUIREMENT_ANALYZE else ("us_id", "id", "item_id", "requirement_id")


def _artifact_path(project_id: str, version_id: str) -> Path:
    return PROJECTS_DIR / project_id / version_id / "artifacts" / DECOMPOSITION_FILE


def _normalize_status(value: Any, fallback: str = "valid") -> str:
    status = str(value or fallback).strip().lower()
    if status in {"valid", "partial", "invalid", "pending"}:
        return status
    if status in {"success", "ok"}:
        return "valid"
    return fallback


def _normalize_string_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if item:
            normalized.append(item)
    return normalized


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("items") or payload.get("requirements") or payload.get("ir_items") or payload.get("us_items") or []
    else:
        raw_items = []
    return [dict(item) for item in raw_items if isinstance(item, dict)]


def _item_id(item: Dict[str, Any]) -> Optional[str]:
    for field in TARGET_ID_FIELDS:
        value = str(item.get(field) or "").strip()
        if value:
            try:
                return requirement_identity_service.normalize_requirement_id(value)
            except ValueError:
                return None
    return None


def _item_title(item: Dict[str, Any], item_id: str) -> str:
    return str(item.get("title") or item.get("name") or item.get("summary") or item_id).strip()


def _item_description(item: Dict[str, Any]) -> str:
    description = item.get("description") or item.get("requirement_text") or item.get("content") or item.get("summary")
    if isinstance(description, (dict, list)):
        return json.dumps(description, ensure_ascii=False)
    return str(description or "").strip()


def process_decomposition_artifact(project_id: str, version_id: str) -> Dict[str, Any]:
    version = metadata_db.get_version(project_id, version_id) or {}
    if not version:
        return {"status": "not_applicable", "derived_requirement_ids": [], "error": "Version not found."}

    path = _artifact_path(project_id, version_id)
    if not path.exists():
        metadata_db.upsert_version(
            project_id,
            version_id,
            version.get("requirement") or "",
            version.get("run_status") or "unknown",
            decomposition_status="pending",
            decomposition_error=f"{DECOMPOSITION_FILE} not found.",
        )
        requirement_identity_service.write_requirement_manifest(project_id, version_id, status=version.get("run_status"))
        return {"status": "pending", "derived_requirement_ids": [], "error": f"{DECOMPOSITION_FILE} not found."}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        error = f"Failed to parse {DECOMPOSITION_FILE}: {exc}"
        metadata_db.upsert_version(
            project_id,
            version_id,
            version.get("requirement") or "",
            version.get("run_status") or "unknown",
            decomposition_status="invalid",
            decomposition_error=error,
        )
        requirement_identity_service.write_requirement_manifest(project_id, version_id, status=version.get("run_status"))
        return {"status": "invalid", "derived_requirement_ids": [], "error": error}

    payload_status = _normalize_status(payload.get("status") if isinstance(payload, dict) else None)
    items = _extract_items(payload)
    derived_ids: List[str] = []
    invalid_count = 0
    source_item_id = str(version.get("requirement_id") or "").strip()
    source_ids_from_version = _normalize_string_list(version.get("source_requirement_ids"))

    for item in items:
        target_id = _item_id(item)
        if not target_id:
            invalid_count += 1
            continue
        derived_ids.append(target_id)
        source_item_ids = _normalize_string_list(item.get("source_requirement_ids")) or ([source_item_id] if source_item_id else source_ids_from_version)
        metadata_db.upsert_requirement_item(
            project_id,
            TARGET_ITEM_TYPE,
            target_id,
            title=_item_title(item, target_id),
            description=_item_description(item),
            id_source="generated_by_pipeline",
            status="active",
            source_item_ids=source_item_ids,
        )
        if source_item_id:
            confidence = item.get("confidence")
            try:
                confidence_value = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence_value = None
            metadata_db.upsert_requirement_trace_edge(
                project_id,
                source_item_type=SOURCE_ITEM_TYPE,
                source_item_id=source_item_id,
                target_item_type=TARGET_ITEM_TYPE,
                target_item_id=target_id,
                edge_type="decomposes_to",
                producing_version_id=version_id,
                confidence=confidence_value,
                evidence={
                    "artifact_ref": f"artifacts/{DECOMPOSITION_FILE}",
                    "item": item,
                },
            )

    if not items or not derived_ids:
        status = "invalid"
        error = f"{DECOMPOSITION_FILE} did not contain any valid {TARGET_ITEM_TYPE} items."
    elif invalid_count > 0 or payload_status == "partial":
        status = "partial"
        error = f"{invalid_count} item(s) could not be persisted." if invalid_count else None
    elif payload_status == "invalid":
        status = "invalid"
        error = f"{DECOMPOSITION_FILE} declared invalid status."
    else:
        status = "valid"
        error = None

    metadata_db.upsert_version(
        project_id,
        version_id,
        version.get("requirement") or "",
        version.get("run_status") or "unknown",
        derived_requirement_ids=derived_ids,
        decomposition_status=status,
        decomposition_error=error,
    )
    requirement_identity_service.write_requirement_manifest(project_id, version_id, status=version.get("run_status"))
    return {"status": status, "derived_requirement_ids": derived_ids, "error": error}
