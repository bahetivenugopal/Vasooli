"""The unified cross-engine run, and the properties the demo depends on.

Everything worked in isolation before this phase. Nothing had been proven to
work *together*, and the demo depends entirely on the together case — so these
are the seams, asserted rather than assumed:

- one command runs all three engines and produces one consolidated report
- the console report and the dashboard's overview are the **same numbers**
- the same seed reproduces identical results
- the run says which mode it is in, and completes with no keys at all
- a leg that dies does not leave a batch stuck `running`

These tests run the real engines over the committed sample datasets. They are
slower than the rest of the suite (a few seconds each), which is why the run is
module-scoped and shared.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import (  # noqa: E402,F401 - registers the tables
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
    InvoiceChaseState,
    InvoiceCommunication,
    MandateCommunication,
    MandateRecoveryState,
    PromiseToPay,
)
from app.models.enums import BatchStatus, Engine, ProvenanceSource  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402
from app.services.overview import OverviewService  # noqa: E402
from app.services.unified_run import (  # noqa: E402
    ENGINE_ORDER,
    UnifiedRunError,
    UnifiedRunner,
    batch_ids_for,
    fingerprint,
    report_for,
    unified_run_id,
)

SEED = 42


def fresh_db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def no_keys_settings() -> Settings:
    """The judge-with-no-keys configuration, which the suite runs in throughout."""
    return Settings(gemini_api_key="", llm_deterministic_only=True)


def run_unified(db: Session, *, run_id: str, seed: int = SEED):
    config = no_keys_settings()
    return UnifiedRunner(db, agent=LLMAgent(settings=config), config=config).run(
        seed=seed, run_id=run_id
    )


@pytest.fixture(scope="module")
def unified():
    db = fresh_db()
    report = run_unified(db, run_id="test-unified-a")
    yield db, report
    db.close()


# ---------------------------------------------------------------------------
# One command, one report
# ---------------------------------------------------------------------------


def test_the_unified_run_covers_all_three_engines(unified) -> None:
    _db, report = unified
    assert {r.engine for r in report.engines} == set(ENGINE_ORDER)
    assert report.overview.engines_missing == []
    assert len(report.batch_ids) == 3


def test_every_leg_completed_and_carries_the_unified_run_id(unified) -> None:
    db, report = unified
    for engine, batch_id in batch_ids_for(report.run_id).items():
        run = db.get(BatchRun, batch_id)
        assert run is not None, f"{engine.value} left no batch row"
        assert run.status == BatchStatus.COMPLETED.value
        assert (run.notes or {}).get("unified_run_id") == report.run_id


def test_the_report_carries_the_provenance_a_number_needs(unified) -> None:
    """Seed, dataset, run clock, mode. A figure without these is not evidence."""
    _db, report = unified
    assert report.seed == SEED
    for record in report.engines:
        assert record.dataset_batch_id, f"{record.engine} did not name its dataset"
        assert record.run_clock is not None, f"{record.engine} did not report its clock"
    assert "recomputed from the audit trail" in report.methodology


def test_each_engine_derives_its_own_run_clock(unified) -> None:
    """All three are right for their own data, and they do not agree.

    Pinned because the temptation to "fix" this by forcing one clock across the
    unified run is real, and the failure it causes is silent: every compliance
    gate fires for the wrong reason and the run still completes.
    """
    _db, report = unified
    clocks = {r.engine: r.run_clock for r in report.engines}
    assert len({c.isoformat() for c in clocks.values() if c}) > 1


# ---------------------------------------------------------------------------
# The console report and the dashboard agree — by construction
# ---------------------------------------------------------------------------


def test_the_console_report_and_the_dashboard_overview_are_the_same_numbers(
    unified,
) -> None:
    db, report = unified
    selection: dict[Engine, str | None] = dict(batch_ids_for(report.run_id))
    dashboard = OverviewService(db).summary(selection)

    assert dashboard.amount_at_risk_paise == report.overview.amount_at_risk_paise
    assert dashboard.amount_recovered_paise == report.overview.amount_recovered_paise
    assert dashboard.recovery_rate == report.overview.recovery_rate
    assert dashboard.entries == report.overview.entries
    assert {t.key: t.value for t in dashboard.trust} == {
        t.key: t.value for t in report.overview.trust
    }


def test_the_dashboards_default_view_lands_on_the_unified_run(unified) -> None:
    """No batch ids passed: the overview picks the latest completed run per engine.

    That is the path the front page actually takes, and after a unified run those
    three runs are the unified run's three. If they were not, the console would
    report one thing and the dashboard another with nobody having chosen it.
    """
    db, report = unified
    default = OverviewService(db).summary()
    assert set(default.batch_ids.values()) == set(report.batch_ids.values())
    assert default.amount_recovered_paise == report.overview.amount_recovered_paise


def test_the_overview_route_serves_the_console_reports_numbers(unified) -> None:
    """The last link in the chain: through FastAPI, over the wire, as JSON.

    The service-level check above proves the arithmetic. This proves the route
    that the browser actually calls hands back that arithmetic unchanged — which
    is the claim "console report and dashboard overview agree exactly" reduced to
    something a test can fail on.
    """
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    db, report = unified
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = TestClient(app).get("/api/v1/overview")
        assert response.status_code == 200
        body = response.json()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert body["amount_at_risk_paise"] == report.overview.amount_at_risk_paise
    assert body["amount_recovered_paise"] == report.overview.amount_recovered_paise
    assert body["recovery_rate"] == report.overview.recovery_rate
    assert body["entries"] == report.overview.entries
    assert body["policy_violations"] == 0
    assert set(body["batch_ids"].values()) == set(report.batch_ids.values())
    assert {t["key"]: t["value"] for t in body["trust"]} == {
        t.key: t.value for t in report.overview.trust
    }


def test_the_per_engine_contributions_sum_to_the_headline(unified) -> None:
    _db, report = unified
    assert sum(c.amount_at_risk_paise for c in report.overview.by_engine) == (
        report.overview.amount_at_risk_paise
    )
    assert sum(c.amount_recovered_paise for c in report.overview.by_engine) == (
        report.overview.amount_recovered_paise
    )


def test_every_contribution_ships_its_own_recovery_definition(unified) -> None:
    """The caveat travels with the number, so it cannot be quoted without it."""
    _db, report = unified
    for contribution in report.overview.by_engine:
        assert len(contribution.recovery_definition) > 80
    assert report.overview.blended_caveat


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_the_run_passes_its_own_integrity_audit(unified) -> None:
    _db, report = unified
    assert report.integrity_passed, report.integrity_detail
    assert all(report.integrity_checks.values())
    assert report.integrity_detail["schema_violations"] == []
    assert report.integrity_detail["discrepancies"] == []


def test_the_gate_actually_fired_somewhere_in_the_run(unified) -> None:
    """A run with no refusals is suspicious, not clean — Phase 1 said so first."""
    _db, report = unified
    trust = {t.key: t.value for t in report.overview.trust}
    assert trust["policy_denials"] > 0
    assert report.overview.policy_violations == 0


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_the_same_seed_reproduces_identical_results(unified) -> None:
    """Every number, not every row.

    Batch ids differ by construction and Engine 1 stamps its entries with
    wall-clock time, so an entry-by-entry comparison would call every run
    non-deterministic and prove nothing. What must be identical is the money, the
    rates and the counts — which is what gets quoted.
    """
    _db, first = unified
    second_db = fresh_db()
    try:
        second = run_unified(second_db, run_id="test-unified-b")
        assert fingerprint(second) == fingerprint(first)
    finally:
        second_db.close()


def test_the_run_id_is_derived_from_the_seed_and_is_stable() -> None:
    assert unified_run_id(42) == unified_run_id(42)
    assert unified_run_id(42) != unified_run_id(7)
    assert unified_run_id(42, salt="second") != unified_run_id(42)
    assert unified_run_id(42).startswith("unified-s42-")


def test_a_completed_run_can_be_re_reported_from_the_database(unified) -> None:
    """Every figure is derived, so nothing has to be kept in memory to re-quote it."""
    db, report = unified
    rebuilt = report_for(db, report.run_id)
    assert rebuilt is not None
    assert rebuilt.overview.amount_recovered_paise == report.overview.amount_recovered_paise
    assert rebuilt.integrity_passed == report.integrity_passed


def test_report_for_an_unknown_run_is_none(unified) -> None:
    db, _report = unified
    assert report_for(db, "unified-s42-nothing") is None


# ---------------------------------------------------------------------------
# The no-keys mode, reported rather than assumed
# ---------------------------------------------------------------------------


def test_the_run_completes_with_no_api_keys_and_says_so(unified) -> None:
    _db, report = unified
    assert report.mode.llm_provider_enabled is False
    assert report.mode.llm_deterministic_only is True
    assert "DETERMINISTIC" in report.mode.description
    assert report.mode.razorpay_mode == "simulated"
    assert report.overview.entries > 0, "a mode that produces no work is not a mode"


def test_with_no_provider_every_entry_is_audited_as_ruled(unified) -> None:
    db, report = unified
    entries = list(
        db.query(AuditEntry).filter(
            AuditEntry.batch_id.in_(list(report.batch_ids.values()))
        )
    )
    assert entries
    assert all(
        e.provenance["source"] == ProvenanceSource.DETERMINISTIC.value for e in entries
    )


# ---------------------------------------------------------------------------
# Refusing to run twice, and failing cleanly
# ---------------------------------------------------------------------------


def test_reusing_a_unified_run_id_is_refused(unified) -> None:
    """Reusing an id would merge two runs into one set of metrics."""
    db, report = unified
    with pytest.raises(UnifiedRunError, match="already exists"):
        run_unified(db, run_id=report.run_id)


def test_a_leg_that_dies_does_not_leave_a_batch_running() -> None:
    """A run stuck `running` is invisible to the overview and holds its batch id."""
    db = fresh_db()
    try:
        config = no_keys_settings()
        runner = UnifiedRunner(db, agent=LLMAgent(settings=config), config=config)
        with pytest.raises(UnifiedRunError, match="mandate_recovery leg"):
            runner.run(
                seed=SEED,
                run_id="test-unified-broken",
                datasets={Engine.MANDATE_RECOVERY: REPO_ROOT / "does" / "not" / "exist.jsonl"},
            )
        ids = batch_ids_for("test-unified-broken")
        assert db.get(BatchRun, ids[Engine.ROOT_CAUSE]).status == BatchStatus.COMPLETED.value
        # Engine 2 never opened a batch — its dataset is resolved before the
        # trail is touched, which is the fail-closed order.
        assert db.get(BatchRun, ids[Engine.MANDATE_RECOVERY]) is None
        assert db.get(BatchRun, ids[Engine.RECEIVABLES]) is None
    finally:
        db.close()
