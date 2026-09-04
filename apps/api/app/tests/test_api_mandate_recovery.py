"""Engine 2 API tests — the wiring, not the logic.

The logic is tested directly elsewhere. What matters here is that someone hitting
the API sees the same decisions, the same citations and the same message text the
run actually wrote, and that the single-mandate timeline — the route the demo
leans on — comes back complete enough to read aloud.

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

SAMPLE = str(REPO_ROOT / "data" / "samples" / "mandates" / "mandates.jsonl")
BATCH = "mr-api-001"
BASE = "/api/v1/mandate-recovery"


@pytest.fixture
def client(db: Session, monkeypatch) -> Iterator[TestClient]:
    """A client bound to this test's own in-memory database, provider off.

    The `with` block runs the app lifespan, which exercises the startup check
    that every reasoning task — including this engine's — has a fallback.
    """
    from app.engines.mandate_recovery import runner as runner_module

    real_agent = runner_module.LLMAgent

    def _offline_agent(*args, **kwargs):
        return real_agent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))

    monkeypatch.setattr(runner_module, "LLMAgent", _offline_agent)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def run(client: TestClient) -> dict:
    response = client.post(
        f"{BASE}/runs", json={"batch_id": BATCH, "seed": 42, "dataset": SAMPLE}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_run_returns_the_honest_summary(run: dict) -> None:
    assert run["mandates_ingested"] == 64
    assert run["compliance_blocked"] > 0
    assert run["retries_suppressed"] > 0
    # Both denominators, and the definition of the flattering one.
    assert run["amount_addressable_paise"] <= run["amount_at_risk_paise"]
    assert run["addressable_definition"]
    assert run["wasted_attempts_baseline"]


def test_a_reused_batch_id_is_refused(client: TestClient, run: dict) -> None:
    """Two runs sharing an id would merge into one set of metrics."""
    response = client.post(
        f"{BASE}/runs", json={"batch_id": BATCH, "seed": 42, "dataset": SAMPLE}
    )
    assert response.status_code == 409


def test_runs_are_listed_with_their_seed(client: TestClient, run: dict) -> None:
    rows = client.get(f"{BASE}/runs").json()
    assert [r["batch_id"] for r in rows] == [BATCH]
    assert rows[0]["seed"] == 42
    assert rows[0]["notes"]["dataset_batch_id"].startswith("mnd-")


def test_mandates_can_be_filtered_by_route_and_afa_side(client: TestClient, run: dict) -> None:
    everything = client.get(f"{BASE}/runs/{BATCH}/mandates").json()
    assert len(everything) == 64

    above = client.get(f"{BASE}/runs/{BATCH}/mandates", params={"afa_side": "above"}).json()
    assert above
    assert {m["afa_side"] for m in above} == {"above"}

    blocked = client.get(
        f"{BASE}/runs/{BATCH}/mandates", params={"compliance_blocked": True}
    ).json()
    assert blocked
    assert all(m["compliance_blocked"] for m in blocked)


def test_every_listed_mandate_states_its_next_step_and_the_rule(run: dict, client) -> None:
    rows = client.get(f"{BASE}/runs/{BATCH}/mandates").json()
    for row in rows:
        assert row["next_step"]
        assert ":" in row["authorising_rule"]
        assert row["explanation"].strip()


def test_the_single_mandate_timeline_is_complete(client: TestClient, run: dict) -> None:
    """The route the demo leans on. One call, the whole story."""
    rows = client.get(f"{BASE}/runs/{BATCH}/mandates").json()
    subject = next(r for r in rows if r["failure_route"] == "authentication")

    timeline = client.get(
        f"{BASE}/runs/{BATCH}/mandates/{subject['mandate_id']}"
    ).json()
    assert timeline["mandate_id"] == subject["mandate_id"]
    assert timeline["state"]["explanation"]
    assert timeline["entries"], "a decision with no entries is unprovable"
    assert timeline["next_step_summary"]

    for entry in timeline["entries"]:
        assert entry["reason_code"]
        assert ":" in entry["authorising_rule"]
        assert entry["rationale"]
        assert entry["provenance"]["source"] in {"model", "deterministic"}

    # Timestamps are ordered, so the story reads forwards.
    stamps = [e["timestamp"] for e in timeline["entries"]]
    assert stamps == sorted(stamps)

    # The message text is there in full, with what stopped it if anything did.
    assert timeline["communications"]
    message = timeline["communications"][0]
    assert message["body"]
    assert message["call_to_action"]
    assert message["status"] in {"marked_for_delivery", "held", "suppressed"}


def test_an_unknown_mandate_is_a_404(client: TestClient, run: dict) -> None:
    assert client.get(f"{BASE}/runs/{BATCH}/mandates/mnd_9999").status_code == 404


def test_an_unknown_run_is_a_404(client: TestClient) -> None:
    assert client.get(f"{BASE}/runs/nope/mandates").status_code == 404


def test_communications_are_listed_and_none_claim_to_be_sent(
    client: TestClient, run: dict
) -> None:
    rows = client.get(f"{BASE}/runs/{BATCH}/communications").json()
    assert rows
    assert "sent" not in {r["status"] for r in rows}
    for row in rows:
        assert row["subject"] and row["body"]
        assert ":" in row["authorising_rule"]
        assert row["provenance"]["source"] in {"model", "deterministic"}


def test_communications_can_be_filtered_by_status(client: TestClient, run: dict) -> None:
    notices = client.get(
        f"{BASE}/runs/{BATCH}/communications", params={"kind": "pre_debit_notice"}
    ).json()
    assert notices
    assert {r["kind"] for r in notices} == {"pre_debit_notice"}


def test_every_action_is_readable_with_its_rule(client: TestClient, run: dict) -> None:
    rows = client.get(f"{BASE}/runs/{BATCH}/actions", params={"limit": 500}).json()
    assert rows
    assert all(":" in r["authorising_rule"] for r in rows)
    assert {r["engine"] for r in rows} == {"mandate_recovery"}


def test_the_config_route_says_which_numbers_are_ours(client: TestClient) -> None:
    config = client.get(f"{BASE}/config").json()
    assert config["notice_window"]["regulatory"] is True
    assert config["attempts"]["regulatory"] is False
    assert config["notice_window"]["rule"] == "rbi-mandate-rules:A2"


def test_the_audit_routes_see_the_same_run(client: TestClient, run: dict) -> None:
    summary = client.get(f"/api/v1/audit/batches/{BATCH}/summary").json()
    assert summary["amount_at_risk_paise"] == run["amount_at_risk_paise"]
    assert summary["policy_violations"] == 0
    assert summary["blocked_count"] > 0
