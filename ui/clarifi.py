"""Financial investigation intake and evidence views, alongside the original UI."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

CAPABILITIES = {
    "Insider trading indicators": "insider_trading",
    "Shell-company networks": "shell_companies",
    "Combined investigation": "combined",
}
LABELS = {
    "employees": "Employees", "accounts": "Trading accounts", "relationships": "Declared related accounts",
    "trades": "Trading history", "events": "Public market events", "entities": "Company records", "payments": "Payments",
}


def input_fingerprint(data, mode, model):
    packet = {"input": data.model_dump(mode="json"), "mode": mode, "model": model}
    return hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()


def input_error_message(error):
    """Show field locations and reviewed guidance, never submitted cell values."""
    from pydantic import ValidationError
    from workbench.agents.clarifi.ingest import IntakeError, TABLES
    from workbench.agents.clarifi.schemas import InvestigationInput
    if isinstance(error, IntakeError):
        return str(error)
    if isinstance(error, ValidationError):
        issues = []
        guidance = {"missing": "Required value or column is missing.",
                    "extra_forbidden": "Unexpected column; use the template headers.",
                    "timezone_aware": "Use a timestamp with a timezone, such as 2022-04-12T16:00:00Z.",
                    "datetime_from_date_parsing": "Use an ISO timestamp with a timezone, such as 2022-04-12T16:00:00Z.",
                    "datetime_parsing": "Use an ISO timestamp with a timezone, such as 2022-04-12T16:00:00Z.",
                    "greater_than": "Use a positive numeric value.",
                    "string_pattern_mismatch": "Check the template's identifier or currency format."}
        for issue in error.errors(include_input=False, include_context=False, include_url=False)[:5]:
            location = issue["loc"]
            parts = []
            if location and location[0] in TABLES:
                table = location[0]
                parts.append(table)
                if len(location) > 1 and type(location[1]) is int:
                    parts.append(f"row {location[1]+2}")
                fields = InvestigationInput.model_fields[table].annotation.__args__[0].model_fields
                if len(location) > 2 and location[2] in fields:
                    parts.append(location[2])
            elif location == ("case_id",):
                parts.append("Case reference")
            explanation = guidance.get(issue["type"], "Check the template's required formats, unique IDs and links between tables.")
            issues.append((" · ".join(parts) + ": " if parts else "") + explanation)
        return "The input could not be validated. " + " ".join(issues)
    return "The input could not be read. Use the selected capability's Excel template or example JSON. Check worksheet names, headers, values rather than formulas, and the 5 MiB upload limit."


def input_panel(tool):
    from workbench.agents.clarifi.ingest import TOOL_TABLES, excel_template, focused_input, read_csv_bytes, read_excel_bytes, sample_input, validate_scope
    from workbench.agents.clarifi.schemas import InvestigationInput

    st.write("Bring your records as JSON, CSV tables or an Excel workbook. Use the template to arrange your existing data into the fields ClariFi needs.")
    sample_directory = Path(__file__).resolve().parents[1] / "outputs/clarifi-demo-01a14a33"
    sample_workbook = sample_directory / "clarifi-combined-review.xlsx"
    if tool == "combined" and sample_workbook.is_file():
        st.info("Try a larger example: 2,493 simulated records inspired by public cases. Includes ordinary activity, review leads and ambiguous identities. Historical names are references; all operational rows are simulated.")
        st.download_button("Download combined investigation sample", sample_workbook.read_bytes(),
                           file_name=sample_workbook.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        guide = sample_directory / "README.md"
        if guide.is_file():
            st.download_button("Download sample guide & baseline results", guide.read_text(), file_name="clarifi-sample-guide.md", mime="text/markdown")
    with st.expander("Download the Excel format & see what to provide"):
        st.write("One worksheet per table. The template contains synthetic examples: replace every example row with your own records before uploading. Keep the worksheet names and column headers unchanged.")
        if tool in {"insider_trading", "combined"}:
            st.write("Required: employees, trading accounts and trades. Add declared related accounts for family-account checks, issuer access and public event dates for stronger context. Initials alone are a lead for identity review.")
        if tool in {"shell_companies", "combined"}:
            st.write("Required: company records and payments. Include registration addresses, bank accounts and documented ownership where available. Add payment dates, amounts, currencies and business purposes to help explain connections.")
        st.download_button("Download Excel template", excel_template(tool), file_name=f"clarifi-{tool}-template.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"clarifi_excel_template_{tool}")
        st.caption("Use values, not formulas. Keep IDs as text and dates as ISO 8601 text with a UTC offset. Separate access_issuers with |. Maximum 5 MiB per investigation.")
    source = st.radio("Investigation data", ["Focused high-priority example", "Synthetic example", "Upload Excel workbook", "Upload CSV tables", "Upload JSON bundle"],
                      horizontal=True, key=f"clarifi_source_{tool}")
    data = None
    try:
        if source in {"Focused high-priority example", "Synthetic example"}:
            focused = source == "Focused high-priority example"
            data = focused_input(tool) if focused else sample_input(tool)
            st.caption("Curated positive synthetic cases for a short demo. These expected signals do not measure real-world detection accuracy." if focused else
                       "Synthetic records only · includes review cases and comparison records · no real people or companies")
            st.download_button("Download example JSON", data.model_dump_json(indent=2),
                               file_name=f"clarifi-{tool}-example.json", mime="application/json")
            workbook = Path(__file__).resolve().parents[1] / "examples/clarifi/focused_review.xlsx"
            if focused and tool == "combined" and workbook.is_file():
                st.download_button("Download focused Excel demo", workbook.read_bytes(), file_name="clarifi-focused-review.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        elif source == "Upload Excel workbook":
            case_id = st.text_input("Case reference", value=f"review-{tool}", key=f"clarifi_excel_case_{tool}")
            upload = st.file_uploader("Completed Excel template", type="xlsx", max_upload_size=5, key=f"clarifi_excel_{tool}")
            if upload:
                tables = read_excel_bytes(upload.getvalue(), tool)
                data = validate_scope(InvestigationInput.model_validate({"case_id": case_id, **tables}), tool)
        elif source == "Upload JSON bundle":
            st.caption("Use the example JSON structure, with the tables for this capability. Other document formats need to be mapped into the template first.")
            upload = st.file_uploader("Investigation bundle", type="json", max_upload_size=5, key=f"clarifi_json_{tool}")
            if upload:
                if upload.size > 5 * 1024 * 1024:
                    raise ValueError("Bundle exceeds 5 MiB")
                data = validate_scope(InvestigationInput.model_validate_json(upload.getvalue()), tool)
        else:
            case_id = st.text_input("Case reference", value=f"review-{tool}", key=f"clarifi_case_{tool}")
            tables = {}
            total = 0
            required = ({"employees", "accounts", "trades"} if tool == "insider_trading" else
                        {"entities", "payments"} if tool == "shell_companies" else
                        {"employees", "accounts", "trades", "entities", "payments"})
            columns = st.columns(2)
            for index, table in enumerate(TOOL_TABLES[tool]):
                with columns[index % 2]:
                    upload = st.file_uploader(LABELS[table] + (" · required" if table in required else " · optional"),
                                             type="csv", max_upload_size=5, key=f"clarifi_csv_{table}")
                if upload:
                    total += upload.size
                    if total > 5 * 1024 * 1024:
                        raise ValueError("Combined input exceeds 5 MiB")
                    tables[table] = read_csv_bytes(upload.getvalue())
            if required <= set(tables):
                data = validate_scope(InvestigationInput.model_validate({"case_id": case_id, **tables}), tool)
            else:
                st.info("Add the required CSV tables to prepare an investigation.")
            with st.expander("CSV columns and mappings"):
                st.write("Column names must match the example JSON. Use ACME|OTHER for access_issuers, true/false for precleared, and timestamps with a UTC offset. Keep account, employee and payment IDs consistent across tables.")
                st.json(sample_input(tool).model_dump(mode="json"), expanded=False)
    except Exception as error:
        st.error(input_error_message(error))
    if data:
        counts = {LABELS[table]: len(getattr(data, table)) for table in TOOL_TABLES[tool] if getattr(data, table)}
        st.success("Ready to investigate · " + " · ".join(f"{count} {label.lower()}" for label, count in counts.items()))
        with st.expander("Review source records before running", expanded=False):
            tables = [table for table in TOOL_TABLES[tool] if getattr(data, table)]
            tabs = st.tabs([LABELS[table] for table in tables])
            for tab, table in zip(tabs, tables):
                with tab:
                    st.dataframe(pd.DataFrame([row.model_dump(mode="json") for row in getattr(data, table)][:100]),
                                 hide_index=True, width="stretch")
                    if len(getattr(data, table)) > 100:
                        st.caption("Showing the first 100 records; the complete validated input is investigated.")
    return data


@st.cache_data(ttl=30, show_spinner=False)
def local_model_inventory(base_url):
    from workbench.llm import OllamaClient
    from workbench.settings import Settings
    client = OllamaClient(Settings(ollama_base_url=base_url, ollama_model="auto"))
    try:
        health = client.health()
        return list(dict.fromkeys(health.get("models", []))) if health.get("available") else []
    finally:
        client.close()


def model_panel(*, allow_remote=True):
    use_remote = allow_remote and bool(os.getenv("CLARIFI_API_URL"))
    if use_remote and os.getenv("CLARIFI_ENABLE_LOCAL", "").lower() == "true":
        service = st.selectbox("Reasoning service", ["Gemma on this GCP VM", "Gemini · private Cloud Run"],
                               key="clarifi_reasoning_service")
        use_remote = service == "Gemini · private Cloud Run"
    if use_remote:
        from workbench.agents.clarifi.remote import health
        model = os.getenv("CLARIFI_REMOTE_MODEL", "")
        st.write("Gemini · Cloud Run")
        st.caption("Planning and investigation run in the private Google Cloud service. The deployed model is fixed for this environment.")
        if model:
            st.code(model, language=None)
        else:
            st.warning("Configure CLARIFI_REMOTE_MODEL before running an investigation.")
        if st.button("Check cloud service", key="clarifi_cloud_check"):
            try:
                result = health()
                if result.get("status") == "ok" and result.get("model") == model:
                    st.success("Cloud service reachable · " + model)
                    st.caption("Model generation, quota and evidence storage are verified when an investigation runs.")
                else:
                    st.error("The deployed model differs from this UI's configuration.")
            except Exception:
                st.error("Cloud service check failed. Check service identity and configuration.")
        return "remote", model
    choice = st.selectbox("Reasoning model", ["Local Gemma via Ollama", "Gemini API · AI Studio", "Gemini on Vertex AI", "Offline rules demo"],
                          index=1 if os.getenv("CLARIFI_DEFAULT_PROVIDER") == "gemini" else 0, key="clarifi_model_choice")
    mode = {"Local Gemma via Ollama": "local", "Gemini API · AI Studio": "gemini", "Gemini on Vertex AI": "vertex", "Offline rules demo": "rules"}[choice]
    model = None
    if mode == "local":
        from workbench.agents.clarifi.models import LocalReasoner
        configured = st.session_state.get("clarifi_local_model") or os.getenv("CLARIFI_LOCAL_MODEL", "gemma3:1b")
        base_url = LocalReasoner(configured).settings.ollama_base_url
        if st.button("Refresh installed models", key="clarifi_refresh_models"):
            local_model_inventory.clear()
        installed = local_model_inventory(base_url)
        custom = "Enter model ID manually"
        preferred = configured if configured in installed else (installed[0] if installed else custom)
        selected = st.selectbox("Local model ID", installed + [custom], index=(installed + [custom]).index(preferred),
            format_func=lambda tag: tag + " · experimental on CPU" if tag == "gemma4:12b-it-qat" else tag,
            key="clarifi_installed_model")
        model = st.text_input("Custom local model ID", value=configured, key="clarifi_local_model") if selected == custom else selected
        st.caption("Models installed on the application server. The list refreshes every 30 seconds.")
        if not installed:
            st.warning("Installed models could not be listed. Check Ollama or enter the exact installed model ID manually.")
        st.caption("Planning and analyst explanations stay on this machine. Prepared-data investigations run financial tools in Docker; direct investigations use reviewed tools in the application process.")
        if st.button("Check local model", key="clarifi_model_check"):
            from workbench.agents.clarifi.models import LocalReasoner
            try:
                health = LocalReasoner(model).health()
                st.session_state["clarifi_model_health"] = {"model": model, **health}
            except Exception:
                st.session_state["clarifi_model_health"] = {"model": model, "available": False, "model_available": False}
        health = st.session_state.get("clarifi_model_health", {})
        if health.get("model") == model:
            if health.get("available") and health.get("model_available"):
                st.success(f"Model available · {model}")
                st.caption("Availability check only; an investigation still needs model generation to complete.")
                if health.get("generation_timeout_seconds"):
                    st.caption(f"Model request timeout · {health['generation_timeout_seconds']} seconds")
            else:
                st.warning("Model is not ready. Start Ollama and download the selected model before running.")
    elif mode == "gemini":
        from workbench.agents.clarifi.models import API_MODELS, GeminiAPIReasoner, gemini_api_config
        try:
            config = gemini_api_config()
        except ValueError:
            from workbench.agents.clarifi.models import DEFAULT_API_MODEL
            config = {"key": "", "model": os.getenv("CLARIFI_API_MODEL", DEFAULT_API_MODEL)}
            st.warning("Gemini credentials are currently unavailable. You can still run scores-first screening; check the server secret configuration before requesting an AI draft.")
        options = list(API_MODELS)
        if config["model"] not in options:
            options.append(config["model"])
        custom = "Custom model ID"
        selected = st.selectbox("Google AI model", options + [custom], index=options.index(config["model"]),
                                format_func=lambda key: API_MODELS.get(key, {}).get("label", key), key="clarifi_google_model")
        model = st.text_input("Gemini API model ID", value=config["model"], key="clarifi_api_model") if selected == custom else selected
        st.caption("Explains financial risk signals and suggests next steps for your review.")
        if not config["key"]:
            st.warning("AI explanations are unavailable. You can still review financial scores; contact the application administrator to enable AI.")
        if st.button("Check Gemini API", key="clarifi_gemini_check"):
            try:
                health = GeminiAPIReasoner(model).health()
                if health["available"]:
                    st.success("Gemini model lookup succeeded · " + model)
                    st.caption("Generation and response validation are checked when you run an investigation.")
                else:
                    st.error(health["error"])
            except Exception:
                st.error("Gemini configuration is unavailable. Check the local credential file.")
    elif mode == "vertex":
        model = st.text_input("Vertex Gemini model ID", value=os.getenv("CLARIFI_GEMINI_MODEL", ""), key="clarifi_vertex_model")
        st.caption("Uses the configured Google Cloud project and Application Default Credentials. Pseudonymous evidence IDs are sent for model reasoning.")
        if not os.getenv("GOOGLE_CLOUD_PROJECT"):
            st.info("Set GOOGLE_CLOUD_PROJECT and configure Application Default Credentials to use Vertex AI.")
    else:
        st.info("Offline demonstration: real financial rules and eligible ML checks run, but no AI model is called.")
    return mode, model


def save_bundle(root: Path, data, report):
    from workbench.workspace import atomic_json, safe_path
    location = safe_path(root, f"clarifi/{report['investigation_id']}.json")
    atomic_json(location, {"input": data.model_dump(mode="json"), "report": report})
    return location


def payment_network(payments):
    """Directed edge plot supports cycles; a Sankey diagram cannot show these reliably."""
    subset = payments[:100]
    nodes = sorted({payment[key] for payment in subset for key in ("sender_id", "receiver_id")})
    positions = {key: (math.cos(2*math.pi*i/len(nodes)), math.sin(2*math.pi*i/len(nodes))) for i, key in enumerate(nodes)}
    fig = go.Figure()
    for payment in subset:
        x0, y0 = positions[payment["sender_id"]]
        x1, y1 = positions[payment["receiver_id"]]
        fig.add_trace(go.Scatter(x=[x0, x1], y=[y0, y1], mode="lines", line={"color": "#89b6bd", "width": 2},
                                text=[f"{payment['transaction_id']} · {payment['amount']:,.2f} {payment['currency']}"]*2,
                                hovertemplate="%{text}<extra></extra>", showlegend=False))
        fig.add_annotation(x=x1*0.8+x0*0.2, y=y1*0.8+y0*0.2, ax=x0*0.65+x1*0.35, ay=y0*0.65+y1*0.35,
                           xref="x", yref="y", axref="x", ayref="y", text="", showarrow=True, arrowhead=2, arrowcolor="#176b72")
    fig.add_trace(go.Scatter(x=[positions[key][0] for key in nodes], y=[positions[key][1] for key in nodes],
                            mode="markers+text", text=nodes, textposition="top center", marker={"size": 24, "color": "#176b72"},
                            hovertemplate="Entity %{text}<extra></extra>", showlegend=False))
    fig.update_layout(height=400, margin={"l": 30, "r": 30, "t": 30, "b": 20}, plot_bgcolor="white",
                      xaxis={"visible": False, "range": [-1.25, 1.25]},
                      yaxis={"visible": False, "range": [-1.25, 1.25], "scaleanchor": "x"})
    return fig


def render_results(bundle, key="current"):
    report, source = bundle["report"], bundle["input"]
    all_findings = report["findings"]
    st.divider()
    st.subheader("Investigation results")
    provider = report["provenance"]["reasoning_provider"]
    model = report["provenance"].get("model") or "No AI model"
    st.caption(f"{report['case_id']} · {model} · {provider} · {report['created_at']}")
    if report["provenance"]["ai_used"]:
        st.success("Model-backed investigation completed · evidence references and score contributions verified")
    elif report.get("status") == "SCREENED":
        st.success("Financial screening saved · scores and evidence verified")
        st.caption("AI analyst draft has not been requested. Scores are computed by reviewed financial tools.")
    else:
        st.info("Offline rules report · no AI model was called")
    selection = st.radio("Findings to display", ["High-priority review", "All findings"], horizontal=True,
                         key=f"clarifi_results_filter_{key}_{report['investigation_id']}")
    findings = findings_for_display(report, high_priority=selection == "High-priority review")
    st.caption(f"Showing {len(findings)} of {len(all_findings)} findings. High-priority review requires an analyst-review disposition and a triage score of at least 80/100. The complete report remains available in the export.")
    columns = st.columns(4)
    columns[0].metric("Findings", len(findings))
    columns[1].metric("Analyst review", sum(f["disposition"] == "analyst_review" for f in findings))
    columns[2].metric("Identity review", sum(f["disposition"] == "identity_review" for f in findings))
    columns[3].metric("Highest triage score", f"{max((f['score'] for f in findings), default=0)}/100")
    st.caption("Scores prioritize analyst review. They are not fraud probabilities or measured detection accuracy.")

    queue, evidence, narrative, execution = st.tabs(["Review queue", "Connections & evidence", "Analyst draft", "Execution & export"])
    with queue:
        if not findings:
            st.info("No findings meet this review filter. Select All findings to inspect weaker leads and evidence gaps; this does not establish absence of risk.")
        frame = pd.DataFrame([{"Finding": f["finding_id"], "Capability": f["capability"].replace("_", " ").title(), "Subject": f["subject_id"], "Score": f["score"],
                               "Disposition": f["disposition"].replace("_", " ").title(),
                               "Signals": ", ".join(s["name"].replace("_", " ") for s in f["signals"]),
                               "Anomaly percentile": f["anomaly_percentile"]} for f in findings],
                             columns=["Finding", "Capability", "Subject", "Score", "Disposition", "Signals", "Anomaly percentile"])
        st.dataframe(frame, hide_index=True, width="stretch", column_config={
            "Score": st.column_config.ProgressColumn("Triage score", min_value=0, max_value=100, format="%d/100")})
        if findings:
            st.bar_chart(pd.DataFrame({"Finding": [f["finding_id"] for f in findings[:20]],
                                   "Score": [f["score"] for f in findings[:20]]}), x="Finding", y="Score", color="#176b72")
        st.caption("Highest-scoring 20 findings are charted. Multiple findings may refer to the same account; findings are not a count of unique people.")
    with evidence:
        evidence_ids = {reference for finding in findings for reference in finding["evidence_ids"]}
        if source.get("payments") and findings:
            # Each currency gets its own amount chart; currencies are never summed.
            payments = [payment for payment in source["payments"] if f"payments:{payment['transaction_id']}" in evidence_ids]
            # Trading-only selections have no payment-network evidence.
            currencies = sorted({p["currency"] for p in payments})
            if currencies:
                currency = st.selectbox("Payment flow currency", currencies, key=f"clarifi_currency_{key}")
                subset = [p for p in payments if p["currency"] == currency]
                st.plotly_chart(payment_network(subset), width="stretch", key=f"clarifi_network_{key}")
                st.caption(f"Payment evidence for displayed findings in {currency}. Arrows show direction; line width does not encode amount. Showing at most 100 payments; the full report retains all findings.")
        selected = st.selectbox("Inspect a finding", [f["finding_id"] for f in findings], key=f"clarifi_finding_{key}") if findings else None
        if selected:
            render_finding(next(f for f in findings if f["finding_id"] == selected), source)
    with narrative:
        if report.get("status") == "SCREENED":
            st.info("Generate an AI analyst draft after reviewing the scores. Your screening results are already saved.")
        st.caption("Draft explanations require analyst review. Citation checks do not establish that the text is semantically correct.")
        st.write("**Investigation plan**")
        st.write(report["plan"]["rationale"])
        visible_ids = {finding["finding_id"] for finding in findings}
        for claim in report["narrative"]["claims"]:
            if claim["finding_id"] not in visible_ids:
                continue
            with st.container(border=True):
                st.write("**" + claim["finding_id"] + "**")
                st.write(claim["explanation"])
                st.caption("Sources: " + ", ".join(claim["evidence_ids"]))
        st.write("**Suggested next steps**")
        for step in report["narrative"]["next_steps"]:
            st.write("• " + step)
    with execution:
        st.dataframe(pd.DataFrame(report["trace"]), hide_index=True, width="stretch")
        st.json(report["provenance"], expanded=False)
        st.caption("Input fingerprint: " + report["input_sha256"])
        st.download_button("Download investigation report", json.dumps(report, indent=2),
                           file_name=f"clarifi-{report['investigation_id']}.json", mime="application/json", key=f"clarifi_export_{key}")


def findings_for_display(report, *, high_priority=True):
    """Presentation filter only; scores are not calibrated fraud probabilities."""
    return sorted((finding for finding in report["findings"] if not high_priority or
                   (finding["disposition"] == "analyst_review" and finding["score"] >= 80)),
                  key=lambda finding: (-finding["score"], finding["finding_id"]))


def render_finding(finding, source):
    st.write(f"**{finding['subject_id']} · {finding['score']}/100 · {finding['disposition'].replace('_', ' ')}**")
    st.dataframe(pd.DataFrame(finding["signals"]), hide_index=True, width="stretch")
    st.write("**Evidence references**")
    st.code("\n".join(finding["evidence_ids"]), language=None)
    st.write("**Evidence gaps and limitations**")
    for gap in finding["gaps"]:
        st.write("• " + gap)
    with st.expander("Computed facts and original referenced rows"):
        st.json(finding["facts"], expanded=False)
        refs = set(finding["evidence_ids"])
        for table, key in (("employees", "employee_id"), ("accounts", "account_id"),
                           ("relationships", "relationship_id"), ("trades", "trade_id"),
                           ("events", "event_id"), ("entities", "entity_id"), ("payments", "transaction_id")):
            rows = [row for row in source.get(table, []) if f"{table}:{row[key]}" in refs]
            if rows:
                st.caption(LABELS[table])
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

def financial_page(service):
    st.markdown("<style>" + Path(__file__).with_name("clarifi.css").read_text() + "</style>", unsafe_allow_html=True)
    st.markdown('<div class="cf-wordmark"><span class="cf-mark" aria-hidden="true">C</span><span>ClariFi <span class="cf-muted">/ Financial intelligence</span></span></div>', unsafe_allow_html=True)
    st.header("ClariFi Agent")
    st.markdown('''<div class="cf-welcome"><span class="cf-eyebrow">BFSI · INTELLIGENT RISK &amp; FINANCIAL INVESTIGATIONS</span>
<h2>Know who you’re doing business with.<br><span class="cf-accent">See the signals in your trading data.</span></h2>
<p>From reviewing a new business partner to investigating potential insider trading, ClariFi connects the records you provide and helps your team decide what deserves a closer look.</p></div>
<div class="cf-capabilities">
<article class="cf-card"><span class="cf-number">01 / COMPANY DUE DILIGENCE</span><h3>Onboarding a new company?<br>Start with the connections.</h3>
<p>Before you sign a contract or onboard a supplier, bring together company and payment records. ClariFi traces circular payments, shared bank accounts and documented ownership links that may warrant a shell-company review.</p>
<div class="cf-detail"><b>You provide</b> Company records, payments and available ownership details.<br><b>You receive</b> Connection maps, evidence-linked signals and a scored review queue.</div></article>
<article class="cf-card"><span class="cf-number">02 / INSIDER TRADING REVIEW</span><h3>Protect trust in your company.<br>Connect people to trading patterns.</h3>
<p>Concerned about the financial and reputational impact of employee trading? ClariFi matches employees and declared related accounts to trading history, issuer access and public event dates to surface patterns for investigation.</p>
<div class="cf-detail"><b>You provide</b> Employees, trading accounts and trades; add declared relationships and event dates.<br><b>You receive</b> Prioritized cases, identity-review leads and traceable score breakdowns.</div></article></div>''', unsafe_allow_html=True)
    st.caption("ClariFi investigates supplied records. Company checks do not include external registry searches; findings guide analyst review and do not establish wrongdoing.")
    st.subheader("ClariFi Capabilities")
    st.write("Choose a capability, bring your data, and let the agent prepare an evidence-linked investigation.")
    intake_mode = st.radio("Data preparation", ["Ready-to-use ClariFi data", "Prepare with Migration"],
                          horizontal=True, key="clarifi_intake_mode")
    selected = st.radio("Choose your capability", list(CAPABILITIES), horizontal=True, key="clarifi_capability")
    tool = CAPABILITIES[selected]
    if intake_mode == "Prepare with Migration":
        from ui.clarifi_preparation import preparation_panel
        preparation_panel(service, tool)
        return
    if tool == "combined":
        st.write("One workbook, two investigations. Upload all seven evidence worksheets to review employee trading and company payment networks in the same run.")
    elif tool == "insider_trading":
        st.write("Match employees and declared related accounts to trades and public market events. Ambiguous names and initials stay in identity review.")
    else:
        st.write("Trace chronological payment cycles and shared company records, while keeping documented treasury activity in context.")
    st.markdown('''<div class="cf-steps"><div><b>1. Bring your evidence</b><span>Upload JSON, CSV or the Excel template.</span></div><div><b>2. Choose your model</b><span>Let AI plan and explain the investigation.</span></div><div><b>3. Review the findings</b><span>Follow the scores back to their source records.</span></div></div>''', unsafe_allow_html=True)
    intake, config = st.columns([2.2, 1], gap="large")
    with intake:
        st.subheader("1 · Prepare the evidence")
        data = input_panel(tool)
    with config:
        st.subheader("2 · Choose the model")
        mode, model = model_panel()
    delivery = st.radio("Results delivery", ["Show scores first", "Wait for the full AI report"],
                        horizontal=True, key="clarifi_delivery_mode")
    if delivery == "Show scores first":
        scores_first_panel(service, data, mode, model)
    else:
        fingerprint = input_fingerprint(data, mode, model) if data else None
        approved = st.checkbox("I have reviewed the input and approve this investigation", key=f"clarifi_approve_{fingerprint}")
        unavailable = not data or not approved or (mode != "rules" and not model)
        if st.button("Run Investigation", type="primary", disabled=unavailable, key="clarifi_run"):
            st.session_state.pop("clarifi_current_result", None)
            st.session_state.pop("clarifi_reopened_bundle", None)
            try:
                with st.status("Running the approved investigation…", expanded=True) as status:
                    st.write("The agent plans reviewed tool calls, computes financial signals, checks evidence and drafts the analyst report.")
                    from time import monotonic
                    from workbench.agents.clarifi.progress import track_progress
                    started = monotonic()
                    progress_note = st.empty()
                    def update_progress(event):
                        status.update(label=event["message"])
                        progress_note.caption(f"Elapsed {monotonic() - started:.0f} seconds · CPU model requests can take several minutes. Avoid submitting the same investigation again while it runs.")
                    with track_progress(update_progress):
                        report = service.run_agent("clarifi", data.model_dump(mode="json"), approved=True, mode=mode, model=model)
                    location = save_bundle(service.settings.workspace_root, data, report)
                    bundle = {"input": data.model_dump(mode="json"), "report": report}
                    st.session_state["clarifi_current_result"] = {"fingerprint": fingerprint, "bundle": bundle}
                    status.update(label="Investigation completed and saved" if mode == "remote" else "Investigation completed and saved locally", state="complete", expanded=False)
                    if report.get("evidence_uri"):
                        st.caption("Cloud evidence bundle: " + report["evidence_uri"])
                    st.caption(f"Saved evidence bundle: {location.name}")
            except Exception as error:
                # Local adapter errors are redacted by the shared client; do not echo
                # raw Pydantic/model/cloud exceptions that may contain submitted rows.
                from workbench.llm import LLMError
                from workbench.agents.clarifi.remote import RemoteClariFiError
                if isinstance(error, (LLMError, RemoteClariFiError)):
                    st.error(str(error))
                elif isinstance(error, RuntimeError) and str(error).startswith(("Gemini API request failed (HTTP ", "Gemini request ", "Gemini could not be reached.")):
                    st.error(str(error))
                else:
                    st.error("The investigation did not complete. Check model readiness, configuration and input scope. No new result was saved.")
        result = st.session_state.get("clarifi_current_result")
        if result and result["fingerprint"] != fingerprint:
            st.info("Input or model selection changed. Run a new investigation to see results for this selection.")
        elif result and not st.session_state.get("clarifi_reopened_bundle"):
            render_results(result["bundle"])
    with st.expander("Reopen a saved financial investigation"):
        root = Path(service.settings.workspace_root).resolve()
        directory = root / "clarifi"
        # An independent directory keeps financial bundles out of the original
        # WorkspaceManager's uc_* namespace and checkpoint lifecycle.
        files = [] if directory.is_symlink() else sorted(directory.glob("*.json"), key=lambda p: p.name, reverse=True)
        files = [p for p in files if not p.is_symlink() and p.stat().st_size <= 16 * 1024 * 1024][:100]
        if files:
            saved = st.selectbox("Saved evidence bundle", files, format_func=lambda p: p.stem)
            if st.button("Open financial report"):
                st.session_state["clarifi_reopened_bundle"] = json.loads(saved.read_text(encoding="utf-8"))
                st.rerun()
        else:
            st.caption("Completed investigations appear here after the first run.")
    reopened = st.session_state.get("clarifi_reopened_bundle")
    if reopened:
        st.caption("Saved report · separate from the current input selection")
        if st.button("Close saved report"):
            st.session_state.pop("clarifi_reopened_bundle")
            st.rerun()
        render_results(reopened, key="saved")


def scores_first_panel(service, data, mode, model, *, key="direct", tool_executor=None, migration=None):
    """Persist evidence before optional model generation; selection changes cannot erase scores."""
    from workbench.agents.clarifi.screening import screen, draft
    fingerprint = input_fingerprint(data, "screen", None) if data else None
    state_key = "clarifi_screen_" + key
    approved = st.checkbox("I have reviewed the input and approve this investigation",
                           key=f"clarifi_screen_approve_{key}_{fingerprint}")
    if st.button("Run Investigation", type="primary", disabled=not data or not approved,
                 key="clarifi_screen_run_" + key):
        try:
            with st.spinner("Computing and verifying financial scores…"):
                from time import monotonic
                started = monotonic()
                report = screen(data, approved=True, tool_executor=tool_executor)
                if migration:
                    report["provenance"]["migration"] = migration
                save_bundle(service.settings.workspace_root, data, report)
                bundle = {"input": data.model_dump(mode="json"), "report": report}
                st.session_state[state_key] = {"fingerprint": fingerprint, "bundle": bundle,
                                               "seconds": monotonic() - started}
                st.session_state.pop("clarifi_reopened_bundle", None)
        except Exception:
            st.error("Screening did not complete. Check the input and sandbox execution logs. No incomplete scores were saved.")
    result = st.session_state.get(state_key)
    if not result or result["fingerprint"] != fingerprint:
        if result:
            st.info("Input changed. Review and run screening for these records.")
        return
    st.caption(f"Verified screening completed and saved in {result['seconds']:.2f} seconds. AI drafting is a separate step.")
    # Render first, so a slow or failed model cannot hide the verified queue.
    render_results(result["bundle"], key="screen_" + key)
    if mode == "rules":
        st.caption("Select Gemini or Gemma to add an AI analyst draft.")
        return
    draft_fp = input_fingerprint(data, mode, model)
    consent = st.checkbox("Generate an AI analyst draft using the selected model",
                          key=f"clarifi_draft_approve_{key}_{fingerprint}_{draft_fp}")
    if st.button("Generate AI analyst draft", disabled=not consent or not model,
                 key="clarifi_draft_run_" + key):
        try:
            with st.spinner("Generating the AI analyst draft. Your verified scores are already saved above…"):
                if mode == "remote":
                    from workbench.agents.clarifi.remote import draft as remote_draft
                    report = remote_draft(data, result["bundle"]["report"], approved=True, model=model)
                else:
                    report = draft(data, result["bundle"]["report"], approved=True, mode=mode,
                                   model=model, tool_executor=tool_executor)
                if migration:
                    report["provenance"]["migration"] = migration
                save_bundle(service.settings.workspace_root, data, report)
                result["draft"] = {"fingerprint": draft_fp,
                                   "bundle": {"input": data.model_dump(mode="json"), "report": report}}
        except Exception:
            st.error("AI drafting did not complete. Your verified screening is saved and remains available above. Check model readiness, quota and connection before trying again.")
    completed = result.get("draft")
    if completed and completed["fingerprint"] == draft_fp:
        render_results(completed["bundle"], key="draft_" + key)
    elif completed:
        st.info("The saved AI draft belongs to a different model selection. Review the new model before requesting another draft.")
