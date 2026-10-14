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


@pytest.mark.parametrize("endpoint", ["http://localhost:1234/v1", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1",
                                     "http://10.1.2.3:8080/v1", "https://192.168.1.10/v1", "http://172.20.0.2/v1", "http://[fd00::2]/v1"])
def test_local_model_endpoint_validation_accepts_loopback_and_private_lan(tmp_path, endpoint):
    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="exact/model-id", llm_base_url=endpoint)
    assert settings.llm_base_url == endpoint


@pytest.mark.parametrize("endpoint", ["https://api.openai.com/v1", "https://8.8.8.8/v1", "http://169.254.169.254/v1",
                                     "http://localhost.attacker.example/v1", "http://0.0.0.0/v1", "ftp://localhost/v1",
                                     "http://secret:credential@localhost/v1", "http://localhost/v1?key=secret", "http://localhost/v1#fragment"])
def test_model_endpoint_validation_rejects_cloud_and_embedded_credentials(tmp_path, endpoint):
    with pytest.raises(ValueError):
        Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="fixture", llm_base_url=endpoint)


def test_provider_configuration_is_explicit_and_preserves_default_ollama(tmp_path):
    assert Settings(workspace_root=tmp_path).llm_provider == "ollama"
    with pytest.raises(ValueError, match="LLM_MODEL"):
        Settings(workspace_root=tmp_path, llm_provider="openai_compatible")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings(workspace_root=tmp_path, llm_provider="cloud")
    with pytest.raises(ValueError, match="LLM_STRUCTURED_MODE"):
        Settings(workspace_root=tmp_path, llm_structured_mode="automatic")
    with pytest.raises(ValueError, match="Cloud models"):
        Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="remote:cloud")
    with pytest.raises(ValueError, match="LLM_MAX_OUTPUT_TOKENS"):
        Settings(workspace_root=tmp_path, llm_context_tokens=4096, llm_max_output_tokens=4096)


def test_provider_environment_configuration_preserves_exact_model_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "owner/gemma-exact:q4_0")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1/")
    monkeypatch.setenv("LLM_STRUCTURED_MODE", "json_object")
    monkeypatch.setenv("LLM_API_KEY", "fixture-secret")
    settings = Settings.from_env()
    assert settings.llm_model == "owner/gemma-exact:q4_0"
    assert settings.llm_base_url == "http://127.0.0.1:1234/v1"
    assert settings.llm_structured_mode == "json_object"
    assert settings.llm_api_key == "fixture-secret"
    assert "fixture-secret" not in repr(settings)


def test_background_dispatch_does_not_persist_model_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from workbench.service import Service

    captured = {}

    def spawn(*arguments, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=123456789)

    monkeypatch.setattr("workbench.service.subprocess.Popen", spawn)
    secret = "fixture-server-token"
    settings = Settings(workspace_root=tmp_path, llm_provider="openai_compatible", llm_model="exact-model", llm_api_key=secret)
    service = Service(settings)
    workspace = service.manager.create("Credential handling", "Inspect request persistence")
    service._dispatch(workspace.name, "propose")
    request_path = next((workspace / "metadata").glob("request_*.json"))
    request_text = request_path.read_text(encoding="utf-8")
    assert secret not in request_text
    assert "llm_api_key" not in json.loads(request_text)["settings"]
    assert captured["env"]["LLM_API_KEY"] == secret
    assert secret not in (workspace / "logs/agent.jsonl").read_text(encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", captured["env"]["LLM_API_KEY"])
    restored = Settings(**json.loads(request_text)["settings"])
    assert restored.llm_api_key == secret
