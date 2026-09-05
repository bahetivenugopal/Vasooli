"""The batch runner end to end, and the audit completeness it has to guarantee.

The claim being defended: **every action in a run has an audit entry with a rule
behind it.** Asserted programmatically over a whole batch rather than spot-checked,
because "we always log it" is a promise and this is the thing that makes it a
property.

Also here: the summary's figures come from the trail and nowhere else, and the
same seed produces the same numbers twice.
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
from app.engines.root_cause.runner import RootCauseRunner  # noqa: E402
from app.models.audit import AuditEntry  # noqa: E402
from app.models.enums import (  # noqa: E402
    Action,
    Engine,
    Outcome,
    ProvenanceSource,
)
from app.models.root_cause import CorridorDetection, CorridorReroute  # noqa: E402
from app.services.audit_trail import AuditTrail  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402
from app.services.policy_engine import RULE_REGISTRY  # noqa: E402

SAMPLE = REPO_ROOT / "data" / "samples" / "payments" / "payments.jsonl"
SEED = 42

#: Citations the engine writes that do not come from the policy registry: the
#: decline taxonomy's own rule ids, and the logged-API-call marker. Both are real
#: named rules, they just live outside `RULE_REGISTRY`.
_NON_POLICY_CITATION_PREFIXES = ("decline-taxonomy:", "razorpay-api:")


def _fresh_db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _deterministic_agent() -> LLMAgent:
    """No network, ever. The whole suite runs with the provider switched off."""
    return LLMAgent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))


@pytest.fixture(scope="module")
def run():
    db = _fresh_db()
    artefacts = RootCauseRunner(db, agent=_deterministic_agent()).run(
        batch_id="rc-runner-1", seed=SEED, dataset=SAMPLE
    )
    yield db, artefacts
    db.close()


# ---------------------------------------------------------------------------
# Audit completeness — asserted over the whole batch, not sampled
# ---------------------------------------------------------------------------


def test_every_entry_in_the_run_cites_a_rule_that_exists(run):
    db, artefacts = run
    entries = list(db.query(AuditEntry).filter_by(batch_id=artefacts.summary.batch_id))
    assert entries, "a run that wrote nothing proves nothing"

    for entry in entries:
        assert entry.authorising_rule, f"entry {entry.id} cites no rule"
        assert ":" in entry.authorising_rule, f"entry {entry.id} citation is malformed"
        assert entry.reason_code, f"entry {entry.id} has no reason code"
        assert entry.rationale.strip(), f"entry {entry.id} has no rationale"
        if not entry.authorising_rule.startswith(_NON_POLICY_CITATION_PREFIXES):
            assert entry.authorising_rule in RULE_REGISTRY, (
                f"entry {entry.id} cites {entry.authorising_rule!r}, which is not a "
                "registered rule — a citation pointing at nothing is worse than no "
                "citation, because it looks like rigour"
            )


def test_every_entry_carries_consistent_provenance(run):
    db, artefacts = run
    entries = list(db.query(AuditEntry).filter_by(batch_id=artefacts.summary.batch_id))
    for entry in entries:
        provenance = entry.provenance
        assert provenance["source"] in {s.value for s in ProvenanceSource}
        if provenance["source"] == ProvenanceSource.DETERMINISTIC.value:
            assert provenance["model"] is None
            assert provenance["latency_ms"] is None
            assert provenance["tokens"] is None
            assert provenance["cache_hit"] is False
        else:
            assert provenance["model"]
            assert entry.model_confidence is not None
        if provenance.get("abstained"):
            assert entry.action == Action.ESCALATE.value
            assert entry.outcome == Outcome.ESCALATED.value


def test_every_action_the_run_reports_has_a_matching_entry(run):
    """The counts in the summary and the rows in the trail are the same rows."""
    db, artefacts = run
    entries = list(db.query(AuditEntry).filter_by(batch_id=artefacts.summary.batch_id))
    assert artefacts.summary.action_counts == _counts(e.action for e in entries)
    assert artefacts.summary.outcome_counts == _counts(e.outcome for e in entries)
    assert artefacts.summary.amount_at_risk_paise == sum(
        e.amount_at_risk_paise for e in entries
    )
    assert artefacts.summary.amount_recovered_paise == sum(
        e.amount_recovered_paise for e in entries
    )


def test_the_at_risk_total_is_the_failed_payments_and_nothing_double_counted(run):
    """One entry carries each failed payment's money. Corridors and API calls carry none.

    Without this, a corridor's value at risk is counted twice — once on the
    corridor entry and again on the payments it covers — and the recovery rate is
    quietly divided by an inflated denominator.
    """
    from app.engines.root_cause.dataset import load_attempts

    db, artefacts = run
    failures = [a for a in load_attempts(SAMPLE) if a.failed]
    assert artefacts.summary.amount_at_risk_paise == sum(a.amount_paise for a in failures)
    assert artefacts.summary.failed_attempts == len(failures)


def test_a_run_produces_refusals_as_well_as_actions(run):
    """A batch with no blocks means the gate never fired — worth investigating, not celebrating."""
    _, artefacts = run
    summary = artefacts.summary
    assert summary.policy_denials > 0
    assert summary.denials_by_rule
    assert summary.human_escalations > 0
    assert summary.retries_suppressed > 0
    assert summary.provenance["policy_violations"] == 0


def test_at_least_one_denial_overruled_a_model_recommendation(run):
    """The number worth stating out loud. Non-zero is the feature."""
    db, artefacts = run
    overruled = [
        e
        for e in db.query(AuditEntry).filter_by(batch_id=artefacts.summary.batch_id)
        if (e.entry_metadata or {}).get("overridden_recommendation")
    ]
    assert overruled, "no recommendation was ever overruled — the gate never fired"
    for entry in overruled:
        assert entry.outcome in {
            Outcome.BLOCKED.value,
            Outcome.ESCALATED.value,
            Outcome.HALTED.value,
        }
        assert entry.entry_metadata["authority_rule"]


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


def test_the_run_detects_diagnoses_and_acts(run):
    _, artefacts = run
    summary = artefacts.summary

    assert summary.attempts_ingested == 589
    assert summary.detections == len(artefacts.detections) > 0
    assert sum(summary.diagnoses_by_determination.values()) == summary.detections
    assert summary.reroutes_authorised == len(artefacts.reroutes)
    assert summary.detection_score is not None
    assert summary.detection_score.recall > 0
    assert summary.dataset_batch_id.startswith("pay-s42-")
    assert summary.dataset_batch_id != summary.batch_id, (
        "a dataset id and a run id are different things and must not be conflated"
    )


def test_the_run_clock_comes_from_the_data_not_the_wall(run):
    """Phase 2's batches are anchored to a fixed `as_of` that is not today."""
    from app.engines.root_cause.dataset import load_attempts

    _, artefacts = run
    assert artefacts.summary.now == load_attempts(SAMPLE)[-1].created_at


