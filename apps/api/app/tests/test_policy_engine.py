"""Policy engine tests — the credibility backbone of the submission.

Every rule category from the phase spec is covered here, and the boundary cases
matter more than the happy paths: a cap that fires at N+1 instead of N is a
stopping rule that does not stop.

The single most important test in the repository is
`test_llm_recommendation_cannot_override_a_policy_denial`. If that one ever
fails, the product's central safety claim is false.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.entity import RecoverableEntity
from app.models.enums import Action, EntityType, Outcome
from app.models.provenance import Provenance
from app.services.decline_taxonomy import DeclineCode
from app.services.policy_engine import (
    HARD_ATTEMPT_CEILING,
    MAX_MESSAGES_PER_WINDOW,
    RULE_REGISTRY,
    THRESHOLDS,
    Engine,
    LLMRecommendation,
    PolicyConfigurationError,
    PolicyEngine,
    PolicyRequest,
    exceeds_threshold,
)
from app.tests.conftest import NOW


def make_request(entity: RecoverableEntity, **overrides) -> PolicyRequest:
    base = {
        "engine": Engine.ROOT_CAUSE,
        "entity": entity,
        "proposed_action": Action.SCHEDULE_RETRY,
        "now": NOW,
    }
    return PolicyRequest(**{**base, **overrides})


def with_entity(entity: RecoverableEntity, **overrides) -> RecoverableEntity:
    return entity.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_every_registered_rule_has_a_description_and_a_traceable_source():
    """A rule with no traceable justification should not exist."""
    for rule_id, rule in RULE_REGISTRY.items():
        assert ":" in rule_id, f"{rule_id} is not a citation"
        assert rule.description.strip(), f"{rule_id} has no description"
        assert rule.citation.strip(), f"{rule_id} has no source citation"
        assert rule.source is not None


def test_a_decision_cannot_cite_an_unregistered_rule(policy: PolicyEngine, entity):
    """The engine refuses to emit a citation it cannot back up."""
    from app.services.policy_engine import _deny

    with pytest.raises(PolicyConfigurationError):
        _deny(rule_id="made-up:rule", reason_code="X", rationale="because")


# ---------------------------------------------------------------------------
# 1. Attempt caps
# ---------------------------------------------------------------------------


def test_attempt_cap_permits_the_last_attempt_in_budget(policy: PolicyEngine, entity):
    """Boundary: at cap-1 used, one attempt remains and it is allowed."""
    request = make_request(with_entity(entity, attempt_count=HARD_ATTEMPT_CEILING - 1))
    decision = policy.evaluate(request)
    assert decision.allowed
    assert decision.attempts_remaining == 1


def test_attempt_cap_reached_exactly_denies(policy: PolicyEngine, entity):
    """Boundary: at exactly the cap, the next attempt is refused."""
    request = make_request(with_entity(entity, attempt_count=HARD_ATTEMPT_CEILING))
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.attempts_remaining == 0
    assert decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
    # policy-bounds HE2: handed to a human, not silently abandoned.
    assert decision.requires_human
    assert decision.action is Action.ESCALATE
    assert decision.outcome is Outcome.ESCALATED


def test_a_configured_cap_above_the_ceiling_is_clamped_not_honoured(policy: PolicyEngine, entity):
    """policy-bounds AC2 — an override of 50 does not buy a 5th attempt."""
    request = make_request(
        with_entity(entity, attempt_count=HARD_ATTEMPT_CEILING),
        max_attempts_override=50,
    )
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.attempts_remaining == 0


def test_a_configured_cap_below_the_ceiling_is_honoured(policy: PolicyEngine, entity):
    request = make_request(with_entity(entity, attempt_count=2), max_attempts_override=2)
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:AC2"


def test_mandate_engine_is_capped_below_the_generic_ceiling(policy: PolicyEngine, entity):
    """rbi-mandate-rules Part B — 3 attempts per cycle, not 4."""
    mandate = with_entity(entity, entity_type=EntityType.MANDATE, attempt_count=3)
    decision = policy.evaluate(make_request(mandate, engine=Engine.MANDATE_RECOVERY))
    assert not decision.allowed
    assert decision.rule_id == "rbi-mandate-rules:PartB.max_retries"


def test_decline_class_budget_can_cut_the_cap_further(policy: PolicyEngine, entity):
    """An AMBIGUOUS decline gets 2 attempts, not the generic 4."""
    request = make_request(
        with_entity(entity, attempt_count=2), decline_code=DeclineCode.DO_NOT_HONOUR
    )
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.rule_id == "decline-taxonomy:budget.AMBIGUOUS"


def test_policy_block_class_permits_no_attempt_at_all(policy: PolicyEngine, entity):
    """POLICY_BLOCK has a budget of zero — we should not have attempted."""
    request = make_request(entity, decline_code=DeclineCode.PRE_DEBIT_NOTICE_MISSING)
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.attempts_remaining == 0


# ---------------------------------------------------------------------------
# 2. Cooldown / backoff
# ---------------------------------------------------------------------------


def test_cooldown_not_yet_elapsed_denies_with_the_earliest_permitted_time(
    policy: PolicyEngine, entity
):
    last = NOW - timedelta(hours=1)
    request = make_request(with_entity(entity, attempt_count=1, last_attempt_at=last))
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.reason_code == "COOLDOWN_NOT_ELAPSED"
    assert decision.earliest_next_attempt_at == last + timedelta(hours=6)


def test_cooldown_boundary_is_inclusive_at_exactly_the_floor(policy: PolicyEngine, entity):
    """At exactly the permitted moment the attempt is allowed, not one second later."""
    last = NOW - timedelta(hours=6)
    request = make_request(with_entity(entity, attempt_count=1, last_attempt_at=last))
    assert policy.evaluate(request).allowed


def test_backoff_escalates_with_each_attempt(policy: PolicyEngine, entity):
    """policy-bounds CD2 — 6h, 12h, 24h, doubling."""
    last = NOW - timedelta(hours=6)
    request = make_request(with_entity(entity, attempt_count=2, last_attempt_at=last))
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.earliest_next_attempt_at == last + timedelta(hours=12)


def test_backoff_is_capped(policy: PolicyEngine, entity):
    """The schedule never runs past 72h between attempts."""
    from app.services.policy_engine import BASE_COOLDOWN, MAX_COOLDOWN, _backoff

    assert _backoff(99, BASE_COOLDOWN) == MAX_COOLDOWN


def test_a_first_attempt_waits_on_nothing(policy: PolicyEngine, entity):
    request = make_request(with_entity(entity, attempt_count=0, last_attempt_at=None))
    decision = policy.evaluate(request)
    assert decision.allowed
    assert decision.earliest_next_attempt_at is None


def test_mandate_spacing_is_24h_not_6h(policy: PolicyEngine, entity):
    """rbi-mandate-rules Part B — anything shorter cannot satisfy the A2 window."""
    last = NOW - timedelta(hours=8)
    mandate = with_entity(
        entity, entity_type=EntityType.MANDATE, attempt_count=1, last_attempt_at=last
    )
    decision = policy.evaluate(make_request(mandate, engine=Engine.MANDATE_RECOVERY))
    assert not decision.allowed
    assert decision.rule_id == "rbi-mandate-rules:PartB.min_spacing"
    assert decision.earliest_next_attempt_at == last + timedelta(hours=24)


# ---------------------------------------------------------------------------
# 3. Hard stops
# ---------------------------------------------------------------------------


def test_a_terminal_entity_halts_everything(policy: PolicyEngine, entity):
    terminal = with_entity(entity, is_terminal=True, terminal_reason="INVOICE_PAID")
    decision = policy.evaluate(make_request(terminal))
    assert not decision.allowed
    assert decision.terminal
    assert decision.action is Action.HALT_SCHEDULE
    assert decision.outcome is Outcome.HALTED
    assert decision.rule_id == "policy-bounds:HS1"


def test_a_revoked_mandate_cites_the_regulatory_rule(policy: PolicyEngine, entity):
    """rbi-mandate-rules A4 — absolute, no grace attempt, no exception."""
    revoked = with_entity(
        entity,
        entity_type=EntityType.MANDATE,
        is_terminal=True,
        terminal_reason=DeclineCode.MANDATE_REVOKED.value,
    )
    decision = policy.evaluate(make_request(revoked, engine=Engine.MANDATE_RECOVERY))
    assert decision.rule_id == "rbi-mandate-rules:A4"
    assert decision.terminal


def test_a_terminal_decline_code_halts_the_schedule(policy: PolicyEngine, entity):
    decision = policy.evaluate(
        make_request(entity, decline_code=DeclineCode.MANDATE_REVOKED)
    )
    assert not decision.allowed
    assert decision.terminal
    assert decision.rule_id == "rbi-mandate-rules:A4"


def test_a_hard_stop_overrides_an_otherwise_permitted_action(policy: PolicyEngine, entity):
    """The entity is fresh, inside every cap, past every cooldown — and still stops.

    This is the ordering guarantee: a hard stop short-circuits every other
    consideration rather than being weighed against them.
    """
    healthy = with_entity(entity, attempt_count=0, last_attempt_at=None)
    assert policy.evaluate(make_request(healthy)).allowed

    stopped = with_entity(healthy, is_terminal=True, terminal_reason="ACCOUNT_CLOSED")
    decision = policy.evaluate(make_request(stopped))
    assert not decision.allowed
    assert decision.terminal


def test_fraud_never_automates(policy: PolicyEngine, entity):
    """policy-bounds HE4 / decline-taxonomy — human review only."""
    decision = policy.evaluate(make_request(entity, decline_code=DeclineCode.FRAUD_SUSPECTED))
    assert not decision.allowed
    assert decision.requires_human
    assert decision.terminal


# ---------------------------------------------------------------------------
# 4. Quiet hours and communication limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc_hour", "utc_minute", "expected_allowed"),
    [
        (3, 29, False),  # 08:59 IST — one minute before the window opens
        (3, 30, True),  # 09:00 IST — the window opens
        (15, 29, True),  # 20:59 IST — the last permitted minute
        (15, 30, False),  # 21:00 IST — the window closes
        (20, 0, False),  # 01:30 IST — the middle of the night
    ],
)
def test_quiet_hours_boundaries(policy: PolicyEngine, entity, utc_hour, utc_minute, expected_allowed):
    """policy-bounds QH1 — 09:00-21:00 IST, evaluated at the exact boundaries."""
    now = datetime(2026, 9, 3, utc_hour, utc_minute, tzinfo=UTC)
    request = make_request(
        entity,
        engine=Engine.RECEIVABLES,
        proposed_action=Action.SEND_REMINDER,
        now=now,
        is_outreach=True,
    )
    assert policy.evaluate(request).allowed is expected_allowed


def test_quiet_hours_reschedules_rather_than_dropping(policy: PolicyEngine, entity):
    """The revenue is still recoverable — only the timing was wrong."""
    night = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)  # 01:30 IST
    request = make_request(
        entity,
        engine=Engine.RECEIVABLES,
        proposed_action=Action.SEND_REMINDER,
        now=night,
        is_outreach=True,
    )
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:QH1"
    assert decision.earliest_next_attempt_at is not None
    assert decision.earliest_next_attempt_at > night


def test_quiet_hours_do_not_gate_silent_actions(policy: PolicyEngine, entity):
    """A server-side retry is not outreach — nobody is woken up by it."""
    night = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
    request = make_request(entity, now=night, is_outreach=False)
    assert policy.evaluate(request).allowed


def test_contact_cap_blocks_the_fourth_message(policy: PolicyEngine, entity):
    """policy-bounds QH2 — chasing further would be harassment, not recovery."""
    request = make_request(
        entity,
        engine=Engine.RECEIVABLES,
        proposed_action=Action.SEND_REMINDER,
        is_outreach=True,
        messages_sent_in_window=MAX_MESSAGES_PER_WINDOW,
    )
    decision = policy.evaluate(request)
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:QH2"
    assert decision.requires_human


def test_contact_cap_permits_the_third_message(policy: PolicyEngine, entity):
    request = make_request(
        entity,
        engine=Engine.RECEIVABLES,
        proposed_action=Action.SEND_REMINDER,
        is_outreach=True,
        messages_sent_in_window=MAX_MESSAGES_PER_WINDOW - 1,
    )
    assert policy.evaluate(request).allowed


# ---------------------------------------------------------------------------
# 5. Amount thresholds
# ---------------------------------------------------------------------------


def test_thresholds_are_named_and_registered():
    assert "afa_fresh_auth" in THRESHOLDS
    assert THRESHOLDS["afa_fresh_auth"].value_paise == 1_500_000  # Rs 15,000, RBI A3
    assert THRESHOLDS["afa_fresh_auth"].rule_id == "rbi-mandate-rules:A3"


def test_an_unregistered_threshold_raises_rather_than_silently_passing():
    """A silently absent threshold is a rule that looks like it fired but didn't."""
    with pytest.raises(PolicyConfigurationError):
        exceeds_threshold("vibes", 1)


