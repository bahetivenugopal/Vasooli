"""The unified cross-engine run — three engines, one auditable unit.

Phase 7 §5.1. Until now the three engines only ever ran separately, and the
project's headline figure had to be assembled by hand from three demo scripts.
A number assembled by hand is a number with no provenance, and the seams between
three separately-built engines are exactly where a judge would look first.

## The one structural decision: how "one batch_id" is done

The phase file asks for **one `batch_id` spanning all three engines**, so the
whole run is auditable as a single unit. `BatchRun.batch_id` is a primary key
and `AuditTrail.start_batch()` refuses to reuse one — deliberately, because
reusing a batch id merges two runs into one set of metrics.

So the unified run gets a **run id**, and the three per-engine batch ids are
derived from it deterministically:

    unified-s42-a1b2c3   ->  unified-s42-a1b2c3-rc
                             unified-s42-a1b2c3-mr
                             unified-s42-a1b2c3-rcv

Each engine's `BatchRun.notes` records the `unified_run_id`, so the relationship
reads in both directions. One id still names the whole run — `batch_ids_for()`
turns it back into the three, and every read path in this module takes all three
together. What is *not* done is collapsing three engine runs into one row, which
would have cost the per-engine contribution split that the whole reporting model
rests on. See ADR 0012.

## What this module refuses to do

It does not compute a metric. The consolidated report's figures come from
`services/overview.py` — the same code path `/api/v1/overview` serves the
dashboard from — so "the console report and the dashboard agree exactly" is a
structural property rather than a thing that was checked once.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.db.types import utcnow
from app.models.batch import BatchRun
from app.models.enums import BatchStatus, Engine
from app.models.unified import EngineRunRecord, RunMode, UnifiedRunReport
from app.services.audit_trail import AuditTrail
from app.services.integrity import verify
from app.services.llm_agent import LLMAgent
from app.services.overview import OverviewService

logger = logging.getLogger(__name__)

#: Engine -> the suffix its batch id carries inside a unified run. Short because
#: the ids appear in console output, on the dashboard, and in `docs/RESULTS.md`,
#: and a batch id nobody can read aloud is a batch id nobody checks.
ENGINE_SUFFIX: dict[Engine, str] = {
    Engine.ROOT_CAUSE: "rc",
    Engine.MANDATE_RECOVERY: "mr",
    Engine.RECEIVABLES: "rcv",
}

#: Run order. Not arbitrary: Engine 1 is the cheapest and the one most likely to
#: surface a dataset problem, so a broken clone fails on it in seconds rather
#: than after Engine 3 has spent the reasoning quota reading 43 replies.
ENGINE_ORDER: tuple[Engine, ...] = (
    Engine.ROOT_CAUSE,
    Engine.MANDATE_RECOVERY,
    Engine.RECEIVABLES,
)


class UnifiedRunError(RuntimeError):
    """A leg of the unified run could not be started or completed."""


def unified_run_id(seed: int, *, salt: str | None = None) -> str:
    """A stable, readable id for one unified run.

    Deterministic in the seed so the same command produces the same id on any
    machine — §5.2's reproducibility property is worth very little if the ids
    move, because then two people cannot compare the same run. `salt` exists so
    a second run of the same seed on the same database can be given a distinct
    id without inventing an id format.
    """
    digest = hashlib.sha256(f"vasooli-unified:{seed}:{salt or ''}".encode()).hexdigest()
    return f"unified-s{seed}-{digest[:8]}"


def batch_ids_for(run_id: str) -> dict[Engine, str]:
    """The three per-engine batch ids belonging to one unified run id."""
    return {engine: f"{run_id}-{suffix}" for engine, suffix in ENGINE_SUFFIX.items()}


def describe_mode(agent: LLMAgent, razorpay_mode: str, config: Settings) -> RunMode:
    """State plainly which mode the run is in.

    §5.2: a judge without keys must still see the project work, *and the run must
    say which mode it is in*. Reporting the mode is the whole point — a run that
    silently fell back to deterministic reasoning produces honest numbers that
    mean something different from what the reader assumes.
    """
    if config.llm_deterministic_only:
        description = (
            "Reasoning: DETERMINISTIC ONLY (LLM_DETERMINISTIC_ONLY=true). Every "
            "reasoning task took its registered fallback; nothing was sent to a "
            "provider. Every result is audited as source: deterministic."
        )
    elif agent.provider_enabled:
        description = (
            f"Reasoning: LIVE PROVIDER ({config.gemini_model}). Reasoning tasks "
            "call the model, with the registered deterministic fallback behind "
            "each one; every result carries its own provenance."
        )
    else:
        description = (
            "Reasoning: DETERMINISTIC (no GEMINI_API_KEY set). Every reasoning "
            "task took its registered fallback — this is a supported mode, not a "
            "degraded one, and the run completes fully."
        )

    return RunMode(
        llm_provider_enabled=agent.provider_enabled,
        llm_deterministic_only=config.llm_deterministic_only,
        llm_model=config.gemini_model if agent.provider_enabled else None,
        razorpay_mode=razorpay_mode,
        description=(
            f"{description}  Razorpay: {razorpay_mode.upper()} "
            "(test-mode credentials only, never live)."
        ),
    )


@dataclass
class _Leg:
    """One engine's run, before it becomes a report row."""

    engine: Engine
    batch_id: str
    summary: Any
    elapsed_seconds: float


