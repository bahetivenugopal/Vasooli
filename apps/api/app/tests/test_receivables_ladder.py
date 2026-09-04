"""The chase ladder and the bounds that stop it.

Every test here is a stopping rule, and stopping rules are the credibility
backbone of this submission. Six of them are the ones a receivables engine is
most likely to get quietly wrong:

- **RL4** — a dispute freezes the chase immediately, and nothing gets past it.
- **RL3** — a customer with many overdue invoices does not receive one message
  per invoice. This is the cap that is easy to miss.
- **QH1** — a message inside quiet hours is *held*, not dropped, and it carries
  the moment it becomes permissible.
- **RL5** — escalation needs evidence. A week passing is not evidence.
- **PR2** — a customer who has already committed is not chased.
- **RL1/RL2** — three rungs, 72 hours apart, and no fourth rung exists.

They are asserted against `PolicyEngine` directly wherever the rule is the thing
under test, because a gate tested only through the engine that calls it is a gate
that can be bypassed by the next engine that forgets to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.engines.receivables.config import default_config
from app.engines.receivables.ladder import (
    SUPPRESSION_RULES,
    ChaseLadder,
    ContactCounts,
    escalation_evidence,
    invoice_entity,
)
from app.engines.receivables.schemas import (
    ChasePlan,
    ChaseStep,
    ExtractionResult,
    Invoice,
    PromiseAssessment,
    PromiseStatus,
    ReplyIntent,
    ReplyRecommendation,
    ReplyUnderstanding,
)
from app.models.enums import Action, Engine, EscalationTrigger, Outcome
from app.models.provenance import Provenance
from app.services.policy_engine import (
    LADDER_RUNGS,
    MAX_MESSAGES_PER_CUSTOMER,
    MAX_MESSAGES_PER_WINDOW,
    PolicyEngine,
    PolicyRequest,
)

CONFIG = default_config()
#: 14:30 IST — comfortably inside the QH1 outreach window.
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def invoice(**overrides: Any) -> Invoice:
    base: dict[str, Any] = {
        "invoice_id": "inv_0001",
        "batch_id": "inv-test",
        "customer_id": "cust_0001",
        "customer_name": "Probe Systems Pvt Ltd",
        "amount_paise": 5_000_000,
        "payment_terms": "NET30",
        "issued_at": datetime(2026, 7, 1, tzinfo=UTC),
        "due_at": datetime(2026, 7, 31, tzinfo=UTC),
        "days_overdue": 32,
        "status": "overdue",
        "communications": [],
        "replies": [],
    }
    return Invoice.model_validate(base | overrides)


def contacts(*, rungs: int, last_at: datetime | None = None) -> list[dict[str, Any]]:
    """`rungs` outbound contacts, spaced a week apart, ending at `last_at`."""
    end = last_at or (NOW - timedelta(days=8))
    return [
        {
            "communication_id": f"inv_0001_out_{i}",
            "direction": "outbound",
            "channel": "email",
            "rung": i,
            "template": f"reminder_rung_{i}",
            "sent_at": end - timedelta(days=7 * (rungs - i)),
        }
        for i in range(1, rungs + 1)
    ]


def extraction(
    *,
    intent: ReplyIntent = ReplyIntent.PROMISE,
    promise: bool = True,
    recommendation: ReplyRecommendation = ReplyRecommendation.PAUSE_CHASE,
    abstained: bool = False,
) -> ExtractionResult:
    return ExtractionResult(
        invoice_id="inv_0001",
        reply_id="inv_0001_in_1",
        received_at=NOW - timedelta(days=2),
        text="stub reply",
        understanding=(
            None
            if abstained
            else ReplyUnderstanding(
                intent=intent,
                promise_detected=promise,
                committed_date="2026-09-10" if promise else None,
                dispute_detail="already paid" if intent is ReplyIntent.DISPUTE else None,
                language="en",
                recommended_action=recommendation,
                confidence=0.9,
                reasoning="stub",
            )
        ),
        abstained=abstained,
        provenance=(
            Provenance.deterministic(abstained=True)
            if abstained
            else Provenance.from_model(provider="stub", model="stub-1", prompt_version="v1")
        ),
        confidence=None if abstained else 0.9,
    )


def promise(
    *, status: PromiseStatus = PromiseStatus.ACTIVE, suppresses: bool | None = None
) -> PromiseAssessment:
    return PromiseAssessment(
        invoice_id="inv_0001",
        reply_id="inv_0001_in_1",
        customer_id="cust_0001",
        received_at=NOW - timedelta(days=2),
        committed_date=NOW + timedelta(days=9),
        committed_amount_paise=None,
        conditional=False,
        condition_detail=None,
        status=status,
        rule_id="policy-bounds:PP1",
        rationale="stub",
        review_at=NOW + timedelta(days=11),
        suppresses_chase=(
            suppresses if suppresses is not None else status is PromiseStatus.ACTIVE
        ),
        confidence=0.9,
    )


def plan(
    inv: Invoice | None = None,
    *,
    ext: ExtractionResult | None = None,
    prom: PromiseAssessment | None = None,
    invoice_messages: int = 0,
    customer_messages: int = 0,
    now: datetime = NOW,
) -> ChasePlan:
    ladder = ChaseLadder(policy=PolicyEngine(), config=CONFIG)
    return ladder.plan(
        inv or invoice(),
        extraction=ext,
        promise=prom,
        counts=ContactCounts(
            invoice_messages_in_window=invoice_messages,
            customer_messages_in_window=customer_messages,
        ),
        now=now,
    )


def outreach_request(**overrides: Any) -> PolicyRequest:
    base: dict[str, Any] = {
        "engine": Engine.RECEIVABLES,
        "entity": invoice_entity(invoice(), messages_sent=0, last_contact_at=None),
        "proposed_action": Action.SEND_REMINDER,
        "now": NOW,
        "is_outreach": True,
    }
    return PolicyRequest.model_validate(base | overrides)


# ---------------------------------------------------------------------------
# RL4 — the dispute freeze
# ---------------------------------------------------------------------------


def test_a_dispute_freezes_the_chase_immediately(policy: PolicyEngine) -> None:
    decision = policy.evaluate(outreach_request(dispute_frozen=True))
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:RL4"
    assert decision.action is Action.ESCALATE
    assert decision.requires_human is True


@pytest.mark.parametrize(
    "action",
    [Action.SEND_REMINDER, Action.ESCALATE, Action.SCHEDULE_RETRY, Action.API_CALL],
)
def test_a_dispute_freeze_stops_every_action_not_only_reminders(
    policy: PolicyEngine, action: Action
) -> None:
    """A rule that only applied to messages would leave the rest running.

    Payment-link creation and escalation would still fire against an invoice the
    customer says is wrong, which is exactly the conduct RL4 exists to prevent.
    """
    decision = policy.evaluate(
        outreach_request(proposed_action=action, dispute_frozen=True, is_outreach=False)
    )
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:RL4"


def test_no_further_contact_is_authorised_after_a_dispute() -> None:
    result = plan(ext=extraction(intent=ReplyIntent.DISPUTE, promise=False))
    assert result.step is ChaseStep.FREEZE_DISPUTE
    assert result.rule_id == "policy-bounds:RL4"
    assert result.suppressed is True
    assert result.escalation_trigger is EscalationTrigger.DISPUTE


def test_a_dispute_overrules_a_model_that_wants_to_keep_chasing() -> None:
    """The gate catching the model is the evidence that the gate is real."""
    result = plan(
        ext=extraction(
            intent=ReplyIntent.DISPUTE,
            promise=False,
            recommendation=ReplyRecommendation.CONTINUE_CHASE,
        )
    )
    assert result.step is ChaseStep.FREEZE_DISPUTE
    assert result.rule_id == "policy-bounds:RL4"
    assert result.overridden_recommendation == Action.SEND_REMINDER.value
    assert result.authority_rule == "policy_engine:llm_authority.denied"


# ---------------------------------------------------------------------------
# RL3 — the cross-invoice contact cap
# ---------------------------------------------------------------------------


def test_a_customer_at_the_cross_invoice_cap_receives_nothing_further(
    policy: PolicyEngine,
) -> None:
    """Eight overdue invoices are not eight licences to write."""
    decision = policy.evaluate(
        outreach_request(customer_messages_in_window=MAX_MESSAGES_PER_CUSTOMER)
    )
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:RL3"
    assert decision.reason_code == "CUSTOMER_CONTACT_CAP_REACHED"


def test_the_customer_cap_bites_before_the_per_invoice_one(policy: PolicyEngine) -> None:
    """RL3 is checked first: a customer at their weekly limit is at it, whatever
    this particular invoice's own ladder has left."""
    decision = policy.evaluate(
        outreach_request(
            customer_messages_in_window=MAX_MESSAGES_PER_CUSTOMER,
            messages_sent_in_window=MAX_MESSAGES_PER_WINDOW,
        )
    )
    assert decision.rule_id == "policy-bounds:RL3"


