"""Engine 2's compliance boundaries, tested adversarially.

Each of these defends one assertion the `rbi-mandate-rules` skill makes out loud,
and each is written to try to *break* it rather than to confirm it. The revoked
mandate case in particular is exercised through every entry point the engine has,
because "no code path retries a revoked mandate" is only worth something if the
code paths were actually enumerated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.engines.mandate_recovery.classification import (
    assess_notice,
    classify_failure,
    mandate_state,
)
from app.engines.mandate_recovery.config import default_config
from app.engines.mandate_recovery.scheduler import (
    RetryScheduler,
    mandate_entity,
    outreach_entity,
)
from app.engines.mandate_recovery.schemas import (
    DebitAttempt,
    FailureRoute,
    Mandate,
    MandateState,
    NextStep,
    NoticeStatus,
)
from app.models.enums import Action, Engine, Outcome
from app.models.provenance import Provenance
from app.services.decline_taxonomy import DeclineCode
from app.services.policy_engine import (
    RULE_REGISTRY,
    LLMRecommendation,
    PolicyEngine,
    PolicyRequest,
)

#: 14:30 IST — inside the outreach window, so a quiet-hours failure in these
#: tests is always a real one and never an accident of when they were run.
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CONFIG = default_config()

BELOW_AFA = 900_000  # Rs 9,000
ABOVE_AFA = 2_000_000  # Rs 20,000


def make_mandate(
    *,
    mandate_id: str = "mnd_test",
    amount_paise: int = BELOW_AFA,
    status: str = "active",
    afa_registered: bool = True,
    attempts: int = 1,
    failure_code: str | None = DeclineCode.INSUFFICIENT_FUNDS.value,
    notice_lead_hours: float | None = 36.0,
    next_debit_in_hours: float = 30.0,
    cap_multiplier: float = 1.5,
    last_attempt_hours_ago: float = 60.0,
) -> Mandate:
    """A mandate built to order, with every gate input under the test's control."""
    next_debit = NOW + timedelta(hours=next_debit_in_hours)
    history = [
        DebitAttempt(
            attempt_no=i + 1,
            cycle=2,
            scheduled_at=NOW - timedelta(hours=last_attempt_hours_ago + 24 * (attempts - 1 - i)),
            attempted_at=NOW - timedelta(hours=last_attempt_hours_ago + 24 * (attempts - 1 - i)),
            notice_sent_at=None,
            status="failed",
            failure_reason_code=failure_code,
        )
        for i in range(attempts)
    ]
    return Mandate(
        mandate_id=mandate_id,
        batch_id="mnd-test",
        customer_id="cust_0001",
        customer_name="Test Customer",
        amount_paise=amount_paise,
        frequency="monthly",
        registered_at=NOW - timedelta(days=200),
        afa_registered=afa_registered,
        mandate_cap_paise=int(amount_paise * cap_multiplier),
        status=status,
        cycle_started_at=NOW - timedelta(days=3),
        attempts_in_current_cycle=attempts,
        next_debit_at=next_debit,
        next_debit_notice_sent_at=(
            next_debit - timedelta(hours=notice_lead_hours)
            if notice_lead_hours is not None
            else None
        ),
        debit_history=history,
    )


def plan(mandate: Mandate, *, now: datetime = NOW):
    classification = classify_failure(
        mandate.latest_failure.failure_reason_code if mandate.latest_failure else None
    )
    scheduler = RetryScheduler(policy=PolicyEngine(), config=CONFIG)
    return scheduler.plan(mandate, classification, now=now)


# ---------------------------------------------------------------------------
# 1. A revoked mandate is untouchable
# ---------------------------------------------------------------------------


def test_revoked_mandate_halts_and_never_schedules_a_debit() -> None:
    schedule = plan(make_mandate(status="revoked"))
    assert schedule.explanation.step is NextStep.HALT
    assert schedule.explanation.terminal
    assert schedule.decision.rule_id == "rbi-mandate-rules:A4"
    assert schedule.decision.outcome is Outcome.HALTED
    assert schedule.proposed_debit_at is None


