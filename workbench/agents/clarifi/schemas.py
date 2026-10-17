from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
Text = Annotated[str, Field(max_length=300)]
Tool = Literal["insider_trading", "shell_companies"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Employee(Record):
    employee_id: Identifier
    name: Text
    identity_key: Text = ""
    access_issuers: list[Identifier] = Field(default_factory=list, max_length=100)


class Account(Record):
    account_id: Identifier
    owner_name: Text
    employee_id: Identifier | None = None
    identity_key: Text = ""


class Relationship(Record):
    relationship_id: Identifier
    employee_id: Identifier
    account_id: Identifier
    relationship: Literal["spouse", "dependent", "declared_related_party"]
    source_reference: Annotated[str, Field(min_length=1, max_length=300)]


class Trade(Record):
    trade_id: Identifier
    account_id: Identifier
    issuer_id: Identifier
    occurred_at: AwareDatetime
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, le=1e15, allow_inf_nan=False)
    price: float = Field(gt=0, le=1e15, allow_inf_nan=False)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    precleared: bool | None = None


class MarketEvent(Record):
    event_id: Identifier
    issuer_id: Identifier
    public_at: AwareDatetime
    description: Text = ""


class Entity(Record):
    entity_id: Identifier
    name: Text
    registered_address: Text = ""
    bank_account: Text = ""
    bank_routing: Text = ""
    owner_entity_id: Identifier | None = None
    ownership_source: Text = ""
    legitimate_group: Text = ""
    business_purpose: Text = ""


class Payment(Record):
    transaction_id: Identifier
    sender_id: Identifier
    receiver_id: Identifier
    amount: float = Field(gt=0, le=1e15, allow_inf_nan=False)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    occurred_at: AwareDatetime
    purpose: Text = ""
    invoice_id: Text = ""


class InvestigationInput(Record):
    case_id: Identifier
    employees: list[Employee] = Field(default_factory=list, max_length=300)
    accounts: list[Account] = Field(default_factory=list, max_length=1000)
    relationships: list[Relationship] = Field(default_factory=list, max_length=1000)
    trades: list[Trade] = Field(default_factory=list, max_length=5000)
    events: list[MarketEvent] = Field(default_factory=list, max_length=300)
    entities: list[Entity] = Field(default_factory=list, max_length=200)
    payments: list[Payment] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def valid_references(self):
        for table, key in (("employees", "employee_id"), ("accounts", "account_id"),
                           ("relationships", "relationship_id"), ("trades", "trade_id"),
                           ("events", "event_id"), ("entities", "entity_id"),
                           ("payments", "transaction_id")):
            ids = [getattr(row, key) for row in getattr(self, table)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate identifiers in {table}")
        employees = {e.employee_id for e in self.employees}
        accounts = {a.account_id for a in self.accounts}
        entities = {e.entity_id for e in self.entities}
        if not self.tools:
            raise ValueError("Supply employees/accounts/trades, entities/payments, or both")
        if self.employees or self.accounts or self.trades or self.relationships or self.events:
            if not (self.employees and self.accounts and self.trades):
                raise ValueError("Insider investigation requires employees, accounts and trades")
        if self.entities or self.payments:
            if not (self.entities and self.payments):
                raise ValueError("Shell investigation requires entities and payments")
        for account in self.accounts:
            if account.employee_id and account.employee_id not in employees:
                raise ValueError("Account references an unknown employee")
        for relation in self.relationships:
            if relation.employee_id not in employees or relation.account_id not in accounts:
                raise ValueError("Relationship references an unknown employee/account")
        if any(t.account_id not in accounts for t in self.trades):
            raise ValueError("Trade references an unknown account")
        if any(p.sender_id not in entities or p.receiver_id not in entities for p in self.payments):
            raise ValueError("Payment references an unknown entity")
        if any(e.owner_entity_id and e.owner_entity_id not in entities for e in self.entities):
            raise ValueError("Ownership references an unknown entity")
        return self

    @property
    def tools(self) -> list[Tool]:
        return (["insider_trading"] if self.trades else []) + (["shell_companies"] if self.payments else [])


class Plan(Record):
    tools: list[Tool] = Field(min_length=1, max_length=2)
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]


class Signal(Record):
    name: str
    points: int
    evidence_ids: list[str]


class Finding(Record):
    finding_id: str
    capability: Tool
    subject_id: str
    score: int = Field(ge=0, le=100)
    disposition: Literal["analyst_review", "identity_review", "insufficient_evidence", "routine_review"]
    signals: list[Signal]
    evidence_ids: list[str]
    gaps: list[str]
    facts: dict
    anomaly_percentile: float | None = Field(default=None, ge=0, le=100)


class DraftClaim(Record):
    finding_id: str
    explanation: Annotated[str, Field(min_length=1, max_length=1500)]
    evidence_ids: list[str] = Field(min_length=1, max_length=30)


class Narrative(Record):
    claims: list[DraftClaim] = Field(max_length=100)
    next_steps: list[Annotated[str, Field(max_length=500)]] = Field(max_length=10)
