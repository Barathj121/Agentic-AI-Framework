"""Opt-in real Ollama smoke test; execution requires separate explicit approval."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-ollama", action="store_true", help="Required: run the configured real local model.")
    parser.add_argument("--workspace", help="Existing smoke workspace ID to approve after reviewing its proposals.")
    parser.add_argument("--approve", metavar="PROPOSAL_ID", help="Explicitly approve this proposal in --workspace and execute in Docker.")
    args = parser.parse_args()
    if not args.real_ollama:
        parser.error("Pass --real-ollama to opt in to live local inference. The standard tests use deterministic test adapters.")
    if bool(args.workspace) != bool(args.approve):
        parser.error("Use --workspace and --approve together, after reviewing the generated proposals.")

    from workbench.service import Service
    from workbench.settings import Settings
    from workbench.workflow import Workflow

    settings = Settings.from_env()
    service = Service(settings)
    workflow = Workflow(settings)
    if args.workspace:
        workspace = service.manager.get(args.workspace)
        workflow.approve(workspace, args.approve)
    else:
        content = b"customer_id,amount\nC1,10\nC1,15\nC2,20\nC2,25\nC3,30\nC3,35\n"
        workspace = service.create_workspace("Real Ollama smoke test",
            "Use SQL to sum amount by customer_id. Create results.customer_totals with customer_id and total_amount. "
            "Validate there are three rows and the grand total is 135. Save reusable SQL/Python and a notebook.",
            content, "smoke.csv")
        workflow.propose(workspace)
    metadata = service.status(workspace.name)
    print(json.dumps(metadata, indent=2, default=str))
    if not args.workspace:
        print(json.dumps(service.manager.read(workspace, "metadata/proposals.json", {}), indent=2))
        print("\nReview these proposals. To approve one and execute it in Docker, run:")
        print(f'python scripts/smoke_test.py --real-ollama --workspace "{workspace.name}" --approve "<proposal_id>"')
        return 0 if metadata.get("status") == "AWAITING_APPROVAL" else 1
    print(json.dumps(service.manager.read(workspace, "metadata/validation.json", {}), indent=2))
    print(f"Persistent workspace: {workspace.resolve()}")
    return 0 if metadata.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
