import os
import json
import time
import threading
import uuid
from contextvars import ContextVar
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from pathlib import Path

# Ensure .env file is loaded from project root
root_env = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=root_env)

def _resolve_llm_setting(llm_settings: dict | None, key: str, env_key: str, default: str = "") -> str:
    if llm_settings and llm_settings.get(key) not in (None, ""):
        return str(llm_settings.get(key))
    return os.getenv(env_key, default)

def _resolve_llm_dict_setting(llm_settings: dict | None, key: str) -> dict[str, str] | None:
    if not llm_settings:
        return None
    value = llm_settings.get(key)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return None

class SubagentOutput(BaseModel):
    reasoning: str = Field(description="LLM reasoning process and decision logic (Markdown format)")
    artifacts: dict[str, str] = Field(description="Generated files dictionary, key is filename, value is content.")

from services.log_service import save_llm_interaction
from services.db_service import metadata_db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
_LLM_CALL_THROTTLE_LOCK = threading.Lock()
_LAST_LLM_CALL_STARTED_AT = 0.0

current_job_id: ContextVar[str | None] = ContextVar("current_job_id", default=None)
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
current_node_type: ContextVar[str | None] = ContextVar("current_node_type", default=None)
current_streaming_enabled: ContextVar[bool | None] = ContextVar("current_streaming_enabled", default=None)
current_stream_callback: ContextVar["StreamCallback | None"] = ContextVar("current_stream_callback", default=None)

StreamCallback = Callable[[str, dict[str, Any]], None]


def _resolve_nonnegative_float_env(env_key: str, default: float = 0.0) -> float:
    raw_value = os.getenv(env_key)
    if raw_value in (None, ""):
        return default
    try:
        return max(0.0, float(raw_value))
    except (TypeError, ValueError):
        return default


def _get_llm_min_call_interval_seconds() -> float:
    return _resolve_nonnegative_float_env("LLM_MIN_CALL_INTERVAL_SECONDS", 0.0)


def _get_llm_request_timeout_seconds() -> float:
    return _resolve_nonnegative_float_env("LLM_REQUEST_TIMEOUT_SECONDS", 600.0)


def _get_llm_connectivity_timeout_seconds() -> float:
    return _resolve_nonnegative_float_env("LLM_CONNECTIVITY_TIMEOUT_SECONDS", 60.0)


def _get_llm_connectivity_max_tokens() -> int:
    raw_value = os.getenv("LLM_CONNECTIVITY_MAX_TOKENS")
    if raw_value in (None, ""):
        return 32
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 32


def _format_timeout_seconds(timeout_seconds: float) -> str:
    if timeout_seconds <= 0:
        return "disabled"
    return f"{timeout_seconds:g}s"


def _summarize_expected_files(expected_files: list[str], max_items: int = 3) -> str:
    if not expected_files:
        return "(none)"
    if len(expected_files) <= max_items:
        return ", ".join(expected_files)
    head = ", ".join(expected_files[:max_items])
    return f"{head}, ... (+{len(expected_files) - max_items} more)"


