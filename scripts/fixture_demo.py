"""Explicit test-fixture demonstrations with real Docker execution.

This adapter supplies fixed, reviewed model responses to test the generic workflow.
It is never used by the production Service or UI and is not evidence of LLM quality.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.schemas import BuildResult, ProposalSet, Understanding
from workbench.service import Service
from workbench.settings import Settings
from workbench.workflow import Workflow

SCENARIOS = ("sql", "ml", "graph", "sales")


def fixture_csv(scenario: str) -> bytes:
    if scenario == "sales":
        return b"order_id,order_date,region,product_category,quantity,unit_price,revenue\nO1,2025-01-10,North,Electronics,2,100,200\nO2,2025-01-11,South,Office,3,100,300\nO3,2025-02-10,North,Electronics,3,50,150\nO4,2025-02-11,South,Office,5,70,350\n"
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["transaction_id", "customer_id", "account_id", "destination_account", "transaction_timestamp", "transaction_amount", "transaction_type"])
    for customer in range(1, 11):
        for item in range(1, 21 if customer == 10 else 5):
            writer.writerow([f"TX{customer:02d}_{item:02d}", f"C{customer:02d}", f"A{customer:02d}", "A20",
                             f"2025-01-10T10:{item:02d}:00", 100000 + item if customer == 10 else customer * 10 + item, "transfer"])
    return buffer.getvalue().encode("utf-8")


def output(name: str, kind: str, location: str, columns: list[str] | None = None, rows: int | None = None) -> dict:
    return {"name": name, "type": kind, "location": location, "required_columns": columns or [], "min_rows": rows}


def scenario_contract(scenario: str) -> dict:
    shared = """\
import duckdb
from pathlib import Path
con = duckdb.connect('database/workspace.duckdb')
"""
    if scenario == "sql":
        title = "Deterministic SQL transfer activity"
        problem = "Count transfers by customer, rank customer activity and save queryable SQL results."
        columns = ["customer_id", "transaction_amount", "transaction_type"]
        dependencies = ["duckdb"]
        outputs = [output("Customer transfer activity", "database_table", "results.transfer_activity", ["customer_id", "transfer_count", "total_amount"], 10)]
        sql = """CREATE OR REPLACE TABLE results.transfer_activity AS
SELECT customer_id, count(*) AS transfer_count, sum(transaction_amount) AS total_amount
FROM raw.source WHERE transaction_type = 'transfer' GROUP BY customer_id;
"""
        code = shared + "con.execute(Path('generated/analysis.sql').read_text())\nprint(con.execute('SELECT * FROM results.transfer_activity ORDER BY transfer_count DESC').fetchall())\ncon.close()\n"
        extra = [{"path": "generated/analysis.sql", "content": sql}]
        assertion = "SELECT count(*)=10 AND sum(transfer_count)=56 AND max(transfer_count)=20 AND sum(total_amount)=2002100 FROM results.transfer_activity"
    elif scenario == "ml":
        title = "Deterministic customer anomaly scoring"
        problem = "Compute customer features, fit an unsupervised anomaly detector and retain model, scores and diagnostics."
        columns = ["customer_id", "transaction_amount"]
        dependencies = ["duckdb", "pandas", "scikit-learn", "joblib"]
        outputs = [output("Customer anomaly scores", "database_table", "results.customer_scores", ["customer_id", "anomaly_score", "is_anomaly"], 10),
                   output("Fitted preprocessing and detector", "model", "models/customer_anomaly.joblib"),
                   output("Computed model diagnostics", "json", "results/model_metrics.json")]
        code = shared + dedent("""\
