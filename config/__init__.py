"""
Phase configuration loader and writer.

This module keeps ``config/phases.yaml`` as the single source of truth for:
- phase ordering and labels
- executability
- expert-to-phase assignments
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


_DEFAULT_DATA = {
    "phases": [
        {"id": "INIT", "label_zh": "初始化", "label_en": "Init", "executable": False, "order": 0, "experts": []},
        {"id": "PLANNING", "label_zh": "设计规划", "label_en": "Design Planning", "executable": True, "order": 1, "experts": []},
        {"id": "ANALYSIS", "label_zh": "需求分析", "label_en": "Analysis", "executable": True, "order": 2, "experts": []},
        {"id": "ARCHITECTURE", "label_zh": "架构设计", "label_en": "Architecture", "executable": True, "order": 3, "experts": ["modular-design", "integration-design"]},
        {"id": "MODELING", "label_zh": "建模设计", "label_en": "Modeling", "executable": True, "order": 4, "experts": ["data-design", "ddd-structure"]},
        {"id": "INTERFACE", "label_zh": "接口设计", "label_en": "Interface", "executable": True, "order": 5, "experts": ["api-design", "config-design", "flow-design"]},
        {"id": "DFX", "label_zh": "DFX设计", "label_en": "DFX", "executable": True, "order": 6, "experts": ["performance-design", "ops-design"]},
        {"id": "QUALITY", "label_zh": "质量保障", "label_en": "Quality", "executable": True, "order": 7, "experts": ["test-design"]},
        {"id": "DELIVERY", "label_zh": "交付装配", "label_en": "Delivery", "executable": True, "order": 8, "experts": ["design-assembler", "validator"]},
        {"id": "DONE", "label_zh": "已完成", "label_en": "Done", "executable": False, "order": 99, "experts": []},
    ]
}


def _normalize_experts(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    stripped = str(value).strip()
    return [stripped] if stripped else []


@dataclass
class PhaseDefinition:
    id: str
    label_zh: str = ""
    label_en: str = ""
    executable: bool = False
    order: int = 0
    experts: List[str] = field(default_factory=list)

    def label(self, lang: str = "zh") -> str:
        return self.label_zh if lang == "zh" else self.label_en

    def to_dict(self, lang: str = "zh") -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label(lang),
            "label_zh": self.label_zh,
            "label_en": self.label_en,
            "executable": self.executable,
            "order": self.order,
            "experts": list(self.experts),
            "agents": list(self.experts),
        }


class PhaseConfig:
    _instance: Optional["PhaseConfig"] = None
    _lock = threading.Lock()

    def __init__(self, config_path: Optional[Path] = None):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._config_path = config_path or self._resolve_config_path()
        self._phases: List[PhaseDefinition] = []
        self._phase_map: Dict[str, PhaseDefinition] = {}
        self._execution_phases: List[str] = []
        self._phase_order: List[str] = []
        self._expert_phase_map: Dict[str, str] = {}
        self._validation_errors: List[str] = []
        self._load()
        self._initialized = True

    @classmethod
    def get_instance(cls) -> "PhaseConfig":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def initialize(cls, config_path: Optional[Path] = None) -> "PhaseConfig":
        with cls._lock:
            cls._instance = cls(config_path)
        return cls._instance

    @staticmethod
    def _resolve_config_path() -> Path:
        current = Path(__file__).resolve().parent
        for _ in range(5):
            candidate = current / "config" / "phases.yaml"
            if candidate.exists():
                return candidate
            parent = current.parent
            if parent == current:
                break
            current = parent
        return Path(__file__).resolve().parent.parent.parent / "config" / "phases.yaml"

    def _load_yaml(self) -> Dict[str, Any]:
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as handle:
                    return yaml.safe_load(handle) or {}
            except Exception:
                return {}
        return dict(_DEFAULT_DATA)

    def _load(self) -> None:
        raw_data = self._load_yaml()
        entries = raw_data.get("phases", [])
        if not entries:
            raw_data = dict(_DEFAULT_DATA)
            entries = raw_data.get("phases", [])

        phases: List[PhaseDefinition] = []
        expert_phase_map: Dict[str, str] = {}
        validation_errors: List[str] = []

        for entry in entries:
            if not entry or not entry.get("id"):
                continue

            phase_id = str(entry["id"]).strip().upper()
            experts = _normalize_experts(entry.get("experts", []))
            unique_experts: List[str] = []
            for expert_id in experts:
                if expert_id in unique_experts:
                    continue
                owner = expert_phase_map.get(expert_id)
                if owner and owner != phase_id:
                    validation_errors.append(
                        f"Expert '{expert_id}' is assigned to multiple phases: '{owner}' and '{phase_id}'.",
                    )
                    continue
                expert_phase_map[expert_id] = phase_id
                unique_experts.append(expert_id)

            phases.append(
                PhaseDefinition(
                    id=phase_id,
                    label_zh=str(entry.get("label_zh", "")),
                    label_en=str(entry.get("label_en", "")),
                    executable=bool(entry.get("executable", False)),
                    order=int(entry.get("order", 99)),
                    experts=unique_experts,
                )
            )

        phases.sort(key=lambda item: item.order)

        self._phases = phases
        self._phase_map = {phase.id: phase for phase in phases}
        self._execution_phases = [phase.id for phase in phases if phase.executable]
        self._phase_order = [phase.id for phase in phases]
        self._expert_phase_map = expert_phase_map
        self._validation_errors = validation_errors

    def reload(self) -> None:
        self._load()

    @property
    def phases(self) -> List[PhaseDefinition]:
        return list(self._phases)

    @property
    def execution_phases(self) -> List[str]:
        return list(self._execution_phases)

    @property
    def phase_order(self) -> List[str]:
        return list(self._phase_order)

    @property
    def executable_phase_map(self) -> Dict[str, PhaseDefinition]:
        return {phase.id: phase for phase in self._phases if phase.executable}

    @property
    def validation_errors(self) -> List[str]:
        return list(self._validation_errors)

    def is_valid_phase(self, phase_id: str) -> bool:
        return phase_id.upper() in self._phase_map

    def is_executable_phase(self, phase_id: str) -> bool:
        phase = self._phase_map.get(phase_id.upper())
        return phase.executable if phase else False

    def get_label(self, phase_id: str, lang: str = "zh") -> str:
        phase = self._phase_map.get(phase_id.upper())
        return phase.label(lang) if phase else phase_id

    def phase_rank(self, phase_id: str) -> int:
        normalized = phase_id.upper()
        for index, current in enumerate(self._execution_phases):
            if current == normalized:
                return index
        return len(self._execution_phases)

    def get_phase(self, phase_id: str) -> Optional[PhaseDefinition]:
        return self._phase_map.get(phase_id.upper())

    def get_phase_labels(self, lang: str = "zh", executable_only: bool = False) -> List[Dict[str, Any]]:
        source = [phase for phase in self._phases if phase.executable] if executable_only else self._phases
        return [phase.to_dict(lang) for phase in source]

    def get_experts_for_phase(self, phase_id: str) -> List[str]:
        phase = self.get_phase(phase_id)
        return list(phase.experts) if phase else []

    def get_phase_for_expert(self, expert_id: str) -> str:
        return self._expert_phase_map.get(expert_id, "")

    def get_expert_phase_map(self) -> Dict[str, str]:
        return dict(self._expert_phase_map)

    def update_phase_configuration(self, phase_updates: List[Dict[str, Any]]) -> None:
        fixed_phase_ids = {"INIT", "PLANNING", "DONE"}
        normalized_updates: Dict[str, Dict[str, Any]] = {}
        owner_by_expert: Dict[str, str] = {}
        requested_orders: Dict[str, int] = {}

        for update in phase_updates:
            phase_id = str(update.get("id") or "").strip().upper()
            if not self.is_valid_phase(phase_id):
                raise ValueError(f"Unknown phase '{phase_id}'.")
            experts = _normalize_experts(update.get("experts", []))
            normalized_updates[phase_id] = {"experts": []}
            requested_order = int(update.get("order", self._phase_map[phase_id].order))
            requested_orders[phase_id] = requested_order
            for expert_id in experts:
                owner = owner_by_expert.get(expert_id)
                if owner and owner != phase_id:
                    raise ValueError(
                        f"Expert '{expert_id}' is assigned to multiple phases: '{owner}' and '{phase_id}'.",
                    )
                owner_by_expert[expert_id] = phase_id
                if expert_id not in normalized_updates[phase_id]["experts"]:
                    normalized_updates[phase_id]["experts"].append(expert_id)

        effective_orders = {
            phase.id: (phase.order if phase.id in fixed_phase_ids else requested_orders.get(phase.id, phase.order))
            for phase in self._phases
        }
        duplicate_orders = {
            order
            for order in effective_orders.values()
            if list(effective_orders.values()).count(order) > 1
        }
        if duplicate_orders:
            raise ValueError(f"Duplicate phase order values are not allowed: {', '.join(str(item) for item in sorted(duplicate_orders))}")

        payload = {
            "phases": [
                {
                    "id": phase.id,
                    "label_zh": phase.label_zh,
                    "label_en": phase.label_en,
                    "executable": phase.executable,
                    "order": effective_orders[phase.id],
                    "experts": list(phase.experts) if phase.id in fixed_phase_ids else normalized_updates.get(phase.id, {}).get("experts", list(phase.experts)),
                }
                for phase in self._phases
            ]
        }

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)

        self.reload()


def get_phase_config() -> PhaseConfig:
    return PhaseConfig.get_instance()
