from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from .standards import (
    normalize_path_text,
    resolve_directory_reference,
    resolve_path_within_root,
    resolve_root_dir,
)

if TYPE_CHECKING:
    from registry.agent_registry import AgentFullConfig


TOOL_ERROR_OK = "OK"
TOOL_ERROR_INVALID_INPUT = "INVALID_INPUT"
TOOL_ERROR_PATH_INVALID = "PATH_INVALID"
TOOL_ERROR_PATH_NOT_FOUND = "PATH_NOT_FOUND"
TOOL_ERROR_UNSUPPORTED = "UNSUPPORTED_TOOL"
TOOL_ERROR_INTERNAL = "INTERNAL_ERROR"
TOOL_ERROR_NOT_ALLOWED = "NOT_ALLOWED"
TOOL_ERROR_EXECUTION_FAILED = "EXECUTION_FAILED"
TOOL_ERROR_TIMEOUT = "TIMEOUT"

_MISSING = object()


@dataclass(frozen=True)
class RetryPolicy:
    max_retry_attempts: int = 0


@dataclass(frozen=True)
class ToolParamSpec:
    kind: str
    required: bool = False
    default: Any = _MISSING
    aliases: tuple[str, ...] = ()
    allowed_values: tuple[str, ...] = ()
    path_mode: str = "normalize_only"
    expected_kind: str = "any"
    must_exist: bool = False
    allow_scalar_list: bool = False
    preserve_whitespace: bool = False


@dataclass(frozen=True)
class ToolSchema:
    parameters: dict[str, ToolParamSpec]
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


class ToolExecutionError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        category: str,
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[list[str]] = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.category = category
        self.context = context or {}
        self.suggestions = suggestions or []
        self.retryable = retryable

    def to_payload(self, *, attempts_used: int, max_retry_attempts: int) -> Dict[str, Any]:
        return {
            "code": self.error_code,
            "category": self.category,
            "message": str(self),
            "context": self.context,
            "suggestions": self.suggestions,
            "retryable": self.retryable,
            "retry": {
                "attempts_used": attempts_used,
                "max_retry_attempts": max_retry_attempts,
            },
        }


class ToolInputError(ToolExecutionError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        category: str = "input_validation",
        context: Optional[Dict[str, Any]] = None,
        suggestions: Optional[list[str]] = None,
        retryable: bool = False,
    ):
        super().__init__(
            error_code,
            message,
            category=category,
            context=context,
            suggestions=suggestions,
            retryable=retryable,
        )


def execute_tool(tool_name: str, tool_input: Dict[str, Any] | None) -> Dict[str, Any]:
    started = time.perf_counter()
    raw_input = dict(tool_input or {})
    attempts: list[Dict[str, Any]] = []
    normalization = {"applied": [], "dropped_parameters": [], "warnings": []}
    prepared_input: Dict[str, Any] = dict(raw_input)
    max_retry_attempts = 0

    try:
        handler = _TOOL_REGISTRY.get(tool_name)
        schema = _TOOL_SCHEMAS.get(tool_name)
        if handler is None or schema is None:
            raise ToolInputError(
                TOOL_ERROR_UNSUPPORTED,
                f"Unsupported tool: {tool_name}",
                category="tool_registry",
                context={"tool_name": tool_name},
                suggestions=["Use one of the registered built-in tools."],
            )

        prepared_input, normalization = _prepare_tool_input(tool_name, raw_input, schema)
        max_retry_attempts = schema.retry_policy.max_retry_attempts
        current_input = dict(prepared_input)
        attempt_number = 0

        while True:
            attempt_number += 1
            try:
                output = handler(current_input)
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "status": "success",
                        "input": dict(current_input),
                    }
                )
                return _success_result(
                    tool_name,
                    current_input,
                    output,
                    normalization,
                    attempts,
                    started,
                )
            except Exception as exc:  # noqa: BLE001
                error = _coerce_tool_error(tool_name, current_input, exc)
                attempt_record = {
                    "attempt": attempt_number,
                    "status": "error",
                    "input": dict(current_input),
                    "error_code": error.error_code,
                    "message": str(error),
                }
                retry_plan = _build_retry_plan(tool_name, current_input, error, attempt_number, max_retry_attempts)
                if retry_plan:
                    attempt_record["retry"] = {
                        "reason": retry_plan["reason"],
                        "adjustments": retry_plan["adjustments"],
                    }
                attempts.append(attempt_record)

                if retry_plan is None:
                    return _error_result(
                        tool_name,
                        current_input,
                        normalization,
                        attempts,
                        error,
                        started,
                        max_retry_attempts,
                    )

                normalization["applied"].extend(retry_plan["adjustments"])
                current_input = retry_plan["tool_input"]

    except ToolExecutionError as exc:
        attempts.append(
            {
                "attempt": 1,
                "status": "error",
                "phase": "preflight",
                "input": dict(prepared_input),
                "error_code": exc.error_code,
                "message": str(exc),
            }
        )
        return _error_result(
            tool_name,
            prepared_input,
            normalization,
            attempts,
            exc,
            started,
            max_retry_attempts,
        )