def test_threshold_comparison_is_strict():
    """Exactly at Rs 15,000 is *within* the no-fresh-AFA allowance, not above it."""
    assert not exceeds_threshold("afa_fresh_auth", 1_500_000)
    assert exceeds_threshold("afa_fresh_auth", 1_500_001)


# ---------------------------------------------------------------------------
# 6. Human escalation
# ---------------------------------------------------------------------------


def test_abstention_routes_to_a_human(policy: PolicyEngine, entity):
    """policy-bounds HE1 — abstention is only successful if someone picks it up."""
    decision = policy.evaluate(make_request(entity, abstained=True))
    assert not decision.allowed
    assert decision.requires_human
    assert decision.action is Action.ESCALATE
    assert decision.outcome is Outcome.ESCALATED
    assert decision.rule_id == "policy-bounds:HE1"


def test_high_value_hard_failures_escalate_before_the_ladder_runs(policy: PolicyEngine, entity):
    """policy-bounds HE3 — a Rs 60,000 hard decline gets a human immediately."""
    big = with_entity(entity, amount_paise=6_000_000, attempt_count=0)
    decision = policy.evaluate(make_request(big, decline_code=DeclineCode.CARD_EXPIRED))
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:HE3"
    assert decision.requires_human


def test_a_small_hard_failure_does_not_escalate_early(policy: PolicyEngine, entity):
    """The threshold is a threshold, not a blanket rule about hard declines."""
    small = with_entity(entity, amount_paise=250_000, attempt_count=0)
    decision = policy.evaluate(make_request(small, decline_code=DeclineCode.CARD_EXPIRED))
    assert decision.rule_id != "policy-bounds:HE3"