def test_revoked_mandate_is_refused_even_with_every_other_precondition_perfect() -> None:
    """Adversarial: hand the gate a mandate that is flawless apart from revocation."""
    mandate = make_mandate(status="revoked", attempts=0, notice_lead_hours=36.0)
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(mandate),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=mandate.mandate_cap_paise,
            fresh_afa_completed=True,
        )
    )
    assert not decision.allowed
    assert decision.rule_id == "rbi-mandate-rules:A4"
    assert decision.terminal


def test_a_model_recommending_a_retry_cannot_revive_a_revoked_mandate() -> None:
    """The recommendation is recorded as overruled; the denial stands."""
    mandate = make_mandate(status="revoked")
    request = PolicyRequest(
        engine=Engine.MANDATE_RECOVERY,
        entity=mandate_entity(mandate),
        proposed_action=Action.ATTEMPT_CHARGE,
        now=NOW,
        afa_registered=True,
        notice_lead_hours=36.0,
        mandate_cap_paise=mandate.mandate_cap_paise,
    )
    decision = PolicyEngine().authorize(
        request,
        LLMRecommendation(
            action=Action.SCHEDULE_RETRY,
            rationale="one more try never hurts",
            confidence=0.99,
            provenance=Provenance.deterministic(),
        ),
    )
    assert not decision.allowed
    assert decision.rule_id == "rbi-mandate-rules:A4"
    assert decision.overridden_recommendation == Action.SCHEDULE_RETRY.value
    assert decision.authority_rule_id == "policy_engine:llm_authority.denied"


def test_a_revoked_mandate_receives_no_customer_message_either() -> None:
    """`policy-bounds:HS1` is stricter than the phase brief's "single notice".

    Deliberate, and flagged in ADR 0008: the stopping rule wins, because doing
    less is the safe direction and a revoked mandate is exactly the case where
    contacting anyway is the embarrassing failure.
    """
    mandate = make_mandate(status="revoked")
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=outreach_entity(mandate),
            proposed_action=Action.SEND_DUNNING,
            now=NOW,
            is_outreach=True,
        )
    )
    assert not decision.allowed
    assert decision.terminal


def test_expired_mandate_is_terminal_too() -> None:
    schedule = plan(make_mandate(status="expired"))
    assert schedule.explanation.terminal
    assert schedule.decision.rule_id == "rbi-mandate-rules:A4"


def test_an_unrecognised_mandate_status_fails_closed() -> None:
    mandate = make_mandate(status="on_holiday")
    assert mandate_state(mandate) is MandateState.UNKNOWN
    schedule = plan(mandate)
    assert schedule.explanation.step is NextStep.ESCALATE
    assert schedule.decision.rule_id == "policy_engine:fail_closed"


# ---------------------------------------------------------------------------
# 2. Notification window enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lead", "status", "rule"),
    [
        (None, NoticeStatus.MISSING, "rbi-mandate-rules:A2"),
        (6.0, NoticeStatus.LATE, "rbi-mandate-rules:A2"),
        (23.9, NoticeStatus.LATE, "rbi-mandate-rules:A2"),
        (72.0, NoticeStatus.STALE, "rbi-mandate-rules:PartB.notice_staleness"),
    ],
)
def test_a_debit_without_a_compliant_notice_is_denied(
    lead: float | None, status: NoticeStatus, rule: str
) -> None:
    mandate = make_mandate(notice_lead_hours=lead)
    assert assess_notice(mandate, CONFIG.notice).status is status

    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(mandate),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            afa_registered=True,
            notice_lead_hours=lead,
            mandate_cap_paise=mandate.mandate_cap_paise,
        )
    )
    assert not decision.allowed
    assert decision.rule_id == rule
    assert decision.reason_code == DeclineCode.PRE_DEBIT_NOTICE_MISSING.value


@pytest.mark.parametrize("lead", [24.0, 36.0, 48.0])
def test_a_notice_inside_the_window_passes_the_a2_gate(lead: float) -> None:
    mandate = make_mandate(notice_lead_hours=lead)
    schedule = plan(mandate)
    assert schedule.notice.compliant
    assert schedule.explanation.step is NextStep.ATTEMPT_DEBIT


