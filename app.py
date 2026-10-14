"""Local-first, approval-gated data solution workbench.

Start with: python -m streamlit run app.py
Generated programs are only executed by the Docker sandbox, never by this app.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.io as pio
import streamlit as st

from ui.helpers import (
    DOWNLOAD_BYTES, chart_has_external_resources, notebook_preview, proposals_from,
    quoted_table, read_json_preview, read_text,
)
from workbench.data import database_schema, sql_preview
from workbench.service import Service
from workbench.settings import Settings
from workbench.workspace import safe_path

PAGES = ["New workspace", "Solution proposals", "Execution monitor", "Results", "Workspace history"]
ACTIVE = {"PROPOSING", "UNDERSTANDING", "EXPLORING", "GENERATING_PROPOSALS", "CHECKING_FEASIBILITY",
          "BUILDING", "GENERATING", "EXECUTING", "VALIDATING", "REPAIRING", "RUNNING", "QUEUED", "APPROVED",
          "EVALUATING_FEASIBILITY", "PLAN_APPROVED", "GENERATING_SOLUTION"}


@st.cache_resource
def get_service() -> Service:
    return Service(Settings.from_env())


def navigate(page: str) -> None:
    st.session_state["next_page"] = page
    st.rerun()


def records_frame(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result.get("rows", []), columns=result.get("columns", []))


def profile_panel(service: Service, workspace: Path) -> None:
    profile = service.manager.read(workspace, "metadata/profile.json", {}) or {}
    if not profile:
        st.info("No dataset profile is available yet.")
        return
    columns = profile.get("columns", [])
    left, middle, right = st.columns(3)
    left.metric("Rows", f"{profile.get('row_count', 0):,}")
    middle.metric("Columns", len(columns))
    right.metric("Quality warnings", len(profile.get("warnings", [])))
    st.caption(f"Source: {profile.get('source_filename', 'CSV upload')} · Encoding: {profile.get('encoding', 'UTF-8')}")
    for warning in profile.get("warnings", []):
        st.warning(str(warning))
    preview_tab, schema_tab, profile_tab = st.tabs(["Dataset preview", "Schema & completeness", "Full profile"])
    with preview_tab:
        try:
            st.dataframe(records_frame(sql_preview(workspace, "SELECT * FROM raw.source", limit=50)), width="stretch", hide_index=True)
            st.caption("First 50 rows from the imported dataset. Profiling context sent to the local model is separately bounded.")
        except Exception as exc:
            st.info(f"Preview is temporarily unavailable: {exc}")
    with schema_tab:
        if columns:
            st.dataframe(pd.DataFrame(columns), width="stretch", hide_index=True)
    with profile_tab:
        st.json(profile, expanded=False)


def new_workspace(service: Service, workspace: Path | None) -> None:
    st.header("Start with your data")
    st.write("Upload a CSV and describe the outcome you need. Review the dataset, compare proposed approaches, then approve an implementation.")
    with st.form("workspace_form", clear_on_submit=False):
        name = st.text_input("Workspace name", placeholder="e.g. Customer activity review", max_chars=100)
        upload = st.file_uploader("Dataset", type=["csv"], max_upload_size=service.settings.max_upload_mb,
                                  help=f"Maximum {service.settings.max_upload_mb} MB. The original CSV is retained locally.")
        problem = st.text_area("What do you want to solve?", placeholder="Describe the business question, desired outputs, and any constraints.", height=125, max_chars=12000)
        submitted = st.form_submit_button("Create & profile workspace", type="primary", width="stretch")
    if submitted:
        if not name.strip() or not problem.strip() or upload is None:
            st.error("Add a name, a CSV dataset, and a business problem to create a workspace.")
        else:
            try:
                with st.spinner("Saving the dataset and profiling it locally…"):
                    created = service.create_workspace(name.strip(), problem.strip(), upload.getvalue(), upload.name)
                st.session_state["workspace_id"] = created.name
                st.success("Workspace created. Inspect the profile below, then request solution proposals.")
                workspace = created
            except Exception as exc:
                st.error(f"Could not create the workspace: {exc}")
    if workspace:
        st.divider()
        metadata = service.status(workspace.name)
        st.subheader(metadata.get("name", workspace.name))
        profile_panel(service, workspace)
        if st.button("Continue to solution proposals", type="primary"):
            navigate("Solution proposals")
    with st.expander("Try a reproducible example"):
        st.write("Run `python examples/generate_data.py` for 10,000 synthetic transactions and a sales dataset. Suggested problems are in `examples/scenarios.md`.")
        examples = Path(__file__).parent / "examples" / "data"
        for filename, label in [("banking_transactions.csv", "Download synthetic transactions"), ("customer_sales.csv", "Download synthetic sales")]:
            path = examples / filename
            if path.is_file():
                st.download_button(label, path.read_bytes(), file_name=filename, mime="text/csv")


def bullet_list(label: str, items: list) -> None:
    if items:
        st.markdown(f"**{label}**")
        for item in items:
            st.write(f"• {item}")


def proposal_panel(proposal: dict) -> None:
    st.write(proposal.get("description", ""))
    st.markdown("**Approach**")
    st.write(proposal.get("approach", ""))
    st.caption("Feasibility: " + proposal.get("feasibility", "Not assessed"))
    st.write(proposal.get("confidence_explanation", ""))
    resources = proposal.get("resource_estimate", {})
    st.caption("Estimated resources · " + " · ".join(f"{key.replace('_', ' ')}: {value}" for key, value in resources.items()))
    outputs = proposal.get("expected_outputs", [])
    if outputs:
        st.markdown("**Expected outputs**")
        st.dataframe(pd.DataFrame(outputs).astype(str), hide_index=True, width="stretch")
    left, right = st.columns(2)
    with left:
        bullet_list("Advantages", proposal.get("advantages", []))
        bullet_list("Implementation steps", proposal.get("implementation_steps", []))
    with right:
        bullet_list("Limitations", proposal.get("limitations", []))
        bullet_list("Assumptions", proposal.get("assumptions", []))
    bullet_list("Validation strategy", proposal.get("validation_strategy", []))
    if proposal.get("required_dependencies"):
        st.caption("Dependencies: " + ", ".join(proposal["required_dependencies"]))


@st.fragment(run_every=3)
def proposals_page(service: Service, workspace: Path) -> None:
    st.header("Choose the approach")
    metadata = service.status(workspace.name)
    status = str(metadata.get("status", "UNKNOWN")).upper()
    busy = not metadata.get("worker_interrupted", False) and (status in ACTIVE or metadata.get("worker_active", False))
    st.caption(f"Workspace: {metadata.get('name', workspace.name)} · Status: {status}")
    st.write(metadata.get("problem", ""))
    understanding = service.manager.read(workspace, "metadata/understanding.json", {}) or {}
    if understanding:
        with st.expander("Agent's understanding of the problem"):
            st.write(understanding.get("summary", ""))
            bullet_list("Assumptions", understanding.get("assumptions", []))
            bullet_list("Limitations", understanding.get("limitations", []))
    proposals = proposals_from(service.manager.read(workspace, "metadata/proposals.json", {}))
    if busy:
        st.info("The worker is running. This page refreshes every three seconds; you may leave it and reopen this workspace later.")
        if st.button("Open execution monitor"):
            navigate("Execution monitor")
    if not proposals:
        st.info("Request proposals after reviewing your dataset. The model will inspect the profile and evaluate feasible approaches.")
    if not busy and status != "AWAITING_APPROVAL":
        if st.button("Generate solution proposals", type="primary", disabled=status == "COMPLETED"):
            try:
                service.start_proposals(workspace.name)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    for index, proposal in enumerate(proposals, start=1):
        with st.expander(f"{index:02d} · {proposal.get('title', 'Untitled proposal')}", expanded=index == 1):
            proposal_panel(proposal)
    if proposals and status == "AWAITING_APPROVAL" and not busy:
        feasible = [item for item in proposals if item.get("feasibility") != "NOT_FEASIBLE"]
        if feasible:
            by_id = {item["proposal_id"]: item for item in feasible}
            with st.form("approve_form"):
                selected = st.radio("Proposal to implement", list(by_id), format_func=lambda key: by_id[key]["title"])
                instructions = st.text_area("Additional implementation instructions (optional)", help="Instructions can refine implementation; changing the objective requires new proposals.", max_chars=6000)
                approved = st.form_submit_button("Approve & implement", type="primary", width="stretch")
                st.caption("Approval permits code generation and bounded execution inside the Docker sandbox.")
            if approved:
                try:
                    service.approve(workspace.name, selected, instructions.strip())
                    navigate("Execution monitor")
                except Exception as exc:
                    st.error(str(exc))
        else:
            st.warning("No proposal is currently feasible. Review limitations and refine the problem or input dataset.")
        with st.expander("Reject these proposals"):
            reason = st.text_input("Reason for rejection", max_chars=2000)
            if st.button("Reject proposals"):
                try:
                    service.reject(workspace.name, reason)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
    if metadata.get("error"):
        st.error(str(metadata["error"]))
    if metadata.get("last_job_error"):
        st.warning("Last job could not start: " + str(metadata["last_job_error"]))


def display_validation(value: dict) -> None:
    if not value:
        st.info("Validation evidence will appear after execution.")
        return
    if value.get("passed"):
        st.success("Output validation passed.")
    else:
        st.warning("Output validation has not passed. Review the evidence below.")
    checks = value.get("checks", [])
    if checks:
        st.dataframe(pd.DataFrame(checks).astype(str), hide_index=True, width="stretch")
    for error in value.get("errors", []):
        st.error(str(error))


@st.fragment(run_every=3)
def monitor_page(service: Service, workspace: Path) -> None:
    st.header("Execution monitor")
    metadata = service.status(workspace.name)
    status = str(metadata.get("status", "UNKNOWN")).upper()
    interrupted = metadata.get("worker_interrupted", False)
    execution = service.manager.read(workspace, "metadata/execution.json", {}) or {}
    events = service.events(workspace.name)
    left, middle, right = st.columns(3)
    left.metric("Current stage", "Interrupted" if interrupted else status.replace("_", " ").title())
    middle.metric("Execution attempt", metadata.get("attempt", execution.get("attempt", "—")))
    seconds = execution.get("execution_time_seconds")
    right.metric("Last execution", f"{seconds:.1f} s" if isinstance(seconds, (int, float)) else "—")
    st.caption("Activity and execution evidence are read from persistent workspace files every three seconds.")
    if status in ACTIVE or interrupted:
        if st.button("Cancel current run"):
            try:
                service.cancel(workspace.name)
                st.info("Cancellation requested. The worker will stop at its next safe boundary.")
            except Exception as exc:
                st.error(str(exc))
    if status == "AWAITING_APPROVAL":
        st.info("Proposals are ready. Execution is waiting for your approval.")
        if st.button("Review proposals", type="primary"):
            navigate("Solution proposals")
    if status == "COMPLETED":
        st.success("Execution and validation completed. Registered outputs are available in Results.")
        if st.button("View results", type="primary"):
            navigate("Results")
    if metadata.get("error"):
        st.error(str(metadata["error"]))
    if metadata.get("last_job_error"):
        st.warning("Last job could not start: " + str(metadata["last_job_error"]))
    activity_tab, logs_tab, code_tab, checks_tab = st.tabs(["Agent activity", "Execution logs", "Generated files", "Validation"])
    with activity_tab:
        if events:
            for event in reversed(events[-80:]):
                st.markdown(f"**{event.get('stage', 'activity')}** · {event.get('timestamp', '')}")
                st.text(str(event.get("message", "")))
                if event.get("details"):
                    with st.expander("Details", expanded=False):
                        st.json(event["details"], expanded=False)
        else:
            st.info("No worker activity yet.")
    with logs_tab:
        if execution:
            st.json(execution, expanded=False)
        log_files = sorted((workspace / "logs").glob("*.log")) if (workspace / "logs").is_dir() else []
        for path in log_files[-12:]:
            with st.expander(path.name, expanded=path == log_files[-1]):
                st.code(read_text(workspace, path.relative_to(workspace).as_posix()), language="text")
        if not log_files:
            st.caption("Container stdout and stderr will be saved here when an execution starts.")
    with code_tab:
        generated = sorted((workspace / "generated").rglob("*")) if (workspace / "generated").is_dir() else []
        for path in generated:
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(workspace).as_posix()
                with st.expander(relative):
                    try:
                        st.code(read_text(workspace, relative), language="sql" if path.suffix == ".sql" else "python" if path.suffix == ".py" else "text")
                    except ValueError as exc:
                        st.warning(str(exc))
        if not generated:
            st.caption("Generated source files will appear after approval.")
    with checks_tab:
        display_validation(service.manager.read(workspace, "metadata/validation.json", {}) or {})


def table_preview(workspace: Path, artifact: dict, key: str) -> None:
    location = artifact["location"]
    quoted = quoted_table(location)
    try:
        count = sql_preview(workspace, f"SELECT COUNT(*) AS row_count FROM {quoted}", limit=1)
        frame = records_frame(count)
        total = int(frame.iloc[0, 0]) if not frame.empty else 0
        st.caption(f"{total:,} rows · {location}")
        page = st.number_input("Page", min_value=1, max_value=max(1, min(1001, (total + 99) // 100)), value=1, step=1, key=f"page_{key}")
        result = sql_preview(workspace, f"SELECT * FROM {quoted}", limit=100, offset=(page - 1) * 100)
        st.dataframe(records_frame(result), hide_index=True, width="stretch")
        st.download_button("Download this page as CSV", records_frame(result).to_csv(index=False).encode("utf-8"), file_name=f"{location}_page_{page}.csv", mime="text/csv", key=f"csv_{key}")
        st.caption("Previews contain up to 100 rows. Use an ORDER BY query below when stable pagination matters.")
        with st.expander("Column information"):
            for entry in database_schema(workspace):
                if f"{entry.get('schema')}.{entry.get('name')}" == location:
                    st.dataframe(pd.DataFrame(entry.get("columns", [])), hide_index=True, width="stretch")
    except Exception as exc:
        st.warning(f"Table preview is unavailable: {exc}")


def artifact_panel(workspace: Path, artifact: dict, key: str) -> None:
    kind = artifact.get("type", "binary_file")
    location = str(artifact.get("location", ""))
    st.caption(f"{kind.replace('_', ' ')} · {location}")
    validation_status = artifact.get("validation_status", "NOT_RECORDED")
    if validation_status == "PASSED":
        st.caption("✓ This artifact passed its recorded validation checks.")
    elif validation_status == "FAILED":
        st.warning("This artifact did not pass its recorded validation checks.")
    else:
        st.caption("This artifact was discovered; no artifact-specific passing check is recorded.")
    if artifact.get("description"):
        st.write(artifact["description"])
    if kind in {"database_table", "database_view"}:
        table_preview(workspace, artifact, key)
    else:
        try:
            path = safe_path(workspace, location)
            if not path.is_file():
                st.warning("This registered file is missing from the workspace.")
                return
            size = path.stat().st_size
            st.caption(f"File size: {size / 1024:,.1f} KB")
            if size <= DOWNLOAD_BYTES:
                st.download_button("Download artifact", data=path.read_bytes(), file_name=path.name, key=f"download_{key}")
            else:
                st.info("This artifact exceeds the 100 MB browser download limit. Open it from the local workspace path shown above.")
            if kind == "model":
                st.info("Model files are available for download. Loading and validation happen only inside the sandbox.")
                for metadata in (artifact.get("metadata"), artifact.get("metrics"), artifact.get("training_details")):
                    if metadata:
                        st.json(metadata, expanded=False)
            elif kind == "notebook" or path.suffix == ".ipynb":
                document = read_json_preview(workspace, location)
                st.caption("Read-only source and saved text outputs; this preview does not execute notebook cells.")
                for cell in notebook_preview(document):
                    if cell["type"] == "code":
                        st.code(cell["source"], language="python")
                    else:
                        st.text(cell["source"])
                    for output in cell["outputs"]:
                        if output:
                            st.code(output[:20000], language="text")
            elif kind == "chart" and path.suffix.lower() == ".json":
                spec = read_json_preview(workspace, location)
                if chart_has_external_resources(spec):
                    st.warning("Chart preview contains external resource references. Download the specification for inspection.")
                else:
                    st.plotly_chart(pio.from_json(json.dumps(spec)), width="stretch", key=f"chart_{key}")
            elif kind == "json" or path.suffix.lower() == ".json":
                st.json(read_json_preview(workspace, location), expanded=False)
            elif kind == "csv" or path.suffix.lower() == ".csv":
                if size <= 10 * 1024 * 1024:
                    st.dataframe(pd.read_csv(path, nrows=100), hide_index=True, width="stretch")
                    st.caption("First 100 rows.")
                else:
                    st.caption("CSV preview is limited to files under 10 MB; the complete download remains available.")
            elif kind in {"python_script", "sql_script", "text_report"} or path.suffix.lower() in {".py", ".sql", ".txt", ".md"}:
                st.code(read_text(workspace, location), language="python" if path.suffix == ".py" else "sql" if path.suffix == ".sql" else "text")
            else:
                st.caption("This artifact type has no inline preview. The original file and registration metadata are available.")
        except Exception as exc:
            st.warning(f"Artifact preview is unavailable: {exc}")
    with st.expander("Registration metadata"):
        st.json(artifact, expanded=False)


def results_page(service: Service, workspace: Path) -> None:
    st.header("Results & evidence")
    manifest = service.manager.read(workspace, "metadata/manifest.json", {}) or {}
    validation = service.manager.read(workspace, "metadata/validation.json", {}) or {}
    artifacts = manifest.get("artifacts", [])
    st.caption("Artifacts below are registered from execution outputs. Proposal descriptions are model-generated; validation and saved data provide the execution evidence.")
    if not artifacts:
        st.info("No artifacts have been registered yet. Approve a proposal and follow its execution in the monitor.")
    else:
        for index, artifact in enumerate(artifacts):
            with st.expander(artifact.get("name", artifact.get("location", f"Artifact {index + 1}")), expanded=index == 0):
                artifact_panel(workspace, artifact, f"{workspace.name}_{index}")
    with st.expander("Validation evidence", expanded=bool(validation) and not validation.get("passed", False)):
        display_validation(validation)
    selected = service.manager.read(workspace, "metadata/selected_plan.json", {}) or {}
    selected = selected.get("proposal", selected)
    if selected:
        with st.expander("Approved approach, assumptions & limitations"):
            proposal_panel(selected)
    st.divider()
    st.subheader("Query workspace data")
    st.caption("Read-only SELECT queries, bounded rows and runtime. File access, extensions, writes and network access are disabled.")
    with st.form("query_form"):
        sql = st.text_area("SQL query", value="SELECT * FROM raw.source LIMIT 100", height=110)
        submitted = st.form_submit_button("Run bounded query")
    if submitted:
        try:
            response = sql_preview(workspace, sql, limit=100)
            st.session_state[f"query_result_{workspace.name}"] = response
        except Exception as exc:
            st.error(str(exc))
    response = st.session_state.get(f"query_result_{workspace.name}")
    if response is not None:
        frame = records_frame(response)
        st.dataframe(frame, hide_index=True, width="stretch")
        if response.get("truncated"):
            st.caption("Results are limited to 100 rows.")
        st.download_button("Download displayed query rows", frame.to_csv(index=False).encode("utf-8"), file_name="query_preview.csv", mime="text/csv")
    with st.expander("Available database objects"):
        try:
            st.json(database_schema(workspace), expanded=False)
        except Exception as exc:
            st.info(f"Schema is temporarily unavailable: {exc}")


def history_page(service: Service) -> None:
    st.header("Workspace history")
    workspaces = service.manager.list()
    if not workspaces:
        st.info("Your saved workspaces will appear here after the first CSV upload.")
        return
    search = st.text_input("Find a workspace", placeholder="Filter by name or problem").casefold()
    for item in workspaces:
        if search and search not in (str(item.get("name", "")) + " " + str(item.get("problem", ""))).casefold():
            continue
        with st.container(border=True):
            left, right = st.columns([4, 1])
            left.subheader(item.get("name", item["id"]))
            left.caption(f"{item.get('status', 'UNKNOWN')} · Updated {item.get('updated_at', 'unknown')} · {item['id']}")
            left.write(str(item.get("problem", ""))[:500])
            if right.button("Open workspace", key=f"open_{item['id']}", width="stretch"):
                st.session_state["workspace_id"] = item["id"]
                navigate("Results" if item.get("status") == "COMPLETED" else "Solution proposals" if item.get("status") == "AWAITING_APPROVAL" else "New workspace")


def main() -> None:
    st.set_page_config(page_title="Data Solution Workbench", page_icon="◈", layout="wide", initial_sidebar_state="expanded")
    st.markdown("""<style>
    .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1320px;}
    [data-testid="stSidebar"] {background-color: #eef2f6;}
    h1,h2,h3 {letter-spacing: -.025em;}
    [data-testid="stMetric"] {background:#f6f8fb; border:1px solid #dce4ed; border-radius:10px; padding:16px;}
    [data-testid="stMetricValue"] {font-size: 1.5rem;}
    </style>""", unsafe_allow_html=True)
    if "next_page" in st.session_state:
        st.session_state["page"] = st.session_state.pop("next_page")
    service = get_service()
    if "diagnostics" not in st.session_state:
        with st.spinner("Checking the local model and sandbox…"):
            try:
                st.session_state["diagnostics"] = service.diagnostics()
            except Exception as exc:
                st.session_state["diagnostics"] = {"error": str(exc)}
    workspace = None
    with st.sidebar:
        st.markdown("### ◈ Data Solution\nWorkbench")
        st.caption("LOCAL-FIRST · APPROVAL-GATED")
        st.divider()
        page = st.radio("Workspace workflow", PAGES, key="page", label_visibility="collapsed")
        st.divider()
        if st.session_state.get("workspace_id"):
            try:
                workspace = service.manager.get(st.session_state["workspace_id"])
                metadata = service.status(workspace.name)
                st.caption("CURRENT WORKSPACE")
                st.write(metadata.get("name", workspace.name))
                st.caption(metadata.get("status", "UNKNOWN"))
            except Exception as exc:
                st.warning(f"Workspace could not be reopened: {exc}")
        else:
            st.caption("No workspace selected")
        with st.expander("Local environment"):
            diagnostics = st.session_state.get("diagnostics", {})
            for name, label, required_key in [("llm", "Local model", "model_available"), ("docker", "Docker image", "image_available")]:
                detail = diagnostics.get(name, diagnostics.get("ollama", {}) if name == "llm" else {})
                ready = detail.get("available", False) and detail.get(required_key, False)
                st.caption(f"{'✓' if ready else '○'} {label}: {'ready' if ready else 'needs attention'}")
            if diagnostics.get("provider"):
                st.caption(f"{diagnostics['provider']} · {diagnostics.get('model', '')}")
            st.caption("Check model and sandbox availability before starting implementation.")
            if st.button("Check services"):
                with st.spinner("Checking local services…"):
                    try:
                        st.session_state["diagnostics"] = service.diagnostics()
                    except Exception as exc:
                        st.session_state["diagnostics"] = {"error": str(exc)}
            if "diagnostics" in st.session_state:
                st.json(st.session_state["diagnostics"], expanded=False)
        st.caption("Your CSV, database, generated source, logs and outputs remain in local workspaces.")
    if page == "New workspace":
        new_workspace(service, workspace)
    elif page == "Workspace history":
        history_page(service)
    elif workspace is None:
        st.header(page)
        st.info("Create a workspace or open one from Workspace history to continue.")
    elif page == "Solution proposals":
        proposals_page(service, workspace)
    elif page == "Execution monitor":
        monitor_page(service, workspace)
    else:
        results_page(service, workspace)


if __name__ == "__main__":
    main()
