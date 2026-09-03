"""Engine 1 API tests — the wiring, not the logic.

The logic is tested directly elsewhere. What matters here is that someone hitting
the API sees the same detections, the same reasoning and the same citations the
run actually wrote, and that a run can be triggered, read back, and inspected
down to the rule that authorised each action.

Runs happen with the provider switched off. No test in this suite touches a
network.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Settings  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402

SAMPLE = str(REPO_ROOT / "data" / "samples" / "payments" / "payments.jsonl")
BATCH = "rc-api-001"


@pytest.fixture
def client(db: Session, monkeypatch) -> Iterator[TestClient]:
    """A client bound to this test's own in-memory database, provider off.

    The `with` block runs the app lifespan, which exercises the startup check
    that every reasoning task — including this engine's — has a fallback.
    """
    from app.engines.root_cause import runner as runner_module

    real_agent = runner_module.LLMAgent

    def _offline_agent(*args, **kwargs):
        return real_agent(
            settings=Settings(gemini_api_key="", llm_deterministic_only=True)
        )

    monkeypatch.setattr(runner_module, "LLMAgent", _offline_agent)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def run(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/root-cause/runs",
        json={"batch_id": BATCH, "seed": 42, "dataset": SAMPLE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_run_returns_the_honest_summary(run):
    assert run["batch_id"] == BATCH
    assert run["seed"] == 42
    assert run["dataset_batch_id"].startswith("pay-s42-")
    assert run["detections"] > 0
    assert run["amount_at_risk_paise"] > 0
    # The numbers the phase brief says to show as prominently as the recoveries.
    assert run["policy_denials"] > 0
    assert run["human_escalations"] > 0
    assert run["detection_score"]["false_positives"] >= 0
    assert run["provenance"]["policy_violations"] == 0


def test_reusing_a_batch_id_is_refused(client, run):
    """Two runs sharing an id would merge into one set of metrics."""
    again = client.post(
        "/api/v1/root-cause/runs",
        json={"batch_id": BATCH, "seed": 42, "dataset": SAMPLE},
    )
    assert again.status_code == 409


def test_runs_are_listed_with_their_seed_and_dataset(client, run):
    listed = client.get("/api/v1/root-cause/runs").json()
    assert [r["batch_id"] for r in listed] == [BATCH]
    assert listed[0]["seed"] == 42
    assert listed[0]["notes"]["dataset_batch_id"].startswith("pay-s42-")
    assert listed[0]["notes"]["razorpay_mode"] == "simulated"

    single = client.get(f"/api/v1/root-cause/runs/{BATCH}").json()
    assert single["status"] == "completed"


def test_detections_carry_their_diagnosis_and_reasoning(client, run):
    detections = client.get(f"/api/v1/root-cause/runs/{BATCH}/detections").json()
    assert len(detections) == run["detections"]
    for detection in detections:
        assert detection["determination"]
        assert detection["diagnosis_reasoning"], "a diagnosis without its reasoning is unusable"
        assert detection["authorising_rule"]
        assert detection["provenance"]["source"] in {"model", "deterministic"}
        assert 0.0 <= detection["confidence"] <= 1.0

    one = client.get(f"/api/v1/root-cause/detections/{detections[0]['detection_id']}")
    assert one.status_code == 200
    assert one.json() == detections[0]


def test_detections_can_be_filtered_by_determination(client, run):
    all_detections = client.get(f"/api/v1/root-cause/runs/{BATCH}/detections").json()
    determination = all_detections[0]["determination"]
    filtered = client.get(
        f"/api/v1/root-cause/runs/{BATCH}/detections",
        params={"determination": determination},
    ).json()
    assert filtered
    assert all(d["determination"] == determination for d in filtered)


def test_actions_are_listed_with_their_authorising_rules(client, run):
    actions = client.get(
        f"/api/v1/root-cause/runs/{BATCH}/actions", params={"limit": 1000}
    ).json()
    assert actions
    assert all(a["authorising_rule"] for a in actions)
    assert all(a["rationale"] for a in actions)
    assert {a["engine"] for a in actions} == {"root_cause"}
    assert any(a["outcome"] in {"blocked", "escalated", "halted"} for a in actions), (
        "a run with no refusals means the gate never fired"
    )


def test_reroutes_expose_their_expiry(client, run):
    reroutes = client.get(f"/api/v1/root-cause/runs/{BATCH}/reroutes").json()
    for reroute in reroutes:
        assert reroute["expires_at"] > reroute["effective_from"]
        assert reroute["expiry_rule"].startswith("corridor-detection:")

    if reroutes:
        lapsed = client.get(
            f"/api/v1/root-cause/runs/{BATCH}/reroutes",
            params={"active_at": reroutes[0]["expires_at"]},
        ).json()
        assert lapsed == [], "an expired reroute must not come back as active"


def test_the_config_is_readable_and_cites_its_rules(client):
    config = client.get("/api/v1/root-cause/config").json()
    assert config["minimum_volume"]["rule"].startswith("corridor-detection:")
    assert any(level["enabled"] for level in config["corridor_levels"])
    assert config["recovery"]["max_reroute_duration_hours"] > 0


def test_unknown_runs_and_detections_are_404(client):
    assert client.get("/api/v1/root-cause/runs/nope").status_code == 404
    assert client.get("/api/v1/root-cause/runs/nope/detections").status_code == 404
    assert client.get("/api/v1/root-cause/detections/nope").status_code == 404
