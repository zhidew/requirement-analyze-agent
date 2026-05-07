from __future__ import annotations

from typing import Any, Dict, List

from subgraphs.reflection_schema import default_reflection_checks, normalize_reflection_report


def build_reflection_prompt(expert_id: str, artifact_title: str) -> str:
    return (
        f"Review artifact '{artifact_title}' produced by expert '{expert_id}'. "
        "Check coverage, context consistency, dependency usage, internal consistency, feasibility, risks, "
        "unconfirmed assumptions, and downstream blockers. Return a structured reflection report."
    )


def parse_reflection_report(value: Dict[str, Any] | None) -> Dict[str, Any]:
    return normalize_reflection_report(value or {})


def should_apply_reflection_revision(report: Dict[str, Any]) -> bool:
    return bool(report.get("status") == "blocking")


def apply_reflection_revision(content: str, report: Dict[str, Any]) -> str:
    if not should_apply_reflection_revision(report):
        return content
    actions = report.get("required_actions") or []
    if not actions:
        return content
    notes = "\n".join(f"- {item}" for item in actions)
    return f"{content.rstrip()}\n\n## Reflection Required Actions\n\n{notes}\n"


def record_reflection_observation(content: str, dependency_refs: List[str] | None = None) -> Dict[str, Any]:
    checks = default_reflection_checks(content, dependency_refs=dependency_refs)
    warning_count = sum(1 for item in checks.values() if item.get("status") == "warning")
    confidence = 0.82 if warning_count == 0 else max(0.45, 0.74 - warning_count * 0.08)
    return normalize_reflection_report(
        {
            "confidence": confidence,
            "checks": checks,
            "assumptions": [],
            "open_questions": [],
            "required_actions": [],
        }
    )
