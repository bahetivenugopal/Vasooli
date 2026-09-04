"""Engine 2's vocabulary: what it ingests, what it derives, what it decides.

Read shapes over the mandate dataset, plus the value objects the engine passes
between its own layers. Nothing here decides anything — classification lives in
`classification.py`, scheduling in `scheduler.py`, judgment in `dunning.py`, and
permission is never here at all.

Note what a `Mandate` does **not** carry: a cohort label, a notice profile, or
whether its registration was defective. All three live only in the generator's
manifest, which engine code never opens. The engine derives the same conclusions
from timestamps and statuses, which is the whole point of the split.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Action
from app.services.llm_agent import StructuredOutput

# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class DebitAttempt(BaseModel):
    """One row of a mandate's debit history. Frozen: the engine reads history."""

    model_config = ConfigDict(frozen=True)

    attempt_no: int
    cycle: int
    scheduled_at: datetime
    attempted_at: datetime
    notice_sent_at: datetime | None = None
    status: str
    failure_reason_code: str | None = None
    razorpay_reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.status != "success"


class Mandate(BaseModel):
    """One row of the mandate dataset."""

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    batch_id: str
    customer_id: str
    customer_name: str
    amount_paise: int = Field(ge=0)
    currency: str = "INR"
    frequency: str
    registered_at: datetime
    afa_registered: bool
    mandate_cap_paise: int = Field(ge=0)
    status: str
    cycle_started_at: datetime
    attempts_in_current_cycle: int = Field(ge=0)
    next_debit_at: datetime
    next_debit_notice_sent_at: datetime | None = None
    debit_history: list[DebitAttempt] = Field(default_factory=list)

    @property
    def current_cycle(self) -> int:
        """The cycle number the mandate is in right now.

        Read off the history rather than counted, because a mandate whose
        registration never completed has no history at all and is still in
        cycle 1.
        """
        return max((a.cycle for a in self.debit_history), default=1)

    @property
    def current_cycle_failures(self) -> list[DebitAttempt]:
        """Failed attempts in the latest cycle only, oldest first."""
        cycle = self.current_cycle
        return [a for a in self.debit_history if a.cycle == cycle and a.failed]

    @property
    def latest_failure(self) -> DebitAttempt | None:
        """The failure this run is trying to recover from, if there is one."""
        failures = self.current_cycle_failures
        return failures[-1] if failures else None

    @property
    def notice_lead_hours(self) -> float | None:
        """Hours between the pre-debit notice and the next scheduled debit.

        `None` when no notice was sent. Negative when the notice went out *after*
        the scheduled debit, which the A2 gate treats exactly like a late one —
        a notice that arrives behind the debit is not a pre-debit notice.
        """
        if self.next_debit_notice_sent_at is None:
            return None
        delta = self.next_debit_at - self.next_debit_notice_sent_at
        return delta / timedelta(hours=1)


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


