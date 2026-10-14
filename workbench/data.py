from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
from pathlib import Path

import duckdb
from charset_normalizer import from_bytes
from filelock import FileLock

from workbench.query import database_schema, quote, sql_preview
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, atomic_json, safe_path


def decode_csv(content: bytes) -> tuple[str, str, list[str]]:
    warnings = []
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16"), "utf-16", warnings
    try:
        return content.decode("utf-8-sig"), "utf-8", warnings
    except UnicodeDecodeError:
        if b"\x00" in content:
            raise ValueError("This file looks binary, not a CSV")
        match = from_bytes(content[:1_000_000]).best()
        if match is None or match.percent_chaos > 10:
            raise ValueError("Unable to identify text encoding; save the CSV as UTF-8")
        try:
            text = content.decode(match.encoding)
        except UnicodeDecodeError as error:
            raise ValueError("Inconsistent encoding; save the CSV as UTF-8") from error
        warnings.append(f"Encoding inferred as {match.encoding}; verify accented characters against original source")
        return text, match.encoding, warnings


def masked(value, column: str = ""):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return "<nested value masked>"
    if re.search(r"id$|account|name|email|phone|address|customer|merchant|destination|ssn|pan|card", column, re.I):
        return "masked_" + hashlib.sha256(str(value).encode()).hexdigest()[:8]
    if isinstance(value, str):
        # Conservative: arbitrary text samples can contain identifiers and prompt injection.
        return f"<text length={len(value)}>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value if isinstance(value, (int, float, bool)) else str(value)


