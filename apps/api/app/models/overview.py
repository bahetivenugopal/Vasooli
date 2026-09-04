"""The cross-engine overview — the dashboard's headline surface.

Exists because the control tower's first screen has to answer "how much was at
risk, how much came back, and can I trust it?" across **all three engines**, and
nothing else in the API answers that question. The alternative was the dashboard
fetching three batch summaries and adding them up, which would have made the
browser a second place metrics are computed — and the first one to drift.

Every figure here is derived from the audit trail by `services/overview.py`. No
counter, no snapshot, no client arithmetic.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.audit import AuditEntryRead, SourceBreakdown
from app.models.enums import BatchStatus, Engine

#: Why the headline rate is a *sum*, not an average, and what it does not mean.
#:
#: The three engines measure at-risk money differently — a degraded corridor's
#: exposure, a mandate cycle's debit, an overdue invoice's balance — and each
#: ships its own definition beside its own number. Blending them is what the
#: cross-engine headline *is*, so the caveat travels with it rather than living
#: in a doc nobody opens on camera.
BLENDED_CAVEAT = (
    "Rupees at risk and rupees recovered are summed across the three engines' "
    "audit entries. The engines measure exposure differently — corridor value at "
    "risk, a mandate cycle's permitted debit, an overdue invoice's balance — so "
    "the blended rate is a breadth figure, not a like-for-like one. Each engine's "
    "own definition is on its contribution row, and the per-source split below "
    "keeps reasoned and ruled recovery apart."
)


class EngineContribution(BaseModel):
    """One engine's share of the headline, and the run it came from.

    Carries the run's `batch_id` and `seed` because a reported number that cannot
    be traced back to a reproducible run is not evidence.
    """

    engine: Engine
    label: str
    batch_id: str
    seed: int
    status: BatchStatus
    started_at: datetime
    completed_at: datetime | None = None
    #: The *dataset* batch id the run consumed. Not the run's own id — the two
    #: are different things and conflating them loses the trace back to the rows.
    dataset_batch_id: str | None = None
    entries: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    by_source: dict[str, SourceBreakdown]
    #: The engine's own statement of what its recovery figure means. Shipped in
    #: the response, not just the docs, so the number cannot be quoted without it.
    recovery_definition: str


class TrustMetric(BaseModel):
    """One count that proves the system's bounds are real.

    `meaning` is part of the payload rather than dashboard copy: a refusal count
    with no explanation of why non-zero is *good* reads as a defect list.
    """

    key: str
    label: str
    value: int
    meaning: str
    #: Citation -> count, so "which rule refused, how often" is one click away.
    by_rule: dict[str, int] = Field(default_factory=dict)


class OverviewSummary(BaseModel):
    """The whole story in one response — the control tower's first screen."""

    generated_at: datetime
    #: Engine -> the run this overview is reporting on.
    batch_ids: dict[str, str]
    engines_reporting: list[Engine]
    #: Engines with no completed run yet. Named rather than silently omitted: a
    #: headline missing a third of the project should say so.
    engines_missing: list[Engine]

    entries: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    blended_caveat: str

    by_engine: list[EngineContribution]
    by_source: dict[str, SourceBreakdown]
    trust: list[TrustMetric]

    #: Entries that break the schema's own rules. Should be zero; if it is not,
    #: the trail says so on the front page rather than hiding it.
    policy_violations: int
    #: The latest decisions **from each engine**, newest first — not simply the
    #: newest entries overall. The three engines do not share a clock, so a plain
    #: sort resolves to "whichever engine ran last". See `OverviewService._recent`.
    recent_activity: list[AuditEntryRead]
