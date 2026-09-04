"""The dunning drafting task: its tone gate, its templates, and its fallback.

Two claims are defended here that are easy to make and hard to keep:

- **The message differs meaningfully by failure class.** An expired-card message
  and an insufficient-funds message asking the customer to do the same thing is
  the tell that the classification is decorative. Asserted across every class the
  engine can produce, on the fallback path — which is the *harder* case, since a
  template set is exactly where the differentiation could quietly collapse.
- **The fallback satisfies its own tone constraints.** It is what a rejected
  draft falls back to, so a template that would itself be rejected would leave
  the customer with nothing.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.engines.mandate_recovery.dunning import (
    ROUTE_TEMPLATES,
    TASK_DRAFT_DUNNING,
    TEMPLATES,
    build_context,
    register_mandate_tasks,
    render_template,
    template_for,
    validate_tone,
)
from app.engines.mandate_recovery.schemas import (
    CallToAction,
    Channel,
    DunningDraft,
    DunningRecommendation,
    FailureRoute,
)
from app.models.enums import ProvenanceSource
from app.services.decline_taxonomy import DeclineCode
from app.services.llm_agent import REGISTRY, LLMAgent


def context_for(code: str, route: FailureRoute) -> dict:
    return build_context(
        mandate_id="mnd_0001",
        customer_name="Priya Nair",
        amount_paise=499_00,
        frequency="monthly",
        decline_code=code,
        decline_class="SOFT",
        taxonomy_action="Retry in a later window.",
        route=route,
        attempts_used=1,
        attempts_allowed=3,
        last_attempt_at="2026-09-01 03:30 UTC",
        notice_status="on_time",
        afa_side="at or below",
        communication_reason="the debit was refused",
        required_action="update the payment method",
    )


def draft(**overrides) -> DunningDraft:
    base = {
        "channel": Channel.EMAIL,
        "subject": "Your payment did not go through",
        "body": "Hi, your subscription payment did not go through. Please update your card.",
        "call_to_action": CallToAction.UPDATE_PAYMENT_METHOD,
        "recommended_action": DunningRecommendation.SEND_DUNNING,
        "confidence": 0.8,
        "reasoning": "the card is dead, so a new one is the only remedy",
    }
    return DunningDraft(**{**base, **overrides})


# ---------------------------------------------------------------------------
# Tone is policy (policy-bounds TN1 / TN2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Pay now or we will begin legal action against you.",
        "This is your final notice before we suspend your account.",
        "Failure to pay will be reported to your credit bureau.",
        "Update your card immediately or a late fee will apply.",
        "Act now — this is your last chance to keep the service.",
        "We have referred this to a collections agency.",
    ],
)
def test_a_threatening_or_urgent_draft_is_rejected(body: str) -> None:
    violations = validate_tone(draft(body=body), FailureRoute.DUNNING)
    assert violations
    assert any(v.rule_id == "policy-bounds:TN1" for v in violations)


def test_a_plain_accurate_draft_passes() -> None:
    assert validate_tone(draft(), FailureRoute.DUNNING) == []


def test_a_remedy_that_cannot_resolve_the_failure_is_rejected() -> None:
    """TN2: telling an expired-card customer to check their balance is wrong."""
    violations = validate_tone(
        draft(call_to_action=CallToAction.ENSURE_SUFFICIENT_BALANCE), FailureRoute.DUNNING
    )
    assert [v.rule_id for v in violations] == ["policy-bounds:TN2"]


def test_the_authentication_route_will_not_accept_a_balance_top_up_remedy() -> None:
    violations = validate_tone(
        draft(call_to_action=CallToAction.ENSURE_SUFFICIENT_BALANCE),
        FailureRoute.AUTHENTICATION,
    )
    assert any(v.rule_id == "policy-bounds:TN2" for v in violations)


def test_violations_are_reported_in_full_not_one_at_a_time() -> None:
    violations = validate_tone(
        draft(
            body="Final notice: pay immediately or face legal action.",
            call_to_action=CallToAction.ENSURE_SUFFICIENT_BALANCE,
        ),
        FailureRoute.DUNNING,
    )
    assert len(violations) > 1


# ---------------------------------------------------------------------------
# The fallback templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", list(FailureRoute))
@pytest.mark.parametrize("code", [*TEMPLATES.keys(), "SOMETHING_UNMAPPED"])
def test_every_template_passes_its_own_tone_gate(code: str, route: FailureRoute) -> None:
    """The fallback is what a rejection falls back *to*. It must be clean."""
    rendered = template_for(code, route).render(context_for(code, route))
    assert validate_tone(rendered, route) == []


def test_the_route_wins_over_the_code_when_they_disagree() -> None:
    """An above-threshold insufficient-funds failure is re-routed to auth.

    The insufficient-funds template would tell that customer to top up, which
    cannot unblock them.
    """
    chosen = template_for(DeclineCode.INSUFFICIENT_FUNDS.value, FailureRoute.AUTHENTICATION)
    assert chosen is ROUTE_TEMPLATES[FailureRoute.AUTHENTICATION]
    assert chosen.cta is CallToAction.COMPLETE_AUTHENTICATION


@pytest.mark.parametrize(
    ("code", "expected_cta"),
    [
        (DeclineCode.INSUFFICIENT_FUNDS.value, CallToAction.ENSURE_SUFFICIENT_BALANCE),
        (DeclineCode.CARD_EXPIRED.value, CallToAction.UPDATE_PAYMENT_METHOD),
        (DeclineCode.ACCOUNT_CLOSED.value, CallToAction.UPDATE_PAYMENT_METHOD),
        (DeclineCode.AFA_REQUIRED.value, CallToAction.COMPLETE_AUTHENTICATION),
    ],
)
def test_the_remedy_matches_the_failure(code: str, expected_cta: CallToAction) -> None:
    route = (
        FailureRoute.AUTHENTICATION
        if code == DeclineCode.AFA_REQUIRED.value
        else (
            FailureRoute.RETRY
            if code == DeclineCode.INSUFFICIENT_FUNDS.value
            else FailureRoute.DUNNING
        )
    )
    rendered = render_template({**context_for(code, route), "_route": route.value})
    assert rendered.call_to_action is expected_cta


def test_messages_differ_meaningfully_across_failure_classes() -> None:
    """The claim the whole classification exists to earn.

    Bodies must differ, and so must the thing the customer is asked to do — two
    differently-worded messages asking for the same action would still be a
    failure to differentiate.
    """
    cases = {
        DeclineCode.INSUFFICIENT_FUNDS.value: FailureRoute.RETRY,
        DeclineCode.CARD_EXPIRED.value: FailureRoute.DUNNING,
        DeclineCode.AFA_REQUIRED.value: FailureRoute.AUTHENTICATION,
        DeclineCode.PRE_DEBIT_NOTICE_MISSING.value: FailureRoute.COMPLIANCE,
    }
    drafts = {
        code: render_template({**context_for(code, route), "_route": route.value})
        for code, route in cases.items()
    }
    bodies = {d.body for d in drafts.values()}
    ctas = {d.call_to_action for d in drafts.values()}
    assert len(bodies) == len(cases), "every failure class must say something different"
    assert len(ctas) >= 3, "asking for the same action is not differentiation"


def test_the_compliance_message_admits_it_was_our_failure() -> None:
    """`PRE_DEBIT_NOTICE_MISSING` is our precondition, not the customer's problem."""
    rendered = render_template(
        {
            **context_for(
                DeclineCode.PRE_DEBIT_NOTICE_MISSING.value, FailureRoute.COMPLIANCE
            ),
            "_route": FailureRoute.COMPLIANCE.value,
        }
    )
    assert "we had not sent" in rendered.body.lower()


# ---------------------------------------------------------------------------
# The registered fallback contract
# ---------------------------------------------------------------------------


def test_the_task_is_registered_with_a_fallback() -> None:
    register_mandate_tasks()
    task = REGISTRY.get(TASK_DRAFT_DUNNING)
    assert task.fallback is not None
    assert task.prompt().version


def test_drafting_degrades_to_the_template_with_deterministic_provenance() -> None:
    """No key at all: the batch still produces an accurate message."""
    register_mandate_tasks()
    agent = LLMAgent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))
    context = context_for(DeclineCode.CARD_EXPIRED.value, FailureRoute.DUNNING)
    result = agent.run(TASK_DRAFT_DUNNING, context)

    assert result.degraded
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC
    assert result.provenance.model is None
    assert not result.abstained, "a message is a safe place to degrade, not to abstain"
    assert isinstance(result.output, DunningDraft)
    assert result.output.call_to_action is CallToAction.UPDATE_PAYMENT_METHOD
