from pathlib import Path

from services.db_service import metadata_db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECTS_DIR = BASE_DIR / "projects"


def resolve_version_path(project_id: str, version_id: str) -> Path:
    version = metadata_db.get_version(project_id, version_id) or {}
    if version.get("temp_archived"):
        temp_version = metadata_db.get_temp_version(project_id, version_id) or {}
        temp_path = str(temp_version.get("temp_path") or "").strip()
        if temp_path:
            return BASE_DIR / temp_path
    return PROJECTS_DIR / project_id / version_id
