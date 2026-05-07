import re
from typing import Dict, List, Optional

from services.db_connector import connect_database

READ_ONLY_QUERY_RE = re.compile(r"^\s*(select|with|pragma)\b", re.IGNORECASE)


def list_tables(config: Dict[str, object], schema: Optional[str] = None) -> List[Dict[str, object]]:
    with connect_database(config) as (kind, conn):
        if kind == "sqlite":
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [{"schema": schema or "main", "table_name": row["name"]} for row in rows]

        from sqlalchemy import inspect

        inspector = inspect(conn)
        schema_names = [schema] if schema else inspector.get_schema_names()
        tables: List[Dict[str, object]] = []
        for schema_name in schema_names:
            for table_name in inspector.get_table_names(schema=schema_name):
                tables.append({"schema": schema_name, "table_name": table_name})
        return tables


def describe_table(config: Dict[str, object], table_name: str, schema: Optional[str] = None) -> Dict[str, object]:
    with connect_database(config) as (kind, conn):
        if kind == "sqlite":
            columns = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
            if not columns:
                raise ValueError(f"Table not found: {table_name}")
            return {
                "schema": schema or "main",
                "table_name": table_name,
                "columns": [
                    {
                        "name": row["name"],
                        "type": row["type"],
                        "nullable": not bool(row["notnull"]),
                        "default": row["dflt_value"],
                        "primary_key": bool(row["pk"]),
                    }
                    for row in columns
                ],
            }

        from sqlalchemy import inspect

        inspector = inspect(conn)
        columns = inspector.get_columns(table_name, schema=schema)
        if not columns:
            raise ValueError(f"Table not found: {table_name}")
        return {
            "schema": schema,
            "table_name": table_name,
            "columns": [
                {
                    "name": column.get("name"),
                    "type": str(column.get("type")),
                    "nullable": column.get("nullable"),
                    "default": column.get("default"),
                    "comment": column.get("comment"),
                    "primary_key": bool(column.get("primary_key")),
                }
                for column in columns
            ],
        }


def list_indexes(config: Dict[str, object], table_name: str, schema: Optional[str] = None) -> List[Dict[str, object]]:
    with connect_database(config) as (kind, conn):
        if kind == "sqlite":
            rows = conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
            return [
                {
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                    "origin": row["origin"],
                    "partial": bool(row["partial"]),
                }
                for row in rows
            ]

        from sqlalchemy import inspect

        inspector = inspect(conn)
        return inspector.get_indexes(table_name, schema=schema)


def list_constraints(config: Dict[str, object], table_name: str, schema: Optional[str] = None) -> Dict[str, object]:
    with connect_database(config) as (kind, conn):
        if kind == "sqlite":
            foreign_keys = conn.execute(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
            indexes = conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
            unique_constraints = [row["name"] for row in indexes if row["unique"]]
            return {
                "foreign_keys": [
                    {
                        "id": row["id"],
                        "from": row["from"],
                        "to": row["to"],
                        "table": row["table"],
                        "on_update": row["on_update"],
                        "on_delete": row["on_delete"],
                    }
                    for row in foreign_keys
                ],
                "unique_constraints": unique_constraints,
            }

        from sqlalchemy import inspect

        inspector = inspect(conn)
        return {
            "foreign_keys": inspector.get_foreign_keys(table_name, schema=schema),
            "unique_constraints": inspector.get_unique_constraints(table_name, schema=schema),
            "check_constraints": inspector.get_check_constraints(table_name, schema=schema),
            "pk_constraint": inspector.get_pk_constraint(table_name, schema=schema),
        }


def execute_read_only_query(config: Dict[str, object], sql: str, limit: int = 100) -> Dict[str, object]:
    if not READ_ONLY_QUERY_RE.match(sql or ""):
        raise ValueError("Only read-only SELECT/WITH/PRAGMA queries are allowed.")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("Multiple SQL statements are not allowed.")

    with connect_database(config) as (kind, conn):
        if kind == "sqlite":
            rows = conn.execute(sql).fetchmany(limit)
            result_rows = [dict(row) for row in rows]
        else:
            from sqlalchemy import text

            result = conn.execute(text(sql))
            result_rows = [dict(row._mapping) for row in result.fetchmany(limit)]
        return {"rows": result_rows, "row_count": len(result_rows), "limit": limit}
