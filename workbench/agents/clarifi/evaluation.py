"""Evaluate frozen case dispositions against separate analyst-adjudicated labels.

Do not tune a threshold using these held-out labels. Accuracy alone is misleading
for rare events; report precision/recall/F1, confusion counts and class prevalence.
"""
from __future__ import annotations

import math


def evaluate(report: dict, labels: dict[str, bool]) -> dict:
    findings = {f["finding_id"]: f for f in report["findings"]}
    if not labels or set(labels) != set(findings):
        raise ValueError("Provide exactly one held-out boolean label for every finding")
    if any(type(label) is not bool for label in labels.values()):
        raise ValueError("Labels must be actual booleans")
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for key, truth in labels.items():
        predicted = findings[key]["disposition"] == "analyst_review"
        counts["tp" if predicted and truth else "fp" if predicted else "fn" if truth else "tn"] += 1
    tp, fp, tn, fn = (counts[k] for k in ("tp", "fp", "tn", "fn"))
    size = len(labels)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    accuracy = (tp + tn) / size
    # Wilson interval for descriptive uncertainty; dependent findings from the
    # same person/company still require grouped evaluation, not this IID interval.
    z = 1.96
    center = (accuracy + z*z/(2*size)) / (1 + z*z/size)
    half = z * math.sqrt(accuracy*(1-accuracy)/size + z*z/(4*size*size)) / (1+z*z/size)
    return {**counts, "n": size, "accuracy": accuracy, "precision": precision, "recall": recall,
            "f1": f1, "positive_prevalence": (tp+fn)/size,
            "accuracy_wilson_95": [center-half, center+half],
            "scope": "Supplied held-out labels and frozen analyst_review disposition only; not proof of production accuracy"}
