"""The audit trail — the single append-only decision record.

Every decision any engine ever makes goes through this writer. An action that
happened without an entry is unprovable, and a later phase asserts against it.

Three properties this module exists to guarantee:

1. **Every entry cites the rule that authorised it** — refusals especially. A
   blocked attempt is the evidence that the stopping rules are real.
2. **Append-only.** The one permitted mutation is resolving a `pending` outcome.
   There is no generic update and no delete on this service, deliberately: an
   API that can rewrite history is an audit trail nobody has to believe.
3. **The summary is computed from the log**, never from a parallel counter, so
   the headline number and its evidence cannot disagree.

Schema source of truth: the `audit-schema` skill.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.types import utcnow
from app.models.audit import AuditEntry, AuditEntryRead, BatchSummary, SourceBreakdown
from app.models.batch import BatchRun
from app.models.enums import (
    Action,
    BatchStatus,
    Engine,
    EntityType,
    Outcome,
    ProvenanceSource,
)
from app.models.provenance import Provenance


class AuditIntegrityError(RuntimeError):
    """Raised when a write would violate the audit trail's own rules.

    Loud on purpose. A silently dropped or malformed entry is worse than a
    crashed batch, because the batch that crashed is the one you notice.
    """


#: Rule-citation format from `audit-schema`: `<skill-or-module>:<rule-id>`.
_CITATION_SEPARATOR = ":"


def _validate_citation(authorising_rule: str) -> None:
    """A citation points at a *specific* named rule, never a description.

    The check is deliberately shallow — it catches "because it seemed fine", not
    a well-formed citation to a rule that does not exist. The policy engine
    covers the latter by only ever emitting ids from its own registry.
    """
    if not authorising_rule or _CITATION_SEPARATOR not in authorising_rule:
        raise AuditIntegrityError(
            f"authorising_rule {authorising_rule!r} is not a citation. "
            "Expected '<skill-or-module>:<rule-id>', e.g. 'rbi-mandate-rules:A4'. "
            "If no rule id exists, add a named rule to the skill — do not invent "
            "a citation string at the call site."
        )


class AuditTrail:
    """Writer and reader for the audit log.

    Holds a session rather than opening its own, so an engine's decisions and
    its audit entries commit or roll back together.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # --- batch bookkeeping ------------------------------------------------

    def start_batch(
        self,
        *,
        batch_id: str,
        engine: Engine,
        seed: int,
        notes: dict[str, Any] | None = None,
    ) -> BatchRun:
        """Open a batch run. Every entry written afterwards cites this id."""
        if self._db.get(BatchRun, batch_id) is not None:
            raise AuditIntegrityError(
                f"batch {batch_id!r} already exists — reusing a batch id would "
                "merge two runs into one set of metrics"
            )
        run = BatchRun(
            batch_id=batch_id,
            engine=engine.value,
            seed=seed,
            status=BatchStatus.RUNNING.value,
            started_at=utcnow(),
            notes=notes,
        )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)
        return run

    def complete_batch(
        self, batch_id: str, *, status: BatchStatus = BatchStatus.COMPLETED
    ) -> BatchRun:
        """Close a batch and snapshot its summary.

        The snapshot is a convenience for the dashboard. Anything that reports a
        figure recomputes it from the entries, so a stale snapshot can never
        become the number someone quotes.
        """
        run = self._require_batch(batch_id)
        run.status = status.value
        run.completed_at = utcnow()
        run.summary = self.batch_summary(batch_id).model_dump()
        self._db.commit()
        self._db.refresh(run)
        return run

    def get_batch(self, batch_id: str) -> BatchRun | None:
        return self._db.get(BatchRun, batch_id)

    def list_batches(self, *, engine: Engine | None = None, limit: int = 50) -> list[BatchRun]:
        stmt = select(BatchRun).order_by(BatchRun.started_at.desc()).limit(limit)
        if engine is not None:
            stmt = stmt.where(BatchRun.engine == engine.value)
        return list(self._db.scalars(stmt))

    def _require_batch(self, batch_id: str) -> BatchRun:
        run = self._db.get(BatchRun, batch_id)
        if run is None:
            raise AuditIntegrityError(f"unknown batch {batch_id!r}")
        return run

    # --- the write path ---------------------------------------------------

    def record(
        self,
        *,
        batch_id: str,
        engine: Engine,
        entity_type: EntityType,
        entity_id: str,
        action: Action,
        outcome: Outcome,
        reason_code: str,
        authorising_rule: str,
        rationale: str,
        provenance: Provenance,
        amount_at_risk_paise: int = 0,
        amount_recovered_paise: int = 0,
        currency: str = "INR",
        attempt_number: int | None = None,
        attempts_remaining: int | None = None,
        model_confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> AuditEntry:
        """Write one entry. The only way anything enters this table.

        Call this **before** the side effect it describes: a record for an action
        that then failed is just an entry with `outcome = failure`, but an action
        with no record at all is unprovable.
        """
        _validate_citation(authorising_rule)
        if not reason_code:
            raise AuditIntegrityError("reason_code is required on every entry")
        if not rationale.strip():
            raise AuditIntegrityError(
                "rationale is required — an entry nobody can read is not an explanation"
            )
        if provenance.source is ProvenanceSource.MODEL and model_confidence is None:
            raise AuditIntegrityError(
                "model_confidence is required whenever provenance.source is 'model' "
                "(audit-schema -> Required fields)"
            )
        if amount_at_risk_paise < 0 or amount_recovered_paise < 0:
            raise AuditIntegrityError("amounts are non-negative integer paise")

        entry = AuditEntry(
            timestamp=timestamp or utcnow(),
            batch_id=batch_id,
            engine=engine.value,
            entity_type=entity_type.value,
            entity_id=entity_id,
            action=action.value,
            outcome=outcome.value,
            reason_code=reason_code,
            authorising_rule=authorising_rule,
            rationale=rationale,
            provenance=provenance.model_dump(mode="json"),
            amount_at_risk_paise=amount_at_risk_paise,
            amount_recovered_paise=amount_recovered_paise,
            currency=currency,
            attempt_number=attempt_number,
            attempts_remaining=attempts_remaining,
            model_confidence=model_confidence,
            entry_metadata=metadata,
        )
        self._db.add(entry)
        self._db.commit()
        self._db.refresh(entry)
        return entry

    def resolve_outcome(
        self,
        entry_id: int,
        *,
        outcome: Outcome,
        amount_recovered_paise: int | None = None,
    ) -> AuditEntry:
        """Resolve a `pending` entry once its result is known.

        This is the **only** mutation the service exposes, and it only moves an
        entry off `pending`. Anything already resolved stays resolved: a
        correction is a new entry, not an edit, so the trail keeps both the
        original judgment and the correction.
        """
        entry = self._db.get(AuditEntry, entry_id)
        if entry is None:
            raise AuditIntegrityError(f"unknown audit entry {entry_id}")
        if entry.outcome != Outcome.PENDING.value:
            raise AuditIntegrityError(
                f"audit entry {entry_id} is already resolved as {entry.outcome!r}. "
                "Entries are append-only: record a correction as a new entry."
            )
        if outcome is Outcome.PENDING:
            raise AuditIntegrityError("resolving to 'pending' is not a resolution")
        if amount_recovered_paise is not None:
            if amount_recovered_paise < 0:
                raise AuditIntegrityError("amount_recovered_paise must be non-negative")
            entry.amount_recovered_paise = amount_recovered_paise
        entry.outcome = outcome.value
        self._db.commit()
        self._db.refresh(entry)
        return entry

    # --- the read path ----------------------------------------------------

    def get(self, entry_id: int) -> AuditEntry | None:
        return self._db.get(AuditEntry, entry_id)

    def query(
        self,
        *,
        batch_id: str | None = None,
        engine: Engine | None = None,
        entity_type: EntityType | None = None,
        entity_id: str | None = None,
        action: Action | None = None,
        outcome: Outcome | None = None,
        source: ProvenanceSource | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AuditEntry]:
        """Filtered fetch, newest first."""
        stmt = select(AuditEntry)
        if batch_id is not None:
            stmt = stmt.where(AuditEntry.batch_id == batch_id)
        if engine is not None:
            stmt = stmt.where(AuditEntry.engine == engine.value)
        if entity_type is not None:
            stmt = stmt.where(AuditEntry.entity_type == entity_type.value)
        if entity_id is not None:
            stmt = stmt.where(AuditEntry.entity_id == entity_id)
        if action is not None:
            stmt = stmt.where(AuditEntry.action == action.value)
        if outcome is not None:
            stmt = stmt.where(AuditEntry.outcome == outcome.value)
        stmt = stmt.order_by(AuditEntry.timestamp.desc(), AuditEntry.id.desc())

        # `provenance` is a JSON column; filtering it in Python keeps this
        # backend-agnostic and costs nothing at batch scale. Slicing happens
        # after the filter so a page is never short of rows that matched.
        if source is not None:
            rows = [e for e in self._db.scalars(stmt) if e.provenance.get("source") == source.value]
            return rows[offset : offset + limit]
        return list(self._db.scalars(stmt.offset(offset).limit(limit)))

    def entity_timeline(self, entity_type: EntityType, entity_id: str) -> list[AuditEntry]:
        """Every decision about one entity, oldest first — the dashboard view."""
        stmt = (
            select(AuditEntry)
            .where(AuditEntry.entity_type == entity_type.value)
            .where(AuditEntry.entity_id == entity_id)
            .order_by(AuditEntry.timestamp.asc(), AuditEntry.id.asc())
        )
        return list(self._db.scalars(stmt))

    def batch_summary(self, batch_id: str) -> BatchSummary:
        """The headline metrics for one batch, computed from the entries.

        Reported **per provenance source**. Blending model-derived and
        rule-derived results into one recovery rate is not a false number, but as
        a single figure it implies more than it delivers.
        """
        entries = list(self._db.scalars(select(AuditEntry).where(AuditEntry.batch_id == batch_id)))

        at_risk = sum(e.amount_at_risk_paise for e in entries)
        recovered = sum(e.amount_recovered_paise for e in entries)

        by_source: dict[str, SourceBreakdown] = {s.value: SourceBreakdown() for s in ProvenanceSource}
        for e in entries:
            bucket = by_source.setdefault(
                str(e.provenance.get("source", "unknown")), SourceBreakdown()
            )
            bucket.entries += 1
            bucket.amount_at_risk_paise += e.amount_at_risk_paise
            bucket.amount_recovered_paise += e.amount_recovered_paise
        for bucket in by_source.values():
            bucket.recovery_rate = _rate(bucket.amount_recovered_paise, bucket.amount_at_risk_paise)

        return BatchSummary(
            batch_id=batch_id,
            entries=len(entries),
            amount_at_risk_paise=at_risk,
            amount_recovered_paise=recovered,
            recovery_rate=_rate(recovered, at_risk),
            action_counts=dict(Counter(e.action for e in entries)),
            outcome_counts=dict(Counter(e.outcome for e in entries)),
            engine_counts=dict(Counter(e.engine for e in entries)),
            by_source=by_source,
            policy_violations=sum(1 for e in entries if _violates_schema(e)),
            blocked_count=sum(1 for e in entries if e.outcome == Outcome.BLOCKED.value),
            halted_count=sum(1 for e in entries if e.outcome == Outcome.HALTED.value),
            escalated_count=sum(1 for e in entries if e.outcome == Outcome.ESCALATED.value),
            abstained_count=sum(1 for e in entries if e.provenance.get("abstained")),
        )

    def read(self, entry: AuditEntry) -> AuditEntryRead:
        """ORM row -> API shape, revalidating provenance on the way out."""
        return AuditEntryRead.model_validate(entry)


def _rate(recovered: int, at_risk: int) -> float:
    """Recovery rate, rounded to four places.

    Zero at risk means zero rate, not 100%: a batch that recovered nothing from
    nothing has proved nothing, and a metric that reads perfect when the
    denominator is empty is the kind that gets quoted by accident.
    """
    if at_risk <= 0:
        return 0.0
    return round(recovered / at_risk, 4)


def _violates_schema(entry: AuditEntry) -> bool:
    """Does this stored entry break the rules its own schema promises?

    Checked at read time as well as write time. The writer cannot be bypassed
    from application code, but a row that arrived some other way — a fixture, a
    manual SQL edit — should be counted and shown, not silently summarised over.
    """
    if not entry.authorising_rule or _CITATION_SEPARATOR not in entry.authorising_rule:
        return True
    if not entry.reason_code or not entry.rationale:
        return True
    provenance = entry.provenance or {}
    if provenance.get("source") not in {s.value for s in ProvenanceSource}:
        return True
    return bool(
        provenance.get("source") == ProvenanceSource.MODEL.value and entry.model_confidence is None
    )
