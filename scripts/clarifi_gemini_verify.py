"""Explicit Gemini API verification using bundled synthetic cases; no key output."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import sample_input
from workbench.agents.clarifi.models import GeminiAPIReasoner
from workbench.workspace import atomic_json


class VerificationReasoner(GeminiAPIReasoner):
    last_stage = "configuration"

    def plan(self, context):
        self.last_stage = "planning"
        result = super().plan(context)
        print("Gemini planning response validated", flush=True)
        return result

    def summarize(self, context):
        self.last_stage = "analyst_draft"
        result = super().summarize(context)
        print("Gemini analyst draft validated", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-model-run", action="store_true")
    parser.add_argument("--probe", action="store_true", help="Minimal text call to separate API access from investigation-schema errors")
    parser.add_argument("--probe-schema", action="store_true", help="Compare a minimal and investigation JSON schema without source records")
    args = parser.parse_args()
    if not args.approve_model_run:
        parser.error("Pass --approve-model-run to call Google with the synthetic examples")
    reasoner = VerificationReasoner()
    if args.probe_schema:
        from workbench.agents.clarifi.models import scoped_plan_schema, gemini_json_schema
        schemas = {
            "simple": {"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]},
            "plan": gemini_json_schema(scoped_plan_schema({"available_tools":["insider_trading"]}).model_json_schema()),
        }
        for name, schema in schemas.items():
            try:
                with reasoner._client() as client:
                    response = client.interactions.create(model=reasoner.model,
                        input='Return a JSON object. If tools are requested use ["insider_trading"] and rationale "Review trades". Otherwise answer "OK".',
                        store=False, timeout=30, response_format={"type":"text","mime_type":"application/json","schema":schema})
                print({"schema":name,"status":response.status},flush=True)
            except Exception as error:
                print({"schema":name,"status":"failed","diagnostic":str(getattr(error,"message"," ")).replace(reasoner._api_key,"[redacted]")[:1000]},flush=True)
        return 0
    if args.probe:
        try:
            with reasoner._client() as client:
                response = client.interactions.create(model=reasoner.model, input="Reply with OK.", store=False, timeout=30)
            print({"probe": "completed", "status": response.status, "has_text": bool(getattr(response, "output_text", None))})
            return 0
        except Exception as error:
            print({"probe": "failed", "status": getattr(error, "status_code", getattr(error, "code", None)),
                   "diagnostic": str(getattr(error, "message", "")).replace(reasoner._api_key, "[redacted]")[:1000]})
            return 1
    health = reasoner.health()
    results = {"model": reasoner.model, "health": health, "cases": []}
    if health["available"]:
        for tool in ("insider_trading", "shell_companies"):
            try:
                data = sample_input(tool)
                report = ClariFiAgent(mode="gemini", reasoner=reasoner).run(data, approved=True)
                atomic_json(Path("workspaces/clarifi") / (report["investigation_id"] + ".json"),
                            {"input": data.model_dump(mode="json"), "report": report})
                item = {"tool": tool, "status": report["status"], "findings": len(report["findings"]),
                        "ai_used": report["provenance"]["ai_used"], "investigation_id": report["investigation_id"]}
            except Exception as error:
                item = {"tool": tool, "status": "FAILED", "error_type": type(error).__name__, "stage": reasoner.last_stage}
                if isinstance(error, RuntimeError) and str(error).startswith("Gemini API request failed (HTTP "):
                    item["error"] = str(error)
                    cause = error.__context__
                    message = str(getattr(cause, "message", ""))
                    if message:
                        item["diagnostic"] = message.replace(reasoner._api_key, "[redacted]")[:1500]
            results["cases"].append(item)
            if item["status"] == "FAILED":
                break
    atomic_json(Path("workspaces/clarifi-gemini-verification.json"), results)
    import json
    print(json.dumps(results, indent=2))
    return int(not health["available"] or any(case["status"] != "COMPLETED" for case in results["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
