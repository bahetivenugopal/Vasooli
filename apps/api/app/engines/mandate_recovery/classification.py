"""Failure classification for recurring debits.

**The shared classifier does the work.** `services/decline_taxonomy.py` already
answers "soft or hard, and what is the retry budget", and re-answering it here
would be the exact bug `CLAUDE.md` names: an engine that reimplements decision
logic. What this module adds is the part a one-off payment does not have — where
a *mandate* failure should be routed, given that a mandate carries obligations a
single charge does not.

Three failure modes only exist because the payment recurs:

- **Authentication** (`AFA_REQUIRED`, `rbi-mandate-rules:A5`). Distinct from
  insufficient funds in the way that matters: retrying without addressing the
  authentication reproduces the identical decline forever, so it must route to
  customer action rather than to blind retry.
- **Compliance** (`PRE_DEBIT_NOTICE_MISSING`, `rbi-mandate-rules:A2`). Not a
  customer failure at all — *our* precondition failed. The correct response is to
  send the notification and reschedule outside the window, never to hammer the
  retry.
- **Terminal mandate** (`MANDATE_REVOKED` / `MANDATE_EXPIRED`,
  `rbi-mandate-rules:A4`). An absolute stop, in every code path.

Every classification traces to a taxonomy row. A raw code the taxonomy does not
recognise goes to the shared `classify_unknown_decline` reasoning task and, if
that cannot answer confidently, is treated as **HARD** — the taxonomy's fail-safe.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.engines.mandate_recovery.config import NoticeConfig
from app.engines.mandate_recovery.schemas import (
    FailureClassification,
    FailureRoute,
    Mandate,
    MandateState,
    NoticeAssessment,
    NoticeStatus,
)
from app.models.enums import DeclineClass
from app.services.decline_taxonomy import (
    UNKNOWN_FAIL_SAFE_RULE,
    DeclineCode,
    Retryability,
    classify,
)
from app.services.llm_agent import LLMAgent
from app.services.reasoning_tasks import TASK_CLASSIFY_UNKNOWN_DECLINE

_HOUR = timedelta(hours=1)

#: Codes whose mandate route is fixed by the regulation rather than by the
#: taxonomy's own retryability column.
ROUTE_OVERRIDES: dict[DeclineCode, tuple[FailureRoute, str]] = {
    DeclineCode.AFA_REQUIRED: (FailureRoute.AUTHENTICATION, "rbi-mandate-rules:A5"),
    DeclineCode.PRE_DEBIT_NOTICE_MISSING: (FailureRoute.COMPLIANCE, "rbi-mandate-rules:A2"),
    DeclineCode.MANDATE_REVOKED: (FailureRoute.TERMINAL, "rbi-mandate-rules:A4"),
    DeclineCode.MANDATE_EXPIRED: (FailureRoute.TERMINAL, "rbi-mandate-rules:A4"),
}


def mandate_state(mandate: Mandate) -> MandateState:
    """Normalize the record's `status`, failing closed on anything unrecognised.

    An unknown status is `UNKNOWN`, which the scheduler treats as unevaluable and
    blocks. A status vocabulary that quietly widens is how a revoked mandate
    spelled differently starts receiving debits.
    """
    try:
        return MandateState(mandate.status)
    except ValueError:
        return MandateState.UNKNOWN


def assess_notice(mandate: Mandate, config: NoticeConfig) -> NoticeAssessment:
    """Evaluate the A2 pre-debit notification window from the record's timestamps.

    Derived, never read: the generator wrote the same label into its manifest, and
    an engine that looked it up there would have measured nothing. A negative lead
    — a notice sent *after* the debit it announces — is `LATE`, because a notice
    behind its own debit is not a pre-debit notice.
    """
    lead = mandate.notice_lead_hours
    status = NoticeStatus(config.classify_lead(lead))
    if status is NoticeStatus.ON_TIME:
        return NoticeAssessment(
            status=status,
            lead_hours=lead,
            compliant=True,
            rule_id=config.rule,
            rationale=(
                f"Pre-debit notice went out {lead:.1f}h before the scheduled debit, "
                f"inside the {config.min_lead / _HOUR:.0f}-{config.max_lead / _HOUR:.0f}h "
                "window A2 requires."
            ),
        )

    if status is NoticeStatus.MISSING:
        detail = "No pre-debit notice has been sent for the next scheduled debit."
        rule = config.rule
    elif status is NoticeStatus.LATE:
        detail = (
            f"Notice went out only {lead:.1f}h ahead of the debit, inside the "
            f"{config.min_lead / _HOUR:.0f}h minimum lead A2 requires."
        )
        rule = config.rule
    else:
        detail = (
            f"Notice went out {lead:.1f}h ahead, past the "
            f"{config.max_lead / _HOUR:.0f}h staleness ceiling — a notice that old is "
            "not the notice A2 asks for."
        )
        rule = "rbi-mandate-rules:PartB.notice_staleness"

    return NoticeAssessment(
        status=status, lead_hours=lead, compliant=False, rule_id=rule, rationale=detail
    )


def classify_failure(
    raw_code: str | None,
    *,
    agent: LLMAgent | None = None,
    context: dict[str, Any] | None = None,
) -> FailureClassification:
    """Classify one recurring-debit failure and say where it belongs.

    `agent` is consulted **only** when the raw code is not in the taxonomy. That
    is the one place a model adds anything here: everything the table already
    knows, the table answers, and paying for a reasoning call to re-derive a
    lookup would be rules dressed up as AI in the opposite direction.
    """
    if raw_code is None:
        return FailureClassification(
            decline_code="NONE",
            decline_class="NONE",
            route=FailureRoute.NONE,
            rule_id="decline-taxonomy:budget.SOFT",
            rationale="No failed debit in the current cycle; nothing to recover.",
        )

    known = _known_code(raw_code)
    reasoned = False
    if known is None:
        known, reasoned = _classify_unknown(raw_code, agent=agent, context=context or {})

    spec = classify(known)
    override = ROUTE_OVERRIDES.get(known)
    if override is not None:
        route, rule_id = override
    else:
        route, rule_id = _route_from_spec(spec.decline_class, spec.retryability), spec.rule_id

    if known is DeclineCode.UNKNOWN:
        # Whether a model was consulted or not, an unresolved reason is treated
        # as HARD under the taxonomy's fail-safe, and cites it. That is the rule
        # actually doing the work, not the UNKNOWN row's own budget.
        rule_id = UNKNOWN_FAIL_SAFE_RULE

    return FailureClassification(
        decline_code=known.value,
        decline_class=spec.decline_class.value,
        route=route,
        rule_id=rule_id,
        rationale=(
            f"{known.value} ({spec.decline_class.value}) -> {route.value}. "
            f"{spec.default_action}"
            + (
                f" Raw reason {raw_code!r} is not in the taxonomy; classified via "
                "the shared reasoning task."
                if reasoned
                else ""
            )
        ),
        reasoned=reasoned,
    )


def _route_from_spec(decline_class: DeclineClass, retryability: Retryability) -> FailureRoute:
    """Map a taxonomy row onto a mandate route.

    `CUSTOMER_PRESENT` is the interesting one: a code the taxonomy says needs the
    customer in the loop cannot be silently re-debited on a mandate, so it routes
    to a message rather than to a retry even though the class is SOFT.
    """
    if retryability is Retryability.NEVER:
        return FailureRoute.TERMINAL
    if retryability is Retryability.CUSTOMER_PRESENT:
        return FailureRoute.AUTHENTICATION
    if decline_class in {DeclineClass.SOFT, DeclineClass.AMBIGUOUS}:
        return FailureRoute.RETRY
    return FailureRoute.DUNNING


def _known_code(raw: str) -> DeclineCode | None:
    try:
        return DeclineCode(raw)
    except ValueError:
        return None


def _classify_unknown(
    raw: str, *, agent: LLMAgent | None, context: dict[str, Any]
) -> tuple[DeclineCode, bool]:
    """Route an unrecognised reason through the shared reasoning task.

    Returns the code to treat it as, and whether a model was actually consulted.
    With no agent — or with a result the wrapper degraded — the answer is the
    taxonomy's fail-safe: treat it as HARD. Defaulting anywhere else would mean
    defaulting to "keep charging this customer".

    Phase 3 flagged this task as unwired and left it to Engine 2. It is wired
    here, and on the committed mandate sample it fires **zero** times, because
    every code in that book is in the taxonomy. Reported as zero rather than
    quietly omitted — an unexercised path is worth saying out loud.
    """
    if agent is None:
        return DeclineCode.UNKNOWN, False
    result = agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, {"raw_reason": raw, **context})
    output = result.output
    assigned = getattr(output, "decline_class", None)
    if assigned is DeclineClass.SOFT and not result.degraded:
        # The only widening the task can do, and it stops at AMBIGUOUS: an
        # unrecognised reason the model believes is soft still gets the reduced
        # budget, because "the model thought so" is not the same evidence as a
        # verified taxonomy row.
        return DeclineCode.DO_NOT_HONOUR, True
    return DeclineCode.UNKNOWN, not result.degraded
