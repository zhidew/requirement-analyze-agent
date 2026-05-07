from .._compat_loader import load_legacy_script


_legacy_module = load_legacy_script("tool_manager")

ToolManager = _legacy_module.ToolManager

__all__ = ["ToolManager"]
