"""Engine 1's own vocabulary: what it ingests, what it detects, what it decides.

Everything here is either a *read shape* over the payments dataset or a value
object the engine passes between its own layers. Nothing in this module makes a
decision — detection lives in `detection.py`, judgment in `diagnosis.py`, and
permission is never here at all.

Note what a `PaymentAttempt` does **not** carry: any notion of whether the
corridor it belongs to was degraded. That lives only in the generator's manifest,
which engine code never opens.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Action, CorridorDetermination
from app.services.llm_agent import StructuredOutput

# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class PaymentAttempt(BaseModel):
    """One row of the payments dataset.

    Frozen: the engine reads history, it does not rewrite it.
    """

    model_config = ConfigDict(frozen=True)

    attempt_id: str
    batch_id: str
    customer_id: str
    created_at: datetime
    amount_paise: int = Field(ge=0)
    currency: str = "INR"
    status: str
    method: str
    issuer: str | None = None
    issuer_name: str | None = None
    route_id: str | None = None
    failure_reason_code: str | None = None
    razorpay_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def failed(self) -> bool:
        return not self.succeeded


# ---------------------------------------------------------------------------
# Corridors
# ---------------------------------------------------------------------------


class Corridor(BaseModel):
    """A slice of the payment stream, at one configured level.

    `key` is the stable identifier used as an audit `entity_id`, so a corridor's
    whole decision history can be pulled with one timeline query.
    """

    model_config = ConfigDict(frozen=True)

    level: str
    issuer: str | None = None
    method: str | None = None
    route_id: str | None = None

    @property
    def key(self) -> str:
        parts = [self.level, self.issuer or "*", self.method or "*", self.route_id or "*"]
        return ":".join(parts)

    @property
    def label(self) -> str:
        """Human-readable, for rationales and the dashboard."""
        bits = [b for b in (self.issuer, self.method, self.route_id) if b]
        return " x ".join(bits) or self.level

    def matches(self, attempt: PaymentAttempt) -> bool:
        return all(
            getattr(attempt, field) == value
            for field, value in (
                ("issuer", self.issuer),
                ("method", self.method),
                ("route_id", self.route_id),
            )
            if value is not None
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class Detection(BaseModel):
    """One degradation episode on one corridor.

    Purely statistical. Produced without a model, without the manifest, and
    without any knowledge of what recovery might follow.
    """

    model_config = ConfigDict(frozen=True)

    detection_id: str
    corridor: Corridor
    window_start: datetime
    window_end: datetime

    window_attempts: int
    window_successes: int
    baseline_attempts: int
    baseline_successes: int

    observed_success_rate: float
    baseline_success_rate: float
    #: One-sided binomial p-value of the observed successes under the baseline
    #: rate. `corridor-detection:ST1`.
    p_value: float
    #: `1 - p_value`. A *statistical* confidence, never mixed with the model's.
    confidence: float

    affected_attempts: int
    value_at_risk_paise: int

    #: Failure code -> count, over the failures inside the window.
    decline_mix: dict[str, int]
    distinct_failing_customers: int

    #: The rules that produced this detection, for the audit rationale.
    cited_rules: list[str]

    @property
    def absolute_drop(self) -> float:
        return round(self.baseline_success_rate - self.observed_success_rate, 4)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


class RootCauseHypothesis(StrEnum):
    """The enumerated set of causes the diagnosis layer may name.

    Enumerated, not free text: a hypothesis the system cannot count is a
    hypothesis nobody can check across a batch.
    """

    ISSUER_OUTAGE = "issuer_outage"
    METHOD_DEGRADATION = "method_degradation"
    ROUTE_OR_ACQUIRER_PROBLEM = "route_or_acquirer_problem"
    CUSTOMER_SIDE_CONCENTRATION = "customer_side_concentration"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RecommendedAction(StrEnum):
    """What the model may recommend. A fixed set — the model never writes an action.

    The model recommends; `policy_engine.py` authorises. Mapping onto the shared
    `Action` vocabulary happens in `ACTION_BY_RECOMMENDATION` and nowhere else.
    """

    REROUTE_TRAFFIC = "reroute_traffic"
    SCHEDULE_RETRY = "schedule_retry"
    SUPPRESS_RETRY = "suppress_retry"
    ESCALATE = "escalate"


#: Recommendation -> the shared action it proposes.
#:
#: `SUPPRESS_RETRY` maps to `halt_schedule` because at corridor level "suppress"
#: means *take no corridor-wide action* — individual payments are still handled
#: on their own per-payment authorisation. `SCHEDULE_RETRY` maps straight across
#: and is, deliberately, outside a corridor's permitted envelope: a retry is
#: authorised per payment against that payment's decline class and budget, never
#: wholesale across a corridor. A model recommending it is refused, and the
#: refusal is one of the honest denials this engine reports.
ACTION_BY_RECOMMENDATION: dict[RecommendedAction, Action] = {
    RecommendedAction.REROUTE_TRAFFIC: Action.REROUTE_TRAFFIC,
    RecommendedAction.SCHEDULE_RETRY: Action.SCHEDULE_RETRY,
    RecommendedAction.SUPPRESS_RETRY: Action.HALT_SCHEDULE,
    RecommendedAction.ESCALATE: Action.ESCALATE,
}


class CorridorDiagnosis(StructuredOutput):
    """What the reasoning layer is allowed to say about a detection.

    Note what is absent: no duration, no route to switch to, no attempt count. The
    model diagnoses and recommends. Every bound on what follows comes from the
    policy engine.

    Inherits `confidence` and `reasoning` from `StructuredOutput`; the reasoning
    is captured verbatim into the audit trail, because "what happened" without
    "why the system believed it" is not an explanation.
    """

    hypothesis: RootCauseHypothesis
    determination: CorridorDetermination
    recommended_action: RecommendedAction


# ---------------------------------------------------------------------------
# Run reporting
# ---------------------------------------------------------------------------


class DetectionScore(BaseModel):
    """Detection performance against the generator's ground truth.

    Computed by `scoring.py`, which is evaluation code and may read the manifest.
    The detector may not, and does not.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    #: Ground-truth label -> hours between degradation onset and the end of the
    #: window that first detected it.
    detection_latency_hours: dict[str, float]
    matched: list[dict[str, object]]
    unmatched_detections: list[dict[str, object]]
    missed_degradations: list[str]
    #: Detections that landed on a corridor the manifest marks as a decoy. Should
    #: be zero; reported either way.
    decoy_false_positives: int
    ground_truth_source: str


class RunSummary(BaseModel):
    """The honest summary of one batch run.

    Every money and count figure here is recomputed from the audit trail by
    `runner.py`, never accumulated in a side counter that could drift from it.
    """

    batch_id: str
    dataset_batch_id: str
    seed: int
    now: datetime
    attempts_ingested: int
    failed_attempts: int

    detections: int
    diagnoses_by_determination: dict[str, int]

    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float

    action_counts: dict[str, int]
    outcome_counts: dict[str, int]
    #: Permanent refusals — a dead instrument, a spent budget. Each one is a
    #: wasted attempt avoided, which is a number worth showing.
    retries_suppressed: int
    #: Refusals that were only about timing. Not suppressions: the revenue is
    #: still recoverable, the moment was wrong.
    retries_deferred: int
    reroutes_authorised: int
    policy_denials: int
    denials_by_rule: dict[str, int]
    human_escalations: int
    llm_fallbacks: int
    provenance: dict[str, object]

    detection_score: DetectionScore | None = None
