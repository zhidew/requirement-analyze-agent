from pathlib import Path

from services.db_service import metadata_db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"


def version_relative_path(project_id: str, version_id: str, version_record: dict | None = None) -> str:
    version = version_record if isinstance(version_record, dict) else (metadata_db.get_version(project_id, version_id) or {})
    if version.get("temp_archived"):
        temp_version = metadata_db.get_temp_version(project_id, version_id) or {}
        temp_path = str(temp_version.get("temp_path") or "").strip()
        if temp_path:
            return temp_path
        return f"projects/{project_id}/temp/{version_id}"

    requirement_id = str(version.get("requirement_id") or "").strip()
    if requirement_id:
        return f"projects/{project_id}/{requirement_id}/{version_id}"

    return f"projects/{project_id}/{version_id}"


def resolve_version_path(project_id: str, version_id: str) -> Path:
    version = metadata_db.get_version(project_id, version_id) or {}
    if version.get("temp_archived"):
        temp_version = metadata_db.get_temp_version(project_id, version_id) or {}
        temp_path = str(temp_version.get("temp_path") or "").strip()
        if temp_path:
            return BASE_DIR / temp_path
        direct_temp_path = PROJECTS_DIR / project_id / "temp" / version_id
        if direct_temp_path.exists():
            return direct_temp_path
        legacy_path = PROJECTS_DIR / project_id / version_id
        if legacy_path.exists():
            return legacy_path
        legacy_snapshot_path = PROJECTS_DIR / project_id / "temp" / "version-snapshots" / version_id
        if legacy_snapshot_path.exists():
            return legacy_snapshot_path
        return direct_temp_path

    requirement_id = str(version.get("requirement_id") or "").strip()
    if requirement_id:
        version_path = PROJECTS_DIR / project_id / requirement_id / version_id
        if version_path.exists():
            return version_path
        legacy_path = PROJECTS_DIR / project_id / version_id
        if legacy_path.exists():
            return legacy_path
        return version_path

    return PROJECTS_DIR / project_id / version_id