def execute_tool_with_permission(
    tool_name: str,
    tool_input: Dict[str, Any] | None,
    agent_config: Optional["AgentFullConfig"] = None,
    agent_capability: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a tool with permission checks based on agent configuration.
    """
    config = agent_config
    if config is None and agent_capability:
        try:
            from registry.agent_registry import AgentRegistry

            registry = AgentRegistry.get_instance()
            config = registry.load_full_config(agent_capability)
        except RuntimeError:
            config = None

    if config is not None and not config.has_tool_permission(tool_name):
        error = ToolInputError(
            TOOL_ERROR_NOT_ALLOWED,
            f"Tool '{tool_name}' is not allowed for agent '{config.manifest.capability}'.",
            category="permission",
            context={
                "tool_name": tool_name,
                "agent_capability": config.manifest.capability,
                "allowed_tools": config.effective_tools,
                "explicit_tools": config.tools_allowed or [],
            },
            suggestions=["Choose a permitted tool or update the agent tool allowlist."],
        )
        return _error_result(
            tool_name,
            dict(tool_input or {}),
            {"applied": [], "dropped_parameters": [], "warnings": []},
            [
                {
                    "attempt": 1,
                    "status": "error",
                    "phase": "permission_check",
                    "input": dict(tool_input or {}),
                    "error_code": error.error_code,
                    "message": str(error),
                }
            ],
            error,
            time.perf_counter(),
            0,
        )

    return execute_tool(tool_name, tool_input)


def _success_result(
    tool_name: str,
    tool_input: Dict[str, Any],
    output: Dict[str, Any],
    normalization: Dict[str, Any],
    attempts: list[Dict[str, Any]],
    started: float,
) -> Dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": "success",
        "error_code": TOOL_ERROR_OK,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "input": tool_input,
        "normalization": normalization,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "output": output,
    }


def _error_result(
    tool_name: str,
    tool_input: Dict[str, Any],
    normalization: Dict[str, Any],
    attempts: list[Dict[str, Any]],
    error: ToolExecutionError,
    started: float,
    max_retry_attempts: int,
) -> Dict[str, Any]:
    error_payload = error.to_payload(
        attempts_used=max(0, len(attempts) - 1),
        max_retry_attempts=max_retry_attempts,
    )
    return {
        "tool_name": tool_name,
        "status": "error",
        "error_code": error.error_code,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "input": tool_input,
        "normalization": normalization,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "output": {
            "message": str(error),
            "error": error_payload,
        },
    }


def _prepare_tool_input(
    tool_name: str,
    raw_input: Dict[str, Any],
    schema: ToolSchema,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    working_input = dict(raw_input)
    normalization = {"applied": [], "dropped_parameters": [], "warnings": []}

    _apply_aliases(tool_name, working_input, schema, normalization)
    _apply_legacy_normalizers(tool_name, working_input, normalization)

    missing_parameters = [
        name
        for name, spec in schema.parameters.items()
        if spec.required and working_input.get(name, _MISSING) is _MISSING
    ]
    if missing_parameters:
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"Missing required parameters: {', '.join(missing_parameters)}",
            context={
                "tool_name": tool_name,
                "missing_parameters": missing_parameters,
                "provided_parameters": sorted(working_input.keys()),
            },
            suggestions=[f"Provide `{name}` using the canonical parameter name." for name in missing_parameters],
        )

    normalized_input: Dict[str, Any] = {}
    root_dir: Path | None = None

    if "root_dir" in schema.parameters:
        root_raw = working_input.pop("root_dir", _MISSING)
        root_spec = schema.parameters["root_dir"]
        normalized_root = _normalize_parameter_value(
            tool_name,
            "root_dir",
            root_spec,
            root_raw,
            None,
            normalization,
        )
        normalized_input["root_dir"] = normalized_root
        root_dir = Path(normalized_root)

    for param_name, spec in schema.parameters.items():
        if param_name == "root_dir":
            continue

        raw_value = working_input.pop(param_name, _MISSING)
        if raw_value is _MISSING:
            if spec.default is _MISSING:
                continue
            raw_value = spec.default

        normalized_input[param_name] = _normalize_parameter_value(
            tool_name,
            param_name,
            spec,
            raw_value,
            root_dir,
            normalization,
        )

    if working_input:
        normalization["dropped_parameters"] = sorted(working_input.keys())
        normalization["warnings"].append(
            f"Dropped unsupported parameters: {', '.join(sorted(working_input.keys()))}"
        )

    return normalized_input, normalization


def _apply_aliases(
    tool_name: str,
    working_input: Dict[str, Any],
    schema: ToolSchema,
    normalization: Dict[str, Any],
) -> None:
    for canonical_name, spec in schema.parameters.items():
        if canonical_name in working_input:
            continue
        for alias in spec.aliases:
            if alias in working_input:
                working_input[canonical_name] = working_input.pop(alias)
                normalization["applied"].append(
                    f"Mapped legacy parameter `{alias}` to canonical parameter `{canonical_name}`."
                )
                break


def _apply_legacy_normalizers(
    tool_name: str,
    working_input: Dict[str, Any],
    normalization: Dict[str, Any],
) -> None:
    if tool_name in {"extract_structure", "extract_lookup_values"}:
        if "files" not in working_input and isinstance(working_input.get("path"), str):
            working_input["files"] = [working_input.pop("path")]
            normalization["applied"].append(
                "Expanded legacy single `path` input into canonical `files` list."
            )

    if tool_name == "patch_file" and "patches" in working_input and (
        "old_content" not in working_input or "new_content" not in working_input
    ):
        patches = working_input.get("patches")
        if isinstance(patches, list) and len(patches) == 1 and isinstance(patches[0], dict):
            patch = patches[0]
            old_content = patch.get("old_content")
            new_content = patch.get("new_content")
            if isinstance(old_content, str) and isinstance(new_content, str):
                working_input["old_content"] = old_content
                working_input["new_content"] = new_content
                normalization["applied"].append(
                    "Converted single-entry legacy `patches` payload into `old_content`/`new_content`."
                )


def _normalize_parameter_value(
    tool_name: str,
    param_name: str,
    spec: ToolParamSpec,
    value: Any,
    root_dir: Path | None,
    normalization: Dict[str, Any],
) -> Any:
    if spec.kind == "string":
        return _normalize_string_param(tool_name, param_name, value, spec)
    if spec.kind == "int":
        return _normalize_int_param(param_name, value, normalization)
    if spec.kind == "float":
        return _normalize_float_param(param_name, value, normalization)
    if spec.kind == "json":
        return _normalize_json_param(param_name, value, spec)
    if spec.kind == "path":
        return _normalize_path_param(param_name, value, spec, root_dir)
    if spec.kind == "path_list":
        return _normalize_path_list_param(param_name, value, spec, root_dir, normalization)
    if spec.kind == "dir_list":
        return _normalize_dir_list_param(param_name, value, root_dir, normalization)
    if spec.kind == "command":
        return _normalize_command_param(value, normalization)
    raise ToolInputError(
        TOOL_ERROR_INTERNAL,
        f"Unsupported parameter normalizer '{spec.kind}' for `{tool_name}.{param_name}`.",
        category="tool_protocol",
        context={"tool_name": tool_name, "parameter": param_name},
    )


def _normalize_string_param(
    tool_name: str,
    param_name: str,
    value: Any,
    spec: ToolParamSpec,
) -> str:
    if not isinstance(value, str):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a string.",
            context={"tool_name": tool_name, "parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a string value."],
        )

    normalized_value = value if spec.preserve_whitespace else value.strip()
    if not normalized_value and spec.required:
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a non-empty string.",
            context={"tool_name": tool_name, "parameter": param_name},
            suggestions=[f"Provide a non-empty value for `{param_name}`."],
        )

    if spec.allowed_values and normalized_value not in spec.allowed_values:
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be one of: {', '.join(spec.allowed_values)}",
            context={
                "tool_name": tool_name,
                "parameter": param_name,
                "allowed_values": list(spec.allowed_values),
                "received_value": normalized_value,
            },
            suggestions=[f"Choose a supported `{param_name}` value."],
        )

    return normalized_value


def _normalize_int_param(param_name: str, value: Any, normalization: Dict[str, Any]) -> int:
    if isinstance(value, bool):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be an integer.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as an integer value."],
        )

    if isinstance(value, str):
        stripped = value.strip()
        try:
            value = int(stripped)
        except ValueError as exc:
            raise ToolInputError(
                TOOL_ERROR_INVALID_INPUT,
                f"`{param_name}` must be an integer.",
                context={"parameter": param_name, "received_value": value},
                suggestions=[f"Use digits only when setting `{param_name}`."],
            ) from exc
        normalization["applied"].append(f"Coerced string parameter `{param_name}` to integer.")

    if not isinstance(value, int):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be an integer.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as an integer value."],
        )
    return value


def _normalize_float_param(param_name: str, value: Any, normalization: Dict[str, Any]) -> float:
    if isinstance(value, bool):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a float.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a float value."],
        )

    if isinstance(value, str):
        stripped = value.strip()
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ToolInputError(
                TOOL_ERROR_INVALID_INPUT,
                f"`{param_name}` must be a float.",
                context={"parameter": param_name, "received_value": value},
                suggestions=[f"Use a numeric value when setting `{param_name}`."],
            ) from exc
        normalization["applied"].append(f"Coerced string parameter `{param_name}` to float.")

    if isinstance(value, int):
        value = float(value)

    if not isinstance(value, float):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a float.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a float value."],
        )
    return value


def _normalize_json_param(param_name: str, value: Any, spec: ToolParamSpec) -> Any:
    if spec.expected_kind == "list" and not isinstance(value, list):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a list.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a JSON array."],
        )
    if spec.expected_kind == "dict" and not isinstance(value, dict):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be an object.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a JSON object."],
        )
    return value


def _normalize_path_param(
    param_name: str,
    value: Any,
    spec: ToolParamSpec,
    root_dir: Path | None,
) -> str:
    if not isinstance(value, str):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a string path.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a path string."],
        )

    if spec.path_mode == "root_dir":
        root_dir = resolve_root_dir(value)
        return str(root_dir)
    if spec.path_mode == "within_root":
        if root_dir is None:
            raise ToolInputError(
                TOOL_ERROR_INTERNAL,
                "`root_dir` must be normalized before path validation.",
                category="tool_protocol",
                context={"parameter": param_name},
            )
        _, normalized_path = resolve_path_within_root(
            root_dir,
            value,
            must_exist=spec.must_exist,
            expected_kind=spec.expected_kind,
        )
        return normalized_path
    return normalize_path_text(value)


def _normalize_path_list_param(
    param_name: str,
    value: Any,
    spec: ToolParamSpec,
    root_dir: Path | None,
    normalization: Dict[str, Any],
) -> list[str]:
    if isinstance(value, str) and spec.allow_scalar_list:
        value = [value]
        normalization["applied"].append(f"Wrapped scalar parameter `{param_name}` into a list.")

    if not isinstance(value, list):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a list of paths.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as an array of path strings."],
        )

    normalized_items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ToolInputError(
                TOOL_ERROR_INVALID_INPUT,
                f"`{param_name}[{index}]` must be a non-empty string path.",
                context={"parameter": param_name, "index": index},
                suggestions=[f"Ensure `{param_name}` contains only non-empty path strings."],
            )

        if spec.path_mode == "within_root":
            if root_dir is None:
                raise ToolInputError(
                    TOOL_ERROR_INTERNAL,
                    "`root_dir` must be normalized before path validation.",
                    category="tool_protocol",
                    context={"parameter": param_name},
                )
            _, normalized_item = resolve_path_within_root(
                root_dir,
                item,
                must_exist=spec.must_exist,
                expected_kind=spec.expected_kind,
            )
            normalized_items.append(normalized_item)
        else:
            normalized_items.append(normalize_path_text(item))

    return normalized_items


def _normalize_dir_list_param(
    param_name: str,
    value: Any,
    root_dir: Path | None,
    normalization: Dict[str, Any],
) -> list[str]:
    if isinstance(value, str):
        value = [value]
        normalization["applied"].append(f"Wrapped scalar parameter `{param_name}` into a list.")

    if not isinstance(value, list):
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            f"`{param_name}` must be a string or a list of directory paths.",
            context={"parameter": param_name, "received_type": type(value).__name__},
            suggestions=[f"Pass `{param_name}` as a directory path or an array of directory paths."],
        )

    if root_dir is None:
        raise ToolInputError(
            TOOL_ERROR_INTERNAL,
            "`root_dir` must be normalized before directory validation.",
            category="tool_protocol",
            context={"parameter": param_name},
        )

    normalized_items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ToolInputError(
                TOOL_ERROR_INVALID_INPUT,
                f"`{param_name}[{index}]` must be a non-empty string path.",
                context={"parameter": param_name, "index": index},
                suggestions=[f"Ensure `{param_name}` contains only non-empty directory paths."],
            )
        _, display_path = resolve_directory_reference(root_dir, item, must_exist=True)
        normalized_items.append(display_path)
    return normalized_items


def _normalize_command_param(value: Any, normalization: Dict[str, Any]) -> list[str]:
    if isinstance(value, str):
        split_command = shlex.split(value, posix=os.name != "nt")
        if not split_command:
            raise ToolInputError(
                TOOL_ERROR_INVALID_INPUT,
                "`command` must not be empty.",
                context={"parameter": "command"},
                suggestions=["Provide a command string or a command array."],
            )
        normalization["applied"].append("Split string `command` into canonical command list.")
        value = split_command
    elif isinstance(value, tuple):
        normalization["applied"].append("Converted tuple `command` into canonical command list.")
        value = list(value)

    if not isinstance(value, list) or not value:
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            "`command` must be a non-empty list of strings.",
            context={"parameter": "command", "received_type": type(value).__name__},
            suggestions=["Provide `command` as a list like `[\"python\", \"script.py\"]`."],
        )

    normalized_command = [str(item).strip() for item in value if str(item).strip()]
    if not normalized_command:
        raise ToolInputError(
            TOOL_ERROR_INVALID_INPUT,
            "`command` must contain at least one non-empty segment.",
            context={"parameter": "command"},
            suggestions=["Remove empty command segments before retrying."],
        )
    return normalized_command


def _coerce_tool_error(tool_name: str, tool_input: Dict[str, Any], exc: Exception) -> ToolExecutionError:
    if isinstance(exc, ToolExecutionError):
        return exc

    message = str(exc)
    lower_message = message.lower()
    context = {
        "tool_name": tool_name,
        "tool_input": tool_input,
    }

    if isinstance(exc, FileNotFoundError):
        return ToolExecutionError(
            TOOL_ERROR_PATH_NOT_FOUND,
            message,
            category="path_validation",
            context=context,
            suggestions=_build_suggestions(tool_name, TOOL_ERROR_PATH_NOT_FOUND),
        )
    if isinstance(exc, ValueError):
        error_code = TOOL_ERROR_PATH_INVALID if _looks_like_path_error(lower_message) else TOOL_ERROR_INVALID_INPUT
        category = "path_validation" if error_code == TOOL_ERROR_PATH_INVALID else "input_validation"
        return ToolExecutionError(
            error_code,
            message,
            category=category,
            context=context,
            suggestions=_build_suggestions(tool_name, error_code),
        )
    if isinstance(exc, RuntimeError):
        error_code = TOOL_ERROR_TIMEOUT if "timed out" in lower_message else TOOL_ERROR_EXECUTION_FAILED
        retryable = error_code == TOOL_ERROR_TIMEOUT or _looks_transient_runtime_error(lower_message)
        return ToolExecutionError(
            error_code,
            message,
            category="execution",
            context=context,
            suggestions=_build_suggestions(tool_name, error_code),
            retryable=retryable,
        )

    return ToolExecutionError(
        TOOL_ERROR_INTERNAL,
        message,
        category="system",
        context=context,
        suggestions=_build_suggestions(tool_name, TOOL_ERROR_INTERNAL),
    )


def _looks_like_path_error(message: str) -> bool:
    return any(token in message for token in ("path", "directory", "search_root", "escapes root", "outside of root"))


def _looks_transient_runtime_error(message: str) -> bool:
    transient_tokens = (
        "temporarily unavailable",
        "connection reset",
        "timeout",
        "timed out",
        "busy",
        "resource temporarily unavailable",
    )
    return any(token in message for token in transient_tokens)


def _build_suggestions(tool_name: str, error_code: str) -> list[str]:
    suggestions_map = {
        TOOL_ERROR_INVALID_INPUT: [
            "Check the tool's canonical parameter names and required fields.",
            "Verify parameter types before retrying.",
        ],
        TOOL_ERROR_PATH_INVALID: [
            "Use a relative path under `root_dir` or an absolute path that resolves inside it.",
            "You can use either Windows or Linux separators; the protocol will normalize them.",
        ],
        TOOL_ERROR_PATH_NOT_FOUND: [
            "Verify the target path exists before retrying.",
            "Confirm the file or directory is located under the selected `root_dir` or `search_root`.",
        ],
        TOOL_ERROR_TIMEOUT: [
            "Retry with a larger `timeout` value if the command legitimately needs more time.",
            "Inspect partial command output to confirm the process is making progress.",
        ],
        TOOL_ERROR_EXECUTION_FAILED: [
            "Review stderr or the surrounding tool context to identify the failing dependency.",
            "Retry after correcting environment or credential issues if the failure is transient.",
        ],
        TOOL_ERROR_NOT_ALLOWED: [
            "Choose a tool that is already allowlisted for the current agent.",
            "If needed, update the agent configuration to permit this tool.",
        ],
        TOOL_ERROR_INTERNAL: [
            "Review the tool implementation or server logs for the unexpected failure.",
            "Retry only after confirming the environment is healthy.",
        ],
        TOOL_ERROR_UNSUPPORTED: [
            "Select one of the registered built-in tools.",
        ],
    }
    return suggestions_map.get(error_code, [f"Review the `{tool_name}` tool input and retry."])


def _build_retry_plan(
    tool_name: str,
    current_input: Dict[str, Any],
    error: ToolExecutionError,
    attempt_number: int,
    max_retry_attempts: int,
) -> Optional[Dict[str, Any]]:
    if attempt_number > max_retry_attempts or not error.retryable:
        return None

    if tool_name == "run_command" and error.error_code == TOOL_ERROR_TIMEOUT:
        current_timeout = int(current_input.get("timeout", 30) or 30)
        next_timeout = min(max(current_timeout * 2, current_timeout + 1), 120)
        if next_timeout == current_timeout:
            return None
        retry_input = dict(current_input)
        retry_input["timeout"] = next_timeout
        return {
            "tool_input": retry_input,
            "reason": "Command timeout detected during execution analysis.",
            "adjustments": [
                f"Increased `timeout` from {current_timeout} to {next_timeout} seconds before retrying."
            ],
        }

    if error.error_code == TOOL_ERROR_EXECUTION_FAILED and _looks_transient_runtime_error(str(error).lower()):
        return {
            "tool_input": dict(current_input),
            "reason": "Transient execution failure detected.",
            "adjustments": ["Retried once without changing parameters after transient failure analysis."],
        }

    return None


def _require_root_dir(tool_input: Dict[str, Any]) -> Path:
    raw_root = tool_input.get("root_dir")
    return resolve_root_dir(raw_root)


def _build_wrapped_error(tool_name: str, tool_input: Dict[str, Any], exc: Exception) -> ToolExecutionError:
    return _coerce_tool_error(tool_name, tool_input, exc)


def _run_list_files(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .list_files import list_files

        return list_files(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("list_files", tool_input, exc) from exc


def _run_clone_repository(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .clone_repository import clone_repository

        return clone_repository(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise _build_wrapped_error("clone_repository", tool_input, exc) from exc


def _run_extract_structure(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .extract_structure import extract_structure

        return extract_structure(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("extract_structure", tool_input, exc) from exc


def _run_grep_search(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .grep_search import grep_search

        return grep_search(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("grep_search", tool_input, exc) from exc


def _run_read_file_chunk(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .read_file_chunk import read_file_chunk

        return read_file_chunk(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("read_file_chunk", tool_input, exc) from exc


def _run_extract_lookup_values(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .extract_lookup_values import extract_lookup_values

        return extract_lookup_values(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("extract_lookup_values", tool_input, exc) from exc


def _run_query_database(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .query_database import query_database

        return query_database(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise _build_wrapped_error("query_database", tool_input, exc) from exc


def _run_query_knowledge_base(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .query_knowledge_base import query_knowledge_base

        return query_knowledge_base(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise _build_wrapped_error("query_knowledge_base", tool_input, exc) from exc


def _run_write_file(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .write_file import write_file

        return write_file(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("write_file", tool_input, exc) from exc


def _run_append_file(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .append_file import append_file

        return append_file(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("append_file", tool_input, exc) from exc


def _run_upsert_markdown_sections(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .upsert_markdown_sections import upsert_markdown_sections

        return upsert_markdown_sections(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("upsert_markdown_sections", tool_input, exc) from exc


def _run_patch_file(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .patch_file import patch_file

        return patch_file(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError) as exc:
        raise _build_wrapped_error("patch_file", tool_input, exc) from exc


def _run_run_command(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .run_command import run_command

        return run_command(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise _build_wrapped_error("run_command", tool_input, exc) from exc


def _run_validate_artifacts(tool_input: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from .validate_artifacts import validate_artifacts

        return validate_artifacts(_require_root_dir(tool_input), tool_input)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise _build_wrapped_error("validate_artifacts", tool_input, exc) from exc


_TOOL_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "list_files": _run_list_files,
    "clone_repository": _run_clone_repository,
    "extract_structure": _run_extract_structure,
    "grep_search": _run_grep_search,
    "read_file_chunk": _run_read_file_chunk,
    "extract_lookup_values": _run_extract_lookup_values,
    "query_database": _run_query_database,
    "query_knowledge_base": _run_query_knowledge_base,
    "write_file": _run_write_file,
    "append_file": _run_append_file,
    "upsert_markdown_sections": _run_upsert_markdown_sections,
    "patch_file": _run_patch_file,
    "run_command": _run_run_command,
    "validate_artifacts": _run_validate_artifacts,
}


_TOOL_SCHEMAS: Dict[str, ToolSchema] = {
    "list_files": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "repos_dir": ToolParamSpec("dir_list"),
        }
    ),
    "clone_repository": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "repo_id": ToolParamSpec("string", required=True),
            "project_id": ToolParamSpec("string"),
            "repo_url": ToolParamSpec("string"),
            "branch": ToolParamSpec("string"),
            "depth": ToolParamSpec("int"),
        },
        retry_policy=RetryPolicy(max_retry_attempts=1),
    ),
    "extract_structure": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "files": ToolParamSpec("path_list", required=True, path_mode="within_root", expected_kind="file", allow_scalar_list=True),
        }
    ),
    "grep_search": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "pattern": ToolParamSpec("string", required=True),
            "repos_dir": ToolParamSpec("dir_list"),
        }
    ),
    "read_file_chunk": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "path": ToolParamSpec("path", required=True, aliases=("file_path",)),
            "search_root": ToolParamSpec("string", default="."),
            "start_line": ToolParamSpec("int", default=1),
            "end_line": ToolParamSpec("int"),
            "repos_dir": ToolParamSpec("dir_list"),
        }
    ),
    "extract_lookup_values": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "files": ToolParamSpec("path_list", path_mode="within_root", expected_kind="file", allow_scalar_list=True),
        }
    ),
    "query_database": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "db_id": ToolParamSpec("string", required=True),
            "project_id": ToolParamSpec("string"),
            "query_type": ToolParamSpec(
                "string",
                required=True,
                allowed_values=("list_tables", "describe_table", "list_indexes", "list_constraints", "execute_query"),
            ),
            "schema": ToolParamSpec("string"),
            "table_name": ToolParamSpec("string"),
            "sql": ToolParamSpec("string", aliases=("query",)),
            "limit": ToolParamSpec("int"),
        }
    ),
    "query_knowledge_base": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "project_id": ToolParamSpec("string"),
            "kb_id": ToolParamSpec("string"),
            "query_type": ToolParamSpec(
                "string",
                required=True,
                allowed_values=(
                    "search_terms",
                    "get_feature_tree",
                    "search_design_docs",
                    "vector_search_design_docs",
                    "retrieve_design_context",
                    "get_related_designs",
                ),
            ),
            "keyword": ToolParamSpec("string"),
            "feature_id": ToolParamSpec("string"),
            "limit": ToolParamSpec("int"),
            "top_k": ToolParamSpec("int"),
        }
    ),
    "write_file": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "path": ToolParamSpec("path", required=True, aliases=("file_path",), path_mode="within_root", expected_kind="file"),
            "content": ToolParamSpec("string", required=True, preserve_whitespace=True),
        }
    ),
    "append_file": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "path": ToolParamSpec("path", required=True, aliases=("file_path",), path_mode="within_root", expected_kind="file"),
            "content": ToolParamSpec("string", required=True, preserve_whitespace=True),
        }
    ),
    "upsert_markdown_sections": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "path": ToolParamSpec("path", required=True, aliases=("file_path",), path_mode="within_root", expected_kind="file"),
            "sections": ToolParamSpec("json", required=True, expected_kind="list"),
            "dedupe_strategy": ToolParamSpec("string", default="heading_or_similar"),
            "similarity_threshold": ToolParamSpec("float", default=0.9),
        }
    ),
    "patch_file": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "path": ToolParamSpec(
                "path",
                required=True,
                aliases=("file_path",),
                path_mode="within_root",
                expected_kind="file",
                must_exist=True,
            ),
            "old_content": ToolParamSpec("string", required=True, preserve_whitespace=True),
            "new_content": ToolParamSpec("string", required=True, preserve_whitespace=True),
        }
    ),
    "run_command": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "command": ToolParamSpec("command", required=True),
            "timeout": ToolParamSpec("int", default=30),
        },
        retry_policy=RetryPolicy(max_retry_attempts=2),
    ),
    "validate_artifacts": ToolSchema(
        parameters={
            "root_dir": ToolParamSpec("path", required=True, path_mode="root_dir"),
            "target_files": ToolParamSpec(
                "path_list",
                path_mode="within_root",
                expected_kind="file",
                allow_scalar_list=True,
            ),
        }
    ),
}