def ingest_csv(workspace: Path, content: bytes, filename: str, settings: Settings) -> dict:
    manager = WorkspaceManager(settings.workspace_root)
    if manager.get(workspace.name) != workspace.resolve():
        raise ValueError("Invalid workspace")
    if "/" in filename or "\\" in filename or ":" in filename or filename in {"", ".", ".."} or "\x00" in filename:
        raise ValueError("Upload filename must not contain a path")
    if not content or not content.strip():
        raise ValueError("CSV is empty")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"CSV exceeds {settings.max_upload_mb} MB upload limit")
    if not filename.lower().endswith(".csv"):
        raise ValueError("Upload a .csv file")
    with FileLock(str(workspace / "metadata/workflow.lock"), timeout=0):
        if (workspace / "input/source.csv").exists():
            raise ValueError("A workspace accepts one immutable source CSV; create a new workspace")
        try:
            text, encoding, warnings = decode_csv(content)
            if "\x00" in text or any(ord(char) < 9 for char in text[:100000]):
                raise ValueError("CSV contains binary/control characters")
            try:
                dialect = csv.Sniffer().sniff(text[:65_536], delimiters=",;\t|")
                delimiter = dialect.delimiter
            except csv.Error:
                delimiter = ","
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            header = next(reader, [])
            if not header or any(not name.strip() for name in header):
                raise ValueError("CSV needs a nonempty header for every column")
            if len(header) > 200:
                raise ValueError("This MVP supports up to 200 columns per dataset")
            if len(set(name.casefold() for name in header)) != len(header):
                raise ValueError("CSV column names must be unique, ignoring case")
            if any(len(name) > 200 for name in header):
                raise ValueError("Column names must be at most 200 characters")
            source = safe_path(workspace, "input/source.csv")
            source.write_bytes(content)
            normalized = safe_path(workspace, "input/normalized.csv")
            normalized.write_text(text, encoding="utf-8", newline="")
            with FileLock(str(workspace / "metadata/database.lock"), timeout=1):
                connection = duckdb.connect(str(workspace / "database/workspace.duckdb"), config={"threads":"2","memory_limit":"1GB"})
                try:
                    connection.execute("BEGIN")
                    for schema in ("raw", "staging", "features", "results", "audit"):
                        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                    connection.execute("CREATE TABLE raw.original AS SELECT * FROM read_csv(?, header=true, delim=?, all_varchar=true, sample_size=-1, strict_mode=true, ignore_errors=false)", [str(normalized), delimiter])
                    rows = connection.execute("SELECT count(*) FROM raw.original").fetchone()[0]
                    if rows == 0:
                        raise ValueError("CSV contains a header but no records")
                    description = connection.execute("DESCRIBE SELECT * FROM read_csv(?, header=true, delim=?, sample_size=-1, strict_mode=true, ignore_errors=false)", [str(normalized), delimiter]).fetchall()
                    expressions = []
                    conversion_failures = {}
                    inferred = {}
                    for name, dtype, *_ in description:
                        col = quote(name)
                        preserve = bool(re.search(r"id$|account|phone|postal|zip|card", name, re.I))
                        if dtype not in {"VARCHAR", "BOOLEAN", "DATE", "TIMESTAMP", "TIME"}:
                            zeros = connection.execute(f"SELECT count(*) FROM raw.original WHERE regexp_matches({col}, '^0[0-9]+$')").fetchone()[0]
                            preserve = preserve or zeros > 0
                        if preserve:
                            dtype = "VARCHAR"
                        if dtype != "VARCHAR":
                            failures = connection.execute(f"SELECT count(*) FROM raw.original WHERE {col} IS NOT NULL AND TRY_CAST({col} AS {dtype}) IS NULL").fetchone()[0]
                            conversion_failures[name] = failures
                            if failures:
                                warnings.append(f"{name}: {failures} conversion failures; retained original text")
                                dtype = "VARCHAR"
                        inferred[name] = dtype
                        expressions.append(f"CAST({col} AS {dtype}) AS {col}")
                    connection.execute("CREATE TABLE raw.source AS SELECT " + ",".join(expressions) + " FROM raw.original")
                    connection.execute("CREATE TABLE audit.ingestion (source_filename VARCHAR, sha256 VARCHAR, row_count BIGINT, encoding VARCHAR)")
                    sha256 = hashlib.sha256(content).hexdigest()
                    connection.execute("INSERT INTO audit.ingestion VALUES (?,?,?,?)", [filename, sha256, rows, encoding])
                    connection.execute("COMMIT")
                    manager.status(workspace, "DATA_INGESTED", row_count=rows)
                    manager.event(workspace, "DATA_INGESTED", f"Loaded {rows:,} rows into DuckDB")
                    columns = []
                    for name, dtype in inferred.items():
                        col = quote(name)
                        nulls, distinct = connection.execute(f"SELECT count(*) FILTER (WHERE {col} IS NULL), approx_count_distinct({col}) FROM raw.source").fetchone()
                        item = {"name": name, "type": dtype, "null_count": nulls, "distinct_count": distinct, "distinct_count_is_approximate": True, "conversion_failures": conversion_failures.get(name, 0)}
                        if dtype.startswith(("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "SMALLINT", "HUGEINT")):
                            mn, mx, avg = connection.execute(f"SELECT min({col}),max({col}),avg({col}) FROM raw.source").fetchone()
                            item["summary"] = {"min":masked(mn,name), "max":masked(mx,name), "mean":masked(avg,name)}
                        item["potential_identifier"] = bool(re.search(r"id$|account", name, re.I)) or (rows > 20 and distinct >= rows * 0.95)
                        item["timestamp_candidate"] = "DATE" in dtype or "TIME" in dtype
                        item["candidate_target"] = bool(re.search(r"target|label|outcome|class|ground_truth", name, re.I))
                        if distinct <= 30:
                            distribution = connection.execute(f"SELECT {col}, count(*) n FROM raw.source GROUP BY 1 ORDER BY n DESC LIMIT 10").fetchall()
                            item["distribution"] = [{"value": masked(value,name), "count":count} for value,count in distribution]
                        if nulls:
                            warnings.append(f"{name}: {nulls:,} missing values")
                        columns.append(item)
                    sample = connection.execute("SELECT * FROM raw.source LIMIT 5").fetchall()
                    # Exact on bounded samples only; full data count is explicitly omitted for large inputs.
                    duplicate_count = rows - connection.execute("SELECT count(*) FROM (SELECT DISTINCT * FROM raw.source)").fetchone()[0] if rows <= 100_000 else None
                    if duplicate_count is None:
                        warnings.append("Exact duplicate count skipped above 100,000 rows")
                    profile = {"row_count": rows, "columns": columns, "warnings": warnings, "samples": [{name: masked(value,name) for name,value in zip(inferred,row)} for row in sample], "source_filename": filename, "encoding": encoding, "delimiter": delimiter, "source_sha256": sha256, "duplicate_count": duplicate_count, "sample_policy": "Identifier-like values hashed; all other strings represented only by length. Original data retained locally."}
                finally:
                    connection.close()
                # Host-only pristine database: each execution attempt starts from the same input.
                shutil.copy2(workspace / "database/workspace.duckdb", workspace / "metadata/ingested.duckdb")
            atomic_json(workspace / "metadata/profile.json", profile)
            manager.status(workspace, "DATA_PROFILED", row_count=rows)
            manager.event(workspace, "DATA_PROFILED", "Dataset profile saved; agent samples masked")
            return profile
        except Exception as error:
            manager.status(workspace, "FAILED", error=str(error)[:2000])
            manager.event(workspace, "FAILED", "CSV ingestion failed", error=str(error)[:2000])
            raise
