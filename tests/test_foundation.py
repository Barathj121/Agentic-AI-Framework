import json
from pathlib import Path

import pytest

from workbench.data import ingest_csv
from workbench.query import database_schema, sql_preview
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager, safe_path


@pytest.fixture
def dataset(tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.create("Sales", "Calculate totals by region")
    profile = ingest_csv(workspace, b"customer_id,amount,region\n001,12.5,East\n002,,West\n003,25,East\n", "sales.csv", settings)
    return settings, manager, workspace, profile


def test_ingest_persistence_and_masking(dataset):
    settings, manager, workspace, profile = dataset
    assert profile["row_count"] == 3
    assert profile["columns"][0]["type"] == "VARCHAR"
    assert profile["columns"][1]["null_count"] == 1
    assert profile["samples"][0]["customer_id"].startswith("masked_")
    assert "001" not in json.dumps(profile["samples"])
    assert sql_preview(workspace, 'SELECT customer_id, amount FROM raw.source ORDER BY customer_id')["rows"][0] == ["001", 12.5]
    assert WorkspaceManager(settings.workspace_root).list()[0]["status"] == "DATA_PROFILED"
    assert {x["name"] for x in database_schema(workspace)} >= {"source", "original", "ingestion"}
    assert sql_preview(workspace, "SELECT count(*) FROM raw.original")["rows"] == [[3]]


@pytest.mark.parametrize("path", ["../outside", "C:\\secret", "/etc/passwd", "generated/../../oops", "input\\..\\oops", "results/file.txt:stream"])
def test_path_escape_rejected(dataset, path):
    with pytest.raises(ValueError):
        safe_path(dataset[2], path)


@pytest.mark.parametrize("sql", ["DELETE FROM raw.source", "SELECT 1; DROP TABLE raw.source", "COPY raw.source TO 'out.csv'", "SELECT * FROM read_csv('secret.csv')", "ATTACH 'elsewhere.db'", "SELECT * INTO tmp FROM raw.source"])
def test_sql_write_and_external_sources_rejected(dataset, sql):
    with pytest.raises(ValueError):
        sql_preview(dataset[2], sql)


def test_readonly_sql_and_pagination(dataset):
    result = sql_preview(dataset[2], "SELECT * FROM raw.source ORDER BY customer_id", limit=1, offset=1)
    assert result["truncated"]
    assert result["rows"][0][0] == "002"
    assert sql_preview(dataset[2], "WITH totals AS (SELECT sum(amount) n FROM raw.source) SELECT n FROM totals")["rows"] == [[37.5]]


@pytest.mark.parametrize("content,filename", [(b"", "a.csv"),(b"x,y\n", "a.csv"),(b"x\n1\n", "../a.csv"),(b"x,x\n1,2", "a.csv"),(b"\x00\x01binary", "a.csv"),(b"x\n1", "data.exe")])
def test_invalid_inputs(tmp_path, content, filename):
    settings = Settings(workspace_root=tmp_path)
    workspace = WorkspaceManager(tmp_path).create("Bad input", "Test")
    with pytest.raises((ValueError, Exception)):
        ingest_csv(workspace, content, filename, settings)


def test_mixed_types_and_semicolon_utf16(tmp_path):
    settings = Settings(workspace_root=tmp_path)
    workspace = WorkspaceManager(tmp_path).create("Mixed", "Inspect")
    profile = ingest_csv(workspace, "id;value\n001;10\n002;unknown\n".encode("utf-16"), "mixed.csv", settings)
    assert profile["encoding"] == "utf-16"
    assert profile["columns"][1]["type"] == "VARCHAR"
    assert sql_preview(workspace, "SELECT value FROM raw.source ORDER BY id")["rows"] == [["10"], ["unknown"]]


def test_large_upload_rejected_before_write(tmp_path):
    settings = Settings(workspace_root=tmp_path, max_upload_mb=1)
    workspace = WorkspaceManager(tmp_path).create("Limit", "Test")
    with pytest.raises(ValueError, match="limit"):
        ingest_csv(workspace, b"x\n" + b"1\n" * 600_000, "large.csv", settings)
    assert not (workspace / "input/source.csv").exists()


def test_input_is_immutable(dataset):
    settings, _, workspace, _ = dataset
    original = (workspace / "input/source.csv").read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        ingest_csv(workspace, b"x\n1", "replace.csv", settings)
    assert (workspace / "input/source.csv").read_bytes() == original


def test_cross_workspace_manager_access_denied(dataset, tmp_path):
    with pytest.raises(ValueError):
        WorkspaceManager(tmp_path / "other").read(dataset[2], "metadata/workspace.json")


def test_retry_starts_from_original_and_preserves_prior_outputs(dataset):
    import duckdb
    from workbench.sandbox import SandboxManager
    _, _, workspace, _ = dataset
    with duckdb.connect(str(workspace / "database/workspace.duckdb")) as connection:
        connection.execute("CREATE TABLE results.old_output AS SELECT 42 n")
    (workspace / "results/old.csv").write_text("n\n42\n")
    SandboxManager._fresh_attempt(workspace, 1)
    assert not (workspace / "results/old.csv").exists()
    assert list((workspace / "metadata/prior_outputs").glob("*/results/old.csv"))
    assert "old_output" not in {table["name"] for table in database_schema(workspace)}
    assert sql_preview(workspace, "SELECT count(*) FROM raw.source")["rows"] == [[3]]
