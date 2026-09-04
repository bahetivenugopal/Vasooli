"""Cross-engine overview tests — the dashboard's headline, and what it must not do.

The overview exists so the browser never computes a metric. That makes two
things worth pinning here rather than trusting:

1. The blended headline is the **sum of the same per-engine figures the trail
   reports**, not a re-derivation that could drift from them.
2. A *permitted* escalation is not counted as a policy denial. Engine 3 records
   an authorised handoff to a human with `outcome: escalated`, and counting the
   outcome alone would report every one of them as the gate refusing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.models.enums import Action, BatchStatus, Engine, EntityType, Outcome
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail
from app.services.overview import OverviewService

RC_BATCH = "ov-rc-1"
MR_BATCH = "ov-mr-1"
RCV_BATCH = "ov-rcv-1"


@pytest.fixture
def seeded(trail: AuditTrail) -> AuditTrail:
    """One completed run per engine, each with a refusal and some money.

    Small on purpose: the arithmetic is what is under test, and three hand-written
    entries make a wrong sum obvious in a way a hundred generated ones do not.
    """
    trail.start_batch(
        batch_id=RC_BATCH,
        engine=Engine.ROOT_CAUSE,
        seed=42,
        notes={"dataset_batch_id": "pay-s42-test"},
    )
    trail.record(
        batch_id=RC_BATCH,
        engine=Engine.ROOT_CAUSE,
        entity_type=EntityType.PAYMENT,
        entity_id="pay_1",
        action=Action.SCHEDULE_RETRY,
        outcome=Outcome.SUCCESS,
        reason_code="RETRY_PERMITTED",
        authorising_rule="decline-taxonomy:SOFT.INSUFFICIENT_FUNDS",
        rationale="Soft decline with budget remaining; the retry settled.",
        provenance=Provenance.deterministic(),
        amount_at_risk_paise=100_000,
        amount_recovered_paise=40_000,
    )
    trail.record(
        batch_id=RC_BATCH,
        engine=Engine.ROOT_CAUSE,
        entity_type=EntityType.PAYMENT,
        entity_id="pay_2",
        action=Action.BLOCK_ATTEMPT,
        outcome=Outcome.BLOCKED,
        reason_code="HARD_DECLINE",
        authorising_rule="decline-taxonomy:HARD.CARD_EXPIRED",
        rationale="Dead instrument; a retry would be a wasted attempt.",
        provenance=Provenance.deterministic(),
        metadata={"suppressed_retry": True},
    )
    trail.complete_batch(RC_BATCH, status=BatchStatus.COMPLETED)

    trail.start_batch(
        batch_id=MR_BATCH,
        engine=Engine.MANDATE_RECOVERY,
        seed=42,
        notes={"dataset_batch_id": "mnd-s42-test"},
    )
    trail.record(
        batch_id=MR_BATCH,
        engine=Engine.MANDATE_RECOVERY,
        entity_type=EntityType.MANDATE,
        entity_id="mnd_1",
        action=Action.BLOCK_ATTEMPT,
        outcome=Outcome.BLOCKED,
        reason_code="PRE_DEBIT_NOTICE_MISSING",
        authorising_rule="rbi-mandate-rules:A2",
        rationale="No pre-debit notice inside the 24h window; the debit is refused.",
        provenance=Provenance.deterministic(),
        amount_at_risk_paise=200_000,
        metadata={"compliance_blocked": True},
    )
    trail.complete_batch(MR_BATCH, status=BatchStatus.COMPLETED)

    trail.start_batch(
        batch_id=RCV_BATCH,
        engine=Engine.RECEIVABLES,
        seed=42,
        notes={"dataset_batch_id": "inv-s42-test"},
    )
    trail.record(
        batch_id=RCV_BATCH,
        engine=Engine.RECEIVABLES,
        entity_type=EntityType.INVOICE,
        entity_id="inv_1",
        action=Action.ESCALATE,
        outcome=Outcome.ESCALATED,
        reason_code="BROKEN_PROMISE",
        authorising_rule="policy-bounds:RL5",
        rationale="Committed date plus grace passed with no payment; a human takes it.",
        provenance=Provenance.deterministic(),
        amount_at_risk_paise=700_000,
        amount_recovered_paise=0,
        metadata={"permitted": True},
    )
    trail.record(
        batch_id=RCV_BATCH,
        engine=Engine.RECEIVABLES,
        entity_type=EntityType.INVOICE,
        entity_id="inv_2",
        action=Action.BLOCK_ATTEMPT,
        outcome=Outcome.BLOCKED,
        reason_code="QUIET_HOURS",
        authorising_rule="policy-bounds:QH1",
        rationale="03:00 IST is outside the outreach window; the reminder is held.",
        provenance=Provenance.deterministic(),
        metadata={"suppressed": True},
    )
    trail.complete_batch(RCV_BATCH, status=BatchStatus.COMPLETED)
    return trail


@pytest.fixture
def client(db: Session, seeded: AuditTrail) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_the_headline_is_the_sum_of_the_engine_contributions(client: TestClient):
    """No client-side arithmetic means the server's own sum has to be right."""
    body = client.get("/api/v1/overview").json()
    assert body["amount_at_risk_paise"] == sum(
        e["amount_at_risk_paise"] for e in body["by_engine"]
    )
    assert body["amount_recovered_paise"] == sum(
        e["amount_recovered_paise"] for e in body["by_engine"]
    )
    assert body["amount_at_risk_paise"] == 1_000_000
    assert body["amount_recovered_paise"] == 40_000
    assert body["recovery_rate"] == 0.04