def _sanitize_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        netloc = parts.netloc.split("@", 1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    except Exception:
        return raw


def _throttle_llm_request() -> None:
    global _LAST_LLM_CALL_STARTED_AT

    min_interval = _get_llm_min_call_interval_seconds()
    if min_interval <= 0:
        return

    with _LLM_CALL_THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_LLM_CALL_STARTED_AT if _LAST_LLM_CALL_STARTED_AT > 0 else None
        remaining = min_interval - elapsed if elapsed is not None else 0.0
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
        _LAST_LLM_CALL_STARTED_AT = now


def _summarize_completion(completion, max_len: int = 300) -> str:
    try:
        text = str(completion)
    except Exception as exc:
        return f"<unprintable completion: {exc}>"
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text

def resolve_runtime_llm_settings(design_context: dict | None) -> dict | None:
    """
    Normalize a runtime-selected model config into llm_settings expected by
    generate_with_llm().
    """
    model_config = (design_context or {}).get("model_config") or {}
    provider = str(model_config.get("provider") or "").strip().lower()
    api_key = model_config.get("api_key")
    model_name = model_config.get("model_name")
    base_url = model_config.get("base_url")

    # Keep runtime model selection active even when the chosen config relies on
    # gateway headers or a local proxy instead of an explicit API key.
    if not provider or not model_name:
        return None

    return {
        "llm_provider": "openai",
        "openai_api_key": api_key,
        "openai_base_url": base_url,
        "openai_model_name": model_name,
        "openai_headers": model_config.get("headers"),
        "streaming_enabled": bool(model_config.get("streaming_enabled")),
        "provider_capabilities": model_config.get("provider_capabilities") or {},
    }


def _truthy_env(env_key: str, default: bool = False) -> bool:
    raw = os.getenv(env_key)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_streaming_enabled(llm_settings: dict | None, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    context_value = current_streaming_enabled.get()
    if context_value is not None:
        return bool(context_value)
    if llm_settings and "streaming_enabled" in llm_settings:
        return bool(llm_settings.get("streaming_enabled"))
    return _truthy_env("LLM_STREAMING_ENABLED", False)


def _provider_capabilities(llm_settings: dict | None) -> dict[str, Any]:
    value = (llm_settings or {}).get("provider_capabilities") or {}
    return value if isinstance(value, dict) else {}


def _stream_with_json_response_format_allowed(llm_settings: dict | None) -> bool:
    capabilities = _provider_capabilities(llm_settings)
    if capabilities.get("force_streaming_disabled"):
        return False
    if capabilities.get("supports_stream") is False:
        return False
    return bool(capabilities.get("supports_stream_with_json_response_format", True))


def _safe_stream_callback(callback: StreamCallback | None, delta: str, meta: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(delta, meta)
    except Exception as exc:
        print(f"[LLM Service] Stream callback ignored failure: {exc}")


def _extract_stream_delta(chunk: Any) -> str:
    try:
        choices = getattr(chunk, "choices", None)
        if choices:
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content:
                return str(content)
    except Exception:
        pass
    if isinstance(chunk, dict):
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            delta = first.get("delta") if isinstance(first, dict) else {}
            if isinstance(delta, dict) and delta.get("content"):
                return str(delta.get("content"))
    return ""

def generate_with_llm(
    system_prompt: str,
    user_prompt: str,
    expected_files: list[str],
    max_retries: int = 2,
    llm_settings: dict | None = None,
    project_id: str | None = None,
    version: str | None = None,
    node_id: str | None = None,
    include_full_artifacts_in_log: bool = False,
    job_id: str | None = None,
    run_id: str | None = None,
    node_type: str | None = None,
    call_purpose: str = "generation",
    call_id: str | None = None,
    stream_callback: StreamCallback | None = None,
    streaming_enabled: bool | None = None,
) -> SubagentOutput:
    """
    Generic LLM generator that enforces output containing reasoning log and specified file contents in JSON.
    """
    configured_provider = _resolve_llm_setting(llm_settings, "llm_provider", "LLM_PROVIDER", "openai").lower()
    provider = "openai"
    if configured_provider not in ("", "openai"):
        print("[LLM Service] Unsupported provider configured; using openai-compatible mode.")
    
    # Dynamically build output constraints
    file_schema_desc = "Generated artifact file contents. Must include the following keys: " + ", ".join(expected_files)
    
    enhanced_system_prompt = system_prompt + f"""
    
    [Mandatory Output Specification]
    You must output only a valid JSON string strictly conforming to the following Schema:
    {{
        "reasoning": "Your reasoning process...",
        "artifacts": {{
            "{expected_files[0]}": "Complete content of file 1...",
            ...
        }}
    }}
    Ensure the artifacts dictionary contains all required files with usable, compliant content. Do NOT output extra Markdown code block symbols (like ```json).
    """

    last_error = None
    model_name = ""
    debug_config = metadata_db.get_project_debug_config(project_id) if project_id else None
    llm_interaction_logging_enabled = bool((debug_config or {}).get("llm_interaction_logging_enabled"))
    llm_full_payload_logging_enabled = bool((debug_config or {}).get("llm_full_payload_logging_enabled"))
    model_name = _resolve_llm_setting(llm_settings, "openai_model_name", "OPENAI_MODEL_NAME", "gpt-4o")
    timeout_seconds = _get_llm_request_timeout_seconds()
    effective_node_id = node_id or node_type or current_node_type.get() or "unknown"
    effective_node_type = node_type or node_id or current_node_type.get() or "unknown"
    effective_job_id = job_id or current_job_id.get()
    effective_run_id = run_id or current_run_id.get() or effective_job_id
    effective_call_id = call_id or f"llm_call_{uuid.uuid4().hex}"
    effective_streaming_enabled = _resolve_streaming_enabled(llm_settings, streaming_enabled)
    effective_stream_callback = stream_callback or current_stream_callback.get()

    for attempt in range(max_retries + 1):
        attempt_number = attempt + 1
        attempt_started_at = time.monotonic()
        stream_meta = {
            "call_id": effective_call_id,
            "job_id": effective_job_id,
            "run_id": effective_run_id,
            "node_id": effective_node_id,
            "node_type": effective_node_type,
            "provider": provider,
            "model": model_name,
            "attempt": attempt_number,
            "call_purpose": call_purpose,
            "streaming_enabled": effective_streaming_enabled,
        }
        try:
            print(
                f"[LLM Service] Attempt {attempt_number}/{max_retries + 1} starting "
                f"provider='{provider}' model='{model_name}' timeout={_format_timeout_seconds(timeout_seconds)} "
                f"expected_files={_summarize_expected_files(expected_files)}."
            )
            raw_data = _call_openai_raw(
                enhanced_system_prompt,
                user_prompt,
                llm_settings=llm_settings,
                stream_enabled=effective_streaming_enabled,
                stream_callback=effective_stream_callback,
                stream_meta=stream_meta,
            )
            stream_log_metadata = {
                "streaming_enabled": effective_streaming_enabled,
                "streaming_used": bool(raw_data.pop("_streaming_used", False)) if isinstance(raw_data, dict) else False,
                "fallback_used": bool(raw_data.pop("_fallback_used", False)) if isinstance(raw_data, dict) else False,
                "chunk_count": int(raw_data.pop("_chunk_count", 0)) if isinstance(raw_data, dict) else 0,
                "final_parse_status": "success",
                "attempt_count": attempt_number,
            }
            
            # Log interaction if project info is provided
            if project_id and version and llm_interaction_logging_enabled:
                save_llm_interaction(
                    project_id=project_id,
                    version=version,
                    base_dir=BASE_DIR,
                    node_id=effective_node_id,
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=raw_data,
                    provider=provider,
                    model=model_name,
                    status="success",
                    include_full_artifacts=include_full_artifacts_in_log,
                    persist_payload_files=llm_full_payload_logging_enabled,
                    metadata=stream_log_metadata,
                )

            # --- Robust data repair logic ---
            # 1. Ensure artifacts is a dict
            artifacts = raw_data.get("artifacts", {})
            if not isinstance(artifacts, dict):
                artifacts = {}
            
            # 2. Fix nested dict issues
            fixed_artifacts = {}
            for k, v in artifacts.items():
                if isinstance(v, (dict, list)):
                    # If LLM outputs nested JSON, convert back to string
                    fixed_artifacts[k] = json.dumps(v, ensure_ascii=False, indent=2)
                else:
                    fixed_artifacts[k] = str(v)
            
            # 3. Fill missing expected files
            for f in expected_files:
                if f not in fixed_artifacts:
                    fixed_artifacts[f] = ""
            
            raw_data["artifacts"] = fixed_artifacts
            
            # 4. Provide default reasoning if missing
            if "reasoning" not in raw_data:
                raw_data["reasoning"] = "No reasoning provided by LLM."

            elapsed = time.monotonic() - attempt_started_at
            print(
                f"[LLM Service] Attempt {attempt_number}/{max_retries + 1} succeeded "
                f"provider='{provider}' model='{model_name}' elapsed={elapsed:.2f}s."
            )
            return SubagentOutput.model_validate(raw_data)

        except json.JSONDecodeError as e:
            last_error = e
            elapsed = time.monotonic() - attempt_started_at
            print(
                f"  [LLM Service] JSON parse failed (attempt {attempt_number}/{max_retries + 1}, "
                f"elapsed={elapsed:.2f}s): {e}"
            )
            if project_id and version and llm_interaction_logging_enabled:
                save_llm_interaction(
                    project_id=project_id,
                    version=version,
                    base_dir=BASE_DIR,
                    node_id=effective_node_id,
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=None,
                    provider=provider,
                    model=model_name,
                    status="error",
                    error=f"JSONDecodeError: {str(e)}",
                    persist_payload_files=llm_full_payload_logging_enabled,
                    metadata={
                        "streaming_enabled": effective_streaming_enabled,
                        "final_parse_status": "json_decode_error",
                        "attempt_count": attempt_number,
                    },
                )
            time.sleep(2)
        except Exception as e:
            last_error = e
            elapsed = time.monotonic() - attempt_started_at
            print(
                f"  [LLM Service] Data validation/call failed (attempt {attempt_number}/{max_retries + 1}, "
                f"elapsed={elapsed:.2f}s): {e}"
            )
            if project_id and version and llm_interaction_logging_enabled:
                save_llm_interaction(
                    project_id=project_id,
                    version=version,
                    base_dir=BASE_DIR,
                    node_id=effective_node_id,
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=None,
                    provider=provider,
                    model=model_name,
                    status="error",
                    error=f"Exception: {str(e)}",
                    persist_payload_files=llm_full_payload_logging_enabled,
                    metadata={
                        "streaming_enabled": effective_streaming_enabled,
                        "final_parse_status": "exception",
                        "attempt_count": attempt_number,
                    },
                )
            time.sleep(2)
            
    raise Exception(f"LLM generation failed after {max_retries} retries. Last error: {last_error}")

import re

def _clean_json_response(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()

    # Only remove ``` wrapper if it wraps the ENTIRE string
    if text.startswith("```"):
        match = re.match(r"^```(?:json)?\s+([\s\S]*?)\s*```$", text)
        if match:
            text = match.group(1)
        else:
            # Fallback for unclosed blocks
            match = re.match(r"^```(?:json)?\s+([\s\S]*)", text)
            if match:
                text = match.group(1)
                if text.endswith("```"):
                    text = text[:-3]
        
    return text.strip()

def _extract_sse_data_payloads(text: str) -> list[str]:
    payloads: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if payload and payload != "[DONE]":
                payloads.append(payload)
    return payloads

def _extract_openai_payload_content(payload: dict) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice, dict) else {}
        if isinstance(delta, dict) and delta.get("content") is not None:
            return str(delta.get("content"))
        message = choice.get("message") if isinstance(choice, dict) else {}
        if isinstance(message, dict) and message.get("content") is not None:
            return str(message.get("content"))
        if isinstance(choice, dict) and choice.get("text") is not None:
            return str(choice.get("text"))
    if payload.get("type") in {"response.output_text.delta", "response.output_text.done"}:
        value = payload.get("delta", payload.get("text"))
        if value is not None:
            return str(value)
    return None

def _decode_sse_response_text(text: str) -> str:
    payloads = _extract_sse_data_payloads(text)
    if not payloads:
        return text

    content_parts: list[str] = []
    direct_json_payloads: list[str] = []
    for payload in payloads:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            content_parts.append(payload)
            continue

        if isinstance(data, dict):
            content = _extract_openai_payload_content(data)
            if content is not None:
                content_parts.append(content)
            elif "choices" not in data:
                direct_json_payloads.append(json.dumps(data, ensure_ascii=False))

    if content_parts:
        return "".join(content_parts)
    if direct_json_payloads:
        return direct_json_payloads[-1]
    return "\n".join(payloads)

def _extract_first_json_object(text: str) -> dict | None:
    in_string = False
    escaped = False
    start_index: int | None = None
    depth = 0

    for index, char in enumerate(text):
        if start_index is None:
            if char == "{":
                start_index = index
                depth = 1
                in_string = False
                escaped = False
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start_index:index + 1]
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    start_index = None
                    continue
                if isinstance(data, dict):
                    return data
                start_index = None
    return None

def _loads_llm_json_dict(text: str) -> dict:
    normalized = _clean_json_response(_decode_sse_response_text(text))
    if not normalized:
        raise json.JSONDecodeError(
            f"LLM response text was empty after cleanup. Raw preview: {_summarize_completion(text)}",
            text,
            0,
        )
    try:
        data = json.loads(normalized)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    extracted = _extract_first_json_object(normalized)
    if extracted is not None:
        return extracted
    return json.loads(normalized)

def _parse_llm_response_to_dict(completion) -> dict:
    """
    Robustly extracts the JSON dictionary from an LLM completion object or string.
    Handles standard OpenAI objects, SSE-prefixed strings, and raw JSON strings.
    """
    # 1. Try standard access (OpenAI SDK Object)
    if hasattr(completion, "choices") and completion.choices:
        content = completion.choices[0].message.content
        if content:
            return _loads_llm_json_dict(content)
        raise json.JSONDecodeError(
            f"LLM returned empty message.content. Completion preview: {_summarize_completion(completion)}",
            "",
            0,
        )
            
    # 2. Handle string or dict-like responses
    raw_text = str(completion).strip()
    if not raw_text:
        raise json.JSONDecodeError("LLM returned an empty response body.", "", 0)
        
    # Try to parse the entire string as a JSON (it might be the full response object as a string)
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            # If it's the full response object, try to find the content in choices
            if "choices" in data and data["choices"]:
                choice = data["choices"][0]
                if isinstance(choice, dict):
                    msg = choice.get("message", {}) 
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if content:
                        return _loads_llm_json_dict(content)
                    raise json.JSONDecodeError(
                        f"LLM response JSON had empty choices[0].message.content. Response preview: {_summarize_completion(data)}",
                        raw_text,
                        0,
                    )
            # Maybe the whole thing is already the JSON we want (the model's direct output)
            return data
    except:
        pass
        
    # 3. Last resort: treat the raw text as the JSON content directly
    return _loads_llm_json_dict(raw_text)

def _build_connectivity_probe_prompts() -> tuple[str, str]:
    system_prompt = (
        "You are a connectivity probe. "
        "Return only a valid JSON object with keys 'ok' and 'message'."
    )
    user_prompt = json.dumps(
        {
            "task": "connectivity_check",
            "instruction": "Reply with JSON only.",
            "required_schema": {
                "ok": True,
                "message": "pong",
            },
        },
        ensure_ascii=False,
    )
    return system_prompt, user_prompt


def _normalize_connectivity_llm_settings(llm_settings: dict) -> dict:
    return {
        "llm_provider": "openai",
        "openai_api_key": llm_settings.get("api_key"),
        "openai_base_url": llm_settings.get("base_url", "https://api.openai.com/v1"),
        "openai_model_name": llm_settings.get("model_name", "gpt-4o"),
        "openai_headers": llm_settings.get("headers") or {},
    }

def test_llm_connectivity(llm_settings: dict) -> dict:
    """
    Test the connectivity and availability of an LLM configuration.
    Returns a dict with success status and message.
    """
    try:
        started_at = time.monotonic()
        system_prompt, user_prompt = _build_connectivity_probe_prompts()
        normalized_settings = _normalize_connectivity_llm_settings(llm_settings)
        timeout_seconds = _get_llm_connectivity_timeout_seconds()
        res_dict = _call_openai_raw(
            system_prompt,
            user_prompt,
            llm_settings=normalized_settings,
            timeout_seconds=timeout_seconds,
            max_tokens=_get_llm_connectivity_max_tokens(),
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)

        if isinstance(res_dict, dict):
            return {
                "success": True,
                "message": "Connected successfully to OpenAI-compatible API.",
                "error_type": None,
                "elapsed_ms": elapsed_ms,
                "timeout_seconds": timeout_seconds,
            }
        return {
            "success": False,
            "message": f"Invalid response format: {type(res_dict).__name__}",
            "error_type": "invalid_response",
            "elapsed_ms": elapsed_ms,
            "timeout_seconds": timeout_seconds,
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "error_type": _classify_llm_connectivity_error(e),
            "timeout_seconds": _get_llm_connectivity_timeout_seconds(),
        }

def _classify_llm_connectivity_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "llm_timeout"
    if "401" in text or "unauthorized" in text or "authentication" in text or "api key" in text:
        return "llm_auth_failed"
    if "404" in text or "model" in text and "not" in text:
        return "llm_invalid_model"
    return "connectivity_failed"


def _call_openai_raw(
    system_prompt: str,
    user_prompt: str,
    llm_settings: dict | None = None,
    timeout_seconds: float | None = None,
    max_tokens: int | None = None,
    stream_enabled: bool = False,
    stream_callback: StreamCallback | None = None,
    stream_meta: dict[str, Any] | None = None,
) -> dict:
    from openai import OpenAI
    api_key = _resolve_llm_setting(llm_settings, "openai_api_key", "OPENAI_API_KEY")
    base_url = _resolve_llm_setting(llm_settings, "openai_base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name = _resolve_llm_setting(llm_settings, "openai_model_name", "OPENAI_MODEL_NAME", "gpt-4o")
    headers = _resolve_llm_dict_setting(llm_settings, "openai_headers")
    effective_timeout_seconds = _get_llm_request_timeout_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
    
    # Use placeholder if key is missing to support local/no-auth gateways
    # Check if Auth header is already present
    has_auth_header = any(k.lower() == "authorization" for k in (headers or {}).keys())
    effective_api_key = api_key
    if not effective_api_key and not has_auth_header:
        effective_api_key = "not-required"

    client_kwargs = {
        "api_key": effective_api_key or "",
        "base_url": base_url,
        "default_headers": headers or None,
    }
    if effective_timeout_seconds > 0:
        client_kwargs["timeout"] = effective_timeout_seconds

    client = OpenAI(**client_kwargs)
    _throttle_llm_request()
    started_at = time.monotonic()
    print(
        f"[LLM Service] OpenAI-compatible request starting model='{model_name}' "
        f"base_url='{_sanitize_base_url(base_url)}' timeout={_format_timeout_seconds(effective_timeout_seconds)}."
    )
    completion_kwargs = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    if max_tokens is not None:
        completion_kwargs["max_tokens"] = max(1, int(max_tokens))

    if stream_enabled and _stream_with_json_response_format_allowed(llm_settings):
        try:
            completion_kwargs["stream"] = True
            stream = client.chat.completions.create(**completion_kwargs)
            full_text_parts: list[str] = []
            sequence = 0
            _safe_stream_callback(stream_callback, "", {**(stream_meta or {}), "event": "started", "sequence": 0})
            for chunk in stream:
                delta = _extract_stream_delta(chunk)
                if not delta:
                    continue
                sequence += 1
                full_text_parts.append(delta)
                _safe_stream_callback(
                    stream_callback,
                    delta,
                    {**(stream_meta or {}), "event": "delta", "sequence": sequence},
                )
            elapsed = time.monotonic() - started_at
            raw_text = "".join(full_text_parts)
            parsed = _parse_llm_response_to_dict(raw_text)
            parsed["_streaming_used"] = True
            parsed["_fallback_used"] = False
            parsed["_chunk_count"] = sequence
            _safe_stream_callback(
                stream_callback,
                "",
                {
                    **(stream_meta or {}),
                    "event": "completed",
                    "sequence": sequence,
                    "final_parse_status": "success",
                    "elapsed_ms": int(elapsed * 1000),
                },
            )
            print(
                f"[LLM Service] OpenAI-compatible stream completed model='{model_name}' "
                f"chunks={sequence} elapsed={elapsed:.2f}s."
            )
            return parsed
        except Exception as exc:
            _safe_stream_callback(
                stream_callback,
                "",
                {
                    **(stream_meta or {}),
                    "event": "failed",
                    "sequence": 0,
                    "error_message": str(exc),
                    "will_retry": True,
                },
            )
            print(f"[LLM Service] Streaming failed; falling back to non-streaming request: {exc}")
            completion_kwargs.pop("stream", None)

    completion = client.chat.completions.create(**completion_kwargs)
    elapsed = time.monotonic() - started_at
    print(
        f"[LLM Service] OpenAI-compatible request completed model='{model_name}' "
        f"elapsed={elapsed:.2f}s."
    )
    
    # Use robust parsing for production design calls
    parsed = _parse_llm_response_to_dict(completion)
    if stream_enabled:
        _safe_stream_callback(
            stream_callback,
            "",
            {
                **(stream_meta or {}),
                "event": "completed",
                "sequence": 0,
                "final_parse_status": "success",
                "fallback_used": True,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    parsed["_streaming_used"] = False
    parsed["_fallback_used"] = bool(stream_enabled)
    parsed["_chunk_count"] = 0
    return parsed