class UnifiedRunner:
    """Runs all three engines under one unified run id, then reports once."""

    def __init__(
        self,
        db: Session,
        *,
        agent: LLMAgent | None = None,
        config: Settings | None = None,
    ) -> None:
        self._db = db
        self._settings = config or default_settings
        self._agent = agent or LLMAgent(settings=self._settings)
        self._trail = AuditTrail(db)

    # --- the run -----------------------------------------------------------

    def run(
        self,
        *,
        seed: int,
        run_id: str | None = None,
        datasets: dict[Engine, Path | str] | None = None,
        now: dict[Engine, datetime] | None = None,
        progress: Callable[[Engine, str], None] | None = None,
    ) -> UnifiedRunReport:
        """Run Engine 1, 2 and 3 in order and produce one consolidated report.

        `now` is a **per-engine** override and defaults to nothing, on purpose.
        All three engines derive their run clock differently and each is right
        for its own data — Engine 1 from the last attempt, Engine 2 from the
        latest debit actually attempted, Engine 3 from the latest observed event
        rounded up to the hour (Phase 5). Forcing one clock across all three
        would make two of them wrong, and the failure is silent: every
        compliance gate fires for the wrong reason and the run still completes.
        """
        run_id = run_id or unified_run_id(seed)
        ids = batch_ids_for(run_id)
        datasets = datasets or {}
        clocks = now or {}

        for batch_id in ids.values():
            if self._db.get(BatchRun, batch_id) is not None:
                raise UnifiedRunError(
                    f"batch {batch_id!r} already exists — unified run {run_id!r} has "
                    f"already been performed against this database. Pass a salt to "
                    f"`unified_run_id()` for a second run of the same seed."
                )

        started = utcnow()
        wall_start = time.perf_counter()
        legs: list[_Leg] = []

        for engine in ENGINE_ORDER:
            if progress is not None:
                progress(engine, "starting")
            leg_start = time.perf_counter()
            try:
                summary = self._run_engine(
                    engine,
                    batch_id=ids[engine],
                    seed=seed,
                    dataset=datasets.get(engine),
                    now=clocks.get(engine),
                )
            except Exception as exc:
                # A leg that dies leaves its batch row `running` forever, and a
                # run stuck in `running` is invisible to the overview (which
                # reads completed runs only) while still occupying its batch id.
                # So it is marked failed before the error escapes: the next
                # attempt gets a clean answer about what happened rather than
                # "that batch id already exists".
                self._fail_run(ids[engine], run_id=run_id)
                raise UnifiedRunError(
                    f"unified run {run_id!r} failed on the {engine.value} leg "
                    f"({ids[engine]}): {exc}"
                ) from exc
            elapsed = time.perf_counter() - leg_start
            self._tag_run(ids[engine], run_id=run_id)
            legs.append(_Leg(engine, ids[engine], summary, elapsed))
            if progress is not None:
                progress(engine, "complete")

        completed = utcnow()
        return self._report(
            run_id=run_id,
            seed=seed,
            ids=ids,
            legs=legs,
            started=started,
            completed=completed,
            elapsed=time.perf_counter() - wall_start,
        )

    # --- one engine --------------------------------------------------------

    def _run_engine(
        self,
        engine: Engine,
        *,
        batch_id: str,
        seed: int,
        dataset: Path | str | None,
        now: datetime | None,
    ) -> Any:
        """Run one engine, sharing this runner's agent so the mode is uniform.

        Sharing the agent matters: three engines each constructing their own
        `LLMAgent` from ambient settings would each resolve the mode
        independently, and a report that says "deterministic" while one engine
        quietly had a provider is precisely the dishonesty §5.2 is guarding
        against.
        """
        # Imported here rather than at module scope: every engine runner reaches
        # for `data.generators.retry_model` at construction, which needs the repo
        # root importable. `app` must still import cleanly without it.
        if engine is Engine.ROOT_CAUSE:
            from app.engines.root_cause.runner import RootCauseRunner

            return RootCauseRunner(self._db, agent=self._agent).run(
                batch_id=batch_id, seed=seed, dataset=dataset, now=now
            ).summary
        if engine is Engine.MANDATE_RECOVERY:
            from app.engines.mandate_recovery.runner import MandateRunner

            return MandateRunner(self._db, agent=self._agent).run(
                batch_id=batch_id, seed=seed, dataset=dataset, now=now
            ).summary
        if engine is Engine.RECEIVABLES:
            from app.engines.receivables.runner import ReceivablesRunner

            return ReceivablesRunner(self._db, agent=self._agent).run(
                batch_id=batch_id, seed=seed, dataset=dataset, now=now
            ).summary
        raise UnifiedRunError(f"{engine!r} is not one of the three product engines")

    def _tag_run(self, batch_id: str, *, run_id: str) -> None:
        """Record the parent run id on the engine's own batch row.

        Written after the leg completes rather than passed into `start_batch()`,
        because none of the three runners accepts extra notes and widening three
        signatures for one key is a worse trade than one write here. `notes` is a
        mutable JSON column on `BatchRun`, not audit data — the append-only rule
        applies to `audit_entries`.
        """
        run = self._db.get(BatchRun, batch_id)
        if run is None:  # pragma: no cover - the leg just wrote it
            raise UnifiedRunError(f"engine leg {batch_id!r} left no batch row")
        notes = dict(run.notes or {})
        notes["unified_run_id"] = run_id
        run.notes = notes
        self._db.commit()

    def _fail_run(self, batch_id: str, *, run_id: str) -> None:
        """Mark a half-finished leg failed, and say which run it belonged to.

        Best-effort by design: this runs while an exception is already in flight,
        and a second exception from the cleanup would replace the real cause with
        a bookkeeping error. Whatever the leg managed to audit before it died
        stays in the trail — those entries are evidence about the failure, not
        debris.
        """
        try:
            self._db.rollback()
            run = self._db.get(BatchRun, batch_id)
            if run is None:
                return
            run.status = BatchStatus.FAILED.value
            run.completed_at = utcnow()
            notes = dict(run.notes or {})
            notes["unified_run_id"] = run_id
            run.notes = notes
            self._db.commit()
        except Exception:  # pragma: no cover - cleanup must not mask the cause
            logger.exception("could not mark %s failed", batch_id)

    # --- the report --------------------------------------------------------

    def _report(
        self,
        *,
        run_id: str,
        seed: int,
        ids: dict[Engine, str],
        legs: list[_Leg],
        started: datetime,
        completed: datetime,
        elapsed: float,
    ) -> UnifiedRunReport:
        selection: dict[Engine, str | None] = dict(ids)
        overview = OverviewService(self._db).summary(selection)
        by_engine = {c.engine: c for c in overview.by_engine}

        records: list[EngineRunRecord] = []
        for leg in legs:
            run = self._db.get(BatchRun, leg.batch_id)
            notes = (run.notes or {}) if run else {}
            contribution = by_engine.get(leg.engine)
            records.append(
                EngineRunRecord(
                    engine=leg.engine,
                    batch_id=leg.batch_id,
                    dataset_batch_id=notes.get("dataset_batch_id"),
                    dataset_path=notes.get("dataset_path"),
                    run_clock=_parse_clock(notes.get("now")),
                    entries=contribution.entries if contribution else 0,
                    amount_at_risk_paise=(
                        contribution.amount_at_risk_paise if contribution else 0
                    ),
                    amount_recovered_paise=(
                        contribution.amount_recovered_paise if contribution else 0
                    ),
                    recovery_rate=contribution.recovery_rate if contribution else 0.0,
                    headline=leg.summary.model_dump(mode="json"),
                    elapsed_seconds=round(leg.elapsed_seconds, 3),
                )
            )

        integrity = verify(self._db, [leg.batch_id for leg in legs])
        razorpay_mode = str(
            next(
                (
                    (self._db.get(BatchRun, leg.batch_id).notes or {}).get("razorpay_mode")
                    for leg in legs
                    if self._db.get(BatchRun, leg.batch_id) is not None
                ),
                self._settings.razorpay_mode,
            )
        )

        return UnifiedRunReport(
            run_id=run_id,
            seed=seed,
            started_at=started,
            completed_at=completed,
            elapsed_seconds=round(elapsed, 3),
            mode=describe_mode(self._agent, razorpay_mode, self._settings),
            batch_ids={engine.value: batch_id for engine, batch_id in ids.items()},
            engines=records,
            overview=overview,
            integrity_passed=integrity.passed,
            integrity_checks=integrity.checks,
            integrity_detail={
                "entries_checked": integrity.audit_check.entries,
                "schema_violations": [str(v) for v in integrity.audit_check.violations],
                "discrepancies": [str(d) for d in integrity.discrepancies],
                "orphan_actions": integrity.orphan_actions,
                "untraced_money": integrity.untraced_money,
                "suspicious": integrity.audit_check.suspicious,
                "rule_citations": integrity.audit_check.rule_citations,
                "provenance": integrity.audit_check.provenance,
            },
        )


