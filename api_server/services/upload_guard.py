import asyncio
import json
import os
import uuid
import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import UploadFile


ALLOWED_BASELINE_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml"}
BLOCKED_INNER_EXTENSIONS = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
}
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 20
MAX_STRUCT_DEPTH = 20
MAX_STRUCT_NODES = 10000
MAX_STRING_LENGTH = 100000


class UploadValidationError(Exception):
    def __init__(self, status_code: int, detail: dict):
        super().__init__(detail.get("message") or "Upload validation failed.")
        self.status_code = status_code
        self.detail = detail


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _supported_formats_message() -> str:
    return "Supported baseline formats: .md, .txt, .json, .yaml, .yml."


def _safe_filename(raw_filename: str | None) -> str:
    filename = (raw_filename or "").strip()
    if not filename:
        raise UploadValidationError(400, {"message": "Uploaded file must have a filename."})
    if "\x00" in filename or "/" in filename or "\\" in filename:
        raise UploadValidationError(400, {"message": "Filename must not contain path separators."})
    safe_name = Path(filename).name
    if safe_name != filename or safe_name in {"", ".", ".."}:
        raise UploadValidationError(400, {"message": "Invalid upload filename."})
    return safe_name


def _validate_extension(filename: str) -> str:
    path = Path(filename)
    suffix = path.suffix.lower()
    if not suffix:
        raise UploadValidationError(415, {"message": f"Files without an extension are not supported. {_supported_formats_message()}"})
    if suffix not in ALLOWED_BASELINE_EXTENSIONS:
        raise UploadValidationError(415, {"message": f"File type '{suffix}' is not supported. {_supported_formats_message()}"})
    inner_suffixes = {item.lower() for item in path.suffixes[:-1]}
    blocked_inner = sorted(inner_suffixes.intersection(BLOCKED_INNER_EXTENSIONS))
    if blocked_inner:
        raise UploadValidationError(415, {"message": f"Double-extension upload is not allowed: {filename}."})
    return suffix


def _inspect_structure(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_STRUCT_NODES:
        raise UploadValidationError(413, {"message": "Structured file is too large: too many JSON/YAML nodes."})
    if depth > MAX_STRUCT_DEPTH:
        raise UploadValidationError(413, {"message": "Structured file is too deep."})
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise UploadValidationError(413, {"message": "Structured file contains an oversized string field."})
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _inspect_structure(key, depth=depth + 1, counter=counter)
            _inspect_structure(child, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for child in value:
            _inspect_structure(child, depth=depth + 1, counter=counter)


def _validate_content(filename: str, suffix: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadValidationError(415, {"message": f"File '{filename}' must be valid UTF-8 text."}) from exc

    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UploadValidationError(422, {"message": f"File '{filename}' is not valid JSON."}) from exc
        _inspect_structure(parsed)
        return "parsed"
    if suffix in {".yaml", ".yml"}:
        try:
            parsed = yaml.safe_load(text) if text.strip() else None
        except yaml.YAMLError as exc:
            raise UploadValidationError(422, {"message": f"File '{filename}' is not valid YAML."}) from exc
        _inspect_structure(parsed)
        return "parsed"
    return "text_only"


async def _read_limited_upload(file: UploadFile, *, max_file_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_file_bytes:
            raise UploadValidationError(413, {"message": f"File '{file.filename}' exceeds the single-file upload limit."})
        chunks.append(chunk)
    return b"".join(chunks)


def _merge_ingestion_status(baseline_dir: Path, statuses: list[dict]) -> None:
    requirements_path = baseline_dir / "requirements.json"
    payload: dict[str, Any] = {}
    if requirements_path.exists():
        try:
            payload = json.loads(requirements_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    existing = {
        item.get("name"): item
        for item in payload.get("file_ingestion_status", [])
        if isinstance(item, dict) and item.get("name")
    }
    for status in statuses:
        existing[status["name"]] = status
    payload["file_ingestion_status"] = sorted(existing.values(), key=lambda item: item["name"])
    requirements_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_event_filename(value: Any) -> str:
    filename = str(value or "").strip()
    if not filename:
        return "<unnamed>"
    return filename.replace("\\", "_").replace("/", "_")[:200]


def record_baseline_upload_event(
    baseline_dir: Path,
    event_type: str,
    *,
    files: list[UploadFile] | None = None,
    detail: dict | None = None,
) -> None:
    try:
        baseline_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "files": [
                {"filename": _safe_event_filename(getattr(file, "filename", ""))}
                for file in (files or [])
            ],
            "detail": detail or {},
        }
        with (baseline_dir / "upload_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[UploadGuard] Failed to record upload event: {exc}")


def _atomic_write_bytes(target_path: Path, data: bytes) -> None:
    temp_path = target_path.parent / f".{target_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp_path.write_bytes(data)
        os.replace(temp_path, target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


async def save_baseline_uploads(baseline_dir: Path, files: list[UploadFile]) -> dict:
    max_files = _env_int("BASELINE_UPLOAD_MAX_FILES", DEFAULT_MAX_FILES)
    max_file_bytes = _env_int("BASELINE_UPLOAD_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)
    max_total_bytes = _env_int("BASELINE_UPLOAD_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES)
    if len(files) > max_files:
        raise UploadValidationError(413, {"message": f"Too many files. Maximum upload count is {max_files}."})

    baseline_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    saved_files: list[str] = []
    ingestion_status: list[dict] = []

    for file in files:
        filename = _safe_filename(file.filename)
        suffix = _validate_extension(filename)
        target_path = baseline_dir / filename
        if target_path.exists():
            raise UploadValidationError(409, {"message": f"File '{filename}' already exists. Rename it before uploading."})

        data = await _read_limited_upload(file, max_file_bytes=max_file_bytes)
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise UploadValidationError(413, {"message": "Total upload size exceeds the configured limit."})

        status = _validate_content(filename, suffix, data)
        await asyncio.to_thread(_atomic_write_bytes, target_path, data)

        saved_files.append(filename)
        ingestion_status.append(
            {
                "name": filename,
                "status": status,
                "size_bytes": len(data),
                "extension": suffix,
            }
        )

    _merge_ingestion_status(baseline_dir, ingestion_status)
    return {"status": "success", "files": saved_files, "file_ingestion_status": ingestion_status}
