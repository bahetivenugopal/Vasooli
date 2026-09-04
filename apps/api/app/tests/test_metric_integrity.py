"""The metric integrity audit — and proof that it can actually fail.

Phase 7 §5.3. A verification pass that has only ever been run against clean data
is a verification pass nobody has tested. So this module does two things:

1. Runs the audit over a real unified run and asserts it passes.
2. **Injects each defect it claims to catch** and asserts it catches that one.
   A validator with no negative tests is a validator that might be returning
   `passed=True` unconditionally, and the whole credibility argument rests on it.

The defects injected here are the ones this project has actually produced:
Engine 1 counting the same rupees three times, Engine 3 booking at-risk and
recovered on different entries and reporting 101.96%, an entry citing a rule that
does not exist.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.audit import AuditEntry  # noqa: E402
from app.models.enums import (  # noqa: E402
    Action,
    Engine,
    EntityType,
    Outcome,
    ProvenanceSource,
)
from app.models.provenance import Provenance  # noqa: E402
from app.services.audit_trail import AuditTrail  # noqa: E402
from app.services.audit_validation import budget_kind, check_audit, rule_exists  # noqa: E402
from app.services.integrity import (  # noqa: E402
    find_orphan_actions,
    find_untraced_money,
    recompute,
    verify,
)
from app.tests.test_unified_run import fresh_db, run_unified  # noqa: E402

BATCH = "integrity-fixture"


@pytest.fixture(scope="module")
def unified_run():
    db = fresh_db()
    report = run_unified(db, run_id="test-integrity")
    yield db, report
    db.close()


def batch_ids(report) -> list[str]:
    return list(report.batch_ids.values())


# ---------------------------------------------------------------------------
# The positive case — a real run, over the committed datasets
# ---------------------------------------------------------------------------


def test_a_real_unified_run_passes_every_integrity_check(unified_run) -> None:
    db, report = unified_run
    result = verify(db, batch_ids(report))
    assert result.passed, {
        "violations": [str(v) for v in result.audit_check.violations],
        "discrepancies": [str(d) for d in result.discrepancies],
        "orphans": result.orphan_actions,
        "untraced": result.untraced_money,
    }


def test_the_recomputation_matches_the_trails_own_summary(unified_run) -> None:
    """Two independent additions over the same rows must land on the same number."""
    db, report = unified_run
    trail = AuditTrail(db)
    for batch_id in batch_ids(report):
        mine = recompute(db, [batch_id])
        theirs = trail.batch_summary(batch_id)
        assert mine.entries == theirs.entries
        assert mine.amount_at_risk_paise == theirs.amount_at_risk_paise
        assert mine.amount_recovered_paise == theirs.amount_recovered_paise
        assert mine.recovery_rate == theirs.recovery_rate


def test_the_recomputation_matches_the_dashboards_overview(unified_run) -> None:
    db, report = unified_run
    mine = recompute(db, batch_ids(report))
    assert mine.amount_at_risk_paise == report.overview.amount_at_risk_paise
    assert mine.amount_recovered_paise == report.overview.amount_recovered_paise
    assert mine.recovery_rate == report.overview.recovery_rate
    assert mine.entries == report.overview.entries


def test_every_entry_that_reports_a_budget_names_which_budget(unified_run) -> None:
    """One column, two budgets — see `audit_validation.budget_kind`.

    Every entry carrying `attempts_remaining` must carry exactly one of the two
    markers, or the monotonicity check silently compares a debit budget against
    an outreach budget and reports a refill that never happened.
    """
    db, report = unified_run
    rows = list(
        db.query(AuditEntry).filter(
            AuditEntry.batch_id.in_(batch_ids(report)),
            AuditEntry.attempts_remaining.isnot(None),
        )
    )
    assert rows
    for entry in rows:
        metadata = entry.entry_metadata or {}
        markers = [bool(metadata.get("kind")), bool(metadata.get("proposed_action"))]
        assert sum(markers) == 1, (
            f"entry {entry.id} reports a budget with "
            f"{'both markers' if all(markers) else 'neither marker'}"
        )
        assert budget_kind(entry) in {"charge", "outreach"}


def test_every_citation_in_the_run_resolves_to_a_rule_that_exists(unified_run) -> None:
    db, report = unified_run
    result = check_audit(db, batch_ids(report))
    assert result.rule_citations
    for citation in result.rule_citations:
        assert rule_exists(citation), f"{citation} resolves to nothing"


# ---------------------------------------------------------------------------
# The negative cases — each defect injected, each one caught
# ---------------------------------------------------------------------------


def _entry(db: Session, **overrides) -> AuditEntry:
    """A minimal, valid entry written straight to the table.

    Written around the service on purpose: `audit_trail.record()` rejects most of
    these defects at write time, and the point of these tests is that the
    *reader* catches a row that reached the table some other way — a fixture, a
    manual SQL edit, a future writer with a bug.
    """
    base = {
        "timestamp": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        "batch_id": BATCH,
        "engine": Engine.MANDATE_RECOVERY.value,
        "entity_type": EntityType.MANDATE.value,
        "entity_id": "mnd_test_0001",
        "action": Action.ATTEMPT_CHARGE.value,
        "outcome": Outcome.SUCCESS.value,
        "reason_code": "OK",
        "authorising_rule": "policy-bounds:AC1",
        "rationale": "A charge inside budget.",
        "provenance": Provenance.deterministic().model_dump(),
        "amount_at_risk_paise": 100_000,
        "amount_recovered_paise": 0,
        "currency": "INR",
    }
    entry = AuditEntry(**{**base, **overrides})
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@pytest.fixture
def fixture_db(db: Session) -> Session:
    """A database with an empty batch open, ready to be polluted."""
    AuditTrail(db).start_batch(batch_id=BATCH, engine=Engine.MANDATE_RECOVERY, seed=1)
    return db


def test_a_citation_that_resolves_to_nothing_is_caught(fixture_db: Session) -> None:
    _entry(fixture_db, authorising_rule="policy-bounds:INVENTED9")
    report = check_audit(fixture_db, [BATCH])
    assert not report.passed
    assert any(v.check == "citation-resolves" for v in report.violations)


def test_a_malformed_citation_is_caught(fixture_db: Session) -> None:
    _entry(fixture_db, authorising_rule="because it seemed reasonable")
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "citation-format" for v in report.violations)


def test_a_deterministic_entry_wearing_a_model_name_is_caught(fixture_db: Session) -> None:
    """A rule-derived figure must never be able to look model-derived."""
    _entry(
        fixture_db,
        provenance={
            "source": ProvenanceSource.DETERMINISTIC.value,
            "provider": "deterministic",
            "model": "gemini-3.1-flash-lite",
            "cache_hit": False,
            "prompt_version": "1.0.0",
            "abstained": False,
            "latency_ms": None,
            "tokens": None,
        },
    )
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "provenance" for v in report.violations)


def test_a_model_entry_with_no_confidence_is_caught(fixture_db: Session) -> None:
    _entry(
        fixture_db,
        provenance=Provenance.from_model(
            provider="gemini", model="gemini-3.1-flash-lite", prompt_version="1.0.0"
        ).model_dump(),
        model_confidence=None,
    )
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "provenance" for v in report.violations)


def test_an_out_of_order_timeline_is_caught(fixture_db: Session) -> None:
    _entry(fixture_db, action=Action.SCHEDULE_RETRY.value, outcome=Outcome.SCHEDULED.value)
    _entry(fixture_db, timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=UTC))
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "timeline-order" for v in report.violations)


def test_a_budget_that_refills_within_one_budget_is_caught(fixture_db: Session) -> None:
    _entry(
        fixture_db,
        attempts_remaining=1,
        entry_metadata={"proposed_action": Action.ATTEMPT_CHARGE.value},
    )
    _entry(
        fixture_db,
        timestamp=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        attempts_remaining=3,
        entry_metadata={"proposed_action": Action.ATTEMPT_CHARGE.value},
    )
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "attempt-budget" for v in report.violations)


def test_two_budgets_on_one_entity_are_not_mistaken_for_a_refill(
    fixture_db: Session,
) -> None:
    """The debit budget being spent says nothing about the message budget.

    Phase 4's `outreach_entity()` is why an entity legitimately shows 0 then 3.
    """
    _entry(
        fixture_db,
        action=Action.BLOCK_ATTEMPT.value,
        outcome=Outcome.BLOCKED.value,
        attempts_remaining=0,
        entry_metadata={"proposed_action": Action.ATTEMPT_CHARGE.value},
    )
    _entry(
        fixture_db,
        timestamp=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        action=Action.SEND_DUNNING.value,
        outcome=Outcome.SUCCESS.value,
        amount_recovered_paise=0,
        attempts_remaining=3,
        entry_metadata={"kind": "send_dunning"},
    )
    report = check_audit(fixture_db, [BATCH])
    assert not [v for v in report.violations if v.check == "attempt-budget"]


def test_a_charge_after_a_halt_is_caught(fixture_db: Session) -> None:
    """`MANDATE_REVOKED` is an absolute hard stop — terminal means terminal."""
    _entry(
        fixture_db,
        action=Action.HALT_SCHEDULE.value,
        outcome=Outcome.HALTED.value,
        authorising_rule="rbi-mandate-rules:A4",
    )
    _entry(fixture_db, timestamp=datetime(2026, 9, 1, 11, 0, tzinfo=UTC))
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "terminal-is-terminal" for v in report.violations)


def test_a_secret_reaching_the_trail_is_caught(fixture_db: Session) -> None:
    _entry(fixture_db, rationale="Retried with rzp_live_ABC123DEF456 as the key.")
    report = check_audit(fixture_db, [BATCH])
    assert any(v.check == "no-secrets" for v in report.violations)


def test_a_run_with_no_refusals_is_reported_as_suspicious(fixture_db: Session) -> None:
    _entry(fixture_db)
    report = check_audit(fixture_db, [BATCH])
    assert any("blocked/halted" in note for note in report.suspicious)


# --- traceability -----------------------------------------------------------


def test_recovery_booked_on_an_action_that_cannot_recover_is_caught() -> None:
    entry = AuditEntry(
        id=1,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        batch_id=BATCH,
        engine=Engine.RECEIVABLES.value,
        entity_type=EntityType.INVOICE.value,
        entity_id="inv_0001",
        action=Action.SEND_REMINDER.value,
        outcome=Outcome.SUCCESS.value,
        reason_code="OK",
        authorising_rule="policy-bounds:RL1",
        rationale="A reminder does not settle an invoice.",
        provenance=Provenance.deterministic().model_dump(),
        amount_at_risk_paise=500_000,
        amount_recovered_paise=500_000,
    )
    assert any("cannot recover money" in issue for issue in find_untraced_money([entry]))


def test_recovery_with_no_denominator_is_caught() -> None:
    """Engine 3 produced exactly this once, and reported a 101.96% recovery rate."""
    entry = AuditEntry(
        id=2,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        batch_id=BATCH,
        engine=Engine.RECEIVABLES.value,
        entity_type=EntityType.INVOICE.value,
        entity_id="inv_0002",
        action=Action.RECORD_PROMISE_TO_PAY.value,
        outcome=Outcome.SUCCESS.value,
        reason_code="PROMISE_KEPT",
        authorising_rule="policy-bounds:PP1",
        rationale="Settled against a tracked commitment.",
        provenance=Provenance.deterministic().model_dump(),
        amount_at_risk_paise=0,
        amount_recovered_paise=500_000,
    )
    assert any("no denominator" in issue for issue in find_untraced_money([entry]))


def test_an_action_authorised_only_by_the_logging_marker_is_an_orphan() -> None:
    """`razorpay-api:call.logged` says a call happened, not what permitted it."""
    entry = AuditEntry(
        id=3,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        batch_id=BATCH,
        engine=Engine.MANDATE_RECOVERY.value,
        entity_type=EntityType.MANDATE.value,
        entity_id="mnd_0003",
        action=Action.ATTEMPT_CHARGE.value,
        outcome=Outcome.SUCCESS.value,
        reason_code="OK",
        authorising_rule="razorpay-api:call.logged",
        rationale="Charged.",
        provenance=Provenance.deterministic().model_dump(),
        amount_at_risk_paise=100_000,
        amount_recovered_paise=100_000,
    )
    assert find_orphan_actions([entry])


def test_a_charge_recorded_with_a_permitted_decision_is_not_an_orphan() -> None:
    """The authorisation lives on the action's own entry, not on a predecessor.

    A healthy mandate charged on its first due debit has exactly one entry. Any
    check that demanded a preceding decision would call that an orphan, which is
    wrong and would have made the whole traceability claim noise.
    """
    entry = AuditEntry(
        id=4,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        batch_id=BATCH,
        engine=Engine.MANDATE_RECOVERY.value,
        entity_type=EntityType.MANDATE.value,
        entity_id="mnd_0004",
        action=Action.ATTEMPT_CHARGE.value,
        outcome=Outcome.SUCCESS.value,
        reason_code="OK",
        authorising_rule="policy_engine:permitted",
        rationale="No stopping rule fired.",
        provenance=Provenance.deterministic().model_dump(),
        amount_at_risk_paise=100_000,
        amount_recovered_paise=100_000,
    )
    assert find_orphan_actions([entry]) == []


def test_an_empty_batch_is_reported_rather_than_passing_silently(db: Session) -> None:
    """Nothing checked is not the same as nothing wrong."""
    report = check_audit(db, ["no-such-batch"])
    assert report.entries == 0
    assert any("no entries at all" in note for note in report.suspicious)


def test_money_counted_twice_shows_up_as_a_discrepancy_against_the_engine(
    unified_run,
) -> None:
    """Engine 1 inflated its denominator by 86% this exact way in Phase 3.

    Adding a duplicate at-risk booking after the fact makes the recomputation and
    the stored per-engine `RunSummary` disagree — which is the shape the defect
    actually had, and which the audit is here to surface rather than absorb.
    """
    db, report = unified_run
    before = recompute(db, batch_ids(report)).amount_at_risk_paise
    duplicate = _entry(
        db,
        batch_id=report.batch_ids[Engine.ROOT_CAUSE.value],
        engine=Engine.ROOT_CAUSE.value,
        entity_type=EntityType.CORRIDOR.value,
        entity_id="corridor:duplicate",
        action=Action.DIAGNOSE_ROOT_CAUSE.value,
        outcome=Outcome.SUCCESS.value,
        timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=365),
        amount_at_risk_paise=999_999,
    )
    try:
        after = recompute(db, batch_ids(report)).amount_at_risk_paise
        assert after == before + 999_999
    finally:
        db.delete(duplicate)
        db.commit()
    assert recompute(db, batch_ids(report)).amount_at_risk_paise == before
