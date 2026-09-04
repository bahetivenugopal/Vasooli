"""Bounded recovery actions for Engine 3, every one authorised before it happens.

Four things this engine can do, and no others: **read a reply**, **record a
promise**, **draft a reminder**, **escalate**. Each goes through
`policy_engine.py` first, and each produces an audit entry naming the rule that
authorised — or refused — it.

Three properties worth reading closely, all inherited from the two engines before
this one:

- **The refusal is a first-class action.** A reminder a cap suppressed and an
  invoice a dispute froze are recorded as deliberately as a message that went
  out. Counting "harassment avoided" is only honest if the suppressions exist in
  the trail with a citation.
- **Money is booked once per invoice.** Engine 1 learned this the expensive way
  and Engine 2 made it checkable; `_booked` is the same guard, and a test asserts
  every invoice carries its outstanding amount on exactly one entry.
- **Nothing is dispatched.** Reminders are drafted, policy-gated, logged and
  rendered. There is no recipient field and no send path — see ADR 0009.

One thing is new here, and it is the reason `PromiseToPay` exists as a table: a
model-derived fact becomes durable state. It is written only when the reasoning
layer produced a coherent reading, it carries its provenance in a column, and an
abstention never reaches it at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.engines.receivables.config import ReceivablesConfig
from app.engines.receivables.reminders import (
    REMINDER_RULE,
    render,
    validate_tone,
)
from app.engines.receivables.schemas import (
    ChasePlan,
    ChaseStep,
    ExtractionResult,
    Invoice,
    PriorityScore,
    PromiseAssessment,
    PromiseStatus,
    ReminderDraft,
)
from app.engines.receivables.understanding import (
    ABSTENTION_RULE,
    EXTRACTION_RULE,
)
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.provenance import Provenance
from app.models.receivables import (
    InvoiceChaseState,
    InvoiceCommunication,
    PromiseToPay,
)
from app.services.audit_trail import AuditTrail
from app.services.policy_engine import PolicyEngine
from app.services.razorpay_client import RazorpayClient

#: What a drafted message may be marked as. Never `sent`.
STATUS_MARKED = "marked_for_delivery"
STATUS_HELD = "held"
STATUS_SUPPRESSED = "suppressed"

#: The citation a payment-link creation carries. Not a policy decision — the
#: record that a call happened, and in which mode.
PAYMENT_LINK_RULE = "razorpay-api:call.logged"


@dataclass
class InvoiceOutcome:
    """Everything one invoice produced in one run."""

    invoice: Invoice
    plan: ChasePlan
    score: PriorityScore
    extraction: ExtractionResult | None = None
    promise: PromiseAssessment | None = None
    decision_entry_id: int | None = None
    communication: InvoiceCommunication | None = None
    reminder_sent: bool = False
    recovered_paise: int = 0
    payment_link_id: str | None = None
    payment_link_failed: bool = False
    tone_violations: list[dict[str, str]] | None = None


class ReceivablesService:
    """Executes Engine 3's bounded actions and records every one of them."""

    def __init__(
        self,
        *,
        db: Session,
        trail: AuditTrail,
        policy: PolicyEngine,
        razorpay: RazorpayClient,
        config: ReceivablesConfig,
        batch_id: str,
        at_risk_paise: dict[str, int],
    ) -> None:
        self._db = db
        self._trail = trail
        self._policy = policy
        self._razorpay = razorpay
        self._config = config
        self._batch_id = batch_id
        #: What each invoice puts at risk, decided once by the runner. An invoice
        #: absent from this map contributes nothing to the denominator — see
        #: `_book_at_risk`.
        self._at_risk = at_risk_paise
        #: Invoices whose amount has already been booked. Money lives on exactly
        #: one entry per invoice; see the module docstring.
        self._booked: set[str] = set()

    # --- reading a reply ---------------------------------------------------

    def record_extraction(
        self, invoice: Invoice, extraction: ExtractionResult
    ) -> int:
        """Write what the reasoning layer made of one reply.

        An abstention is recorded as `escalate` / `escalated` with
        `provenance.abstained = true`, per `audit-schema`: it is a *successful*
        outcome for a task where guessing would be worse, and the trail should
        show it as deliberate rather than as a failure.
        """
        abstained = extraction.abstained
        understanding = extraction.understanding
        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.RECEIVABLES,
            entity_type=EntityType.INVOICE,
            entity_id=invoice.invoice_id,
            action=Action.ESCALATE if abstained else Action.CLASSIFY_DECLINE,
            outcome=Outcome.ESCALATED if abstained else Outcome.SUCCESS,
            reason_code=("REPLY_UNREADABLE" if abstained else extraction.intent.value.upper()),
            authorising_rule=ABSTENTION_RULE if abstained else EXTRACTION_RULE,
            rationale=(
                f"Reply {extraction.reply_id} routed to human review: "
                f"{extraction.abstention_reason}"
                if abstained
                else (
                    f"Reply {extraction.reply_id} read as "
                    f"{extraction.intent.value}. "
                    + (understanding.reasoning if understanding else "")
                )
            ),
            provenance=extraction.provenance,
            model_confidence=extraction.confidence,
            # Deliberately zero. An invoice's money is booked on the entry that
            # can also carry what came back — the promise entry where there is
            # one, the chase decision otherwise. Booking it here instead would
            # split the at-risk amount and the recovered amount across two
            # entries with different provenance, and the per-source recovery
            # rate would come out above 100% for the ruled bucket.
            amount_at_risk_paise=0,
            currency=invoice.currency,
            timestamp=extraction.received_at,
            metadata={
                "kind": "reply_understanding",
                "reply_id": extraction.reply_id,
                "intent": extraction.intent.value,
                "abstained": abstained,
                "degraded": extraction.degraded,
                "degradation_reason": extraction.degradation_reason,
                "language": understanding.language if understanding else None,
                "promise_detected": bool(understanding and understanding.promise_detected),
                "conditional": bool(understanding and understanding.conditional),
                "committed_date": understanding.committed_date if understanding else None,
                "dispute_detail": understanding.dispute_detail if understanding else None,
                "recommended_action": (
                    understanding.recommended_action.value if understanding else None
                ),
                # The reply text itself, so a timeline reads as a conversation
                # rather than as a list of verdicts about one.
                "reply_text": extraction.text,
            },
        )
        return int(entry.id)

    # --- the promise register ---------------------------------------------

    def record_promise(
        self,
        invoice: Invoice,
        assessment: PromiseAssessment,
        *,
        extraction: ExtractionResult,
    ) -> PromiseToPay:
        """Persist one commitment and write the entry that says what became of it.

        The audit entry carries the money **only** when the promise was kept and
        the invoice settled: that is the one moment in this engine where revenue
        actually came back, and booking it anywhere else would put recovered
        rupees on a decision that recovered nothing.
        """
        recovered = (
            invoice.amount_paid_paise
            if assessment.status is PromiseStatus.KEPT and invoice.is_paid
            else 0
        )
        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.RECEIVABLES,
            entity_type=EntityType.INVOICE,
            entity_id=invoice.invoice_id,
            action=Action.RECORD_PROMISE_TO_PAY,
            outcome={
                PromiseStatus.KEPT: Outcome.SUCCESS,
                PromiseStatus.BROKEN: Outcome.FAILURE,
                PromiseStatus.ACTIVE: Outcome.SCHEDULED,
                PromiseStatus.SUPERSEDED: Outcome.SKIPPED,
            }[assessment.status],
            reason_code=f"PROMISE_{assessment.status.value.upper()}",
            authorising_rule=assessment.rule_id,
            rationale=assessment.rationale,
            # The *status* was ruled — PP1/PP2/PP3 against dates and a payment
            # record — but the commitment it is a status about was read by a
            # model, and without that reading there would be no promise to have
            # a status. So the entry is model-sourced, which is also what puts
            # this invoice's money in the reasoned bucket of the per-source
            # split rather than the ruled one. The arithmetic is spelled out in
            # the rationale, where a reader can check it.
            provenance=extraction.provenance,
            model_confidence=assessment.confidence,
            amount_at_risk_paise=self._book_at_risk(invoice),
            amount_recovered_paise=recovered,
            currency=invoice.currency,
            timestamp=assessment.received_at,
            metadata={
                "kind": "promise_to_pay",
                "reply_id": assessment.reply_id,
                "status": assessment.status.value,
                "conditional": assessment.conditional,
                "committed_date": (
                    assessment.committed_date.isoformat() if assessment.committed_date else None
                ),
                "committed_amount_paise": assessment.committed_amount_paise,
                "review_at": assessment.review_at.isoformat() if assessment.review_at else None,
                # The *status* was ruled by PP1/PP2/PP3 against dates and a
                # payment record — deterministic. The commitment it is a status
                # about was read by a model. Both facts belong on the entry, and
                # the provenance field can only carry one, so the extraction's
                # confidence goes here rather than being quietly dropped.
                "extraction_confidence": assessment.confidence,
            },
        )
        row = PromiseToPay(
            batch_id=self._batch_id,
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer_id,
            reply_id=assessment.reply_id,
            reply_received_at=assessment.received_at,
            reply_text=_reply_text(invoice, assessment.reply_id),
            committed_date=assessment.committed_date,
            committed_amount_paise=assessment.committed_amount_paise,
            conditional=assessment.conditional,
            condition_detail=assessment.condition_detail,
            status=assessment.status.value,
            status_rule=assessment.rule_id,
            status_rationale=f"[audit entry {entry.id}] {assessment.rationale}",
            review_at=assessment.review_at,
            model_confidence=assessment.confidence,
            # The commitment was *read* by a model; the status was *ruled* by
            # PP1/PP2/PP3 against dates and a payment record. The column records
            # the reading, because that is the fact a reader of the register
            # needs to know was reasoned — the dates are arithmetic, and the
            # arithmetic is spelled out in `status_rationale`.
            provenance=extraction.provenance.model_dump(mode="json"),
            model_reasoning=assessment.reasoning,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    # --- the chase ---------------------------------------------------------

    def record_plan(self, invoice: Invoice, plan: ChasePlan) -> int:
        """Write what the gate said about the proposed chase step.

        Written first, before anything is executed, because an action without a
        record is unprovable.
        """
        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.RECEIVABLES,
            entity_type=EntityType.INVOICE,
            entity_id=invoice.invoice_id,
            action=_ACTION_BY_STEP[plan.step],
            outcome=_outcome_for(plan),
            reason_code=plan.reason_code,
            authorising_rule=plan.rule_id,
            rationale=plan.explanation,
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=self._book_at_risk(invoice),
            currency=invoice.currency,
            attempt_number=invoice.rungs_used + 1,
            attempts_remaining=plan.attempts_remaining,
            timestamp=plan.scheduled_for,
            metadata={
                "kind": "chase_decision",
                "step": plan.step.value,
                "permitted": plan.allowed,
                "rung": plan.rung,
                "rung_number": plan.rung_number,
                "rungs_used": invoice.rungs_used,
                "escalation_trigger": (
                    plan.escalation_trigger.value if plan.escalation_trigger else None
                ),
                "suppressed": plan.suppressed,
                "suppressed_rule": plan.suppressed_rule,
                "overridden_recommendation": plan.overridden_recommendation,
                "authority_rule": plan.authority_rule,
            },
        )
        return int(entry.id)

    def draft_reminder(
        self, invoice: Invoice, plan: ChasePlan, *, now: datetime
    ) -> tuple[InvoiceCommunication, ReminderDraft, list[dict[str, str]], bool]:
        """Render, tone-check, link and persist one reminder.

        Returns `(row, draft, tone_violations, link_failed)`. The gate has already
        spoken by the time this runs — `plan.step` is `send_reminder` — so what
        happens here is the message itself, not the permission for it.
        """
        rung = next(r for r in self._config.ladder.rungs if r.name == plan.rung)
        link_url, link_id, link_failed = self._payment_link(
            invoice, at=plan.scheduled_for
        )
        draft = render(
            rung=rung,
            invoice=invoice,
            now=now,
            config=self._config,
            payment_link_url=link_url,
            payment_link_id=link_id,
        )
        violations = validate_tone(draft)

        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.RECEIVABLES,
            entity_type=EntityType.INVOICE,
            entity_id=invoice.invoice_id,
            action=Action.SEND_REMINDER,
            outcome=Outcome.BLOCKED if violations else Outcome.SCHEDULED,
            reason_code="TONE_REJECTED" if violations else "REMINDER_DRAFTED",
            authorising_rule="policy-bounds:TN3" if violations else REMINDER_RULE,
            rationale=(
                "Reminder discarded on tone: " + "; ".join(v["detail"] for v in violations)
                if violations
                else (
                    f"Rung {draft.rung_number} ({draft.rung}) reminder drafted for "
                    f"{invoice.invoice_id}, "
                    + (
                        f"carrying payment link {link_id}."
                        if link_url
                        else "without a payment link — link creation failed, so the "
                        "message degrades rather than the chase aborting."
                    )
                )
            ),
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=self._book_at_risk(invoice),
            currency=invoice.currency,
            timestamp=plan.scheduled_for,
            metadata={
                "kind": "reminder",
                "rung": draft.rung,
                "rung_number": draft.rung_number,
                "channel": draft.channel.value,
                "call_to_action": draft.call_to_action.value,
                "payment_link_id": link_id,
                "payment_link_failed": link_failed,
                "tone_violations": violations or None,
                "simulated_delivery": True,
                "status": STATUS_SUPPRESSED if violations else STATUS_MARKED,
            },
        )

        row = InvoiceCommunication(
            batch_id=self._batch_id,
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer_id,
            rung=draft.rung,
            rung_number=draft.rung_number,
            channel=draft.channel.value,
            subject=draft.subject,
            body=draft.body,
            call_to_action=draft.call_to_action.value,
            payment_link_url=link_url,
            payment_link_id=link_id,
            status=STATUS_SUPPRESSED if violations else STATUS_MARKED,
            scheduled_for=plan.scheduled_for,
            authorising_rule="policy-bounds:TN3" if violations else REMINDER_RULE,
            held_rule="policy-bounds:TN1" if violations else None,
            rationale=f"[audit entry {entry.id}] {plan.explanation}",
            provenance=Provenance.deterministic().model_dump(mode="json"),
            tone_violations=violations or None,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row, draft, violations, link_failed

    def record_held_reminder(
        self, invoice: Invoice, plan: ChasePlan
    ) -> InvoiceCommunication:
        """Persist the message that was *not* sent, and the rule that stopped it.

        `policy-bounds:QH1` is explicit that a message inside quiet hours is
        blocked and rescheduled, never dropped — so a held message carries the
        moment it becomes permissible, and "held" never quietly means "lost".
        Suppressions by a cap or a freeze are held with no reopening time, which
        is the honest difference: the timing was not the problem.
        """
        rung = self._config.ladder.rung_for(invoice.rungs_used)
        row = InvoiceCommunication(
            batch_id=self._batch_id,
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer_id,
            rung=rung.name if rung else "none_remaining",
            rung_number=rung.number if rung else len(self._config.ladder.rungs),
            channel=(rung.channel.value if rung else "email"),
            subject=f"[held] Invoice {invoice.invoice_id} reminder",
            body=(
                "This reminder was drafted and then withheld. "
                f"{plan.explanation} Nothing was sent to the customer."
            ),
            call_to_action=(
                rung.call_to_action.value if rung else "contact_accounts"
            ),
            payment_link_url=None,
            payment_link_id=None,
            status=STATUS_HELD,
            scheduled_for=plan.scheduled_for,
            authorising_rule=plan.rule_id,
            held_rule=plan.rule_id,
            rationale=plan.explanation,
            provenance=Provenance.deterministic().model_dump(mode="json"),
            tone_violations=None,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    # --- the payment link ---------------------------------------------------

    def _payment_link(
        self, invoice: Invoice, *, at: datetime | None
    ) -> tuple[str | None, str | None, bool]:
        """Create a Razorpay test-mode payment link for one invoice.

        A failure degrades the message rather than aborting the chase: an invoice
        the customer cannot pay by link is still an invoice worth a reminder, and
        letting a link outage stop the whole recovery path would be the tail
        wagging the dog. The call is audited either way, so a run's link failures
        are visible rather than inferred from missing URLs.
        """
        result = self._razorpay.create_payment_link(
            amount_paise=invoice.outstanding_paise,
            description=(
                f"Invoice {invoice.invoice_id} — {invoice.customer_name} — "
                f"Rs {invoice.outstanding_paise / 100:,.2f}"
            ),
            notes={
                "batch_id": self._batch_id,
                "invoice_id": invoice.invoice_id,
                "customer_id": invoice.customer_id,
                "engine": Engine.RECEIVABLES.value,
                "expires_in_days": self._config.payment_link_expiry.days,
            },
            currency=invoice.currency,
        )
        self._razorpay.record_call(
            result,
            batch_id=self._batch_id,
            engine=Engine.RECEIVABLES,
            entity_type=EntityType.INVOICE,
            entity_id=invoice.invoice_id,
            # The run clock, not wall-clock time. Without it the link creation
            # is stamped months after the decision that authorised it, and the
            # invoice's timeline stops being ordered.
            timestamp=at,
        )
        if not result.ok or not result.data:
            return None, None, True
        link_id = str(result.data.get("id"))
        return f"https://rzp.io/i/{link_id}", link_id, False

    # --- persistence --------------------------------------------------------

    def persist_state(self, outcome: InvoiceOutcome, *, reliability: float | None) -> InvoiceChaseState:
        """Snapshot where this invoice stands, for the API and the dashboard.

        Not a metric source. Every reported figure is recomputed from the audit
        trail, so a stale row here can never become a number anyone quotes.
        """
        invoice = outcome.invoice
        plan = outcome.plan
        understanding = outcome.extraction.understanding if outcome.extraction else None
        row = InvoiceChaseState(
            batch_id=self._batch_id,
            invoice_id=invoice.invoice_id,
            customer_id=invoice.customer_id,
            customer_name=invoice.customer_name,
            amount_paise=invoice.amount_paise,
            amount_paid_paise=invoice.amount_paid_paise,
            currency=invoice.currency,
            invoice_status=invoice.status,
            due_at=invoice.due_at,
            paid_at=invoice.paid_at,
            days_overdue=invoice.days_overdue,
            ageing_bucket=self._config.ageing_bucket(
                invoice.days_late(plan.scheduled_for or invoice.due_at)
            ),
            priority_score=outcome.score.score,
            priority_rank=outcome.score.rank,
            score_breakdown={
                "components": [c.model_dump() for c in outcome.score.components],
                "deprioritised_rule": outcome.score.deprioritised_rule,
                "deprioritised_reason": outcome.score.deprioritised_reason,
                "weights": self._config.prioritization.weights,
                "rule": self._config.prioritization.rule,
            },
            deprioritised=outcome.score.deprioritised,
            reply_intent=(outcome.extraction.intent.value if outcome.extraction else None),
            reply_language=(understanding.language if understanding else None),
            disputed=bool(outcome.extraction and outcome.extraction.is_dispute),
            dispute_detail=(understanding.dispute_detail if understanding else None),
            abstained=bool(outcome.extraction and outcome.extraction.abstained),
            rungs_used=invoice.rungs_used,
            current_rung=plan.rung,
            next_step=plan.step.value,
            scheduled_for=plan.scheduled_for,
            authorising_rule=plan.rule_id,
            reason_code=plan.reason_code,
            explanation=plan.explanation,
            attempts_remaining=plan.attempts_remaining,
            escalated=plan.step is ChaseStep.ESCALATE,
            escalation_trigger=(
                plan.escalation_trigger.value if plan.escalation_trigger else None
            ),
            reminder_sent=outcome.reminder_sent,
            suppressed_rule=plan.suppressed_rule,
            payment_link_id=outcome.payment_link_id,
            amount_recovered_paise=outcome.recovered_paise,
            customer_reliability=reliability,
        )
        self._db.merge(row)
        self._db.commit()
        return row

    # --- helpers -----------------------------------------------------------

    def _book_at_risk(self, invoice: Invoice) -> int:
        """This invoice's amount at risk, booked on exactly one entry.

        The map is built by the runner and holds two kinds of invoice: one that
        is genuinely overdue and unpaid, and one that settled against a promise
        this run tracked. Everything else contributes zero — an invoice that is
        not yet due is not at risk, and putting the whole ledger in the
        denominator would make the recovery rate a statement about nothing.

        The second kind matters as much as the first: money that came back has to
        have been at risk first, or the rate has a numerator with no denominator.
        """
        if invoice.invoice_id in self._booked:
            return 0
        amount = self._at_risk.get(invoice.invoice_id, 0)
        if amount <= 0:
            return 0
        self._booked.add(invoice.invoice_id)
        return amount

    def messages_recorded(self, invoice_id: str) -> int:
        """Reminders this run has already marked for delivery on one invoice.

        Counted from the trail rather than a local tally, for the same reason the
        summary is: a counter and the log it claims to describe can drift, and
        QH2 is a bound the trail has to be able to prove.
        """
        return sum(
            1
            for entry in self._trail.query(
                batch_id=self._batch_id, entity_id=invoice_id, limit=1000
            )
            if entry.action == Action.SEND_REMINDER.value
            and entry.outcome == Outcome.SCHEDULED.value
        )


def _reply_text(invoice: Invoice, reply_id: str) -> str:
    for reply in invoice.replies:
        if reply.reply_id == reply_id:
            return reply.text
    return ""


#: `ChaseStep` -> what the audit trail records. Held here rather than reused from
#: `schemas.ACTION_BY_STEP` for the outcome half: a step maps to one action, but
#: the same action can be a refusal or a permission, and the outcome is what says
#: which.
_ACTION_BY_STEP: dict[ChaseStep, Action] = {
    ChaseStep.SEND_REMINDER: Action.SEND_REMINDER,
    ChaseStep.HOLD_FOR_PROMISE: Action.BLOCK_ATTEMPT,
    ChaseStep.FREEZE_DISPUTE: Action.ESCALATE,
    ChaseStep.ESCALATE: Action.ESCALATE,
    ChaseStep.DEFER: Action.BLOCK_ATTEMPT,
    ChaseStep.HALT: Action.HALT_SCHEDULE,
    ChaseStep.NO_ACTION: Action.BLOCK_ATTEMPT,
}

def _outcome_for(plan: ChasePlan) -> Outcome:
    """How the trail records one chase decision.

    `no_action` is the case that needs the `allowed` flag. It covers two things
    that look identical in the step and are opposite in meaning: the gate
    refusing a reminder, and the reasoning layer *choosing* not to send one on a
    reply that asked for patience. The first is `blocked` — a refusal, and part
    of the compliance evidence. The second is `skipped` — permitted, deliberate,
    and not something the denial count should include.
    """
    if plan.step is ChaseStep.NO_ACTION and plan.allowed:
        return Outcome.SKIPPED
    return _OUTCOME_BY_STEP[plan.step]


_OUTCOME_BY_STEP: dict[ChaseStep, Outcome] = {
    ChaseStep.SEND_REMINDER: Outcome.SCHEDULED,
    # A deprioritised invoice was deliberately not chased. `skipped`, not
    # `blocked`: nothing refused it, the system chose to leave a cooperating
    # customer alone.
    ChaseStep.HOLD_FOR_PROMISE: Outcome.SKIPPED,
    ChaseStep.FREEZE_DISPUTE: Outcome.ESCALATED,
    ChaseStep.ESCALATE: Outcome.ESCALATED,
    ChaseStep.DEFER: Outcome.BLOCKED,
    ChaseStep.HALT: Outcome.HALTED,
    ChaseStep.NO_ACTION: Outcome.BLOCKED,
}

#: Steps that produce a message the customer would have received. Escalations
#: deliberately do not: a human decides what that customer hears, and drafting
#: one anyway would be the system acting past the point at which it handed over.
MESSAGE_STEPS: frozenset[ChaseStep] = frozenset({ChaseStep.SEND_REMINDER})

#: Steps where a message the engine wanted to send did not go out. These get a
#: `held` communication row so the dashboard can show what was withheld and why —
#: a suppression nobody can read is not evidence of anything.
HELD_STEPS: frozenset[ChaseStep] = frozenset(
    {ChaseStep.DEFER, ChaseStep.FREEZE_DISPUTE, ChaseStep.HOLD_FOR_PROMISE}
)

__all__ = [
    "HELD_STEPS",
    "MESSAGE_STEPS",
    "PAYMENT_LINK_RULE",
    "STATUS_HELD",
    "STATUS_MARKED",
    "STATUS_SUPPRESSED",
    "InvoiceOutcome",
    "ReceivablesService",
]
