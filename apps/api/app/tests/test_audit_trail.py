"""Audit trail tests.

What is being defended here: entries are written correctly and completely, the
log cannot be rewritten through the service, and the batch summary computes from
the log rather than from anything that could drift away from it.

The zero-recovery case is tested explicitly. A recovery rate that reads well when
nothing was recovered is the one metric bug nobody notices until a judge asks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import (
    Action,
    BatchStatus,
    Engine,
    EntityType,
    Outcome,
    ProvenanceSource,
)
from app.models.provenance import Provenance
from app.services.audit_trail import AuditIntegrityError, AuditTrail

BATCH = "batch_test_001"


@pytest.fixture
def batch(trail: AuditTrail):
    return trail.start_batch(batch_id=BATCH, engine=Engine.ROOT_CAUSE, seed=42)


def write(trail: AuditTrail, **overrides):
    base = {
        "batch_id": BATCH,
        "engine": Engine.ROOT_CAUSE,
        "entity_type": EntityType.PAYMENT,
        "entity_id": "pay_0001",
        "action": Action.SCHEDULE_RETRY,
        "outcome": Outcome.SCHEDULED,
        "reason_code": "INSUFFICIENT_FUNDS",
        "authorising_rule": "decline-taxonomy:SOFT.INSUFFICIENT_FUNDS",
        "rationale": "Soft decline, retry inside a bounded schedule.",
        "provenance": Provenance.deterministic(),
        "amount_at_risk_paise": 250_000,
    }
    return trail.record(**{**base, **overrides})


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_an_entry_is_written_with_everything_the_schema_requires(trail, batch):
    entry = write(trail)
    read = trail.read(entry)
    assert read.batch_id == BATCH
    assert read.authorising_rule == "decline-taxonomy:SOFT.INSUFFICIENT_FUNDS"
    assert read.rationale
    assert read.provenance.source is ProvenanceSource.DETERMINISTIC
    assert read.amount_at_risk_paise == 250_000
    assert read.timestamp.tzinfo is not None, "timestamps are tz-aware UTC"


def test_an_entry_without_a_citation_is_refused(trail, batch):
    """A blocked action with no citing rule is a bug, not a gap."""
    with pytest.raises(AuditIntegrityError, match="not a citation"):
        write(trail, authorising_rule="because it seemed reasonable")


def test_an_entry_without_a_rationale_is_refused(trail, batch):
    with pytest.raises(AuditIntegrityError, match="rationale"):
        write(trail, rationale="   ")


def test_a_model_derived_entry_must_carry_its_confidence(trail, batch):
    """`audit-schema`: model_confidence is required whenever source is `model`."""
    model_provenance = Provenance.from_model(
        provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
    )
    with pytest.raises(AuditIntegrityError, match="model_confidence"):
        write(trail, provenance=model_provenance)

    entry = write(trail, provenance=model_provenance, model_confidence=0.82)
    assert trail.read(entry).model_confidence == 0.82


def test_deterministic_provenance_cannot_masquerade_as_model_derived():
    """A fallback reporting a model name would make ruled results look reasoned."""
    with pytest.raises(ValueError, match="must leave"):
        Provenance(
            source=ProvenanceSource.DETERMINISTIC,
            provider="deterministic",
            model="gemini-3.1-flash-lite",
        )


def test_a_blocked_action_is_a_first_class_entry(trail, batch):
    """Refusals are the compliance evidence, so they record like anything else."""
    entry = write(
        trail,
        action=Action.BLOCK_ATTEMPT,
        outcome=Outcome.BLOCKED,
        reason_code="ATTEMPT_BUDGET_EXHAUSTED",
        authorising_rule="policy-bounds:AC1",
        rationale="4 of 4 permitted attempts used.",
    )
    assert trail.read(entry).outcome is Outcome.BLOCKED


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_the_service_exposes_no_delete():
    """Not 'delete is guarded' — delete does not exist on the API at all."""
    assert not [name for name in dir(AuditTrail) if "delete" in name.lower()]


def test_the_service_exposes_no_generic_update():
    """`resolve_outcome` is the one mutation, and its name says what it does."""
    mutators = [
        name
        for name in dir(AuditTrail)
        if not name.startswith("_") and name in {"update", "edit", "patch", "set"}
    ]
    assert mutators == []


def test_a_pending_outcome_resolves_once(trail, batch):
    entry = write(trail, outcome=Outcome.PENDING, amount_recovered_paise=0)
    resolved = trail.resolve_outcome(
        entry.id, outcome=Outcome.SUCCESS, amount_recovered_paise=250_000
    )
    assert resolved.outcome == Outcome.SUCCESS.value
    assert resolved.amount_recovered_paise == 250_000


def test_a_resolved_entry_cannot_be_resolved_again(trail, batch):
    """A correction is a new entry, not an edit — the trail keeps both."""
    entry = write(trail, outcome=Outcome.PENDING)
    trail.resolve_outcome(entry.id, outcome=Outcome.SUCCESS)
    with pytest.raises(AuditIntegrityError, match="already resolved"):
        trail.resolve_outcome(entry.id, outcome=Outcome.FAILURE)


def test_a_settled_entry_cannot_be_reopened(trail, batch):
    entry = write(trail, outcome=Outcome.BLOCKED)
    with pytest.raises(AuditIntegrityError, match="already resolved"):
        trail.resolve_outcome(entry.id, outcome=Outcome.SUCCESS)


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------


def test_entries_filter_by_engine_entity_and_action(trail, batch):
    write(trail, entity_id="pay_0001")
    write(trail, entity_id="pay_0002", action=Action.BLOCK_ATTEMPT, outcome=Outcome.BLOCKED)

    assert len(trail.query(batch_id=BATCH)) == 2
    assert len(trail.query(entity_id="pay_0002")) == 1
    assert len(trail.query(action=Action.BLOCK_ATTEMPT)) == 1
    assert len(trail.query(engine=Engine.RECEIVABLES)) == 0


def test_entries_filter_by_provenance_source(trail, batch):
    write(trail)
    write(
        trail,
        provenance=Provenance.from_model(
            provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
        ),
        model_confidence=0.9,
    )
    assert len(trail.query(source=ProvenanceSource.MODEL)) == 1
    assert len(trail.query(source=ProvenanceSource.DETERMINISTIC)) == 1


def test_an_entity_timeline_reads_oldest_first(trail, batch):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    write(trail, timestamp=now + timedelta(hours=1), reason_code="SECOND")
    write(trail, timestamp=now, reason_code="FIRST")
    timeline = trail.entity_timeline(EntityType.PAYMENT, "pay_0001")
    assert [e.reason_code for e in timeline] == ["FIRST", "SECOND"]


# ---------------------------------------------------------------------------
# The batch summary — the headline number
# ---------------------------------------------------------------------------


def test_a_batch_with_zero_recoveries_reports_a_zero_rate(trail, batch):
    """The metric cannot be accidentally hardcoded optimistic."""
    write(trail, amount_at_risk_paise=500_000, amount_recovered_paise=0)
    write(trail, amount_at_risk_paise=300_000, amount_recovered_paise=0)
    summary = trail.batch_summary(BATCH)
    assert summary.amount_at_risk_paise == 800_000
    assert summary.amount_recovered_paise == 0
    assert summary.recovery_rate == 0.0


def test_an_empty_batch_reports_zero_not_one(trail, batch):
    """Nothing recovered from nothing is 0%, never a flattering 100%."""
    summary = trail.batch_summary(BATCH)
    assert summary.entries == 0
    assert summary.recovery_rate == 0.0


def test_the_summary_computes_from_the_log(trail, batch):
    write(trail, amount_at_risk_paise=400_000, amount_recovered_paise=100_000)
    write(trail, amount_at_risk_paise=600_000, amount_recovered_paise=0)
    summary = trail.batch_summary(BATCH)
    assert summary.entries == 2
    assert summary.amount_at_risk_paise == 1_000_000
    assert summary.amount_recovered_paise == 100_000
    assert summary.recovery_rate == 0.1


def test_the_summary_reports_per_source_never_blended(trail, batch):
    """A rate blending template output and model judgment implies too much."""
    write(trail, amount_at_risk_paise=100_000, amount_recovered_paise=100_000)
    write(
        trail,
        amount_at_risk_paise=100_000,
        amount_recovered_paise=0,
        provenance=Provenance.from_model(
            provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
        ),
        model_confidence=0.7,
    )
    summary = trail.batch_summary(BATCH)
    assert summary.by_source["deterministic"].recovery_rate == 1.0
    assert summary.by_source["model"].recovery_rate == 0.0
    assert summary.recovery_rate == 0.5


def test_the_summary_counts_the_compliance_evidence(trail, batch):
    write(trail, action=Action.BLOCK_ATTEMPT, outcome=Outcome.BLOCKED)
    write(trail, action=Action.HALT_SCHEDULE, outcome=Outcome.HALTED)
    write(
        trail,
        action=Action.ESCALATE,
        outcome=Outcome.ESCALATED,
        provenance=Provenance.deterministic(abstained=True),
    )
    summary = trail.batch_summary(BATCH)
    assert summary.blocked_count == 1
    assert summary.halted_count == 1
    assert summary.escalated_count == 1
    assert summary.abstained_count == 1
    assert summary.policy_violations == 0


def test_the_summary_counts_action_and_outcome_breakdowns(trail, batch):
    write(trail)
    write(trail, action=Action.BLOCK_ATTEMPT, outcome=Outcome.BLOCKED)
    summary = trail.batch_summary(BATCH)
    assert summary.action_counts[Action.SCHEDULE_RETRY.value] == 1
    assert summary.outcome_counts[Outcome.BLOCKED.value] == 1
    assert summary.engine_counts[Engine.ROOT_CAUSE.value] == 2


def test_a_row_that_bypassed_the_writer_is_counted_as_a_violation(trail, db, batch):
    """A row inserted some other way is shown, not silently summarised over."""
    from app.models.audit import AuditEntry

    db.add(
        AuditEntry(
            batch_id=BATCH,
            engine=Engine.ROOT_CAUSE.value,
            entity_type=EntityType.PAYMENT.value,
            entity_id="pay_bad",
            action=Action.SCHEDULE_RETRY.value,
            outcome=Outcome.SCHEDULED.value,
            reason_code="X",
            authorising_rule="no-citation-here",
            rationale="inserted directly",
            provenance={"source": "deterministic", "provider": "deterministic"},
        )
    )
    db.commit()
    assert trail.batch_summary(BATCH).policy_violations == 1


# ---------------------------------------------------------------------------
# Batch bookkeeping
# ---------------------------------------------------------------------------


def test_a_batch_id_cannot_be_reused(trail, batch):
    """Reusing an id would merge two runs into one set of metrics."""
    with pytest.raises(AuditIntegrityError, match="already exists"):
        trail.start_batch(batch_id=BATCH, engine=Engine.ROOT_CAUSE, seed=42)


def test_completing_a_batch_snapshots_its_summary(trail, batch):
    write(trail, amount_at_risk_paise=100_000, amount_recovered_paise=50_000)
    run = trail.complete_batch(BATCH)
    assert run.status == BatchStatus.COMPLETED.value
    assert run.completed_at is not None
    assert run.summary["recovery_rate"] == 0.5
    assert run.seed == 42, "every reported number travels with its seed"