def test_the_customer_cap_sits_above_the_per_invoice_cap() -> None:
    """One whole ladder plus room to raise a second invoice, and no more.

    If these were equal, a customer's second invoice could never be raised at
    all; if RL3 were much larger it would stop being a bound on the recipient.
    """
    assert MAX_MESSAGES_PER_CUSTOMER == MAX_MESSAGES_PER_WINDOW + 1


def test_one_invoice_below_the_customer_cap_is_still_contactable() -> None:
    result = plan(customer_messages=MAX_MESSAGES_PER_CUSTOMER - 1)
    assert result.step is ChaseStep.SEND_REMINDER


def test_a_customer_at_the_cap_produces_a_suppression_not_a_deferral() -> None:
    """A cap is not a timing problem, and the summary counts them apart."""
    result = plan(customer_messages=MAX_MESSAGES_PER_CUSTOMER)
    assert result.suppressed is True
    assert result.suppressed_rule == "policy-bounds:RL3"
    assert "policy-bounds:RL3" in SUPPRESSION_RULES


# ---------------------------------------------------------------------------
# QH1 — quiet hours hold, never drop
# ---------------------------------------------------------------------------


def test_a_reminder_inside_quiet_hours_is_held_and_carries_its_reopening(
    policy: PolicyEngine,
) -> None:
    """03:00 IST. The revenue is still recoverable; only the timing was wrong."""
    small_hours = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)
    decision = policy.evaluate(outreach_request(now=small_hours))
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:QH1"
    assert decision.earliest_next_attempt_at is not None
    assert decision.earliest_next_attempt_at > small_hours


