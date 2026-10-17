"""Opt-in, real local-model verification of bundled synthetic ClariFi cases."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import sample_input
from workbench.workspace import atomic_json


def verify_ui(tool, model):
    from streamlit.testing.v1 import AppTest
    labels = {"insider_trading": "Insider trading indicators", "shell_companies": "Shell-company networks"}
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=180)
    app.session_state["page"] = "ClariFi Agent"
    app.run()
    next(item for item in app.radio if item.label == "Choose your capability").set_value(labels[tool]).run()
    next(item for item in app.text_input if item.label == "Local model ID").set_value(model).run()
    app.checkbox[0].check().run()
    next(button for button in app.button if button.label == "Run Investigation").click().run(timeout=180)
    if app.exception or app.error:
        raise ValueError("UI investigation failed: " + "; ".join(item.value for item in app.error))
    report = app.session_state["clarifi_current_result"]["bundle"]["report"]
    return report, [{"label": item.label, "value": item.value} for item in app.metric]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemma3:1b")
    parser.add_argument("--capability", choices=["insider_trading", "shell_companies", "both"], default="both")
    parser.add_argument("--approve-model-run", action="store_true")
    parser.add_argument("--ui", action="store_true", help="Exercise the actual Streamlit intake, approval and result rendering with AppTest")
    args = parser.parse_args()
    if not args.approve_model_run:
        parser.error("Review the bundled synthetic inputs and pass --approve-model-run")
    results = []
    for tool in (["insider_trading", "shell_companies"] if args.capability == "both" else [args.capability]):
        try:
            metrics = []
            if args.ui:
                report, metrics = verify_ui(tool, args.model)
            else:
                report = ClariFiAgent(mode="local", model=args.model).run(sample_input(tool), approved=True)
                atomic_json(Path("workspaces/clarifi") / (report["investigation_id"] + ".json"),
                            {"input": sample_input(tool).model_dump(mode="json"), "report": report})
            item = {"capability": tool, "status": report["status"], "ai_used": report["provenance"]["ai_used"],
                    "model": args.model, "findings": len(report["findings"]), "investigation_id": report["investigation_id"],
                    "surface": "streamlit_apptest" if args.ui else "agent", "ui_metrics": metrics}
        except Exception as error:
            item = {"capability": tool, "status": "FAILED", "model": args.model,
                    "error_type": type(error).__name__, "error": str(error)[:1000]}
        results.append(item)
        print(json.dumps(item), flush=True)
    atomic_json(Path("workspaces/clarifi-local-ui-verification.json" if args.ui else "workspaces/clarifi-local-model-verification.json"), results)
    return int(any(item["status"] != "COMPLETED" for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
