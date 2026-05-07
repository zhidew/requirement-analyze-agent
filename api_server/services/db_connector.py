from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Tuple

import sqlite3


class DatabaseConnectionError(RuntimeError):
    pass


def build_connection_url(config: Dict[str, object]) -> str:
    db_type = str(config.get("type", "")).lower()
    database_name = str(config.get("database", ""))
    host = str(config.get("host", ""))
    port = config.get("port")
    username = config.get("username") or ""
    password = config.get("password") or ""

    if db_type == "sqlite":
        db_path = Path(database_name)
        if not db_path.is_absolute():
            db_path = Path(host or ".") / db_path
        return f"sqlite:///{db_path.resolve().as_posix()}"
    if db_type == "mysql":
        return f"mysql+pymysql://{username}:{password}@{host}:{port}/{database_name}"
    if db_type == "postgresql":
        return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{database_name}"
    if db_type == "opengauss":
        return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{database_name}"
    if db_type == "dws":
        return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{database_name}"
    if db_type == "oracle":
        return f"oracle+cx_oracle://{username}:{password}@{host}:{port}/?service_name={database_name}"
    raise DatabaseConnectionError(f"Unsupported database type: {db_type}")


@contextmanager
def connect_database(config: Dict[str, object]) -> Iterator[Tuple[str, object]]:
    db_type = str(config.get("type", "")).lower()
    if db_type == "sqlite":
        database_name = str(config.get("database", ""))
        host = str(config.get("host", ""))
        db_path = Path(database_name)
        if not db_path.is_absolute():
            db_path = Path(host or ".") / db_path
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield "sqlite", conn
        finally:
            conn.close()
        return

    try:
        from sqlalchemy import create_engine
    except ImportError as exc:  # pragma: no cover
        raise DatabaseConnectionError(
            "SQLAlchemy is required for non-SQLite database connections."
        ) from exc

    url = build_connection_url(config)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            yield "sqlalchemy", connection
    except Exception as exc:  # pragma: no cover
        raise DatabaseConnectionError(str(exc)) from exc
    finally:
        engine.dispose()
