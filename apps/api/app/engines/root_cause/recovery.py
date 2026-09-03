"""Bounded recovery actions, every one of them authorised before it happens.

Four actions exist and no others: **reroute**, **schedule retry**, **suppress**,
**escalate**. Each one goes through `policy_engine.py` first, without exception,
and each one produces an audit entry naming the rule that authorised — or
refused — it.

The two things worth reading closely:

- `authorise_corridor_action()` calls `PolicyEngine.authorize()`, never
  `evaluate()` followed by acting on the model's word. That is the difference
  between a gate and a suggestion, and the tell in the trail when someone gets it
  wrong is an entry citing no policy rule.
- `suppress` is a **real action**. A hard decline that never receives a retry is
  a wasted attempt avoided, and it is recorded as deliberately as a retry that
  was sent. Counting suppressions is only honest if they exist in the trail.

Recovery executes against the deterministic simulation path from Phase 1, or a
Razorpay test-mode call — never live traffic. Which one is used is written into
every entry's metadata by `razorpay_client`, so a reader can always tell.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Any

from sqlalchemy.orm import Session

from app.engines.root_cause.config import RecoveryConfig
from app.engines.root_cause.schemas import (
    ACTION_BY_RECOMMENDATION,
    Corridor,
    CorridorDiagnosis,
    Detection,
    PaymentAttempt,
)
from app.models.entity import RecoverableEntity
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.provenance import Provenance
from app.models.root_cause import CorridorReroute
from app.services.audit_trail import AuditTrail
from app.services.decline_taxonomy import DeclineCode, classify
from app.services.llm_agent import ReasoningResult
from app.services.policy_engine import (
    LLMRecommendation,
    PolicyDecision,
    PolicyEngine,
    PolicyRequest,
)
from app.services.razorpay_client import RazorpayClient

#: Every payment recovery decision this engine takes is an attempt on a payment
#: that has already been tried once. Nothing here re-derives that from the data.
ORIGINAL_ATTEMPT_COUNT = 1


@dataclass(frozen=True)
class CorridorOutcome:
    """What the corridor-level loop decided, and what it did about it."""

    detection: Detection
    diagnosis: CorridorDiagnosis
    reasoning: ReasoningResult
    decision: PolicyDecision
    audit_entry_id: int
    reroute: CorridorReroute | None = None


@dataclass(frozen=True)
class PaymentOutcome:
    """What happened to one failed payment."""

    attempt: PaymentAttempt
    decision: PolicyDecision
    audit_entry_id: int
    retried: bool
    recovered_paise: int
    retry_probability: float | None = None


def corridor_entity(detection: Detection) -> RecoverableEntity:
    """The corridor as something the policy engine can reason about.

    `amount_paise` carries the value at risk, which is what
    `corridor-detection:ES1` compares against — a corridor action is a bulk
    action, and its size is the money it moves, not a count of rows.
    """
    return RecoverableEntity(
        entity_id=detection.corridor.key,
        entity_type=EntityType.CORRIDOR,
        amount_paise=detection.value_at_risk_paise,
        status="degraded",
        attempt_count=0,
    )


def payment_entity(attempt: PaymentAttempt) -> RecoverableEntity:
    """The failed payment as a recoverable entity.

    `attempt_count=1` because the original attempt is itself an attempt against
    the budget — `decline-taxonomy` is explicit that a budget counts the original.
    """
    return RecoverableEntity(
        entity_id=attempt.attempt_id,
        entity_type=EntityType.PAYMENT,
        amount_paise=attempt.amount_paise,
        currency=attempt.currency,
        status=attempt.status,
        attempt_count=ORIGINAL_ATTEMPT_COUNT,
        last_attempt_at=attempt.created_at,
    )


class RecoveryService:
    """Executes bounded recovery, and records every refusal as carefully as every act."""

    def __init__(
        self,
        *,
        db: Session,
        trail: AuditTrail,
        policy: PolicyEngine,
        razorpay: RazorpayClient,
        config: RecoveryConfig,
        batch_id: str,
    ) -> None:
        self._db = db
        self._trail = trail
        self._policy = policy
        self._razorpay = razorpay
        self._config = config
        self._batch_id = batch_id

    # --- corridor level ----------------------------------------------------

    def authorise_corridor_action(
        self,
        detection: Detection,
        diagnosis: CorridorDiagnosis,
        *,
        now: datetime,
        alternate_route_available: bool,
        provenance: Provenance,
    ) -> PolicyDecision:
        """Put the model's recommendation in front of the gate.

        `authorize()` rather than `evaluate()`: the envelope is computed before
        the recommendation is looked at, a denial is returned as a denial with
        the recommendation recorded on it, and there is no path by which the
        model's answer becomes the permission.
        """
        request = PolicyRequest(
            engine=Engine.ROOT_CAUSE,
            entity=corridor_entity(detection),
            proposed_action=Action.REROUTE_TRAFFIC,
            now=now,
            corridor_determination=diagnosis.determination,
            alternate_route_available=alternate_route_available,
            abstained=diagnosis.determination.value == "insufficient_evidence",
        )
        recommendation = LLMRecommendation(
            action=ACTION_BY_RECOMMENDATION[diagnosis.recommended_action],
            rationale=diagnosis.reasoning,
            confidence=diagnosis.confidence,
            provenance=provenance,
        )
        return self._policy.authorize(request, recommendation)

    def record_corridor_decision(
        self,
        detection: Detection,
        diagnosis: CorridorDiagnosis,
        decision: PolicyDecision,
        result: ReasoningResult,
    ) -> int:
        """Write the corridor decision, with the model's reasoning verbatim."""
        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.ROOT_CAUSE,
            entity_type=EntityType.CORRIDOR,
            entity_id=detection.corridor.key,
            action=decision.action,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            authorising_rule=decision.rule_id,
            rationale=(
                f"{detection.corridor.label}: observed "
                f"{detection.observed_success_rate:.1%} against a baseline of "
                f"{detection.baseline_success_rate:.1%} over "
                f"{detection.window_attempts} attempts. Diagnosis "
                f"({diagnosis.determination.value}, {diagnosis.hypothesis.value}): "
                f"{diagnosis.reasoning} Policy: {decision.rationale}"
            ),
            provenance=result.provenance,
            model_confidence=result.confidence,
            # Zero, deliberately. The money at risk on this corridor is the same
            # money the per-payment entries already carry; counting it here as
            # well would inflate the batch denominator and understate the
            # recovery rate against a total nobody could reconcile. The figure
            # lives in metadata, where it describes the corridor without being
            # summed twice.
            amount_at_risk_paise=0,
            attempts_remaining=decision.attempts_remaining,
            metadata={
                "value_at_risk_paise": detection.value_at_risk_paise,
                "detection_id": detection.detection_id,
                "corridor_level": detection.corridor.level,
                "statistical_confidence": detection.confidence,
                "p_value": detection.p_value,
                "detection_rules": detection.cited_rules,
                "hypothesis": diagnosis.hypothesis.value,
                "determination": diagnosis.determination.value,
                "recommended_action": diagnosis.recommended_action.value,
                "proposed_action": Action.REROUTE_TRAFFIC.value,
                **decision.override_metadata(),
                "degraded": result.degraded,
                "degradation_reason": result.degradation_reason,
                "decline_mix": detection.decline_mix,
            },
        )
        return int(entry.id)

    def open_reroute(
        self,
        detection: Detection,
        *,
        to_route_id: str,
        now: datetime,
        authorising_rule: str,
    ) -> CorridorReroute:
        """Create the bounded directive. Expiry is set here and is not optional.

        The duration is `corridor-detection:RR1`'s, clamped. There is no argument
        for a caller to pass a longer one, deliberately: a bound adjustable at the
        call site is not a bound.
        """
        duration = self._config.bounded_reroute_duration()
        reroute = CorridorReroute(
            batch_id=self._batch_id,
            detection_id=detection.detection_id,
            corridor_key=detection.corridor.key,
            method=detection.corridor.method or "unknown",
            from_route_id=detection.corridor.route_id,
            to_route_id=to_route_id,
            effective_from=now,
            expires_at=now + duration,
            authorising_rule=authorising_rule,
            expiry_rule=self._config.rule,
            value_at_risk_paise=detection.value_at_risk_paise,
        )
        # The same session the audit trail writes through, so the directive and
        # the entry that authorised it commit or roll back together.
        self._db.add(reroute)
        self._db.commit()
        self._db.refresh(reroute)
        return reroute

    def active_reroute_for(
        self, corridor: Corridor, reroutes: list[CorridorReroute], now: datetime
    ) -> CorridorReroute | None:
        """The reroute in force for this corridor at `now`, if any.

        Expiry is checked on every read, so a lapsed directive can never be
        applied by a code path that forgot to sweep.
        """
        for reroute in reroutes:
            if reroute.corridor_key == corridor.key and reroute.is_active(now):
                return reroute
        return None

    # --- payment level -----------------------------------------------------

    def recover_payment(
        self,
        attempt: PaymentAttempt,
        *,
        now: datetime,
        retry_model: Any,
        rng: Random,
        rerouted_to: str | None = None,
    ) -> PaymentOutcome:
        """Decide, and act, on one failed payment.

        The decision is always the policy engine's. A hard decline reaches the
        attempt cap with nothing left and is refused; that refusal *is* the
        suppression, and it is recorded with the rule that produced it rather
        than as a silent no-op.
        """
        code = _decline_code(attempt.failure_reason_code)
        request = PolicyRequest(
            engine=Engine.ROOT_CAUSE,
            entity=payment_entity(attempt),
            proposed_action=Action.SCHEDULE_RETRY,
            now=now,
            decline_code=code,
        )
        decision = self._policy.evaluate(request)

        base_metadata: dict[str, Any] = {
            "proposed_action": Action.SCHEDULE_RETRY.value,
            "decline_code": code.value,
            "decline_class": classify(code).decline_class.value,
            "corridor": f"{attempt.issuer}:{attempt.method}:{attempt.route_id}",
            "rerouted_to": rerouted_to,
        }

        if not decision.allowed:
            entry = self._trail.record(
                batch_id=self._batch_id,
                engine=Engine.ROOT_CAUSE,
                entity_type=EntityType.PAYMENT,
                entity_id=attempt.attempt_id,
                action=decision.action,
                outcome=decision.outcome,
                reason_code=decision.reason_code,
                authorising_rule=decision.rule_id,
                rationale=decision.rationale,
                provenance=Provenance.deterministic(),
                amount_at_risk_paise=attempt.amount_paise,
                attempt_number=ORIGINAL_ATTEMPT_COUNT,
                attempts_remaining=decision.attempts_remaining,
                metadata={
                    **base_metadata,
                    **decision.override_metadata(),
                    # A refusal is either permanent (the instrument is dead, the
                    # budget is gone) or a matter of timing. Conflating the two
                    # would let a cooldown block be reported as a wasted attempt
                    # avoided, which it is not.
                    "suppressed_retry": decision.earliest_next_attempt_at is None,
                    "deferred_retry": decision.earliest_next_attempt_at is not None,
                },
            )
            return PaymentOutcome(
                attempt=attempt,
                decision=decision,
                audit_entry_id=int(entry.id),
                retried=False,
                recovered_paise=0,
            )

        hours_since = (now - attempt.created_at).total_seconds() / 3600
        outcome = retry_model.sample(
            rng,
            code.value,
            hours_since_previous=hours_since,
            attempt_number=ORIGINAL_ATTEMPT_COUNT + 1,
        )

        entry = self._trail.record(
            batch_id=self._batch_id,
            engine=Engine.ROOT_CAUSE,
            entity_type=EntityType.PAYMENT,
            entity_id=attempt.attempt_id,
            action=Action.SCHEDULE_RETRY,
            outcome=Outcome.PENDING,
            reason_code=decision.reason_code,
            authorising_rule=decision.rule_id,
            rationale=(
                f"{decision.rationale} Retry drawn against the documented "
                f"retry-success model at p={outcome.probability:.3f} "
                f"({outcome.model_version}), {hours_since:.1f}h after the original "
                "attempt."
            ),
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=attempt.amount_paise,
            attempt_number=ORIGINAL_ATTEMPT_COUNT + 1,
            attempts_remaining=decision.attempts_remaining,
            metadata={
                **base_metadata,
                "retry_probability": outcome.probability,
                "retry_model_version": outcome.model_version,
            },
        )

        recovered = self._execute_retry(attempt, outcome)
        self._trail.resolve_outcome(
            int(entry.id),
            outcome=Outcome.SUCCESS if outcome.succeeded else Outcome.FAILURE,
            amount_recovered_paise=recovered,
        )
        return PaymentOutcome(
            attempt=attempt,
            decision=decision,
            audit_entry_id=int(entry.id),
            retried=True,
            recovered_paise=recovered,
            retry_probability=outcome.probability,
        )

    def _execute_retry(self, attempt: PaymentAttempt, outcome: Any) -> int:
        """Put the retry through the Razorpay client and audit the call.

        A failed retry is only replayed through the client when the decline has a
        doc-verified upstream reason. Where it does not — most of the taxonomy —
        round-tripping it would normalize the failure to `UNKNOWN` and mislabel
        it, so the failure is recorded from the code we already hold. Phase 2's
        `simulate_failure_key` is `None` in exactly those cases, and `None` means
        "do not call the client expecting a failure", never "call it and hope".
        """
        notes: dict[str, Any] = {
            "batch_id": self._batch_id,
            "attempt_id": attempt.attempt_id,
            "engine": Engine.ROOT_CAUSE.value,
        }
        if not outcome.succeeded:
            if outcome.simulate_failure_key is None:
                return 0
            notes["simulate_failure"] = outcome.simulate_failure_key

        result = self._razorpay.create_order(
            amount_paise=attempt.amount_paise,
            receipt=f"retry-{attempt.attempt_id}",
            notes=notes,
            currency=attempt.currency,
        )
        # The call is audited with no money attached: the `schedule_retry` entry
        # already carries this payment's amount at risk, and adding it again here
        # would double-count the same rupees in the batch summary.
        self._razorpay.record_call(
            result,
            batch_id=self._batch_id,
            engine=Engine.ROOT_CAUSE,
            entity_type=EntityType.PAYMENT,
            entity_id=attempt.attempt_id,
        )
        return attempt.amount_paise if result.ok and outcome.succeeded else 0


def _decline_code(raw: str | None) -> DeclineCode:
    """The record's failure code as a taxonomy code, failing closed.

    Anything unrecognised becomes `UNKNOWN`, which carries a budget of 1 and no
    retryability. Defaulting anywhere else would mean defaulting to "keep
    charging this customer".
    """
    if not raw:
        return DeclineCode.UNKNOWN
    try:
        return DeclineCode(raw)
    except ValueError:
        return DeclineCode.UNKNOWN


def reroute_expiry(now: datetime, config: RecoveryConfig) -> datetime:
    """When a reroute opened at `now` lapses. Exposed for tests and the API."""
    return now + config.bounded_reroute_duration()


def clamped_duration_hours(config: RecoveryConfig) -> float:
    return config.bounded_reroute_duration() / timedelta(hours=1)