def test_a_held_reminder_is_a_deferral_with_a_time_to_come_back() -> None:
    small_hours = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)
    result = plan(now=small_hours)
    assert result.step is ChaseStep.DEFER
    assert result.rule_id == "policy-bounds:QH1"
    assert result.scheduled_for is not None
    assert result.scheduled_for > small_hours


def test_an_escalation_is_not_outreach_and_ignores_quiet_hours() -> None:
    """A handoff to a human is not something the customer receives at 03:00."""
    small_hours = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)
    result = plan(invoice(communications=contacts(rungs=3)), now=small_hours)
    assert result.step is ChaseStep.ESCALATE
    assert result.escalation_trigger is EscalationTrigger.LADDER_EXHAUSTED


# ---------------------------------------------------------------------------
# RL5 — escalation needs evidence
# ---------------------------------------------------------------------------


def test_an_escalation_with_no_evidence_is_refused(policy: PolicyEngine) -> None:
    decision = policy.evaluate(
        outreach_request(
            proposed_action=Action.ESCALATE, is_outreach=False, escalation_trigger=None
        )
    )
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:RL5"
    assert decision.reason_code == "NO_ESCALATION_EVIDENCE"


def test_elapsed_time_alone_is_not_evidence() -> None:
    """A month-old invoice with rungs left and no reply does not escalate."""
    ancient = invoice(
        due_at=datetime(2026, 5, 1, tzinfo=UTC),
        days_overdue=123,
        communications=contacts(rungs=1),
    )
    assert (
        escalation_evidence(invoice=ancient, extraction=None, promise=None, config=CONFIG)
        is None
    )
    assert plan(ancient).step is ChaseStep.SEND_REMINDER


def test_a_broken_promise_is_evidence() -> None:
    result = plan(ext=extraction(), prom=promise(status=PromiseStatus.BROKEN))
    assert result.step is ChaseStep.ESCALATE
    assert result.escalation_trigger is EscalationTrigger.BROKEN_PROMISE


def test_an_exhausted_ladder_is_evidence() -> None:
    result = plan(invoice(communications=contacts(rungs=len(LADDER_RUNGS))))
    assert result.step is ChaseStep.ESCALATE
    assert result.escalation_trigger is EscalationTrigger.LADDER_EXHAUSTED