def test_an_unevaluable_precondition_fails_closed(policy: PolicyEngine, entity):
    """CLAUDE.md non-negotiable #3 — the safe direction of error is 'do less'."""
    decision = policy.evaluate(
        make_request(entity, unevaluable_precondition="pre_debit_notice_timestamp")
    )
    assert not decision.allowed
    assert decision.rule_id == "policy_engine:fail_closed"
    assert decision.requires_human


# ---------------------------------------------------------------------------
# The decision-authority boundary — the most important tests in the repo
# ---------------------------------------------------------------------------


def _recommendation(action: Action, **overrides) -> LLMRecommendation:
    base = {
        "action": action,
        "rationale": "The model believes this is recoverable.",
        "confidence": 0.95,
        "provenance": Provenance.from_model(
            provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
        ),
    }
    return LLMRecommendation(**{**base, **overrides})


def test_llm_recommendation_cannot_override_a_policy_denial(policy: PolicyEngine, entity):
    """**The single most important test in this repository.**

    A model recommending a fifth retry where the budget is four gets 'no'. If
    this ever passes a recommendation through, the product's central safety
    claim — bounded, gated, provable — is false.
    """
    exhausted = with_entity(entity, attempt_count=HARD_ATTEMPT_CEILING)
    request = make_request(exhausted)

    denial = policy.evaluate(request)
    assert not denial.allowed

    authorized = policy.authorize(request, _recommendation(Action.SCHEDULE_RETRY))
    assert not authorized.allowed, "an LLM recommendation widened a policy bound"
    assert authorized.rule_id == "policy_engine:llm_authority.denied"
    # The refusal records what the model wanted. A trail showing the gate
    # overruling a recommendation is the evidence that the gate is real.
    assert authorized.overridden_recommendation == Action.SCHEDULE_RETRY.value


