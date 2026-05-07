from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
import sys


_LEGACY_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "expert-creator" / "scripts"


def load_legacy_script(script_name: str) -> ModuleType:
    """Load a script from the legacy hyphenated expert-creator skill path."""
    module_name = f"skills.expert_creator._legacy_{script_name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    module_path = _LEGACY_SCRIPTS_DIR / f"{script_name}.py"
    if not module_path.exists():
        raise ModuleNotFoundError(f"Legacy expert-creator script not found: {module_path}")

    spec = spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec for {module_path}")

    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
