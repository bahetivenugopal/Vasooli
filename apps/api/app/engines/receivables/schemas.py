"""Engine 3's vocabulary: what it ingests, what it derives, what it decides.

Read shapes over the invoice ledger, plus the value objects the engine passes
between its own layers. Nothing here decides anything — prioritisation lives in
`prioritization.py`, the ladder in `ladder.py`, judgment in `understanding.py`,
promise bookkeeping in `promises.py`, and permission is never here at all.

Note what an `Invoice` does **not** carry: an archetype label, a `disputed` flag,
whether a promise was kept, or what a reply means. All of that lives only in the
generator's manifest, which engine code never opens. The engine has to read
`replies[].text` and reach the same conclusions unaided — which is the whole
reason its extraction accuracy is a measurement rather than a claim.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Action, EscalationTrigger
from app.models.provenance import Provenance
from app.services.llm_agent import StructuredOutput

# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class Communication(BaseModel):
    """One outbound contact already in the ledger before this run."""

    model_config = ConfigDict(frozen=True)

    communication_id: str
    direction: str
    channel: str
    rung: int
    template: str
    sent_at: datetime


class Reply(BaseModel):
    """One inbound customer reply. The text is all the engine gets."""

    model_config = ConfigDict(frozen=True)

    reply_id: str
    direction: str
    channel: str
    received_at: datetime
    text: str


class Invoice(BaseModel):
    """One row of the invoice ledger."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    batch_id: str
    customer_id: str
    customer_name: str
    amount_paise: int = Field(ge=0)
    amount_paid_paise: int = Field(default=0, ge=0)
    currency: str = "INR"
    payment_terms: str
    issued_at: datetime
    due_at: datetime
    days_overdue: int
    status: str
    paid_at: datetime | None = None
    communications: list[Communication] = Field(default_factory=list)
    replies: list[Reply] = Field(default_factory=list)

    @property
    def outstanding_paise(self) -> int:
        """What is still owed. Zero on a settled invoice."""
        return max(self.amount_paise - self.amount_paid_paise, 0)

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None or self.status == "paid"

    @property
    def rungs_used(self) -> int:
        """How far up the RL1 ladder this invoice already is.

        Read off the ledger's own outbound history rather than counted fresh: the
        contact budget a run inherits is the one the customer actually
        experienced, not the one this run started with.
        """
        return len([c for c in self.communications if c.direction == "outbound"])

    @property
    def last_contact_at(self) -> datetime | None:
        outbound = [c.sent_at for c in self.communications if c.direction == "outbound"]
        return max(outbound) if outbound else None

    @property
    def latest_reply(self) -> Reply | None:
        """The reply this run reasons about, if any."""
        inbound = sorted(
            (r for r in self.replies if r.direction == "inbound"),
            key=lambda r: r.received_at,
        )
        return inbound[-1] if inbound else None

    def days_late(self, now: datetime) -> int:
        """Days past due, derived — never read from `days_overdue`.

        A settled invoice reports how late it was when it landed; an open one
        reports how late it is now. Same convention the ledger uses, so the two
        agree instead of quietly disagreeing.
        """
        end = self.paid_at if self.paid_at is not None else now
        return max((end - self.due_at).days, 0)


# ---------------------------------------------------------------------------
# Reply understanding — what the reasoning layer may say
# ---------------------------------------------------------------------------


class ReplyIntent(StrEnum):
    """What a customer reply is *doing*. `policy-architect` owns this list.

    `UNCLEAR` is deliberately one of the values rather than an error case: an
    explicit abstention on an ambiguous reply is a trust feature, and forcing a
    classification on one is worse than admitting uncertainty.
    """

    PROMISE = "promise"
    PARTIAL_PROMISE = "partial_promise"
    DISPUTE = "dispute"
    INFORMATION_REQUEST = "information_request"
    REFUSAL = "refusal"
    NON_RESPONSE = "non_response"
    OUT_OF_OFFICE = "out_of_office"
    UNCLEAR = "unclear"


#: Intents that constitute a commitment. `PARTIAL_PROMISE` counts: a commitment
#: to part of the balance is still a commitment, and `committed_amount_paise` is
#: where the difference is recorded.
PROMISE_INTENTS: frozenset[ReplyIntent] = frozenset(
    {ReplyIntent.PROMISE, ReplyIntent.PARTIAL_PROMISE}
)


