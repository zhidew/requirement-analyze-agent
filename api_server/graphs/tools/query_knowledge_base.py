from pathlib import Path
from typing import Any, Dict, List, Tuple

from services.db_service import metadata_db
from services.kb_indexer import (
    KnowledgeBaseError,
    get_feature_tree,
    get_related_designs,
    load_knowledge_base,
    retrieve_design_context,
    search_design_docs,
    search_terms,
    vector_search_design_docs,
)

from .clone_repository import _resolve_project_id


def _resolve_local_kb_root(project_id: str, kb_config: Dict[str, Any]) -> Path:
    path = kb_config.get("path")
    if not path:
        raise KnowledgeBaseError(f"Knowledge base '{kb_config['id']}' is missing path.")

    kb_root = Path(path)
    if not kb_root.is_absolute():
        kb_root = Path(__file__).resolve().parents[3] / "projects" / project_id / kb_root
    return kb_root.resolve()


def _load_kb(project_id: str, kb_config: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    kb_type = kb_config.get("type")
    if kb_type == "local":
        kb_root = _resolve_local_kb_root(project_id, kb_config)
        return (
            load_knowledge_base(kb_root, includes=kb_config.get("includes"), kb_type="local"),
            str(kb_root),
        )
    if kb_type == "remote":
        index_url = kb_config.get("index_url")
        if not index_url:
            raise KnowledgeBaseError(f"Knowledge base '{kb_config['id']}' is missing index_url.")
        return (
            load_knowledge_base(
                None,
                includes=kb_config.get("includes"),
                kb_type="remote",
                index_url=index_url,
            ),
            str(index_url),
        )
    raise KnowledgeBaseError(f"Unsupported knowledge base type: {kb_type}")


def query_knowledge_base(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    query_type = tool_input.get("query_type")
    if not isinstance(query_type, str) or not query_type.strip():
        raise ValueError("`query_type` must be a non-empty string.")

    project_id = _resolve_project_id(root_dir, tool_input)
    kb_id = tool_input.get("kb_id")
    if kb_id:
        config = metadata_db.get_knowledge_base(project_id, kb_id)
        if config is None:
            raise ValueError(f"Knowledge base config not found for kb_id='{kb_id}'.")
        kb_configs: List[Dict[str, Any]] = [config]
    else:
        kb_configs = metadata_db.list_knowledge_bases(project_id)
        if not kb_configs:
            raise ValueError(f"No knowledge bases configured for project '{project_id}'.")

    aggregated = []
    errors = []
    keyword = tool_input.get("keyword")
    feature_id = tool_input.get("feature_id")
    limit = int(tool_input.get("limit") or 10)
    top_k = int(tool_input.get("top_k") or 5)

    for kb_config in kb_configs:
        try:
            index, kb_location = _load_kb(project_id, kb_config)
            if query_type == "search_terms":
                if not isinstance(keyword, str) or not keyword.strip():
                    raise ValueError("`keyword` is required for search_terms.")
                payload = {"matches": search_terms(index, keyword, limit=limit)}
            elif query_type == "get_feature_tree":
                payload = {"feature_tree": get_feature_tree(index)}
            elif query_type == "search_design_docs":
                if not isinstance(keyword, str) or not keyword.strip():
                    raise ValueError("`keyword` is required for search_design_docs.")
                payload = {"matches": search_design_docs(index, keyword, limit=limit, feature_id=feature_id)}
            elif query_type == "vector_search_design_docs":
                if not isinstance(keyword, str) or not keyword.strip():
                    raise ValueError("`keyword` is required for vector_search_design_docs.")
                payload = {"matches": vector_search_design_docs(index, keyword, top_k=top_k, feature_id=feature_id)}
            elif query_type == "retrieve_design_context":
                if not isinstance(keyword, str) or not keyword.strip():
                    raise ValueError("`keyword` is required for retrieve_design_context.")
                payload = {"matches": retrieve_design_context(index, keyword, top_k=top_k, feature_id=feature_id)}
            elif query_type == "get_related_designs":
                if not isinstance(feature_id, str) or not feature_id.strip():
                    raise ValueError("`feature_id` is required for get_related_designs.")
                payload = {"matches": get_related_designs(index, feature_id, limit=limit)}
            else:
                raise ValueError(f"Unsupported query_type: {query_type}")

            aggregated.append(
                {
                    "kb_id": kb_config["id"],
                    "kb_name": kb_config["name"],
                    "kb_type": kb_config.get("type"),
                    "kb_location": kb_location,
                    **payload,
                }
            )
        except Exception as exc:  # noqa: BLE001
            error_payload = {
                "kb_id": kb_config.get("id"),
                "kb_name": kb_config.get("name"),
                "kb_type": kb_config.get("type"),
                "message": str(exc),
            }
            if kb_id:
                raise
            errors.append(error_payload)

    return {
        "project_id": project_id,
        "query_type": query_type,
        "knowledge_bases": aggregated,
        "errors": errors,
    }
