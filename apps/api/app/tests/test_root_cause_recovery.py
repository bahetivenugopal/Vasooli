"""Recovery: what the engine is allowed to do, and what it is refused.

The first test in this file is the most important one in the phase. If a model
recommendation ever authorises an action the rules forbid, the product's central
claim — bounded, gated, provable — is false, and everything else here is
decoration.

The rest defend the specific bounds Engine 1 adds: a reroute needs a systemic
determination, needs somewhere to go, and always expires.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.engines.root_cause.config import RecoveryConfig, default_config
from app.engines.root_cause.recovery import (
    RecoveryService,
    clamped_duration_hours,
    corridor_entity,
    payment_entity,
)
from app.engines.root_cause.schemas import (
    ACTION_BY_RECOMMENDATION,
    Corridor,
    CorridorDiagnosis,
    Detection,
    PaymentAttempt,
    RecommendedAction,
    RootCauseHypothesis,
)
from app.models.enums import (
    Action,
    CorridorDetermination,
    Engine,
    EntityType,
    Outcome,
)
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail
from app.services.decline_taxonomy import DeclineCode
from app.services.llm_agent import ReasoningResult
from app.services.policy_engine import (
    THRESHOLDS,
    LLMRecommendation,
    PolicyEngine,
    PolicyRequest,
)
from app.services.razorpay_client import RazorpayClient, RazorpayMode

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
BATCH = "rc-test-batch"


@pytest.fixture
def recovery(db: Session, trail: AuditTrail) -> RecoveryService:
    trail.start_batch(batch_id=BATCH, engine=Engine.ROOT_CAUSE, seed=42)
    return RecoveryService(
        db=db,
        trail=trail,
        policy=PolicyEngine(),
        razorpay=RazorpayClient(audit=trail, mode=RazorpayMode.SIMULATED),
        config=default_config().recovery,
        batch_id=BATCH,
    )


def make_detection(
    *,
    value_at_risk_paise: int = 500_000,
    route_id: str | None = None,
    decline_mix: dict[str, int] | None = None,
) -> Detection:
    return Detection(
        detection_id="det_recovery",
        corridor=Corridor(
            level="issuer_method", issuer="HDFC", method="upi", route_id=route_id
        ),
        window_start=NOW - timedelta(hours=6),
        window_end=NOW,
        window_attempts=20,
        window_successes=6,
        baseline_attempts=60,
        baseline_successes=57,
        observed_success_rate=0.3,
        baseline_success_rate=0.95,
        p_value=0.0001,
        confidence=0.9999,
        affected_attempts=14,
        value_at_risk_paise=value_at_risk_paise,
        decline_mix=decline_mix or {"ISSUER_UNAVAILABLE": 14},
        distinct_failing_customers=14,
        cited_rules=["corridor-detection:BW1"],
    )


def make_diagnosis(
    determination: CorridorDetermination,
    recommended: RecommendedAction,
    *,
    confidence: float = 0.9,
) -> CorridorDiagnosis:
    return CorridorDiagnosis(
        hypothesis=RootCauseHypothesis.ISSUER_OUTAGE,
        determination=determination,
        recommended_action=recommended,
        confidence=confidence,
        reasoning="Rail-side declines concentrated on one issuer.",
    )


def _model_provenance() -> Provenance:
    return Provenance.from_model(
        provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
    )


def _result(diagnosis: CorridorDiagnosis) -> ReasoningResult:
    return ReasoningResult(
        task="diagnose_corridor",
        output=diagnosis,
        provenance=_model_provenance(),
        confidence=diagnosis.confidence,
    )


# ---------------------------------------------------------------------------
# Policy supremacy — the most important test in this phase
# ---------------------------------------------------------------------------


def test_a_model_recommendation_the_rules_forbid_is_denied_and_audited(recovery, trail):
    """The model says reroute. The rules say the evidence does not support it.

    `individual` means many customers each had their own problem. Rerouting on
    that changes routing for everybody to fix nobody, so `corridor-detection:RR2`
    refuses — and the denial is written to the trail naming that rule, with the
    recommendation recorded beside it.
    """
    detection = make_detection()
    diagnosis = make_diagnosis(
        CorridorDetermination.INDIVIDUAL, RecommendedAction.REROUTE_TRAFFIC
    )

    decision = recovery.authorise_corridor_action(
        detection,
        diagnosis,
        now=NOW,
        alternate_route_available=True,
        provenance=_model_provenance(),
    )

    assert not decision.allowed, "a recommendation widened a policy bound"
    assert decision.rule_id == "corridor-detection:RR2"
    assert decision.overridden_recommendation == Action.REROUTE_TRAFFIC.value
    assert decision.authority_rule_id == "policy_engine:llm_authority.denied"

    entry_id = recovery.record_corridor_decision(
        detection, diagnosis, decision, _result(diagnosis)
    )
    entry = trail.get(entry_id)
    assert entry is not None
    assert entry.authorising_rule == "corridor-detection:RR2"
    assert entry.outcome == Outcome.BLOCKED.value
    assert entry.entry_metadata["overridden_recommendation"] == Action.REROUTE_TRAFFIC.value
    assert (
        entry.entry_metadata["authority_rule"] == "policy_engine:llm_authority.denied"
    )


def test_no_recommendation_can_reroute_a_corridor_that_is_not_systemic(recovery):
    """Every recommendation, against every non-systemic determination."""
    detection = make_detection()
    for determination in (
        CorridorDetermination.INDIVIDUAL,
        CorridorDetermination.INSUFFICIENT_EVIDENCE,
    ):
        for recommended in RecommendedAction:
            decision = recovery.authorise_corridor_action(
                detection,
                make_diagnosis(determination, recommended),
                now=NOW,
                alternate_route_available=True,
                provenance=_model_provenance(),
            )
            assert not (
                decision.allowed and decision.action is Action.REROUTE_TRAFFIC
            ), f"{determination.value}/{recommended.value} authorised a reroute"


def test_a_corridor_retry_recommendation_is_outside_the_envelope(recovery):
    """A retry is authorised per payment, never wholesale across a corridor."""
    decision = recovery.authorise_corridor_action(
        make_detection(),
        make_diagnosis(CorridorDetermination.SYSTEMIC, RecommendedAction.SCHEDULE_RETRY),
        now=NOW,
        alternate_route_available=True,
        provenance=_model_provenance(),
    )
    assert not decision.allowed
    assert decision.rule_id == "policy_engine:llm_authority.out_of_envelope"
    assert decision.overridden_recommendation == Action.SCHEDULE_RETRY.value


def test_a_systemic_diagnosis_with_a_route_to_use_is_permitted(recovery):
    decision = recovery.authorise_corridor_action(
        make_detection(),
        make_diagnosis(CorridorDetermination.SYSTEMIC, RecommendedAction.REROUTE_TRAFFIC),
        now=NOW,
        alternate_route_available=True,
        provenance=_model_provenance(),
    )
    assert decision.allowed
    assert decision.action is Action.REROUTE_TRAFFIC
    assert decision.rule_id == "corridor-detection:RR2"


def test_a_reroute_with_nowhere_to_go_fails_closed(recovery):
    """`corridor-detection:RR3`. An action that cannot happen is not recorded as one."""
    decision = recovery.authorise_corridor_action(
        make_detection(),
        make_diagnosis(CorridorDetermination.SYSTEMIC, RecommendedAction.REROUTE_TRAFFIC),
        now=NOW,
        alternate_route_available=False,
        provenance=_model_provenance(),
    )
    assert not decision.allowed
    assert decision.rule_id == "corridor-detection:RR3"
    assert decision.requires_human


def test_a_corridor_above_the_value_ceiling_escalates(recovery):
    """`corridor-detection:ES1`. A bulk action at this size gets a human first."""
    ceiling = THRESHOLDS["corridor_autonomous_action"].value_paise
    decision = recovery.authorise_corridor_action(
        make_detection(value_at_risk_paise=ceiling + 1),
        make_diagnosis(CorridorDetermination.SYSTEMIC, RecommendedAction.REROUTE_TRAFFIC),
        now=NOW,
        alternate_route_available=True,
        provenance=_model_provenance(),
    )
    assert not decision.allowed
    assert decision.rule_id == "corridor-detection:ES1"
    assert decision.action is Action.ESCALATE
    assert decision.outcome is Outcome.ESCALATED


def test_an_abstaining_diagnosis_escalates_to_a_human(recovery):
    """`policy-bounds:HE1`. Abstention is only a success if something picks it up."""
    decision = recovery.authorise_corridor_action(
        make_detection(),
        make_diagnosis(
            CorridorDetermination.INSUFFICIENT_EVIDENCE,
            RecommendedAction.ESCALATE,
            confidence=0.0,
        ),
        now=NOW,
        alternate_route_available=True,
        provenance=_model_provenance(),
    )
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:HE1"
    assert decision.requires_human


# ---------------------------------------------------------------------------
# Reroute expiry
# ---------------------------------------------------------------------------


def test_a_reroute_expires_and_cannot_persist_indefinitely(recovery, db):
    """`corridor-detection:RR1`. Expiry is what keeps a recovery action bounded."""
    detection = make_detection()
    reroute = recovery.open_reroute(
        detection,
        to_route_id="rt_upi_npci_b",
        now=NOW,
        authorising_rule="corridor-detection:RR2",
    )

    assert reroute.expires_at > reroute.effective_from
    assert reroute.is_active(NOW)
    assert reroute.is_active(reroute.expires_at - timedelta(seconds=1))
    assert not reroute.is_active(reroute.expires_at)
    assert not reroute.is_active(reroute.expires_at + timedelta(days=365))
    assert reroute.expiry_rule == default_config().recovery.rule

    corridor = detection.corridor
    assert recovery.active_reroute_for(corridor, [reroute], NOW) is reroute
    assert recovery.active_reroute_for(corridor, [reroute], reroute.expires_at) is None


def test_a_configured_duration_above_the_maximum_is_clamped():
    """A bound adjustable at a call site is not a bound."""
    greedy = RecoveryConfig(
        reroute_duration=timedelta(days=30),
        max_reroute_duration=timedelta(hours=6),
        rule="corridor-detection:RR1",
    )
    assert greedy.bounded_reroute_duration() == timedelta(hours=6)
    assert clamped_duration_hours(greedy) == 6.0


# ---------------------------------------------------------------------------
# Per-payment recovery: suppression and the hard-decline rule
# ---------------------------------------------------------------------------


def make_attempt(
    code: str | None,
    *,
    amount_paise: int = 250_000,
    hours_ago: float = 30.0,
    attempt_id: str = "att_0001",
) -> PaymentAttempt:
    return PaymentAttempt(
        attempt_id=attempt_id,
        batch_id="pay-test",
        customer_id="cust_0001",
        created_at=NOW - timedelta(hours=hours_ago),
        amount_paise=amount_paise,
        status="failed",
        method="card",
        issuer="HDFC",
        route_id="rt_card_acq_1",
        failure_reason_code=code,
    )


class _AlwaysSucceeds:
    """A retry model that always says yes, so a denial cannot be luck."""

    model_version = "test-1"

    class _Outcome:
        succeeded = True
        probability = 1.0
        model_version = "test-1"
        failure_code = None
        simulate_failure_key = None

    def sample(self, *_args, **_kwargs):
        return self._Outcome()


@pytest.mark.parametrize(
    "code",
    [
        DeclineCode.CARD_EXPIRED.value,
        DeclineCode.ACCOUNT_CLOSED.value,
        DeclineCode.INVALID_ACCOUNT.value,
        DeclineCode.CARD_DISABLED_ONLINE.value,
        DeclineCode.FRAUD_SUSPECTED.value,
        DeclineCode.CARD_LOST_OR_STOLEN.value,
    ],
)
def test_a_hard_decline_never_receives_a_retry_authorisation(recovery, trail, code):
    """Retrying a dead instrument burns an attempt and trips fraud filters.

    The refusal *is* the suppression: it is recorded as an action with the rule
    that produced it, not skipped silently, because a suppression nobody can find
    in the trail is a suppression nobody can count.
    """
    import random

    outcome = recovery.recover_payment(
        make_attempt(code, attempt_id=f"att_{code}"),
        now=NOW,
        retry_model=_AlwaysSucceeds(),
        rng=random.Random(0),
    )
    assert not outcome.retried
    assert not outcome.decision.allowed
    assert outcome.recovered_paise == 0

    entry = trail.get(outcome.audit_entry_id)
    assert entry is not None
    assert entry.action != Action.SCHEDULE_RETRY.value
    assert entry.authorising_rule
    assert entry.entry_metadata["suppressed_retry"] is True
    assert entry.entry_metadata["deferred_retry"] is False


def test_an_unrecognised_decline_fails_closed_to_suppression(recovery, trail):
    """Unknown never defaults to soft — that would mean "keep charging"."""
    import random

    outcome = recovery.recover_payment(
        make_attempt("bank_reference_mismatch", attempt_id="att_unknown"),
        now=NOW,
        retry_model=_AlwaysSucceeds(),
        rng=random.Random(0),
    )
    assert not outcome.retried
    entry = trail.get(outcome.audit_entry_id)
    assert entry.reason_code in {"ATTEMPT_BUDGET_EXHAUSTED", DeclineCode.UNKNOWN.value}
    assert entry.entry_metadata["decline_class"] == "UNKNOWN"


def test_a_soft_decline_past_its_cooldown_is_retried_and_resolved(recovery, trail):
    import random

    outcome = recovery.recover_payment(
        make_attempt(DeclineCode.INSUFFICIENT_FUNDS.value, attempt_id="att_soft"),
        now=NOW,
        retry_model=_AlwaysSucceeds(),
        rng=random.Random(0),
    )
    assert outcome.retried
    assert outcome.recovered_paise == 250_000

    entry = trail.get(outcome.audit_entry_id)
    assert entry.action == Action.SCHEDULE_RETRY.value
    assert entry.outcome == Outcome.SUCCESS.value
    assert entry.amount_recovered_paise == 250_000
    assert entry.attempt_number == 2


def test_a_soft_decline_inside_its_cooldown_is_deferred_not_suppressed(recovery, trail):
    """`policy-bounds:CD1`. The revenue is still recoverable; the moment is wrong."""
    import random

    outcome = recovery.recover_payment(
        make_attempt(
            DeclineCode.INSUFFICIENT_FUNDS.value, hours_ago=1.0, attempt_id="att_early"
        ),
        now=NOW,
        retry_model=_AlwaysSucceeds(),
        rng=random.Random(0),
    )
    assert not outcome.retried
    entry = trail.get(outcome.audit_entry_id)
    assert entry.outcome == Outcome.BLOCKED.value
    assert entry.entry_metadata["deferred_retry"] is True
    assert entry.entry_metadata["suppressed_retry"] is False


def test_the_recommendation_mapping_covers_every_recommendation():
    """A recommendation with no mapped action would be a KeyError mid-batch."""
    assert set(ACTION_BY_RECOMMENDATION) == set(RecommendedAction)


def test_entities_carry_the_right_shape_into_the_policy_engine():
    detection = make_detection(value_at_risk_paise=777)
    corridor = corridor_entity(detection)
    assert corridor.entity_type is EntityType.CORRIDOR
    assert corridor.amount_paise == 777
    assert corridor.attempt_count == 0

    payment = payment_entity(make_attempt(DeclineCode.INSUFFICIENT_FUNDS.value))
    assert payment.entity_type is EntityType.PAYMENT
    assert payment.attempt_count == 1, "the original attempt counts against the budget"
    assert payment.last_attempt_at is not None


def test_the_policy_request_shape_is_what_the_engine_actually_sends():
    """A guard against the request drifting from what the rules expect."""
    request = PolicyRequest(
        engine=Engine.ROOT_CAUSE,
        entity=corridor_entity(make_detection()),
        proposed_action=Action.REROUTE_TRAFFIC,
        now=NOW,
        corridor_determination=CorridorDetermination.SYSTEMIC,
        alternate_route_available=True,
    )
    decision = PolicyEngine().authorize(
        request,
        LLMRecommendation(
            action=Action.REROUTE_TRAFFIC,
            rationale="rail down",
            confidence=0.9,
            provenance=_model_provenance(),
        ),
    )
    assert decision.allowed
    assert Action.REROUTE_TRAFFIC in decision.permitted_actions
