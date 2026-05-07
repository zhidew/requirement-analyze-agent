from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def run_command(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    cmd = tool_input.get("command")
    if not cmd or not isinstance(cmd, list):
        raise ValueError("`command` is required and must be a list of strings.")
    timeout = int(tool_input.get("timeout", 30) or 30)
    if timeout < 1:
        raise ValueError("`timeout` must be >= 1.")

    # Convert cmd elements to strings for safety
    cmd_str_list = [str(item) for item in cmd]

    # Special handling for python to use current executable
    if cmd_str_list[0] == "python":
        cmd_str_list[0] = sys.executable

    try:
        result = subprocess.run(
            cmd_str_list,
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": cmd_str_list,
            "timeout": timeout,
        }
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout} seconds: {' '.join(cmd_str_list)}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to execute command {' '.join(cmd_str_list)}: {exc}")
