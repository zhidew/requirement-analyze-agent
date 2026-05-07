from __future__ import annotations

from typing import Any, Dict, List


CHECK_KEYS = [
    "coverage_check",
    "context_consistency_check",
    "dependency_check",
    "internal_consistency_check",
    "feasibility_check",
    "risk_check",
]


def default_reflection_checks(content: str, dependency_refs: List[str] | None = None) -> Dict[str, Dict[str, Any]]:
    dependency_refs = dependency_refs or []
    trimmed = (content or "").strip()
    wordish_length = len(trimmed)
    has_headings = "\n#" in f"\n{trimmed}"
    has_risk_language = any(marker in trimmed.lower() for marker in ["risk", "风险", "assumption", "假设", "待确认"])

    return {
        "coverage_check": {
            "status": "passed" if wordish_length >= 200 else "warning",
            "message": "Artifact has enough body content for first-pass review." if wordish_length >= 200 else "Artifact content is short; coverage may be incomplete.",
        },
        "context_consistency_check": {
            "status": "unknown",
            "message": "System-level context consistency is handled by the dedicated consistency stage.",
        },
        "dependency_check": {
            "status": "passed" if dependency_refs else "unknown",
            "message": "Dependency references were recorded." if dependency_refs else "No explicit upstream dependency references were available.",
        },
        "internal_consistency_check": {
            "status": "passed" if has_headings or wordish_length < 200 else "warning",
            "message": "Structured sections are present." if has_headings else "No markdown headings were detected.",
        },
        "feasibility_check": {
            "status": "unknown",
            "message": "Feasibility requires project asset checks and follow-up validation.",
        },
        "risk_check": {
            "status": "passed" if has_risk_language else "warning",
            "message": "Risk or assumption language is present." if has_risk_language else "No explicit risk or assumption section was detected.",
        },
    }


def normalize_reflection_report(report: Dict[str, Any]) -> Dict[str, Any]:
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    normalized_checks: Dict[str, Dict[str, Any]] = {}
    issues: List[Dict[str, Any]] = []
    blocking = False

    for key in CHECK_KEYS:
        raw = checks.get(key) if isinstance(checks.get(key), dict) else {}
        status = str(raw.get("status") or "unknown").strip().lower()
        if status not in {"passed", "warning", "blocking", "unknown"}:
            status = "unknown"
        message = str(raw.get("message") or "").strip()
        normalized_checks[key] = {"status": status, "message": message}
        if status in {"warning", "blocking"}:
            issue = {"check": key, "severity": status, "message": message}
            issues.append(issue)
        if status == "blocking":
            blocking = True

    confidence = report.get("confidence", 0.65)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.65
    confidence = max(0.0, min(1.0, confidence))

    if blocking:
        status = "blocking"
    elif any(issue["severity"] == "warning" for issue in issues):
        status = "warning"
    else:
        status = "passed"

    return {
        "status": status,
        "confidence": confidence,
        "checks": normalized_checks,
        "issues": issues,
        "assumptions": list(report.get("assumptions") or []),
        "open_questions": list(report.get("open_questions") or []),
        "required_actions": list(report.get("required_actions") or []),
        "blocks_downstream": blocking,
    }
