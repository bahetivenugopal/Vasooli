"""Metric integrity — proving the numbers are consistent and honest.

Phase 7 §5.3. Three things report the same figures: the audit trail itself, the
summary API, and the dashboard. Everywhere in this project the *intent* is that
all three are one computation read three ways — but intent is not a property, and
"we always recompute from the trail" is exactly the kind of claim that is true
until the one place it isn't.

So this module recomputes every headline metric a fourth time, from raw
`audit_entries` rows, with arithmetic that shares no code path with
`audit_trail.batch_summary()` or `services/overview.py`, and asserts the three
agree exactly. A discrepancy here is a defect to fix, never a rounding issue to
explain away.

It also proves two traceability properties that no single number can show:

- **No orphan actions.** Every recovery action traces to an authorising policy
  decision, and every policy decision traces to a named rule that exists.
- **No untraced money.** Every rupee counted as recovered sits on an entry that
  names the action that brought it back, and every recovered rupee was booked as
  at risk first. A numerator with no denominator is how a recovery rate reads
  above 100%, which Engine 3 did produce once (Phase 5, deviation #6).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.audit import AuditEntry
from app.models.enums import Action, Engine, Outcome, ProvenanceSource
from app.services.audit_trail import AuditTrail
from app.services.audit_validation import (
    AuditCheckReport,
    check_audit,
    load_entries,
    rule_exists,
)
from app.services.overview import OverviewService
from app.services.razorpay_client import API_CALL_RULE

#: Actions that can carry recovered money. Anything else booking a recovery is a
#: bug: a reminder does not settle an invoice, and a diagnosis does not settle a
#: payment.
#:
#: Three actions, because the three engines book money at three different points
#: in their own loops, and each is right for its own shape:
#:
#: - Engine 1 books on `schedule_retry`, the entry that authorised the retry,
#:   resolved to `success` once the retry model returns. The decision entry is
#:   written *before* the side effect, so the outcome is genuinely unknown at
#:   write time — that is the one documented exception to append-only.
#: - Engine 2 books on `attempt_charge`, the debit itself.
#: - Engine 3 books on `record_promise_to_pay`, because what it measures is a
#:   payment that honoured a commitment the engine tracked (Phase 5, deviation
#:   #6 — at-risk and recovered must land on the *same* entry, or the per-source
#:   rate goes above 100%).
RECOVERY_BEARING_ACTIONS: frozenset[str] = frozenset(
    {
        Action.SCHEDULE_RETRY.value,
        Action.ATTEMPT_CHARGE.value,
        Action.RECORD_PROMISE_TO_PAY.value,
    }
)

#: Actions that commit the system to doing something to a customer or an
#: instrument. Each must cite a rule that resolves — this is the "no orphan
#: actions" property, stated as a list rather than left implicit.
RECOVERY_ACTIONS: frozenset[str] = frozenset(
    {
        Action.ATTEMPT_CHARGE.value,
        Action.SCHEDULE_RETRY.value,
        Action.REROUTE_TRAFFIC.value,
        Action.SEND_DUNNING.value,
        Action.SEND_REMINDER.value,
        Action.SEND_PRE_DEBIT_NOTICE.value,
        Action.RECORD_PROMISE_TO_PAY.value,
        Action.ESCALATE.value,
        Action.HALT_SCHEDULE.value,
        Action.BLOCK_ATTEMPT.value,
    }
)

#: Citations that record *that* something happened rather than *what permitted
#: it*. A recovery action citing only one of these was logged but not authorised.
_NON_AUTHORISING_CITATIONS: frozenset[str] = frozenset({API_CALL_RULE})


class Discrepancy(BaseModel):
    """One place two sources disagreed about the same number."""

    metric: str
    scope: str
    recomputed: Any
    reported: Any
    source: str

    def __str__(self) -> str:  # pragma: no cover - console rendering
        return (
            f"{self.scope}.{self.metric}: recomputed {self.recomputed!r} != "
            f"{self.source} {self.reported!r}"
        )


class Recomputation(BaseModel):
    """Headline metrics derived from raw entries, independently of every summary."""

    batch_ids: list[str]
    entries: int
    amount_at_risk_paise: int
    amount_recovered_paise: int
    recovery_rate: float
    by_engine: dict[str, dict[str, int]] = Field(default_factory=dict)
    by_source: dict[str, dict[str, int]] = Field(default_factory=dict)
    outcome_counts: dict[str, int] = Field(default_factory=dict)


class IntegrityReport(BaseModel):
    """The verification pass, in one object.

    `passed` is the conjunction of everything: the audit-check validations, the
    cross-source comparison, and the two traceability properties. Anything less
    than all of them is a fail, because a partial pass is what a skipped check
    looks like from the outside.
    """

    recomputed: Recomputation
    audit_check: AuditCheckReport
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    orphan_actions: list[str] = Field(default_factory=list)
    untraced_money: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.audit_check.passed
            and not self.discrepancies
            and not self.orphan_actions
            and not self.untraced_money
        )

    @property
    def checks(self) -> dict[str, bool]:
        """Every check by name, so a report can never say "passed" while silent."""
        return {
            "audit_schema_validations": self.audit_check.passed,
            "cross_source_metrics_agree": not self.discrepancies,
            "no_orphan_actions": not self.orphan_actions,
            "no_untraced_money": not self.untraced_money,
        }


# ---------------------------------------------------------------------------
# Recomputation — deliberately not sharing code with batch_summary()
# ---------------------------------------------------------------------------


def recompute(db: Session, batch_ids: list[str]) -> Recomputation:
    """Headline metrics, summed by hand from the rows.

    This function exists to be a *second opinion*. It does not call
    `batch_summary()`, `OverviewService`, or any engine's `RunSummary` — if it
    did, the comparison it feeds would be a tautology.
    """
    entries = load_entries(db, batch_ids)

    at_risk = 0
    recovered = 0
    by_engine: dict[str, dict[str, int]] = defaultdict(
        lambda: {"entries": 0, "amount_at_risk_paise": 0, "amount_recovered_paise": 0}
    )
    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"entries": 0, "amount_at_risk_paise": 0, "amount_recovered_paise": 0}
    )

    for entry in entries:
        at_risk += entry.amount_at_risk_paise
        recovered += entry.amount_recovered_paise

        engine_bucket = by_engine[entry.engine]
        engine_bucket["entries"] += 1
        engine_bucket["amount_at_risk_paise"] += entry.amount_at_risk_paise
        engine_bucket["amount_recovered_paise"] += entry.amount_recovered_paise

        source = str((entry.provenance or {}).get("source", "unknown"))
        source_bucket = by_source[source]
        source_bucket["entries"] += 1
        source_bucket["amount_at_risk_paise"] += entry.amount_at_risk_paise
        source_bucket["amount_recovered_paise"] += entry.amount_recovered_paise

    return Recomputation(
        batch_ids=list(batch_ids),
        entries=len(entries),
        amount_at_risk_paise=at_risk,
        amount_recovered_paise=recovered,
        recovery_rate=round(recovered / at_risk, 4) if at_risk > 0 else 0.0,
        by_engine=dict(by_engine),
        by_source=dict(by_source),
        outcome_counts=dict(sorted(Counter(e.outcome for e in entries).items())),
    )


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


def find_orphan_actions(entries: list[AuditEntry]) -> list[str]:
    """Recovery actions with no authorising policy decision behind them.

    The authorising decision is recorded **on the action's own entry**, not on a
    preceding one: `PolicyDecision.audit_fields()` maps straight onto
    `audit_trail.record()`, so the rule that permitted an action and the record
    of the action are the same row. That is the design, and it means an orphan is
    an entry whose citation does not name a policy decision — not one that lacks
    a predecessor. A healthy mandate charged on its first due debit has exactly
    one entry, cites `policy_engine:permitted`, and is fully authorised.

    So two things make an action an orphan:

    1. It cites no rule, or cites one that resolves to nothing. That is the
       Phase 1 tell — an entry citing no policy rule is an engine that acted on
       a model recommendation without going through `authorize()`.
    2. It cites only the Razorpay logging marker. `razorpay-api:call.logged`
       names a logging convention, not an authorisation; an action carrying it
       as its sole citation was recorded but never permitted.
    """
    out: list[str] = []
    for entry in entries:
        if entry.action not in RECOVERY_ACTIONS:
            continue
        citation = entry.authorising_rule or ""
        if not citation or not rule_exists(citation):
            out.append(
                f"entry {entry.id} ({entry.engine}/{entry.action} on "
                f"{entry.entity_id}) cites {citation!r}, which resolves to no rule"
            )
        elif citation in _NON_AUTHORISING_CITATIONS:
            out.append(
                f"entry {entry.id} ({entry.engine}/{entry.action} on "
                f"{entry.entity_id}) cites {citation!r}, which records that a call "
                "happened rather than what permitted it"
            )
    return out


def find_untraced_money(entries: list[AuditEntry]) -> list[str]:
    """Recovered rupees that no audited action accounts for.

    Three ways money goes untraced, all of which have actually happened in this
    project at least once:

    - Recovery booked on an action that cannot recover anything.
    - Recovery booked on an entry with no at-risk amount, so the numerator has no
      denominator (Engine 3's 101.96% per-source rate, Phase 5).
    - Recovery booked on an entry whose outcome is not a success.
    """
    out: list[str] = []
    for entry in entries:
        if entry.amount_recovered_paise <= 0:
            continue
        if entry.action not in RECOVERY_BEARING_ACTIONS:
            out.append(
                f"entry {entry.id} books {entry.amount_recovered_paise} paise "
                f"recovered on action {entry.action!r}, which cannot recover money"
            )
        if entry.amount_at_risk_paise <= 0:
            out.append(
                f"entry {entry.id} books {entry.amount_recovered_paise} paise "
                "recovered but nothing at risk — a numerator with no denominator"
            )
        elif entry.amount_recovered_paise > entry.amount_at_risk_paise:
            out.append(
                f"entry {entry.id} recovered more than it put at risk "
                f"({entry.amount_recovered_paise} > {entry.amount_at_risk_paise})"
            )
        if entry.outcome != Outcome.SUCCESS.value:
            out.append(
                f"entry {entry.id} books money recovered on a "
                f"{entry.outcome!r} outcome"
            )
    return out


# ---------------------------------------------------------------------------
# The cross-source comparison
# ---------------------------------------------------------------------------


def verify(db: Session, batch_ids: list[str]) -> IntegrityReport:
    """Recompute, compare against every reporting path, and check traceability."""
    entries = load_entries(db, batch_ids)
    recomputed = recompute(db, batch_ids)
    trail = AuditTrail(db)

    discrepancies: list[Discrepancy] = []

    # --- 1. Against `audit_trail.batch_summary()`, per batch ---------------
    summed_at_risk = 0
    summed_recovered = 0
    summed_entries = 0
    for batch_id in batch_ids:
        summary = trail.batch_summary(batch_id)
        per_batch = recompute(db, [batch_id])
        summed_at_risk += summary.amount_at_risk_paise
        summed_recovered += summary.amount_recovered_paise
        summed_entries += summary.entries
        for metric, mine, theirs in (
            ("entries", per_batch.entries, summary.entries),
            (
                "amount_at_risk_paise",
                per_batch.amount_at_risk_paise,
                summary.amount_at_risk_paise,
            ),
            (
                "amount_recovered_paise",
                per_batch.amount_recovered_paise,
                summary.amount_recovered_paise,
            ),
            ("recovery_rate", per_batch.recovery_rate, summary.recovery_rate),
        ):
            if mine != theirs:
                discrepancies.append(
                    Discrepancy(
                        metric=metric,
                        scope=batch_id,
                        recomputed=mine,
                        reported=theirs,
                        source="batch_summary",
                    )
                )

    for metric, mine, theirs in (
        ("entries", recomputed.entries, summed_entries),
        ("amount_at_risk_paise", recomputed.amount_at_risk_paise, summed_at_risk),
        ("amount_recovered_paise", recomputed.amount_recovered_paise, summed_recovered),
    ):
        if mine != theirs:
            discrepancies.append(
                Discrepancy(
                    metric=metric,
                    scope="unified",
                    recomputed=mine,
                    reported=theirs,
                    source="sum(batch_summary)",
                )
            )

    # --- 2. Against the overview the dashboard renders ---------------------
    #
    # The dashboard displays exactly this response and computes nothing itself
    # (Phase 6's one structural rule), so agreeing with the overview *is*
    # agreeing with the UI. There is no fourth number rendered in the browser.
    selection = _selection_for(db, batch_ids)
    if selection:
        overview = OverviewService(db).summary(selection)
        for metric, mine, theirs in (
            ("entries", recomputed.entries, overview.entries),
            (
                "amount_at_risk_paise",
                recomputed.amount_at_risk_paise,
                overview.amount_at_risk_paise,
            ),
            (
                "amount_recovered_paise",
                recomputed.amount_recovered_paise,
                overview.amount_recovered_paise,
            ),
            ("recovery_rate", recomputed.recovery_rate, overview.recovery_rate),
        ):
            if mine != theirs:
                discrepancies.append(
                    Discrepancy(
                        metric=metric,
                        scope="unified",
                        recomputed=mine,
                        reported=theirs,
                        source="overview (dashboard)",
                    )
                )

        for contribution in overview.by_engine:
            mine = recomputed.by_engine.get(contribution.engine.value, {})
            for metric, key in (
                ("entries", "entries"),
                ("amount_at_risk_paise", "amount_at_risk_paise"),
                ("amount_recovered_paise", "amount_recovered_paise"),
            ):
                if mine.get(key, 0) != getattr(contribution, metric):
                    discrepancies.append(
                        Discrepancy(
                            metric=metric,
                            scope=contribution.engine.value,
                            recomputed=mine.get(key, 0),
                            reported=getattr(contribution, metric),
                            source="overview (dashboard)",
                        )
                    )

        for source in (ProvenanceSource.MODEL, ProvenanceSource.DETERMINISTIC):
            mine = recomputed.by_source.get(source.value, {})
            theirs = overview.by_source.get(source.value)
            if theirs is None:
                continue
            if mine.get("amount_recovered_paise", 0) != theirs.amount_recovered_paise:
                discrepancies.append(
                    Discrepancy(
                        metric="amount_recovered_paise",
                        scope=f"source:{source.value}",
                        recomputed=mine.get("amount_recovered_paise", 0),
                        reported=theirs.amount_recovered_paise,
                        source="overview (dashboard)",
                    )
                )

    return IntegrityReport(
        recomputed=recomputed,
        audit_check=check_audit(db, batch_ids),
        discrepancies=discrepancies,
        orphan_actions=find_orphan_actions(entries),
        untraced_money=find_untraced_money(entries),
    )


def _selection_for(db: Session, batch_ids: list[str]) -> dict[Engine, str | None]:
    """Map the given batches onto the overview's per-engine selection.

    Only the three product engines are selectable; a `core` batch has no
    contribution row, by design (`services/overview.py`).
    """
    from app.models.batch import BatchRun

    selection: dict[Engine, str | None] = {}
    for batch_id in batch_ids:
        run = db.get(BatchRun, batch_id)
        if run is None:
            continue
        try:
            engine = Engine(run.engine)
        except ValueError:  # pragma: no cover - unreachable with valid data
            continue
        if engine is Engine.CORE:
            continue
        selection[engine] = batch_id
    return selection


__all__ = [
    "Discrepancy",
    "IntegrityReport",
    "Recomputation",
    "find_orphan_actions",
    "find_untraced_money",
    "recompute",
    "verify",
]
