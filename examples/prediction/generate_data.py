"""Create an entirely synthetic supervised-learning dataset, never train a model.

Feature snapshots precede simulated outcomes. Independent RNG streams generate
features, unobserved outcome noise, and Bernoulli labels for reproducibility.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

START = datetime(2025, 1, 1, tzinfo=timezone.utc)
DAYS = 400
ALERTS_PER_DAY = 20
SEED = 20261015
FEATURES = [
    "amount_log", "amount_to_median", "transfers_24h", "unique_recipients_7d",
    "cross_border_ratio_30d", "new_recipient_ratio_7d", "night_transfer_ratio_7d",
    "account_age_days", "prior_alerts_180d", "inflow_outflow_ratio_7d",
]
TARGET = "confirmed_escalation"


def sigmoid(value):
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30, 30)))


def generate_data() -> tuple[list[dict], dict]:
    """Return 8,000 unique customer alerts and candid generator metadata."""
    count = DAYS * ALERTS_PER_DAY
    feature_rng = np.random.default_rng(SEED)
    noise_rng = np.random.default_rng(SEED + 1)
    label_rng = np.random.default_rng(SEED + 2)
    activity = feature_rng.normal(0, 1, count)
    latent_risk = feature_rng.normal(0, 1, count)
    normal = lambda scale: feature_rng.normal(0, scale, count)
    features = {
        "amount_log": np.round(np.clip(7.2 + .6 * activity + .3 * latent_risk + normal(.8), 2.5, 13.5), 6),
        "amount_to_median": np.round(np.clip(np.exp(-.10 + .40 * latent_risk + .12 * activity + normal(.52)), .1, 15), 6),
        "transfers_24h": np.clip(feature_rng.poisson(np.exp(1.2 + .55 * activity + .2 * latent_risk + normal(.2))), 0, 150),
        "unique_recipients_7d": np.clip(feature_rng.poisson(np.exp(1.3 + .45 * activity + .15 * latent_risk)), 1, 80),
        "cross_border_ratio_30d": np.round(sigmoid(-2.5 + .65 * latent_risk + .2 * activity + normal(.9)), 6),
        "new_recipient_ratio_7d": np.round(sigmoid(-1.7 + .9 * latent_risk + .2 * activity + normal(.7)), 6),
        "night_transfer_ratio_7d": np.round(sigmoid(-2.0 + .55 * latent_risk + .1 * activity + normal(.7)), 6),
        "account_age_days": np.round(np.clip(np.exp(np.log(540) - .15 * latent_risk + normal(.9)), 7, 5000)).astype(int),
        "prior_alerts_180d": np.clip(feature_rng.poisson(np.exp(-1.2 + .6 * latent_risk + .1 * activity)), 0, 20),
        "inflow_outflow_ratio_7d": np.round(np.clip(np.exp(.3 * latent_risk + .5 * activity + normal(.5)), .05, 20), 6),
    }
    # These describe simulated analyst escalations, not confirmed real-world fraud.
    # No date, identifier, future value, or output label enters the feature formula.
    logit = (
        -2.35
        + .70 * np.log(features["amount_to_median"])
        + .45 * np.log1p(features["transfers_24h"] / 5)
        + .35 * np.log1p(features["unique_recipients_7d"] / 6)
        + 1.40 * (features["new_recipient_ratio_7d"] - .2)
        + .70 * (features["cross_border_ratio_30d"] - .12)
        + 1.20 * (features["night_transfer_ratio_7d"] - .15)
        - .25 * np.log1p(features["account_age_days"] / 365)
        + .30 * features["prior_alerts_180d"]
        + .80 * ((features["amount_to_median"] > 2) & (features["new_recipient_ratio_7d"] > .35))
        + .40 * np.maximum(np.log(features["inflow_outflow_ratio_7d"]), 0)
        + noise_rng.normal(0, .75, count)
    )
    probabilities = sigmoid(logit)
    labels = label_rng.binomial(1, probabilities)
    rows = []
    for index in range(count):
        offset, position = divmod(index, ALERTS_PER_DAY)
        alert_time = START + timedelta(days=offset, hours=8, minutes=35 * position)
        row = {
            "alert_id": f"AL{index + 1:07d}",
            "customer_id": f"CU{index + 1:07d}",
            "alert_time": alert_time.isoformat(),
            "label_available_at": (alert_time + timedelta(days=30)).isoformat(),
            "day_offset": offset,
        }
        row.update({name: values[index].item() for name, values in features.items()})
        row[TARGET] = int(labels[index])
        rows.append(row)
    splits = {
        "train": (0, 230), "maturity_gap_1": (230, 260),
        "validation": (260, 320), "maturity_gap_2": (320, 350), "test": (350, 400),
    }
    metadata = {
        "dataset": "synthetic_alerts.csv", "synthetic": True,
        "authorship": "Codex-assisted demonstration; no local LLM inference generated this dataset or solution.",
        "rows": count, "days": DAYS, "alerts_per_day": ALERTS_PER_DAY,
        "seed_features": SEED, "seed_outcome_noise": SEED + 1, "seed_labels": SEED + 2,
        "time_zone": "UTC", "label_maturity_days": 30,
        "outcome": "Stochastically simulated analyst decision to escalate an alert within a 30-day label window; not verified fraud.",
        "feature_columns": FEATURES, "target_column": TARGET,
        "excluded_from_features": ["alert_id", "customer_id", "alert_time", "label_available_at", "day_offset", TARGET],
        "positive_rows": int(labels.sum()), "positive_rate": float(labels.mean()),
        "observation_as_of": max(row["label_available_at"] for row in rows),
        "splits": {
            name: {
                "day_offset_inclusive": begin, "day_offset_exclusive": end,
                "first_alert_date": (START + timedelta(days=begin)).date().isoformat(),
                "last_alert_date": (START + timedelta(days=end - 1)).date().isoformat(),
                "rows": (end - begin) * ALERTS_PER_DAY,
            } for name, (begin, end) in splits.items()
        },
        "generator": {
            "features": "Independent standard-normal latent activity and latent risk create correlated synthetic past-behavior snapshots. All equations, rounding and bounds are in generate_data.py. Each customer appears exactly once.",
            "label_formula": "logit = -2.35 + 0.70*log(amount_to_median) + 0.45*log1p(transfers_24h/5) + 0.35*log1p(unique_recipients_7d/6) + 1.40*(new_recipient_ratio_7d-0.2) + 0.70*(cross_border_ratio_30d-0.12) + 1.20*(night_transfer_ratio_7d-0.15) - 0.25*log1p(account_age_days/365) + 0.30*prior_alerts_180d + 0.80*I(amount_to_median>2 and new_recipient_ratio_7d>0.35) + 0.40*max(log(inflow_outflow_ratio_7d),0) + Normal(0,0.75); y ~ Bernoulli(sigmoid(logit))",
            "hidden_fields_not_exported": ["latent_activity", "latent_risk", "unobserved_outcome_noise", "label_probability"],
            "stochastic_labels": "Outcome noise and Bernoulli sampling use distinct RNG streams; features do not deterministically identify labels.",
        },
        "feature_definitions": {
            "amount_log": "Natural log(1 + alert transaction amount in synthetic currency units).",
            "amount_to_median": "Alert amount divided by the customer's historical median amount.",
            "transfers_24h": "Count of prior transfers over 24 hours.",
            "unique_recipients_7d": "Count of unique prior recipients over 7 days.",
            "cross_border_ratio_30d": "Share of prior transfers that were cross-border over 30 days.",
            "new_recipient_ratio_7d": "Share of prior transfers to newly observed recipients over 7 days.",
            "night_transfer_ratio_7d": "Share of prior transfers during night hours over 7 days.",
            "account_age_days": "Account age at the alert time.",
            "prior_alerts_180d": "Count of alerts known before the current alert over 180 days; not future outcomes.",
            "inflow_outflow_ratio_7d": "Prior incoming amount divided by prior outgoing amount over 7 days.",
        },
        "limitations": [
            "No real customers, transactions, analyst decisions or production validation are represented.",
            "Features are synthetic snapshots; their lookback semantics are stipulated rather than reconstructed from an event ledger.",
            "Days are stationary draws. Chronological evaluation demonstrates time discipline but does not demonstrate robustness to real-world drift.",
            "All customers are unique; repeated-customer dependence and real operational feedback are absent.",
            "The known outcome formula makes this an educational benchmark; its metrics cannot establish deployment suitability or fraud detection quality.",
            "Thirty-day maturity is assumed fixed. Production label delay, censoring, privacy, fairness and analyst selection bias require separate review.",
            "Maturity-gap rows are excluded from all model fitting, selection, threshold tuning and final test evaluation.",
        ],
    }
    return rows, metadata


def main() -> None:
    folder = Path(__file__).resolve().parent
    rows, metadata = generate_data()
    csv_path = folder / "synthetic_alerts.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metadata["csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    (folder / "dataset_metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "positive_rate": metadata["positive_rate"], "dataset": str(csv_path)}, indent=2))


if __name__ == "__main__":
    main()
