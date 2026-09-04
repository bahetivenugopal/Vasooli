"""The consolidated cross-engine run report.

Phase 7 §5.1. Three engines have always run separately, each producing a
defensible number for its own leak point. What nobody could quote was *the*
number — how much of the money this system was pointed at came back — and a
figure assembled by hand from three demo scripts is a figure with no provenance.

This is that report, and it is deliberately thin. Almost everything in it is an
`OverviewSummary`: the exact object the dashboard's front page renders, so the
console report and the dashboard cannot disagree — not because two computations
were checked against each other, but because there is one computation with two
renderings.

What this model adds on top is the run's own reproducibility header — the seed,
the datasets, the mode each subsystem was in, the clock each engine derived —
and the integrity verdict. A number without those is not evidence.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import Engine
from app.models.overview import OverviewSummary

#: What the unified recovery rate does and does not mean. Distinct from
#: `overview.BLENDED_CAVEAT`, which explains the *blending*; this explains the
#: run. Shipped in the payload so the console, the API and any doc quoting the
#: figure carry the same sentence.
UNIFIED_METHODOLOGY = (
    "One seeded run of all three engines over the committed synthetic datasets, "
    "under a single unified run id. Every figure is recomputed from the audit "
    "trail at read time — no counter, no snapshot, no client arithmetic. The "
    "engines measure exposure differently, so the headline rate is a breadth "
    "figure across three leak points, not a like-for-like recovery rate; each "
    "engine's own definition travels with its contribution row."
)


class EngineRunRecord(BaseModel):
    """One engine's leg of the unified run.

    `run_clock` is here because it is the field most likely to be misread. All
    three engines derive `now` differently and each is right for its own data
    (Phase 5) — so the report states the clock each one used rather than implying
    they share one.
    """

    engine: Engine
    batch_id: str
    dataset_batch_id: str | None = None
    dataset_path: str | None = None
    run_clock: datetime | None = None
    entries: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    #: The engine's own `RunSummary`, as stored on the run. Carries the splits
    #: that cannot be recomputed from audit entries alone — detection scoring,
    #: the AFA branch, per-ageing-bucket recovery, extraction accuracy.
    headline: dict = Field(default_factory=dict)
    elapsed_seconds: float = 0.0


class RunMode(BaseModel):
    """Which mode every subsystem was in, reported rather than assumed.

    §5.2 requires the system to run with no live keys at all *and to say so*. A
    run that silently fell back is a run whose numbers mean something different
    from what the reader thinks they mean.
    """

    llm_provider_enabled: bool
    llm_deterministic_only: bool
    llm_model: str | None = None
    razorpay_mode: str
    #: The plain-English line the console prints and the report carries.
    description: str


class UnifiedRunReport(BaseModel):
    """One run of all three engines, as one auditable unit."""

    run_id: str
    seed: int
    started_at: datetime
    completed_at: datetime
    elapsed_seconds: float
    mode: RunMode
    methodology: str = UNIFIED_METHODOLOGY

    #: Engine -> batch id. The three ids this run's trail lives under; querying
    #: the trail for all three is what makes the run auditable as one unit.
    batch_ids: dict[str, str]
    engines: list[EngineRunRecord]

    #: The consolidated figures — the same object `/api/v1/overview` returns.
    overview: OverviewSummary

    #: The §5.3 verification pass, serialised. Present in the report rather than
    #: run separately, so a consolidated report cannot be produced without its
    #: own integrity verdict beside it.
    integrity_passed: bool
    integrity_checks: dict[str, bool] = Field(default_factory=dict)
    integrity_detail: dict = Field(default_factory=dict)

    @property
    def batch_id_list(self) -> list[str]:
        return list(self.batch_ids.values())
