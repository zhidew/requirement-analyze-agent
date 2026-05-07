from .._compat_loader import load_legacy_script


_legacy_module = load_legacy_script("generate_expert")

ExpertGenerator = _legacy_module.ExpertGenerator
create_expert = _legacy_module.create_expert

__all__ = ["ExpertGenerator", "create_expert"]
