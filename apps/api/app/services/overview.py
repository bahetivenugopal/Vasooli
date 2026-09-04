"""The cross-engine overview — one read model over three engines' audit trails.

The control tower's first screen has to answer, in about three seconds, *how
much was at risk, how much came back, and can I trust it?* — across all three
engines at once. Nothing else in the API answers that, and the alternative was
the browser fetching three summaries and adding them up. That would have made
the dashboard a second place metrics are computed, and a second place is where
the first disagreement starts.

So the arithmetic lives here, on the same side of the wire as the trail it reads,
and the dashboard formats what it is given and nothing else.

Two properties this module exists to preserve:

1. **Nothing is accumulated.** Every figure is recomputed from `audit_entries`
   on each request. The snapshot on `BatchRun.summary` is never quoted.
2. **The caveats travel with the numbers.** Each engine measures exposure
   differently, so each contribution row carries that engine's own definition of
   what its recovery figure means, and the blended headline carries the reason it
   is a breadth figure rather than a like-for-like one.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.types import utcnow
from app.engines.mandate_recovery.runner import ADDRESSABLE_DEFINITION
from app.engines.receivables.runner import RECOVERY_DEFINITION as RECEIVABLES_RECOVERY_DEFINITION
from app.engines.root_cause.runner import RECOVERY_DEFINITION as ROOT_CAUSE_RECOVERY_DEFINITION
from app.models.audit import AuditEntry, AuditEntryRead, SourceBreakdown
from app.models.batch import BatchRun
from app.models.enums import BatchStatus, Engine, Outcome, ProvenanceSource
from app.models.overview import (
    BLENDED_CAVEAT,
    EngineContribution,
    OverviewSummary,
    TrustMetric,
)
from app.services.audit_trail import AuditTrail

#: The three product engines, in the order the dashboard reads them. `CORE` is
#: excluded deliberately: shared-core bookkeeping is not a recovery engine, and
#: counting it would put batch admin in a per-engine contribution chart.
OVERVIEW_ENGINES: tuple[Engine, ...] = (
    Engine.ROOT_CAUSE,
    Engine.MANDATE_RECOVERY,
    Engine.RECEIVABLES,
)

ENGINE_LABELS: dict[Engine, str] = {
    Engine.ROOT_CAUSE: "Root-Cause Recovery",
    Engine.MANDATE_RECOVERY: "Mandate & Subscription Recovery",
    Engine.RECEIVABLES: "B2B Receivables Chaser",
}

#: Each engine's own statement of what its recovery number means, imported from
#: the engine that owns it rather than restated here. A caveat with two copies
#: goes stale in one of them.
RECOVERY_DEFINITIONS: dict[Engine, str] = {
    Engine.ROOT_CAUSE: ROOT_CAUSE_RECOVERY_DEFINITION,
    Engine.MANDATE_RECOVERY: ADDRESSABLE_DEFINITION,
    Engine.RECEIVABLES: RECEIVABLES_RECOVERY_DEFINITION,
}

#: The outcomes that mean "the system refused". The same set all three engines
#: use in their own summaries, so the overview and an engine view cannot disagree
#: about what counts as a refusal.
REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {Outcome.BLOCKED.value, Outcome.HALTED.value, Outcome.ESCALATED.value}
)


class OverviewService:
    """Reads the audit trail across engines and reports the headline honestly."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._trail = AuditTrail(db)

    # --- run selection ----------------------------------------------------

    def latest_runs(self) -> dict[Engine, BatchRun]:
        """The newest **completed** run per engine.

        Completed only: a run still in flight has a partial trail behind it, and
        a headline that moves while a batch is mid-flight is a headline nobody
        can quote.
        """
        runs: dict[Engine, BatchRun] = {}
        for engine in OVERVIEW_ENGINES:
            row = self._db.scalars(
                select(BatchRun)
                .where(BatchRun.engine == engine.value)
                .where(BatchRun.status == BatchStatus.COMPLETED.value)
                .order_by(BatchRun.started_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                runs[engine] = row
        return runs

    def resolve_runs(self, selection: dict[Engine, str | None]) -> dict[Engine, BatchRun]:
        """Honour an explicit per-engine batch id, defaulting to the latest run.

        An explicitly named batch that does not exist is an error rather than a
        silent fall-back to the latest: a demo that quietly shows a different run
        than the one asked for is worse than one that says the id was wrong.
        """
        latest = self.latest_runs()
        runs: dict[Engine, BatchRun] = {}
        for engine in OVERVIEW_ENGINES:
            requested = selection.get(engine)
            if requested is None:
                if engine in latest:
                    runs[engine] = latest[engine]
                continue
            run = self._db.get(BatchRun, requested)
            if run is None:
                raise LookupError(f"no run {requested!r}")
            if run.engine != engine.value:
                raise LookupError(
                    f"run {requested!r} belongs to {run.engine!r}, not {engine.value!r}"
                )
            runs[engine] = run
        return runs

    # --- the summary ------------------------------------------------------

    def summary(
        self,
        selection: dict[Engine, str | None] | None = None,
        *,
        recent_limit: int = 12,
    ) -> OverviewSummary:
        """The whole story in one response, recomputed from the trail."""
        runs = self.resolve_runs(selection or {})
        batch_ids = [run.batch_id for run in runs.values()]

        contributions = [
            self._contribution(engine, runs[engine])
            for engine in OVERVIEW_ENGINES
            if engine in runs
        ]
        entries = self._entries(batch_ids)

        at_risk = sum(c.amount_at_risk_paise for c in contributions)
        recovered = sum(c.amount_recovered_paise for c in contributions)

        return OverviewSummary(
            generated_at=utcnow(),
            batch_ids={engine.value: run.batch_id for engine, run in runs.items()},
            engines_reporting=[e for e in OVERVIEW_ENGINES if e in runs],
            engines_missing=[e for e in OVERVIEW_ENGINES if e not in runs],
            entries=len(entries),
            amount_at_risk_paise=at_risk,
            amount_recovered_paise=recovered,
            recovery_rate=rate(recovered, at_risk),
            blended_caveat=BLENDED_CAVEAT,
            by_engine=contributions,
            by_source=self._by_source(entries),
            trust=trust_metrics(entries),
            policy_violations=sum(
                self._trail.batch_summary(b).policy_violations for b in batch_ids
            ),
            recent_activity=self._recent(entries, runs, recent_limit),
        )

    # --- internals --------------------------------------------------------

    def _contribution(self, engine: Engine, run: BatchRun) -> EngineContribution:
        summary = self._trail.batch_summary(run.batch_id)
        notes = run.notes or {}
        return EngineContribution(
            engine=engine,
            label=ENGINE_LABELS[engine],
            batch_id=run.batch_id,
            seed=run.seed,
            status=BatchStatus(run.status),
            started_at=run.started_at,
            completed_at=run.completed_at,
            dataset_batch_id=notes.get("dataset_batch_id"),
            entries=summary.entries,
            amount_at_risk_paise=summary.amount_at_risk_paise,
            amount_recovered_paise=summary.amount_recovered_paise,
            recovery_rate=summary.recovery_rate,
            by_source=summary.by_source,
            recovery_definition=RECOVERY_DEFINITIONS[engine],
        )

    def _recent(
        self,
        entries: list[AuditEntry],
        runs: dict[Engine, BatchRun],
        limit: int,
    ) -> list[AuditEntryRead]:
        """The latest decisions **from each engine**, not simply the latest overall.

        A plain newest-first sort does not do what it looks like it does here.
        The three engines do not share a clock: Engine 3 stamps its entries with
        its run clock (derived from the ledger), Engine 2 with each debit's
        scheduled time, and Engine 1 with wall-clock time. So "the newest entries
        across three batches" resolves to "every entry belongs to whichever engine
        was run most recently", and the overview's activity panel silently becomes
        a single-engine view.

        Taking a share per engine and then ordering them is the honest fix: the
        panel says it shows recent activity from each engine, and it does. The
        underlying clock inconsistency is a real defect and is not hidden by this
        — it is just not allowed to make the front page misleading.
        """
        if not runs:
            return []
        per_engine = max(1, limit // len(runs))
        by_engine: dict[str, list[AuditEntry]] = {}
        for entry in entries:
            by_engine.setdefault(entry.engine, []).append(entry)

        picked: list[AuditEntry] = []
        for rows in by_engine.values():
            rows.sort(key=lambda e: (e.timestamp, e.id), reverse=True)
            picked.extend(rows[:per_engine])
        picked.sort(key=lambda e: (e.timestamp, e.id), reverse=True)
        return [self._trail.read(e) for e in picked[:limit]]

    def _entries(self, batch_ids: list[str]) -> list[AuditEntry]:
        if not batch_ids:
            return []
        return list(
            self._db.scalars(select(AuditEntry).where(AuditEntry.batch_id.in_(batch_ids)))
        )

    def _by_source(self, entries: list[AuditEntry]) -> dict[str, SourceBreakdown]:
        buckets: dict[str, SourceBreakdown] = {
            s.value: SourceBreakdown() for s in ProvenanceSource
        }
        for entry in entries:
            bucket = buckets.setdefault(
                str(entry.provenance.get("source", "unknown")), SourceBreakdown()
            )
            bucket.entries += 1
            bucket.amount_at_risk_paise += entry.amount_at_risk_paise
            bucket.amount_recovered_paise += entry.amount_recovered_paise
        for bucket in buckets.values():
            bucket.recovery_rate = rate(
                bucket.amount_recovered_paise, bucket.amount_at_risk_paise
            )
        return buckets


# ---------------------------------------------------------------------------
# The trust strip
# ---------------------------------------------------------------------------


def trust_metrics(entries: list[AuditEntry]) -> list[TrustMetric]:
    """The counts that prove the bounds are real, each with why non-zero is good.

    Read off the same audit metadata the engines' own summaries read, so the
    overview and an engine view cannot report a different number of refusals for
    the same run. The metadata keys differ slightly between engines — Engines 1
    and 2 mark a suppressed retry `suppressed_retry`, Engine 3 marks a suppressed
    *message* `suppressed` — because they suppress different things, and each is
    counted under the heading that describes what was actually withheld.
    """
    meta: list[tuple[AuditEntry, dict[str, Any]]] = [
        (e, e.entry_metadata or {}) for e in entries
    ]

    # A permitted escalation is recorded `escalated` too, and counting it would
    # report every authorised handoff to a human as the gate having said no.
    denials = [
        e for e, m in meta if e.outcome in REFUSAL_OUTCOMES and m.get("permitted") is not True
    ]
    compliance = [e for e, m in meta if m.get("compliance_blocked")]
    retries_suppressed = [e for e, m in meta if m.get("suppressed_retry")]
    messages_suppressed = [e for e, m in meta if m.get("suppressed")]
    escalations = [e for e in entries if e.outcome == Outcome.ESCALATED.value]
    fallbacks = [
        e
        for e, m in meta
        if m.get("degraded")
        and e.provenance.get("source") == ProvenanceSource.DETERMINISTIC.value
    ]
    abstentions = [e for e in entries if e.provenance.get("abstained")]

    return [
        TrustMetric(
            key="policy_denials",
            label="Policy denials",
            value=len(denials),
            meaning=(
                "Actions the system proposed and its own rules refused. A run with "
                "none means the gate never fired."
            ),
            by_rule=_by_rule(denials),
        ),
        TrustMetric(
            key="compliance_blocked",
            label="Compliance-blocked",
            value=len(compliance),
            meaning=(
                "Debits refused because an RBI e-mandate precondition was not met. "
                "Each one is a charge a blind retrier would have attempted."
            ),
            by_rule=_by_rule(compliance),
        ),
        TrustMetric(
            key="retries_suppressed",
            label="Retries suppressed",
            value=len(retries_suppressed),
            meaning=(
                "Attempts on dead instruments and spent budgets that were never "
                "made. Wasted attempts avoided, not revenue forgone."
            ),
            by_rule=_by_rule(retries_suppressed),
        ),
        TrustMetric(
            key="messages_suppressed",
            label="Messages suppressed",
            value=len(messages_suppressed),
            meaning=(
                "Reminders withheld by quiet hours, a contact cap or a dispute "
                "freeze. The direct measure of harassment avoided."
            ),
            by_rule=_by_rule(messages_suppressed),
        ),
        TrustMetric(
            key="human_escalations",
            label="Human escalations",
            value=len(escalations),
            meaning=(
                "Cases handed to a person on evidence — a broken promise, a "
                "dispute, a high-value exposure — rather than acted on alone."
            ),
            by_rule=_by_rule(escalations),
        ),
        TrustMetric(
            key="deterministic_fallbacks",
            label="Deterministic fallbacks",
            value=len(fallbacks),
            meaning=(
                "Reasoning calls that degraded to their registered fallback and "
                "still completed. The batch finishes with no provider at all."
            ),
            by_rule=_by_rule(fallbacks),
        ),
        TrustMetric(
            key="abstentions",
            label="Abstentions",
            value=len(abstentions),
            meaning=(
                "Judgments the reasoning layer declined to make, routed to human "
                "review instead of guessed at."
            ),
            by_rule=_by_rule(abstentions),
        ),
    ]


def _by_rule(entries: list[AuditEntry]) -> dict[str, int]:
    return dict(Counter(e.authorising_rule for e in entries))


def rate(recovered: int, at_risk: int) -> float:
    """Recovery rate, rounded to four places.

    Zero at risk means zero rate, not 100% — the same convention `audit_trail`
    uses, and for the same reason: a metric that reads perfect on an empty
    denominator is the kind that gets quoted by accident.
    """
    if at_risk <= 0:
        return 0.0
    return round(recovered / at_risk, 4)


__all__ = [
    "ENGINE_LABELS",
    "OVERVIEW_ENGINES",
    "RECOVERY_DEFINITIONS",
    "REFUSAL_OUTCOMES",
    "OverviewService",
    "rate",
    "trust_metrics",
]