@pytest.mark.parametrize(
    "denial_case",
    [
        {"attempt_count": HARD_ATTEMPT_CEILING},
        {"is_terminal": True, "terminal_reason": "MANDATE_REVOKED"},
    ],
)
def test_no_denial_shape_can_be_flipped_by_a_recommendation(
    policy: PolicyEngine, entity, denial_case
):
    """Every denial path, not just the attempt cap, survives a recommendation."""
    request = make_request(with_entity(entity, **denial_case))
    assert not policy.evaluate(request).allowed
    for action in Action:
        assert not policy.authorize(request, _recommendation(action)).allowed


def test_a_recommendation_outside_the_envelope_is_refused(policy: PolicyEngine, entity):
    """Permitted does not mean permitted to do anything."""
    request = make_request(entity, decline_code=DeclineCode.CARD_EXPIRED)
    base = policy.evaluate(request)
    assert base.allowed

    outside = next(a for a in Action if a not in base.permitted_actions)
    decision = policy.authorize(request, _recommendation(outside))
    assert not decision.allowed
    assert decision.rule_id == "policy_engine:llm_authority.out_of_envelope"


def test_a_recommendation_inside_the_envelope_is_applied(policy: PolicyEngine, entity):
    request = make_request(entity, decline_code=DeclineCode.INSUFFICIENT_FUNDS)
    base = policy.evaluate(request)
    assert Action.SCHEDULE_RETRY in base.permitted_actions

    decision = policy.authorize(request, _recommendation(Action.SCHEDULE_RETRY))
    assert decision.allowed
    assert decision.rationale == "The model believes this is recoverable."


