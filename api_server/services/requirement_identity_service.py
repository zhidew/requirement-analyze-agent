import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.db_service import metadata_db
from services.version_path_resolver import resolve_version_path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_REQUIREMENT_TYPE = "RR" if BASE_DIR.name == "requirement-analyze-agent" else "IR"
VALID_REQUIREMENT_TYPES = {"RR", "IR", "US"}
MAX_REQUIREMENT_ID_LENGTH = 128
INVALID_ID_PATTERN = re.compile(r"[\\/\x00-\x1f\x7f]|(^|[.])\.\.?($|[.])")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize_string_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if item:
            normalized.append(item)
    return normalized


def normalize_requirement_type(requirement_type: Optional[str]) -> str:
    normalized = str(requirement_type or DEFAULT_REQUIREMENT_TYPE).strip().upper()
    if normalized not in VALID_REQUIREMENT_TYPES:
        raise ValueError("requirement_type must be RR, IR, or US.")
    return normalized


def normalize_requirement_id(requirement_id: Optional[str]) -> Optional[str]:
    normalized = str(requirement_id or "").strip()
    if not normalized:
        return None
    if len(normalized) > MAX_REQUIREMENT_ID_LENGTH:
        raise ValueError(f"requirement_id must be at most {MAX_REQUIREMENT_ID_LENGTH} characters.")
    if INVALID_ID_PATTERN.search(normalized) or ".." in normalized:
        raise ValueError("requirement_id must not contain path separators, control characters, or '..'.")
    return normalized


def _version_dir(project_id: str, version_id: str) -> Path:
    return resolve_version_path(project_id, version_id)


def _planned_version_relative_path(project_id: str, version_id: str, requirement_id: Optional[str]) -> str:
    if requirement_id:
        return f"projects/{project_id}/{requirement_id}/{version_id}"
    return f"projects/{project_id}/temp/{version_id}"


def _manifest_payload(version_record: Dict[str, Any], *, status: Optional[str] = None) -> Dict[str, Any]:
    now = _utcnow()
    created_at = version_record.get("created_at") or now
    return {
        "schema_version": 1,
        "project_id": version_record.get("project_id"),
        "agent_role": "requirement_analyze" if DEFAULT_REQUIREMENT_TYPE == "RR" else "it_design",
        "requirement_type": version_record.get("requirement_type"),
        "requirement_id": version_record.get("requirement_id"),
        "requirement_id_source": version_record.get("requirement_id_source"),
        "version_id": version_record.get("version_id"),
        "pipeline_sequence": version_record.get("pipeline_sequence"),
        "status": status or version_record.get("run_status"),
        "source_requirement_ids": version_record.get("source_requirement_ids") or [],
        "derived_requirement_ids": version_record.get("derived_requirement_ids") or [],
        "temp_archived": bool(version_record.get("temp_archived")),
        "created_at": created_at,
        "updated_at": now,
    }


def write_requirement_manifest(project_id: str, version_id: str, *, status: Optional[str] = None) -> Dict[str, Any]:
    version_record = metadata_db.get_version(project_id, version_id) or {}
    if not version_record:
        return {}
    project_root = _version_dir(project_id, version_id)
    baseline_dir = project_root / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    payload = _manifest_payload(version_record, status=status)
    manifest_path = project_root / "manifest.json"
    requirement_item_path = baseline_dir / "requirement-item.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    requirement_item_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def prepare_version(
    project_id: str,
    version_id: str,
    *,
    requirement_text: str = "",
    requirement_type: Optional[str] = None,
    requirement_id: Optional[str] = None,
    source_requirement_ids: Optional[List[str]] = None,
    title: Optional[str] = None,
    run_status: str = "prepared",
) -> Dict[str, Any]:
    normalized_type = normalize_requirement_type(requirement_type)
    normalized_id = normalize_requirement_id(requirement_id)
    source_ids = _normalize_string_list(source_requirement_ids)
    project_relative_path = _planned_version_relative_path(project_id, version_id, normalized_id)
    project_root = BASE_DIR / project_relative_path
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "baseline").mkdir(parents=True, exist_ok=True)
    (project_root / "logs").mkdir(parents=True, exist_ok=True)
    manifest_path = f"{project_relative_path}/manifest.json"

    temp_archived = normalized_id is None
    version_record = metadata_db.prepare_requirement_version(
        project_id,
        version_id,
        requirement=str(requirement_text or "").strip(),
        item_type=normalized_type,
        item_id=normalized_id,
        requirement_id_source="temp_missing_id" if temp_archived else "user_provided",
        source_requirement_ids=source_ids,
        title=title,
        manifest_path=manifest_path,
        run_status=run_status,
        temp_archived=temp_archived,
        archive_reason="missing_requirement_id",
        temp_path=project_relative_path if temp_archived else None,
    )
    manifest = write_requirement_manifest(project_id, version_id, status=run_status)
    return {
        "project_id": project_id,
        "version_id": version_id,
        "requirement_type": version_record.get("requirement_type"),
        "requirement_id": version_record.get("requirement_id"),
        "requirement_id_source": version_record.get("requirement_id_source"),
        "pipeline_sequence": version_record.get("pipeline_sequence"),
        "manifest_path": manifest_path,
        "temp_archived": bool(version_record.get("temp_archived")),
        "source_requirement_ids": version_record.get("source_requirement_ids") or [],
        "warnings": [],
        "manifest": manifest,
    }


