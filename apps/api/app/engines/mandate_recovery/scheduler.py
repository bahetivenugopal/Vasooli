"""The compliance-aware retry scheduler.

The centerpiece of Engine 2, and the module that has to be right. Anyone can
write a retry loop; this one answers, for any mandate, **what it will do next,
when, and which rule permits it** — as a returned object, not a log line.

Every scheduling decision is authorised by `policy_engine.py`. This module
decides *what to propose and when*; it never decides what is allowed. Two
consequences worth stating because they are easy to get wrong:

- **The proposed debit time is what gets evaluated**, not the wall clock. A retry
  is a scheduled future debit, so the notice window, the 24h Part B spacing and
  the escalating backoff are all measured against the moment the debit would
  actually fire.
- **A compliance block is not a dead end.** A2 says the response to a missing
  notice is to *send the notice and reschedule beyond the window* — so the
  scheduler works out when a compliant notice could go out, and proposes that,
  rather than treating the mandate as unrecoverable.

One judgment worth naming, because the regulation does not settle it: whether to
send a compliance notice for a debit that can never be permitted anyway. The
scheduler checks — `_would_be_permitted_with_notice()` — and does not. Sending a
pre-debit notice for a debit the attempt cap forbids spends the customer's
attention on an event that will not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.engines.mandate_recovery.classification import assess_notice, mandate_state
from app.engines.mandate_recovery.config import MandateConfig
from app.engines.mandate_recovery.schemas import (
    FailureClassification,
    FailureRoute,
    Mandate,
    MandateState,
    NextStep,
    NoticeAssessment,
    ScheduleExplanation,
)
from app.models.entity import RecoverableEntity
from app.models.enums import Action, Engine, EntityType, Outcome
from app.services.decline_taxonomy import DeclineCode
from app.services.policy_engine import (
    OUTREACH_TZ,
    PolicyDecision,
    PolicyEngine,
    PolicyRequest,
)

#: Rules whose denial means "a compliance gate refused", as opposed to "the
#: budget is spent" or "the instrument is dead". Held in one place so the summary
#: and the tests cannot disagree about what a compliance block is.
COMPLIANCE_RULES: frozenset[str] = frozenset(
    {
        "rbi-mandate-rules:A1",
        "rbi-mandate-rules:A2",
        "rbi-mandate-rules:A3",
        "rbi-mandate-rules:A5",
        "rbi-mandate-rules:PartB.notice_staleness",
        "rbi-mandate-rules:PartB.mandate_cap",
        "rbi-mandate-rules:PartB.paused",
    }
)

#: Routes whose correct response is a message rather than a debit.
COMMUNICATION_ROUTES: frozenset[FailureRoute] = frozenset(
    {FailureRoute.DUNNING, FailureRoute.AUTHENTICATION}
)


@dataclass(frozen=True)
class ScheduleDecision:
    """The scheduler's full answer about one mandate.

    Carries the `PolicyDecision` alongside the explanation so the caller records
    the gate's own citation rather than a paraphrase of it.
    """

    mandate: Mandate
    classification: FailureClassification
    notice: NoticeAssessment
    explanation: ScheduleExplanation
    decision: PolicyDecision
    #: When the debit would fire, if one is proposed.
    proposed_debit_at: datetime | None = None
    #: When a compliant pre-debit notice would go out, if one is needed.
    proposed_notice_at: datetime | None = None
    #: True when the debit was refused permanently rather than deferred — a
    #: retry a blind system would have spent.
    suppressed: bool = False


def mandate_entity(mandate: Mandate, *, terminal_reason: str | None = None) -> RecoverableEntity:
    """The mandate as something the policy engine can reason about.

    `attempt_count` is the failures already made **in the current cycle**, which
    is what `rbi-mandate-rules` Part B caps. Counting the mandate's whole history
    would exhaust the budget of every long-lived subscription on its first bad
    month.
    """
    state = mandate_state(mandate)
    is_terminal = state in {MandateState.REVOKED, MandateState.EXPIRED}
    reason = terminal_reason
    if is_terminal and reason is None:
        reason = (
            DeclineCode.MANDATE_REVOKED.value
            if state is MandateState.REVOKED
            else DeclineCode.MANDATE_EXPIRED.value
        )
    last = mandate.latest_failure
    return RecoverableEntity(
        entity_id=mandate.mandate_id,
        entity_type=EntityType.MANDATE,
        amount_paise=mandate.amount_paise,
        currency=mandate.currency,
        status=mandate.status,
        attempt_count=mandate.attempts_in_current_cycle,
        last_attempt_at=last.attempted_at if last else None,
        is_terminal=is_terminal,
        terminal_reason=reason,
    )


def outreach_entity(
    mandate: Mandate,
    *,
    messages_sent: int = 0,
    last_message_at: datetime | None = None,
) -> RecoverableEntity:
    """The mandate as the gate should see it for a **customer message**.

    The difference from `mandate_entity()` is the whole reason this exists: the
    "attempt" being bounded for outreach is the *contact*, not the charge. A
    message is not a debit, so subjecting it to the debit's retry budget and its
    24h Part B spacing would silence the dunning path on exactly the mandates that
    most need it — a hard decline has spent its debit budget by definition, and
    telling the customer their card died is the entire remaining recovery.

    What bounds outreach instead is `policy-bounds` QH1 and QH2: the 09:00-21:00
    window, and at most three messages per entity per rolling seven days. Passing
    the message count as `attempt_count` is what puts the contact cap in front of
    the same machinery, so a fourth message is refused by a cap rather than by a
    convention someone has to remember.

    The terminal flag is carried across unchanged. A revoked mandate receives no
    debit *and* no message.
    """
    base = mandate_entity(mandate)
    return base.model_copy(
        update={"attempt_count": messages_sent, "last_attempt_at": last_message_at}
    )


class RetryScheduler:
    """Decides the next bounded step for one mandate, and says why."""

    def __init__(self, *, policy: PolicyEngine, config: MandateConfig) -> None:
        self._policy = policy
        self._config = config

    # --- the entry point ---------------------------------------------------

    def plan(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        *,
        now: datetime,
    ) -> ScheduleDecision:
        """Work out what happens next to this mandate.

        Order mirrors `rbi-mandate-rules` -> Preconditions, with revocation first
        because it short-circuits everything else.
        """
        notice = assess_notice(mandate, self._config.notice)
        state = mandate_state(mandate)

        if state is MandateState.UNKNOWN:
            return self._unevaluable(mandate, classification, notice, now)

        # A revoked or expired mandate, or a terminal decline. The policy engine
        # halts on the entity's own terminal flag before anything else runs, so
        # this is one call and not a special case in this module.
        if state in {MandateState.REVOKED, MandateState.EXPIRED} or (
            classification.route is FailureRoute.TERMINAL
        ):
            return self._halt(mandate, classification, notice, now)

        if classification.route is FailureRoute.NONE:
            return self._no_failure(mandate, classification, notice, now)

        if classification.route in COMMUNICATION_ROUTES:
            return self._communicate(mandate, classification, notice, now)

        # A retryable failure. Propose the debit at the scheduled time (or the
        # earliest compliant moment after `now`, whichever is later) and put it
        # in front of the gate.
        return self._propose_debit(mandate, classification, notice, now)

    # --- branches ----------------------------------------------------------

    def _unevaluable(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
    ) -> ScheduleDecision:
        """A status vocabulary we do not recognise. Fail closed, ask a human."""
        decision = self._policy.evaluate(
            PolicyRequest(
                engine=Engine.MANDATE_RECOVERY,
                entity=mandate_entity(mandate),
                proposed_action=Action.ATTEMPT_CHARGE,
                now=now,
                unevaluable_precondition=f"mandate status {mandate.status!r} is unrecognised",
            )
        )
        return self._decided(
            mandate,
            classification,
            notice,
            decision,
            step=NextStep.ESCALATE,
            scheduled_for=None,
            explanation=decision.rationale,
        )

    def _halt(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
    ) -> ScheduleDecision:
        """A4 / HS1. No retry, and — deliberately — no communication either.

        The phase brief permits "a single notice" urging re-authorization.
        `policy-bounds:HS1` permits none, and the stricter bound wins: doing less
        is always the safe direction, and a revoked mandate is exactly the case
        where a system chasing anyway is the embarrassing failure. Flagged in
        ADR 0008 as a judgment call rather than buried.
        """
        entity = mandate_entity(
            mandate,
            terminal_reason=(
                classification.decline_code
                if classification.route is FailureRoute.TERMINAL
                else None
            ),
        )
        decision = self._policy.evaluate(
            PolicyRequest(
                engine=Engine.MANDATE_RECOVERY,
                entity=entity,
                proposed_action=Action.ATTEMPT_CHARGE,
                now=now,
                decline_code=_code_or_none(classification.decline_code),
            )
        )
        return self._decided(
            mandate,
            classification,
            notice,
            decision,
            step=NextStep.HALT,
            scheduled_for=None,
            explanation=(
                f"{decision.rationale} No further debit and no further "
                "communication, in any code path."
            ),
        )

    def _no_failure(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
    ) -> ScheduleDecision:
        """Nothing failed this cycle. The A2 obligation still applies.

        A healthy mandate with no pre-debit notice for its upcoming debit is a
        compliance problem waiting to become a failure, and the notice is the
        cheapest possible fix. There is no money at risk here, and the runner
        books none.
        """
        if notice.compliant:
            return self._decided(
                mandate,
                classification,
                notice,
                self._permitted_noop(mandate, now),
                step=NextStep.NO_ACTION,
                scheduled_for=None,
                explanation=(
                    "No failed debit in the current cycle and the pre-debit notice "
                    f"for {mandate.next_debit_at:%Y-%m-%d %H:%M} UTC is inside the A2 "
                    "window. Nothing to do."
                ),
            )
        # Propose the debit anyway, so the A2 refusal is on the record with its
        # own citation rather than implied by the notice that follows it.
        denial = self._policy.evaluate(
            self._debit_request(mandate, classification, self._proposed_debit_at(mandate, now))
        )
        return self._schedule_notice(mandate, classification, notice, now, denial, at_risk=False)

    def _communicate(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
    ) -> ScheduleDecision:
        """A dead instrument or an authentication requirement.

        The debit is put in front of the gate anyway, and the refusal is the
        point: a hard decline that never receives a retry is a wasted attempt
        avoided, and counting those is only honest if the refusal exists in the
        trail with the rule that produced it.
        """
        proposed = self._proposed_debit_at(mandate, now)
        decision = self._policy.evaluate(self._debit_request(mandate, classification, proposed))
        step = (
            NextStep.REQUEST_AUTHENTICATION
            if classification.route is FailureRoute.AUTHENTICATION
            else NextStep.SEND_DUNNING
        )
        return self._decided(
            mandate,
            classification,
            notice,
            decision,
            step=step,
            scheduled_for=now,
            explanation=(
                f"{classification.rationale} The debit was refused by "
                f"{decision.rule_id}: {decision.rationale} Routing to the customer "
                "instead of spending retry budget that cannot succeed."
            ),
            proposed_debit_at=proposed,
            suppressed=True,
        )

    def _propose_debit(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
    ) -> ScheduleDecision:
        """The retryable case. Propose a debit; let the gate answer."""
        proposed = self._proposed_debit_at(mandate, now)
        request = self._debit_request(mandate, classification, proposed)
        decision = self._policy.evaluate(request)

        if decision.allowed:
            return self._decided(
                mandate,
                classification,
                notice,
                decision,
                step=NextStep.ATTEMPT_DEBIT,
                scheduled_for=proposed,
                explanation=(
                    f"Retry {mandate.attempts_in_current_cycle + 1} of "
                    f"{self._config.attempts.max_per_cycle} permitted this cycle, at "
                    f"{proposed:%Y-%m-%d %H:%M} UTC. Pre-debit notice went out "
                    f"{notice.lead_hours:.1f}h ahead, inside the A2 window. "
                    f"{decision.rationale}"
                ),
                proposed_debit_at=proposed,
            )

        # Denied. A notice problem is fixable and the debit is rescheduled; a
        # spent budget or a mandate-level block is not.
        if decision.rule_id in {"rbi-mandate-rules:A2", "rbi-mandate-rules:PartB.notice_staleness"}:
            return self._schedule_notice(
                mandate, classification, notice, now, decision, at_risk=True
            )

        if decision.earliest_next_attempt_at is not None:
            return self._decided(
                mandate,
                classification,
                notice,
                decision,
                step=NextStep.DEFER,
                scheduled_for=decision.earliest_next_attempt_at,
                explanation=(
                    f"{decision.rationale} The revenue is still recoverable; only the "
                    "timing was wrong, so the debit moves rather than stopping."
                ),
                proposed_debit_at=decision.earliest_next_attempt_at,
            )

        # A spent budget, a mandate-level compliance block, or a human trigger.
        return self._decided(
            mandate,
            _reroute(classification, decision),
            notice,
            decision,
            step=_follow_up_step(decision),
            scheduled_for=now,
            explanation=(
                f"{decision.rationale} Stopped retrying, handed on rather than "
                "silently abandoned."
            ),
            proposed_debit_at=proposed,
            suppressed=True,
        )

    def _schedule_notice(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        now: datetime,
        debit_denial: PolicyDecision,
        *,
        at_risk: bool,
    ) -> ScheduleDecision:
        """A2's prescribed response: send the notice, reschedule beyond the window.

        `debit_denial` is carried through to the recorded decision so the primary
        entry cites **the rule that refused the debit** — A2, or the staleness
        ceiling. Citing the notice's own permission instead would leave the trail
        unable to answer why the debit did not happen, which is the question the
        entry exists for.

        The debit moves to `notice_at + target_lead`, inside the window by
        construction. The notice is customer-facing, so quiet hours can hold it —
        and when they do the debit moves with it, because a notice that has not
        gone out is not a notice.
        """
        debit_at, notice_at = self._compliant_pair(mandate, now)

        viable, blocker = self._would_be_permitted_with_notice(mandate, classification, debit_at)
        if at_risk and not viable:
            # The debit this notice would announce can never fire — the budget is
            # spent, or a mandate-level gate refuses it outright. Sending the
            # notice anyway would spend a customer contact on an event that will
            # not happen. A *timing* block is not this case: there the debit is
            # still coming, so the notice is exactly what A2 asks for.
            return self._decided(
                mandate,
                _reroute(classification, blocker),
                notice,
                blocker,
                step=_follow_up_step(blocker),
                scheduled_for=now,
                explanation=(
                    f"{notice.rationale} A compliant notice would not help: the debit "
                    f"it announced would still be refused by {blocker.rule_id}. "
                    f"{blocker.rationale} Routing to the customer instead."
                ),
                suppressed=True,
            )

        quiet = self._policy.evaluate(
            PolicyRequest(
                engine=Engine.MANDATE_RECOVERY,
                entity=outreach_entity(mandate),
                proposed_action=Action.SEND_PRE_DEBIT_NOTICE,
                now=notice_at,
                is_outreach=True,
            )
        )
        if not quiet.allowed and quiet.earliest_next_attempt_at is not None:
            # Quiet hours. The notice moves to the next opening, and the debit
            # moves with it so the A2 lead is preserved rather than quietly lost.
            notice_at = quiet.earliest_next_attempt_at
            debit_at = notice_at + self._config.notice.target_lead

        return self._decided(
            mandate,
            classification,
            notice,
            debit_denial,
            step=NextStep.SEND_PRE_DEBIT_NOTICE,
            scheduled_for=notice_at,
            explanation=(
                f"{notice.rationale} A2's response is to notify and reschedule, not "
                f"to attempt: notice at {notice_at:%Y-%m-%d %H:%M} UTC "
                f"({notice_at.astimezone(OUTREACH_TZ):%H:%M} IST), debit moved to "
                f"{debit_at:%Y-%m-%d %H:%M} UTC, "
                f"{self._config.notice.target_lead / timedelta(hours=1):.0f}h later."
            ),
            proposed_debit_at=debit_at,
            proposed_notice_at=notice_at,
            compliance_blocked=True,
        )

    # --- helpers -----------------------------------------------------------

    def _proposed_debit_at(self, mandate: Mandate, now: datetime) -> datetime:
        """When the next debit would fire.

        The mandate's own scheduled time, unless that has already passed — a
        debit cannot be attempted in the past, and pretending otherwise would let
        an overdue schedule skip the spacing bound by arithmetic.
        """
        return max(mandate.next_debit_at, now)

    def _compliant_pair(self, mandate: Mandate, now: datetime) -> tuple[datetime, datetime]:
        """A (debit, notice) pair that satisfies A2 by construction.

        The notice goes out as soon as possible; the debit lands `target_lead`
        after it. If the mandate's own scheduled debit is far enough out to still
        take a compliant notice, that time is kept rather than pushed — moving a
        customer's billing date further than necessary is a cost with no benefit.
        """
        target = self._config.notice.target_lead
        scheduled = mandate.next_debit_at
        if scheduled - now >= self._config.notice.min_lead:
            notice_at = max(now, scheduled - target)
            return scheduled, notice_at
        notice_at = now
        return notice_at + target, notice_at

    def _debit_request(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        at: datetime,
        *,
        notice_lead_override: bool = False,
    ) -> PolicyRequest:
        """The gate's view of a proposed debit.

        `notice_lead_override` supplies a hypothetical compliant notice, used only
        to ask "would this debit be permitted if the notice problem were fixed?".
        It never authorises a real attempt — the caller that uses it is deciding
        whether to *send a notice*, not whether to debit.
        """
        lead = (
            self._config.notice.target_lead / timedelta(hours=1)
            if notice_lead_override
            else _lead_hours(mandate.next_debit_notice_sent_at, at)
        )
        return PolicyRequest(
            engine=Engine.MANDATE_RECOVERY,
            entity=mandate_entity(mandate),
            proposed_action=Action.ATTEMPT_CHARGE,
            now=at,
            decline_code=_code_or_none(classification.decline_code),
            mandate_paused=mandate_state(mandate) is MandateState.PAUSED,
            afa_registered=mandate.afa_registered,
            notice_lead_hours=lead,
            mandate_cap_paise=mandate.mandate_cap_paise,
            fresh_afa_completed=False,
        )

    def _would_be_permitted_with_notice(
        self, mandate: Mandate, classification: FailureClassification, debit_at: datetime
    ) -> tuple[bool, PolicyDecision]:
        """Would this debit be allowed if its notice problem were fixed?

        A denial that is only about *timing* counts as viable: the debit is still
        coming, just later, and the notice A2 requires is exactly what should go
        out for it. Only a permanent refusal — a spent budget, a dead instrument,
        an authentication requirement — makes the notice pointless.
        """
        decision = self._policy.evaluate(
            self._debit_request(mandate, classification, debit_at, notice_lead_override=True)
        )
        return decision.allowed or decision.earliest_next_attempt_at is not None, decision

    def _permitted_noop(self, mandate: Mandate, now: datetime) -> PolicyDecision:
        """A permitted decision for a mandate that needs nothing done.

        Still goes through `evaluate()`: an entity that skipped the gate entirely
        would appear in the trail citing no rule, which is the exact tell that
        something bypassed the policy engine.
        """
        return self._policy.evaluate(
            PolicyRequest(
                engine=Engine.MANDATE_RECOVERY,
                entity=mandate_entity(mandate),
                proposed_action=Action.SCHEDULE_RETRY,
                now=now,
            )
        )

    def _decided(
        self,
        mandate: Mandate,
        classification: FailureClassification,
        notice: NoticeAssessment,
        decision: PolicyDecision,
        *,
        step: NextStep,
        scheduled_for: datetime | None,
        explanation: str,
        proposed_debit_at: datetime | None = None,
        proposed_notice_at: datetime | None = None,
        suppressed: bool = False,
        compliance_blocked: bool | None = None,
    ) -> ScheduleDecision:
        """Assemble the answer, deriving the compliance flag from the citation."""
        blocked = (
            compliance_blocked
            if compliance_blocked is not None
            else (not decision.allowed and decision.rule_id in COMPLIANCE_RULES)
        )
        return ScheduleDecision(
            mandate=mandate,
            classification=classification,
            notice=notice,
            explanation=ScheduleExplanation(
                mandate_id=mandate.mandate_id,
                step=step,
                scheduled_for=scheduled_for,
                rule_id=decision.rule_id,
                reason_code=decision.reason_code,
                explanation=explanation,
                attempts_used=mandate.attempts_in_current_cycle,
                attempts_remaining=decision.attempts_remaining,
                terminal=decision.terminal,
                compliance_blocked=blocked,
            ),
            decision=decision,
            proposed_debit_at=proposed_debit_at,
            proposed_notice_at=proposed_notice_at,
            suppressed=suppressed and decision.outcome is not Outcome.PENDING,
        )


#: Denials whose remedy is the customer authenticating, not replacing anything.
#: A mandate refused by one of these is *re-routed* to the authentication path
#: even when its own decline was an ordinary soft one — an above-threshold
#: insufficient-funds failure still cannot auto-debit next cycle without fresh
#: AFA, and a message telling that customer to top up their balance would be
#: describing a different problem than the one blocking them.
AUTHENTICATION_DENIALS: frozenset[str] = frozenset(
    {"rbi-mandate-rules:A1", "rbi-mandate-rules:A3", "rbi-mandate-rules:A5"}
)


def _reroute(
    classification: FailureClassification, decision: PolicyDecision
) -> FailureClassification:
    """Re-point a classification when a mandate-level gate changed the remedy.

    The decline class never changes — that is a fact about what the issuer said.
    Only the *route* moves, and with it what the customer is asked to do, which
    is what `policy-bounds:TN2` validates the drafted message against.
    """
    if decision.allowed or decision.rule_id not in AUTHENTICATION_DENIALS:
        return classification
    return classification.model_copy(
        update={
            "route": FailureRoute.AUTHENTICATION,
            "rule_id": decision.rule_id,
            "rationale": (
                f"{classification.rationale} Re-routed to customer authentication: "
                f"{decision.rule_id} refuses the debit until the customer "
                "authenticates, whatever the original decline was."
            ),
        }
    )


def _follow_up_step(decision: PolicyDecision) -> NextStep:
    """What follows a refused debit: tell the customer, or tell a human.

    A spent budget routes to dunning even though `policy-bounds:HE2` also raises
    it for human review — `rbi-mandate-rules` is explicit that budget exhaustion
    hands off to the communication path, and the two are not in tension: the
    escalation is recorded on the refusal entry, the message is a separate action
    with its own gate. "Stopped retrying" and "gave up on the revenue" are
    different outcomes and the trail shows both.

    Everything else that needs a human — a high-value hard failure, a risk block,
    an unevaluable precondition — produces **no** customer message. A person
    decides what that customer should hear, and drafting one anyway would be the
    system acting past the point it escalated.
    """
    if decision.rule_id in AUTHENTICATION_DENIALS:
        return NextStep.REQUEST_AUTHENTICATION
    if decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED":
        return NextStep.SEND_DUNNING
    if decision.requires_human:
        return NextStep.ESCALATE
    return NextStep.SEND_DUNNING


def _lead_hours(notice_sent_at: datetime | None, debit_at: datetime) -> float | None:
    if notice_sent_at is None:
        return None
    return (debit_at - notice_sent_at) / timedelta(hours=1)


def _code_or_none(raw: str | None) -> DeclineCode | None:
    """The classification's code as a taxonomy code, or None when there was none."""
    if not raw or raw == "NONE":
        return None
    try:
        return DeclineCode(raw)
    except ValueError:  # pragma: no cover - classification normalises first
        return DeclineCode.UNKNOWN
