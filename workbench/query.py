from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb
import sqlglot
from filelock import FileLock
from sqlglot import exp

from workbench.workspace import read_json, safe_path


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def readonly_connection(workspace: Path):
    with FileLock(str(safe_path(workspace, "metadata/database.lock")), timeout=1):
        if (read_json(safe_path(workspace, "metadata/sandbox.json"), {}) or {}).get("cleanup_failed"):
            raise RuntimeError("Database access is paused until the previous sandbox container is confirmed stopped")
        connection = duckdb.connect(str(safe_path(workspace, "database/workspace.duckdb")), read_only=True,
            config={"enable_external_access": "false", "autoload_known_extensions": "false", "autoinstall_known_extensions": "false", "allow_unsigned_extensions": "false", "threads": "2", "memory_limit": "256MB"})
        try:
            connection.execute("SET lock_configuration=true")
            yield connection
        finally:
            connection.close()


def validate_query(sql: str) -> str:
    if not sql.strip() or len(sql) > 20_000:
        raise ValueError("Enter a SELECT query of at most 20,000 characters")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as error:
        raise ValueError(f"Invalid SQL: {error}") from error
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("Only one read-only SELECT query is allowed")
    tree = statements[0]
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Copy, exp.Into)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ValueError("This query contains a write or system command")
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or table.catalog:
            raise ValueError("External sources and table functions are disabled")
    return tree.sql(dialect="duckdb")


def _cell(value, depth=0):
    if depth >= 3:
        return "<nested preview truncated>"
    if isinstance(value, (list, tuple)):
        result = [_cell(item, depth + 1) for item in value[:20]]
        return result + (["<additional items omitted>"] if len(value) > 20 else [])
    if isinstance(value, dict):
        return {str(key)[:100]: _cell(item, depth + 1) for key,item in list(value.items())[:20]}
    if isinstance(value, str):
        return value[:4000] + ("…" if len(value) > 4000 else "")
    if isinstance(value, bytes):
        return f"<binary: {len(value)} bytes>"
    return value


def sql_preview(workspace: Path, sql: str, limit: int = 100, offset: int = 0) -> dict:
    if not 1 <= limit <= 500 or not 0 <= offset <= 100_000:
        raise ValueError("Preview limits: 1–500 rows, offset up to 100,000")
    sql = validate_query(sql)
    with readonly_connection(workspace) as connection:
        timer = threading.Timer(5, connection.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cursor = connection.execute(f"SELECT * FROM ({sql}) AS bounded_preview LIMIT ? OFFSET ?", [limit + 1, offset])
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            return {"columns": columns, "rows": [[_cell(value) for value in row] for row in rows[:limit]], "truncated": len(rows) > limit}
        finally:
            timer.cancel()
            timer.join()


def database_schema(workspace: Path) -> list[dict]:
    with readonly_connection(workspace) as connection:
        rows = connection.execute("SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema IN ('raw','staging','features','results','audit') ORDER BY 1,2").fetchall()
        result = []
        for schema, name, kind in rows:
            columns = connection.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position", [schema, name]).fetchall()
            result.append({"schema": schema, "name": name, "type": kind, "columns": [{"name": n, "type": t} for n, t in columns]})
        return result
