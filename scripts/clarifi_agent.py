"""Review a JSON investigation bundle, then explicitly approve tool execution."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.schemas import InvestigationInput
from workbench.agents.clarifi.storage import persist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Validated JSON tables; see examples/clarifi")
    parser.add_argument("--mode", choices=["vertex", "gemini", "local", "rules"], default="vertex")
    parser.add_argument("--model", help="Explicit Ollama or Vertex model ID")
    parser.add_argument("--approve-investigation", action="store_true")
    parser.add_argument("--output", type=Path, help="Save report; refuses to overwrite an existing file")
    args = parser.parse_args()
    if args.input.stat().st_size > 5 * 1024 * 1024:
        parser.error("Input exceeds 5 MiB")
    data = InvestigationInput.model_validate_json(args.input.read_text(encoding="utf-8"))
    if not args.approve_investigation:
        print(json.dumps({"case_id": data.case_id, "available_tools": data.tools,
                          "status": "AWAITING_APPROVAL", "mode": args.mode,
                          "note": "No tools or AI called. Review inputs and rerun with --approve-investigation."}, indent=2))
        return 0
    report = ClariFiAgent(mode=args.mode, model=args.model).run(data, approved=True)
    report["evidence_uri"] = persist(data, report)
    rendered = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
