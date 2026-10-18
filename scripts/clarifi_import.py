"""Convert separately supplied financial CSV tables to ClariFi's validated bundle."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.agents.clarifi.schemas import InvestigationInput
from workbench.agents.clarifi.ingest import TABLES, read_csv_bytes


def load_csv(path):
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("CSV exceeds 5 MiB")
    return read_csv_bytes(path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    for table in TABLES:
        parser.add_argument("--" + table, type=Path)
    args = parser.parse_args()
    data = InvestigationInput.model_validate({"case_id": args.case_id, **{
        table: load_csv(getattr(args, table)) for table in TABLES if getattr(args, table)}})
    with args.output.open("x", encoding="utf-8") as output:
        output.write(data.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
