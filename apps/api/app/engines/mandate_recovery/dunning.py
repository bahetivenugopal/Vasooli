"""Dunning drafting — the reasoning task, its tone gate, and its fallback.

Three separable things happen to a customer message, in this order, and keeping
them separate is what makes the result defensible:

1. **Drafting** (`llm_agent.run`). The model writes the message and recommends
   what should happen next. Judgment.
2. **Tone validation** (`policy-bounds` TN1-TN3). Checked against the produced
   text, not requested in the prompt. A constraint that lives only in a prompt is
   a suggestion, and this is the point where a fluent model reaches for pressure
   it has no standing to apply.
3. **Permission** (`policy_engine.authorize`). Quiet hours, the contact cap, hard
   stops, and whether the recommendation is inside the envelope at all.

The registered deterministic fallback is a **per-failure-class template with
slot-filled variables**. This is the safest fallback in the project: a plain,
accurate message saying what happened and what the customer can do costs almost
nothing in quality, so degrading to boring here is entirely acceptable. It is
also the replacement a tone rejection falls back to, so a rejected draft never
leaves the customer with nothing.
"""

from __future__ import annotations

import re
from typing import Any

from app.engines.mandate_recovery.schemas import (
    CallToAction,
    Channel,
    DunningDraft,
    DunningRecommendation,
    FailureRoute,
    ToneViolation,
)
from app.services.decline_taxonomy import DeclineCode
from app.services.llm_agent import (
    REGISTRY,
    FallbackResult,
    ReasoningTask,
    TaskRegistry,
)

TASK_DRAFT_DUNNING = "draft_dunning_message"

#: The citation a message carries when the model drafted it.
DUNNING_RULE = "policy-bounds:TN2"
#: The citation a templated message carries.
TEMPLATE_RULE = "policy-bounds:TN3"


# ---------------------------------------------------------------------------
# Tone validation — policy-bounds TN1 / TN2
# ---------------------------------------------------------------------------

#: TN1. Phrases asserting a consequence Vasooli will not carry out, or a deadline
#: no rule imposes. Matched case-insensitively on word boundaries. The list is
#: deliberately concrete rather than clever: a fuzzy "sounds threatening" check
#: would fail unpredictably, and an unpredictable gate is not a gate.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\blegal action\b", "threatens legal action"),
    (r"\blawsuit\b|\bsue\b|\bsued\b", "threatens litigation"),
    (r"\bcollection(s)? agency\b|\bdebt collector\b", "threatens collections"),
    (r"\bcredit (score|bureau|report)\b|\bcibil\b", "threatens credit reporting"),
    (r"\bblacklist(ed)?\b", "threatens blacklisting"),
    (r"\bpenalt(y|ies)\b|\blate fee(s)?\b|\bfine\b", "asserts a charge that does not exist"),
    (r"\bsuspend(ed)?\b|\bterminat(e|ed|ion)\b|\bdeactivat(e|ed)\b", "threatens service loss"),
    (r"\bfinal (notice|warning|reminder)\b", "manufactures a terminal deadline"),
    (r"\bimmediately\b|\bact now\b|\burgent(ly)?\b", "manufactures urgency"),
    (r"\bwithin 24 hours\b|\blast chance\b", "manufactures a deadline"),
)

#: TN2. The remedy each failure class actually admits. A draft asking for
#: something else is describing a different failure than the one that happened.
PERMITTED_CTA: dict[FailureRoute, frozenset[CallToAction]] = {
    FailureRoute.RETRY: frozenset(
        {
            CallToAction.ENSURE_SUFFICIENT_BALANCE,
            CallToAction.PAY_VIA_LINK,
            CallToAction.UPDATE_PAYMENT_METHOD,
        }
    ),
    FailureRoute.DUNNING: frozenset(
        {
            CallToAction.UPDATE_PAYMENT_METHOD,
            CallToAction.PAY_VIA_LINK,
            CallToAction.CONTACT_SUPPORT,
        }
    ),
    FailureRoute.AUTHENTICATION: frozenset(
        {
            CallToAction.COMPLETE_AUTHENTICATION,
            CallToAction.REAUTHORIZE_MANDATE,
            CallToAction.PAY_VIA_LINK,
        }
    ),
    FailureRoute.COMPLIANCE: frozenset(
        {
            CallToAction.ENSURE_SUFFICIENT_BALANCE,
            CallToAction.CONTACT_SUPPORT,
        }
    ),
}


