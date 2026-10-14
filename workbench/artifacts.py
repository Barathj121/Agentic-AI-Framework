"""Evidence-based output validation, notebooks, and a format-agnostic registry."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import nbformat
from filelock import FileLock

from .workspace import atomic_json, read_json, safe_path

MAX_INSPECT_BYTES = 8 * 1024 * 1024
ARTIFACT_DIRECTORIES = {"generated", "results", "models", "notebooks"}


def _artifact_file(workspace: Path, location: str) -> Path:
    path = safe_path(workspace, location)
    if Path(location).parts[0] not in ARTIFACT_DIRECTORIES:
        raise ValueError("Artifact must be under generated, results, models, or notebooks")
    if not path.is_file():
        raise ValueError(f"Expected artifact does not exist: {location}")
    return path


def _read_bounded(path: Path, maximum=MAX_INSPECT_BYTES) -> str:
    if path.stat().st_size > maximum:
        raise ValueError(f"Artifact exceeds {maximum} byte structural inspection limit")
    return path.read_text(encoding="utf-8")


def _file_type(path: Path) -> str:
    return {".csv": "csv", ".json": "json", ".ipynb": "notebook", ".py": "python_script",
            ".sql": "sql_script", ".txt": "text_report", ".md": "text_report",
            ".html": "text_report", ".png": "chart", ".jpg": "chart", ".jpeg": "chart",
            ".svg": "chart", ".joblib": "model", ".pkl": "model", ".pickle": "model"}.get(path.suffix.lower(), "binary_file")


def _identity(location: str) -> str:
    return "artifact_" + hashlib.sha256(location.encode()).hexdigest()[:16]


def _host_check(workspace, check):
    item = {"name": check["name"], "kind": check["kind"], "target": check["target"], "passed": False}
    try:
        path = _artifact_file(workspace, check["target"])
        size = path.stat().st_size
        evidence = {"bytes": size}
        if check["kind"] == "file_exists":
            if not check.get("allow_empty", False) and size == 0:
                raise ValueError("Output file is empty")
        elif check["kind"] == "json_valid":
            document = json.loads(_read_bounded(path))
            evidence["root_type"] = type(document).__name__
            if isinstance(document, dict):
                evidence["keys"] = list(document)[:100]
        elif check["kind"] == "notebook_valid":
            notebook = nbformat.reads(_read_bounded(path), as_version=4)
            nbformat.validate(notebook)
            code_cells = [c for c in notebook.cells if c.cell_type == "code"]
            errors = [out for cell in code_cells for out in cell.get("outputs", []) if out.get("output_type") == "error"]
            if errors:
                raise ValueError("Notebook contains unresolved execution errors")
            evidence.update(cells=len(notebook.cells), code_cells=len(code_cells),
                            executed=bool(code_cells) and all(c.get("execution_count") is not None for c in code_cells),
                            scope="Notebook structure and saved outputs; this does not claim that cells executed")
        else:
            raise ValueError(f"Unsupported host structural check: {check['kind']}")
        item.update(passed=True, evidence=evidence)
    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    return item


def _make_notebook(workspace, build, execution, checks):
    metadata = read_json(safe_path(workspace, "metadata/workspace.json"), {}) or {}
    profile = read_json(safe_path(workspace, "metadata/profile.json"), {}) or {}
    approval = read_json(safe_path(workspace, "metadata/selected_plan.json"), {}) or {}
    proposal = approval.get("proposal", approval.get("selected_proposal", approval))
    stdout_path = safe_path(workspace, execution.stdout_path)
    stderr_path = safe_path(workspace, execution.stderr_path)
    excerpts = {}
    for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        if path.is_file():
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                excerpts[label] = stream.read(12000)
    schemas = [{"name": c.get("name"), "type": c.get("type")} for c in profile.get("columns", [])]
    details = [{"path": f.path, "bytes": len(f.content.encode("utf-8"))} for f in build.files]
    summary = {"exit_code": execution.exit_code, "process_success": execution.success,
               "duration_seconds": execution.execution_time_seconds, "error": execution.error,
               "checks": checks}
    notebook = nbformat.v4.new_notebook()
    notebook.metadata.update({"kernelspec": {"display_name": "Python 3 (sandbox)", "language": "python", "name": "python3"},
                              "language_info": {"name": "python", "version": "3.11"},
                              "workbench": {"workspace_id": workspace.name, "executed": False,
                                            "execution_note": "Recorded module execution evidence. Notebook cells have not been executed."}})
    notebook.cells = [
        nbformat.v4.new_markdown_cell("# Solution notebook\n\n## Problem\n\n" + str(metadata.get("problem", ""))),
        nbformat.v4.new_markdown_cell("## Dataset summary\n\n```json\n" + json.dumps({"rows": profile.get("row_count"), "columns": schemas,
                                       "warnings": profile.get("warnings", [])}, indent=2, default=str) + "\n```"),
        nbformat.v4.new_markdown_cell("## Approved solution approach\n\n```json\n" + json.dumps(proposal, indent=2, default=str) + "\n```"),
        nbformat.v4.new_markdown_cell("## Implementation details\n\nAuthoritative reusable modules and SQL files:\n\n```json\n" +
                                     json.dumps(details, indent=2) + "\n```\n\n" + "\n".join(build.notes)),
        nbformat.v4.new_markdown_cell("## Reproduce inside the sandbox\n\nThe following cells are intentionally unexecuted. "
                                     "Run this notebook only in the approved Docker environment with workspace mounts. "
                                     "Rerunning the module may replace generated outputs."),
        nbformat.v4.new_code_cell("from pathlib import Path\nimport os, runpy, sys\n"
                                  "assert Path('/.dockerenv').exists(), 'Run generated code only inside the approved sandbox'\n"
                                  "os.chdir('/workspace')\nsys.path.insert(0, '/workspace/generated')\n"
                                  "runpy.run_path('generated/solution.py', run_name='__main__')"),
        nbformat.v4.new_markdown_cell("## Validation steps\n\nChecks below were evaluated against the actual execution and outputs. "
                                     "Structural checks alone do not establish statistical validity or causal claims.\n\n```json\n" +
                                     json.dumps(checks, indent=2, default=str) + "\n```"),
        nbformat.v4.new_markdown_cell("## Results\n\nCaptured module execution evidence (not notebook-cell execution):\n\n```json\n" +
                                     json.dumps(summary, indent=2, default=str) + "\n```\n\n### Log excerpts\n\n```text\n" +
                                     json.dumps(excerpts, indent=2, ensure_ascii=False) + "\n```"),
        nbformat.v4.new_markdown_cell("## Limitations\n\n" + "\n".join("- " + str(x) for x in
                                     (proposal.get("limitations", []) if isinstance(proposal, dict) else [])) +
                                     "\n- The generated descriptions are model suggestions; only recorded runtime evidence is computed."
                                     "\n- This notebook has not itself been executed; reusable module execution is recorded above."
                                     "\n- Model metrics, assumptions, train/test separation, and domain conclusions require review."),
    ]
    for check in build.validation_checks:
        if check.kind == "sql_assertion" and check.sql:
            notebook.cells.append(nbformat.v4.new_code_cell(
                "import duckdb\n"
                "with duckdb.connect('/workspace/database/workspace.duckdb', read_only=True, config={'enable_external_access': False}) as con:\n"
                f"    result = con.execute({check.sql!r}).fetchall()\n"
                "    assert result == [(True,)] or result == [(1,)], result\n    print(result)"))
    nbformat.validate(notebook)
    path = safe_path(workspace, "notebooks/solution.ipynb")
    path.parent.mkdir(exist_ok=True)
    nbformat.write(notebook, path)


def validate_and_register(workspace, build, execution, settings, sandbox) -> dict:
    """Validate every declared output and check, and discover actual persisted artifacts."""
    checks = [{"name": "Container execution", "kind": "execution", "target": build.entrypoint,
               "passed": bool(execution.success), "evidence": execution.model_dump(mode="json")}]
    host_checks, isolated_checks = [], []
    for output in build.expected_outputs:
        if output.type in {"database_table", "database_view"}:
            isolated_checks.append({"name": output.name, "kind": "table_exists", "target": output.location,
                                    "required_table_type": "VIEW" if output.type == "database_view" else "BASE TABLE",
                                    "min_rows": output.min_rows if output.min_rows is not None else (0 if output.allow_empty else 1),
                                    "required_columns": output.required_columns})
        else:
            host_checks.append({"name": output.name, "kind": "file_exists", "target": output.location, "allow_empty": output.allow_empty})
            if output.type == "model":
                isolated_checks.append({"name": output.name + " load", "kind": "model_load", "target": output.location})
            elif output.type in {"json", "notebook"} or (output.type == "chart" and output.location.lower().endswith(".json")):
                host_checks.append({"name": output.name + " structure", "kind": "notebook_valid" if output.type == "notebook" else "json_valid", "target": output.location})
            if output.type in {"csv", "parquet"} and (output.required_columns or output.min_rows is not None or not output.allow_empty):
                isolated_checks.append({"name": output.name + " data", "kind": "row_count", "target": output.location,
                                        "min_rows": output.min_rows if output.min_rows is not None else (0 if output.allow_empty else 1),
                                        "required_columns": output.required_columns})
    for check in build.validation_checks:
        data = check.model_dump(mode="json")
        if check.kind in {"file_exists", "json_valid", "notebook_valid"}:
            host_checks.append(data)
        else:
            isolated_checks.append(data)
    # The standard solution notebook is generated after output inspection, using those real checks.
    notebook_checks = [c for c in host_checks if c["target"] == "notebooks/solution.ipynb"]
    for check in host_checks:
        if check not in notebook_checks:
            checks.append(_host_check(workspace, check))
    evidence = sandbox.inspect(workspace, isolated_checks)
    returned = evidence.get("checks", [])
    for expected in isolated_checks:
        matching = next((c for c in returned if c.get("name") == expected["name"] and c.get("target") == expected["target"]
                         and c.get("kind") == expected["kind"]), None)
        checks.append(matching or {"name": expected["name"], "kind": expected["kind"], "target": expected["target"],
                                   "passed": False, "error": evidence.get("error") or "Sandbox returned no evidence for this check"})
    if evidence.get("error") and not isolated_checks:
        checks.append({"name": "Output inventory", "kind": "inspection", "target": "database", "passed": False, "error": evidence["error"]})
    try:
        _make_notebook(workspace, build, execution, checks)
        checks.append(_host_check(workspace, {"name": "Solution notebook", "kind": "notebook_valid", "target": "notebooks/solution.ipynb"}))
    except Exception as exc:
        checks.append({"name": "Solution notebook", "kind": "notebook_valid", "target": "notebooks/solution.ipynb", "passed": False, "error": str(exc)})
    for check in notebook_checks:
        checks.append(_host_check(workspace, check))
    expected = {output.location: output for output in build.expected_outputs}
    artifacts = []
    for entry in evidence.get("artifacts", []):
        if not isinstance(entry, dict) or entry.get("type") not in {"database_table", "database_view"}:
            continue
        artifact = dict(entry)
        artifact["id"] = _identity(artifact["location"])
        if artifact["location"] in expected:
            artifact["name"] = expected[artifact["location"]].name
        artifacts.append(artifact)
    count = 0
    for directory in sorted(ARTIFACT_DIRECTORIES):
        folder = safe_path(workspace, directory)
        def bounded_files():
            for current, directories, files in os.walk(folder, followlinks=False):
                directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()
                                   and not (hasattr(Path(current) / d, "is_junction") and (Path(current) / d).is_junction())]
                for filename in files:
                    yield Path(current) / filename
        for path in bounded_files() if folder.exists() else []:
            count += 1
            if count > 500:
                break
            relative = path.relative_to(workspace).as_posix()
            try:
                safe = _artifact_file(workspace, relative)
                declared = expected.get(relative)
                artifact = {"id": _identity(relative), "name": declared.name if declared else path.name,
                            "type": declared.type if declared else _file_type(path), "location": relative,
                            "size_bytes": safe.stat().st_size, "preview_enabled": True}
                model_evidence = next((c.get("evidence") for c in checks if c.get("kind") == "model_load" and c.get("target") == relative and c.get("passed")), None)
                if model_evidence:
                    artifact["metadata"] = model_evidence
                artifacts.append(artifact)
            except (OSError, ValueError):
                continue
    if count > 500:
        checks.append({"name": "Artifact inventory limit", "kind": "inventory", "target": "workspace", "passed": False,
                       "error": "More than 500 artifact files were produced; inventory is capped"})
    passed = all(item.get("passed") is True for item in checks)
    errors = [f"{item['name']}: {item.get('error', 'validation failed')}" for item in checks if not item.get("passed")]
    for artifact in artifacts:
        relevant = [c for c in checks if c.get("target") == artifact["location"]]
        artifact["validation_status"] = "PASSED" if relevant and all(c.get("passed") for c in relevant) else "FAILED" if relevant else "DISCOVERED"
    # The inspection containers have exited. One host writer records runtime evidence.
    try:
        import duckdb
        with FileLock(str(safe_path(workspace, "metadata/database.lock")), timeout=5):
            if (read_json(safe_path(workspace, "metadata/sandbox.json"), {}) or {}).get("cleanup_failed"):
                raise RuntimeError("Sandbox cleanup is unconfirmed; database writes are paused")
            with duckdb.connect(str(safe_path(workspace, "database/workspace.duckdb")), config={
                "enable_external_access": False, "autoload_known_extensions": False,
                "autoinstall_known_extensions": False, "memory_limit": "256MB", "threads": 2,
            }) as connection:
                connection.execute("CREATE SCHEMA IF NOT EXISTS audit")
                connection.execute("CREATE TABLE IF NOT EXISTS audit.workbench_runs (recorded_at TIMESTAMP DEFAULT current_timestamp, success BOOLEAN, execution_json VARCHAR, checks_json VARCHAR)")
                connection.execute("INSERT INTO audit.workbench_runs(success,execution_json,checks_json) VALUES (?,?,?)", [passed, execution.model_dump_json(), json.dumps(checks, default=str)])
                connection.execute("CREATE TABLE IF NOT EXISTS audit.workbench_artifacts (artifact_id VARCHAR, name VARCHAR, type VARCHAR, location VARCHAR, validation_status VARCHAR)")
                connection.execute("DELETE FROM audit.workbench_artifacts")
                if artifacts:
                    connection.executemany("INSERT INTO audit.workbench_artifacts VALUES (?,?,?,?,?)", [(a["id"], a["name"], a["type"], a["location"], a["validation_status"]) for a in artifacts])
    except Exception as exc:
        passed = False
        error = f"Audit persistence failed: {exc}"
        errors.append(error)
        checks.append({"name": "Audit persistence", "kind": "audit", "target": "audit.workbench_runs", "passed": False, "error": error})
    result = {"passed": passed, "checks": checks, "artifacts": artifacts, "errors": errors}
    atomic_json(safe_path(workspace, "metadata/validation.json"), result)
    current = read_json(safe_path(workspace, "metadata/manifest.json"), {}) or {}
    current.update(workspace_id=workspace.name, status="COMPLETED" if passed else "FAILED", artifacts=artifacts,
                   validation_passed=passed, execution=execution.model_dump(mode="json"))
    atomic_json(safe_path(workspace, "metadata/manifest.json"), current)
    return result