def test_a_denied_debit_schedules_the_notice_rather_than_giving_up() -> None:
    """A2's prescribed response, not merely a refusal."""
    schedule = plan(make_mandate(notice_lead_hours=None))
    assert schedule.explanation.step is NextStep.SEND_PRE_DEBIT_NOTICE
    assert schedule.explanation.compliance_blocked
    assert schedule.decision.rule_id == "rbi-mandate-rules:A2"
    # The rescheduled debit satisfies the window it was blocked for.
    lead = (schedule.proposed_debit_at - schedule.proposed_notice_at) / timedelta(hours=1)
    assert 24.0 <= lead <= 48.0


def test_no_notice_is_sent_for_a_debit_that_could_never_fire() -> None:
    """A mandate at its cap gets no compliance notice — the debit is not coming."""
    schedule = plan(make_mandate(attempts=3, notice_lead_hours=None))
    assert schedule.explanation.step is not NextStep.SEND_PRE_DEBIT_NOTICE
    assert schedule.decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"


# ---------------------------------------------------------------------------
# 3. AFA threshold branching
# ---------------------------------------------------------------------------


def test_below_the_threshold_a_registered_mandate_may_auto_debit() -> None:
    schedule = plan(make_mandate(amount_paise=BELOW_AFA))
    assert schedule.explanation.step is NextStep.ATTEMPT_DEBIT


def test_above_the_threshold_a_debit_needs_fresh_afa_and_routes_to_the_customer() -> None:
    schedule = plan(make_mandate(amount_paise=ABOVE_AFA))
    assert schedule.explanation.step is NextStep.REQUEST_AUTHENTICATION
    assert schedule.decision.rule_id == "rbi-mandate-rules:A3"
    assert schedule.decision.reason_code == DeclineCode.AFA_REQUIRED.value
    # The remedy moves with the branch: TN2 would reject a "top up your balance"
    # message for a mandate blocked on authentication.
    assert schedule.classification.route is FailureRoute.AUTHENTICATION


def test_the_two_branches_differ_on_amount_alone() -> None:
    """Same failure, same notice, same attempts. Only the amount moves."""
    below = plan(make_mandate(amount_paise=BELOW_AFA))
    above = plan(make_mandate(amount_paise=ABOVE_AFA))
    assert below.explanation.step is not above.explanation.step


def test_a_mandate_that_never_completed_registration_can_never_debit() -> None:
    schedule = plan(make_mandate(afa_registered=False))
    assert schedule.decision.rule_id == "rbi-mandate-rules:A1"
    assert schedule.explanation.step is NextStep.REQUEST_AUTHENTICATION


def test_an_afa_required_decline_cites_a5_rather_than_the_amount_rule() -> None:
    """A5 is more specific: the issuer already asked, and we did not supply it."""
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(make_mandate(amount_paise=BELOW_AFA)),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            decline_code=DeclineCode.AFA_REQUIRED,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=BELOW_AFA * 2,
        )
    )
    assert decision.rule_id == "rbi-mandate-rules:A5"


def test_unestablished_afa_registration_denies_exactly_as_a_false_one_does() -> None:
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(make_mandate()),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            afa_registered=None,
            notice_lead_hours=36.0,
            mandate_cap_paise=BELOW_AFA * 2,
        )
    )
    assert not decision.allowed
    assert decision.rule_id == "rbi-mandate-rules:A1"


# ---------------------------------------------------------------------------
# 4. The attempt cap is absolute
# ---------------------------------------------------------------------------


def test_a_fourth_attempt_in_a_cycle_is_refused() -> None:
    schedule = plan(make_mandate(attempts=3))
    assert schedule.decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
    assert schedule.decision.attempts_remaining == 0
    assert schedule.explanation.step is NextStep.SEND_DUNNING


def test_a_caller_cannot_raise_the_cap() -> None:
    """`policy-bounds:AC2` clamps a configured override; it never honours one."""
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(make_mandate(attempts=3)),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            max_attempts_override=50,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=BELOW_AFA * 2,
        )
    )
    assert not decision.allowed
    assert decision.attempts_remaining == 0