class ReplyRecommendation(StrEnum):
    """What the reasoning layer thinks should happen to the chase next.

    A recommendation, never an authorisation. `CONTINUE_CHASE` is deliberately
    available on every reply — a model that believes a disputed invoice should
    keep being chased should be able to say so and be refused on the record by
    `policy-bounds:RL4`, rather than be prevented from expressing the thing the
    gate exists to catch.
    """

    CONTINUE_CHASE = "continue_chase"
    PAUSE_CHASE = "pause_chase"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    HALT_CHASE = "halt_chase"


ACTION_BY_RECOMMENDATION: dict[ReplyRecommendation, Action] = {
    ReplyRecommendation.CONTINUE_CHASE: Action.SEND_REMINDER,
    ReplyRecommendation.PAUSE_CHASE: Action.BLOCK_ATTEMPT,
    ReplyRecommendation.ESCALATE_TO_HUMAN: Action.ESCALATE,
    ReplyRecommendation.HALT_CHASE: Action.HALT_SCHEDULE,
}


class ReplyUnderstanding(StructuredOutput):
    """The structured reading of one customer reply.

    Note what is absent: no send time, no rung, no priority, no permission. The
    model reads the reply and recommends; every bound on what follows comes from
    `policy_engine.py`.

    `committed_date` is an **absolute** date. Resolving "end of the month" or
    "next Friday" against the reply's own timestamp is part of the task, and the
    prompt is given that timestamp explicitly — a relative expression resolved
    against run time instead of reply time is the single most common way this
    extraction goes subtly and invisibly wrong.
    """

    intent: ReplyIntent
    #: True only when a commitment genuinely exists. An intent of `promise` with
    #: `promise_detected = false` is contradictory and is treated as an
    #: abstention by `understanding.py`, not silently reconciled.
    promise_detected: bool = False
    committed_date: str | None = Field(
        default=None,
        description="ISO 8601 date (YYYY-MM-DD), absolute. Null when none can be extracted.",
    )
    committed_amount_paise: int | None = Field(
        default=None, ge=0, description="Set only for a partial promise."
    )
    conditional: bool = False
    condition_detail: str | None = None
    dispute_detail: str | None = None
    language: str = Field(default="en", max_length=16)
    recommended_action: ReplyRecommendation


class ExtractionResult(BaseModel):
    """One reply, understood — or explicitly not.

    `abstained` and `understanding` are mutually exclusive by construction: an
    abstention has nothing to say, and a reading that exists was not an
    abstention. Keeping them in one object is what stops the rest of the engine
    from having to remember which case it is in.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    invoice_id: str
    reply_id: str
    received_at: datetime
    text: str
    understanding: ReplyUnderstanding | None
    abstained: bool
    #: How this reading was produced, carried verbatim from the reasoning layer.
    #: Not reconstructed anywhere downstream: a provenance object built at a call
    #: site is a claim about how a result was made by code that did not make it.
    provenance: Provenance
    abstention_reason: str | None = None
    confidence: float | None = None
    degraded: bool = False
    degradation_reason: str | None = None

    @property
    def intent(self) -> ReplyIntent:
        """The intent, with abstention collapsing to `unclear`."""
        if self.understanding is None:
            return ReplyIntent.UNCLEAR
        return self.understanding.intent

    @property
    def is_dispute(self) -> bool:
        """Whether `policy-bounds:RL4` should freeze this invoice.

        An abstention is **not** a dispute. It routes to a human under HE1, which
        is a different rule with a different meaning, and conflating the two
        would make the dispute count a measure of model failure.
        """
        return self.understanding is not None and self.understanding.intent is ReplyIntent.DISPUTE


# ---------------------------------------------------------------------------
# Promises
# ---------------------------------------------------------------------------


class PromiseStatus(StrEnum):
    """Where a commitment stands. `superseded` needs a later reply to exist."""

    ACTIVE = "active"
    KEPT = "kept"
    BROKEN = "broken"
    SUPERSEDED = "superseded"


class PromiseAssessment(BaseModel):
    """One promise and what became of it, with the rule that decided."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    reply_id: str
    customer_id: str
    received_at: datetime
    committed_date: datetime | None
    committed_amount_paise: int | None
    conditional: bool
    condition_detail: str | None
    status: PromiseStatus
    rule_id: str
    rationale: str
    review_at: datetime | None
    #: Whether `policy-bounds:PR2` should deprioritise this invoice *right now*.
    #:
    #: A field rather than a property derived from `status`, because "active" and
    #: "still buying room" are not the same thing and the difference is the
    #: entire point of PP3. A conditional or undateable promise stays `active`
    #: forever — it has no date it could break — but it stops suppressing the
    #: chase once its 7-day horizon passes. Deriving this from the status alone
    #: parked those invoices permanently, which a test caught.
    suppresses_chase: bool
    confidence: float | None = None
    reasoning: str | None = None