def report_for(db: Session, run_id: str) -> UnifiedRunReport | None:
    """Rebuild a unified run's report from the database, after the fact.

    Everything in a report is derived from the trail, so a completed run can be
    re-reported at any time without re-running anything. Used by the tests that
    assert determinism, and by anyone re-checking a number after a demo.
    """
    ids = batch_ids_for(run_id)
    runs = {engine: db.get(BatchRun, bid) for engine, bid in ids.items()}
    if not any(runs.values()):
        return None

    overview = OverviewService(db).summary(
        {engine: bid for engine, bid in ids.items() if runs[engine] is not None}
    )
    by_engine = {c.engine: c for c in overview.by_engine}
    present = [(engine, bid) for engine, bid in ids.items() if runs[engine] is not None]

    records = []
    for engine, batch_id in present:
        run = runs[engine]
        assert run is not None
        notes = run.notes or {}
        contribution = by_engine.get(engine)
        records.append(
            EngineRunRecord(
                engine=engine,
                batch_id=batch_id,
                dataset_batch_id=notes.get("dataset_batch_id"),
                dataset_path=notes.get("dataset_path"),
                run_clock=_parse_clock(notes.get("now")),
                entries=contribution.entries if contribution else 0,
                amount_at_risk_paise=contribution.amount_at_risk_paise if contribution else 0,
                amount_recovered_paise=(
                    contribution.amount_recovered_paise if contribution else 0
                ),
                recovery_rate=contribution.recovery_rate if contribution else 0.0,
                headline=notes.get("run_summary") or {},
            )
        )

    integrity = verify(db, [bid for _, bid in present])
    first = runs[present[0][0]]
    assert first is not None
    notes = first.notes or {}

    return UnifiedRunReport(
        run_id=run_id,
        seed=first.seed,
        started_at=first.started_at,
        completed_at=max(
            (r.completed_at or r.started_at for r in runs.values() if r is not None),
        ),
        elapsed_seconds=0.0,
        mode=RunMode(
            llm_provider_enabled=bool(notes.get("llm_provider_enabled")),
            llm_deterministic_only=not notes.get("llm_provider_enabled"),
            llm_model=None,
            razorpay_mode=str(notes.get("razorpay_mode", "unknown")),
            description="Reconstructed from the stored run notes, not from a live run.",
        ),
        batch_ids={engine.value: bid for engine, bid in present},
        engines=records,
        overview=overview,
        integrity_passed=integrity.passed,
        integrity_checks=integrity.checks,
        integrity_detail={
            "entries_checked": integrity.audit_check.entries,
            "schema_violations": [str(v) for v in integrity.audit_check.violations],
            "discrepancies": [str(d) for d in integrity.discrepancies],
            "orphan_actions": integrity.orphan_actions,
            "untraced_money": integrity.untraced_money,
            "suspicious": integrity.audit_check.suspicious,
        },
    )