def test_an_llm_recommendation_cannot_force_a_fourth_attempt() -> None:
    mandate = make_mandate(attempts=3)
    request = PolicyRequest(
        engine=Engine.MANDATE_RECOVERY,
        entity=mandate_entity(mandate),
        proposed_action=Action.ATTEMPT_CHARGE,
        now=NOW,
        afa_registered=True,
        notice_lead_hours=36.0,
        mandate_cap_paise=mandate.mandate_cap_paise,
    )
    decision = PolicyEngine().authorize(
        request,
        LLMRecommendation(
            action=Action.SCHEDULE_RETRY,
            rationale="the customer will surely have funds now",
            confidence=1.0,
            provenance=Provenance.deterministic(),
        ),
    )
    assert not decision.allowed
    assert decision.overridden_recommendation == Action.SCHEDULE_RETRY.value


def test_the_engine_cap_is_stricter_than_the_shared_ceiling() -> None:
    """Part B's 3 wins over `policy-bounds:AC1`'s 4 for mandates."""
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(make_mandate(attempts=3, failure_code=None)),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=BELOW_AFA * 2,
        )
    )
    assert decision.rule_id == "rbi-mandate-rules:PartB.max_retries"


# ---------------------------------------------------------------------------
# 5. Backoff enforcement
# ---------------------------------------------------------------------------


def test_a_retry_inside_the_cooldown_is_denied() -> None:
    """Two prior failures means a 48h wait; a debit 30h later is refused."""
    mandate = make_mandate(attempts=2, last_attempt_hours_ago=2.0, next_debit_in_hours=30.0)
    schedule = plan(mandate)
    assert schedule.explanation.step is NextStep.DEFER
    assert schedule.decision.rule_id == "rbi-mandate-rules:PartB.min_spacing"
    assert schedule.decision.earliest_next_attempt_at is not None


def test_a_deferred_retry_is_rescheduled_not_abandoned() -> None:
    mandate = make_mandate(attempts=2, last_attempt_hours_ago=2.0, next_debit_in_hours=30.0)
    schedule = plan(mandate)
    assert schedule.explanation.scheduled_for == schedule.decision.earliest_next_attempt_at
    assert not schedule.suppressed


def test_the_mandate_spacing_floor_is_24h_not_the_generic_6h() -> None:
    mandate = make_mandate(attempts=1, last_attempt_hours_ago=1.0, next_debit_in_hours=8.0)
    schedule = plan(mandate)
    assert schedule.explanation.step is NextStep.DEFER
    gap = schedule.decision.earliest_next_attempt_at - mandate.latest_failure.attempted_at
    assert gap >= timedelta(hours=24)


# ---------------------------------------------------------------------------
# 6. Quiet hours
# ---------------------------------------------------------------------------


def test_a_message_in_quiet_hours_is_held_and_rescheduled_not_dropped() -> None:
    #: 03:00 IST.
    night = datetime(2026, 9, 1, 21, 30, tzinfo=UTC)
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=outreach_entity(make_mandate()),
            proposed_action=Action.SEND_DUNNING,
            now=night,
            is_outreach=True,
        )
    )
    assert not decision.allowed
    assert decision.rule_id == "policy-bounds:QH1"
    assert decision.earliest_next_attempt_at is not None
    assert decision.earliest_next_attempt_at > night


def test_the_contact_cap_refuses_a_fourth_message() -> None:
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=outreach_entity(make_mandate(), messages_sent=3),
            proposed_action=Action.SEND_DUNNING,
            now=NOW,
            is_outreach=True,
            messages_sent_in_window=3,
        )
    )
    assert not decision.allowed
    assert decision.rule_id in {"policy-bounds:QH2", "rbi-mandate-rules:PartB.max_retries"}


def test_outreach_is_not_bounded_by_the_debit_budget() -> None:
    """A hard decline has spent its debit budget by definition.

    If a message were gated on that budget the dunning path would be silent on
    exactly the mandates that most need it — which is why `outreach_entity()`
    exists at all.
    """
    mandate = make_mandate(failure_code=DeclineCode.CARD_EXPIRED.value, attempts=1)
    debit = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(mandate),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            decline_code=DeclineCode.CARD_EXPIRED,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=mandate.mandate_cap_paise,
        )
    )
    message = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=outreach_entity(mandate),
            proposed_action=Action.SEND_DUNNING,
            now=NOW,
            is_outreach=True,
        )
    )
    assert not debit.allowed
    assert message.allowed


