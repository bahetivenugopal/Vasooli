"""The batch runner: read replies -> track promises -> prioritise -> chase -> audit.

One entry point, one honest summary. Every figure in that summary is recomputed
from the audit trail at the end of the run, never accumulated in a counter as the
loop goes — a counter and the trail it claims to summarise can drift, and then
the headline number and its evidence disagree.

**The phase order is load-bearing and worth stating.** Replies are read *before*
anything is prioritised, because a live promise deprioritises its invoice
(`policy-bounds:PR2`) and a customer's promise reliability feeds their other
invoices' scores. Then the chase runs **in rank order**, which has one real
consequence: the RL3 per-customer contact cap is consumed highest-priority-first,
so when a customer's weekly budget runs out it runs out on their least important
invoice. That is a design decision, not an accident of iteration order.

Two figures in the summary deserve the scepticism a reader should bring to them,
and both carry their definition inline rather than in a footnote:

- **Recovery.** The engine does not collect money in this run; the ledger records
  payments that arrived. What the engine does is decide which of those settled a
  commitment. `recovery_definition` says so in the response body, so the number
  cannot be quoted without the caveat.
- **Extraction accuracy.** Reported over model-handled replies only, with the
  abstention rate beside it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.receivables import promises as promise_rules
from app.engines.receivables.config import ReceivablesConfig, default_config
from app.engines.receivables.dataset import (
    dataset_anchor,
    dataset_batch_id,
    load_invoices,
    manifest_for,
    resolve_dataset,
)
from app.engines.receivables.ladder import ChaseLadder, ContactCounts
from app.engines.receivables.prioritization import build_worklist
from app.engines.receivables.recovery import (
    HELD_STEPS,
    MESSAGE_STEPS,
    InvoiceOutcome,
    ReceivablesService,
)
from app.engines.receivables.schemas import (
    AgeingBreakdown,
    ChaseStep,
    CustomerReliability,
    ExtractionResult,
    ExtractionScore,
    Invoice,
    PriorityScore,
    PromiseAssessment,
    PromiseStatus,
    RunSummary,
)
from app.engines.receivables.scoring import load_manifest, score_extractions
from app.engines.receivables.understanding import (
    TASK_UNDERSTAND_REPLY,
    register_receivables_tasks,
    understand_reply,
)
from app.models.audit import AuditEntry
from app.models.enums import Action, BatchStatus, Engine, Outcome, ProvenanceSource
from app.services.audit_trail import AuditTrail
from app.services.llm_agent import REGISTRY, LLMAgent
from app.services.policy_engine import PolicyEngine
from app.services.razorpay_client import RazorpayClient

RECOVERY_DEFINITION = (
    "amount recovered = the outstanding value of invoices that settled against a "
    "promise the engine extracted and tracked as kept. The engine does not "
    "collect: the payments are in the ledger, and what this measures is the "
    "engine correctly identifying which of them honoured a commitment. Stated "
    "explicitly because a recovery rate that implied the engine caused the "
    "payment would be claiming more than the run demonstrates."
)


@dataclass
class RunArtefacts:
    """Everything one run produced, for tests and the demo script."""

    summary: RunSummary
    outcomes: list[InvoiceOutcome] = field(default_factory=list)
    extractions: list[ExtractionResult] = field(default_factory=list)
    assessments: list[PromiseAssessment] = field(default_factory=list)
    worklist: list[PriorityScore] = field(default_factory=list)


class ReceivablesRunner:
    """Runs Engine 3 over one invoice ledger."""

    def __init__(
        self,
        db: Session,
        *,
        config: ReceivablesConfig | None = None,
        agent: LLMAgent | None = None,
        policy: PolicyEngine | None = None,
        razorpay: RazorpayClient | None = None,
    ) -> None:
        self._db = db
        self._config = config or default_config()
        self._trail = AuditTrail(db)
        self._policy = policy or PolicyEngine()
        self._razorpay = razorpay or RazorpayClient(audit=self._trail)

        if TASK_UNDERSTAND_REPLY not in REGISTRY.names():
            register_receivables_tasks()
        self._agent = agent or LLMAgent()
        self._ladder = ChaseLadder(policy=self._policy, config=self._config)

    # --- the loop ----------------------------------------------------------

    def run(
        self,
        *,
        batch_id: str,
        seed: int,
        dataset: Path | str | None = None,
        now: datetime | None = None,
        score: bool = True,
    ) -> RunArtefacts:
        """Read, track, prioritise, chase, audit and summarise.

        `now` defaults to the latest **observed** event in the ledger — the last
        reply, contact or payment — rounded up to the hour. Phase 2's invoice
        book is anchored to a fixed `as_of` that is not today, and several
        invoices are not yet due, so neither wall-clock time nor "the latest
        timestamp in the file" is right. See `dataset.dataset_anchor`.

        `score=False` skips the ground-truth comparison, for callers running
        against a dataset with no manifest beside it.
        """
        dataset_path = resolve_dataset(dataset)
        invoices = load_invoices(dataset_path)
        source_batch = dataset_batch_id(invoices)
        run_now = (now or dataset_anchor(invoices)).astimezone(UTC)

        self._trail.start_batch(
            batch_id=batch_id,
            engine=Engine.RECEIVABLES,
            seed=seed,
            notes={
                "dataset_batch_id": source_batch,
                "dataset_path": str(dataset_path),
                "now": run_now.isoformat(),
                "config_version": self._config.version,
                "razorpay_mode": self._razorpay.mode.value,
                "llm_provider_enabled": self._agent.provider_enabled,
            },
        )

        # --- 1. Read every reply, before anything is ranked or recorded -----
        #
        # Reading is separated from recording so the at-risk denominator can be
        # decided before the first entry is written: an invoice that settled
        # against a promise belongs in that denominator, and which ones did is
        # not knowable until the promises have been assessed.
        extractions = self._read_replies(invoices, run_now)
        by_invoice_extraction = {e.invoice_id: e for e in extractions}

        # --- 2. Resolve every promise's status ------------------------------
        assessments = self._assess_promises(invoices, extractions, run_now)
        by_invoice_promise = {a.invoice_id: a for a in assessments}
        reliability = promise_rules.reliability(assessments)

        service = ReceivablesService(
            db=self._db,
            trail=self._trail,
            policy=self._policy,
            razorpay=self._razorpay,
            config=self._config,
            batch_id=batch_id,
            at_risk_paise=self._at_risk(invoices, by_invoice_promise, run_now),
        )
        by_id = {i.invoice_id: i for i in invoices}

        # --- 3. Record what was read, and what became of each commitment ----
        for extraction in extractions:
            service.record_extraction(by_id[extraction.invoice_id], extraction)
        for assessment in assessments:
            service.record_promise(
                by_id[assessment.invoice_id],
                assessment,
                extraction=by_invoice_extraction[assessment.invoice_id],
            )

        # --- 4. Rank ---------------------------------------------------------
        customer_messages = self._inherited_customer_contacts(invoices, run_now)
        worklist = build_worklist(
            invoices=invoices,
            config=self._config,
            now=run_now,
            reliability=reliability,
            promises=by_invoice_promise,
            customer_messages_used=dict(customer_messages),
        )

        # --- 5. Chase, in rank order -----------------------------------------
        outcomes: list[InvoiceOutcome] = []
        for score_row in worklist:
            invoice = by_id[score_row.invoice_id]
            outcomes.append(
                self._chase(
                    invoice=invoice,
                    score_row=score_row,
                    service=service,
                    extraction=by_invoice_extraction.get(invoice.invoice_id),
                    promise=by_invoice_promise.get(invoice.invoice_id),
                    customer_messages=customer_messages,
                    reliability=reliability,
                    now=run_now,
                )
            )

        extraction_score = self._score(extractions, dataset_path, enabled=score)
        summary = self._summarise(
            batch_id=batch_id,
            dataset_batch_id=source_batch,
            seed=seed,
            now=run_now,
            invoices=invoices,
            outcomes=outcomes,
            assessments=assessments,
            reliability=reliability,
            extraction=extraction_score,
        )
        run = self._trail.complete_batch(batch_id, status=BatchStatus.COMPLETED)
        # The extraction score is the one figure in this run that cannot be
        # recomputed from the audit trail: it needs the manifest, and nothing
        # serving an API request should be reaching for one. So it is persisted
        # on the batch record, beside the dataset id that produced it, and the
        # route serves exactly the numbers the run reported.
        run.notes = {
            **(run.notes or {}),
            "extraction": summary.extraction.model_dump(mode="json"),
            "run_summary": summary.model_dump(mode="json"),
        }
        self._db.commit()
        return RunArtefacts(
            summary=summary,
            outcomes=outcomes,
            extractions=extractions,
            assessments=assessments,
            worklist=worklist,
        )

    # --- phases -------------------------------------------------------------

    def _read_replies(self, invoices: list[Invoice], now: datetime) -> list[ExtractionResult]:
        """Understand every inbound reply in the ledger, or abstain on it."""
        results: list[ExtractionResult] = []
        for invoice in invoices:
            reply = invoice.latest_reply
            if reply is None:
                continue
            results.append(
                understand_reply(
                    invoice=invoice,
                    reply=reply,
                    agent=self._agent,
                    now=now,
                    min_confidence=self._config.understanding_min_confidence,
                )
            )
        return results

    def _at_risk(
        self,
        invoices: list[Invoice],
        promises: dict[str, PromiseAssessment],
        now: datetime,
    ) -> dict[str, int]:
        """What each invoice contributes to the recovery denominator.

        Two kinds of invoice qualify, and nothing else does:

        1. **Overdue and unpaid** — the money the engine is chasing. Its
           outstanding balance.
        2. **Settled against a commitment this run tracked** — its full amount.
           Money that came back has to have been at risk first, or the recovery
           rate has a numerator with no denominator.

        An invoice that is not yet due is not at risk, and one that settled with
        no commitment behind it is not this engine's recovery to claim. Both are
        excluded, which is what keeps the rate a statement about the chase rather
        than about the ledger.
        """
        at_risk: dict[str, int] = {}
        for invoice in invoices:
            if not invoice.is_paid and invoice.days_late(now) > 0:
                at_risk[invoice.invoice_id] = invoice.outstanding_paise
            elif invoice.is_paid and invoice.invoice_id in promises:
                at_risk[invoice.invoice_id] = invoice.amount_paise
        return at_risk

    def _assess_promises(
        self,
        invoices: list[Invoice],
        extractions: list[ExtractionResult],
        now: datetime,
    ) -> list[PromiseAssessment]:
        """Turn readings into a promise register with statuses.

        Broken-promise detection is this call and nothing else — an explicit
        check that returns a status with a citation, run over every promise every
        time. A promise that decayed into "broken" as a side effect of some other
        branch would be a promise nobody could audit.
        """
        by_id = {i.invoice_id: i for i in invoices}
        assessments = [
            assessment
            for extraction in extractions
            if (
                assessment := promise_rules.assess(
                    invoice=by_id[extraction.invoice_id],
                    extraction=extraction,
                    config=self._config,
                    now=now,
                )
            )
            is not None
        ]
        return promise_rules.supersede(assessments)

    def _chase(
        self,
        *,
        invoice: Invoice,
        score_row: PriorityScore,
        service: ReceivablesService,
        extraction: ExtractionResult | None,
        promise: PromiseAssessment | None,
        customer_messages: Counter[str],
        reliability: dict[str, CustomerReliability],
        now: datetime,
    ) -> InvoiceOutcome:
        """Plan and carry out one invoice's next bounded step."""
        counts = ContactCounts(
            invoice_messages_in_window=self._invoice_contacts_in_window(invoice, now)
            + service.messages_recorded(invoice.invoice_id),
            customer_messages_in_window=customer_messages[invoice.customer_id],
        )
        plan = self._ladder.plan(
            invoice, extraction=extraction, promise=promise, counts=counts, now=now
        )
        outcome = InvoiceOutcome(
            invoice=invoice, plan=plan, score=score_row, extraction=extraction, promise=promise
        )
        outcome.decision_entry_id = service.record_plan(invoice, plan)

        if plan.step in MESSAGE_STEPS:
            row, _draft, violations, link_failed = service.draft_reminder(
                invoice, plan, now=now
            )
            outcome.communication = row
            outcome.tone_violations = violations or None
            outcome.payment_link_id = row.payment_link_id
            outcome.payment_link_failed = link_failed
            outcome.reminder_sent = not violations
            if outcome.reminder_sent:
                # The contact this message consumes, spent immediately so the
                # next invoice for the same customer sees a smaller budget. This
                # is what makes the RL3 cap bite in rank order rather than only
                # being checked against a stale count.
                customer_messages[invoice.customer_id] += 1
        elif plan.step in HELD_STEPS:
            outcome.communication = service.record_held_reminder(invoice, plan)

        if promise is not None and promise.status is PromiseStatus.KEPT and invoice.is_paid:
            outcome.recovered_paise = invoice.amount_paid_paise

        service.persist_state(
            outcome,
            reliability=(
                reliability[invoice.customer_id].reliability
                if invoice.customer_id in reliability
                else None
            ),
        )
        return outcome

    def _score(
        self, extractions: list[ExtractionResult], dataset_path: Path, *, enabled: bool
    ) -> ExtractionScore:
        if not enabled:
            return ExtractionScore(
                ground_truth_source="not scored",
                scoring_note="scoring disabled for this run",
            )
        return score_extractions(extractions, load_manifest(manifest_for(dataset_path)))

    # --- contact-window bookkeeping ----------------------------------------

    def _invoice_contacts_in_window(self, invoice: Invoice, now: datetime) -> int:
        """Outbound contacts on this invoice inside the QH2 rolling window.

        Deliberately not `rungs_used`: the ladder position is a lifetime count
        and the contact cap is a rolling one. On this ledger they differ — the
        generator spaces reminders a week apart — and conflating them would make
        an invoice at rung 3 look capped when its window has long since cleared.
        """
        cutoff = now - self._config.contact_caps.window
        return sum(
            1
            for c in invoice.communications
            if c.direction == "outbound" and c.sent_at > cutoff
        )

    def _inherited_customer_contacts(
        self, invoices: list[Invoice], now: datetime
    ) -> Counter[str]:
        """Messages each customer has already received inside the RL3 window.

        Summed across every invoice they owe, from the ledger's own history. This
        is the count the cross-invoice cap is checked against, and starting it at
        zero would let a run send a customer four fresh messages on top of the
        four they already had this week.
        """
        counts: Counter[str] = Counter()
        for invoice in invoices:
            counts[invoice.customer_id] += self._invoice_contacts_in_window(invoice, now)
        return counts

    # --- the summary, computed from the trail ------------------------------

    def _summarise(
        self,
        *,
        batch_id: str,
        dataset_batch_id: str,
        seed: int,
        now: datetime,
        invoices: list[Invoice],
        outcomes: list[InvoiceOutcome],
        assessments: list[PromiseAssessment],
        reliability: dict[str, CustomerReliability],
        extraction: ExtractionScore,
    ) -> RunSummary:
        entries = list(
            self._db.scalars(select(AuditEntry).where(AuditEntry.batch_id == batch_id))
        )
        base = self._trail.batch_summary(batch_id)
        meta = [(e, e.entry_metadata or {}) for e in entries]

        # A refusal outcome is not always a refusal: a *permitted* escalation is
        # recorded `escalated` too, and counting it would report every authorised
        # handoff to a human as the gate having said no.
        denials = [
            e
            for e, m in meta
            if e.outcome in _REFUSAL_OUTCOMES and m.get("permitted") is not True
        ]
        suppressed = [e for e, m in meta if m.get("suppressed")]
        fallbacks = [
            e
            for e, m in meta
            if m.get("degraded")
            and e.provenance.get("source") == ProvenanceSource.DETERMINISTIC.value
        ]
        model_entries = [
            e for e in entries if e.provenance.get("source") == ProvenanceSource.MODEL.value
        ]
        # Only the drafted-message entries. `record_plan` also writes a
        # `send_reminder` entry for the *decision* to send one, and counting both
        # would double every reminder figure in the summary.
        reminders = [
            (e, m)
            for e, m in meta
            if e.action == Action.SEND_REMINDER.value and m.get("kind") == "reminder"
        ]
        links = [
            (e, m)
            for e, m in meta
            if e.action == Action.API_CALL.value and m.get("operation") == "payment_link.create"
        ]

        # Genuinely past due, not merely unpaid. This ledger carries invoices
        # that are open and not yet due; they are not overdue and are not chased.
        overdue = [i for i in invoices if not i.is_paid and i.days_late(now) > 0]
        by_status = Counter(o.plan.step for o in outcomes)

        return RunSummary(
            batch_id=batch_id,
            dataset_batch_id=dataset_batch_id,
            seed=seed,
            now=now,
            invoices_ingested=len(invoices),
            invoices_overdue=len(overdue),
            invoices_worklisted=len(outcomes),
            amount_outstanding_paise=sum(i.outstanding_paise for i in overdue),
            amount_at_risk_paise=base.amount_at_risk_paise,
            amount_recovered_paise=base.amount_recovered_paise,
            recovery_rate=base.recovery_rate,
            recovery_definition=RECOVERY_DEFINITION,
            by_ageing_bucket=self._by_ageing(invoices, outcomes, assessments, now),
            extraction=extraction,
            promises_made=len(assessments),
            promises_kept=sum(1 for a in assessments if a.status is PromiseStatus.KEPT),
            promises_broken=sum(1 for a in assessments if a.status is PromiseStatus.BROKEN),
            promises_active=sum(1 for a in assessments if a.status is PromiseStatus.ACTIVE),
            promises_superseded=sum(
                1 for a in assessments if a.status is PromiseStatus.SUPERSEDED
            ),
            promises_conditional=sum(1 for a in assessments if a.conditional),
            promises_undateable=sum(1 for a in assessments if a.committed_date is None),
            customer_reliability=reliability,
            reminders_sent=sum(
                1 for e, _ in reminders if e.outcome == Outcome.SCHEDULED.value
            ),
            reminders_held=sum(1 for o in outcomes if o.plan.step in HELD_STEPS),
            reminders_by_rung=dict(
                Counter(
                    str(m.get("rung"))
                    for e, m in reminders
                    if e.outcome == Outcome.SCHEDULED.value and m.get("rung")
                )
            ),
            payment_links_created=sum(
                1 for e, _ in links if e.outcome == Outcome.SUCCESS.value
            ),
            payment_links_failed=sum(
                1 for e, _ in links if e.outcome != Outcome.SUCCESS.value
            ),
            chase_steps=dict(sorted((s.value, n) for s, n in by_status.items())),
            escalations=by_status[ChaseStep.ESCALATE] + by_status[ChaseStep.FREEZE_DISPUTE],
            escalations_by_trigger=dict(
                Counter(
                    o.plan.escalation_trigger.value
                    for o in outcomes
                    if o.plan.escalation_trigger is not None
                )
            ),
            disputes_frozen=by_status[ChaseStep.FREEZE_DISPUTE],
            messages_suppressed=len(suppressed),
            suppressed_by_rule=dict(
                Counter(
                    str(m.get("suppressed_rule")) for _e, m in meta if m.get("suppressed")
                )
            ),
            invoices_deprioritised=sum(1 for o in outcomes if o.score.deprioritised),
            action_counts=base.action_counts,
            outcome_counts=base.outcome_counts,
            policy_denials=len(denials),
            denials_by_rule=dict(Counter(e.authorising_rule for e in denials)),
            llm_fallbacks=len(fallbacks),
            abstentions=base.abstained_count,
            provenance={
                "model_entries": len(model_entries),
                "deterministic_entries": len(entries) - len(model_entries),
                "cache_hits": sum(1 for e in model_entries if e.provenance.get("cache_hit")),
                "abstentions": base.abstained_count,
                "policy_violations": base.policy_violations,
                "by_source": {k: v.model_dump() for k, v in base.by_source.items()},
            },
        )

    def _by_ageing(
        self,
        invoices: list[Invoice],
        outcomes: list[InvoiceOutcome],
        assessments: list[PromiseAssessment],
        now: datetime,
    ) -> dict[str, AgeingBreakdown]:
        """Recovery per ageing bucket. Expect older buckets to do worse; say so.

        Computed over the whole ledger rather than only the worklist. Recovery is
        read off the **promise register**, not off the outcomes, and that is the
        fix for a bug the demo made obvious: every settled invoice is excluded
        from the worklist by HS1, so an outcome-based attribution put the entire
        recovered amount in no bucket at all and the table read as a column of
        zeroes beside a non-zero total.
        """
        buckets: dict[str, AgeingBreakdown] = {
            b.name: AgeingBreakdown() for b in self._config.ageing_buckets
        }
        by_id = {i.invoice_id: i for i in invoices}
        recovered_by_invoice = {
            a.invoice_id: by_id[a.invoice_id].amount_paid_paise
            for a in assessments
            if a.status is PromiseStatus.KEPT and by_id[a.invoice_id].is_paid
        }
        step_by_invoice = {o.invoice.invoice_id: o.plan.step for o in outcomes}
        reminded = {o.invoice.invoice_id for o in outcomes if o.reminder_sent}

        for invoice in invoices:
            bucket = buckets[self._config.ageing_bucket(invoice.days_late(now))]
            bucket.invoices += 1
            bucket.amount_outstanding_paise += invoice.outstanding_paise
            bucket.amount_recovered_paise += recovered_by_invoice.get(invoice.invoice_id, 0)
            bucket.reminders_sent += int(invoice.invoice_id in reminded)
            if step_by_invoice.get(invoice.invoice_id) in {
                ChaseStep.ESCALATE,
                ChaseStep.FREEZE_DISPUTE,
            }:
                bucket.escalations += 1

        for bucket in buckets.values():
            denominator = bucket.amount_outstanding_paise + bucket.amount_recovered_paise
            bucket.recovery_rate = (
                round(bucket.amount_recovered_paise / denominator, 4) if denominator else 0.0
            )
        return buckets


#: The outcomes that mean "the system refused". Held here rather than inline so
#: the summary and `/audit-check` cannot drift on what counts as a refusal.
_REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {Outcome.BLOCKED.value, Outcome.HALTED.value, Outcome.ESCALATED.value}
)

__all__ = ["RECOVERY_DEFINITION", "ReceivablesRunner", "RunArtefacts"]
