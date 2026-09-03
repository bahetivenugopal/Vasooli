"""The decline taxonomy as code.

A lookup table, nothing more: normalized reason code -> class -> retryability ->
retry budget. It lives in the shared core because three separate consumers need
exactly the same answer — `policy_engine.py` for retry eligibility,
`razorpay_client.py` for boundary normalization, and later the engines
themselves.

Source of truth: the `decline-taxonomy` skill. Every row here corresponds to a
row there. Adding a code means adding it to the skill first — a code with no
documented rationale is an invented threshold wearing a constant's clothes.

The one rule the whole table exists to encode: **an unrecognised reason never
defaults to SOFT.** Defaulting to soft means defaulting to "keep charging this
customer", which is the exact failure mode the taxonomy prevents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.enums import DeclineClass


class DeclineCode(StrEnum):
    """Normalized, internal reason codes. Engines branch on these only.

    Raw upstream strings are normalized once, at the boundary, inside
    `razorpay_client.py`. An engine that sees `insufficient_fund` instead of
    `INSUFFICIENT_FUNDS` means the boundary leaked.
    """

    # SOFT — retry is appropriate
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    ISSUER_UNAVAILABLE = "ISSUER_UNAVAILABLE"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    TRANSACTION_LIMIT_EXCEEDED = "TRANSACTION_LIMIT_EXCEEDED"
    AUTH_TIMEOUT = "AUTH_TIMEOUT"
    CUSTOMER_CANCELLED = "CUSTOMER_CANCELLED"

    # HARD — retrying is wrong
    CARD_EXPIRED = "CARD_EXPIRED"
    CARD_LOST_OR_STOLEN = "CARD_LOST_OR_STOLEN"
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    INVALID_ACCOUNT = "INVALID_ACCOUNT"
    INTERNATIONAL_NOT_ALLOWED = "INTERNATIONAL_NOT_ALLOWED"
    CARD_DISABLED_ONLINE = "CARD_DISABLED_ONLINE"
    FRAUD_SUSPECTED = "FRAUD_SUSPECTED"

    # TERMINAL_MANDATE — invalidates the schedule, not just the attempt
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"

    # AMBIGUOUS — the issuer declined without saying why
    DO_NOT_HONOUR = "DO_NOT_HONOUR"
    TECHNICAL_DECLINE = "TECHNICAL_DECLINE"

    # POLICY_BLOCK — not an issuer decline at all; our own precondition failed
    AFA_REQUIRED = "AFA_REQUIRED"
    PRE_DEBIT_NOTICE_MISSING = "PRE_DEBIT_NOTICE_MISSING"

    # The fail-safe bucket
    UNKNOWN = "UNKNOWN"


class Retryability(StrEnum):
    """Whether, and how, a decline may be retried.

    `CUSTOMER_PRESENT` is its own value rather than a flavour of `YES` because
    the difference is load-bearing: a silent server-side retry of an
    `AUTH_TIMEOUT` reproduces the same decline forever.
    """

    YES = "yes"
    CUSTOMER_PRESENT = "customer_present"
    NO = "no"
    NEVER = "never"


@dataclass(frozen=True)
class DeclineSpec:
    """One row of the taxonomy."""

    code: DeclineCode
    decline_class: DeclineClass
    retryability: Retryability
    default_action: str
    #: Citation for the audit trail, in `<skill>:<rule-id>` form.
    rule_id: str
    #: True when the original attempt may have landed and must be reconciled
    #: before any retry. Retrying an unreconciled timeout can double-charge.
    reconcile_first: bool = False
    #: True when only a human may decide what happens next.
    human_review: bool = False


def _spec(
    code: DeclineCode,
    decline_class: DeclineClass,
    retryability: Retryability,
    default_action: str,
    *,
    reconcile_first: bool = False,
    human_review: bool = False,
) -> DeclineSpec:
    return DeclineSpec(
        code=code,
        decline_class=decline_class,
        retryability=retryability,
        default_action=default_action,
        rule_id=f"decline-taxonomy:{decline_class.value}.{code.value}",
        reconcile_first=reconcile_first,
        human_review=human_review,
    )


TAXONOMY: dict[DeclineCode, DeclineSpec] = {
    s.code: s
    for s in (
        # --- SOFT ---------------------------------------------------------
        _spec(
            DeclineCode.INSUFFICIENT_FUNDS,
            DeclineClass.SOFT,
            Retryability.YES,
            "Retry in a later window. Payday-aware spacing beats re-hammering.",
        ),
        _spec(
            DeclineCode.ISSUER_UNAVAILABLE,
            DeclineClass.SOFT,
            Retryability.YES,
            "Retry, and emit a corridor-health signal — a spike here is the "
            "classic corridor-wide failure, not a customer problem.",
        ),
        _spec(
            DeclineCode.GATEWAY_TIMEOUT,
            DeclineClass.SOFT,
            Retryability.YES,
            "Reconcile first — the original attempt may have succeeded late.",
            reconcile_first=True,
        ),
        _spec(
            DeclineCode.NETWORK_ERROR,
            DeclineClass.SOFT,
            Retryability.YES,
            "Short backoff, then retry. Reconcile first: the debit may have landed.",
            reconcile_first=True,
        ),
        _spec(
            DeclineCode.TRANSACTION_LIMIT_EXCEEDED,
            DeclineClass.SOFT,
            Retryability.YES,
            "Retry in the next window, never same-day — a same-day retry hits the same cap.",
        ),
        _spec(
            DeclineCode.AUTH_TIMEOUT,
            DeclineClass.SOFT,
            Retryability.CUSTOMER_PRESENT,
            "Needs a customer-present attempt (fresh link or notification), never a silent retry.",
        ),
        _spec(
            DeclineCode.CUSTOMER_CANCELLED,
            DeclineClass.SOFT,
            Retryability.CUSTOMER_PRESENT,
            "Intent, not capability, is the blocker. A fresh payment link beats a re-charge.",
        ),
        # --- HARD ---------------------------------------------------------
        _spec(
            DeclineCode.CARD_EXPIRED,
            DeclineClass.HARD,
            Retryability.NO,
            "Dunning: request an updated instrument.",
        ),
        _spec(
            DeclineCode.CARD_LOST_OR_STOLEN,
            DeclineClass.HARD,
            Retryability.NO,
            "Stop. Do not dun for the same instrument. Flag for review.",
            human_review=True,
        ),
        _spec(
            DeclineCode.ACCOUNT_CLOSED,
            DeclineClass.HARD,
            Retryability.NO,
            "Dunning: request a new instrument.",
        ),
        _spec(
            DeclineCode.INVALID_ACCOUNT,
            DeclineClass.HARD,
            Retryability.NO,
            "Dunning: request corrected details.",
        ),
        _spec(
            DeclineCode.INTERNATIONAL_NOT_ALLOWED,
            DeclineClass.HARD,
            Retryability.NO,
            "Do not retry the same corridor. Reroute, or request another instrument.",
        ),
        _spec(
            DeclineCode.CARD_DISABLED_ONLINE,
            DeclineClass.HARD,
            Retryability.NO,
            "Only the cardholder's bank can lift this. Dunning: request another instrument.",
        ),
        _spec(
            DeclineCode.FRAUD_SUSPECTED,
            DeclineClass.HARD,
            Retryability.NEVER,
            "Terminal. Never auto-retry, never auto-dun. Human review only.",
            human_review=True,
        ),
        # --- TERMINAL_MANDATE ---------------------------------------------
        _spec(
            DeclineCode.MANDATE_REVOKED,
            DeclineClass.TERMINAL_MANDATE,
            Retryability.NEVER,
            "Immediate hard stop. Cancel the entire retry schedule. Absolute (RBI A4).",
        ),
        _spec(
            DeclineCode.MANDATE_EXPIRED,
            DeclineClass.TERMINAL_MANDATE,
            Retryability.NEVER,
            "Hard stop. Re-registration with fresh AFA required (RBI A4).",
        ),
        # --- AMBIGUOUS ----------------------------------------------------
        _spec(
            DeclineCode.DO_NOT_HONOUR,
            DeclineClass.AMBIGUOUS,
            Retryability.YES,
            "Issuer declined without stating why. Reduced retry budget; if it "
            "repeats across attempts, reclassify as hard.",
        ),
        _spec(
            DeclineCode.TECHNICAL_DECLINE,
            DeclineClass.AMBIGUOUS,
            Retryability.YES,
            "Generic issuer-side technical refusal. Reduced retry budget.",
        ),
        # --- POLICY_BLOCK -------------------------------------------------
        _spec(
            DeclineCode.AFA_REQUIRED,
            DeclineClass.POLICY_BLOCK,
            Retryability.CUSTOMER_PRESENT,
            "Trigger customer-present authentication. A silent retry reproduces "
            "this decline forever (RBI A5).",
        ),
        _spec(
            DeclineCode.PRE_DEBIT_NOTICE_MISSING,
            DeclineClass.POLICY_BLOCK,
            Retryability.NO,
            "Our own precondition failed. Send the notice, wait out the window, "
            "then attempt (RBI A2).",
        ),
        # --- UNKNOWN — the fail-safe path ----------------------------------
        _spec(
            DeclineCode.UNKNOWN,
            DeclineClass.UNKNOWN,
            Retryability.NO,
            "Unrecognised reason. Classify via the reasoning layer with the "
            "taxonomy in context; treat as HARD if confidence is low or the "
            "provider is unavailable.",
        ),
    )
}

#: Retry budgets by class, including the original attempt. Vasooli policy
#: choices, documented in the `decline-taxonomy` skill so they can be
#: challenged rather than merely obeyed. Mandate flows are additionally bounded
#: by `rbi-mandate-rules` Part B, which is stricter and therefore wins.
RETRY_BUDGET: dict[DeclineClass, int] = {
    DeclineClass.SOFT: 4,
    DeclineClass.AMBIGUOUS: 2,
    DeclineClass.HARD: 1,
    DeclineClass.TERMINAL_MANDATE: 1,
    DeclineClass.POLICY_BLOCK: 0,
    # An unknown code is treated as HARD until something classifies it.
    DeclineClass.UNKNOWN: 1,
}

BUDGET_RULE_ID: dict[DeclineClass, str] = {
    cls: f"decline-taxonomy:budget.{cls.value}" for cls in RETRY_BUDGET
}

#: The `UNKNOWN` fail-safe, cited whenever a decline is treated as hard because
#: nothing could classify it confidently.
UNKNOWN_FAIL_SAFE_RULE = "decline-taxonomy:UNKNOWN.fail_safe"


def classify(code: DeclineCode | str) -> DeclineSpec:
    """Look up a normalized code. Anything unrecognised becomes `UNKNOWN`.

    Note the direction of the default: unknown resolves to the `UNKNOWN` spec,
    which carries a HARD-equivalent budget of 1 and no retryability. Failing
    closed is the whole point.
    """
    try:
        return TAXONOMY[DeclineCode(code)]
    except ValueError:
        return TAXONOMY[DeclineCode.UNKNOWN]


def retry_budget(code: DeclineCode | str) -> int:
    """Total permitted attempts for this code, original attempt included."""
    return RETRY_BUDGET[classify(code).decline_class]


def is_terminal_class(code: DeclineCode | str) -> bool:
    """Does this code end the schedule rather than just the attempt?"""
    spec = classify(code)
    return spec.retryability is Retryability.NEVER