import json
import joblib
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import IsolationForest
features = con.execute('SELECT customer_id, count(*) AS transfer_count, sum(transaction_amount) AS total_amount, avg(transaction_amount) AS mean_amount FROM raw.source GROUP BY customer_id ORDER BY customer_id').df()
values = features[['transfer_count', 'total_amount', 'mean_amount']]
model = Pipeline([('scale', StandardScaler()), ('detector', IsolationForest(n_estimators=50, contamination=0.1, random_state=42, n_jobs=1))])
model.fit(values)
scored = features[['customer_id']].copy()
scored['anomaly_score'] = -model.score_samples(values)
scored['is_anomaly'] = model.predict(values) == -1
con.register('computed_scores', scored)
con.execute('CREATE OR REPLACE TABLE results.customer_scores AS SELECT * FROM computed_scores')
joblib.dump(model, 'models/customer_anomaly.joblib')
metrics = {'method': 'IsolationForest', 'seed': 42, 'customers': len(scored), 'anomaly_count': int(scored['is_anomaly'].sum()), 'score_min': float(scored['anomaly_score'].min()), 'score_max': float(scored['anomaly_score'].max()), 'features': list(values.columns), 'evaluation': 'Unsupervised in-sample diagnostics only; no ground-truth accuracy is claimed.'}
Path('results/model_metrics.json').write_text(json.dumps(metrics, indent=2))
print(json.dumps(metrics))
con.close()
""")
        extra = []
        assertion = "SELECT count(*)=10 AND bool_and(isfinite(anomaly_score)) AND sum(CASE WHEN is_anomaly THEN 1 ELSE 0 END)>=1 FROM results.customer_scores"
    elif scenario == "graph":
        title = "Deterministic directed transfer graph"
        problem = "Analyze account transfer relationships and retain computed node degree and edge statistics."
        columns = ["account_id", "destination_account", "transaction_amount"]
        dependencies = ["duckdb", "pandas", "networkx"]
        outputs = [output("Account connectivity", "database_table", "results.account_connectivity", ["account_id", "in_degree", "out_degree"], 11),
                   output("Aggregated transfer edges", "database_table", "results.transfer_edges", ["source", "destination", "transfers", "total_amount"], 10)]
        code = shared + dedent("""\
import networkx as nx
import pandas as pd
edges = con.execute('SELECT account_id AS source, destination_account AS destination, count(*) AS transfers, sum(transaction_amount) AS total_amount FROM raw.source GROUP BY 1,2 ORDER BY 1,2').df()
graph = nx.DiGraph()
for row in edges.itertuples(index=False):
    graph.add_edge(row.source, row.destination, transfers=int(row.transfers), amount=float(row.total_amount))
nodes = pd.DataFrame([{'account_id': node, 'in_degree': graph.in_degree(node), 'out_degree': graph.out_degree(node), 'degree_centrality': nx.degree_centrality(graph)[node]} for node in sorted(graph.nodes)])
con.register('computed_nodes', nodes)
con.register('computed_edges', edges)
con.execute('CREATE OR REPLACE TABLE results.account_connectivity AS SELECT * FROM computed_nodes')
con.execute('CREATE OR REPLACE TABLE results.transfer_edges AS SELECT * FROM computed_edges')
print({'nodes': graph.number_of_nodes(), 'edges': graph.number_of_edges(), 'hub_in_degree': graph.in_degree('A20')})
con.close()
""")
        extra = []
        assertion = "SELECT (SELECT count(*)=11 FROM results.account_connectivity) AND (SELECT in_degree=10 AND out_degree=0 FROM results.account_connectivity WHERE account_id='A20') AND (SELECT sum(transfers)=56 FROM results.transfer_edges)"
    elif scenario == "sales":
        title = "Deterministic non-banking sales summary"
        problem = "Summarize monthly sales by region, validate computed totals and save a Plotly chart."
        columns = ["order_date", "region", "quantity", "unit_price", "revenue"]
        dependencies = ["duckdb", "pandas", "plotly"]
        outputs = [output("Monthly regional revenue", "database_table", "results.monthly_sales", ["month", "region", "revenue"], 4),
                   output("Revenue chart", "chart", "results/revenue_chart.json"),
                   output("Custom artifact example", "custom_note", "results/run.custom")]
        code = shared + dedent("""\