def test_detections_and_reroutes_are_persisted_with_their_reasoning(run):
    db, artefacts = run
    rows = list(db.query(CorridorDetection).filter_by(batch_id=artefacts.summary.batch_id))
    assert len(rows) == artefacts.summary.detections
    for row in rows:
        assert row.determination
        assert row.diagnosis_reasoning, "a diagnosis with no stated reasoning is unusable"
        assert row.authorising_rule
        assert row.provenance["source"] in {s.value for s in ProvenanceSource}

    reroutes = list(db.query(CorridorReroute).filter_by(batch_id=artefacts.summary.batch_id))
    for reroute in reroutes:
        assert reroute.expires_at > reroute.effective_from
        assert reroute.to_route_id != reroute.from_route_id


def test_the_run_is_reproducible_from_the_same_seed():
    """Same seed, same figures. A number without this is unverifiable."""
    first = RootCauseRunner(_fresh_db(), agent=_deterministic_agent()).run(
        batch_id="rc-repro-a", seed=SEED, dataset=SAMPLE
    )
    second = RootCauseRunner(_fresh_db(), agent=_deterministic_agent()).run(
        batch_id="rc-repro-b", seed=SEED, dataset=SAMPLE
    )

    assert [d.detection_id for d in first.detections] == [
        d.detection_id for d in second.detections
    ]
    for field in (
        "amount_at_risk_paise",
        "amount_recovered_paise",
        "recovery_rate",
        "retries_suppressed",
        "reroutes_authorised",
        "policy_denials",
        "human_escalations",
    ):
        assert getattr(first.summary, field) == getattr(second.summary, field), field
    assert first.summary.detection_score == second.summary.detection_score


def test_a_run_with_no_provider_at_all_still_completes():
    """The project must complete a full batch with no API key. That is a feature."""
    artefacts = RootCauseRunner(_fresh_db(), agent=_deterministic_agent()).run(
        batch_id="rc-no-key", seed=SEED, dataset=SAMPLE
    )
    assert artefacts.summary.llm_fallbacks == artefacts.summary.detections
    assert artefacts.summary.provenance["model_entries"] == 0
    assert artefacts.summary.detections > 0


def test_the_run_summary_and_the_trails_own_summary_agree(run):
    db, artefacts = run
    trail_summary = AuditTrail(db).batch_summary(artefacts.summary.batch_id)
    assert trail_summary.amount_recovered_paise == artefacts.summary.amount_recovered_paise
    assert trail_summary.recovery_rate == artefacts.summary.recovery_rate
    assert trail_summary.engine_counts == {Engine.ROOT_CAUSE.value: trail_summary.entries}


def _counts(values) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(values))
