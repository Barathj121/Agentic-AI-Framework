import json
from pathlib import Path

import nbformat
import pytest

from workbench.artifacts import validate_and_register
from workbench.data import ingest_csv
from workbench.schemas import BuildResult, ExecutionResult, GeneratedFile, OutputSpec
from workbench.settings import Settings
from workbench.workspace import WorkspaceManager


@pytest.fixture
def artifact_workspace(tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    workspace = WorkspaceManager(settings.workspace_root).create("artifact test", "Compute totals")
    ingest_csv(workspace, b"id,value\n1,10\n2,20\n", "data.csv", settings)
    (workspace / "generated/solution.py").write_text("# fixture source; not executed\n")
    (workspace / "logs/out.log").write_text("actual fixture log")
    (workspace / "logs/err.log").write_text("")
    execution = ExecutionResult(success=True, exit_code=0, execution_time_seconds=1,
                                stdout_path="logs/out.log", stderr_path="logs/err.log")
    return workspace, settings, execution


class StubInspector:
    def __init__(self):
        self.requested = []

    def inspect(self, workspace, checks):
        self.requested = checks
        return {"checks": [{**c, "passed": True, "evidence": {"source": "unit-test fixture"}} for c in checks],
                "artifacts": [{"name": "totals", "type": "database_table", "location": "results.totals", "row_count": 2}]}


def make_build(outputs):
    return BuildResult(files=[GeneratedFile(path="generated/solution.py", content="# fixture")], expected_outputs=outputs)


def test_registers_tables_notebook_source_and_unknown_file(artifact_workspace):
    workspace, settings, execution = artifact_workspace
    (workspace / "results/custom.xyz").write_bytes(b"unknown but real output")
    build = make_build([OutputSpec(name="Totals", type="database_table", location="results.totals", required_columns=["value"], min_rows=1)])
    inspector = StubInspector()
    result = validate_and_register(workspace, build, execution, settings, inspector)
    assert result["passed"], result["errors"]
    assert {x["type"] for x in result["artifacts"]} >= {"database_table", "notebook", "python_script", "binary_file"}
    assert inspector.requested[0]["required_columns"] == ["value"]
    notebook = nbformat.read(workspace / "notebooks/solution.ipynb", as_version=4)
    assert notebook.metadata.workbench.executed is False
    assert all(c.execution_count is None for c in notebook.cells if c.cell_type == "code")
    assert "actual fixture log" in str(notebook.cells)
    assert json.loads((workspace / "metadata/manifest.json").read_text())["status"] == "COMPLETED"


def test_model_is_only_inspected_through_sandbox(artifact_workspace):
    workspace, settings, execution = artifact_workspace
    # Deliberately not a pickle: unit test proves host validation never deserializes the bytes.
    (workspace / "models/model.joblib").write_bytes(b"untrusted bytes")
    inspector = StubInspector()
    build = make_build([OutputSpec(name="Model", type="model", location="models/model.joblib")])
    result = validate_and_register(workspace, build, execution, settings, inspector)
    assert result["passed"]
    assert any(c["kind"] == "model_load" for c in inspector.requested)
    assert any(a["type"] == "model" for a in result["artifacts"])
    import hashlib
    registered = next(a for a in result["artifacts"] if a["location"] == "models/model.joblib")
    assert registered["sha256"] == hashlib.sha256(b"untrusted bytes").hexdigest()


def test_missing_and_empty_outputs_fail(artifact_workspace):
    workspace, settings, execution = artifact_workspace
    (workspace / "results/empty.csv").touch()
    build = make_build([OutputSpec(name="Missing", type="json", location="results/missing.json"),
                        OutputSpec(name="Empty", type="csv", location="results/empty.csv")])
    result = validate_and_register(workspace, build, execution, settings, StubInspector())
    assert not result["passed"]
    assert any("does not exist" in error for error in result["errors"])
    assert any("empty" in error for error in result["errors"])
    assert all(a["location"] != "results/missing.json" for a in result["artifacts"])


def test_zero_exit_does_not_override_validation_failure(artifact_workspace):
    workspace, settings, execution = artifact_workspace
    (workspace / "results/bad.json").write_text("not JSON")
    build = make_build([OutputSpec(name="Metrics", type="json", location="results/bad.json")])
    result = validate_and_register(workspace, build, execution, settings, StubInspector())
    assert not result["passed"]
    assert any("JSONDecodeError" in error for error in result["errors"])


def test_inspector_cannot_omit_declared_checks(artifact_workspace):
    workspace, settings, execution = artifact_workspace

    class MissingInspector:
        def inspect(self, *args):
            return {"checks": [], "artifacts": []}

    result = validate_and_register(workspace, make_build([OutputSpec(name="Required table", type="database_table", location="results.totals")]),
                                   execution, settings, MissingInspector())
    assert not result["passed"]
    assert any("no evidence" in error for error in result["errors"])


@pytest.mark.docker
def test_real_table_model_and_artifact_validation(artifact_workspace):
    from workbench.sandbox import SandboxManager
    workspace, settings, _ = artifact_workspace
    source = """import duckdb, joblib, json
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import IsolationForest
with duckdb.connect('database/workspace.duckdb') as con:
    df = con.execute('SELECT value FROM raw.source').df()
    con.execute('CREATE TABLE results.totals AS SELECT sum(value) AS total FROM raw.source')
model = make_pipeline(StandardScaler(), IsolationForest(n_estimators=5, random_state=42))
model.fit(df)
joblib.dump(model, 'models/model.joblib')
with open('results/metrics.json','w') as f:
    json.dump({'training_rows': len(df), 'random_seed': 42}, f)
"""
    (workspace / "generated/solution.py").write_text(source)
    build = BuildResult(files=[GeneratedFile(path="generated/solution.py", content=source)],
                        expected_outputs=[OutputSpec(name="Totals", type="database_table", location="results.totals", required_columns=["total"], min_rows=1),
                                          OutputSpec(name="Model", type="model", location="models/model.joblib"),
                                          OutputSpec(name="Metrics", type="json", location="results/metrics.json")])
    sandbox = SandboxManager(settings)
    execution = sandbox.execute(workspace, build, 1)
    assert execution.success, (workspace / execution.stderr_path).read_text()
    result = validate_and_register(workspace, build, execution, settings, sandbox)
    assert result["passed"], result["errors"]
    model = next(a for a in result["artifacts"] if a["type"] == "model")
    assert model["metadata"]["class"] == "Pipeline"
    assert model["metadata"]["pipeline_steps"] == ["standardscaler", "isolationforest"]


@pytest.mark.docker
def test_model_cannot_replace_trusted_inspection_report(artifact_workspace):
    from workbench.sandbox import SandboxManager
    workspace, settings, _ = artifact_workspace
    source = '''import pickle
class Payload:
    def __reduce__(self):
        return (exec, ("import __main__; __main__.MARKER = 'FORGED='; print('WORKBENCH_INSPECTION_JSON={\\\"checks\\\": [], \\\"artifacts\\\": []}')",))
with open('models/payload.pkl', 'wb') as target:
    pickle.dump(Payload(), target)
'''
    (workspace / "generated/solution.py").write_text(source)
    build = BuildResult(files=[GeneratedFile(path="generated/solution.py", content=source)],
                        expected_outputs=[OutputSpec(name="Payload", type="model", location="models/payload.pkl")])
    sandbox = SandboxManager(settings)
    execution = sandbox.execute(workspace, build, 1)
    assert execution.success, (workspace / execution.stderr_path).read_text()
    result = sandbox.inspect(workspace, [{"name": "Load", "kind": "model_load", "target": "models/payload.pkl"},
                                        {"name": "Must fail", "kind": "sql_assertion", "target": "results", "sql": "SELECT FALSE"}])
    assert not result.get("error"), result
    assert len(result["checks"]) == 2
    assert result["checks"][0]["passed"]
    assert not result["checks"][1]["passed"]


@pytest.mark.docker
def test_inventory_overflow_fails_explicitly(artifact_workspace):
    from workbench.sandbox import SandboxManager
    workspace, settings, _ = artifact_workspace
    source = "import duckdb\nwith duckdb.connect('database/workspace.duckdb') as con:\n    for i in range(201):\n        con.execute(f'CREATE TABLE results.table_{i} AS SELECT 1 AS value')\n"
    (workspace / "generated/solution.py").write_text(source)
    build = BuildResult(files=[GeneratedFile(path="generated/solution.py", content=source)],
                        expected_outputs=[OutputSpec(name="Data", type="database_table", location="results.table_0")])
    sandbox = SandboxManager(settings)
    execution = sandbox.execute(workspace, build, 1)
    assert execution.success, (workspace / execution.stderr_path).read_text()
    result = sandbox.inspect(workspace, [])
    assert result.get("error")
    assert result["artifacts"] == []