class CustomerReliability(BaseModel):
    """How often this customer's commitments hold.

    Feeds prioritisation on the next run and is the engine's one visible piece of
    learning-from-behaviour. Reported as `None` rather than as a default when a
    customer has made no resolved promise: a customer with no history is not the
    same as a customer with a bad one, and a default would fabricate the
    difference.
    """

    model_config = ConfigDict(frozen=True)

    customer_id: str
    promises_made: int = 0
    kept: int = 0
    broken: int = 0
    reliability: float | None = None


# ---------------------------------------------------------------------------
# Prioritisation
# ---------------------------------------------------------------------------


class ScoreComponent(BaseModel):
    """One term of the priority score, with its weight and what produced it."""

    model_config = ConfigDict(frozen=True)

    name: str
    raw: float
    normalized: float
    weight: float
    contribution: float
    explanation: str


class PriorityScore(BaseModel):
    """A ranked invoice, with every term of its score visible.

    `policy-bounds:PR1`: the ranking decides the order of work, never permission.
    Nothing here is model-derived — a reasoned ranking would be unexplainable and
    unstable across runs for no gain.
    """

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    customer_id: str
    score: float
    rank: int = 0
    components: list[ScoreComponent]
    deprioritised: bool = False
    deprioritised_rule: str | None = None
    deprioritised_reason: str | None = None


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class ChaseStep(StrEnum):
    """What the ladder decided to do next about one invoice."""

    SEND_REMINDER = "send_reminder"
    #: A live promise, or a promise with no date inside its PP3 horizon.
    HOLD_FOR_PROMISE = "hold_for_promise"
    #: RL4. A human takes it from here.
    FREEZE_DISPUTE = "freeze_dispute"
    ESCALATE = "escalate"
    #: Quiet hours, a contact cap, or the RL2 interval. Timing, not refusal.
    DEFER = "defer"
    HALT = "halt"
    NO_ACTION = "no_action"


#: `ChaseStep` -> the shared audit vocabulary. Mapping lives here and nowhere else.
ACTION_BY_STEP: dict[ChaseStep, Action] = {
    ChaseStep.SEND_REMINDER: Action.SEND_REMINDER,
    ChaseStep.HOLD_FOR_PROMISE: Action.BLOCK_ATTEMPT,
    ChaseStep.FREEZE_DISPUTE: Action.ESCALATE,
    ChaseStep.ESCALATE: Action.ESCALATE,
    ChaseStep.DEFER: Action.BLOCK_ATTEMPT,
    ChaseStep.HALT: Action.HALT_SCHEDULE,
    ChaseStep.NO_ACTION: Action.BLOCK_ATTEMPT,
}


class ChasePlan(BaseModel):
    """What this invoice gets next, when, and which rule permits it.

    A first-class output rather than a log line, for the same reason Engine 2's
    `ScheduleExplanation` is: it is what the dashboard renders and what the video
    demonstrates. An engine that cannot state its next move in one sentence with
    a citation has a boundedness claim that is decoration.
    """

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    step: ChaseStep
    #: Whether the gate *permitted* the proposed action. Distinct from the step:
    #: a permitted escalation and a refused reminder can both end up escalating,
    #: and only one of them is a policy denial. Counting denials from the outcome
    #: alone would report every authorised handoff as a refusal.
    allowed: bool
    rung: str | None
    rung_number: int
    scheduled_for: datetime | None
    rule_id: str
    reason_code: str
    explanation: str
    attempts_remaining: int | None
    escalation_trigger: EscalationTrigger | None = None
    #: True when a message was refused by a cap, quiet hours or a freeze — the
    #: direct measure of harassment avoided.
    suppressed: bool = False
    suppressed_rule: str | None = None
    overridden_recommendation: str | None = None
    authority_rule: str | None = None


# ---------------------------------------------------------------------------
# Reminder drafting
# ---------------------------------------------------------------------------


class ReminderChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class ReminderCallToAction(StrEnum):
    """The one thing the customer is asked to do.

    Enumerated so `policy-bounds:TN2` — "a remedy the customer can actually
    complete" — stays checkable rather than a matter of reading the prose.
    """

    PAY_VIA_LINK = "pay_via_link"
    CONFIRM_PAYMENT_DATE = "confirm_payment_date"
    SHARE_REMITTANCE_ADVICE = "share_remittance_advice"
    CONTACT_ACCOUNTS = "contact_accounts"


