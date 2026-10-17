# MVP verification and handoff

Verified on 17 October 2026. This is a local hackathon MVP, with further testing on the personal laptop requested by the owner.

- **Automated tests:** 290 passed, zero failures, errors or skips, in 282.503 seconds. This includes actual Docker integration tests and deterministic mocked model responses.
- **Local model:** `gemma3:4b` installed and verified. Basic inference and structured JSON through Google ADK passed. Native tool calling is not required; approved tools run deterministically.
- **Migration Studio:** live Gemma mapping proposals were reviewed and corrected before approval. Gemma generated the approved salary expression; the existing Docker sandbox executed it successfully. Eight target rows were materialized, and all eight salary values matched an independent calculation. CSV and connector ZIP downloads were verified in the browser.
- **Intentional migration gaps:** all eight synthetic records lack a required tax identifier, so the result is correctly `MIGRATION_INCOMPLETE`, with zero fully valid records and 13 validation issues. Missing employment status remains null. This is a partial preview, not a completed payroll migration.
- **Generic workbench:** automated regression and actual Docker tests passed. Live Gemma produced a feasible proposal after bounded repairs, and the proposal was approved in Streamlit. The final live code-generation run was interrupted and cancelled when the owner requested handoff; its end-to-end result is **not verified**.
- **External applications:** local image registration, localhost launch, HTTP page access and stop were verified with real Docker and the Streamlit interface.

Small local models still require review of business meanings and generated plans. The live migration demonstration covers the SQL branch; the Python fallback has contract/routing tests but was not demonstrated with live Gemma. Connector ZIP files are source/configuration packages for manual review and reuse, with no automatic import/replay interface or verified vendor import compatibility.

Start with [personal laptop setup](PERSONAL-LAPTOP-SETUP.md), then use the [three-demo guide](MVP-DEMO-GUIDE.md). Set the installed Gemma tag in `.env`, or use `OLLAMA_MODEL=auto`. Local `.env`, workspaces, downloaded models and virtual environments are intentionally excluded from Git.
