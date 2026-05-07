from pathlib import Path
from typing import Any, Dict

from services.db_metadata import describe_table, execute_read_only_query, list_constraints, list_indexes, list_tables
from services.db_service import metadata_db

from .clone_repository import _resolve_project_id


def query_database(root_dir: Path, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    db_id = tool_input.get("db_id")
    query_type = tool_input.get("query_type")
    if not isinstance(db_id, str) or not db_id.strip():
        raise ValueError("`db_id` must be a non-empty string.")
    if not isinstance(query_type, str) or not query_type.strip():
        raise ValueError("`query_type` must be a non-empty string.")

    project_id = _resolve_project_id(root_dir, tool_input)
    db_config = metadata_db.get_database(project_id, db_id, include_secrets=True)
    if db_config is None:
        raise ValueError(f"Database config not found for db_id='{db_id}'.")

    schema = tool_input.get("schema")
    if schema is not None and not isinstance(schema, str):
        raise ValueError("`schema` must be a string when provided.")

    if query_type == "list_tables":
        result = {"tables": list_tables(db_config, schema=schema)}
    elif query_type == "describe_table":
        table_name = tool_input.get("table_name")
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("`table_name` is required for describe_table.")
        result = describe_table(db_config, table_name, schema=schema)
    elif query_type == "list_indexes":
        table_name = tool_input.get("table_name")
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("`table_name` is required for list_indexes.")
        result = {"indexes": list_indexes(db_config, table_name, schema=schema)}
    elif query_type == "list_constraints":
        table_name = tool_input.get("table_name")
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("`table_name` is required for list_constraints.")
        result = list_constraints(db_config, table_name, schema=schema)
    elif query_type == "execute_query":
        sql = tool_input.get("sql") or tool_input.get("query")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("`sql` is required for execute_query.")
        limit = int(tool_input.get("limit", 100) or 100)
        result = execute_read_only_query(db_config, sql, limit=limit)
    else:
        raise ValueError(f"Unsupported query_type: {query_type}")

    return {
        "project_id": project_id,
        "db_id": db_id,
        "query_type": query_type,
        **result,
    }