class ReminderDraft(BaseModel):
    """One rendered reminder, before the gate has said anything about it."""

    model_config = ConfigDict(frozen=True)

    rung: str
    rung_number: int
    channel: ReminderChannel
    subject: str
    body: str
    call_to_action: ReminderCallToAction
    payment_link_url: str | None = None
    payment_link_id: str | None = None


# ---------------------------------------------------------------------------
# Run reporting
# ---------------------------------------------------------------------------


class ConfusionMatrix(BaseModel):
    """A 2x2 over one binary extraction claim, plus the derived rates.

    Reported in full rather than as headline accuracy: precision and recall move
    in opposite directions here, and which one matters depends on the claim —
    a false promise suppresses a legitimate chase, a missed promise chases
    someone who was cooperating.
    """

    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    accuracy: float = 0.0


class DateAccuracy(BaseModel):
    """How well extracted dates match the ground truth, split by difficulty.

    `explicit` dates are stated in the text in one of several everyday formats;
    `inferable` ones need an anchor ("end of the month") resolved against the
    reply's own timestamp. Scoring them together would hide which half the engine
    is actually good at.
    """

    scored: int = 0
    exact: int = 0
    within_one_day: int = 0
    wrong: int = 0
    missing: int = 0
    spurious: int = 0
    exact_rate: float = 0.0
    by_difficulty: dict[str, dict[str, int]] = Field(default_factory=dict)


class ExtractionScore(BaseModel):
    """Extraction accuracy against Phase 2's held-out annotations.

    Every rate here is computed **over model-handled replies only**, with
    abstentions reported separately and never folded in. Blending them would
    imply the model answered when it explicitly did not, which is the one thing
    an abstention path must never be allowed to do to a metric.
    """

    ground_truth_source: str
    replies_total: int = 0
    replies_scored: int = 0
    abstentions: int = 0
    abstention_rate: float = 0.0
    promise_detection: ConfusionMatrix = Field(default_factory=ConfusionMatrix)
    dispute_detection: ConfusionMatrix = Field(default_factory=ConfusionMatrix)
    conditional_detection: ConfusionMatrix = Field(default_factory=ConfusionMatrix)
    date_accuracy: DateAccuracy = Field(default_factory=DateAccuracy)
    intent_confusion: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_language: dict[str, ConfusionMatrix] = Field(default_factory=dict)
    #: Every reply the engine got wrong, with both readings. The interesting part.
    failures: list[dict[str, object]] = Field(default_factory=list)
    scoring_note: str = ""


class AgeingBreakdown(BaseModel):
    """Recovery per ageing bucket. Older buckets recover worse; say so."""

    invoices: int = 0
    amount_outstanding_paise: int = 0
    amount_recovered_paise: int = 0
    recovery_rate: float = 0.0
    reminders_sent: int = 0
    escalations: int = 0


class RunSummary(BaseModel):
    """The honest summary of one Engine 3 batch run.

    Every money and count figure is recomputed from the audit trail by
    `runner.py`, never accumulated in a side counter that could drift from it.
    """

    batch_id: str
    dataset_batch_id: str
    seed: int
    now: datetime

    invoices_ingested: int
    invoices_overdue: int
    invoices_worklisted: int

    amount_outstanding_paise: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    recovery_definition: str
    by_ageing_bucket: dict[str, AgeingBreakdown]

    extraction: ExtractionScore

    promises_made: int
    promises_kept: int
    promises_broken: int
    promises_active: int
    promises_superseded: int
    promises_conditional: int
    promises_undateable: int
    customer_reliability: dict[str, CustomerReliability]

    reminders_sent: int
    reminders_held: int
    reminders_by_rung: dict[str, int]
    payment_links_created: int
    payment_links_failed: int

    #: Every invoice on the worklist, by what the ladder decided. The one figure
    #: that has to add up to `invoices_worklisted`, and the quickest way to see
    #: that no invoice fell through a branch.
    chase_steps: dict[str, int]

    escalations: int
    escalations_by_trigger: dict[str, int]
    disputes_frozen: int

    messages_suppressed: int
    suppressed_by_rule: dict[str, int]
    invoices_deprioritised: int

    action_counts: dict[str, int]
    outcome_counts: dict[str, int]
    policy_denials: int
    denials_by_rule: dict[str, int]
    llm_fallbacks: int
    abstentions: int
    provenance: dict[str, object]
