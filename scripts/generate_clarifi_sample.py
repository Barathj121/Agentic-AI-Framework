"""Reproducible simulated evidence for public-case-inspired ClariFi scenarios.

Writes JSON, schema and the comparison guide. Workbook authoring is separate.
All operational rows are simulated, including any rows with historical names.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.agents.clarifi.agent import ClariFiAgent
from workbench.agents.clarifi.ingest import TABLES
from workbench.agents.clarifi.schemas import InvestigationInput

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs/clarifi-demo-01a14a33"
SEC = "https://www.sec.gov/newsroom/press-releases/2023-98"
DOJ = "https://www.justice.gov/opa/press-release/file/1291146/dl"


def generate():
    rng = random.Random(20261017)
    data = {table: [] for table in TABLES}
    data["case_id"] = "simulated-public-case-review-v1"
    first = "Avery Blake Casey Drew Ellis Finley Gale Harper Indy Jules Kai Lane Morgan Noel Oakley Parker Quinn River Sage Taylor".split()
    last = "Amber Brook Cedar Dale Elm Frost".split()
    for i in range(119):
        employee = f"SIM_E{i+1:03}"
        name = f"{first[i//6]} {last[i%6]}"
        issuer = f"SIM_CORP_{i%3+1}"
        data["employees"].append(dict(employee_id=employee, name=name, identity_key=f"SIM_K{i+1:03}", access_issuers=[issuer]))
        data["accounts"].append(dict(account_id=f"SIM_A{i+1:03}", owner_name=name, employee_id=employee, identity_key=f"SIM_K{i+1:03}"))
    data["employees"].append(dict(employee_id="SIM_E_WAHI", name="Ishan Wahi", identity_key="SIM_K_WAHI", access_issuers=["SIM_LISTING"]))
    data["accounts"].extend([
        dict(account_id="SIM_A_NIKHIL", owner_name="Nikhil Wahi"),
        dict(account_id="SIM_A_FRIEND", owner_name="Sameer Ramani"),
    ])
    data["relationships"].append(dict(relationship_id="SIM_R_WAHI", employee_id="SIM_E_WAHI", account_id="SIM_A_NIKHIL", relationship="declared_related_party", source_reference="Simulated onboarding declaration; public sibling relationship: SEC 2023-98. Not an actual employee declaration."))
    for i in range(10):
        data["accounts"].append(dict(account_id=f"SIM_AF{i:02}", owner_name=f"Related Person {i:02}"))
        data["relationships"].append(dict(relationship_id=f"SIM_RF{i:02}", employee_id=f"SIM_E{i+1:03}", account_id=f"SIM_AF{i:02}", relationship="spouse", source_reference=f"Synthetic declaration {i:02}"))
        name = data["employees"][i]["name"]
        data["accounts"].append(dict(account_id=f"SIM_AI{i:02}", owner_name=" ".join(part[0] for part in name.split())))
    for i in range(19):
        data["accounts"].append(dict(account_id=f"SIM_AX{i:02}", owner_name=f"Zoe Zulu {i:02}"))
    event = datetime(2022, 4, 12, 16, tzinfo=timezone.utc)
    for i in range(12):
        data["events"].append(dict(event_id=f"SIM_EV{i:02}", issuer_id="SIM_LISTING" if i < 9 else f"SIM_CORP_{i-8}", public_at=(event + timedelta(days=i*10)).isoformat(), description="Simulated listing or corporate announcement. Date and asset ID are generated, not a historical case event."))

    def trade(key, account, issuer, when, quantity=100, price=40, precleared=True):
        data["trades"].append(dict(trade_id=key, account_id=account, issuer_id=issuer, occurred_at=when.isoformat(), side="buy", quantity=quantity, price=price, currency="USD", precleared=precleared))

    # Public-name reference: synthetic account/price/time and a simulated declaration.
    for i in range(9):
        trade(f"SIM_T_WAHI_{i:02}", "SIM_A_NIKHIL", "SIM_LISTING", event+timedelta(days=i*10-1), 5000+i*500, 20+i, False)
    trade("SIM_T_UNLINKED_FRIEND", "SIM_A_FRIEND", "SIM_LISTING", event-timedelta(days=1), 8000, 50, False)
    for i in range(10):
        corp_event = event + timedelta(days=(9+i%3)*10)
        issuer = f"SIM_CORP_{i%3+1}"
        trade(f"SIM_T_DIRECT_{i:02}", f"SIM_A{i+1:03}", issuer, corp_event-timedelta(days=2), 20000+i*1000, 50, False)
        trade(f"SIM_T_FAMILY_{i:02}", f"SIM_AF{i:02}", issuer, corp_event-timedelta(days=3), 17000+i*1000, 50, False)
        trade(f"SIM_T_INITIALS_{i:02}", f"SIM_AI{i:02}", issuer, corp_event-timedelta(days=1), 100, 50, False)
    # Controls exercise missing approval, missing access, and the exact event boundary.
    trade("SIM_T_NO_PRECLEARANCE_DATA", "SIM_A011", "SIM_CORP_2", event+timedelta(days=99), 100, 40, None)
    trade("SIM_T_NO_ISSUER_ACCESS", "SIM_A012", "SIM_CORP_1", event+timedelta(days=89), 100, 40, False)
    trade("SIM_T_AT_PUBLIC_EVENT", "SIM_A013", "SIM_CORP_1", event+timedelta(days=90), 100, 40, False)
    while len(data["trades"]) < 900:
        i = len(data["trades"])
        employee_index = i % 119
        trade(f"SIM_T_NORMAL_{i:04}", f"SIM_A{employee_index+1:03}", f"SIM_CORP_{employee_index%3+1}", event+timedelta(days=180+i%90, minutes=i), rng.randint(10, 500), round(rng.uniform(20, 150), 2), True)

    def entity(key, name=None, **details):
        data["entities"].append(dict(entity_id=key, name=name or f"Simulated business {key}", registered_address=f"Synthetic premises {key}", bank_account=f"SIM_BANK_{key}", bank_routing="SIM_COMMON_ROUTING", **details))

    def payment(key, sender, receiver, when, amount, currency="USD", purpose="Simulated goods payment"):
        data["payments"].append(dict(transaction_id=key, sender_id=sender, receiver_id=receiver, amount=amount, currency=currency, occurred_at=when.isoformat(), purpose=purpose, invoice_id=f"SIM_INV_{key}"))

    for group in range(3):
        keys = [f"SIM_RISK_{group}_{i}" for i in range(3)]
        for i, key in enumerate(keys):
            name = "Good Star Limited [simulated test representation]" if group == 0 and i == 0 else f"Simulated review company {group}-{i}"
            entity(key, name)
            data["entities"][-1].update(registered_address=f"SIM shared office {group}", bank_account=f"SIM_SHARED_BANK_{group}", owner_entity_id=keys[(i+1)%3], ownership_source="Synthetic ownership assertion; not taken from public-case records")
        for i in range(3):
            payment(f"SIM_P_RISK_{group}_{i}", keys[i], keys[(i+1)%3], event+timedelta(days=group*10+i), 1000000-i*10000, purpose="Simulated uncorroborated consulting payment")
    purpose = "Documented group treasury settlement"
    for i in range(3):
        entity(f"SIM_TREASURY_{i}", legitimate_group="SIM_DOCUMENTED_GROUP", business_purpose=purpose)
        data["entities"][-1].update(registered_address="SIM Treasury office", bank_account="SIM_TREASURY_BANK")
        payment(f"SIM_P_TREASURY_{i}", f"SIM_TREASURY_{i}", f"SIM_TREASURY_{(i+1)%3}", event+timedelta(days=i), 250000, purpose=purpose)
    for i in range(6):
        entity(f"SIM_ADDRESS_ONLY_{i}")
        data["entities"][-1]["registered_address"] = "SIM Serviced office for unrelated tenants"
    # Three-node cycles split across currencies must not count as a single cycle.
    for i in range(3):
        entity(f"SIM_MIXED_{i}")
        payment(f"SIM_P_MIXED_{i}", f"SIM_MIXED_{i}", f"SIM_MIXED_{(i+1)%3}", event+timedelta(days=i), 25000, currency="USD" if i != 1 else "INR")
    for i in range(69):
        entity(f"SIM_BIZ_{i:02}")
    # Outward-only star gives a substantial ledger without unbounded dense cycles.
    while len(data["payments"]) < 1200:
        i = len(data["payments"])
        payment(f"SIM_P_NORMAL_{i:04}", "SIM_BIZ_00", f"SIM_BIZ_{i%68+1:02}", event+timedelta(days=i%180, minutes=i), round(rng.uniform(100, 85000), 2), currency="INR" if i%4 == 0 else "USD")
    return InvestigationInput.model_validate(data)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = generate()
    report = ClariFiAgent(mode="rules").run(data, approved=True)
    root = ROOT / "workspaces/sample-authoring"
    root.mkdir(parents=True, exist_ok=True)
    (root / "input.json").write_text(data.model_dump_json(indent=2))
    columns = {table: list(InvestigationInput.model_fields[table].annotation.__args__[0].model_fields) for table in TABLES}
    (root / "columns.json").write_text(json.dumps(columns))
    (OUTPUT / "baseline-report.json").write_text(json.dumps(report, indent=2))
    counts = {table: len(getattr(data, table)) for table in TABLES}
    summary = Counter((f["capability"], f["disposition"]) for f in report["findings"])
    representative = [f for f in report["findings"] if any(token in f["finding_id"] for token in ("WAHI_00", "DIRECT_00", "FAMILY_00", "INITIALS_00", "UNLINKED", "NO_PRECLEARANCE", "NO_ISSUER", "AT_PUBLIC", "shell:SIM_RISK_0_0", "shell:SIM_TREASURY_0", "shell:SIM_ADDRESS_ONLY_0", "shell:SIM_MIXED_0"))]
    lines = ["# ClariFi combined investigation sample", "", "## Upload", "", "Choose **ClariFi Agent → Combined investigation → Upload Excel workbook**. Upload `clarifi-combined-review.xlsx`, choose a model, review the source records, approve and run. Use Offline rules demo to reproduce the baseline without an AI call. Gemini/Local Gemma adds planning and narrative; the same reviewed tools compute the scores.", "", "## Data provenance", "", "All operational rows are SIMULATED. Historical names identify public-case references only. Generated prices, amounts, dates, account/employee IDs, issuer IDs, access assertions, relationship declarations, addresses, bank accounts, ownership edges and payment cycles are not historical evidence. The rows are not a reconstruction of complete case ledgers.", "", f"- Insider reference: [SEC, 30 May 2023]({SEC}). Ishan Wahi's confidential-listing access and tipping to Nikhil Wahi and Sameer Ramani motivated the related-account scenario. `SIM_R_WAHI` is an invented onboarding declaration, not an assertion that Coinbase had one. `SIM_LISTING` and all trade dates/prices are invented.", f"- Company reference: [DOJ civil forfeiture complaint, filed 1 July 2020, printed pages 17–18, paragraphs 44–48]({DOJ}). The complaint alleges diversion to Good Star Limited and describes its control. `SIM_RISK_0_0` uses its name as a case reference; every bank/address/owner/payment connection in this sample is invented. The three-company cycle is a detector exercise, not an alleged 1MDB transaction path.", "", "## Volume", "", "| Table | Rows |", "|---|---:|", *[f"| {k} | {v} |" for k,v in counts.items()], f"| **Total evidence rows** | **{sum(counts.values())}** |", "", "## Baseline results", "", "Computed by the actual reviewed tools with the offline reasoner. Scores prioritize review and are not fraud probabilities. No real-world accuracy is established by these constructed examples. This is a demonstration fixture; do not use it as a held-out accuracy benchmark.", "", "| Capability | Disposition | Findings |", "|---|---|---:|", *[f"| {cap} | {disp} | {n} |" for (cap,disp),n in sorted(summary.items())], "", "## Cases to inspect", "", "| Finding | Score | Disposition |", "|---|---:|---|", *[f"| {f['finding_id']} | {f['score']} | {f['disposition']} |" for f in representative], "", "The unlinked friend is intentionally unresolved: without a supplied identity/relationship link the tool cannot recover a tipping network from news. Initials remain identity-review leads. Missing preclearance data adds no points; an exact event-time trade is outside the strictly-before window. Mixed-currency edges do not form one cycle. Shared addresses alone do not establish wrongdoing. Documented treasury cycles receive contextual score reductions.", "", "The baseline report contains all findings. AI narrative is limited to the highest 25 findings, so it is not a narrative for every row. On the initial overview, the payment chart shows at most 100 rows per currency; use finding evidence to inspect specific cycles."]
    (OUTPUT / "README.md").write_text("\n".join(lines)+"\n")
    print(json.dumps({"rows": counts, "total": sum(counts.values()), "findings": len(report["findings"]), "dispositions": {f"{a}/{b}": n for (a,b),n in summary.items()}}, indent=2))


if __name__ == "__main__":
    main()
