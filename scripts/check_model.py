"""Check the configured local provider; optionally verify a real structured response."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate', action='store_true', help='Run one small structured-response check with the real model.')
    args = parser.parse_args()
    from pydantic import BaseModel
    from workbench.llm import create_llm
    from workbench.settings import Settings

    class Ready(BaseModel):
        status: Literal['ready']
        count: Literal[3]

    settings = Settings.from_env()
    client = create_llm(settings)
    try:
        health = client.health()
        report = {'provider': settings.llm_provider, 'health': health}
        if not (health.get('available') and health.get('model_available')):
            print(json.dumps(report, indent=2))
            return 1
        if args.generate:
            started = time.monotonic()
            value = client.structured(Ready, 'Return the requested JSON object.',
                                      'Set status to ready and count to the integer 3.')
            report.update(structured_response=value.model_dump(), elapsed_seconds=round(time.monotonic() - started, 2))
        print(json.dumps(report, indent=2))
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
