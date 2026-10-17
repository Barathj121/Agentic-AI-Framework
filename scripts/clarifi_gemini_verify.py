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
    parser.add_argument("--probe-http", action="store_true", help="Bounded direct HTTP checks without SDK retries; synthetic content only")
    parser.add_argument("--model", help="Explicit Gemini or Gemma API model ID; no automatic switching")
    parser.add_argument("--capability", choices=["insider_trading", "shell_companies", "combined"], help="Verify only this input scope")
    parser.add_argument("--input", type=Path, help="Explicit JSON evidence bundle to verify instead of the small built-in case")
    parser.add_argument("--diagnose", action="store_true", help="Print redacted provider diagnostics; use synthetic data because provider text may contain submitted identifiers")
    parser.add_argument("--probe-schema", action="store_true", help="Compare a minimal and investigation JSON schema without source records")
    args = parser.parse_args()
    if not args.approve_model_run:
        parser.error("Pass --approve-model-run to call Google with the synthetic examples")
    if args.input and not args.capability:
        parser.error("Use --capability with --input to specify the intended scope")
    print("Loading configured Gemini provider", flush=True)
    reasoner = VerificationReasoner(args.model)
    if args.probe_http:
        import httpx
        import time
        from workbench.agents.clarifi.models import gemini_request_error
        with httpx.Client(timeout=20) as client:
            for name, method, url, body in (
                ("model_lookup", "GET", f"https://generativelanguage.googleapis.com/v1beta/models/{reasoner.model}", None),
                ("minimal_interaction", "POST", "https://generativelanguage.googleapis.com/v1beta/interactions", {"model":reasoner.model,"input":"Reply with OK.","store":False}),
            ):
                started = time.monotonic()
                print(f"Starting {name}", flush=True)
                try:
                    response = client.request(method, url, headers={"x-goog-api-key":reasoner._api_key}, json=body)
                    print({"probe":name,"http_status":response.status_code,"elapsed_seconds":round(time.monotonic()-started,2)},flush=True)
                    if not response.is_success:
                        error_data = response.json().get("error", {})
                        print({"api_error_status":error_data.get("status"),"diagnostic":str(error_data.get("message", "")).replace(reasoner._api_key,"[redacted]")[:1500]},flush=True)
                    if response.is_success and name == "minimal_interaction":
                        print({"interaction_status":response.json().get("status")},flush=True)
                except Exception as error:
                    print({"probe":name,"error_type":type(error).__name__,"diagnostic":gemini_request_error(error,timeout_seconds=20),"elapsed_seconds":round(time.monotonic()-started,2)},flush=True)
        return 0
    if args.probe_schema:
        from workbench.agents.clarifi.models import scoped_plan_schema, gemini_json_schema
        schemas = {
            "simple": {"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]},
            "plan": gemini_json_schema(scoped_plan_schema({"available_tools":["insider_trading"]}).model_json_schema()),
        }
        for name, schema in schemas.items():
            try:
                response = reasoner._interaction({"model":reasoner.model,
                    "input":'Return a JSON object. If tools are requested use ["insider_trading"] and rationale "Review trades". Otherwise answer "OK".',
                    "store":False,"response_format":{"type":"text","mime_type":"application/json","schema":schema}},timeout_seconds=30)
                print({"schema":name,"status":response.get("status")},flush=True)
            except Exception as error:
                print({"schema":name,"status":"failed","diagnostic":str(error) if isinstance(error,RuntimeError) else type(error).__name__},flush=True)
                return 1
        return 0
    if args.probe:
        try:
            response = reasoner._interaction({"model":reasoner.model,"input":"Reply with OK.","store":False},timeout_seconds=30)
            print({"probe": "completed", "status": response.get("status")})
            return 0
        except Exception as error:
            print({"probe": "failed", "diagnostic": str(error) if isinstance(error,RuntimeError) else type(error).__name__})
            return 1
    health = reasoner.health()
    results = {"model": reasoner.model, "health": health, "cases": []}
    if health["available"]:
        for tool in ([args.capability] if args.capability else ("insider_trading", "shell_companies")):
            try:
                if args.input:
                    from workbench.agents.clarifi.schemas import InvestigationInput
                    from workbench.agents.clarifi.ingest import validate_scope
                    data = validate_scope(InvestigationInput.model_validate_json(args.input.read_bytes()), tool)
                else:
                    data = sample_input(tool)
                report = ClariFiAgent(mode="gemini", reasoner=reasoner).run(data, approved=True)
                atomic_json(Path("workspaces/clarifi") / (report["investigation_id"] + ".json"),
                            {"input": data.model_dump(mode="json"), "report": report})
                item = {"tool": tool, "status": report["status"], "findings": len(report["findings"]),
                        "ai_used": report["provenance"]["ai_used"], "investigation_id": report["investigation_id"]}
            except Exception as error:
                item = {"tool": tool, "status": "FAILED", "error_type": type(error).__name__, "stage": reasoner.last_stage}
                if isinstance(error, RuntimeError) and str(error).startswith(("Gemini API request failed (HTTP ", "Gemini request ", "Gemini could not be reached.")):
                    item["error"] = str(error)
                    response = getattr(error.__context__, "response", None)
                    if args.diagnose and response is not None:
                        detail = response.json().get("error", {}).get("message", "")
                        item["provider_diagnostic"] = str(detail).replace(reasoner._api_key,"[redacted]")[:1500]
            results["cases"].append(item)
            if item["status"] == "FAILED":
                break
    atomic_json(Path("workspaces/clarifi-gemini-verification.json"), results)
    import json
    print(json.dumps(results, indent=2))
    return int(not health["available"] or any(case["status"] != "COMPLETED" for case in results["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
