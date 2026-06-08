import os
import json
import datetime
import hashlib
import re
from pathlib import Path
from typing import Any

# =====================================================================
# 持久化日志存储功能
# =====================================================================

_RUN_LOG_TIMESTAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]\s+")
_SENSITIVE_KEY_RE = re.compile(
    r"(api[-_ ]?key|authorization|proxy[-_ ]?authorization|token|password|secret|credential)",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<creds>[^/@\s]+)@")
_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_INLINE_SECRET_RE = re.compile(
    r"(?P<key>api[-_ ]?key|authorization|token|password|secret|x-api-key)(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"
_MAX_PAYLOAD_FILE_BYTES = max(1024, int(os.getenv("LLM_LOG_MAX_PAYLOAD_FILE_BYTES", str(512 * 1024))))
_MAX_LLM_LOG_FILES = max(0, int(os.getenv("LLM_LOG_MAX_FILES", "2000")))
_MAX_LLM_LOG_DIR_BYTES = max(0, int(os.getenv("LLM_LOG_MAX_DIR_BYTES", str(256 * 1024 * 1024))))
_LLM_LOG_RETENTION_DAYS = max(0, int(os.getenv("LLM_LOG_RETENTION_DAYS", "30")))


def _get_run_log_timestamp() -> str:
    """Generate a human-readable local timestamp for orchestrator_run.log."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def format_run_log_entry(message: str) -> str:
    """
    Prefix an orchestrator run log line with a timestamp unless it already has one.
    """
    text = str(message or "")
    if _RUN_LOG_TIMESTAMP_RE.match(text):
        return text
    return f"[{_get_run_log_timestamp()}] {text}"


def run_log_dedupe_key(message: str) -> str:
    """
    Return a stable comparison key so timestamped and legacy untimestamped
    copies of the same log line are treated as duplicates.
    """
    return _RUN_LOG_TIMESTAMP_RE.sub("", str(message or ""), count=1)


def _resolve_version_log_dir(project_id: str, version: str, base_dir: Path) -> Path:
    try:
        from services.version_path_resolver import resolve_version_path

        return resolve_version_path(project_id, version) / "logs"
    except Exception:
        return base_dir / "projects" / project_id / version / "logs"

def save_run_log(project_id: str, version: str, base_dir: Path, logs: list):
    """
    将执行日志持久化到项目的对应版本目录下
    """
    try:
        log_dir = _resolve_version_log_dir(project_id, version, base_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "orchestrator_run.log"
        
        with open(log_file, "w", encoding="utf-8") as f:
            for log in logs:
                f.write(format_run_log_entry(log) + "\n")
        print(f"[LogService] Successfully saved {len(logs)} logs to {log_file}")
    except Exception as e:
        print(f"[LogService] Error saving logs: {e}")

def _get_content_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()

def _get_timestamp_id() -> str:
    """Generate a high-precision timestamp ID: YYYYMMDD_HHMMSS_mmm"""
    now = datetime.datetime.now()
    return now.strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Keep 3 digits of microsecond for milliseconds


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def redact_sensitive_payload(value: Any, *, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEY_RE.search(str(key)):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): redact_sensitive_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    if isinstance(value, str):
        text = _URL_CREDENTIAL_RE.sub(r"\g<scheme>[REDACTED]@", value)
        text = _BEARER_RE.sub(lambda match: f"{match.group(1)} {_REDACTED}", text)
        text = _INLINE_SECRET_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}{_REDACTED}", text)
        return text
    return value


def _safe_text(value: str | None) -> str:
    return str(redact_sensitive_payload(value or ""))


def _safe_json_text(value: Any) -> str:
    return json.dumps(redact_sensitive_payload(value), ensure_ascii=False)


def _write_limited_text(path: Path, content: str) -> tuple[str, dict]:
    encoded = content.encode("utf-8")
    metadata = {
        "bytes": len(encoded),
        "truncated": False,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    if len(encoded) <= _MAX_PAYLOAD_FILE_BYTES:
        path.write_text(content, encoding="utf-8")
        return path.name, metadata

    summary = {
        "truncated": True,
        "reason": "payload_file_size_limit_exceeded",
        "original_bytes": len(encoded),
        "sha256": metadata["sha256"],
        "max_bytes": _MAX_PAYLOAD_FILE_BYTES,
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata["truncated"] = True
    return path.name, metadata


def _iter_project_llm_log_files(project_dir: Path) -> list[Path]:
    if not project_dir.exists():
        return []
    files: list[Path] = []
    for log_dir in project_dir.rglob("logs"):
        if not log_dir.is_dir():
            continue
        index_file = log_dir / "llm_interactions.jsonl"
        if index_file.is_file():
            files.append(index_file)
        for subdir_name in ("prompts", "responses"):
            subdir = log_dir / subdir_name
            if subdir.is_dir():
                files.extend(path for path in subdir.rglob("*") if path.is_file())
    return files


def _file_sort_key(path: Path) -> tuple[float, str]:
    try:
        return (path.stat().st_mtime, str(path))
    except OSError:
        return (0.0, str(path))


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        print(f"[LogService] Failed to prune LLM log file {path}: {exc}")
        return False


def _cleanup_empty_llm_log_dirs(project_dir: Path) -> None:
    for subdir in sorted(project_dir.rglob("logs/*"), key=lambda item: len(item.parts), reverse=True):
        if subdir.name not in {"prompts", "responses"} or not subdir.is_dir():
            continue
        try:
            subdir.rmdir()
        except OSError:
            pass


def enforce_llm_log_retention(project_id: str, base_dir: Path) -> dict:
    project_dir = base_dir / "projects" / project_id
    deleted_files = 0
    deleted_bytes = 0

    def delete_file(path: Path) -> None:
        nonlocal deleted_files, deleted_bytes
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if _safe_unlink(path):
            deleted_files += 1
            deleted_bytes += size

    if _LLM_LOG_RETENTION_DAYS > 0:
        cutoff = datetime.datetime.now().timestamp() - (_LLM_LOG_RETENTION_DAYS * 86400)
        for path in _iter_project_llm_log_files(project_dir):
            try:
                if path.stat().st_mtime < cutoff:
                    delete_file(path)
            except OSError:
                continue

    files = sorted(_iter_project_llm_log_files(project_dir), key=_file_sort_key)
    if _MAX_LLM_LOG_FILES > 0 and len(files) > _MAX_LLM_LOG_FILES:
        overflow = len(files) - _MAX_LLM_LOG_FILES
        for path in files[:overflow]:
            delete_file(path)

    files = sorted(_iter_project_llm_log_files(project_dir), key=_file_sort_key)
    if _MAX_LLM_LOG_DIR_BYTES > 0:
        file_sizes: list[tuple[Path, int]] = []
        total_bytes = 0
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            file_sizes.append((path, size))
            total_bytes += size
        for path, size in file_sizes:
            if total_bytes <= _MAX_LLM_LOG_DIR_BYTES:
                break
            if _safe_unlink(path):
                deleted_files += 1
                deleted_bytes += size
                total_bytes -= size

    _cleanup_empty_llm_log_dirs(project_dir)
    return {
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "limits": {
            "max_files": _MAX_LLM_LOG_FILES,
            "max_dir_bytes": _MAX_LLM_LOG_DIR_BYTES,
            "retention_days": _LLM_LOG_RETENTION_DAYS,
        },
    }

def save_llm_interaction(
    project_id: str,
    version: str,
    base_dir: Path,
    node_id: str,
    system_prompt: str,
    user_prompt: str,
    response: dict | str | None,
    provider: str,
    model: str,
    status: str = "success",
    error: str | None = None,
    include_full_artifacts: bool = False,
    persist_payload_files: bool = True,
    metadata: dict | None = None,
):
    """
    ULTIMATE OPTIMIZATION WITH CHRONOLOGICAL FILENAMES:
    1. System Prompt -> prompts/{ts}_{node}_sys.txt
    2. User Prompt -> prompts/{ts}_{node}_user.txt
    3. LLM Response -> responses/{ts}_{node}_res.json
    4. JSONL -> Lightweight index with timestamped refs.
    """
    try:
        log_dir = _resolve_version_log_dir(project_id, version, base_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Generate Base ID for this interaction
        ts_id = _get_timestamp_id()
        file_prefix = f"{ts_id}_{node_id}"

        sys_ref = "none"
        user_ref = "none"
        res_ref = "none"
        res_summary = "no_response"
        payload_file_limits: dict[str, dict] = {}

        if persist_payload_files:
            prompt_dir = log_dir / "prompts"
            response_dir = log_dir / "responses"
            prompt_dir.mkdir(exist_ok=True)
            response_dir.mkdir(exist_ok=True)

            sys_ref = f"prompts/{file_prefix}_sys.txt"
            _, payload_file_limits["system"] = _write_limited_text(log_dir / sys_ref, _safe_text(system_prompt))

            user_ref = f"prompts/{file_prefix}_user.txt"
            _, payload_file_limits["user"] = _write_limited_text(log_dir / user_ref, _safe_text(user_prompt))

            if response:
                res_ref = f"responses/{file_prefix}_res.json"
                _, payload_file_limits["response"] = _write_limited_text(log_dir / res_ref, _safe_json_text(response))

        if response:
            if isinstance(response, dict):
                res_summary = {
                    "reasoning_preview": (response.get("reasoning") or "")[:200] + "...",
                    "artifacts_summary": {k: f"{len(str(v))} bytes" for k, v in response.get("artifacts", {}).items()}
                }
            else:
                res_summary = str(response)[:200] + "..."

        # 2. Write to JSONL (Chronological reference)
        log_file = log_dir / "llm_interactions.jsonl"
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "node_id": node_id,
            "status": status,
            "provider": provider,
            "model": model,
            "sizes": {
                "system_prompt_chars": len(system_prompt or ""),
                "user_prompt_chars": len(user_prompt or ""),
                "system_prompt_tokens_est": _estimate_tokens(system_prompt or ""),
                "user_prompt_tokens_est": _estimate_tokens(user_prompt or ""),
            },
            "refs": {
                "system": sys_ref,
                "user": user_ref,
                "response": res_ref
            },
            "preview": {
                "user": _safe_text(user_prompt)[:200] + "...",
                "response": redact_sensitive_payload(res_summary)
            },
            "error": error,
            "metadata": redact_sensitive_payload(metadata or {}),
            "payload_files_persisted": persist_payload_files,
            "payload_file_limits": payload_file_limits,
        }
        
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        enforce_llm_log_retention(project_id, base_dir)
            
    except Exception as e:
        print(f"[LogService] Error saving LLM interaction: {e}")

def get_run_log(project_id: str, version: str, base_dir: Path) -> list:
    """
    读取指定版本的持久化执行日志
    """
    log_file = _resolve_version_log_dir(project_id, version, base_dir) / "orchestrator_run.log"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            return [line.strip() for line in f.readlines()]
    return []
