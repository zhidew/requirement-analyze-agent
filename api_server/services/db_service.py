import base64
import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_DIR = BASE_DIR / "projects" / ".orchestrator"
DB_PATH = DB_DIR / "metadata.sqlite"
KEY_PATH = DB_DIR / "metadata.key"
ENV_PATH = BASE_DIR / ".env"
LEGACY_EXPERT_ID_MIGRATIONS = {
    "architecture-mapping": "requirement-clarification",
    "design-assembler": "ir-assembler",
}
JSON_UNSET = object()

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception


class SensitiveValueCodec:
    """Encrypt sensitive values when cryptography is available."""

    def __init__(self, key_path: Path):
        self._key_path = key_path
        self._fernet = self._build_fernet()

    def _build_fernet(self):
        if Fernet is None:
            return None

        env_key = os.getenv("REQUIREMENT_ANALYZE_AGENT_METADATA_KEY") or os.getenv("IT_DESIGN_AGENT_METADATA_KEY")
        if env_key:
            return Fernet(env_key.encode("utf-8"))

        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
        return Fernet(key)

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if self._fernet is None:
            encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
            return f"plain:{encoded}"
        token = self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        return f"fernet:{token}"

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value.startswith("fernet:"):
            token = value[len("fernet:") :]
            if self._fernet is None:
                raise RuntimeError("Encrypted value found but cryptography is not installed.")
            try:
                return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
            except InvalidToken as exc:
                raise RuntimeError("Failed to decrypt stored secret.") from exc
        if value.startswith("plain:"):
            encoded = value[len("plain:") :]
            return base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        return value

    @staticmethod
    def mask(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


class MetadataDB:
    def __init__(self, db_path: Optional[Path] = None, env_path: Optional[Path] = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_dir = self.db_path.parent
        self.env_path = Path(env_path or ENV_PATH)
        key_path = KEY_PATH if db_path is None else self.db_dir / "metadata.key"
        self.codec = SensitiveValueCodec(key_path)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.synced = False

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _utcnow() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _dumps_json(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _loads_json(value: Optional[str], default: Any):
        if value in (None, ""):
            return default
        return json.loads(value)

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    requirement TEXT,
                    run_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (version_id, project_id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'git',
                    url TEXT NOT NULL,
                    branch TEXT NOT NULL DEFAULT 'main',
                    username TEXT,
                    token TEXT,
                    local_path TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS databases (
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    database_name TEXT NOT NULL,
                    username TEXT,
                    password TEXT,
                    schema_filter TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    path TEXT,
                    index_url TEXT,
                    includes TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_experts (
                    expert_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, expert_id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_llm_configs (
                    project_id TEXT PRIMARY KEY,
                    llm_provider TEXT,
                    openai_api_key TEXT,
                    openai_base_url TEXT,
                    openai_model_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_debug_configs (
                    project_id TEXT PRIMARY KEY,
                    llm_interaction_logging_enabled INTEGER NOT NULL DEFAULT 0,
                    llm_full_payload_logging_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_model_configs (
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    api_key TEXT,
                    base_url TEXT,
                    headers TEXT,
                    model_name TEXT NOT NULL,
                    is_default INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    run_id TEXT,
                    status TEXT NOT NULL,
                    current_phase TEXT,
                    current_node TEXT,
                    waiting_reason TEXT,
                    pending_interrupt_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, version_id),
                    FOREIGN KEY (version_id, project_id) REFERENCES versions(version_id, project_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    task_id TEXT,
                    run_id TEXT,
                    phase TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER,
                    dependencies_json TEXT,
                    metadata_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, version_id, node_type),
                    FOREIGN KEY (project_id, version_id) REFERENCES workflow_runs(project_id, version_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_task_events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    run_id TEXT,
                    task_id TEXT,
                    node_type TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS human_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    run_id TEXT,
                    scope TEXT NOT NULL,
                    owner_node TEXT NOT NULL,
                    owner_expert_id TEXT,
                    status TEXT NOT NULL,
                    turn_index INTEGER NOT NULL DEFAULT 0,
                    parent_interaction_id TEXT,
                    question_text TEXT NOT NULL,
                    question_schema_json TEXT,
                    context_json TEXT,
                    answer_json TEXT,
                    summary TEXT,
                    knowledge_refs_json TEXT,
                    affected_artifacts_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (project_id, version_id) REFERENCES workflow_runs(project_id, version_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS human_interaction_events (
                    event_id TEXT PRIMARY KEY,
                    interaction_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (interaction_id) REFERENCES human_interactions(interaction_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS design_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    run_id TEXT,
                    expert_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    artifact_version INTEGER NOT NULL DEFAULT 1,
                    parent_artifact_id TEXT,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    summary TEXT,
                    source_refs_json TEXT,
                    dependency_refs_json TEXT,
                    decision_refs_json TEXT,
                    reflection_report_id TEXT,
                    consistency_report_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS design_artifact_events (
                    event_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS expert_reflection_reports (
                    report_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    expert_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    checks_json TEXT,
                    issues_json TEXT,
                    assumptions_json TEXT,
                    open_questions_json TEXT,
                    required_actions_json TEXT,
                    blocks_downstream INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_consistency_reports (
                    report_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checks_json TEXT,
                    conflict_ids_json TEXT,
                    suggested_actions_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_conflicts (
                    conflict_id TEXT PRIMARY KEY,
                    report_id TEXT,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    artifact_id TEXT,
                    conflict_type TEXT NOT NULL,
                    semantic TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_refs_json TEXT,
                    suggested_actions_json TEXT,
                    decision_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (report_id) REFERENCES system_consistency_reports(report_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_logs (
                    decision_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    conflict_ids_json TEXT,
                    decision TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    applies_to_json TEXT,
                    evidence_refs_json TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_sessions (
                    revision_session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    target_artifact_id TEXT NOT NULL,
                    target_expert_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    user_feedback TEXT,
                    normalized_revision_request_json TEXT,
                    conflict_report_id TEXT,
                    decision_id TEXT,
                    affected_artifacts_json TEXT,
                    created_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (target_artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_session_events (
                    event_id TEXT PRIMARY KEY,
                    revision_session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (revision_session_id) REFERENCES revision_sessions(revision_session_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_anchors (
                    anchor_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    anchor_type TEXT NOT NULL,
                    label TEXT,
                    text_excerpt TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    structural_path_json TEXT,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_dependency_edges (
                    edge_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    upstream_artifact_id TEXT NOT NULL,
                    downstream_artifact_id TEXT NOT NULL,
                    dependency_type TEXT NOT NULL,
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (upstream_artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE,
                    FOREIGN KEY (downstream_artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_impact_records (
                    impact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    source_artifact_id TEXT NOT NULL,
                    impacted_artifact_id TEXT NOT NULL,
                    impact_status TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_ref_id TEXT,
                    reason TEXT NOT NULL,
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (source_artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE,
                    FOREIGN KEY (impacted_artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_patches (
                    patch_id TEXT PRIMARY KEY,
                    revision_session_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    anchor_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    preserve_policy TEXT NOT NULL,
                    patch_status TEXT NOT NULL,
                    source_content_hash TEXT NOT NULL,
                    allowed_range_json TEXT,
                    diff_json TEXT,
                    rationale TEXT,
                    predicted_impact_json TEXT,
                    created_artifact_id TEXT,
                    apply_result_json TEXT,
                    post_apply_content_hash TEXT,
                    post_apply_validation_json TEXT,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    FOREIGN KEY (revision_session_id) REFERENCES revision_sessions(revision_session_id) ON DELETE CASCADE,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE,
                    FOREIGN KEY (anchor_id) REFERENCES artifact_anchors(anchor_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_section_reviews (
                    section_review_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    anchor_id TEXT,
                    status TEXT NOT NULL,
                    reviewer_note TEXT,
                    revision_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (artifact_id) REFERENCES design_artifacts(artifact_id) ON DELETE CASCADE,
                    FOREIGN KEY (anchor_id) REFERENCES artifact_anchors(anchor_id) ON DELETE SET NULL,
                    FOREIGN KEY (revision_session_id) REFERENCES revision_sessions(revision_session_id) ON DELETE SET NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    schedule_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    requirement TEXT,
                    model TEXT,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    triggered_job_id TEXT,
                    error TEXT,
                    triggered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (version_id, project_id) REFERENCES versions(version_id, project_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_updated ON workflow_runs(status, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_tasks_run_status ON workflow_tasks(project_id, version_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_task_events_run_created ON workflow_task_events(project_id, version_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_human_interactions_project_version ON human_interactions(project_id, version_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_human_interactions_status ON human_interactions(status, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_human_interaction_events_interaction_created ON human_interaction_events(interaction_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_runs_status_time ON scheduled_runs(status, scheduled_for)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_runs_project_version ON scheduled_runs(project_id, version_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_design_artifacts_project_version ON design_artifacts(project_id, version_id, expert_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_design_artifacts_file ON design_artifacts(project_id, version_id, file_path, artifact_version)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_consistency_reports_artifact ON system_consistency_reports(artifact_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_conflicts_project_version ON context_conflicts(project_id, version_id, status, severity)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_logs_project_version ON decision_logs(project_id, version_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_revision_sessions_target ON revision_sessions(target_artifact_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_dependency_edges_upstream ON artifact_dependency_edges(project_id, version_id, upstream_artifact_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_impact_records_source ON artifact_impact_records(project_id, version_id, source_artifact_id, impact_status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_revision_patches_session ON revision_patches(revision_session_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifact_section_reviews_artifact ON artifact_section_reviews(artifact_id, status, updated_at)"
            )
            self._ensure_column(conn, "project_model_configs", "headers", "TEXT")
            self._ensure_column(conn, "workflow_runs", "pending_interrupt_json", "TEXT")
            self._migrate_legacy_project_experts(conn)
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str):
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def _migrate_legacy_project_experts(self, conn: sqlite3.Connection) -> None:
        now = self._utcnow()
        for legacy_id, canonical_id in LEGACY_EXPERT_ID_MIGRATIONS.items():
            conn.execute(
                """
                INSERT INTO project_experts (
                    expert_id, project_id, enabled, description, created_at, updated_at
                )
                SELECT ?, project_id, enabled, description, created_at, ?
                FROM project_experts
                WHERE expert_id = ?
                ON CONFLICT(project_id, expert_id) DO UPDATE SET
                    enabled = CASE
                        WHEN project_experts.enabled = 1 OR excluded.enabled = 1 THEN 1
                        ELSE 0
                    END,
                    description = COALESCE(project_experts.description, excluded.description),
                    updated_at = excluded.updated_at
                """,
                (canonical_id, now, legacy_id),
            )
            conn.execute(
                "DELETE FROM project_experts WHERE expert_id = ?",
                (legacy_id,),
            )

    def _load_env_lines(self) -> List[str]:
        if not self.env_path.exists():
            return []
        return self.env_path.read_text(encoding="utf-8").splitlines()

    def _parse_env(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for line in self._load_env_lines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def get_system_llm_defaults(self, include_secrets: bool = False) -> Dict[str, Any]:
        env = self._parse_env()
        openai_api_key = env.get("OPENAI_API_KEY") or None
        llm_provider = (env.get("LLM_PROVIDER") or "openai").strip().lower() or "openai"
        if llm_provider != "openai":
            llm_provider = "openai"
        result: Dict[str, Any] = {
            "llm_provider": llm_provider,
            "openai_base_url": env.get("OPENAI_BASE_URL", ""),
            "openai_model_name": env.get("OPENAI_MODEL_NAME", ""),
            "has_openai_api_key": bool(openai_api_key),
        }
        if include_secrets:
            result["openai_api_key"] = openai_api_key
        else:
            result["openai_api_key"] = self.codec.mask(openai_api_key)
        return result

    def upsert_project_model(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        model_id = payload.get("id")
        existing = self.get_project_model(project_id, model_id, include_secrets=True) if model_id else None

        api_key = payload.get("api_key")
        headers = payload.get("headers")
        encrypted_api_key = (
            self.codec.encrypt(api_key)
            if api_key not in (None, "", "******")
            else (existing.get("_api_key_encrypted") if existing else None)
        )
        encrypted_headers = (
            self.codec.encrypt(self._dumps_json(headers))
            if headers not in (None, "", {})
            else (existing.get("_headers_encrypted") if existing else None)
        )

        with self._get_connection() as conn:
            if payload.get("is_default"):
                # Reset other default models for this project
                conn.execute(
                    "UPDATE project_model_configs SET is_default = 0 WHERE project_id = ?",
                    (project_id,),
                )

            conn.execute(
                """
                INSERT INTO project_model_configs (
                    id, project_id, name, provider, api_key, base_url, headers, model_name, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, id) DO UPDATE SET
                    name=excluded.name,
                    provider=excluded.provider,
                    api_key=excluded.api_key,
                    base_url=excluded.base_url,
                    headers=excluded.headers,
                    model_name=excluded.model_name,
                    is_default=excluded.is_default,
                    updated_at=excluded.updated_at
                """,
                (
                    model_id,
                    project_id,
                    payload.get("name"),
                    "openai",
                    encrypted_api_key,
                    payload.get("base_url"),
                    encrypted_headers,
                    payload.get("model_name"),
                    1 if payload.get("is_default") else 0,
                    (existing.get("created_at") if existing else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_project_model(project_id, model_id, include_secrets=False)

    def list_project_models(self, project_id: str, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_model_configs WHERE project_id = ? ORDER BY is_default DESC, updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._row_to_model_config(dict(row), include_secrets=include_secrets) for row in rows]

    def delete_project_model(self, project_id: str, model_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM project_model_configs WHERE project_id = ? AND id = ?",
                (project_id, model_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_project_model(self, project_id: str, model_id: str, include_secrets: bool = False) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM project_model_configs WHERE project_id = ? AND id = ?",
                (project_id, model_id),
            ).fetchone()
        return self._row_to_model_config(dict(row), include_secrets=include_secrets) if row else None

    def _row_to_model_config(self, row: Dict[str, Any], include_secrets: bool) -> Dict[str, Any]:
        encrypted_api_key = row.pop("api_key", None)
        encrypted_headers = row.pop("headers", None)
        api_key = self.codec.decrypt(encrypted_api_key) if encrypted_api_key else None
        headers_raw = self.codec.decrypt(encrypted_headers) if encrypted_headers else None
        headers = self._loads_json(headers_raw, None)

        result: Dict[str, Any] = {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "provider": "openai",
            "base_url": row["base_url"],
            "model_name": row["model_name"],
            "is_default": bool(row["is_default"]),
            "has_api_key": bool(encrypted_api_key),
            "has_headers": bool(encrypted_headers),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secrets:
            result["api_key"] = api_key
            result["headers"] = headers
            result["_api_key_encrypted"] = encrypted_api_key
            result["_headers_encrypted"] = encrypted_headers
        else:
            result["api_key"] = self.codec.mask(api_key)
            result["headers"] = None
        return result

    def upsert_project(self, project_id: str, name: str, description: Optional[str] = None):
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (project_id, name, description, now, now),
            )
            conn.commit()

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["pending_interrupt"] = self._loads_json(data.pop("pending_interrupt_json", None), None)
        return data

    def list_projects(self, runtime_states: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        
        projects = []
        for row in rows:
            proj = dict(row)
            # Get project status info
            project_id = proj['id']
            
            # Count versions with all status breakdown
            with self._get_connection() as conn:
                version_rows = conn.execute(
                    "SELECT version_id, run_status FROM versions WHERE project_id = ?",
                    (project_id,)
                ).fetchall()
            
            # Build version status map, overlay with runtime states
            version_statuses = {v['version_id']: v['run_status'] for v in version_rows}
            if runtime_states:
                for version_id, rt_state in runtime_states.items():
                    if rt_state.get('project_id') == project_id:
                        rt_status = rt_state.get('run_status')
                        if rt_status:
                            version_statuses[version_id] = rt_status
            
            # Count all statuses
            status_counts = {
                'running': 0,
                'success': 0,
                'failed': 0,
                'waiting_human': 0,
                'queued': 0,
                'unknown': 0,
            }
            for status in version_statuses.values():
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    status_counts['unknown'] += 1
            
            total_versions = len(version_statuses)
            
            # Determine status
            has_versions = total_versions > 0
            is_active = status_counts['running'] > 0 or status_counts['waiting_human'] > 0
            
            proj['total_versions'] = total_versions
            proj['enabled_experts_count'] = len(self.list_enabled_expert_ids(project_id))
            proj['running_versions'] = status_counts['running']
            proj['success_versions'] = status_counts['success']
            proj['failed_versions'] = status_counts['failed']
            proj['waiting_versions'] = status_counts['waiting_human']
            proj['queued_versions'] = status_counts['queued']
            proj['unknown_versions'] = status_counts['unknown']
            proj['status_counts'] = status_counts
            proj['has_versions'] = has_versions
            proj['is_active'] = is_active
            proj['status'] = 'active' if is_active else ('ready' if has_versions else 'empty')
            
            projects.append(proj)
        
        return projects

    def delete_project(self, project_id: str):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()

    def upsert_version(self, project_id: str, version_id: str, requirement: str, run_status: str):
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO versions (version_id, project_id, requirement, run_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id, project_id) DO UPDATE SET
                    run_status=excluded.run_status,
                    updated_at=excluded.updated_at
                """,
                (version_id, project_id, requirement, run_status, now, now),
            )
            conn.commit()

    def upsert_workflow_run(
        self,
        project_id: str,
        version_id: str,
        *,
        run_id: Optional[str],
        status: str,
        current_phase: Optional[str] = None,
        current_node: Optional[str] = None,
        waiting_reason: Optional[str] = None,
        pending_interrupt: Any = JSON_UNSET,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_workflow_run(project_id, version_id)
        if pending_interrupt is JSON_UNSET:
            effective_pending_interrupt = existing.get("pending_interrupt") if existing else None
        else:
            effective_pending_interrupt = pending_interrupt
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    project_id, version_id, run_id, status, current_phase, current_node, waiting_reason, pending_interrupt_json,
                    started_at, finished_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, version_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    status=excluded.status,
                    current_phase=COALESCE(excluded.current_phase, workflow_runs.current_phase),
                    current_node=excluded.current_node,
                    waiting_reason=excluded.waiting_reason,
                    pending_interrupt_json=excluded.pending_interrupt_json,
                    started_at=COALESCE(workflow_runs.started_at, excluded.started_at),
                    finished_at=excluded.finished_at,
                    updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    version_id,
                    run_id,
                    status,
                    current_phase,
                    current_node,
                    waiting_reason,
                    self._dumps_json(effective_pending_interrupt),
                    started_at or (existing.get("started_at") if existing else now),
                    finished_at,
                    (existing.get("created_at") if existing else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_workflow_run(project_id, version_id) or {}

    def get_workflow_run(self, project_id: str, version_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE project_id = ? AND version_id = ?",
                (project_id, version_id),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["pending_interrupt"] = self._loads_json(data.pop("pending_interrupt_json", None), None)
        return data

    def create_scheduled_run(
        self,
        *,
        schedule_id: str,
        project_id: str,
        version_id: str,
        requirement: str,
        scheduled_for: str,
        model: Optional[str] = None,
        status: str = "scheduled",
        error: Optional[str] = None,
        triggered_job_id: Optional[str] = None,
        triggered_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_runs (
                    schedule_id, project_id, version_id, requirement, model,
                    scheduled_for, status, triggered_job_id, error, triggered_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    project_id,
                    version_id,
                    requirement,
                    model,
                    scheduled_for,
                    status,
                    triggered_job_id,
                    error,
                    triggered_at,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_scheduled_run(schedule_id) or {}

    def update_scheduled_run(
        self,
        schedule_id: str,
        *,
        status: Optional[str] = None,
        error: Optional[str] = None,
        triggered_job_id: Optional[str] = None,
        triggered_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_scheduled_run(schedule_id)
        if not existing:
            return None

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE scheduled_runs
                SET status = ?, error = ?, triggered_job_id = ?, triggered_at = ?, updated_at = ?
                WHERE schedule_id = ?
                """,
                (
                    status or existing["status"],
                    error,
                    triggered_job_id if triggered_job_id is not None else existing.get("triggered_job_id"),
                    triggered_at if triggered_at is not None else existing.get("triggered_at"),
                    self._utcnow(),
                    schedule_id,
                ),
            )
            conn.commit()
        return self.get_scheduled_run(schedule_id)

    def get_scheduled_run(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["pending_interrupt"] = self._loads_json(data.pop("pending_interrupt_json", None), None)
        return data

    def list_scheduled_runs_for_version(
        self,
        project_id: str,
        version_id: str,
        statuses: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM scheduled_runs
            WHERE project_id = ? AND version_id = ?
        """
        params: List[Any] = [project_id, version_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY scheduled_for DESC, created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_latest_scheduled_run_for_version(
        self,
        project_id: str,
        version_id: str,
        statuses: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = self.list_scheduled_runs_for_version(project_id, version_id, statuses=statuses)
        return rows[0] if rows else None

    def list_pending_scheduled_runs(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_runs
                WHERE status = 'scheduled'
                ORDER BY scheduled_for ASC, created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_scheduled_runs_for_version(
        self,
        project_id: str,
        version_id: str,
        *,
        statuses: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> int:
        query = """
            UPDATE scheduled_runs
            SET status = 'cancelled', error = ?, updated_at = ?
            WHERE project_id = ? AND version_id = ?
        """
        params: List[Any] = [error, self._utcnow(), project_id, version_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            conn.commit()
        return cursor.rowcount

    @staticmethod
    def _status_rank(status: Optional[str]) -> int:
        order = {
            "todo": 0,
            "scheduled": 1,
            "queued": 1,
            "dispatched": 2,
            "running": 3,
            "waiting_human": 4,
            "success": 5,
            "skipped": 5,
            "failed": 6,
            "cancelled": 6,
        }
        return order.get(str(status or "").lower(), -1)

    def _merge_task_status(self, current_status: Optional[str], incoming_status: Optional[str], *, authoritative: bool) -> str:
        if authoritative:
            return incoming_status or current_status or "todo"
        if current_status in {"success", "failed", "skipped", "cancelled"} and self._status_rank(incoming_status) < self._status_rank(current_status):
            return current_status
        if self._status_rank(incoming_status) >= self._status_rank(current_status):
            return incoming_status or current_status or "todo"
        return current_status or incoming_status or "todo"

    def upsert_workflow_task(
        self,
        project_id: str,
        version_id: str,
        *,
        node_type: str,
        task_id: Optional[str],
        run_id: Optional[str],
        status: str,
        phase: Optional[str] = None,
        priority: Optional[int] = None,
        dependencies: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        authoritative: bool = False,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_workflow_task(project_id, version_id, node_type)
        resolved_status = self._merge_task_status(existing.get("status") if existing else None, status, authoritative=authoritative)
        resolved_started_at = (
            started_at
            or (existing.get("started_at") if existing else None)
            or (now if resolved_status in {"dispatched", "running", "waiting_human", "success", "failed", "skipped", "cancelled"} else None)
        )
        resolved_finished_at = (
            finished_at
            or (existing.get("finished_at") if existing and resolved_status in {"success", "failed", "skipped", "cancelled"} else None)
            or (now if resolved_status in {"success", "failed", "skipped", "cancelled"} else None)
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_tasks (
                    project_id, version_id, node_type, task_id, run_id, phase, status, attempt, priority,
                    dependencies_json, metadata_json, started_at, finished_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, version_id, node_type) DO UPDATE SET
                    task_id=COALESCE(excluded.task_id, workflow_tasks.task_id),
                    run_id=COALESCE(excluded.run_id, workflow_tasks.run_id),
                    phase=COALESCE(excluded.phase, workflow_tasks.phase),
                    status=excluded.status,
                    attempt=workflow_tasks.attempt,
                    priority=COALESCE(excluded.priority, workflow_tasks.priority),
                    dependencies_json=COALESCE(excluded.dependencies_json, workflow_tasks.dependencies_json),
                    metadata_json=COALESCE(excluded.metadata_json, workflow_tasks.metadata_json),
                    started_at=COALESCE(workflow_tasks.started_at, excluded.started_at),
                    finished_at=COALESCE(excluded.finished_at, workflow_tasks.finished_at),
                    updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    version_id,
                    node_type,
                    task_id,
                    run_id,
                    phase,
                    resolved_status,
                    existing.get("attempt", 0) if existing else 0,
                    priority,
                    self._dumps_json(dependencies),
                    self._dumps_json(metadata),
                    resolved_started_at,
                    resolved_finished_at,
                    (existing.get("created_at") if existing else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_workflow_task(project_id, version_id, node_type) or {}

    def get_workflow_task(self, project_id: str, version_id: str, node_type: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_tasks
                WHERE project_id = ? AND version_id = ? AND node_type = ?
                """,
                (project_id, version_id, node_type),
            ).fetchone()
        return self._row_to_workflow_task(dict(row)) if row else None

    def list_workflow_tasks(self, project_id: str, version_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_tasks
                WHERE project_id = ? AND version_id = ?
                ORDER BY CASE WHEN node_type = 'planner' THEN 0 ELSE 1 END, COALESCE(priority, 0) DESC, node_type ASC
                """,
                (project_id, version_id),
            ).fetchall()
        return [self._row_to_workflow_task(dict(row)) for row in rows]

    def replace_workflow_tasks(
        self,
        project_id: str,
        version_id: str,
        *,
        run_id: Optional[str],
        tasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM workflow_tasks WHERE project_id = ? AND version_id = ?",
                (project_id, version_id),
            )
            for task in tasks:
                status = task.get("status", "todo")
                started_at = now if status in {"running", "waiting_human", "success", "failed", "skipped", "cancelled"} else None
                finished_at = now if status in {"success", "failed", "skipped", "cancelled"} else None
                conn.execute(
                    """
                    INSERT INTO workflow_tasks (
                        project_id, version_id, node_type, task_id, run_id, phase, status, attempt, priority,
                        dependencies_json, metadata_json, started_at, finished_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        version_id,
                        task.get("agent_type"),
                        task.get("id"),
                        run_id,
                        task.get("phase") or (task.get("metadata") or {}).get("workflow_phase"),
                        status,
                        int(task.get("attempt", 0) or 0),
                        task.get("priority"),
                        self._dumps_json(task.get("dependencies") or []),
                        self._dumps_json(task.get("metadata") or {}),
                        started_at,
                        finished_at,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return self.list_workflow_tasks(project_id, version_id)

    def append_workflow_task_event(
        self,
        *,
        event_id: str,
        project_id: str,
        version_id: str,
        run_id: Optional[str],
        task_id: Optional[str],
        node_type: str,
        event_type: str,
        status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_task_events (
                    event_id, project_id, version_id, run_id, task_id, node_type, event_type, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    project_id,
                    version_id,
                    run_id,
                    task_id,
                    node_type,
                    event_type,
                    status,
                    self._dumps_json(payload or {}),
                    created_at or self._utcnow(),
                ),
            )
            conn.commit()

    def create_human_interaction(
        self,
        *,
        interaction_id: str,
        project_id: str,
        version_id: str,
        run_id: Optional[str],
        scope: str,
        owner_node: str,
        owner_expert_id: Optional[str] = None,
        status: str = "created",
        turn_index: int = 0,
        parent_interaction_id: Optional[str] = None,
        question_text: str,
        question_schema: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        answer: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        knowledge_refs: Optional[List[str]] = None,
        affected_artifacts: Optional[List[str]] = None,
        completed_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO human_interactions (
                    interaction_id, project_id, version_id, run_id, scope, owner_node, owner_expert_id,
                    status, turn_index, parent_interaction_id, question_text, question_schema_json,
                    context_json, answer_json, summary, knowledge_refs_json, affected_artifacts_json,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    project_id,
                    version_id,
                    run_id,
                    scope,
                    owner_node,
                    owner_expert_id,
                    status,
                    turn_index,
                    parent_interaction_id,
                    question_text,
                    self._dumps_json(question_schema or {}),
                    self._dumps_json(context or {}),
                    self._dumps_json(answer or {}),
                    summary or "",
                    self._dumps_json(knowledge_refs or []),
                    self._dumps_json(affected_artifacts or []),
                    now,
                    now,
                    completed_at,
                ),
            )
            conn.commit()
        return self.get_human_interaction(interaction_id) or {}

    def update_human_interaction(
        self,
        interaction_id: str,
        *,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        question_text: Optional[str] = None,
        question_schema: Any = JSON_UNSET,
        context: Any = JSON_UNSET,
        answer: Any = JSON_UNSET,
        summary: Optional[str] = None,
        knowledge_refs: Any = JSON_UNSET,
        affected_artifacts: Any = JSON_UNSET,
        completed_at: Any = JSON_UNSET,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_human_interaction(interaction_id)
        if not existing:
            return None

        effective_question_schema = existing.get("question_schema") if question_schema is JSON_UNSET else (question_schema or {})
        effective_context = existing.get("context") if context is JSON_UNSET else (context or {})
        effective_answer = existing.get("answer") if answer is JSON_UNSET else (answer or {})
        effective_knowledge_refs = existing.get("knowledge_refs") if knowledge_refs is JSON_UNSET else (knowledge_refs or [])
        effective_affected_artifacts = existing.get("affected_artifacts") if affected_artifacts is JSON_UNSET else (affected_artifacts or [])
        effective_completed_at = existing.get("completed_at") if completed_at is JSON_UNSET else completed_at

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE human_interactions
                SET run_id = ?,
                    status = ?,
                    question_text = ?,
                    question_schema_json = ?,
                    context_json = ?,
                    answer_json = ?,
                    summary = ?,
                    knowledge_refs_json = ?,
                    affected_artifacts_json = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE interaction_id = ?
                """,
                (
                    run_id if run_id is not None else existing.get("run_id"),
                    status or existing.get("status"),
                    question_text or existing.get("question_text"),
                    self._dumps_json(effective_question_schema),
                    self._dumps_json(effective_context),
                    self._dumps_json(effective_answer),
                    summary if summary is not None else existing.get("summary", ""),
                    self._dumps_json(effective_knowledge_refs),
                    self._dumps_json(effective_affected_artifacts),
                    effective_completed_at,
                    self._utcnow(),
                    interaction_id,
                ),
            )
            conn.commit()
        return self.get_human_interaction(interaction_id)

    def get_human_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM human_interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
        return self._row_to_human_interaction(dict(row)) if row else None

    def get_latest_human_interaction_for_version(
        self,
        project_id: str,
        version_id: str,
        *,
        statuses: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        rows = self.list_human_interactions(project_id, version_id, statuses=statuses, limit=1)
        return rows[0] if rows else None

    def list_human_interactions(
        self,
        project_id: str,
        version_id: str,
        *,
        statuses: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM human_interactions
            WHERE project_id = ? AND version_id = ?
        """
        params: List[Any] = [project_id, version_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY updated_at DESC, created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_human_interaction(dict(row)) for row in rows]

    def append_human_interaction_event(
        self,
        *,
        event_id: str,
        interaction_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO human_interaction_events (
                    event_id, interaction_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    interaction_id,
                    event_type,
                    self._dumps_json(payload or {}),
                    created_at or self._utcnow(),
                ),
            )
            conn.commit()

    def list_human_interaction_events(self, interaction_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM human_interaction_events
                WHERE interaction_id = ?
                ORDER BY created_at ASC
                """,
                (interaction_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "interaction_id": row["interaction_id"],
                "event_type": row["event_type"],
                "payload": self._loads_json(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_design_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        version_id: str,
        run_id: Optional[str],
        expert_id: str,
        artifact_type: str,
        artifact_version: int,
        parent_artifact_id: Optional[str],
        status: str,
        title: str,
        file_name: str,
        file_path: str,
        content_hash: str,
        summary: Optional[str] = None,
        source_refs: Optional[List[Any]] = None,
        dependency_refs: Optional[List[Any]] = None,
        decision_refs: Optional[List[Any]] = None,
        reflection_report_id: Optional[str] = None,
        consistency_report_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO design_artifacts (
                    artifact_id, project_id, version_id, run_id, expert_id, artifact_type,
                    artifact_version, parent_artifact_id, status, title, file_name, file_path,
                    content_hash, summary, source_refs_json, dependency_refs_json, decision_refs_json,
                    reflection_report_id, consistency_report_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    version_id,
                    run_id,
                    expert_id,
                    artifact_type,
                    int(artifact_version or 1),
                    parent_artifact_id,
                    status,
                    title,
                    file_name,
                    file_path,
                    content_hash,
                    summary or "",
                    self._dumps_json(source_refs or []),
                    self._dumps_json(dependency_refs or []),
                    self._dumps_json(decision_refs or []),
                    reflection_report_id,
                    consistency_report_id,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_design_artifact(artifact_id) or {}

    def update_design_artifact(
        self,
        artifact_id: str,
        *,
        status: Optional[str] = None,
        content_hash: Optional[str] = None,
        summary: Optional[str] = None,
        source_refs: Any = JSON_UNSET,
        dependency_refs: Any = JSON_UNSET,
        reflection_report_id: Optional[str] = None,
        consistency_report_id: Optional[str] = None,
        decision_refs: Any = JSON_UNSET,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_design_artifact(artifact_id)
        if not existing:
            return None
        effective_source_refs = existing.get("source_refs") if source_refs is JSON_UNSET else (source_refs or [])
        effective_dependency_refs = existing.get("dependency_refs") if dependency_refs is JSON_UNSET else (dependency_refs or [])
        effective_decision_refs = existing.get("decision_refs") if decision_refs is JSON_UNSET else (decision_refs or [])
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE design_artifacts
                SET status = ?,
                    content_hash = ?,
                    summary = ?,
                    source_refs_json = ?,
                    dependency_refs_json = ?,
                    reflection_report_id = ?,
                    consistency_report_id = ?,
                    decision_refs_json = ?,
                    updated_at = ?
                WHERE artifact_id = ?
                """,
                (
                    status or existing.get("status"),
                    content_hash if content_hash is not None else existing.get("content_hash"),
                    summary if summary is not None else existing.get("summary"),
                    self._dumps_json(effective_source_refs),
                    self._dumps_json(effective_dependency_refs),
                    reflection_report_id if reflection_report_id is not None else existing.get("reflection_report_id"),
                    consistency_report_id if consistency_report_id is not None else existing.get("consistency_report_id"),
                    self._dumps_json(effective_decision_refs),
                    self._utcnow(),
                    artifact_id,
                ),
            )
            conn.commit()
        return self.get_design_artifact(artifact_id)

    def get_design_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM design_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return self._row_to_design_artifact(dict(row)) if row else None

    def get_latest_design_artifact_by_file(self, project_id: str, version_id: str, file_path: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM design_artifacts
                WHERE project_id = ? AND version_id = ? AND file_path = ?
                ORDER BY artifact_version DESC, updated_at DESC
                LIMIT 1
                """,
                (project_id, version_id, file_path),
            ).fetchone()
        return self._row_to_design_artifact(dict(row)) if row else None

    def list_design_artifacts(self, project_id: str, version_id: str, *, expert_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM design_artifacts
            WHERE project_id = ? AND version_id = ?
        """
        params: List[Any] = [project_id, version_id]
        if expert_id:
            query += " AND expert_id = ?"
            params.append(expert_id)
        query += " ORDER BY updated_at DESC, artifact_version DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_design_artifact(dict(row)) for row in rows]

    def append_design_artifact_event(
        self,
        *,
        event_id: str,
        artifact_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO design_artifact_events (
                    event_id, artifact_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, artifact_id, event_type, self._dumps_json(payload or {}), self._utcnow()),
            )
            conn.commit()

    def upsert_artifact_dependency_edge(
        self,
        *,
        edge_id: str,
        project_id: str,
        version_id: str,
        upstream_artifact_id: str,
        downstream_artifact_id: str,
        dependency_type: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        existing = None
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM artifact_dependency_edges
                WHERE project_id = ? AND version_id = ? AND upstream_artifact_id = ? AND downstream_artifact_id = ?
                LIMIT 1
                """,
                (project_id, version_id, upstream_artifact_id, downstream_artifact_id),
            ).fetchone()
            existing = self._row_to_artifact_dependency_edge(dict(row)) if row else None
            conn.execute(
                """
                INSERT OR REPLACE INTO artifact_dependency_edges (
                    edge_id, project_id, version_id, upstream_artifact_id, downstream_artifact_id,
                    dependency_type, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    existing["edge_id"] if existing else edge_id,
                    project_id,
                    version_id,
                    upstream_artifact_id,
                    downstream_artifact_id,
                    dependency_type,
                    self._dumps_json(evidence or {}),
                    existing["created_at"] if existing else now,
                ),
            )
            conn.commit()
        return self.get_artifact_dependency_edge(existing["edge_id"] if existing else edge_id) or {}

    def get_artifact_dependency_edge(self, edge_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_dependency_edges WHERE edge_id = ?",
                (edge_id,),
            ).fetchone()
        return self._row_to_artifact_dependency_edge(dict(row)) if row else None

    def list_artifact_dependency_edges(
        self,
        *,
        project_id: Optional[str] = None,
        version_id: Optional[str] = None,
        upstream_artifact_id: Optional[str] = None,
        downstream_artifact_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM artifact_dependency_edges WHERE 1 = 1"
        params: List[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if version_id:
            query += " AND version_id = ?"
            params.append(version_id)
        if upstream_artifact_id:
            query += " AND upstream_artifact_id = ?"
            params.append(upstream_artifact_id)
        if downstream_artifact_id:
            query += " AND downstream_artifact_id = ?"
            params.append(downstream_artifact_id)
        query += " ORDER BY created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_artifact_dependency_edge(dict(row)) for row in rows]

    def create_artifact_impact_record(
        self,
        *,
        impact_id: str,
        project_id: str,
        version_id: str,
        source_artifact_id: str,
        impacted_artifact_id: str,
        impact_status: str,
        trigger_type: str,
        trigger_ref_id: Optional[str],
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO artifact_impact_records (
                    impact_id, project_id, version_id, source_artifact_id, impacted_artifact_id,
                    impact_status, trigger_type, trigger_ref_id, reason, evidence_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    impact_id,
                    project_id,
                    version_id,
                    source_artifact_id,
                    impacted_artifact_id,
                    impact_status,
                    trigger_type,
                    trigger_ref_id,
                    reason,
                    self._dumps_json(evidence or {}),
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_artifact_impact_record(impact_id) or {}

    def get_artifact_impact_record(self, impact_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_impact_records WHERE impact_id = ?",
                (impact_id,),
            ).fetchone()
        return self._row_to_artifact_impact_record(dict(row)) if row else None

    def list_artifact_impact_records(
        self,
        *,
        project_id: Optional[str] = None,
        version_id: Optional[str] = None,
        source_artifact_id: Optional[str] = None,
        impacted_artifact_id: Optional[str] = None,
        impact_status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM artifact_impact_records WHERE 1 = 1"
        params: List[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if version_id:
            query += " AND version_id = ?"
            params.append(version_id)
        if source_artifact_id:
            query += " AND source_artifact_id = ?"
            params.append(source_artifact_id)
        if impacted_artifact_id:
            query += " AND impacted_artifact_id = ?"
            params.append(impacted_artifact_id)
        if impact_status:
            query += " AND impact_status = ?"
            params.append(impact_status)
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_artifact_impact_record(dict(row)) for row in rows]

    def create_expert_reflection_report(
        self,
        *,
        report_id: str,
        artifact_id: str,
        expert_id: str,
        status: str,
        confidence: float,
        checks: Dict[str, Any],
        issues: List[Any],
        assumptions: List[Any],
        open_questions: List[Any],
        required_actions: List[Any],
        blocks_downstream: bool = False,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO expert_reflection_reports (
                    report_id, artifact_id, expert_id, status, confidence, checks_json, issues_json,
                    assumptions_json, open_questions_json, required_actions_json, blocks_downstream, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    artifact_id,
                    expert_id,
                    status,
                    float(confidence or 0),
                    self._dumps_json(checks or {}),
                    self._dumps_json(issues or []),
                    self._dumps_json(assumptions or []),
                    self._dumps_json(open_questions or []),
                    self._dumps_json(required_actions or []),
                    1 if blocks_downstream else 0,
                    now,
                ),
            )
            conn.commit()
        return self.get_expert_reflection_report(report_id) or {}

    def get_expert_reflection_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM expert_reflection_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return self._row_to_reflection_report(dict(row)) if row else None

    def get_reflection_report_for_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM expert_reflection_reports
                WHERE artifact_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (artifact_id,),
            ).fetchone()
        return self._row_to_reflection_report(dict(row)) if row else None

    def create_system_consistency_report(
        self,
        *,
        report_id: str,
        artifact_id: str,
        project_id: str,
        version_id: str,
        status: str,
        checks: List[Dict[str, Any]],
        conflict_ids: List[str],
        suggested_actions: List[str],
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO system_consistency_reports (
                    report_id, artifact_id, project_id, version_id, status,
                    checks_json, conflict_ids_json, suggested_actions_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    artifact_id,
                    project_id,
                    version_id,
                    status,
                    self._dumps_json(checks or []),
                    self._dumps_json(conflict_ids or []),
                    self._dumps_json(suggested_actions or []),
                    now,
                ),
            )
            conn.commit()
        return self.get_system_consistency_report(report_id) or {}

    def update_system_consistency_report(
        self,
        report_id: str,
        *,
        conflict_ids: Optional[List[str]] = None,
        suggested_actions: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_system_consistency_report(report_id)
        if not existing:
            return None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE system_consistency_reports
                SET conflict_ids_json = ?,
                    suggested_actions_json = ?
                WHERE report_id = ?
                """,
                (
                    self._dumps_json(conflict_ids if conflict_ids is not None else existing.get("conflict_ids", [])),
                    self._dumps_json(suggested_actions if suggested_actions is not None else existing.get("suggested_actions", [])),
                    report_id,
                ),
            )
            conn.commit()
        return self.get_system_consistency_report(report_id)

    def get_system_consistency_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM system_consistency_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return self._row_to_system_consistency_report(dict(row)) if row else None

    def get_system_consistency_report_for_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM system_consistency_reports
                WHERE artifact_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (artifact_id,),
            ).fetchone()
        return self._row_to_system_consistency_report(dict(row)) if row else None

    def create_context_conflict(
        self,
        *,
        conflict_id: str,
        report_id: Optional[str],
        project_id: str,
        version_id: str,
        artifact_id: Optional[str],
        conflict_type: str,
        semantic: str,
        severity: str,
        status: str,
        summary: str,
        evidence_refs: Optional[List[Any]] = None,
        suggested_actions: Optional[List[str]] = None,
        decision_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO context_conflicts (
                    conflict_id, report_id, project_id, version_id, artifact_id,
                    conflict_type, semantic, severity, status, summary,
                    evidence_refs_json, suggested_actions_json, decision_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict_id,
                    report_id,
                    project_id,
                    version_id,
                    artifact_id,
                    conflict_type,
                    semantic,
                    severity,
                    status,
                    summary,
                    self._dumps_json(evidence_refs or []),
                    self._dumps_json(suggested_actions or []),
                    decision_id,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_context_conflict(conflict_id) or {}

    def get_context_conflict(self, conflict_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM context_conflicts WHERE conflict_id = ?",
                (conflict_id,),
            ).fetchone()
        return self._row_to_context_conflict(dict(row)) if row else None

    def list_context_conflicts(
        self,
        *,
        project_id: Optional[str] = None,
        version_id: Optional[str] = None,
        report_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM context_conflicts WHERE 1 = 1"
        params: List[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if version_id:
            query += " AND version_id = ?"
            params.append(version_id)
        if report_id:
            query += " AND report_id = ?"
            params.append(report_id)
        if artifact_id:
            query += " AND artifact_id = ?"
            params.append(artifact_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_context_conflict(dict(row)) for row in rows]

    def create_decision_log(
        self,
        *,
        decision_id: str,
        project_id: str,
        version_id: str,
        scope: str,
        conflict_ids: Optional[List[str]] = None,
        decision: str,
        basis: str,
        authority: str,
        applies_to: Optional[List[str]] = None,
        evidence_refs: Optional[List[Dict[str, Any]]] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO decision_logs (
                    decision_id, project_id, version_id, scope, conflict_ids_json,
                    decision, basis, authority, applies_to_json, evidence_refs_json,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    project_id,
                    version_id,
                    scope,
                    self._dumps_json(conflict_ids or []),
                    decision,
                    basis,
                    authority,
                    self._dumps_json(applies_to or []),
                    self._dumps_json(evidence_refs or []),
                    created_by,
                    self._utcnow(),
                ),
            )
            conn.commit()
        return self.get_decision_log(decision_id) or {}

    def get_decision_log(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM decision_logs WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return self._row_to_decision_log(dict(row)) if row else None

    def list_decision_logs(
        self,
        *,
        project_id: Optional[str] = None,
        version_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM decision_logs WHERE 1 = 1"
        params: List[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if version_id:
            query += " AND version_id = ?"
            params.append(version_id)
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_decision_log(dict(row)) for row in rows]

    def list_decision_logs_for_conflict(self, conflict_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM decision_logs
                WHERE conflict_ids_json LIKE ?
                ORDER BY created_at DESC
                """,
                (f'%"{conflict_id}"%',),
            ).fetchall()
        return [self._row_to_decision_log(dict(row)) for row in rows]

    def update_context_conflict(
        self,
        conflict_id: str,
        *,
        status: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_context_conflict(conflict_id)
        if not existing:
            return None
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE context_conflicts
                SET status = ?,
                    decision_id = ?,
                    updated_at = ?
                WHERE conflict_id = ?
                """,
                (
                    status or existing.get("status"),
                    decision_id if decision_id is not None else existing.get("decision_id"),
                    self._utcnow(),
                    conflict_id,
                ),
            )
            conn.commit()
        return self.get_context_conflict(conflict_id)

    def list_open_context_conflicts(self, project_id: str, version_id: str) -> List[Dict[str, Any]]:
        return self.list_context_conflicts(project_id=project_id, version_id=version_id, status="open")

    def create_revision_session(
        self,
        *,
        revision_session_id: str,
        project_id: str,
        version_id: str,
        target_artifact_id: str,
        target_expert_id: str,
        status: str,
        user_feedback: str = "",
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO revision_sessions (
                    revision_session_id, project_id, version_id, target_artifact_id, target_expert_id,
                    status, user_feedback, normalized_revision_request_json, affected_artifacts_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_session_id,
                    project_id,
                    version_id,
                    target_artifact_id,
                    target_expert_id,
                    status,
                    user_feedback or "",
                    self._dumps_json({}),
                    self._dumps_json([]),
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_revision_session(revision_session_id) or {}

    def update_revision_session(
        self,
        revision_session_id: str,
        *,
        status: Optional[str] = None,
        user_feedback: Optional[str] = None,
        normalized_revision_request: Any = JSON_UNSET,
        conflict_report_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        affected_artifacts: Any = JSON_UNSET,
        created_artifact_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_revision_session(revision_session_id)
        if not existing:
            return None
        effective_normalized = existing.get("normalized_revision_request") if normalized_revision_request is JSON_UNSET else (normalized_revision_request or {})
        effective_affected = existing.get("affected_artifacts") if affected_artifacts is JSON_UNSET else (affected_artifacts or [])
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE revision_sessions
                SET status = ?,
                    user_feedback = ?,
                    normalized_revision_request_json = ?,
                    conflict_report_id = ?,
                    decision_id = ?,
                    affected_artifacts_json = ?,
                    created_artifact_id = ?,
                    updated_at = ?
                WHERE revision_session_id = ?
                """,
                (
                    status or existing.get("status"),
                    user_feedback if user_feedback is not None else existing.get("user_feedback"),
                    self._dumps_json(effective_normalized),
                    conflict_report_id if conflict_report_id is not None else existing.get("conflict_report_id"),
                    decision_id if decision_id is not None else existing.get("decision_id"),
                    self._dumps_json(effective_affected),
                    created_artifact_id if created_artifact_id is not None else existing.get("created_artifact_id"),
                    self._utcnow(),
                    revision_session_id,
                ),
            )
            conn.commit()
        return self.get_revision_session(revision_session_id)

    def get_revision_session(self, revision_session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM revision_sessions WHERE revision_session_id = ?",
                (revision_session_id,),
            ).fetchone()
        return self._row_to_revision_session(dict(row)) if row else None

    def list_revision_sessions(
        self,
        *,
        project_id: Optional[str] = None,
        version_id: Optional[str] = None,
        target_artifact_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM revision_sessions WHERE 1 = 1"
        params: List[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if version_id:
            query += " AND version_id = ?"
            params.append(version_id)
        if target_artifact_id:
            query += " AND target_artifact_id = ?"
            params.append(target_artifact_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_revision_session(dict(row)) for row in rows]

    def append_revision_session_event(
        self,
        *,
        event_id: str,
        revision_session_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO revision_session_events (
                    event_id, revision_session_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, revision_session_id, event_type, self._dumps_json(payload or {}), self._utcnow()),
            )
            conn.commit()

    def list_revision_session_events(self, revision_session_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM revision_session_events
                WHERE revision_session_id = ?
                ORDER BY created_at ASC
                """,
                (revision_session_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "revision_session_id": row["revision_session_id"],
                "event_type": row["event_type"],
                "payload": self._loads_json(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_artifact_anchor(
        self,
        *,
        anchor_id: str,
        artifact_id: str,
        file_name: str,
        anchor_type: str,
        label: str,
        text_excerpt: str,
        start_offset: int,
        end_offset: int,
        structural_path: Optional[Dict[str, Any]],
        content_hash: str,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO artifact_anchors (
                    anchor_id, artifact_id, file_name, anchor_type, label, text_excerpt,
                    start_offset, end_offset, structural_path_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor_id,
                    artifact_id,
                    file_name,
                    anchor_type,
                    label,
                    text_excerpt,
                    int(start_offset),
                    int(end_offset),
                    self._dumps_json(structural_path or {}),
                    content_hash,
                    now,
                ),
            )
            conn.commit()
        return self.get_artifact_anchor(anchor_id) or {}

    def get_artifact_anchor(self, anchor_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_anchors WHERE anchor_id = ?",
                (anchor_id,),
            ).fetchone()
        return self._row_to_artifact_anchor(dict(row)) if row else None

    def list_artifact_anchors(self, artifact_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact_anchors WHERE artifact_id = ? ORDER BY created_at DESC",
                (artifact_id,),
            ).fetchall()
        return [self._row_to_artifact_anchor(dict(row)) for row in rows]

    def create_revision_patch(
        self,
        *,
        patch_id: str,
        revision_session_id: str,
        artifact_id: str,
        anchor_id: str,
        scope: str,
        preserve_policy: str,
        patch_status: str,
        source_content_hash: str,
        allowed_range: Dict[str, Any],
        diff: Dict[str, Any],
        rationale: str,
        predicted_impact: Dict[str, Any],
        apply_result: Dict[str, Any],
        post_apply_validation: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = self._utcnow()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO revision_patches (
                    patch_id, revision_session_id, artifact_id, anchor_id, scope, preserve_policy,
                    patch_status, source_content_hash, allowed_range_json, diff_json, rationale,
                    predicted_impact_json, apply_result_json, post_apply_validation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patch_id,
                    revision_session_id,
                    artifact_id,
                    anchor_id,
                    scope,
                    preserve_policy,
                    patch_status,
                    source_content_hash,
                    self._dumps_json(allowed_range or {}),
                    self._dumps_json(diff or {}),
                    rationale or "",
                    self._dumps_json(predicted_impact or {}),
                    self._dumps_json(apply_result or {}),
                    self._dumps_json(post_apply_validation or {}),
                    now,
                ),
            )
            conn.commit()
        return self.get_revision_patch(patch_id) or {}

    def update_revision_patch(
        self,
        patch_id: str,
        *,
        patch_status: Optional[str] = None,
        created_artifact_id: Optional[str] = None,
        apply_result: Any = JSON_UNSET,
        post_apply_content_hash: Optional[str] = None,
        post_apply_validation: Any = JSON_UNSET,
        applied: bool = False,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_revision_patch(patch_id)
        if not existing:
            return None
        effective_apply_result = existing.get("apply_result") if apply_result is JSON_UNSET else (apply_result or {})
        effective_validation = existing.get("post_apply_validation") if post_apply_validation is JSON_UNSET else (post_apply_validation or {})
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE revision_patches
                SET patch_status = ?,
                    created_artifact_id = ?,
                    apply_result_json = ?,
                    post_apply_content_hash = ?,
                    post_apply_validation_json = ?,
                    applied_at = ?
                WHERE patch_id = ?
                """,
                (
                    patch_status or existing.get("patch_status"),
                    created_artifact_id if created_artifact_id is not None else existing.get("created_artifact_id"),
                    self._dumps_json(effective_apply_result),
                    post_apply_content_hash if post_apply_content_hash is not None else existing.get("post_apply_content_hash"),
                    self._dumps_json(effective_validation),
                    self._utcnow() if applied else existing.get("applied_at"),
                    patch_id,
                ),
            )
            conn.commit()
        return self.get_revision_patch(patch_id)

    def get_revision_patch(self, patch_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM revision_patches WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        return self._row_to_revision_patch(dict(row)) if row else None

    def list_revision_patches(self, revision_session_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM revision_patches WHERE revision_session_id = ? ORDER BY created_at DESC",
                (revision_session_id,),
            ).fetchall()
        return [self._row_to_revision_patch(dict(row)) for row in rows]

    def upsert_artifact_section_review(
        self,
        *,
        section_review_id: str,
        artifact_id: str,
        anchor_id: Optional[str],
        status: str,
        reviewer_note: str = "",
        revision_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._utcnow()
        existing = None
        with self._get_connection() as conn:
            if anchor_id:
                row = conn.execute(
                    """
                    SELECT * FROM artifact_section_reviews
                    WHERE artifact_id = ? AND anchor_id = ?
                    LIMIT 1
                    """,
                    (artifact_id, anchor_id),
                ).fetchone()
                existing = self._row_to_artifact_section_review(dict(row)) if row else None
            review_id = existing["section_review_id"] if existing else section_review_id
            created_at = existing.get("created_at") if existing else now
            conn.execute(
                """
                INSERT INTO artifact_section_reviews (
                    section_review_id, artifact_id, anchor_id, status, reviewer_note,
                    revision_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(section_review_id) DO UPDATE SET
                    status=excluded.status,
                    reviewer_note=excluded.reviewer_note,
                    revision_session_id=COALESCE(excluded.revision_session_id, artifact_section_reviews.revision_session_id),
                    updated_at=excluded.updated_at
                """,
                (
                    review_id,
                    artifact_id,
                    anchor_id,
                    status,
                    reviewer_note or "",
                    revision_session_id,
                    created_at,
                    now,
                ),
            )
            conn.commit()
        return self.get_artifact_section_review(review_id) or {}

    def get_artifact_section_review(self, section_review_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_section_reviews WHERE section_review_id = ?",
                (section_review_id,),
            ).fetchone()
        return self._row_to_artifact_section_review(dict(row)) if row else None

    def list_artifact_section_reviews(
        self,
        artifact_id: str,
        *,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM artifact_section_reviews WHERE artifact_id = ?"
        params: List[Any] = [artifact_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_artifact_section_review(dict(row)) for row in rows]

    def _row_to_workflow_task(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "agent_type": row["node_type"],
            "id": row.get("task_id") or row["node_type"],
            "run_id": row.get("run_id"),
            "phase": row.get("phase"),
            "status": row["status"],
            "attempt": int(row.get("attempt") or 0),
            "priority": row.get("priority"),
            "dependencies": self._loads_json(row.get("dependencies_json"), []),
            "metadata": self._loads_json(row.get("metadata_json"), {}),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _row_to_human_interaction(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "interaction_id": row["interaction_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "run_id": row.get("run_id"),
            "scope": row["scope"],
            "owner_node": row["owner_node"],
            "owner_expert_id": row.get("owner_expert_id"),
            "status": row["status"],
            "turn_index": int(row.get("turn_index") or 0),
            "parent_interaction_id": row.get("parent_interaction_id"),
            "question_text": row.get("question_text") or "",
            "question_schema": self._loads_json(row.get("question_schema_json"), {}),
            "context": self._loads_json(row.get("context_json"), {}),
            "answer": self._loads_json(row.get("answer_json"), {}),
            "summary": row.get("summary") or "",
            "knowledge_refs": self._loads_json(row.get("knowledge_refs_json"), []),
            "affected_artifacts": self._loads_json(row.get("affected_artifacts_json"), []),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "completed_at": row.get("completed_at"),
        }

    def _row_to_design_artifact(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "run_id": row.get("run_id"),
            "expert_id": row["expert_id"],
            "artifact_type": row["artifact_type"],
            "artifact_version": int(row.get("artifact_version") or 1),
            "parent_artifact_id": row.get("parent_artifact_id"),
            "status": row["status"],
            "title": row["title"],
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "content_hash": row["content_hash"],
            "summary": row.get("summary") or "",
            "source_refs": self._loads_json(row.get("source_refs_json"), []),
            "dependency_refs": self._loads_json(row.get("dependency_refs_json"), []),
            "decision_refs": self._loads_json(row.get("decision_refs_json"), []),
            "reflection_report_id": row.get("reflection_report_id"),
            "consistency_report_id": row.get("consistency_report_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _row_to_artifact_dependency_edge(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "edge_id": row["edge_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "upstream_artifact_id": row["upstream_artifact_id"],
            "downstream_artifact_id": row["downstream_artifact_id"],
            "dependency_type": row["dependency_type"],
            "evidence": self._loads_json(row.get("evidence_json"), {}),
            "created_at": row.get("created_at"),
        }

    def _row_to_artifact_impact_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "impact_id": row["impact_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "source_artifact_id": row["source_artifact_id"],
            "impacted_artifact_id": row["impacted_artifact_id"],
            "impact_status": row["impact_status"],
            "trigger_type": row["trigger_type"],
            "trigger_ref_id": row.get("trigger_ref_id"),
            "reason": row["reason"],
            "evidence": self._loads_json(row.get("evidence_json"), {}),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _row_to_reflection_report(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "report_id": row["report_id"],
            "artifact_id": row["artifact_id"],
            "expert_id": row["expert_id"],
            "status": row["status"],
            "confidence": float(row.get("confidence") or 0),
            "checks": self._loads_json(row.get("checks_json"), {}),
            "issues": self._loads_json(row.get("issues_json"), []),
            "assumptions": self._loads_json(row.get("assumptions_json"), []),
            "open_questions": self._loads_json(row.get("open_questions_json"), []),
            "required_actions": self._loads_json(row.get("required_actions_json"), []),
            "blocks_downstream": bool(row.get("blocks_downstream")),
            "created_at": row.get("created_at"),
        }

    def _row_to_system_consistency_report(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "report_id": row["report_id"],
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "status": row["status"],
            "checks": self._loads_json(row.get("checks_json"), []),
            "conflict_ids": self._loads_json(row.get("conflict_ids_json"), []),
            "suggested_actions": self._loads_json(row.get("suggested_actions_json"), []),
            "created_at": row.get("created_at"),
        }

    def _row_to_context_conflict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "conflict_id": row["conflict_id"],
            "report_id": row.get("report_id"),
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "artifact_id": row.get("artifact_id"),
            "conflict_type": row["conflict_type"],
            "semantic": row["semantic"],
            "severity": row["severity"],
            "status": row["status"],
            "summary": row["summary"],
            "evidence_refs": self._loads_json(row.get("evidence_refs_json"), []),
            "suggested_actions": self._loads_json(row.get("suggested_actions_json"), []),
            "decision_id": row.get("decision_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _row_to_decision_log(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "scope": row["scope"],
            "conflict_ids": self._loads_json(row.get("conflict_ids_json"), []),
            "decision": row["decision"],
            "basis": row["basis"],
            "authority": row["authority"],
            "applies_to": self._loads_json(row.get("applies_to_json"), []),
            "evidence_refs": self._loads_json(row.get("evidence_refs_json"), []),
            "created_by": row.get("created_by"),
            "created_at": row.get("created_at"),
        }

    def _row_to_revision_session(self, row: Dict[str, Any]) -> Dict[str, Any]:
        session_id = row["revision_session_id"]
        return {
            "revision_session_id": session_id,
            "project_id": row["project_id"],
            "version_id": row["version_id"],
            "target_artifact_id": row["target_artifact_id"],
            "target_expert_id": row["target_expert_id"],
            "status": row["status"],
            "user_feedback": row.get("user_feedback") or "",
            "normalized_revision_request": self._loads_json(row.get("normalized_revision_request_json"), {}),
            "conflict_report_id": row.get("conflict_report_id"),
            "decision_id": row.get("decision_id"),
            "affected_artifacts": self._loads_json(row.get("affected_artifacts_json"), []),
            "created_artifact_id": row.get("created_artifact_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "events": self.list_revision_session_events(session_id),
            "patches": self.list_revision_patches(session_id),
        }

    def _row_to_artifact_anchor(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "anchor_id": row["anchor_id"],
            "artifact_id": row["artifact_id"],
            "file_name": row["file_name"],
            "anchor_type": row["anchor_type"],
            "label": row.get("label") or "",
            "text_excerpt": row.get("text_excerpt") or "",
            "start_offset": int(row.get("start_offset") or 0),
            "end_offset": int(row.get("end_offset") or 0),
            "structural_path": self._loads_json(row.get("structural_path_json"), {}),
            "content_hash": row["content_hash"],
            "created_at": row.get("created_at"),
        }

    def _row_to_revision_patch(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "patch_id": row["patch_id"],
            "revision_session_id": row["revision_session_id"],
            "artifact_id": row["artifact_id"],
            "anchor_id": row["anchor_id"],
            "scope": row["scope"],
            "preserve_policy": row["preserve_policy"],
            "patch_status": row["patch_status"],
            "source_content_hash": row["source_content_hash"],
            "allowed_range": self._loads_json(row.get("allowed_range_json"), {}),
            "diff": self._loads_json(row.get("diff_json"), {}),
            "rationale": row.get("rationale") or "",
            "predicted_impact": self._loads_json(row.get("predicted_impact_json"), {}),
            "created_artifact_id": row.get("created_artifact_id"),
            "apply_result": self._loads_json(row.get("apply_result_json"), {}),
            "post_apply_content_hash": row.get("post_apply_content_hash"),
            "post_apply_validation": self._loads_json(row.get("post_apply_validation_json"), {}),
            "created_at": row.get("created_at"),
            "applied_at": row.get("applied_at"),
        }

    def _row_to_artifact_section_review(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "section_review_id": row["section_review_id"],
            "artifact_id": row["artifact_id"],
            "anchor_id": row.get("anchor_id"),
            "status": row["status"],
            "reviewer_note": row.get("reviewer_note") or "",
            "revision_session_id": row.get("revision_session_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def list_versions(self, project_id: str, page: int = 1, page_size: int = 10) -> Dict[str, Any]:
        offset = (page - 1) * page_size
        with self._get_connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM versions WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT * FROM versions
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (project_id, page_size, offset),
            ).fetchall()
        return {
            "versions": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def delete_version(self, project_id: str, version_id: str):
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM versions WHERE project_id = ? AND version_id = ?",
                (project_id, version_id),
            )
            conn.commit()

    def get_version(self, project_id: str, version_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE project_id = ? AND version_id = ?",
                (project_id, version_id),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["pending_interrupt"] = self._loads_json(data.pop("pending_interrupt_json", None), None)
        return data

    def upsert_repository(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_repository(project_id, payload["id"], include_secrets=True)
        token = payload.get("token")
        encrypted_token = self.codec.encrypt(token) if token is not None else (existing.get("_token_encrypted") if existing else None)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO repositories (
                    id, project_id, name, type, url, branch, username, token, local_path,
                    description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, id) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    url=excluded.url,
                    branch=excluded.branch,
                    username=excluded.username,
                    token=excluded.token,
                    local_path=excluded.local_path,
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["id"],
                    project_id,
                    payload["name"],
                    payload.get("type", "git"),
                    payload["url"],
                    payload.get("branch", "main"),
                    payload.get("username"),
                    encrypted_token,
                    payload.get("local_path"),
                    payload.get("description"),
                    existing.get("created_at") if existing else now,
                    now,
                ),
            )
            conn.commit()
        return self.get_repository(project_id, payload["id"], include_secrets=False)

    def list_repositories(self, project_id: str, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM repositories WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._row_to_repository(dict(row), include_secrets) for row in rows]

    def get_repository(self, project_id: str, repo_id: str, include_secrets: bool = False) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE project_id = ? AND id = ?",
                (project_id, repo_id),
            ).fetchone()
        return self._row_to_repository(dict(row), include_secrets) if row else None

    def delete_repository(self, project_id: str, repo_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM repositories WHERE project_id = ? AND id = ?",
                (project_id, repo_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _row_to_repository(self, row: Dict[str, Any], include_secrets: bool) -> Dict[str, Any]:
        encrypted_token = row.pop("token", None)
        result = {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "type": row["type"],
            "url": row["url"],
            "branch": row["branch"],
            "username": row["username"],
            "local_path": row["local_path"],
            "description": row["description"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secrets:
            result["token"] = self.codec.decrypt(encrypted_token)
            result["_token_encrypted"] = encrypted_token
        else:
            token = self.codec.decrypt(encrypted_token) if encrypted_token else None
            result["token"] = self.codec.mask(token)
            result["has_token"] = bool(encrypted_token)
        return result

    def upsert_database(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_database(project_id, payload["id"], include_secrets=True)
        password = payload.get("password")
        encrypted_password = self.codec.encrypt(password) if password is not None else (existing.get("_password_encrypted") if existing else None)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO databases (
                    id, project_id, name, type, host, port, database_name, username, password,
                    schema_filter, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, id) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    host=excluded.host,
                    port=excluded.port,
                    database_name=excluded.database_name,
                    username=excluded.username,
                    password=excluded.password,
                    schema_filter=excluded.schema_filter,
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["id"],
                    project_id,
                    payload["name"],
                    payload["type"],
                    payload["host"],
                    payload["port"],
                    payload["database"],
                    payload.get("username"),
                    encrypted_password,
                    self._dumps_json(payload.get("schema_filter", [])),
                    payload.get("description"),
                    (existing.get("created_at") if existing and existing.get("created_at") else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_database(project_id, payload["id"], include_secrets=False)

    def list_databases(self, project_id: str, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM databases WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._row_to_database(dict(row), include_secrets) for row in rows]

    def get_database(self, project_id: str, db_id: str, include_secrets: bool = False) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM databases WHERE project_id = ? AND id = ?",
                (project_id, db_id),
            ).fetchone()
        return self._row_to_database(dict(row), include_secrets) if row else None

    def delete_database(self, project_id: str, db_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM databases WHERE project_id = ? AND id = ?",
                (project_id, db_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _row_to_database(self, row: Dict[str, Any], include_secrets: bool) -> Dict[str, Any]:
        encrypted_password = row.pop("password", None)
        result = {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "type": row["type"],
            "host": row["host"],
            "port": row["port"],
            "database": row["database_name"],
            "username": row["username"],
            "schema_filter": self._loads_json(row["schema_filter"], []),
            "description": row["description"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secrets:
            result["password"] = self.codec.decrypt(encrypted_password)
            result["_password_encrypted"] = encrypted_password
        else:
            password = self.codec.decrypt(encrypted_password) if encrypted_password else None
            result["password"] = self.codec.mask(password)
            result["has_password"] = bool(encrypted_password)
        return result

    def upsert_knowledge_base(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_knowledge_base(project_id, payload["id"])
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_bases (
                    id, project_id, name, type, path, index_url, includes, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, id) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    path=excluded.path,
                    index_url=excluded.index_url,
                    includes=excluded.includes,
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["id"],
                    project_id,
                    payload["name"],
                    payload["type"],
                    payload.get("path"),
                    payload.get("index_url"),
                    self._dumps_json(payload.get("includes", [])),
                    payload.get("description"),
                    existing.get("created_at") if existing else now,
                    now,
                ),
            )
            conn.commit()
        return self.get_knowledge_base(project_id, payload["id"])

    def list_knowledge_bases(self, project_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_bases WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._row_to_knowledge_base(dict(row)) for row in rows]

    def get_knowledge_base(self, project_id: str, kb_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_bases WHERE project_id = ? AND id = ?",
                (project_id, kb_id),
            ).fetchone()
        return self._row_to_knowledge_base(dict(row)) if row else None

    def delete_knowledge_base(self, project_id: str, kb_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM knowledge_bases WHERE project_id = ? AND id = ?",
                (project_id, kb_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _row_to_knowledge_base(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "type": row["type"],
            "path": row["path"],
            "index_url": row["index_url"],
            "includes": self._loads_json(row["includes"], []),
            "description": row["description"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_project_expert(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_project_expert(project_id, payload["id"])
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO project_experts (
                    expert_id, project_id, enabled, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, expert_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["id"],
                    project_id,
                    1 if payload.get("enabled", True) else 0,
                    payload.get("description"),
                    (existing.get("created_at") if existing and existing.get("created_at") else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_project_expert(project_id, payload["id"]) or {
            "id": payload["id"],
            "project_id": project_id,
            "name": payload.get("name", payload["id"]),
            "name_zh": payload.get("name_zh"),
            "name_en": payload.get("name_en", payload.get("name", payload["id"])),
            "enabled": bool(payload.get("enabled", True)),
            "description": payload.get("description"),
            "created_at": (existing.get("created_at") if existing and existing.get("created_at") else now),
            "updated_at": now,
        }

    def list_project_experts(self, project_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_experts WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        stored = {row["expert_id"]: dict(row) for row in rows}

        try:
            from registry.expert_registry import ExpertRegistry

            registry = ExpertRegistry.get_instance()
            manifests = sorted(registry.get_all_manifests(), key=lambda item: item.capability)
        except RuntimeError:
            manifests = []

        # System experts that should not be configurable per project
        system_experts = {"expert-creator"}

        experts: List[Dict[str, Any]] = []
        for manifest in manifests:
            if manifest.capability in system_experts:
                continue

            row = stored.pop(manifest.capability, None)
            experts.append(
                {
                    "id": manifest.capability,
                    "project_id": project_id,
                    "name": manifest.name or manifest.capability,
                    "name_zh": manifest.name_zh or None,
                    "name_en": manifest.name_en or manifest.name or manifest.capability,
                    "enabled": bool(row["enabled"]) if row else False,
                    "description": row["description"] if row and row.get("description") else manifest.description,
                    "created_at": row["created_at"] if row else None,
                    "updated_at": row["updated_at"] if row else None,
                }
            )

        if not manifests:
            for expert_id, row in stored.items():
                if expert_id in system_experts:
                    continue

                experts.append(
                    {
                        "id": expert_id,
                        "project_id": project_id,
                        "name": expert_id,
                        "name_zh": None,
                        "name_en": expert_id,
                        "enabled": bool(row["enabled"]),
                        "description": row.get("description"),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )

        return experts

    def get_project_expert(self, project_id: str, expert_id: str) -> Optional[Dict[str, Any]]:
        for expert in self.list_project_experts(project_id):
            if expert["id"] == expert_id:
                return expert
        return None

    def list_enabled_expert_ids(self, project_id: str) -> List[str]:
        return [expert["id"] for expert in self.list_project_experts(project_id) if expert.get("enabled")]

    def upsert_project_llm_config(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_project_llm_config(project_id, include_secrets=True, merge_defaults=False)

        openai_api_key = payload.get("openai_api_key")
        llm_provider = str(
            payload.get("llm_provider")
            or (existing.get("llm_provider") if existing else "")
            or "openai"
        ).strip().lower()
        if llm_provider != "openai":
            llm_provider = "openai"

        encrypted_openai_api_key = (
            self.codec.encrypt(openai_api_key)
            if openai_api_key not in (None, "")
            else (existing.get("_openai_api_key_encrypted") if existing else None)
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO project_llm_configs (
                    project_id, llm_provider, openai_api_key, openai_base_url, openai_model_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    llm_provider=excluded.llm_provider,
                    openai_api_key=excluded.openai_api_key,
                    openai_base_url=excluded.openai_base_url,
                    openai_model_name=excluded.openai_model_name,
                    updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    llm_provider,
                    encrypted_openai_api_key,
                    payload.get("openai_base_url"),
                    payload.get("openai_model_name"),
                    (existing.get("created_at") if existing and existing.get("created_at") else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_project_llm_config(project_id, include_secrets=False, merge_defaults=True)

    def get_project_llm_config(
        self,
        project_id: str,
        *,
        include_secrets: bool = False,
        merge_defaults: bool = True,
    ) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM project_llm_configs WHERE project_id = ?",
                (project_id,),
            ).fetchone()

        project_config = self._row_to_project_llm_config(dict(row), include_secrets=include_secrets) if row else None
        if not merge_defaults:
            return project_config or {
                "project_id": project_id,
                "llm_provider": "openai",
                "openai_api_key": None if include_secrets else None,
                "openai_base_url": None,
                "openai_model_name": None,
                "has_openai_api_key": False,
            }

        defaults = self.get_system_llm_defaults(include_secrets=include_secrets)
        result: Dict[str, Any] = {
            "project_id": project_id,
            "llm_provider": (project_config or {}).get("llm_provider") or defaults.get("llm_provider") or "openai",
            "openai_base_url": (project_config or {}).get("openai_base_url") or defaults.get("openai_base_url") or "",
            "openai_model_name": (project_config or {}).get("openai_model_name") or defaults.get("openai_model_name") or "",
            "has_openai_api_key": bool((project_config or {}).get("has_openai_api_key") or defaults.get("has_openai_api_key")),
        }

        if include_secrets:
            result["openai_api_key"] = (project_config or {}).get("openai_api_key") or defaults.get("openai_api_key")
        else:
            result["openai_api_key"] = (
                (project_config or {}).get("openai_api_key")
                or defaults.get("openai_api_key")
            )

        if project_config:
            result["created_at"] = project_config.get("created_at")
            result["updated_at"] = project_config.get("updated_at")
        return result

    def _row_to_project_llm_config(self, row: Dict[str, Any], include_secrets: bool) -> Dict[str, Any]:
        encrypted_openai_api_key = row.pop("openai_api_key", None)
        openai_api_key = self.codec.decrypt(encrypted_openai_api_key) if encrypted_openai_api_key else None

        result: Dict[str, Any] = {
            "project_id": row["project_id"],
            "llm_provider": "openai",
            "openai_base_url": row["openai_base_url"],
            "openai_model_name": row["openai_model_name"],
            "has_openai_api_key": bool(encrypted_openai_api_key),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secrets:
            result["openai_api_key"] = openai_api_key
            result["_openai_api_key_encrypted"] = encrypted_openai_api_key
        else:
            result["openai_api_key"] = self.codec.mask(openai_api_key)
        return result

    def upsert_project_debug_config(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = self._utcnow()
        existing = self.get_project_debug_config(project_id)
        llm_interaction_logging_enabled = bool(payload.get("llm_interaction_logging_enabled", False))
        llm_full_payload_logging_enabled = bool(payload.get("llm_full_payload_logging_enabled", False))
        if not llm_interaction_logging_enabled:
            llm_full_payload_logging_enabled = False

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO project_debug_configs (
                    project_id, llm_interaction_logging_enabled, llm_full_payload_logging_enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    llm_interaction_logging_enabled=excluded.llm_interaction_logging_enabled,
                    llm_full_payload_logging_enabled=excluded.llm_full_payload_logging_enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    1 if llm_interaction_logging_enabled else 0,
                    1 if llm_full_payload_logging_enabled else 0,
                    (existing.get("created_at") if existing and existing.get("created_at") else now),
                    now,
                ),
            )
            conn.commit()
        return self.get_project_debug_config(project_id)

    def get_project_debug_config(self, project_id: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM project_debug_configs WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if not row:
            return {
                "project_id": project_id,
                "llm_interaction_logging_enabled": False,
                "llm_full_payload_logging_enabled": False,
                "created_at": None,
                "updated_at": None,
            }
        raw = dict(row)
        return {
            "project_id": raw["project_id"],
            "llm_interaction_logging_enabled": bool(raw["llm_interaction_logging_enabled"]),
            "llm_full_payload_logging_enabled": bool(raw["llm_full_payload_logging_enabled"]),
            "created_at": raw["created_at"],
            "updated_at": raw["updated_at"],
        }


metadata_db = MetadataDB()

