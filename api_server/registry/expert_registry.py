"""
Expert Registry - Centralized expert profile management.

The runtime still exposes compatibility aliases for the historical
Agent* class names so existing orchestrator code can keep working while
the product surface shifts to the Expert mental model.
"""

import asyncio
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from .errors import AgentNotFoundError, ConfigLoadError, ValidationError
from .skill_parser import SkillParser
from graphs.tools.permissions import build_effective_tools, has_effective_tool_permission


def _ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return [str(value).strip()] if str(value).strip() else []


def _normalize_artifact_mapping(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for upstream, outputs in value.items():
        upstream_id = str(upstream).strip()
        if not upstream_id:
            continue
        normalized[upstream_id] = _ensure_list(outputs)
    return normalized


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value or ""))


@dataclass
class ExpertProfile:
    """Lightweight expert metadata used for discovery and routing."""

    capability: str
    name: str
    name_zh: str = ""
    name_en: str = ""
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    required_inputs: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    expert_yaml_path: Optional[str] = None
    skill_md_path: Optional[str] = None
    # Hot-pluggable task scheduling configuration
    has_scheduling: bool = False
    dependencies: List[str] = field(default_factory=list)
    upstream_artifacts: Dict[str, List[str]] = field(default_factory=dict)
    boundary_upstream_inputs: List[str] = field(default_factory=list)
    priority: int = 50
    phase: str = ""  # Explicit phase declaration from scheduling.phase (e.g. "RULES")

    @property
    def expertise(self) -> List[str]:
        return list(self.keywords)

    @property
    def agent_yaml_path(self) -> Optional[str]:
        return self.expert_yaml_path

    def to_planner_description(self) -> str:
        return f"- {self.capability}: {self.description}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def matches_keywords(self, search_terms: List[str]) -> bool:
        search_lower = [t.lower() for t in search_terms]
        keyword_lower = [k.lower() for k in self.keywords]
        return any(
            term in keyword_lower or any(term in kw for kw in keyword_lower)
            for term in search_lower
        )


@dataclass
class ExpertConfig:
    """Complete expert configuration used at execution time."""

    manifest: ExpertProfile
    tools_allowed: List[str] = field(default_factory=list)
    policies: Dict[str, Any] = field(default_factory=dict)
    workflow_steps: List[str] = field(default_factory=list)
    prompt_instructions: str = ""
    templates: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def explicit_tools_allowed(self) -> List[str]:
        return list(self.tools_allowed)

    @property
    def effective_tools(self) -> List[str]:
        return build_effective_tools(self.tools_allowed)

    @property
    def dependencies(self) -> List[str]:
        """Get expert dependencies for task scheduling."""
        return self.manifest.dependencies

    @property
    def priority(self) -> int:
        """Get expert priority for task scheduling."""
        return self.manifest.priority

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.manifest.capability,
            "name": self.manifest.name,
            "description": self.manifest.description,
            "tools_allowed": self.tools_allowed,
            "explicit_tools_allowed": self.explicit_tools_allowed,
            "effective_tools": self.effective_tools,
            "policies": self.policies,
            "workflow_steps": self.workflow_steps,
            "prompt_instructions_length": len(self.prompt_instructions),
            "templates": list(self.templates.keys()),
            "metadata": self.metadata,
            "dependencies": self.dependencies,
            "priority": self.priority,
        }

    def has_tool_permission(self, tool_name: str) -> bool:
        return has_effective_tool_permission(tool_name, self.tools_allowed)