def test_each_contribution_matches_that_batch_s_own_summary(client: TestClient):
    """The overview and `/audit/batches/{id}/summary` cannot be allowed to differ."""
    body = client.get("/api/v1/overview").json()
    for contribution in body["by_engine"]:
        batch_id = contribution["batch_id"]
        summary = client.get(f"/api/v1/audit/batches/{batch_id}/summary").json()
        assert contribution["amount_at_risk_paise"] == summary["amount_at_risk_paise"]
        assert contribution["amount_recovered_paise"] == summary["amount_recovered_paise"]
        assert contribution["recovery_rate"] == summary["recovery_rate"]
        assert contribution["entries"] == summary["entries"]


def test_all_three_engines_report_and_each_carries_its_own_caveat(client: TestClient):
    body = client.get("/api/v1/overview").json()
    assert body["engines_reporting"] == ["root_cause", "mandate_recovery", "receivables"]
    assert body["engines_missing"] == []
    assert body["blended_caveat"]
    # Each engine measures exposure differently, so no two definitions may be
    # the same string — a shared one would mean a caveat got copied.
    definitions = {e["recovery_definition"] for e in body["by_engine"]}
    assert len(definitions) == 3
    assert all(d.strip() for d in definitions)


def test_a_permitted_escalation_is_not_counted_as_a_policy_denial(client: TestClient):
    """The RL5 escalation is authorised. Only the three refusals are denials."""
    trust = {t["key"]: t for t in client.get("/api/v1/overview").json()["trust"]}
    assert trust["policy_denials"]["value"] == 3
    assert set(trust["policy_denials"]["by_rule"]) == {
        "decline-taxonomy:HARD.CARD_EXPIRED",
        "rbi-mandate-rules:A2",
        "policy-bounds:QH1",
    }
    # It is still an escalation, and still shown as one.
    assert trust["human_escalations"]["value"] == 1


def test_the_trust_strip_separates_what_was_actually_withheld(client: TestClient):
    trust = {t["key"]: t["value"] for t in client.get("/api/v1/overview").json()["trust"]}
    assert trust["compliance_blocked"] == 1
    assert trust["retries_suppressed"] == 1
    assert trust["messages_suppressed"] == 1
    assert trust["deterministic_fallbacks"] == 0
    assert trust["abstentions"] == 0


def test_every_trust_metric_explains_why_non_zero_is_good(client: TestClient):
    """A refusal count with no explanation reads as a defect list."""
    for metric in client.get("/api/v1/overview").json()["trust"]:
        assert metric["label"]
        assert len(metric["meaning"]) > 40