def test_a_recommendation_may_delay_but_never_hasten(policy: PolicyEngine, entity):
    """A model may choose to do less. It may never choose to do more."""
    last = NOW - timedelta(hours=7)
    request = make_request(
        with_entity(entity, attempt_count=1, last_attempt_at=last),
        decline_code=DeclineCode.INSUFFICIENT_FUNDS,
    )
    floor = policy.evaluate(request).earliest_next_attempt_at
    assert floor is not None

    later = policy.authorize(
        request,
        _recommendation(Action.SCHEDULE_RETRY, proposed_next_attempt_at=floor + timedelta(days=1)),
    )
    assert later.earliest_next_attempt_at == floor + timedelta(days=1)

    earlier = policy.authorize(
        request,
        _recommendation(Action.SCHEDULE_RETRY, proposed_next_attempt_at=floor - timedelta(days=1)),
    )
    assert earlier.earliest_next_attempt_at == floor


def test_a_permitted_decision_always_offers_the_exits(policy: PolicyEngine, entity):
    """Doing less is never outside the envelope."""
    decision = policy.evaluate(make_request(entity))
    assert Action.ESCALATE in decision.permitted_actions
    assert Action.HALT_SCHEDULE in decision.permitted_actions


def test_a_decision_is_immutable(policy: PolicyEngine, entity):
    """A decision that can be edited after the fact is not a decision."""
    decision = policy.evaluate(make_request(entity))
    with pytest.raises(ValidationError):
        decision.allowed = False
