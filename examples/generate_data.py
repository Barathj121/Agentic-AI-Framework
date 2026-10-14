"""Reproducible synthetic datasets. No production data or model is used."""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

BANK_COLUMNS = ["transaction_id", "customer_id", "account_id", "transaction_timestamp",
                "transaction_amount", "transaction_type", "merchant_category", "destination_account", "country", "channel"]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def generate_banking(rows: int = 10000, seed: int = 42) -> tuple[list[dict], list[dict]]:
    if rows < 1000:
        raise ValueError("At least 1,000 rows are required to preserve all seeded scenarios.")
    rng = random.Random(seed)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    transactions, annotations = [], []
    for index in range(rows):
        customer = rng.randrange(1, 501)
        timestamp = start + timedelta(seconds=rng.randrange(90 * 86400))
        row = {
            "transaction_id": f"TX{index + 1:07d}",
            "customer_id": f"C{customer:04d}",
            "account_id": f"A{customer:04d}",
            "transaction_timestamp": timestamp.isoformat(),
            "transaction_amount": round(rng.lognormvariate(4.1, 0.8), 2),
            "transaction_type": rng.choices(["purchase", "transfer", "withdrawal"], [65, 25, 10])[0],
            "merchant_category": rng.choice(["groceries", "travel", "retail", "utilities", "services"]),
            "destination_account": f"A{rng.randrange(1, 701):04d}",
            "country": rng.choices(["IN", "GB", "US", "SG", "AE"], [70, 10, 10, 5, 5])[0],
            "channel": rng.choice(["mobile", "web", "branch", "atm"]),
        }
        label, explanation = "baseline", "Synthetic baseline; not a verified normal transaction."
        if index < 50:
            row["transaction_amount"] = round(rng.uniform(18000, 65000), 2)
            label, explanation = "large_amount", "Injected high transaction amount relative to the synthetic baseline."
        elif index < 150:
            cluster = (index - 50) // 20
            row.update(customer_id=f"C{501 + cluster:04d}", account_id=f"A{501 + cluster:04d}",
                       transaction_type="transfer", transaction_amount=round(rng.uniform(80, 95), 2),
                       transaction_timestamp=(start + timedelta(days=35 + cluster, minutes=index % 20 * 3)).isoformat(),
                       destination_account=f"A{650 + cluster:04d}", channel="mobile")
            label, explanation = "repeated_small_transfers", "Twenty similar small transfers per seeded account within one hour."
        elif index < 250:
            row.update(customer_id="C0506", account_id="A0506", transaction_type="transfer",
                       transaction_timestamp=(start + timedelta(days=60, seconds=(index - 150) * 12)).isoformat())
            label, explanation = "high_frequency", "One hundred transfers by one account within twenty minutes."
        elif index < 350:
            offset = index - 250
            row.update(customer_id="C0507", account_id="A0507", transaction_type="purchase",
                       transaction_timestamp=(start + timedelta(days=offset // 2)).isoformat(),
                       transaction_amount=round(rng.uniform(15, 40) if offset < 60 else rng.uniform(1800, 4200), 2))
            label = "behavior_baseline" if offset < 60 else "behavior_shift"
            explanation = "Seeded customer changes from low to high spending after thirty days."
        elif index < 450:
            row.update(customer_id=f"C{508 + (index - 350) % 25:04d}", account_id=f"A{508 + (index - 350) % 25:04d}",
                       destination_account="A0999", transaction_type="transfer",
                       transaction_timestamp=(start + timedelta(days=50, minutes=(index - 350) * 9)).isoformat())
            label, explanation = "common_destination", "Twenty-five source accounts transfer to one seeded hub account."
        transactions.append(row)
        annotations.append({"transaction_id": row["transaction_id"], "scenario": label,
                            "is_injected_pattern": label not in {"baseline", "behavior_baseline"}, "explanation": explanation})
    rng.shuffle(transactions)
    return transactions, annotations


def generate_sales(rows: int = 5000, seed: int = 43) -> list[dict]:
    rng = random.Random(seed)
    start = datetime(2025, 1, 1)
    result = []
    prices = {"Accessories": 35, "Electronics": 450, "Home": 120, "Office": 75}
    for index in range(rows):
        date = start + timedelta(days=rng.randrange(365))
        category = rng.choice(list(prices))
        region = rng.choice(["North", "South", "East", "West"])
        quantity = rng.randint(1, 5)
        unit_price = round(prices[category] * rng.uniform(0.8, 1.2), 2)
        discount = 0.15 if date.month in [10, 11, 12] else rng.choice([0.0, 0.0, 0.05])
        if category == "Electronics" and region == "South" and date.month >= 7:
            quantity += 3
        result.append({"order_id": f"ORD{index + 1:07d}", "customer_id": f"CUS{rng.randrange(1, 401):04d}",
                       "order_date": date.date().isoformat(), "region": region, "product_category": category,
                       "quantity": quantity, "unit_price": unit_price, "discount_rate": discount,
                       "revenue": round(quantity * unit_price * (1 - discount), 2),
                       "channel": rng.choice(["online", "retail", "partner"])})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000, help="Banking row count (minimum 1,000).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "data")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    transactions, annotations = generate_banking(args.rows, args.seed)
    sales = generate_sales(seed=args.seed + 1)
    write_csv(args.output / "banking_transactions.csv", BANK_COLUMNS, transactions)
    write_csv(args.output / "banking_annotations.csv", list(annotations[0]), annotations)
    write_csv(args.output / "customer_sales.csv", list(sales[0]), sales)
    metadata = {"synthetic": True, "seed": args.seed, "banking_rows": len(transactions), "sales_rows": len(sales),
                "currency": "Synthetic common currency units; amounts are comparable within each dataset.",
                "annotations": "Kept separate from features. Injected patterns do not establish financial crime or real-world model quality.",
                "banking_patterns": ["large_amount", "repeated_small_transfers", "high_frequency", "behavior_shift", "common_destination"],
                "sales_patterns": ["South-region electronics quantity rises from July", "Q4 discounts are higher"]}
    (args.output / "dataset_info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(transactions):,} synthetic transactions and {len(sales):,} synthetic sales rows in {args.output.resolve()}")


if __name__ == "__main__":
    main()
