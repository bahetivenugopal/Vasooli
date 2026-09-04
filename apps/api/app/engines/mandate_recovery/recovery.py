"""Bounded recovery actions for Engine 2, every one authorised before it happens.

Four things this engine can do, and no others: **attempt a debit**, **send a
pre-debit notice**, **draft a customer message**, **escalate**. Each one goes
through `policy_engine.py` first, and each produces an audit entry naming the
rule that authorised — or refused — it.

Three properties worth reading closely:

- **The refusal is a first-class action.** A hard decline that never receives a
  retry is a wasted attempt avoided, and it is recorded as deliberately as a
  debit that fired. Counting suppressions is only honest if they exist in the
  trail with a citation.
- **Money is booked once per mandate.** Engine 1 learned this the expensive way:
  the same rupees counted on a payment entry, then again on a corridor entry, and
  a third time on the API call, inflated the batch denominator by 86%. Here the
  first entry written for a mandate carries its amount at risk, and every later
  entry for that mandate carries zero.
- **Nothing is dispatched.** Communications are drafted, policy-gated, logged and
  rendered. There is no recipient field and no send path — see ADR 0009.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from random import Random
from typing import Any

from sqlalchemy.orm import Session

from app.engines.mandate_recovery.dunning import (
    TASK_DRAFT_DUNNING,
    TEMPLATE_RULE,
    build_context,
    render_template,
    validate_tone,
)
from app.engines.mandate_recovery.scheduler import ScheduleDecision, outreach_entity
from app.engines.mandate_recovery.schemas import (
    ACTION_BY_RECOMMENDATION,
    Channel,
    DunningDraft,
    DunningRecommendation,
    FailureRoute,
    Mandate,
    NextStep,
    ToneViolation,
)
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.mandate import MandateCommunication, MandateRecoveryState
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail
from app.services.decline_taxonomy import classify
from app.services.llm_agent import LLMAgent, ReasoningResult
from app.services.policy_engine import (
    THRESHOLDS,
    LLMRecommendation,
    PolicyEngine,
    PolicyRequest,
)
from app.services.razorpay_client import RazorpayClient

#: What a drafted message may be marked as. Never `sent`.
STATUS_MARKED = "marked_for_delivery"
STATUS_HELD = "held"
STATUS_SUPPRESSED = "suppressed"

#: The rule a pre-debit notice cites when it is permitted.
NOTICE_RULE = "rbi-mandate-rules:A2"


@dataclass
class MandateOutcome:
    """Everything one mandate produced in one run."""

    schedule: ScheduleDecision
    decision_entry_id: int
    debit_attempted: bool = False
    recovered_paise: int = 0
    retry_probability: float | None = None
    communication: MandateCommunication | None = None
    reasoning: ReasoningResult | None = None
    tone_violations: list[ToneViolation] | None = None


def afa_side(mandate: Mandate) -> str:
    """Which side of the A3 threshold this mandate sits on.

    Derived from the registered threshold rather than a literal, so the branch
    and the gate cannot drift apart (`policy-bounds:AT1`).
    """
    return (
        "above"
        if mandate.amount_paise > THRESHOLDS["afa_fresh_auth"].value_paise
        else "at_or_below"
    )


class MandateRecoveryService:
    """Executes Engine 2's bounded actions and records every one of them."""

    def __init__(
        self,
        *,
        db: Session,
        trail: AuditTrail,
        policy: PolicyEngine,
        razorpay: RazorpayClient,
        agent: LLMAgent,
        batch_id: str,
    ) -> None:
        self._db = db
        self._trail = trail
        self._policy = policy
        self._razorpay = razorpay
        self._agent = agent
        self._batch_id = batch_id
        #: Mandates whose amount at risk has already been booked. Money lives on
        #: exactly one entry per mandate; see the module docstring.
        self._booked: set[str] = set()

    # --- the primary decision ---------------------------------------------

    def record_decision(self, schedule: ScheduleDecision) -> int:
        """Write what the gate said about the proposed debit.

        Written first, before anything is executed, because an action without a
        record is unprovable. A debit that is permitted is recorded `pending` and
        resolved once its result is known.
        """
        mandate = schedule.mandate
        decision = schedule.decision
        explanation = schedule.explanation

        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.MANDATE_RECOVERY,
            entity_type=EntityType.MANDATE,
            entity_id=mandate.mandate_id,
            action=decision.action,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            authorising_rule=decision.rule_id,
            rationale=explanation.explanation,
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=self._book_at_risk(schedule),
            currency=mandate.currency,
            attempt_number=mandate.attempts_in_current_cycle + 1,
            attempts_remaining=decision.attempts_remaining,
            timestamp=schedule.explanation.scheduled_for,
            metadata=self._decision_metadata(schedule),
        )
        return int(entry.id)

    def _decision_metadata(self, schedule: ScheduleDecision) -> dict[str, Any]:
        mandate = schedule.mandate
        return {
            "proposed_action": Action.ATTEMPT_CHARGE.value,
            "next_step": schedule.explanation.step.value,
            "decline_code": schedule.classification.decline_code,
            "decline_class": schedule.classification.decline_class,
            "failure_route": schedule.classification.route.value,
            "classification_rule": schedule.classification.rule_id,
            "classification_reasoned": schedule.classification.reasoned,
            "notice_status": schedule.notice.status.value,
            "notice_lead_hours": schedule.notice.lead_hours,
            "afa_side": afa_side(mandate),
            "mandate_status": mandate.status,
            "attempts_in_cycle": mandate.attempts_in_current_cycle,
            "proposed_debit_at": _iso(schedule.proposed_debit_at),
            "proposed_notice_at": _iso(schedule.proposed_notice_at),
            # The two counts §5.4 asks for, flagged on the entry itself so the
            # summary reads them back rather than re-deriving the distinction.
            "suppressed_retry": schedule.suppressed,
            "compliance_blocked": schedule.explanation.compliance_blocked,
            **schedule.decision.override_metadata(),
        }

    def _book_at_risk(self, schedule: ScheduleDecision) -> int:
        """The amount at risk, once per mandate, and only where money is at risk.

        A mandate with no failure in the current cycle has nothing at risk — its
        debit has not happened yet. Booking it would put the whole mandate book in
        the denominator and make the recovery rate a statement about nothing.
        """
        mandate = schedule.mandate
        if schedule.classification.route is FailureRoute.NONE:
            return 0
        if mandate.mandate_id in self._booked:
            return 0
        self._booked.add(mandate.mandate_id)
        return mandate.amount_paise

    # --- the debit ---------------------------------------------------------

    def execute_debit(
        self,
        schedule: ScheduleDecision,
        entry_id: int,
        *,
        retry_model: Any,
        rng: Random,
    ) -> tuple[int, float]:
        """Fire the authorised debit and resolve its audit entry.

        The outcome is a seeded draw against the documented retry-success model,
        not a decision made here. `hours_since_previous` is measured from the last
        real attempt to the moment the debit fires, which is what the model's
        spacing term is defined against.
        """
        mandate = schedule.mandate
        at = schedule.proposed_debit_at or schedule.explanation.scheduled_for
        last = mandate.latest_failure
        hours_since = (
            (at - last.attempted_at).total_seconds() / 3600 if last and at else 24.0
        )
        outcome = retry_model.sample(
            rng,
            schedule.classification.decline_code,
            hours_since_previous=hours_since,
            attempt_number=mandate.attempts_in_current_cycle + 1,
        )
        recovered = self._call_razorpay(mandate, outcome)
        self._trail.resolve_outcome(
            entry_id,
            outcome=Outcome.SUCCESS if outcome.succeeded else Outcome.FAILURE,
            amount_recovered_paise=recovered,
        )
        return recovered, float(outcome.probability)

    def _call_razorpay(self, mandate: Mandate, outcome: Any) -> int:
        """Put the debit through the client and audit the call.

        A failed debit is replayed through the client only when its decline has a
        doc-verified upstream reason. Where it does not — most of the taxonomy —
        round-tripping it would normalize the failure to `UNKNOWN` and mislabel it,
        so `simulate_failure_key` of `None` means "do not call the client
        expecting a failure", never "call it and hope".
        """
        notes: dict[str, Any] = {
            "batch_id": self._batch_id,
            "mandate_id": mandate.mandate_id,
            "engine": Engine.MANDATE_RECOVERY.value,
        }
        if not outcome.succeeded:
            if outcome.simulate_failure_key is None:
                return 0
            notes["simulate_failure"] = outcome.simulate_failure_key

        result = self._razorpay.create_order(
            amount_paise=mandate.amount_paise,
            receipt=f"mandate-{mandate.mandate_id}",
            notes=notes,
            currency=mandate.currency,
        )
        # Audited with no money attached: the decision entry already carries this
        # mandate's amount at risk.
        self._razorpay.record_call(
            result,
            batch_id=self._batch_id,
            engine=Engine.MANDATE_RECOVERY,
            entity_type=EntityType.MANDATE,
            entity_id=mandate.mandate_id,
        )
        return mandate.amount_paise if result.ok and outcome.succeeded else 0

    # --- the pre-debit notice ---------------------------------------------

    def send_notice(self, schedule: ScheduleDecision) -> MandateCommunication:
        """Record the A2 notification, and whether the gate let it go out now.

        The notice is customer-facing, so it passes the outreach gate: quiet hours
        and the contact cap both apply. A notice held by quiet hours is *held*,
        not dropped, and the scheduler has already moved the debit with it so the
        A2 lead is preserved rather than quietly lost.
        """
        mandate = schedule.mandate
        at = schedule.proposed_notice_at or schedule.explanation.scheduled_for
        sent = self._messages_sent(mandate.mandate_id)
        decision = self._policy.evaluate(
            PolicyRequest(
                engine=Engine.MANDATE_RECOVERY,
                entity=outreach_entity(mandate, messages_sent=sent),
                proposed_action=Action.SEND_PRE_DEBIT_NOTICE,
                now=at,
                is_outreach=True,
                messages_sent_in_window=sent,
            )
        )
        held = not decision.allowed
        body = (
            f"Hi {mandate.customer_name}, this is advance notice that "
            f"Rs {mandate.amount_paise / 100:,.2f} will be collected for your "
            f"{mandate.frequency} subscription on "
            f"{(schedule.proposed_debit_at or mandate.next_debit_at):%d %b %Y}. "
            "No action is needed if that is fine."
        )
        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.MANDATE_RECOVERY,
            entity_type=EntityType.MANDATE,
            entity_id=mandate.mandate_id,
            action=Action.SEND_PRE_DEBIT_NOTICE,
            outcome=Outcome.BLOCKED if held else Outcome.SCHEDULED,
            reason_code=decision.reason_code if held else "PRE_DEBIT_NOTICE",
            authorising_rule=decision.rule_id if held else NOTICE_RULE,
            rationale=(
                decision.rationale
                if held
                else (
                    f"Pre-debit notice scheduled for {at:%Y-%m-%d %H:%M} UTC, "
                    f"{schedule.explanation.explanation}"
                )
            ),
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=self._book_at_risk(schedule),
            currency=mandate.currency,
            timestamp=at,
            metadata={
                "kind": "pre_debit_notice",
                "notice_status": schedule.notice.status.value,
                "notice_lead_hours": schedule.notice.lead_hours,
                "proposed_debit_at": _iso(schedule.proposed_debit_at),
                "held": held,
                "simulated_delivery": True,
            },
        )
        return self._persist_communication(
            mandate,
            schedule,
            kind="pre_debit_notice",
            channel=Channel.SMS,
            subject="Advance notice of your upcoming payment",
            body=body,
            call_to_action="none",
            status=STATUS_HELD if held else STATUS_MARKED,
            scheduled_for=at,
            authorising_rule=NOTICE_RULE,
            held_rule=decision.rule_id if held else None,
            rationale=f"[audit entry {entry.id}] {decision.rationale}",
            provenance=Provenance.deterministic(),
        )

    # --- the dunning message ----------------------------------------------

    def draft_communication(self, schedule: ScheduleDecision) -> MandateOutcome | None:
        """Draft, validate, gate and record one customer message.

        The order is the whole safety story: the model drafts, `policy-bounds` TN
        validates the text it produced, and only then does the policy engine
        decide whether the message may go out at all — with the model's own
        recommendation put in front of it via `authorize()`, never applied to it.
        """
        mandate = schedule.mandate
        context = self._dunning_context(schedule)
        result = self._agent.run(TASK_DRAFT_DUNNING, context)
        draft = _draft_from(result, context)

        violations = validate_tone(draft, schedule.classification.route)
        provenance = result.provenance
        confidence = result.confidence
        if violations:
            # TN3: discard, do not edit. The template is accurate by construction,
            # and the rejection stays visible in the trail as a rejection.
            draft = render_template(context)
            provenance = Provenance.deterministic()
            confidence = None

        recommendation = LLMRecommendation(
            action=ACTION_BY_RECOMMENDATION[draft.recommended_action],
            rationale=draft.reasoning,
            confidence=draft.confidence,
            provenance=provenance,
        )
        sent = self._messages_sent(mandate.mandate_id)
        request = PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            # An outreach view of the mandate: what is bounded here is the
            # contact, not the charge. See `scheduler.outreach_entity`.
            entity=outreach_entity(mandate, messages_sent=sent),
            proposed_action=Action.SEND_DUNNING,
            now=schedule.explanation.scheduled_for or mandate.next_debit_at,
            decline_code=None,
            is_outreach=True,
            messages_sent_in_window=sent,
        )
        decision = self._policy.authorize(request, recommendation)

        # An inadmissible *recommendation* is a defect in the draft, not a reason
        # to say nothing. The model that recommends retrying a paused mandate has
        # usually written a message promising that retry — mnd_0006's own
        # reasoning on the live run said the message "prepares them for the
        # scheduled retry" — so the text cannot be sent as written either. Same
        # remedy as a tone violation (`policy-bounds:TN3`): discard the draft,
        # fall back to the template, and keep the refusal on the record. The
        # customer still gets told what happened, which is the difference between
        # "stopped retrying" and "gave up on the revenue".
        overruled = decision.overridden_recommendation
        overruled_note = ""
        # `authorize()` returns a base denial *before* it looks at the envelope,
        # so a message held by quiet hours would otherwise keep a draft whose
        # recommendation the gate never got to refuse — and be delivered at 09:00
        # still promising the retry. Checking the recommendation directly as well
        # makes the substitution independent of which rule refused first.
        promises_a_retry = draft.recommended_action is DunningRecommendation.SCHEDULE_RETRY
        if (
            decision.rule_id == "policy_engine:llm_authority.out_of_envelope"
            or promises_a_retry
        ):
            overruled = overruled or draft.recommended_action.value
            overruled_note = (
                f" The reasoning layer recommended "
                f"'{draft.recommended_action.value}' at confidence "
                f"{draft.confidence:.2f}; the rules do not permit one here, so the "
                "draft written to announce it was discarded and the templated "
                "message sent in its place."
            )
            draft = render_template(context)
            provenance = Provenance.deterministic()
            confidence = None
            decision = self._policy.authorize(
                request,
                LLMRecommendation(
                    action=ACTION_BY_RECOMMENDATION[draft.recommended_action],
                    rationale=draft.reasoning,
                    confidence=draft.confidence,
                    provenance=provenance,
                ),
            )

        allowed = decision.allowed and decision.action is Action.SEND_DUNNING
        if decision.allowed and not allowed:
            # The model chose to do less than send a message. Permitted, and
            # recorded as a deliberate skip rather than a failure.
            status = STATUS_SUPPRESSED
        elif allowed:
            status = STATUS_MARKED
        else:
            status = STATUS_HELD

        # `policy-bounds:QH1` is explicit that a message inside quiet hours is
        # "blocked and rescheduled to the next window opening, never dropped".
        # The block is recorded as a block — that is the compliance evidence —
        # and the message carries the time it becomes permissible, so "held"
        # never quietly means "lost".
        deliver_at = (
            decision.earliest_next_attempt_at
            if status is STATUS_HELD and decision.earliest_next_attempt_at is not None
            else schedule.explanation.scheduled_for
        )

        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.MANDATE_RECOVERY,
            entity_type=EntityType.MANDATE,
            entity_id=mandate.mandate_id,
            action=decision.action,
            outcome=(
                Outcome.SCHEDULED
                if allowed
                else (Outcome.SKIPPED if decision.allowed else decision.outcome)
            ),
            reason_code=schedule.classification.decline_code,
            authorising_rule=decision.rule_id,
            rationale=(
                f"{draft.reasoning} Policy: {decision.rationale}"
                + overruled_note
                + (
                    " Draft rejected on tone and replaced by the templated message: "
                    + "; ".join(v.detail for v in violations)
                    if violations
                    else ""
                )
            ),
            provenance=provenance,
            model_confidence=confidence,
            amount_at_risk_paise=self._book_at_risk(schedule),
            currency=mandate.currency,
            attempts_remaining=decision.attempts_remaining,
            timestamp=schedule.explanation.scheduled_for,
            metadata={
                "kind": schedule.explanation.step.value,
                "channel": draft.channel.value,
                "call_to_action": draft.call_to_action.value,
                "recommended_action": draft.recommended_action.value,
                "decline_class": schedule.classification.decline_class,
                "failure_route": schedule.classification.route.value,
                "tone_violations": [v.model_dump() for v in violations],
                "overridden_recommendation": overruled,
                "degraded": result.degraded,
                "degradation_reason": result.degradation_reason,
                "simulated_delivery": True,
                "status": status,
                "deliverable_from": _iso(deliver_at),
                **decision.override_metadata(),
            },
        )

        communication = self._persist_communication(
            mandate,
            schedule,
            kind=schedule.explanation.step.value,
            channel=draft.channel,
            subject=draft.subject,
            body=draft.body,
            call_to_action=draft.call_to_action.value,
            status=status,
            scheduled_for=deliver_at,
            authorising_rule=TEMPLATE_RULE if violations else decision.rule_id,
            # Only a refusal holds a message. A permitted decision to do *less*
            # than send one is a deliberate skip, and naming a rule as having
            # "held" it would misread the trail.
            held_rule=None if decision.allowed else decision.rule_id,
            rationale=f"[audit entry {entry.id}] {decision.rationale}",
            provenance=provenance,
            model_reasoning=draft.reasoning,
            model_confidence=confidence,
            recommended_action=draft.recommended_action.value,
            overridden_recommendation=overruled or decision.overridden_recommendation,
            tone_violations=[v.model_dump() for v in violations],
        )
        return MandateOutcome(
            schedule=schedule,
            decision_entry_id=int(entry.id),
            communication=communication,
            reasoning=result,
            tone_violations=violations,
        )

    def _dunning_context(self, schedule: ScheduleDecision) -> dict[str, Any]:
        mandate = schedule.mandate
        classification = schedule.classification
        spec = classify(classification.decline_code)
        last = mandate.latest_failure
        return build_context(
            mandate_id=mandate.mandate_id,
            customer_name=mandate.customer_name,
            amount_paise=mandate.amount_paise,
            frequency=mandate.frequency,
            decline_code=classification.decline_code,
            decline_class=classification.decline_class,
            taxonomy_action=spec.default_action,
            route=classification.route,
            attempts_used=mandate.attempts_in_current_cycle,
            attempts_allowed=schedule.explanation.attempts_used
            + (schedule.explanation.attempts_remaining or 0),
            last_attempt_at=(
                f"{last.attempted_at:%Y-%m-%d %H:%M} UTC" if last else "no attempt this cycle"
            ),
            notice_status=schedule.notice.status.value,
            afa_side=("above" if afa_side(mandate) == "above" else "at or below"),
            communication_reason=schedule.decision.rationale,
            required_action=classification.rationale,
        )

    # --- persistence -------------------------------------------------------

    def _persist_communication(
        self,
        mandate: Mandate,
        schedule: ScheduleDecision,
        *,
        kind: str,
        channel: Channel,
        subject: str,
        body: str,
        call_to_action: str,
        status: str,
        scheduled_for: datetime | None,
        authorising_rule: str,
        held_rule: str | None,
        rationale: str,
        provenance: Provenance,
        model_reasoning: str | None = None,
        model_confidence: float | None = None,
        recommended_action: str | None = None,
        overridden_recommendation: str | None = None,
        tone_violations: list[dict[str, Any]] | None = None,
    ) -> MandateCommunication:
        row = MandateCommunication(
            batch_id=self._batch_id,
            mandate_id=mandate.mandate_id,
            kind=kind,
            channel=channel.value,
            subject=subject,
            body=body,
            call_to_action=call_to_action,
            decline_code=schedule.classification.decline_code,
            failure_route=schedule.classification.route.value,
            status=status,
            scheduled_for=scheduled_for,
            authorising_rule=authorising_rule,
            held_rule=held_rule,
            rationale=rationale,
            model_reasoning=model_reasoning,
            model_confidence=model_confidence,
            provenance=provenance.model_dump(mode="json"),
            recommended_action=recommended_action,
            overridden_recommendation=overridden_recommendation,
            tone_violations=tone_violations or None,
        )
        # The same session the audit trail commits on, so the message and the
        # entry authorising it land together.
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def persist_state(self, outcome: MandateOutcome) -> MandateRecoveryState:
        """Snapshot where this mandate stands, for the API and the dashboard.

        Not a metric source. Every reported figure is recomputed from the audit
        trail, so a stale row here can never become a number anyone quotes.
        """
        schedule = outcome.schedule
        mandate = schedule.mandate
        explanation = schedule.explanation
        row = MandateRecoveryState(
            batch_id=self._batch_id,
            mandate_id=mandate.mandate_id,
            customer_id=mandate.customer_id,
            customer_name=mandate.customer_name,
            amount_paise=mandate.amount_paise,
            currency=mandate.currency,
            frequency=mandate.frequency,
            mandate_status=mandate.status,
            mandate_cap_paise=mandate.mandate_cap_paise,
            afa_registered=mandate.afa_registered,
            afa_side=afa_side(mandate),
            attempts_in_cycle=mandate.attempts_in_current_cycle,
            next_debit_at=mandate.next_debit_at,
            next_debit_notice_sent_at=mandate.next_debit_notice_sent_at,
            decline_code=schedule.classification.decline_code,
            decline_class=schedule.classification.decline_class,
            failure_route=schedule.classification.route.value,
            classification_rule=schedule.classification.rule_id,
            notice_status=schedule.notice.status.value,
            notice_lead_hours=schedule.notice.lead_hours,
            next_step=explanation.step.value,
            scheduled_for=explanation.scheduled_for,
            authorising_rule=explanation.rule_id,
            reason_code=explanation.reason_code,
            explanation=explanation.explanation,
            attempts_remaining=explanation.attempts_remaining,
            terminal=explanation.terminal,
            compliance_blocked=explanation.compliance_blocked,
            debit_attempted=outcome.debit_attempted,
            amount_recovered_paise=outcome.recovered_paise,
            retry_probability=outcome.retry_probability,
        )
        self._db.merge(row)
        self._db.commit()
        return row

    # --- helpers -----------------------------------------------------------

    def _messages_sent(self, mandate_id: str) -> int:
        """Customer messages already recorded for this mandate in this run.

        Counted from the trail rather than a local tally, for the same reason the
        summary is: a counter and the log it claims to describe can drift, and
        `policy-bounds:QH2` is a bound the trail has to be able to prove.
        """
        return sum(
            1
            for entry in self._trail.query(
                batch_id=self._batch_id, entity_id=mandate_id, limit=1000
            )
            if entry.action
            in {Action.SEND_DUNNING.value, Action.SEND_PRE_DEBIT_NOTICE.value}
            and entry.outcome in {Outcome.SCHEDULED.value, Outcome.SUCCESS.value}
        )


def _draft_from(result: ReasoningResult, context: dict[str, Any]) -> DunningDraft:
    """Pull the draft out of a reasoning result, failing closed to the template.

    The wrapper's contract does not produce a result with no output, but a
    mis-registered fallback could, and a message is not a place to improvise.
    """
    output = result.output
    if isinstance(output, DunningDraft):
        return output
    return render_template(context)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


#: Steps that produce a drafted customer message. Escalations deliberately do
#: not: a human decides what that customer hears, and drafting one anyway would
#: be the system acting past the point at which it handed over.
MESSAGE_STEPS: frozenset[NextStep] = frozenset(
    {NextStep.SEND_DUNNING, NextStep.REQUEST_AUTHENTICATION}
)

__all__ = [
    "MESSAGE_STEPS",
    "NOTICE_RULE",
    "STATUS_HELD",
    "STATUS_MARKED",
    "STATUS_SUPPRESSED",
    "MandateOutcome",
    "MandateRecoveryService",
    "afa_side",
]