class MandateState(StrEnum):
    """The mandate's own lifecycle, normalized from the record's `status`.

    `PAUSED` is separate from `REVOKED` because the difference decides whether
    the schedule is suspended or destroyed — see `rbi-mandate-rules:PartB.paused`
    against A4.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class FailureRoute(StrEnum):
    """Where a failed recurring debit belongs. Engine 2's central branch.

    This is the recurring-payment extension the shared taxonomy does not make on
    its own: the same `SOFT`/`HARD` classes exist for one-off payments, but
    `AUTHENTICATION` and `COMPLIANCE` only arise because a mandate carries
    obligations a single payment does not.
    """

    #: Retry inside the compliance window. The recoverable case.
    RETRY = "retry"
    #: A dead instrument. Never retry; the customer has to supply a new one.
    DUNNING = "dunning"
    #: The debit needs the customer to authenticate. A silent retry reproduces
    #: the decline forever (`rbi-mandate-rules:A5`).
    AUTHENTICATION = "authentication"
    #: *Our* precondition failed, not the customer's instrument. Send the notice
    #: and reschedule beyond the window (`rbi-mandate-rules:A2`).
    COMPLIANCE = "compliance"
    #: Absolute stop. No retry, no communication (`rbi-mandate-rules:A4`).
    TERMINAL = "terminal"
    #: Nothing failed in the current cycle.
    NONE = "none"


class NoticeStatus(StrEnum):
    """How the pre-debit notice for the next debit stands, per A2."""

    ON_TIME = "on_time"
    LATE = "late"
    STALE = "stale"
    MISSING = "missing"


class FailureClassification(BaseModel):
    """What kind of failure this is, and which rule says so."""

    model_config = ConfigDict(frozen=True)

    decline_code: str
    decline_class: str
    route: FailureRoute
    rule_id: str
    rationale: str
    #: True when the shared `classify_unknown_decline` task was consulted because
    #: the raw code is not in the taxonomy. Reported separately in the summary —
    #: a reasoned classification and a table lookup are not the same evidence.
    reasoned: bool = False


class NoticeAssessment(BaseModel):
    """The A2 window, evaluated against the record's own timestamps."""

    model_config = ConfigDict(frozen=True)

    status: NoticeStatus
    lead_hours: float | None
    compliant: bool
    rule_id: str
    rationale: str


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class NextStep(StrEnum):
    """What the scheduler decided to do next about one mandate.

    Enumerated so a batch can be counted by decision rather than described by
    prose. Each value maps to exactly one shared `Action`.
    """

    ATTEMPT_DEBIT = "attempt_debit"
    SEND_PRE_DEBIT_NOTICE = "send_pre_debit_notice"
    REQUEST_AUTHENTICATION = "request_authentication"
    SEND_DUNNING = "send_dunning"
    DEFER = "defer"
    ESCALATE = "escalate"
    HALT = "halt"
    NO_ACTION = "no_action"


#: `NextStep` -> the shared audit vocabulary. Mapping lives here and nowhere else.
ACTION_BY_STEP: dict[NextStep, Action] = {
    NextStep.ATTEMPT_DEBIT: Action.ATTEMPT_CHARGE,
    NextStep.SEND_PRE_DEBIT_NOTICE: Action.SEND_PRE_DEBIT_NOTICE,
    NextStep.REQUEST_AUTHENTICATION: Action.SEND_DUNNING,
    NextStep.SEND_DUNNING: Action.SEND_DUNNING,
    NextStep.DEFER: Action.SCHEDULE_RETRY,
    NextStep.ESCALATE: Action.ESCALATE,
    NextStep.HALT: Action.HALT_SCHEDULE,
    NextStep.NO_ACTION: Action.BLOCK_ATTEMPT,
}


class ScheduleExplanation(BaseModel):
    """What this mandate gets next, when, and which rule permits it.

    A first-class output rather than a log line, because it is what the dashboard
    renders and what the video demonstrates. If the engine cannot state its next
    move in one sentence with a citation, the boundedness claim is decoration.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    step: NextStep
    scheduled_for: datetime | None
    rule_id: str
    reason_code: str
    explanation: str
    attempts_used: int
    attempts_remaining: int | None
    #: True when this decision permanently ends the mandate's schedule.
    terminal: bool = False
    #: True when the refusal was a compliance gate rather than a spent budget or
    #: a dead instrument. Counted separately in the summary.
    compliance_blocked: bool = False


# ---------------------------------------------------------------------------
# Dunning
# ---------------------------------------------------------------------------


class Channel(StrEnum):
    """Where a message goes. Enumerated — the model never writes a channel."""

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class CallToAction(StrEnum):
    """The one thing the customer is asked to do.

    Enumerated so `policy-bounds:TN2` — "a remedy the customer can actually
    complete" — is checkable rather than a matter of reading the prose.
    """

    UPDATE_PAYMENT_METHOD = "update_payment_method"
    COMPLETE_AUTHENTICATION = "complete_authentication"
    ENSURE_SUFFICIENT_BALANCE = "ensure_sufficient_balance"
    PAY_VIA_LINK = "pay_via_link"
    REAUTHORIZE_MANDATE = "reauthorize_mandate"
    CONTACT_SUPPORT = "contact_support"


class DunningRecommendation(StrEnum):
    """What the drafting task may recommend the system do about this mandate.

    A recommendation, never an authorisation. `SCHEDULE_RETRY` is deliberately
    available: a model that believes a hard decline deserves another attempt
    should be able to say so and be refused on the record, rather than be
    prevented from expressing the thing the gate exists to catch.
    """

    SEND_DUNNING = "send_dunning"
    SCHEDULE_RETRY = "schedule_retry"
    ESCALATE = "escalate"
    HALT_SCHEDULE = "halt_schedule"


ACTION_BY_RECOMMENDATION: dict[DunningRecommendation, Action] = {
    DunningRecommendation.SEND_DUNNING: Action.SEND_DUNNING,
    DunningRecommendation.SCHEDULE_RETRY: Action.SCHEDULE_RETRY,
    DunningRecommendation.ESCALATE: Action.ESCALATE,
    DunningRecommendation.HALT_SCHEDULE: Action.HALT_SCHEDULE,
}


class DunningDraft(StructuredOutput):
    """What the reasoning layer is allowed to say about a customer message.

    Note what is absent: no send time, no channel priority, no retry count, no
    recipient. The model drafts and recommends; every bound on what follows comes
    from the policy engine, and the message is never dispatched to a real person.
    """

    channel: Channel
    subject: str = Field(min_length=1, max_length=140)
    body: str = Field(min_length=1, max_length=1200)
    call_to_action: CallToAction
    recommended_action: DunningRecommendation


class ToneViolation(BaseModel):
    """One `policy-bounds` TN constraint a draft broke, and the evidence."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    detail: str