class ExpertRegistry:
    """Thread-safe singleton for managing expert profiles."""

    _instance: Optional["ExpertRegistry"] = None
    _lock = threading.Lock()

    def __init__(self, base_dir: Optional[Path] = None):
        if hasattr(self, "_initialized") and self._initialized:
            return
        if base_dir:
            self._base_dir = Path(base_dir)
            self._manifests: Dict[str, ExpertProfile] = {}
            self._configs: Dict[str, ExpertConfig] = {}
            self._skill_parser = SkillParser()
            self._load_errors: List[str] = []
            self._initialized = True

    @classmethod
    def get_instance(cls) -> "ExpertRegistry":
        if cls._instance is None or not getattr(cls._instance, "_initialized", False):
            raise RuntimeError(
                "ExpertRegistry not initialized. "
                "Call ExpertRegistry.initialize() first."
            )
        return cls._instance

    @classmethod
    def initialize(cls, base_dir: Path) -> "ExpertRegistry":
        with cls._lock:
            if cls._instance is not None and getattr(cls._instance, "_initialized", False):
                return cls._instance

            instance = object.__new__(cls)
            instance._base_dir = Path(base_dir)
            instance._manifests = {}
            instance._configs = {}
            instance._skill_parser = SkillParser()
            instance._load_errors = []
            instance._initialized = False

            cls._instance = instance
            instance._load_all_manifests()
            instance._initialized = True
            return instance

    @classmethod
    async def initialize_async(cls, base_dir: Path) -> "ExpertRegistry":
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: cls.initialize(base_dir))

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._instance = None

    def _resolve_experts_dir(self) -> Path:
        preferred = self._base_dir / "experts"
        legacy = self._base_dir / "subagents"
        if preferred.exists():
            return preferred
        return legacy

    def _get_phase_config(self):
        try:
            from config import PhaseConfig
        except ModuleNotFoundError:
            import sys

            project_root = Path(__file__).resolve().parents[2]
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))
            from config import PhaseConfig

        return PhaseConfig.initialize(self._base_dir / "config" / "phases.yaml")

    def _load_all_manifests(self) -> None:
        experts_dir = self._resolve_experts_dir()
        skills_dir = self._base_dir / "skills"

        self._load_errors = []
        if not experts_dir.exists():
            self._load_errors.append(f"Experts directory not found: {experts_dir}")
            return

        expert_files = list(experts_dir.glob("*.expert.yaml")) + list(experts_dir.glob("*.agent.yaml"))
        for expert_file in expert_files:
            try:
                manifest = self._load_expert_profile(expert_file, skills_dir)
                if manifest:
                    self._manifests[manifest.capability] = manifest
            except Exception as exc:
                self._load_errors.append(f"Failed to load {expert_file.name}: {exc}")

    def _load_expert_profile(
        self,
        expert_file: Path,
        skills_dir: Path,
    ) -> Optional[ExpertProfile]:
        try:
            with open(expert_file, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ConfigLoadError(str(expert_file), f"Invalid YAML: {exc}")
        except Exception as exc:
            raise ConfigLoadError(str(expert_file), f"Failed to read file: {exc}")

        capability = data.get("capability")
        if not capability:
            stem = expert_file.stem
            capability = stem.replace(".expert", "").replace(".agent", "")
        if not capability:
            raise ValidationError("capability", None, "Missing capability")

        skill_path = skills_dir / capability / "SKILL.md"
        skill_frontmatter: Dict[str, Any] = {}
        if skill_path.exists():
            try:
                skill_frontmatter, _ = self._skill_parser.parse(skill_path)
            except Exception as exc:
                self._load_errors.append(f"Warning: Could not parse {skill_path}: {exc}")

        skill_name = str(skill_frontmatter.get("name") or "").strip()
        name = data.get("name") or skill_name or capability
        name_en = str(data.get("name_en") or data.get("name") or capability).strip()
        name_zh = str(data.get("name_zh") or "").strip()
        if not name_zh and skill_name and skill_name != name_en and _contains_cjk(skill_name):
            name_zh = skill_name
        description = (
            skill_frontmatter.get("description")
            or data.get("description")
            or f"Expert for {capability}"
        )
        keywords = skill_frontmatter.get("keywords") or data.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [item.strip() for item in keywords.split(",")]

        # Parse hot-pluggable scheduling configuration
        scheduling = data.get("scheduling")
        has_scheduling = scheduling is not None
        if not has_scheduling:
            scheduling = {}
        dependencies = _ensure_list(scheduling.get("dependencies", []))
        priority = scheduling.get("priority", 50)
        phase = ""
        try:
            phase = self._get_phase_config().get_phase_for_expert(str(capability).strip())
        except Exception:
            phase = ""
        if not phase:
            phase = str(scheduling.get("phase", "")).strip().upper() if scheduling.get("phase") else ""
        upstream_artifacts = _normalize_artifact_mapping(data.get("upstream_artifacts", {}))
        boundary_upstream_inputs = _ensure_list(
            data.get("metadata", {}).get("boundary_contract", {}).get("upstream_inputs", [])
        )

        return ExpertProfile(
            capability=capability,
            name=name,
            name_zh=name_zh,
            name_en=name_en,
            description=description,
            keywords=list(keywords),
            required_inputs=_ensure_list(data.get("inputs", {}).get("required", [])),
            expected_outputs=_ensure_list(data.get("outputs", {}).get("expected", [])),
            expert_yaml_path=str(expert_file),
            skill_md_path=str(skill_path) if skill_path.exists() else None,
            has_scheduling=has_scheduling,
            dependencies=list(dependencies),
            upstream_artifacts=upstream_artifacts,
            boundary_upstream_inputs=boundary_upstream_inputs,
            priority=int(priority),
            phase=phase,
        )

    def get_all_manifests(self) -> List[ExpertProfile]:
        return list(self._manifests.values())

    def get_manifest(self, capability: str) -> Optional[ExpertProfile]:
        return self._manifests.get(capability)

    def get_manifests_by_keywords(self, keywords: List[str]) -> List[ExpertProfile]:
        return [m for m in self._manifests.values() if m.matches_keywords(keywords)]

    def get_planner_agent_descriptions(self, filter_ids: Optional[List[str]] = None) -> str:
        """Get description of all experts, optionally filtered by IDs."""
        manifests = self._manifests.values()
        if filter_ids is not None:
            manifests = [m for m in manifests if m.capability in filter_ids]

        descriptions = [
            manifest.to_planner_description()
            for manifest in sorted(manifests, key=lambda item: item.capability)
        ]
        return "\n".join(descriptions)

    def get_capabilities(self) -> List[str]:
        return list(self._manifests.keys())

    def get_load_errors(self) -> List[str]:
        return list(self._load_errors)

    def load_full_config(self, capability: str) -> ExpertConfig:
        if capability in self._configs:
            return self._configs[capability]

        manifest = self._manifests.get(capability)
        if not manifest:
            raise AgentNotFoundError(capability)

        expert_data: Dict[str, Any] = {}
        if manifest.expert_yaml_path:
            try:
                with open(manifest.expert_yaml_path, "r", encoding="utf-8") as handle:
                    expert_data = yaml.safe_load(handle) or {}
            except Exception as exc:
                self._load_errors.append(f"Failed to reload {manifest.expert_yaml_path}: {exc}")

        workflow_steps: List[str] = []
        prompt_instructions = ""
        if manifest.skill_md_path:
            try:
                skill_path = Path(manifest.skill_md_path)
                _, body = self._skill_parser.parse(skill_path)
                workflow_steps = self._skill_parser.extract_workflow(body)
                prompt_instructions = self._skill_parser.build_prompt_instructions(body)
            except Exception as exc:
                self._load_errors.append(f"Failed to parse {manifest.skill_md_path}: {exc}")

        config = ExpertConfig(
            manifest=manifest,
            tools_allowed=expert_data.get("tools", {}).get("allowed", []),
            policies=expert_data.get("policies", {}),
            workflow_steps=workflow_steps,
            prompt_instructions=prompt_instructions,
            templates=self._load_templates(capability),
            metadata={
                **expert_data.get("metadata", {}),
                "interaction": expert_data.get("interaction", {}),
                "execution": expert_data.get("execution", {}),
                "expected_outputs": manifest.expected_outputs,
                "upstream_artifacts": manifest.upstream_artifacts,
            },
        )
        self._configs[capability] = config
        return config

    def _load_templates(self, capability: str) -> Dict[str, str]:
        templates: Dict[str, str] = {}
        template_dir = self._base_dir / "skills" / capability / "assets" / "templates"
        if not template_dir.exists():
            return templates

        for template_file in template_dir.glob("*"):
            if template_file.is_file():
                try:
                    templates[template_file.name] = template_file.read_text(encoding="utf-8")
                except Exception as exc:
                    self._load_errors.append(f"Failed to load template {template_file}: {exc}")
        return templates

    def clear_config_cache(self, capability: str = None) -> None:
        if capability:
            self._configs.pop(capability, None)
        else:
            self._configs.clear()

    def reload(self) -> None:
        self._manifests.clear()
        self._configs.clear()
        self._load_errors.clear()
        self._load_all_manifests()

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_experts": len(self._manifests),
            "total_agents": len(self._manifests),
            "cached_configs": len(self._configs),
            "load_errors": list(self._load_errors),
            "capabilities": self.get_capabilities(),
        }

    def validate_dependency_graph(self, exclude_capabilities: Optional[Set[str]] = None) -> Dict[str, Any]:
        excluded = {item.strip() for item in (exclude_capabilities or set()) if item and item.strip()}
        manifests = {
            manifest.capability: manifest
            for manifest in sorted(self._manifests.values(), key=lambda item: item.capability)
            if manifest.capability not in excluded
        }
        findings: List[Dict[str, Any]] = []

        def add_finding(
            severity: str,
            code: str,
            message: str,
            *,
            expert_id: Optional[str] = None,
            related_expert_id: Optional[str] = None,
            details: Optional[Dict[str, Any]] = None,
        ) -> None:
            findings.append(
                {
                    "severity": severity,
                    "code": code,
                    "message": message,
                    "expert_id": expert_id,
                    "related_expert_id": related_expert_id,
                    "details": details or {},
                }
            )

        dependency_edges = 0
        schedulable_count = 0
        output_owners: Dict[str, List[str]] = {}

        for manifest in manifests.values():
            if not manifest.has_scheduling:
                continue
            schedulable_count += 1
            dependency_edges += len(manifest.dependencies)

            if (
                manifest.boundary_upstream_inputs
                and set(manifest.boundary_upstream_inputs) != set(manifest.dependencies)
            ):
                add_finding(
                    "warning",
                    "BOUNDARY_INPUT_MISMATCH",
                    "Boundary-contract upstream inputs do not match scheduling dependencies.",
                    expert_id=manifest.capability,
                    details={
                        "dependencies": manifest.dependencies,
                        "boundary_upstream_inputs": manifest.boundary_upstream_inputs,
                    },
                )

            for dependency in manifest.dependencies:
                if dependency == manifest.capability:
                    add_finding(
                        "error",
                        "SELF_DEPENDENCY",
                        "Expert cannot depend on itself.",
                        expert_id=manifest.capability,
                    )
                    continue
                if dependency not in manifests:
                    add_finding(
                        "error",
                        "MISSING_DEPENDENCY",
                        "Dependency points to an expert that does not exist.",
                        expert_id=manifest.capability,
                        related_expert_id=dependency,
                    )

            if manifest.dependencies and not manifest.upstream_artifacts:
                add_finding(
                    "warning",
                    "MISSING_UPSTREAM_ARTIFACT_MAPPING",
                    "Expert has dependencies but does not declare any upstream artifact mapping.",
                    expert_id=manifest.capability,
                    details={"dependencies": manifest.dependencies},
                )

            for upstream_id, artifact_names in manifest.upstream_artifacts.items():
                if upstream_id == manifest.capability:
                    add_finding(
                        "error",
                        "SELF_UPSTREAM_ARTIFACT",
                        "Expert cannot consume its own artifacts as an upstream source.",
                        expert_id=manifest.capability,
                    )
                    continue
                if upstream_id not in manifests:
                    add_finding(
                        "error",
                        "UNKNOWN_UPSTREAM_EXPERT",
                        "Upstream artifact mapping references an unknown expert.",
                        expert_id=manifest.capability,
                        related_expert_id=upstream_id,
                    )
                    continue
                if upstream_id not in manifest.dependencies:
                    add_finding(
                        "warning",
                        "UPSTREAM_NOT_IN_DEPENDENCIES",
                        "Upstream artifact mapping references an expert that is not declared as a dependency.",
                        expert_id=manifest.capability,
                        related_expert_id=upstream_id,
                    )
                if not artifact_names:
                    add_finding(
                        "warning",
                        "EMPTY_UPSTREAM_ARTIFACT_MAPPING",
                        "Upstream artifact mapping exists but does not list any artifacts.",
                        expert_id=manifest.capability,
                        related_expert_id=upstream_id,
                    )
                    continue

                available_outputs = set(manifests[upstream_id].expected_outputs)
                if not available_outputs:
                    add_finding(
                        "warning",
                        "UPSTREAM_HAS_NO_EXPECTED_OUTPUTS",
                        "Upstream expert declares no expected outputs, so artifact lookup may stay empty.",
                        expert_id=manifest.capability,
                        related_expert_id=upstream_id,
                    )
                    continue

                unknown_outputs = [name for name in artifact_names if name not in available_outputs]
                if unknown_outputs:
                    add_finding(
                        "error",
                        "UNKNOWN_UPSTREAM_ARTIFACT",
                        "Upstream artifact mapping references files that the upstream expert does not produce.",
                        expert_id=manifest.capability,
                        related_expert_id=upstream_id,
                        details={"artifacts": unknown_outputs},
                    )

            for dependency in manifest.dependencies:
                upstream_outputs = manifests.get(dependency).expected_outputs if dependency in manifests else []
                if upstream_outputs and dependency not in manifest.upstream_artifacts:
                    add_finding(
                        "warning",
                        "DEPENDENCY_WITHOUT_ARTIFACT_MAPPING",
                        "Dependency exists but no upstream artifacts are declared for it.",
                        expert_id=manifest.capability,
                        related_expert_id=dependency,
                        details={"available_outputs": upstream_outputs},
                    )

            for output_name in manifest.expected_outputs:
                output_owners.setdefault(output_name, []).append(manifest.capability)

        graph = {
            capability: [
                dependency
                for dependency in manifest.dependencies
                if dependency in manifests and dependency != capability
            ]
            for capability, manifest in manifests.items()
        }
        cycle_signatures: Set[str] = set()
        visit_state: Dict[str, int] = {}
        stack: List[str] = []

        def walk(node: str) -> None:
            visit_state[node] = 1
            stack.append(node)
            for neighbor in graph.get(node, []):
                state = visit_state.get(neighbor, 0)
                if state == 0:
                    walk(neighbor)
                elif state == 1:
                    cycle = stack[stack.index(neighbor):] + [neighbor]
                    signature = " -> ".join(cycle)
                    if signature not in cycle_signatures:
                        cycle_signatures.add(signature)
                        add_finding(
                            "error",
                            "DEPENDENCY_CYCLE",
                            "Dependency cycle detected in expert graph.",
                            expert_id=node,
                            related_expert_id=neighbor,
                            details={"cycle": cycle},
                        )
            stack.pop()
            visit_state[node] = 2

        for capability in graph:
            if visit_state.get(capability, 0) == 0:
                walk(capability)

        for output_name, owners in sorted(output_owners.items()):
            if len(owners) > 1:
                add_finding(
                    "warning",
                    "DUPLICATE_EXPECTED_OUTPUT",
                    "Multiple experts declare the same expected output file name.",
                    details={"output": output_name, "owners": owners},
                )

        # --- Phase dependency validation ---
        # Build a phase map: expert_id -> phase for all schedulable experts.
        _pcfg = self._get_phase_config()
        for error_message in _pcfg.validation_errors:
            add_finding(
                "error",
                "DUPLICATE_PHASE_ASSIGNMENT",
                error_message,
            )

        # Collect phase per expert from phases.yaml, falling back only for older configs.
        expert_phase_map: Dict[str, str] = _pcfg.get_expert_phase_map()
        try:
            from graphs.nodes import AGENT_PHASE_MAP as _legacy_phase_map
        except Exception:
            _legacy_phase_map = {}
        for capability, manifest in manifests.items():
            if not manifest.has_scheduling or capability in expert_phase_map:
                continue
            if manifest.phase and _pcfg.is_executable_phase(manifest.phase):
                expert_phase_map[capability] = manifest.phase
            elif capability in _legacy_phase_map:
                expert_phase_map[capability] = _legacy_phase_map[capability]

        for capability, manifest in manifests.items():
            if not manifest.has_scheduling:
                continue

            # MISSING_PHASE_BINDING: expert has no phase binding at all
            if capability not in expert_phase_map:
                add_finding(
                    "error",
                    "MISSING_PHASE_BINDING",
                    "Expert has no phase binding (neither scheduling.phase in YAML nor legacy AGENT_PHASE_MAP).",
                    expert_id=capability,
                    details={"available_phases": _pcfg.execution_phases},
                )
                continue

            my_phase = expert_phase_map[capability]
            my_rank = _pcfg.phase_rank(my_phase)

            # BACKWARD_PHASE_DEPENDENCY: dependency is in the same or later phase
            for dependency in manifest.dependencies:
                if dependency not in expert_phase_map:
                    continue  # already reported as MISSING_DEPENDENCY above
                dep_phase = expert_phase_map[dependency]
                dep_rank = _pcfg.phase_rank(dep_phase)

                if dep_rank >= my_rank:
                    add_finding(
                        "error",
                        "BACKWARD_PHASE_DEPENDENCY",
                        f"Expert depends on '{dependency}' which is in phase '{dep_phase}' (rank {dep_rank}), "
                        f"same as or later than this expert's phase '{my_phase}' (rank {my_rank}). "
                        f"Dependencies must come from strictly earlier phases.",
                        expert_id=capability,
                        related_expert_id=dependency,
                        details={
                            "my_phase": my_phase,
                            "my_rank": my_rank,
                            "dep_phase": dep_phase,
                            "dep_rank": dep_rank,
                        },
                    )

        severity_rank = {"error": 0, "warning": 1, "info": 2}
        findings.sort(
            key=lambda item: (
                severity_rank.get(item["severity"], 99),
                item.get("expert_id") or "",
                item.get("related_expert_id") or "",
                item["code"],
            )
        )

        summary = {
            "errors": sum(1 for item in findings if item["severity"] == "error"),
            "warnings": sum(1 for item in findings if item["severity"] == "warning"),
            "infos": sum(1 for item in findings if item["severity"] == "info"),
        }
        return {
            "ok": summary["errors"] == 0,
            "expert_count": schedulable_count,
            "dependency_edges": dependency_edges,
            "summary": summary,
            "findings": findings,
        }


# Backward-compatible aliases for existing runtime imports.
AgentManifest = ExpertProfile
AgentFullConfig = ExpertConfig
AgentRegistry = ExpertRegistry
