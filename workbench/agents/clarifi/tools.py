"""Reviewed financial tools. Scores are transparent triage rules, not probabilities.

Shell signals adapt the local ClariFi backend/agent.py and backend/graph.py:
chronological same-currency cycles, shared records, and treasury context.
This agent does not claim the original PDF, BigQuery or invoice reconciliation adapters.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta

from .schemas import Finding, InvestigationInput, Signal


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold(), re.UNICODE))


def initials(value: str) -> str:
    return "".join(part[0] for part in normalize(value).split())


def ref(table: str, key: str) -> str:
    return f"{table}:{key}"


def anomaly_percentiles(rows, id_field, amount, group):
    """Isolation Forest on log amount, per currency/issuer; descriptive only.

    Fit and score the supplied retrospective batch. No labels, future forecast,
    classification accuracy, or fraud probability is inferred from this model.
    """
    from sklearn.ensemble import IsolationForest

    groups = defaultdict(list)
    for row in rows:
        groups[group(row)].append(row)
    result = {}
    for members in groups.values():
        if len(members) < 20:
            continue
        values = [[math.log1p(amount(row))] for row in members]
        if len({v[0] for v in values}) < 3:
            continue
        model = IsolationForest(n_estimators=100, random_state=42, n_jobs=1, contamination="auto")
        scores = -model.fit(values).score_samples(values)
        ordered = sorted(scores)
        # Midrank treats equal scores equally; higher means less typical.
        for row, score in zip(members, scores):
            percentile = 50 * (bisect_left(ordered, score) + bisect_right(ordered, score)) / len(scores)
            result[getattr(row, id_field)] = round(float(percentile), 2)
    return result


def insider_trading(data: InvestigationInput) -> list[Finding]:
    accounts = {a.account_id: a for a in data.accounts}
    relations = defaultdict(list)
    for relation in data.relationships:
        relations[(relation.account_id, relation.employee_id)].append(relation)
    events = defaultdict(list)
    for event in data.events:
        events[event.issuer_id].append(event)
    anomalies = anomaly_percentiles(data.trades, "trade_id", lambda t: t.quantity * t.price,
                                   lambda t: (t.currency, t.issuer_id))
    findings = []
    for trade in data.trades:
        account = accounts[trade.account_id]
        candidates = []
        for employee in data.employees:
            linked = relations.get((account.account_id, employee.employee_id), [])
            id_match = account.employee_id == employee.employee_id
            key_match = bool(account.identity_key and employee.identity_key
                             and account.identity_key == employee.identity_key)
            full_name = bool(normalize(account.owner_name) and normalize(account.owner_name) == normalize(employee.name))
            # Initials are deliberately weak, including abbreviated owners such as A R.
            abbreviated = normalize(account.owner_name).replace(" ", "")
            initial_match = bool(initials(employee.name) and
                                 (initials(account.owner_name) == initials(employee.name)
                                  or abbreviated == initials(employee.name)))
            if not any((linked, id_match, key_match, full_name, initial_match)):
                continue
            evidence = [ref("trades", trade.trade_id), ref("accounts", account.account_id),
                        ref("employees", employee.employee_id)]
            strong = bool(linked or id_match or key_match)
            conflict = bool((account.employee_id and account.employee_id != employee.employee_id and not linked)
                            or (account.identity_key and employee.identity_key
                                and account.identity_key != employee.identity_key and not linked))
            if conflict:
                strong = False
            identity_reason = ("declared_relationship" if linked else "employee_id" if id_match else
                               "identity_key" if key_match else "full_name_only" if full_name else "initials_only")
            evidence += [ref("relationships", r.relationship_id) for r in linked]
            if conflict:
                identity_reason = "conflicting_identity_records"
            signals = [Signal(name=identity_reason, points=40 if strong else 20 if full_name else 5,
                              evidence_ids=evidence.copy())]
            gaps = ["Identity and access are supplied records, not independently verified.",
                    "Pre-event trading does not establish knowledge, intent or possession of inside information."]
            if not strong:
                gaps.append("Name/initial match is ambiguous; corroborate identity before escalation.")
            if conflict:
                gaps.append("Supplied identity records conflict; resolve them before attributing this account.")
            before = [e for e in events[trade.issuer_id]
                      if timedelta(0) < e.public_at - trade.occurred_at <= timedelta(days=14)]
            if before:
                ids = [ref("events", e.event_id) for e in before]
                evidence += ids
                signals.append(Signal(name="within_14_days_before_public_event", points=25, evidence_ids=ids + [evidence[0]]))
            else:
                gaps.append("No supplied public event within 14 days after this trade.")
            access = trade.issuer_id in employee.access_issuers
            if access:
                signals.append(Signal(name="supplied_issuer_access", points=20,
                                      evidence_ids=[ref("employees", employee.employee_id)]))
            else:
                gaps.append("No supplied issuer-specific employee access record.")
            if trade.precleared is False:
                signals.append(Signal(name="no_preclearance_recorded", points=10, evidence_ids=[evidence[0]]))
                gaps.append("Preclearance is a supplied boolean; missing approval is not proof of a violation.")
            elif trade.precleared is None:
                gaps.append("No preclearance data supplied; no preclearance points were added.")
            percentile = anomalies.get(trade.trade_id)
            if percentile is not None and percentile >= 95:
                signals.append(Signal(name="unusual_notional_in_supplied_batch", points=5, evidence_ids=[evidence[0]]))
            score = min(100, sum(s.points for s in signals))
            disposition = ("analyst_review" if strong and access and before and score >= 65 else
                           "identity_review" if not strong else "insufficient_evidence")
            candidates.append(Finding(
                finding_id=f"insider:{trade.trade_id}:{employee.employee_id}", capability="insider_trading",
                subject_id=trade.account_id, score=score, disposition=disposition,
                signals=signals, evidence_ids=sorted(set(evidence)), gaps=gaps,
                facts={"trade_id": trade.trade_id, "employee_id": employee.employee_id,
                       "issuer_id": trade.issuer_id, "identity_basis": identity_reason,
                       "event_ids": [e.event_id for e in before], "currency": trade.currency,
                       "notional": trade.quantity * trade.price, "side": trade.side},
                anomaly_percentile=percentile))
        if not candidates:
            candidates.append(Finding(
                finding_id=f"insider:{trade.trade_id}:unresolved", capability="insider_trading",
                subject_id=trade.account_id, score=0, disposition="insufficient_evidence", signals=[],
                evidence_ids=[ref("trades", trade.trade_id), ref("accounts", account.account_id)],
                gaps=["No identity or declared relationship matched the supplied employees. This does not establish absence of risk."],
                facts={"trade_id": trade.trade_id, "issuer_id": trade.issuer_id},
                anomaly_percentile=anomalies.get(trade.trade_id)))
        findings.extend(candidates)
        if len(findings) > 2000:
            raise ValueError("Too many identity candidates; narrow the investigation scope")
    return findings


def payment_cycles(data: InvestigationInput):
    """Bounded, chronological cycles of 3–4 distinct counterparties within 30 days."""
    outgoing = defaultdict(list)
    for payment in data.payments:
        outgoing[payment.sender_id].append(payment)
    for values in outgoing.values():
        values.sort(key=lambda p: (p.occurred_at, p.transaction_id))
    cycles = []
    visits = 0

    def walk(origin, node, nodes, path):
        nonlocal visits
        if len(path) >= 4:
            return
        for payment in outgoing[node]:
            visits += 1
            if visits > 100_000:
                raise ValueError("Payment graph exploration limit exceeded; supply a smaller investigation scope")
            if path and (payment.currency != path[0].currency or payment.occurred_at <= path[-1].occurred_at
                         or payment.occurred_at - path[0].occurred_at > timedelta(days=30)):
                continue
            extended = path + [payment]
            if payment.receiver_id == origin and len(extended) >= 3:
                cycles.append((nodes, extended))
                if len(cycles) > 1000:
                    raise ValueError("Too many payment cycles; narrow the investigation scope")
            elif payment.receiver_id not in nodes:
                walk(origin, payment.receiver_id, nodes + [payment.receiver_id], extended)

    for entity in data.entities:
        walk(entity.entity_id, entity.entity_id, [entity.entity_id], [])
    return cycles


def shell_companies(data: InvestigationInput) -> list[Finding]:
    cycles = payment_cycles(data)
    by_id = {e.entity_id: e for e in data.entities}
    clusters = defaultdict(list)
    for entity in data.entities:
        for field in ("registered_address", "bank_account", "bank_routing", "owner_entity_id"):
            # Accounts/owners are identifiers: preserve punctuation and compare exactly.
            value = getattr(entity, field)
            value = normalize(value) if field == "registered_address" else value
            if value:
                clusters[(field, value)].append(entity.entity_id)
    anomalies = anomaly_percentiles(data.payments, "transaction_id", lambda p: p.amount, lambda p: p.currency)
    findings = []
    for entity in data.entities:
        evidence = [ref("entities", entity.entity_id)]
        signals = [Signal(name="baseline", points=5, evidence_ids=evidence.copy())]
        relevant = [(nodes, path) for nodes, path in cycles if entity.entity_id in nodes]
        explained = []
        for nodes, path in relevant:
            members = [by_id[key] for key in nodes]
            groups = {e.legitimate_group for e in members}
            explained.append(len(groups) == 1 and bool(next(iter(groups)))
                             and all(e.business_purpose == "Documented group treasury settlement" for e in members)
                             and all(p.purpose == "Documented group treasury settlement" for p in path))
        if relevant:
            ids = sorted({ref("payments", p.transaction_id) for _, path in relevant for p in path})
            evidence += ids
            signals.append(Signal(name="chronological_payment_cycle", points=45, evidence_ids=ids))
        overlaps = []
        for (field, _), members in clusters.items():
            if len(members) < 2 or entity.entity_id not in members:
                continue
            ids = [ref("entities", key) for key in members]
            evidence += ids
            points = {"bank_account": 20, "registered_address": 10, "bank_routing": 0,
                      "owner_entity_id": 0}[field]
            overlaps.append({"field": field, "entity_ids": sorted(members)})
            signals.append(Signal(name=f"shared_{field}", points=points, evidence_ids=ids))
        owner_links = [e for e in data.entities if e.owner_entity_id and e.ownership_source and
                       any(e.entity_id in nodes and e.owner_entity_id in nodes for nodes, _ in relevant)]
        if owner_links:
            ids = sorted({ref("entities", key) for e in owner_links for key in (e.entity_id, e.owner_entity_id)})
            evidence += ids
            signals.append(Signal(name="cycle_includes_asserted_owner", points=10, evidence_ids=ids))
        if explained and all(explained):
            ids = sorted({ref("entities", key) for nodes, _ in relevant for key in nodes}
                         | {ref("payments", p.transaction_id) for _, path in relevant for p in path})
            signals.append(Signal(name="documented_treasury_context", points=-65, evidence_ids=ids))
            evidence += ids
        score = max(0, min(100, sum(s.points for s in signals)))
        related_payments = [p for p in data.payments if entity.entity_id in (p.sender_id, p.receiver_id)]
        percentiles = [anomalies[p.transaction_id] for p in related_payments if p.transaction_id in anomalies]
        findings.append(Finding(
            finding_id=f"shell:{entity.entity_id}", capability="shell_companies", subject_id=entity.entity_id,
            score=score, disposition="analyst_review" if score >= 65 else "routine_review",
            signals=signals, evidence_ids=sorted(set(evidence)),
            gaps=["Shared records and payment cycles do not establish a shell company or illicit activity.",
                  "Beneficial ownership and commercial purpose require independent corroboration.",
                  "No invoice or delivery evidence was analyzed; payment cycles are not proof of circular invoicing."]
                 + (["Treasury context is supplied and requires analyst corroboration."] if explained and all(explained) else []),
            facts={"cycles": [{"entity_ids": nodes, "transaction_ids": [p.transaction_id for p in path],
                               "currency": path[0].currency, "first_at": path[0].occurred_at.isoformat(),
                               "last_at": path[-1].occurred_at.isoformat()} for nodes, path in relevant],
                   "shared_records": overlaps},
            anomaly_percentile=max(percentiles) if percentiles else None))
    return findings


TOOLS = {"insider_trading": insider_trading, "shell_companies": shell_companies}