def validate_tone(draft: DunningDraft, route: FailureRoute) -> list[ToneViolation]:
    """Check a draft against the TN constraints. Empty means it may proceed.

    Returns every violation rather than the first, so the audit entry can say
    what was wrong with the message instead of only that something was.
    """
    violations: list[ToneViolation] = []
    text = f"{draft.subject}\n{draft.body}"
    for pattern, detail in FORBIDDEN_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            violations.append(
                ToneViolation(
                    rule_id="policy-bounds:TN1",
                    detail=f"{detail} ({match.group(0)!r})",
                )
            )

    permitted = PERMITTED_CTA.get(route)
    if permitted is not None and draft.call_to_action not in permitted:
        violations.append(
            ToneViolation(
                rule_id="policy-bounds:TN2",
                detail=(
                    f"call to action {draft.call_to_action.value!r} cannot resolve a "
                    f"{route.value} failure; permitted here: "
                    f"{sorted(c.value for c in permitted)}"
                ),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# The deterministic fallback — one template per failure class
# ---------------------------------------------------------------------------


class _Template:
    """One per-failure-class message, with its channel and its call to action."""

    def __init__(
        self,
        *,
        cause: str,
        remedy: str,
        cta: CallToAction,
        channel: Channel = Channel.EMAIL,
    ) -> None:
        self.cause = cause
        self.remedy = remedy
        self.cta = cta
        self.channel = channel

    def render(self, context: dict[str, Any]) -> DunningDraft:
        name = str(context.get("customer_name") or "there")
        amount = str(context.get("amount_rupees") or "the scheduled amount")
        return DunningDraft(
            channel=self.channel,
            subject=f"Your {context.get('frequency', 'recurring')} payment of {amount} did not go through",
            body=(
                f"Hi {name}, we tried to collect {amount} for your "
                f"{context.get('frequency', 'recurring')} subscription and it did not "
                f"go through. {self.cause} {self.remedy} "
                "If you have already sorted this out, no action is needed."
            ),
            call_to_action=self.cta,
            recommended_action=DunningRecommendation.SEND_DUNNING,
            confidence=0.0,
            reasoning=(
                "Templated message for this failure class. The reasoning layer was "
                "unavailable or its draft was rejected, so the system said the "
                "accurate, boring thing rather than guessing at a better one."
            ),
        )


#: Keyed by normalized decline code, with a per-route default behind it. Every
#: template names the real cause and asks for one completable thing — TN2, in a
#: form that cannot drift, which is why this is a safe place to degrade to.
TEMPLATES: dict[str, _Template] = {
    DeclineCode.INSUFFICIENT_FUNDS.value: _Template(
        cause="Your bank reported insufficient balance at the time of the debit.",
        remedy="If you top up before the next scheduled attempt, we will retry automatically.",
        cta=CallToAction.ENSURE_SUFFICIENT_BALANCE,
        channel=Channel.SMS,
    ),
    DeclineCode.TRANSACTION_LIMIT_EXCEEDED.value: _Template(
        cause="The debit exceeded a limit set on your account for this kind of payment.",
        remedy="Raising that limit with your bank, or leaving it to the next cycle, will let the retry through.",
        cta=CallToAction.ENSURE_SUFFICIENT_BALANCE,
    ),
    DeclineCode.CARD_EXPIRED.value: _Template(
        cause="The card saved for this subscription has expired.",
        remedy="Adding a current card will let the next collection run normally.",
        cta=CallToAction.UPDATE_PAYMENT_METHOD,
    ),
    DeclineCode.ACCOUNT_CLOSED.value: _Template(
        cause="The bank account behind this mandate is no longer open.",
        remedy="Adding a different payment method will restore the subscription.",
        cta=CallToAction.UPDATE_PAYMENT_METHOD,
    ),
    DeclineCode.INVALID_ACCOUNT.value: _Template(
        cause="The account details on this mandate were not recognised by the bank.",
        remedy="Correcting or replacing the payment method will let the next collection run.",
        cta=CallToAction.UPDATE_PAYMENT_METHOD,
    ),
    DeclineCode.CARD_DISABLED_ONLINE.value: _Template(
        cause="Your card is currently blocked for online payments by your bank.",
        remedy="Enabling online payments, or adding another method, will let the next collection run.",
        cta=CallToAction.UPDATE_PAYMENT_METHOD,
    ),
    DeclineCode.AFA_REQUIRED.value: _Template(
        cause=(
            "This amount needs you to approve the payment yourself, which the "
            "automatic collection cannot do on its own."
        ),
        remedy="Completing the one-time approval will let this cycle be collected.",
        cta=CallToAction.COMPLETE_AUTHENTICATION,
    ),
    DeclineCode.DO_NOT_HONOUR.value: _Template(
        cause="Your bank declined the debit without giving a reason.",
        remedy=(
            "A retry is scheduled; if it does not clear, checking with your bank or "
            "using another payment method will resolve it."
        ),
        cta=CallToAction.PAY_VIA_LINK,
    ),
    DeclineCode.ISSUER_UNAVAILABLE.value: _Template(
        cause="Your bank's systems were not reachable when we tried to collect.",
        remedy="Nothing is wrong at your end; a retry is scheduled for a later window.",
        cta=CallToAction.ENSURE_SUFFICIENT_BALANCE,
        channel=Channel.SMS,
    ),
}

#: Used when the code has no template of its own. Keyed by route so the fallback
#: still differentiates by *kind* of failure rather than collapsing to one message.
ROUTE_TEMPLATES: dict[FailureRoute, _Template] = {
    FailureRoute.RETRY: _Template(
        cause="The payment was declined for a reason that usually clears on its own.",
        remedy="A retry is scheduled inside the permitted window; no action is needed unless it fails again.",
        cta=CallToAction.ENSURE_SUFFICIENT_BALANCE,
        channel=Channel.SMS,
    ),
    FailureRoute.DUNNING: _Template(
        cause="The payment method saved for this subscription can no longer be charged.",
        remedy="Adding a different payment method will restore the subscription.",
        cta=CallToAction.UPDATE_PAYMENT_METHOD,
    ),
    FailureRoute.AUTHENTICATION: _Template(
        cause="This collection needs you to approve it yourself before it can go through.",
        remedy="Completing the approval step will let this cycle be collected.",
        cta=CallToAction.COMPLETE_AUTHENTICATION,
    ),
    FailureRoute.COMPLIANCE: _Template(
        cause=(
            "We had not sent you the advance notice this debit requires, so we did "
            "not attempt it."
        ),
        remedy="The notice is on its way and the collection will be rescheduled after it.",
        cta=CallToAction.CONTACT_SUPPORT,
    ),
}


def template_for(decline_code: str, route: FailureRoute) -> _Template:
    """The registered template for a failure, most specific first.

    The route wins over the code when the two disagree, and they do disagree in
    a case that actually occurs: an above-threshold insufficient-funds failure is
    re-routed to authentication by `rbi-mandate-rules:A3`, and the
    insufficient-funds template would tell that customer to top up their balance —
    a remedy that cannot unblock them. `policy-bounds:TN2` would reject it, which
    is the right answer but a wasteful way to arrive at it: the fallback is the
    thing a rejection falls back *to*, so it must satisfy the constraints itself.
    """
    template = TEMPLATES.get(decline_code)
    permitted = PERMITTED_CTA.get(route)
    if template is not None and (permitted is None or template.cta in permitted):
        return template
    return ROUTE_TEMPLATES.get(route, ROUTE_TEMPLATES[FailureRoute.DUNNING])


def render_template(context: dict[str, Any]) -> DunningDraft:
    """The templated message for this failure. Exposed for direct testing."""
    route = FailureRoute(context.get("_route") or FailureRoute.DUNNING.value)
    return template_for(str(context.get("decline_code") or ""), route).render(context)


def _draft_dunning_fallback(context: dict[str, Any]) -> FallbackResult:
    """The registered deterministic fallback for `draft_dunning_message`.

    Not an abstention. A message is one of the few places where the rule-based
    answer is nearly as good as the reasoned one — the customer needs to know
    what happened and what to do, and a template says both accurately. Abstaining
    here would cost a real recovery to avoid a small quality loss.
    """
    draft = render_template(context)
    return FallbackResult(
        output=draft,
        abstained=False,
        rationale=f"templated {draft.call_to_action.value} message for {context.get('decline_code')}",
        confidence=0.0,
    )


def build_context(
    *,
    mandate_id: str,
    customer_name: str,
    amount_paise: int,
    frequency: str,
    decline_code: str,
    decline_class: str,
    taxonomy_action: str,
    route: FailureRoute,
    attempts_used: int,
    attempts_allowed: int,
    last_attempt_at: str,
    notice_status: str,
    afa_side: str,
    communication_reason: str,
    required_action: str,
) -> dict[str, Any]:
    """Everything the prompt needs, plus what the fallback needs to template.

    One context object feeds both paths. If the model and the template saw
    different evidence, a fallback result and a model result would not be
    comparable, and the per-source metric split would mean nothing.
    """
    return {
        "mandate_id": mandate_id,
        "customer_name": customer_name,
        "amount_rupees": f"Rs {amount_paise / 100:,.2f}",
        "frequency": frequency,
        "decline_code": decline_code,
        "decline_class": decline_class,
        "taxonomy_action": taxonomy_action,
        "attempts_used": attempts_used,
        "attempts_allowed": attempts_allowed,
        "last_attempt_at": last_attempt_at,
        "notice_status": notice_status,
        "afa_side": afa_side,
        "communication_reason": communication_reason,
        "required_action": required_action,
        # Not rendered into the prompt — carried so the fallback templates the
        # same failure the model was shown.
        "_route": route.value,
    }


def register_mandate_tasks(registry: TaskRegistry = REGISTRY) -> None:
    """Register Engine 2's reasoning tasks.

    Called from the app lifespan alongside the other engines'. Registration loads
    the prompt file eagerly, so a missing or unversioned prompt fails at boot
    rather than mid-batch.
    """
    from app.engines.mandate_recovery.config import default_config

    registry.register(
        ReasoningTask(
            name=TASK_DRAFT_DUNNING,
            prompt_name=TASK_DRAFT_DUNNING,
            response_model=DunningDraft,
            fallback=_draft_dunning_fallback,
            description=(
                "Draft the customer message for a failed recurring debit, and "
                "recommend what the system should do next about the mandate."
            ),
            min_confidence=default_config().dunning.min_confidence,
        )
    )


__all__ = [
    "DUNNING_RULE",
    "FORBIDDEN_PATTERNS",
    "PERMITTED_CTA",
    "TASK_DRAFT_DUNNING",
    "TEMPLATE_RULE",
    "build_context",
    "register_mandate_tasks",
    "render_template",
    "template_for",
    "validate_tone",
]
