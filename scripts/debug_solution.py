"""Inspect edited generated files, or explicitly execute their snapshot in a new workspace."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from workbench.debug import debug_solution
    from workbench.settings import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Existing workspace ID under WORKSPACE_ROOT")
    parser.add_argument("--execute-reviewed-code", action="store_true",
                        help="Approve current generated file contents for one Docker run in a NEW workspace; never calls a model")
    args = parser.parse_args()
    try:
        report = debug_solution(Settings.from_env(), args.workspace, execute_reviewed_code=args.execute_reviewed_code)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "execution_requested": args.execute_reviewed_code}, indent=2))
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("dry_run") or report.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