# ---------------------------------------------------------------------------
# 7. Hard declines never retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        DeclineCode.CARD_EXPIRED.value,
        DeclineCode.ACCOUNT_CLOSED.value,
        DeclineCode.INVALID_ACCOUNT.value,
        DeclineCode.CARD_DISABLED_ONLINE.value,
    ],
)
def test_a_hard_decline_routes_to_communication_and_never_to_a_debit(code: str) -> None:
    schedule = plan(make_mandate(failure_code=code))
    assert schedule.classification.route is FailureRoute.DUNNING
    assert schedule.explanation.step is not NextStep.ATTEMPT_DEBIT
    assert schedule.suppressed


def test_a_soft_decline_does_get_a_debit() -> None:
    """The contrast that makes the previous test mean something."""
    schedule = plan(make_mandate(failure_code=DeclineCode.INSUFFICIENT_FUNDS.value))
    assert schedule.classification.route is FailureRoute.RETRY
    assert schedule.explanation.step is NextStep.ATTEMPT_DEBIT


def test_a_paused_mandate_blocks_the_debit_without_terminating_the_schedule() -> None:
    schedule = plan(make_mandate(status="paused"))
    assert schedule.decision.rule_id == "rbi-mandate-rules:PartB.paused"
    assert not schedule.explanation.terminal


def test_a_debit_above_the_registered_cap_is_refused() -> None:
    mandate = make_mandate(cap_multiplier=0.5)
    schedule = plan(mandate)
    assert schedule.decision.rule_id == "rbi-mandate-rules:PartB.mandate_cap"
    assert schedule.decision.requires_human


def test_an_unestablished_mandate_cap_fails_closed() -> None:
    decision = PolicyEngine().evaluate(
        PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(make_mandate()),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=NOW,
            afa_registered=True,
            notice_lead_hours=36.0,
            mandate_cap_paise=None,
        )
    )
    assert not decision.allowed
    assert decision.reason_code == "MANDATE_CAP_UNKNOWN"


# ---------------------------------------------------------------------------
# The rules themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    [
        "rbi-mandate-rules:A1",
        "rbi-mandate-rules:A2",
        "rbi-mandate-rules:A3",
        "rbi-mandate-rules:A4",
        "rbi-mandate-rules:A5",
        "rbi-mandate-rules:PartB.max_retries",
        "rbi-mandate-rules:PartB.min_spacing",
        "rbi-mandate-rules:PartB.notice_staleness",
        "rbi-mandate-rules:PartB.mandate_cap",
        "rbi-mandate-rules:PartB.paused",
        "policy-bounds:TN1",
        "policy-bounds:TN2",
        "policy-bounds:TN3",
    ],
)
def test_every_rule_this_engine_cites_is_registered(rule_id: str) -> None:
    assert rule_id in RULE_REGISTRY
    assert RULE_REGISTRY[rule_id].citation


def test_the_config_cannot_disagree_with_the_gate() -> None:
    """The displayed window and the enforced window are the same window."""
    from app.services.policy_engine import NOTICE_MAX_LEAD_HOURS, NOTICE_MIN_LEAD_HOURS

    assert CONFIG.notice.min_lead == timedelta(hours=NOTICE_MIN_LEAD_HOURS)
    assert CONFIG.notice.max_lead == timedelta(hours=NOTICE_MAX_LEAD_HOURS)


def test_a_config_that_contradicts_the_gate_refuses_to_load(tmp_path) -> None:
    import json

    from app.engines.mandate_recovery.config import MandateConfigError, load_config

    raw = json.loads((CONFIG.raw and json.dumps(CONFIG.raw)) or "{}")
    raw["notice_window"]["min_lead_hours"] = 6
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(MandateConfigError, match="policy engine enforces"):
        load_config(path)
