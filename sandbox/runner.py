"""Trusted image runner; this file is used only inside Docker."""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/workspace")
MARKER = "WORKBENCH_INSPECTION_JSON="


def safe_file(relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.parts[0] not in {"results", "models", "notebooks", "generated", "input"}:
        raise ValueError("Artifact path is outside permitted directories")
    target = ROOT / path
    if any(item.is_symlink() for item in [target, *target.parents] if item != ROOT.parent):
        raise ValueError("Symlink artifacts are forbidden")
    if not target.resolve().is_relative_to(ROOT):
        raise ValueError("Artifact path escaped workspace")
    return target


def table_parts(value):
    parts = value.split(".")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise ValueError("Table must use a schema.table identifier")
    return parts, ".".join('"' + part + '"' for part in parts)


def inspect(contract):
    import duckdb
    results, artifacts = [], []
    database = ROOT / "database/workspace.duckdb"
    connection = duckdb.connect(str(database), read_only=True, config={"enable_external_access": False,
                                "autoload_known_extensions": False, "autoinstall_known_extensions": False})
    inventory = connection.execute("SELECT table_schema,table_name,table_type FROM information_schema.tables "
                                   "WHERE table_schema IN ('results','features','staging','audit') ORDER BY 1,2 LIMIT 201").fetchall()
    if len(inventory) > 200:
        raise ValueError("More than 200 derived tables/views were produced; validation inventory limit exceeded")
    for schema, name, kind in inventory:
        try:
            _, identifier = table_parts(f"{schema}.{name}")
            count = connection.execute(f"SELECT count(*) FROM {identifier}").fetchone()[0]
            columns = [{"name": c[0], "type": c[1]} for c in connection.execute(
                "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                [schema, name]).fetchall()]
            artifacts.append({"name": name, "type": "database_view" if kind == "VIEW" else "database_table",
                              "location": f"{schema}.{name}", "row_count": count, "columns": columns, "preview_enabled": True})
        except Exception as exc:
            artifacts.append({"name": name, "type": "database_view" if kind == "VIEW" else "database_table",
                              "location": f"{schema}.{name}", "preview_enabled": False, "error": str(exc)[:1000]})
    for check in contract.get("checks", []):
        item = {"name": check["name"], "kind": check["kind"], "target": check["target"], "passed": False}
        try:
            kind, target = check["kind"], check["target"]
            if kind in {"row_count", "columns"} and "/" in target:
                import pyarrow.csv
                import pyarrow.parquet
                path = safe_file(target)
                if path.suffix.lower() == ".parquet":
                    document = pyarrow.parquet.ParquetFile(path)
                    count = document.metadata.num_rows
                    columns = document.schema.names
                elif path.suffix.lower() == ".csv":
                    with pyarrow.csv.open_csv(path) as document:
                        columns = document.schema.names
                        count = sum(batch.num_rows for batch in document)
                else:
                    raise ValueError("Tabular file inspection supports CSV and Parquet")
                if check.get("min_rows") is not None and count < check["min_rows"]:
                    raise ValueError(f"Expected at least {check['min_rows']} rows; found {count}")
                missing = sorted(set(check.get("required_columns", [])) - set(columns))
                if missing:
                    raise ValueError(f"Missing required columns: {missing}")
                item.update(passed=True, evidence={"row_count": count, "columns": columns})
            elif kind in {"table_exists", "row_count", "columns"}:
                (schema, name), identifier = table_parts(target)
                found = connection.execute("SELECT table_type FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                                           [schema, name]).fetchone()
                if not found:
                    raise ValueError("Expected table/view does not exist")
                if check.get("required_table_type") and found[0] != check["required_table_type"]:
                    raise ValueError(f"Expected {check['required_table_type']}; found {found[0]}")
                count = connection.execute(f"SELECT count(*) FROM {identifier}").fetchone()[0]
                columns = [r[0] for r in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position", [schema, name]).fetchall()]
                if check.get("min_rows") is not None and count < check["min_rows"]:
                    raise ValueError(f"Expected at least {check['min_rows']} rows; found {count}")
                missing = sorted(set(check.get("required_columns", [])) - set(columns))
                if missing:
                    raise ValueError(f"Missing required columns: {missing}")
                item.update(passed=True, evidence={"row_count": count, "columns": columns, "table_type": found[0]})
            elif kind == "sql_assertion":
                sql = check.get("sql") or ""
                statements = connection.extract_statements(sql)
                if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                    raise ValueError("Validation SQL must contain exactly one read-only SELECT")
                values = connection.execute(sql).fetchmany(2)
                if len(values) != 1 or len(values[0]) != 1 or values[0][0] not in (True, 1):
                    raise ValueError(f"SQL assertion must return one TRUE value; got {values!r}"[:1000])
                item.update(passed=True, evidence={"result": True, "sql": sql[:3000]})
            elif kind == "model_load":
                path = safe_file(target)
                if path.suffix.lower() not in {".joblib", ".pkl", ".pickle"}:
                    raise ValueError("Model load validation currently supports joblib/pickle; add an approved inspector for other formats")
                # Pickle is executable. The child cannot mutate this interpreter's validation/report state.
                # Discard child stdout, including any forged inspection-marker messages.
                with tempfile.TemporaryDirectory(prefix="model-inspect-") as directory:
                    report = Path(directory) / "metadata.json"
                    with tempfile.TemporaryFile() as errors:
                        completed = subprocess.run([sys.executable, "-I", __file__, "model", target, str(report)],
                                                   stdout=subprocess.DEVNULL, stderr=errors, timeout=60)
                        if completed.returncode:
                            errors.seek(0)
                            raise ValueError("Isolated model loader failed: " + errors.read(2000).decode(errors="replace"))
                    if not report.is_file() or report.stat().st_size > 65536:
                        raise ValueError("Isolated model loader did not return bounded metadata")
                    reported = json.loads(report.read_text())
                metadata = {"class": str(reported.get("class", ""))[:200], "module": str(reported.get("module", ""))[:200],
                            "bytes": path.stat().st_size, "load_exit_code": 0,
                            "metadata_source": "Model-reported metadata from isolated loader; not trusted validation facts"}
                if isinstance(reported.get("n_features_in"), int):
                    metadata["n_features_in"] = reported["n_features_in"]
                if isinstance(reported.get("pipeline_steps"), list):
                    metadata["pipeline_steps"] = [str(step)[:200] for step in reported["pipeline_steps"][:100]]
                item.update(passed=True, evidence=metadata)
            else:
                raise ValueError(f"Unsupported sandbox check: {kind}")
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        results.append(item)
    connection.close()
    print(MARKER + json.dumps({"checks": results, "artifacts": artifacts}, default=str), flush=True)


def execute(contract):
    from packaging.requirements import Requirement
    aliases = {"sklearn": "scikit-learn", "PIL": "Pillow", "cv2": "opencv-python"}
    for dependency in contract.get("required_dependencies", []):
        if dependency in sys.stdlib_module_names:
            continue
        requested = Requirement(aliases.get(dependency, dependency))
        try:
            version = importlib.metadata.version(requested.name)
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError(f"Dependency {requested.name} is not installed in the approved sandbox image. No packages are installed during execution.")
        if requested.specifier and not requested.specifier.contains(version):
            raise RuntimeError(f"Installed {requested.name} {version} does not satisfy {requested.specifier}")
    if contract.get("entrypoint") != "generated/solution.py":
        raise ValueError("Unapproved entry point")
    # A child interpreter prevents generated code from accidentally replacing runner globals.
    return subprocess.call([sys.executable, "-B", "generated/solution.py"], cwd=ROOT)


def model_metadata(target, report):
    import joblib
    model = joblib.load(safe_file(target))
    metadata = {"class": type(model).__name__, "module": type(model).__module__}
    if hasattr(model, "n_features_in_"):
        metadata["n_features_in"] = int(model.n_features_in_)
    if hasattr(model, "named_steps"):
        metadata["pipeline_steps"] = list(model.named_steps)
    Path(report).write_text(json.dumps(metadata), encoding="utf-8")


if __name__ == "__main__":
    # Some WSL kernels cannot cap cgroup swap. Also enforce an inherited hard address-space
    # ceiling per process; generated children cannot raise it without CAP_SYS_RESOURCE.
    import resource
    memory_limit = int(os.environ["SANDBOX_MEMORY_BYTES"])
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    if sys.argv[1] == "model":
        model_metadata(sys.argv[2], sys.argv[3])
        raise SystemExit(0)
    contract = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    if sys.argv[1] == "inspect":
        inspect(contract)
    elif sys.argv[1] == "execute":
        raise SystemExit(execute(contract))
    else:
        raise SystemExit("Unknown runner mode")
