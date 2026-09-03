"""Audit API tests — the routes answering against a seeded entry.

These cover the wiring, not the logic: the services are tested directly
elsewhere. What matters here is that a reader hitting the API sees the same
citation, reasoning and provenance the trail stored.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail

BATCH = "batch_api_001"


@pytest.fixture
def client(db: Session, trail: AuditTrail) -> Iterator[TestClient]:
    """A client bound to the test's own in-memory database.

    The `with` block runs the app's lifespan, which is the point: it exercises
    the startup fallback check on every API test, so a reasoning task shipped
    without one fails the suite rather than the demo. Queries still go to the
    in-memory session through the dependency override.
    """
    trail.start_batch(batch_id=BATCH, engine=Engine.ROOT_CAUSE, seed=42)
    trail.record(
        batch_id=BATCH,
        engine=Engine.ROOT_CAUSE,
        entity_type=EntityType.PAYMENT,
        entity_id="pay_api_1",
        action=Action.BLOCK_ATTEMPT,
        outcome=Outcome.BLOCKED,
        reason_code="ATTEMPT_BUDGET_EXHAUSTED",
        authorising_rule="policy-bounds:AC1",
        rationale="4 of 4 permitted attempts used; the hard ceiling refuses a fifth.",
        provenance=Provenance.deterministic(),
        amount_at_risk_paise=250_000,
    )

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_entries_list_returns_the_seeded_entry(client: TestClient):
    response = client.get("/api/v1/audit/entries", params={"batch_id": BATCH})
    assert response.status_code == 200
    (entry,) = response.json()
    assert entry["authorising_rule"] == "policy-bounds:AC1"
    assert entry["provenance"]["source"] == "deterministic"


def test_a_single_entry_carries_its_full_reasoning(client: TestClient):
    entry_id = client.get("/api/v1/audit/entries").json()[0]["id"]
    entry = client.get(f"/api/v1/audit/entries/{entry_id}").json()
    assert "hard ceiling refuses a fifth" in entry["rationale"]
    assert entry["outcome"] == "blocked"


def test_a_missing_entry_is_a_404(client: TestClient):
    assert client.get("/api/v1/audit/entries/999999").status_code == 404


def test_entries_filter_by_action(client: TestClient):
    assert len(client.get("/api/v1/audit/entries", params={"action": "block_attempt"}).json()) == 1
    assert len(client.get("/api/v1/audit/entries", params={"action": "send_dunning"}).json()) == 0


def test_the_batch_summary_route_reports_the_headline_figures(client: TestClient):
    summary = client.get(f"/api/v1/audit/batches/{BATCH}/summary").json()
    assert summary["amount_at_risk_paise"] == 250_000
    assert summary["recovery_rate"] == 0.0
    assert summary["blocked_count"] == 1
    assert summary["policy_violations"] == 0
    assert "deterministic" in summary["by_source"]


def test_a_missing_batch_is_a_404(client: TestClient):
    assert client.get("/api/v1/audit/batches/nope/summary").status_code == 404


def test_the_batch_list_carries_the_seed(client: TestClient):
    (run,) = client.get("/api/v1/audit/batches").json()
    assert run["seed"] == 42
    assert run["batch_id"] == BATCH


def test_the_entity_timeline_route_responds(client: TestClient):
    timeline = client.get("/api/v1/audit/entities/payment/pay_api_1/timeline").json()
    assert [e["entity_id"] for e in timeline] == ["pay_api_1"]


def test_every_exposed_rule_has_a_description_and_a_source(client: TestClient):
    rules = client.get("/api/v1/audit/rules").json()
    assert rules
    for rule in rules:
        assert rule["description"].strip()
        assert rule["citation"].strip()
        assert ":" in rule["rule_id"]


def test_the_openapi_schema_renders(client: TestClient):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/audit/entries" in schema.json()["paths"]
