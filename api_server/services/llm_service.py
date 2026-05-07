import os
import json
import time
import threading
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
    }

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

    for attempt in range(max_retries + 1):
        attempt_number = attempt + 1
        attempt_started_at = time.monotonic()
        try:
            print(
                f"[LLM Service] Attempt {attempt_number}/{max_retries + 1} starting "
                f"provider='{provider}' model='{model_name}' timeout={_format_timeout_seconds(timeout_seconds)} "
                f"expected_files={_summarize_expected_files(expected_files)}."
            )
            raw_data = _call_openai_raw(enhanced_system_prompt, user_prompt, llm_settings=llm_settings)
            
            # Log interaction if project info is provided
            if project_id and version and llm_interaction_logging_enabled:
                save_llm_interaction(
                    project_id=project_id,
                    version=version,
                    base_dir=BASE_DIR,
                    node_id=node_id or "unknown",
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=raw_data,
                    provider=provider,
                    model=model_name,
                    status="success",
                    include_full_artifacts=include_full_artifacts_in_log,
                    persist_payload_files=llm_full_payload_logging_enabled,
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
                    node_id=node_id or "unknown",
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=None,
                    provider=provider,
                    model=model_name,
                    status="error",
                    error=f"JSONDecodeError: {str(e)}",
                    persist_payload_files=llm_full_payload_logging_enabled,
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
                    node_id=node_id or "unknown",
                    system_prompt=enhanced_system_prompt,
                    user_prompt=user_prompt,
                    response=None,
                    provider=provider,
                    model=model_name,
                    status="error",
                    error=f"Exception: {str(e)}",
                    persist_payload_files=llm_full_payload_logging_enabled,
                )
            time.sleep(2)
            
    raise Exception(f"LLM generation failed after {max_retries} retries. Last error: {last_error}")

import re

def _clean_json_response(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    
    # Handle SSE 'data:' prefix if the whole string is prefixed
    if text.startswith("data:"):
        text = text[5:].strip()

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

def _parse_llm_response_to_dict(completion) -> dict:
    """
    Robustly extracts the JSON dictionary from an LLM completion object or string.
    Handles standard OpenAI objects, SSE-prefixed strings, and raw JSON strings.
    """
    import json
    
    # 1. Try standard access (OpenAI SDK Object)
    if hasattr(completion, "choices") and completion.choices:
        content = completion.choices[0].message.content
        if content:
            return json.loads(_clean_json_response(content))
        raise json.JSONDecodeError(
            f"LLM returned empty message.content. Completion preview: {_summarize_completion(completion)}",
            "",
            0,
        )
            
    # 2. Handle string or dict-like responses
    raw_text = str(completion).strip()
    if raw_text.startswith("data:"):
        raw_text = raw_text[5:].strip()

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
                        return json.loads(_clean_json_response(content))
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
    cleaned = _clean_json_response(raw_text)
    if not cleaned:
        raise json.JSONDecodeError(
            f"LLM response text was empty after cleanup. Raw preview: {_summarize_completion(raw_text)}",
            raw_text,
            0,
        )
    return json.loads(cleaned)

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
        system_prompt, user_prompt = _build_connectivity_probe_prompts()
        normalized_settings = _normalize_connectivity_llm_settings(llm_settings)
        res_dict = _call_openai_raw(system_prompt, user_prompt, llm_settings=normalized_settings)

        if isinstance(res_dict, dict):
            return {
                "success": True,
                "message": "Connected successfully to OpenAI-compatible API.",
            }
        return {"success": False, "message": f"Invalid response format: {type(res_dict).__name__}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

def _call_openai_raw(system_prompt: str, user_prompt: str, llm_settings: dict | None = None) -> dict:
    from openai import OpenAI
    api_key = _resolve_llm_setting(llm_settings, "openai_api_key", "OPENAI_API_KEY")
    base_url = _resolve_llm_setting(llm_settings, "openai_base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name = _resolve_llm_setting(llm_settings, "openai_model_name", "OPENAI_MODEL_NAME", "gpt-4o")
    headers = _resolve_llm_dict_setting(llm_settings, "openai_headers")
    timeout_seconds = _get_llm_request_timeout_seconds()
    
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
    if timeout_seconds > 0:
        client_kwargs["timeout"] = timeout_seconds

    client = OpenAI(**client_kwargs)
    _throttle_llm_request()
    started_at = time.monotonic()
    print(
        f"[LLM Service] OpenAI-compatible request starting model='{model_name}' "
        f"base_url='{_sanitize_base_url(base_url)}' timeout={_format_timeout_seconds(timeout_seconds)}."
    )
    completion = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"}
    )
    elapsed = time.monotonic() - started_at
    print(
        f"[LLM Service] OpenAI-compatible request completed model='{model_name}' "
        f"elapsed={elapsed:.2f}s."
    )
    
    # Use robust parsing for production design calls
    return _parse_llm_response_to_dict(completion)
