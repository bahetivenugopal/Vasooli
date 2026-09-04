"""Persisted Engine 3 artefacts: chase state, the promise register, and messages.

Three tables, and the middle one is the reason this engine exists. `PromiseToPay`
is the only place in the project where a *model-derived* fact is stored as
durable state rather than only as an audit entry — so it carries its provenance
column, and a promise the reasoning layer abstained on is never written at all.

Same primary-key discipline as Engine 2's tables: keyed by `(batch_id, ...)`, not
by invoice id alone. One dataset feeds many runs, and a table that could only
hold one run's view of an invoice would silently overwrite the previous run's
evidence. `RecoverableEntityMixin` is deliberately not used, for the same reason.

None of these is a metric source. Every reported figure is recomputed from
`audit_entries`, so a stale row here can never become a number anyone quotes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, utcnow


class InvoiceChaseState(Base):
    """Where one invoice stands at the end of one run, and why.

    `explanation` is the load-bearing column, exactly as it is in Engine 2: for
    any invoice the engine must be able to state what it will do next, when, and
    which rule permits it. The score breakdown sits beside it so a ranking can be
    argued with rather than merely trusted (`policy-bounds:PR1`).
    """

    __tablename__ = "invoice_chase_states"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    # --- the invoice as the run saw it -------------------------------------
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paid_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    invoice_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    days_overdue: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Derived by the engine from `due_at`, never read from the manifest.
    ageing_bucket: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # --- prioritisation (policy-bounds:PR1) --------------------------------
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    priority_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Every component and weight that produced `priority_score`.
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    deprioritised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # --- what the engine derived from the reply ----------------------------
    reply_intent: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    reply_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    disputed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    dispute_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # --- the ladder --------------------------------------------------------
    rungs_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_rung: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    next_step: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    authorising_rule: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    attempts_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    escalation_trigger: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # --- what happened ------------------------------------------------------
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suppressed_rule: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    payment_link_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_recovered_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    customer_reliability: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_chase_batch_rank", "batch_id", "priority_rank"),
        Index("ix_chase_batch_step", "batch_id", "next_step"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<InvoiceChaseState {self.invoice_id} rank {self.priority_rank}"
            f" -> {self.next_step} by {self.authorising_rule}>"
        )


class PromiseToPay(Base):
    """One extracted commitment, and what became of it.

    The only durable model-derived state in the project, which is why it carries
    `provenance` and `model_confidence` as columns rather than only in the audit
    trail: a promise register a reader cannot tell was reasoned is a register
    nobody should act on.

    An abstention never lands here. That asymmetry is the point of
    `policy-bounds:PP2` and the Engine 3 fallback contract — fabricating a
    promise would suppress a legitimate chase and corrupt this table.
    """

    __tablename__ = "promises_to_pay"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reply_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    #: When the reply that made this promise arrived. Relative expressions in the
    #: text ("end of month") are resolved against this, never against run time.
    reply_received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    reply_text: Mapped[str] = mapped_column(Text, nullable=False)

    #: Absolute, normalized. Null when the reply gave no extractable date, which
    #: is a legitimate outcome — see `policy-bounds:PP3`.
    committed_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    committed_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: "once our client pays us" is a commitment to intent, not to a date.
    conditional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    condition_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: `active` | `kept` | `broken` | `superseded`.
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status_rule: Mapped[str] = mapped_column(String(96), nullable=False)
    status_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: The moment the promise becomes reviewable: committed date + PP1 grace, or
    #: reply time + the PP3 horizon when no date was given.
    review_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    model_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_promise_batch_status", "batch_id", "status"),
        Index("ix_promise_batch_customer", "batch_id", "customer_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PromiseToPay {self.invoice_id} {self.status} due {self.committed_date}>"


class InvoiceCommunication(Base):
    """One drafted reminder.

    **Drafted, policy-gated, logged and rendered — never dispatched.** No
    recipient column, no send path; see ADR 0009. `status` records what the
    policy engine decided, and `held_rule` names the bound that stopped it.
    """

    __tablename__ = "invoice_communications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    rung: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rung_number: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    call_to_action: Mapped[str] = mapped_column(String(48), nullable=False)
    #: The Razorpay test-mode payment link, when one was created. Null when link
    #: creation failed — the message degrades to one without a link rather than
    #: the chase aborting.
    payment_link_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_link_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: `marked_for_delivery` | `held` | `suppressed`. Never `sent`.
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    authorising_rule: Mapped[str] = mapped_column(String(96), nullable=False)
    held_rule: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tone_violations: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_invoice_comm_batch_status", "batch_id", "status"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InvoiceCommunication {self.invoice_id} {self.rung} {self.status}>"


# --- API read shapes -------------------------------------------------------


class InvoiceChaseStateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: str
    invoice_id: str
    customer_id: str
    customer_name: str
    amount_paise: int
    amount_paid_paise: int
    currency: str
    invoice_status: str
    due_at: datetime
    paid_at: datetime | None
    days_overdue: int
    ageing_bucket: str
    priority_score: float
    priority_rank: int
    score_breakdown: dict[str, Any]
    deprioritised: bool
    reply_intent: str | None
    reply_language: str | None
    disputed: bool
    dispute_detail: str | None
    abstained: bool
    rungs_used: int
    current_rung: str | None
    next_step: str
    scheduled_for: datetime | None
    authorising_rule: str
    reason_code: str
    explanation: str
    attempts_remaining: int | None
    escalated: bool
    escalation_trigger: str | None
    reminder_sent: bool
    suppressed_rule: str | None
    payment_link_id: str | None
    amount_recovered_paise: int
    customer_reliability: float | None


class PromiseToPayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: str
    invoice_id: str
    customer_id: str
    reply_id: str
    reply_received_at: datetime
    reply_text: str
    committed_date: datetime | None
    committed_amount_paise: int | None
    conditional: bool
    condition_detail: str | None
    status: str
    status_rule: str
    status_rationale: str
    review_at: datetime | None
    superseded_by: int | None
    model_confidence: float | None
    provenance: dict[str, Any]
    model_reasoning: str | None


class InvoiceCommunicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    batch_id: str
    invoice_id: str
    customer_id: str
    created_at: datetime
    rung: str
    rung_number: int
    channel: str
    subject: str
    body: str
    call_to_action: str
    payment_link_url: str | None
    payment_link_id: str | None
    status: str
    scheduled_for: datetime | None
    authorising_rule: str
    held_rule: str | None
    rationale: str
    provenance: dict[str, Any]
    tone_violations: list[Any] | None