def test_a_live_promise_outranks_an_exhausted_ladder() -> None:
    """A cooperating customer is not escalated for having been chased before.

    Found on the first live run: six invoices whose customers had committed and
    were inside their window were escalating anyway, purely because they had
    already had three reminders. "The ladder ran out" is a timer argument wearing
    evidence's clothes.
    """
    exhausted = invoice(communications=contacts(rungs=len(LADDER_RUNGS)))
    assert (
        escalation_evidence(
            invoice=exhausted, extraction=extraction(), promise=promise(), config=CONFIG
        )
        is None
    )
    result = plan(exhausted, ext=extraction(), prom=promise())
    assert result.step is ChaseStep.HOLD_FOR_PROMISE


def test_an_abstention_is_evidence_and_routes_to_a_human() -> None:
    result = plan(ext=extraction(abstained=True))
    assert result.step is ChaseStep.ESCALATE
    assert result.rule_id == "policy-bounds:HE1"
    assert result.escalation_trigger is EscalationTrigger.ABSTENTION


# ---------------------------------------------------------------------------
# PR2 — a live promise deprioritises rather than being chased
# ---------------------------------------------------------------------------


def test_a_customer_with_a_live_promise_is_not_chased(policy: PolicyEngine) -> None:
    decision = policy.evaluate(outreach_request(active_promise=True))
    assert decision.allowed is False
    assert decision.rule_id == "policy-bounds:PR2"
    assert decision.outcome is Outcome.SKIPPED


def test_pr2_is_evaluated_above_the_caps_and_the_cooldown(policy: PolicyEngine) -> None:
    """The refusal has to name the *true* reason.

    All three of PR2, the RL1 cap and the RL2 interval would refuse this. Only
    one of them says the thing a reader needs: the customer already committed.
    """
    entity = invoice_entity(
        invoice(communications=contacts(rungs=3, last_at=NOW - timedelta(hours=1))),
        messages_sent=MAX_MESSAGES_PER_WINDOW,
        last_contact_at=NOW - timedelta(hours=1),
    )
    decision = policy.evaluate(
        outreach_request(
            entity=entity,
            active_promise=True,
            messages_sent_in_window=MAX_MESSAGES_PER_WINDOW,
        )
    )
    assert decision.rule_id == "policy-bounds:PR2"


def test_a_broken_promise_no_longer_suppresses_the_chase() -> None:
    result = plan(ext=extraction(), prom=promise(status=PromiseStatus.BROKEN))
    assert result.step is not ChaseStep.HOLD_FOR_PROMISE


# ---------------------------------------------------------------------------
# RL1 / RL2 — three rungs, 72 hours apart
# ---------------------------------------------------------------------------


def test_the_ladder_has_exactly_three_rungs_and_no_fourth_exists() -> None:
    assert len(CONFIG.ladder.rungs) == len(LADDER_RUNGS) == 3
    assert CONFIG.ladder.rung_for(0).name == "gentle_reminder"
    assert CONFIG.ladder.rung_for(2).name == "formal_notice"
    assert CONFIG.ladder.rung_for(3) is None


def test_a_rung_inside_the_72_hour_interval_is_deferred() -> None:
    recent = invoice(communications=contacts(rungs=1, last_at=NOW - timedelta(hours=10)))
    result = plan(recent)
    assert result.step is ChaseStep.DEFER
    assert result.rule_id == "policy-bounds:RL2"
    assert result.scheduled_for is not None


def test_a_rung_past_the_interval_proceeds() -> None:
    older = invoice(communications=contacts(rungs=1, last_at=NOW - timedelta(hours=80)))
    result = plan(older)
    assert result.step is ChaseStep.SEND_REMINDER
    assert result.rung == "firm_follow_up"


def test_three_rungs_at_the_interval_fit_inside_the_contact_window() -> None:
    """RL2 and QH2 have to be compatible, or the ladder cannot run to completion.

    Day 0, day 3, day 6 — all three inside the rolling 7 days QH2 allows.
    """
    span = CONFIG.ladder.min_interval * (len(CONFIG.ladder.rungs) - 1)
    assert span < CONFIG.contact_caps.window


# ---------------------------------------------------------------------------
# HS1 — a settled invoice is terminal
# ---------------------------------------------------------------------------


def test_a_settled_invoice_is_never_chased() -> None:
    """The most embarrassing failure a recovery system can have."""
    paid = invoice(status="paid", paid_at=NOW - timedelta(days=1), amount_paid_paise=5_000_000)
    result = plan(paid)
    assert result.step is ChaseStep.HALT
    assert result.rule_id == "policy-bounds:HS1"