def ensure_prepared_for_run(
    project_id: str,
    version_id: str,
    *,
    requirement_text: str = "",
    requirement_type: Optional[str] = None,
    requirement_id: Optional[str] = None,
    source_requirement_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    existing = metadata_db.get_version(project_id, version_id)
    if existing and (
        existing.get("requirement_id_source")
        or existing.get("requirement_type")
        or existing.get("temp_archived")
    ):
        if requirement_text:
            metadata_db.upsert_version(project_id, version_id, requirement_text, existing.get("run_status") or "prepared")
        return {
            "project_id": project_id,
            "version_id": version_id,
            "requirement_type": existing.get("requirement_type"),
            "requirement_id": existing.get("requirement_id"),
            "requirement_id_source": existing.get("requirement_id_source"),
            "pipeline_sequence": existing.get("pipeline_sequence"),
            "manifest_path": existing.get("manifest_path"),
            "temp_archived": bool(existing.get("temp_archived")),
            "source_requirement_ids": existing.get("source_requirement_ids") or [],
            "warnings": [],
        }
    return prepare_version(
        project_id,
        version_id,
        requirement_text=requirement_text,
        requirement_type=requirement_type,
        requirement_id=requirement_id,
        source_requirement_ids=source_requirement_ids,
    )


def bind_temp_version(
    project_id: str,
    version_id: str,
    *,
    requirement_type: str,
    requirement_id: str,
    title: Optional[str] = None,
    source_requirement_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    existing = metadata_db.get_temp_version(project_id, version_id)
    if not existing:
        raise ValueError("Only temp archived versions can be bound.")
    result = prepare_version(
        project_id,
        version_id,
        requirement_text=existing.get("requirement") or "",
        requirement_type=requirement_type,
        requirement_id=requirement_id,
        source_requirement_ids=source_requirement_ids,
        title=title,
        run_status=existing.get("run_status") or "unknown",
    )
    metadata_db.mark_temp_version_restored(project_id, version_id)
    version_record = metadata_db.get_version(project_id, version_id) or {}
    write_requirement_manifest(project_id, version_id, status=version_record.get("run_status"))
    return {
        "project_id": project_id,
        "version_id": version_id,
        "requirement_type": version_record.get("requirement_type"),
        "requirement_id": version_record.get("requirement_id"),
        "pipeline_sequence": version_record.get("pipeline_sequence"),
        "temp_archived": bool(version_record.get("temp_archived")),
        "manifest_path": result.get("manifest_path"),
    }


def version_response(version_record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "project_id": version_record.get("project_id"),
        "requirement_type": version_record.get("requirement_type"),
        "requirement_id": version_record.get("requirement_id"),
        "requirement_id_source": version_record.get("requirement_id_source"),
        "version_id": version_record.get("version_id"),
        "pipeline_sequence": version_record.get("pipeline_sequence"),
        "run_status": version_record.get("run_status") or "unknown",
        "manifest_path": version_record.get("manifest_path"),
        "source_requirement_ids": version_record.get("source_requirement_ids") or [],
        "derived_requirement_ids": version_record.get("derived_requirement_ids") or [],
        "decomposition_status": version_record.get("decomposition_status"),
        "temp_archived": bool(version_record.get("temp_archived")),
        "created_at": version_record.get("created_at"),
        "updated_at": version_record.get("updated_at"),
    }
