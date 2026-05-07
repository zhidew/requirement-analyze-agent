from __future__ import annotations

import uuid
from typing import Any, Dict, List

from services.db_service import metadata_db


def _extract_artifact_id(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("artifact_id") or ref.get("id") or "").strip()
    return str(ref or "").strip()


def build_artifact_dependency_graph(project_id: str, version_id: str) -> Dict[str, Any]:
    artifacts = metadata_db.list_design_artifacts(project_id, version_id)
    artifact_ids = {item["artifact_id"] for item in artifacts}
    edges: List[Dict[str, Any]] = []

    for artifact in artifacts:
        for ref in artifact.get("dependency_refs") or []:
            upstream_id = _extract_artifact_id(ref)
            if not upstream_id or upstream_id not in artifact_ids:
                continue
            edge = metadata_db.upsert_artifact_dependency_edge(
                edge_id=str(uuid.uuid4()),
                project_id=project_id,
                version_id=version_id,
                upstream_artifact_id=upstream_id,
                downstream_artifact_id=artifact["artifact_id"],
                dependency_type="artifact_ref",
                evidence={"dependency_ref": ref, "downstream_expert": artifact.get("expert_id")},
            )
            edges.append(edge)

    return {
        "project_id": project_id,
        "version_id": version_id,
        "nodes": artifacts,
        "edges": edges,
    }


def find_downstream_artifacts(artifact_id: str) -> List[Dict[str, Any]]:
    artifact = metadata_db.get_design_artifact(artifact_id)
    if not artifact:
        raise ValueError("Design artifact not found.")
    build_artifact_dependency_graph(artifact["project_id"], artifact["version_id"])
    edges = metadata_db.list_artifact_dependency_edges(
        project_id=artifact["project_id"],
        version_id=artifact["version_id"],
        upstream_artifact_id=artifact_id,
    )
    downstream = []
    for edge in edges:
        item = metadata_db.get_design_artifact(edge["downstream_artifact_id"])
        if item:
            downstream.append({**item, "dependency_edge": edge})
    return downstream


def list_dependency_edges(project_id: str, version_id: str) -> List[Dict[str, Any]]:
    build_artifact_dependency_graph(project_id, version_id)
    return metadata_db.list_artifact_dependency_edges(project_id=project_id, version_id=version_id)