# ---------------------------------------------------------------------------
# Run reporting
# ---------------------------------------------------------------------------


class FailureClassBreakdown(BaseModel):
    """Recovery, per failure class. Where the story lives.

    Soft declines recover at a very different rate from hard ones, and a single
    blended rate hides exactly the fact that makes the engine worth building.
    """

    mandates: int = 0
    amount_at_risk_paise: int = 0
    amount_recovered_paise: int = 0
    recovery_rate: float = 0.0
    debits_attempted: int = 0
    debits_recovered: int = 0
    retries_suppressed: int = 0


class AfaBranchBreakdown(BaseModel):
    """Both sides of the A3 threshold, so the branch is visible not asserted."""

    mandates: int = 0
    amount_at_risk_paise: int = 0
    amount_recovered_paise: int = 0
    debits_attempted: int = 0
    authentication_requests: int = 0
    blocked_for_fresh_afa: int = 0


class RunSummary(BaseModel):
    """The honest summary of one Engine 2 batch run.

    Every money and count figure is recomputed from the audit trail by
    `runner.py`, never accumulated in a side counter that could drift from it.
    """

    batch_id: str
    dataset_batch_id: str
    seed: int
    now: datetime

    mandates_ingested: int
    mandates_with_failed_cycle: int

    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float

    #: The subset of the money at risk that this engine was *permitted to debit*
    #: this cycle — mandates whose next step the policy engine allowed to be an
    #: attempt, or deferred to a later one. Reported beside the headline rate,
    #: never instead of it: two thirds of the book sits above the Rs 15,000 AFA
    #: threshold or behind a hard stop, where no compliant system can auto-debit
    #: at all, and a single blended rate reads as failure at recovering money
    #: nobody is allowed to collect automatically.
    amount_addressable_paise: int
    addressable_recovery_rate: float
    addressable_definition: str

    by_failure_class: dict[str, FailureClassBreakdown]
    by_route: dict[str, int]
    afa_branch: dict[str, AfaBranchBreakdown]

    debits_attempted: int
    debits_recovered: int
    #: Permanent refusals on hard and terminal declines — retries that a blind
    #: system would have spent. The most persuasive count in the run.
    retries_suppressed: int
    #: What blind retrying would have cost, against the stated baseline. An
    #: estimate, and labelled as one everywhere it is reported.
    wasted_attempts_avoided: int
    wasted_attempts_baseline: str

    #: Attempts the engine refused to make because a compliance gate forbade it.
    #: Non-zero is the point.
    compliance_blocked: int
    compliance_blocked_by_rule: dict[str, int]

    notices_sent: int
    notices_scheduled: int
    notices_held: int
    notice_status_counts: dict[str, int]

    communications_drafted: int
    communications_held: int
    tone_rejections: int

    mandates_at_attempt_cap: int
    mandates_halted: int
    human_escalations: int

    action_counts: dict[str, int]
    outcome_counts: dict[str, int]
    policy_denials: int
    denials_by_rule: dict[str, int]
    llm_fallbacks: int
    unknown_declines_classified: int
    provenance: dict[str, object]