def test_recent_activity_is_newest_first_and_capped(client: TestClient):
    body = client.get("/api/v1/overview", params={"recent_limit": 2}).json()
    activity = body["recent_activity"]
    assert len(activity) == 2
    timestamps = [e["timestamp"] for e in activity]
    assert timestamps == sorted(timestamps, reverse=True)
    assert all(e["authorising_rule"] for e in activity)


def test_recent_activity_shows_every_engine_not_just_the_last_one_run(
    client: TestClient,
):
    """A plain newest-first sort silently becomes a single-engine view.

    The engines do not share a clock — Engine 1 stamps wall-clock time, Engine 2
    each debit's scheduled time, Engine 3 its run clock — so "the newest entries
    across three batches" resolves to "every entry from whichever engine ran
    last". The overview's front page must not be misleading about that.
    """
    body = client.get("/api/v1/overview", params={"recent_limit": 6}).json()
    engines = {entry["engine"] for entry in body["recent_activity"]}
    assert engines == {"root_cause", "mandate_recovery", "receivables"}


def test_an_explicit_batch_id_is_honoured(client: TestClient):
    body = client.get("/api/v1/overview", params={"root_cause": RC_BATCH}).json()
    assert body["batch_ids"]["root_cause"] == RC_BATCH


def test_a_batch_id_from_the_wrong_engine_is_refused_not_silently_ignored(
    client: TestClient,
):
    """Showing a different run than the one asked for is worse than an error."""
    response = client.get("/api/v1/overview", params={"root_cause": RCV_BATCH})
    assert response.status_code == 404
    assert "receivables" in response.json()["detail"]


def test_an_unknown_batch_id_is_a_404(client: TestClient):
    response = client.get("/api/v1/overview", params={"receivables": "no-such-run"})
    assert response.status_code == 404


def test_an_engine_with_no_completed_run_is_named_not_omitted(
    db: Session, trail: AuditTrail
):
    """A headline missing a third of the project should say so."""
    trail.start_batch(batch_id=RC_BATCH, engine=Engine.ROOT_CAUSE, seed=42)
    trail.complete_batch(RC_BATCH, status=BatchStatus.COMPLETED)
    summary = OverviewService(db).summary()
    assert summary.engines_reporting == [Engine.ROOT_CAUSE]
    assert summary.engines_missing == [Engine.MANDATE_RECOVERY, Engine.RECEIVABLES]


def test_a_run_still_in_flight_is_not_reported(db: Session, trail: AuditTrail):
    """A headline that moves mid-batch is one nobody can quote."""
    trail.start_batch(batch_id=RC_BATCH, engine=Engine.ROOT_CAUSE, seed=42)
    assert OverviewService(db).latest_runs() == {}


def test_an_empty_database_gives_an_empty_overview_rather_than_an_error(db: Session):
    """The dashboard's empty state needs a 200, not a stack trace."""
    summary = OverviewService(db).summary()
    assert summary.engines_reporting == []
    assert summary.amount_at_risk_paise == 0
    assert summary.recovery_rate == 0.0
    assert summary.recent_activity == []
    # The trust strip is still fully present, all zeros. A missing strip and a
    # strip of zeros mean different things.
    assert len(summary.trust) == 7


def test_the_repo_root_is_importable_so_post_runs_can_work(client: TestClient):
    """`POST /runs` was broken in every engine, and only outside the test suite.

    All three engines lazily import `data.generators.retry_model` from the repo
    root. Pytest puts the root on `sys.path` for free, so the routes passed their
    tests while failing with a bare `ModuleNotFoundError` under uvicorn — nothing
    called them for real until the dashboard's run trigger did.

    The fix is a bootstrap in the app's lifespan, and the `client` fixture runs
    that lifespan. Asserting the import works here is what stops it regressing:
    the failure is invisible to every other test in this suite.
    """
    import importlib

    module = importlib.import_module("data.generators.retry_model")
    assert hasattr(module, "RetrySuccessModel")
