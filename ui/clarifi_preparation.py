"""Review-first local migration and financial investigation, sharing Docker."""
from __future__ import annotations

import hashlib
import io
import os

import pandas as pd
import streamlit as st

from workbench.agents.clarifi.preparation import FinancialPreparation, read_sources
from workbench.agents.clarifi.ingest import TOOL_TABLES
from workbench.migration import MappingPlan
from workbench.workspace import read_json, safe_path


def preparation_panel(service, tool):
    from ui.clarifi import input_error_message, LABELS, model_panel, render_results, input_fingerprint
    from ui.migration import _review, _preview

    st.write("Upload your existing data and describe the investigation. Migration prepares the ClariFi format; you review it before the financial agent runs.")
    st.caption("CSV, Excel worksheets or JSON tables · 5 MiB combined · PDFs need extraction into structured tables first. Missing evidence is never invented.")
    if st.button("Check shared Docker sandbox"):
        from workbench.sandbox import SandboxManager
        health = SandboxManager(service.settings).health()
        if health.get("available") and health.get("image_available"):
            st.success("Docker and sandbox image are available. Rebuild the image after updating financial tools.")
        else:
            st.warning("Install/start Docker and build the current sandbox image before conversion or investigation.")
            st.code("docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .", language="bash")
    instruction = st.text_area("Instruction for the financial agent", key="clarifi_instruction",
        value="Use ClariFi agent to investigate these financial records. Preserve the evidence and flag missing information.", max_chars=10000)
    case = st.text_input("Preparation case reference", value="financial-review", key="clarifi_preparation_case", max_chars=100)
    files = st.file_uploader("Source financial files", type=["csv", "xlsx", "json"], accept_multiple_files=True,
                             max_upload_size=5, key="clarifi_preparation_files")
    seeded = st.session_state.get("clarifi_routed_files", [])
    if seeded:
        st.caption("Files received from your workbench instruction: " + ", ".join(name for name, _ in seeded))
        if st.button("Clear files received from workbench"):
            st.session_state.pop("clarifi_routed_files", None)
            st.rerun()
    uploads = [(file.name, file.getvalue()) for file in files] if files else seeded
    mapping_provider = st.selectbox("Financial mapping model", ["Local Gemma", "Gemini Flash-Lite"],
        index=1 if os.getenv("CLARIFI_DEFAULT_PROVIDER") == "gemini" else 0, key="cf_mapping_provider")
    mapping_client = None
    if mapping_provider == "Gemini Flash-Lite":
        from workbench.agents.clarifi.models import FinancialGeminiMappingClient
        mapping_client = FinancialGeminiMappingClient()
        st.caption("Hosted Gemini receives source metadata and masked profile samples for mapping. Review every mapping before Docker conversion. Free-tier quota applies; there is no automatic model fallback.")
    pipeline = FinancialPreparation(service.settings, llm=mapping_client)
    sources, assignments = [], {}
    try:
        if uploads:
            sources = read_sources(uploads)
            st.write("**Identify the evidence in each source table**")
            options = ["Choose a table", *TOOL_TABLES[tool]]
            upload_hash = hashlib.sha256(b"".join(item.content for item in sources)).hexdigest()
            for item in sources:
                suggested = item.label.lower().replace(" ", "_")
                selection = st.selectbox(f"{item.filename} · {item.label}", options,
                    index=options.index(suggested) if suggested in options else 0,
                    format_func=lambda value: LABELS.get(value, value), key=f"cf_assign_{tool}_{upload_hash}_{item.key}")
                if selection != options[0]:
                    assignments[item.key] = selection
            with st.expander("Review original source tables"):
                for item in sources:
                    st.write(item.filename + " · " + item.label)
                    frame = pd.read_csv(io.BytesIO(item.content), dtype=str, keep_default_na=False, nrows=100)
                    st.dataframe(frame, hide_index=True, width="stretch")
                st.caption("First 100 rows per table. All supported rows are preserved during migration.")
        canonical = None
        if sources and len(assignments) == len(sources):
            from workbench.agents.clarifi.preparation import canonical_input, requests_clarifi
            try:
                canonical = canonical_input(sources, assignments, case_id=case.strip(), tool=tool)
            except ValueError:
                pass  # Arbitrary source schemas continue through reviewed migration.
        if canonical is not None and requests_clarifi(instruction):
            st.success("These records already match the ClariFi format. AI mapping and conversion can be skipped.")
            if st.checkbox("Use the validated format directly", key="cf_skip_migration_" + upload_hash):
                # Retain the same Docker execution even though no conversion is needed.
                from workbench.agents.clarifi.sandbox import FinancialSandbox
                from ui.clarifi import scores_first_panel
                from workbench.workspace import WorkspaceManager
                manager = WorkspaceManager(service.settings.workspace_root / "clarifi-valid-inputs")
                contract = input_fingerprint(canonical, "screen", None)
                workspace_key = "cf_valid_workspace_" + contract
                if workspace_key not in st.session_state:
                    workspace = manager.create(case.strip(), instruction)
                    from workbench.agents.clarifi.screening import input_hash
                    manager.write(workspace, "metadata/source_contract.json", {
                        "input_sha256": input_hash(canonical), "conversion": "skipped_validated_contract",
                        "tables": list(assignments.values()),
                        "sources": [{"filename": item.filename, "table": assignments[item.key],
                                     "original_sha256": item.original_sha256} for item in sources]})
                    st.session_state[workspace_key] = workspace.name
                workspace = manager.get(st.session_state[workspace_key])
                mode, model = model_panel(allow_remote=False)
                scores_first_panel(service, canonical, mode, model, key="validated_" + contract,
                                   tool_executor=FinancialSandbox(service.settings, workspace),
                                   migration=manager.read(workspace, "metadata/source_contract.json"))
                return
        if st.button("Create financial preparation", type="primary", disabled=not sources):
            workspace = pipeline.create(case.strip(), instruction, tool, sources, assignments)
            st.session_state["clarifi_preparation_id"] = workspace.name
            st.rerun()
    except Exception as error:
        st.error(input_error_message(error))

    cases = pipeline.manager.list()
    if cases:
        with st.expander("Resume a financial preparation"):
            chosen = st.selectbox("Saved preparation", [item["id"] for item in cases],
                format_func=lambda value: next(item["name"] + " · " + item["status"] for item in cases if item["id"] == value))
            if st.button("Open preparation"):
                st.session_state["clarifi_preparation_id"] = chosen
                st.rerun()
    identifier = st.session_state.get("clarifi_preparation_id")
    if not identifier:
        return
    try:
        workspace = pipeline.workspace(identifier)
        state = pipeline.state(identifier)
        st.divider()
        st.subheader("Prepare · " + state["case_id"])
        st.caption("Saved instruction: " + state["instruction"])
        if state["tool"] != tool:
            st.info("This saved preparation has a different capability scope. Its saved scope is used for validation and investigation.")
        migration = pipeline.migration(identifier)
        st.subheader("1 · Review migration mappings")
        st.caption("Mapping proposals use your selected financial mapping model. Planning runs outside Docker; conversions execute only in Docker after approval.")
        ready = True
        for table, item in state["tables"].items():
            child = migration.workspace(item["workspace_id"])
            approval = read_json(safe_path(child, "metadata/mapping_approval.json"))
            with st.expander(LABELS[table] + " · " + ("Mappings approved" if approval else "Review needed"), expanded=not approval):
                if approval:
                    st.dataframe(approval["plan"]["mappings"], hide_index=True, width="stretch")
                else:
                    ready = False
                    plan = read_json(safe_path(child, "generated/mapping.json"))
                    if st.button("Propose mappings · " + LABELS[table], key="cf_propose_" + child.name):
                        with st.spinner("Migration agent is proposing mappings…"):
                            pipeline.propose(identifier, table)
                        st.rerun()
                    if plan:
                        _, target = migration.metadata(child.name)
                        _review(migration, child, MappingPlan.model_validate(plan), target)
        handoff = read_json(safe_path(workspace, "results/handoff.json"))
        if not handoff and st.button("Convert and validate in sandbox", disabled=not ready, type="primary"):
            with st.spinner("Running approved migrations and checking linked financial records…"):
                pipeline.prepare(identifier)
            st.rerun()
        if not handoff:
            st.info("Approve each mapping and complete conversion before ClariFi can investigate. Incomplete migrations or invalid financial references block the handoff.")
            return
        data = pipeline.prepared(identifier)
        st.subheader("2 · Review converted financial evidence")
        st.success("Migration complete · financial schema and cross-table references validated")
        for table, item in state["tables"].items():
            with st.expander(LABELS[table] + " · " + str(len(getattr(data, table))) + " records"):
                _preview(migration.preview(item["workspace_id"]))
        st.download_button("Download prepared ClariFi JSON", data.model_dump_json(indent=2),
                           file_name="clarifi-prepared.json", mime="application/json")
        with st.expander("Source and migration audit trail"):
            st.json(handoff, expanded=False)
        st.subheader("3 · Run ClariFi in the shared sandbox")
        mode, model = model_panel(allow_remote=False)
        delivery = st.radio("Results delivery", ["Show scores first", "Wait for the full AI report"],
                            horizontal=True, key="cf_prepared_delivery_" + identifier)
        if delivery == "Show scores first":
            from ui.clarifi import scores_first_panel
            from workbench.agents.clarifi.sandbox import FinancialSandbox
            scores_first_panel(service, data, mode, model, key="prepared_" + identifier,
                               tool_executor=FinancialSandbox(service.settings, workspace, sandbox=pipeline.sandbox),
                               migration=handoff)
            return
        fingerprint = input_fingerprint(data, mode, model)
        approved = st.checkbox("I reviewed the converted evidence and approve the sandboxed ClariFi investigation",
                               key="cf_prepared_approve_" + identifier + fingerprint)
        if st.button("Run prepared ClariFi investigation", type="primary", disabled=not approved or (mode != "rules" and not model)):
            with st.spinner("ClariFi is planning, running sandboxed tools and verifying evidence…"):
                pipeline.investigate(identifier, approved=True, mode=mode, model=model)
            st.rerun()
        bundle = read_json(safe_path(workspace, "results/investigation.json"))
        approval = read_json(safe_path(workspace, "metadata/investigation_approval.json"), {})
        if bundle and approval.get("mode") == mode and approval.get("model") == model:
            render_results(bundle, key="prepared_" + identifier)
        elif bundle:
            st.info("Model selection changed. The saved report belongs to the previously approved model.")
    except Exception as error:
        from workbench.agents.clarifi.ingest import IntakeError
        from workbench.llm import LLMError
        if isinstance(error, IntakeError):
            st.error(str(error))
        elif isinstance(error, LLMError):
            st.error(str(error))
        else:
            st.error("Preparation or investigation did not complete. Check the local mapping model, Docker image and saved execution logs. No incomplete data is sent to ClariFi.")
