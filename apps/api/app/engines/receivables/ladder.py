"""The bounded chase ladder, and the gate every rung passes through.

Engine 3 is entirely outreach, which makes one Phase 4 lesson load-bearing here
rather than incidental: **a message is not a charge, and the gate cannot tell
them apart on its own.** `PolicyEngine.evaluate()` applies the attempt cap and
the cooldown to whatever action is proposed, so the entity handed to it has to
present the *contact* count as its attempt count. `invoice_entity()` is that
shape. Passing anything else would gate a reminder on a budget that has nothing
to do with reminders.

What this module decides, in order:

1. What the ladder *would* do next — which rung, or that there is no rung left.
2. What evidence exists for escalating (`policy-bounds:RL5`): a broken promise,
   or an exhausted ladder. Elapsed time is not evidence.
3. What the policy engine permits, with the reasoning layer's own recommendation
   put in front of it through `authorize()` when there was a reply to read.

The rules that do the refusing — RL3's per-customer cap, RL4's dispute freeze,
PR2's live-promise suppression — live in `policy_engine.py`, not here. That is
the point: a precondition an engine is trusted to check for itself is an
intention, and an engine can always forget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.engines.receivables.config import ReceivablesConfig, Rung
from app.engines.receivables.schemas import (
    ACTION_BY_RECOMMENDATION,
    ChasePlan,
    ChaseStep,
    ExtractionResult,
    Invoice,
    PromiseAssessment,
    PromiseStatus,
)
from app.models.entity import RecoverableEntity
from app.models.enums import Action, Engine, EntityType, EscalationTrigger, Outcome
from app.services.policy_engine import (
    LLMRecommendation,
    PolicyDecision,
    PolicyEngine,
    PolicyRequest,
)

#: Rules that mean a message the engine wanted to send was refused. §5.6's
#: "messages suppressed by caps, quiet hours, or dispute freeze" — the direct
#: measure of harassment avoided. Held here so the ladder, the summary and the
#: tests cannot drift on what counts.
SUPPRESSION_RULES: frozenset[str] = frozenset(
    {
        "policy-bounds:QH1",  # quiet hours
        "policy-bounds:QH2",  # per-invoice contact cap
        "policy-bounds:RL3",  # per-customer contact cap
        "policy-bounds:RL4",  # dispute freeze
    }
)

#: Rules whose refusal is about *timing*, not permission. The revenue is still
#: recoverable; only the moment was wrong. Counted apart from suppressions for
#: the same reason Engine 1 split "suppressed" from "deferred".
DEFERRAL_RULES: frozenset[str] = frozenset({"policy-bounds:QH1", "policy-bounds:RL2"})


#: What each escalation trigger means, in one clause, for the audit rationale.
#: The evidence RL5 requires has to be legible in the trail, not only present in
#: metadata — an escalation whose entry says "no stopping rule fired" reads as an
#: escalation with no reason, which is the opposite of what the rule guarantees.
_EVIDENCE: dict[EscalationTrigger, str] = {
    EscalationTrigger.BROKEN_PROMISE: (
        "Escalating on a broken promise: the customer committed to a date and "
        "nothing arrived by it, past the grace window."
    ),
    EscalationTrigger.LADDER_EXHAUSTED: (
        "Escalating on an exhausted ladder: every rung has been spent and the "
        "invoice is still outstanding, so a human takes it from here."
    ),
    EscalationTrigger.DISPUTE: (
        "Escalating on a dispute: the customer says this invoice is wrong, and "
        "the system cannot adjudicate that."
    ),
    EscalationTrigger.ABSTENTION: (
        "Escalating on an abstention: the reasoning layer declined to read this "
        "reply rather than guess at it."
    ),
    EscalationTrigger.HIGH_VALUE: (
        "Escalating on value: above the high-value review threshold, a human "
        "looks at it before the ladder runs."
    ),
}


@dataclass(frozen=True)
class ContactCounts:
    """How much contact budget this invoice and this customer have left.

    Two counts, because two different rules consume them: QH2 bounds the
    conversation about one invoice, RL3 bounds what one recipient hears in a week
    across all of theirs.
    """

    invoice_messages_in_window: int
    customer_messages_in_window: int


def invoice_entity(
    invoice: Invoice, *, messages_sent: int, last_contact_at: datetime | None
) -> RecoverableEntity:
    """The invoice as the gate should see it for a **reminder**.

    `attempt_count` is the ladder position, not a retry count: RL1 caps it at the
    number of rungs and RL2 spaces successive rungs 72 hours apart, and both of
    those are bounds on *contact*. A settled invoice is terminal under
    `policy-bounds:HS1` — chasing someone who already paid is the most
    embarrassing failure a recovery system can have, so it is a hard stop rather
    than a filter someone has to remember to apply.
    """
    return RecoverableEntity(
        entity_id=invoice.invoice_id,
        entity_type=EntityType.INVOICE,
        amount_paise=invoice.outstanding_paise,
        currency=invoice.currency,
        status=invoice.status,
        attempt_count=messages_sent,
        last_attempt_at=last_contact_at,
        is_terminal=invoice.is_paid,
        terminal_reason="INVOICE_SETTLED" if invoice.is_paid else None,
    )


def escalation_evidence(
    *,
    invoice: Invoice,
    extraction: ExtractionResult | None,
    promise: PromiseAssessment | None,
    config: ReceivablesConfig,
) -> EscalationTrigger | None:
    """The RL5 evidence for escalating this invoice, or `None`.

    Ordered by what a human would want to know first. A broken promise outranks
    an exhausted ladder because it is the more actionable fact: the ladder ran out
    on plenty of invoices, but "they committed and did not follow through" is the
    sentence that decides what happens next.

    Note the two things that are deliberately **not** evidence: elapsed time, and
    a conditional promise going quiet. `policy-bounds:PP2` is explicit that a
    conditional promise cannot trigger an escalation, because there was never a
    date to break — its exit is ladder exhaustion like any other invoice.
    """
    if extraction is not None and extraction.abstained:
        return EscalationTrigger.ABSTENTION
    if extraction is not None and extraction.is_dispute:
        return EscalationTrigger.DISPUTE
    if promise is not None and promise.status is PromiseStatus.BROKEN:
        return EscalationTrigger.BROKEN_PROMISE
    if promise is not None and promise.suppresses_chase:
        # A live, unbroken promise outranks an exhausted ladder. Found on the
        # first live run: six invoices whose customers had committed and were
        # inside their window were being escalated anyway, because they happened
        # to have had three reminders before replying. Escalating a cooperating
        # customer for having been chased is exactly the behaviour RL5 exists to
        # prevent, and "the ladder ran out" is a timer argument wearing evidence's
        # clothes. The promise's own window decides when this resumes.
        return None
    if invoice.rungs_used >= len(config.ladder.rungs):
        return EscalationTrigger.LADDER_EXHAUSTED
    return None


class ChaseLadder:
    """Decides the next bounded step for one invoice, and says why."""

    def __init__(self, *, policy: PolicyEngine, config: ReceivablesConfig) -> None:
        self._policy = policy
        self._config = config

    def plan(
        self,
        invoice: Invoice,
        *,
        extraction: ExtractionResult | None,
        promise: PromiseAssessment | None,
        counts: ContactCounts,
        now: datetime,
    ) -> ChasePlan:
        """What happens to this invoice next, and which rule permits it."""
        trigger = escalation_evidence(
            invoice=invoice, extraction=extraction, promise=promise, config=self._config
        )
        rung = self._config.ladder.rung_for(invoice.rungs_used)

        # An escalation is proposed only when there is evidence for it. Without
        # evidence the proposal stays a reminder, and RL5 never has to fire —
        # the rule is the backstop for a caller that proposes one anyway, which
        # is exactly what a gate is for.
        escalating = trigger is not None and trigger is not EscalationTrigger.DISPUTE
        proposed = Action.ESCALATE if escalating else Action.SEND_REMINDER

        request = PolicyRequest(
            engine=Engine.RECEIVABLES,
            entity=invoice_entity(
                invoice,
                messages_sent=counts.invoice_messages_in_window,
                last_contact_at=invoice.last_contact_at,
            ),
            proposed_action=proposed,
            now=now,
            # An escalation is an internal handoff, not something a customer
            # receives, so it is not outreach and QH1 does not apply to it.
            is_outreach=not escalating,
            messages_sent_in_window=counts.invoice_messages_in_window,
            customer_messages_in_window=counts.customer_messages_in_window,
            # A dispute freezes everything, so it is passed on every request
            # rather than only on the ones that look like messages.
            dispute_frozen=extraction is not None and extraction.is_dispute,
            active_promise=promise is not None and promise.suppresses_chase,
            abstained=extraction is not None and extraction.abstained,
            escalation_trigger=trigger,
        )

        decision = self._decide(request, extraction)
        return self._to_plan(
            invoice=invoice,
            rung=rung,
            decision=decision,
            trigger=trigger,
            promise=promise,
            now=now,
        )

    # --- internals ---------------------------------------------------------

    def _decide(
        self, request: PolicyRequest, extraction: ExtractionResult | None
    ) -> PolicyDecision:
        """Evaluate, or authorize when the reasoning layer had something to say.

        The split mirrors Engine 1's: where a model judgment exists, it goes in
        front of the gate through `authorize()` and can only narrow the outcome;
        where none exists, `evaluate()` decides alone. An abstention is *not* a
        judgment — there is no recommendation to weigh — so it takes the
        `evaluate()` path and is refused by HE1.

        The recommendation is consulted **only on the reminder path**, and that
        boundary is load-bearing. What the model answered was "should this chase
        continue, given what this reply says?" — a question about the reply. A
        broken promise is a fact discovered *afterwards*, by comparing the
        commitment against a payment that never arrived, and the model never saw
        it. A test caught the consequence: a perfectly reasonable `pause_chase`
        on a credible-sounding promise was narrowing a broken-promise escalation
        down to doing nothing, which would let a stale recommendation suppress
        this engine's entire differentiator.
        """
        if (
            extraction is None
            or extraction.understanding is None
            or request.proposed_action is not Action.SEND_REMINDER
        ):
            return self._policy.evaluate(request)

        understanding = extraction.understanding
        return self._policy.authorize(
            request,
            LLMRecommendation(
                action=ACTION_BY_RECOMMENDATION[understanding.recommended_action],
                rationale=understanding.reasoning,
                confidence=understanding.confidence,
                # Carried verbatim from the reading, never rebuilt here. A
                # provenance object constructed at a call site is a claim about
                # how a result was produced, made by code that did not produce it.
                provenance=extraction.provenance,
            ),
        )

    def _to_plan(
        self,
        *,
        invoice: Invoice,
        rung: Rung | None,
        decision: PolicyDecision,
        trigger: EscalationTrigger | None,
        promise: PromiseAssessment | None,
        now: datetime,
    ) -> ChasePlan:
        step = self._step_for(decision, rung, promise)
        suppressed = decision.rule_id in SUPPRESSION_RULES and not decision.allowed
        scheduled = (
            decision.earliest_next_attempt_at
            if not decision.allowed and decision.earliest_next_attempt_at is not None
            else now
        )
        return ChasePlan(
            invoice_id=invoice.invoice_id,
            step=step,
            allowed=decision.allowed,
            rung=rung.name if rung is not None and step is ChaseStep.SEND_REMINDER else None,
            rung_number=rung.number if rung is not None else len(self._config.ladder.rungs),
            scheduled_for=scheduled,
            rule_id=decision.rule_id,
            reason_code=decision.reason_code,
            explanation=self._explain(decision, step, rung, invoice, trigger),
            attempts_remaining=decision.attempts_remaining,
            # A dispute freeze *is* an escalation to a human, so it carries its
            # trigger too. Reporting it only on `ESCALATE` would leave every
            # frozen invoice out of "escalations by cause", which is the split
            # the summary exists to show.
            escalation_trigger=(
                trigger if step in {ChaseStep.ESCALATE, ChaseStep.FREEZE_DISPUTE} else None
            ),
            suppressed=suppressed,
            suppressed_rule=decision.rule_id if suppressed else None,
            overridden_recommendation=decision.overridden_recommendation,
            authority_rule=decision.authority_rule_id,
        )

    def _step_for(
        self,
        decision: PolicyDecision,
        rung: Rung | None,
        promise: PromiseAssessment | None,
    ) -> ChaseStep:
        """Map the gate's answer onto what the engine actually does.

        Keyed on the rule that decided rather than on the outcome, because the
        outcomes collapse cases the engine has to keep apart: a dispute freeze, a
        live promise and an exhausted ladder are all `blocked` or `escalated`, and
        they lead to three completely different places.
        """
        if decision.allowed:
            if decision.action is Action.SEND_REMINDER:
                # A permitted reminder with no rung left would be a fourth rung.
                # Structurally impossible under RL1 — the cap refuses first — but
                # a silent off-by-one here would be invisible, so it is checked.
                return ChaseStep.SEND_REMINDER if rung is not None else ChaseStep.ESCALATE
            if decision.action is Action.ESCALATE:
                return ChaseStep.ESCALATE
            if decision.action is Action.HALT_SCHEDULE:
                return ChaseStep.HALT
            return ChaseStep.NO_ACTION

        if decision.rule_id == "policy-bounds:RL4":
            return ChaseStep.FREEZE_DISPUTE
        if decision.rule_id == "policy-bounds:PR2":
            return ChaseStep.HOLD_FOR_PROMISE
        if decision.outcome is Outcome.HALTED:
            return ChaseStep.HALT
        if decision.outcome is Outcome.ESCALATED:
            return ChaseStep.ESCALATE
        if decision.rule_id in DEFERRAL_RULES:
            return ChaseStep.DEFER
        if decision.rule_id in SUPPRESSION_RULES:
            return ChaseStep.DEFER
        return ChaseStep.NO_ACTION

    def _explain(
        self,
        decision: PolicyDecision,
        step: ChaseStep,
        rung: Rung | None,
        invoice: Invoice,
        trigger: EscalationTrigger | None,
    ) -> str:
        """One sentence a human can read, with the rule already in it."""
        if step is ChaseStep.SEND_REMINDER and rung is not None:
            return (
                f"Rung {rung.number} of {len(self._config.ladder.rungs)} "
                f"({rung.name}, {rung.tone} tone) on invoice {invoice.invoice_id}, "
                f"{invoice.rungs_used} contact(s) already made. {decision.rationale}"
            )
        if step is ChaseStep.ESCALATE and trigger is not None:
            # The gate's own rationale for a *permitted* escalation says only
            # that nothing stopped it, which on the demo surface reads as an
            # escalation with no reason. RL5's whole claim is that there was one,
            # so the evidence goes in front of it.
            return f"{_EVIDENCE[trigger]} {decision.rationale}"
        return decision.rationale


__all__ = [
    "DEFERRAL_RULES",
    "SUPPRESSION_RULES",
    "ChaseLadder",
    "ContactCounts",
    "escalation_evidence",
    "invoice_entity",
]