import plotly.express as px
con.execute("CREATE OR REPLACE TABLE results.monthly_sales AS SELECT strftime(CAST(order_date AS DATE), '%Y-%m') AS month, region, sum(revenue) AS revenue FROM raw.source GROUP BY 1,2")
data = con.execute('SELECT * FROM results.monthly_sales ORDER BY month, region').df()
figure = px.bar(data, x='month', y='revenue', color='region', barmode='group', title='Computed monthly synthetic revenue')
Path('results/revenue_chart.json').write_text(figure.to_json())
Path('results/run.custom').write_bytes(b'Custom downloadable artifact from an actual sandbox run.\n')
print(data.to_string(index=False))
con.close()
""")
        # Keep the generated Python string literal escaped, rather than embedding a newline in it.
        code = code.replace("actual sandbox run.\n')", "actual sandbox run.\\n')")
        extra = []
        assertion = "SELECT (SELECT count(*)=4 AND sum(revenue)=1000 FROM results.monthly_sales) AND (SELECT bool_and(revenue=quantity*unit_price) FROM raw.source)"
    else:
        raise ValueError(f"Unknown fixture scenario: {scenario}")
    proposal = {"proposal_id": "fixture_" + scenario, "title": title,
        "description": "Explicit deterministic test fixture; supplied responses are not generated by a real model.",
        "approach": problem, "implementation_steps": ["Compute with the reviewed fixture code inside Docker", "Validate actual outputs", "Register persistent artifacts"],
        "required_columns": columns, "expected_outputs": outputs,
        "advantages": ["Reproducible pipeline integration evidence"], "limitations": ["Does not test local model reasoning or real-world business validity", "Small synthetic dataset"],
        "assumptions": ["All rows are intentionally synthetic test inputs"], "resource_estimate": {"cpu": "low", "memory": "low", "execution_time": "short"},
        "validation_strategy": ["Check output shape and known synthetic invariants"], "feasibility": "FEASIBLE", "confidence_explanation": "Reviewed test code exercises the same generic workflow as live model builds.", "required_dependencies": dependencies}
    build = {"files": [{"path": "generated/solution.py", "content": code}, *extra], "entrypoint": "generated/solution.py",
             "required_dependencies": dependencies, "expected_outputs": outputs,
             "validation_checks": [{"name": "Known synthetic invariant", "kind": "sql_assertion", "target": outputs[0]["location"], "sql": assertion}],
             "notes": ["Explicit deterministic fixture model responses; computation and validation run in the real Docker sandbox."]}
    return {"problem": problem, "understanding": {"summary": problem, "relevant_columns": columns, "limitations": ["Synthetic fixture data"], "exploration_queries": ["SELECT count(*) AS row_count FROM raw.source"]},
            "proposal_set": {"proposals": [proposal]}, "build": build}


class FixtureModel:
    """Test-only structured-response fixture. Never a production fallback."""

    def __init__(self, contract: dict):
        self.responses = {Understanding: contract["understanding"], ProposalSet: contract["proposal_set"], BuildResult: contract["build"]}

    def health(self) -> dict:
        return {"available": True, "model_available": True, "test_fixture": True}

    def structured(self, schema_class, system: str, prompt: str):
        return schema_class.model_validate(self.responses[schema_class])


def run_scenario(settings: Settings, scenario: str, approve: bool = False) -> tuple[Path, dict]:
    contract = scenario_contract(scenario)
    settings = replace(settings, max_repair_attempts=0)
    service = Service(settings)
    workspace = service.create_workspace(f"FIXTURE DEMO · {scenario.upper()}", contract["problem"], fixture_csv(scenario), f"fixture_{scenario}.csv")
    service.manager.write(workspace, "metadata/demo_fixture.json", {"test_fixture": True, "scenario": scenario, "real_model_used": False, "execution": "Real Docker sandbox required"})
    workflow = Workflow(settings, llm=FixtureModel(contract))
    state = workflow.propose(workspace)
    if approve and state.get("status") == "AWAITING_APPROVAL":
        state = workflow.approve(workspace, "fixture_" + scenario, "Explicitly approved deterministic fixture integration demonstration.")
    return workspace, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--approve-fixture-run", action="store_true", help="Approve the reviewed fixture proposals and execute in the real Docker sandbox.")
    args = parser.parse_args()
    print("FIXTURE DEMO: model responses are fixed test data; computations run in Docker. This is not a live-LLM demonstration.")
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    settings = Settings.from_env()
    results = []
    for scenario in scenarios:
        workspace, state = run_scenario(settings, scenario, approve=args.approve_fixture_run)
        result = {"scenario": scenario, "workspace": str(workspace), "status": state.get("status"), "error": state.get("error"), "real_model_used": False}
        results.append(result)
        print(json.dumps(result, indent=2))
    expected = "COMPLETED" if args.approve_fixture_run else "AWAITING_APPROVAL"
    return 0 if all(item["status"] == expected for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