def fingerprint(report: UnifiedRunReport) -> dict[str, Any]:
    """The part of a run that a second run with the same seed must reproduce.

    Deliberately not the whole report. Batch ids differ when a salt is used, wall
    time obviously differs, and Engine 1 stamps its audit entries with wall-clock
    time (a known defect, recorded in Phase 6) — so an entry-by-entry comparison
    would report every run as non-deterministic and prove nothing.

    What must be identical is every number: the money, the rates, the counts, and
    each engine's own headline metrics. That is what "the same seed reproduces
    identical results" means, and it is what `docs/RESULTS.md` quotes.
    """
    return {
        "seed": report.seed,
        "amount_at_risk_paise": report.overview.amount_at_risk_paise,
        "amount_recovered_paise": report.overview.amount_recovered_paise,
        "recovery_rate": report.overview.recovery_rate,
        "entries": report.overview.entries,
        "policy_violations": report.overview.policy_violations,
        "trust": {t.key: t.value for t in report.overview.trust},
        "by_source": {
            source: (bucket.amount_at_risk_paise, bucket.amount_recovered_paise)
            for source, bucket in sorted(report.overview.by_source.items())
        },
        "engines": {
            record.engine.value: {
                "entries": record.entries,
                "amount_at_risk_paise": record.amount_at_risk_paise,
                "amount_recovered_paise": record.amount_recovered_paise,
                "recovery_rate": record.recovery_rate,
                "headline": _stable_headline(record.headline),
            }
            for record in report.engines
        },
    }


#: Fields inside an engine's `RunSummary` that legitimately differ between two
#: runs of the same seed, because they name the run rather than describe it.
_UNSTABLE_HEADLINE_KEYS = frozenset({"batch_id"})


def _stable_headline(headline: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in sorted(headline.items()) if k not in _UNSTABLE_HEADLINE_KEYS}


def _parse_clock(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:  # pragma: no cover - notes are written by us
        return None


__all__ = [
    "ENGINE_ORDER",
    "UnifiedRunError",
    "UnifiedRunner",
    "batch_ids_for",
    "describe_mode",
    "fingerprint",
    "report_for",
    "unified_run_id",
]
