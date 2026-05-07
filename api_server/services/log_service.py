import os
import json
import datetime
import hashlib
import re
from pathlib import Path

# =====================================================================
# 持久化日志存储功能
# =====================================================================

_RUN_LOG_TIMESTAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\]\s+")


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

def save_run_log(project_id: str, version: str, base_dir: Path, logs: list):
    """
    将执行日志持久化到项目的对应版本目录下
    """
    try:
        log_dir = base_dir / "projects" / project_id / version / "logs"
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
):
    """
    ULTIMATE OPTIMIZATION WITH CHRONOLOGICAL FILENAMES:
    1. System Prompt -> prompts/{ts}_{node}_sys.txt
    2. User Prompt -> prompts/{ts}_{node}_user.txt
    3. LLM Response -> responses/{ts}_{node}_res.json
    4. JSONL -> Lightweight index with timestamped refs.
    """
    try:
        log_dir = base_dir / "projects" / project_id / version / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Generate Base ID for this interaction
        ts_id = _get_timestamp_id()
        file_prefix = f"{ts_id}_{node_id}"

        sys_ref = "none"
        user_ref = "none"
        res_ref = "none"
        res_summary = "no_response"

        if persist_payload_files:
            prompt_dir = log_dir / "prompts"
            response_dir = log_dir / "responses"
            prompt_dir.mkdir(exist_ok=True)
            response_dir.mkdir(exist_ok=True)

            sys_ref = f"prompts/{file_prefix}_sys.txt"
            (log_dir / sys_ref).write_text(system_prompt, encoding="utf-8")

            user_ref = f"prompts/{file_prefix}_user.txt"
            (log_dir / user_ref).write_text(user_prompt, encoding="utf-8")

            if response:
                res_ref = f"responses/{file_prefix}_res.json"
                (log_dir / res_ref).write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")

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
                "user": user_prompt[:200] + "...",
                "response": res_summary
            },
            "error": error,
            "payload_files_persisted": persist_payload_files,
        }
        
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            
    except Exception as e:
        print(f"[LogService] Error saving LLM interaction: {e}")

def get_run_log(project_id: str, version: str, base_dir: Path) -> list:
    """
    读取指定版本的持久化执行日志
    """
    log_file = base_dir / "projects" / project_id / version / "logs" / "orchestrator_run.log"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            return [line.strip() for line in f.readlines()]
    return []
