"""The Engine 2 batch runner end to end, and the audit completeness it guarantees.

The claim being defended: **every action in a run has an audit entry with a rule
behind it**, and the summary's figures come from those entries and nowhere else.
Asserted programmatically over the whole batch rather than spot-checked, because
"we always log it" is a promise and this is what makes it a property.

Also here: the phase's acceptance criteria as executable assertions — a
successful soft-decline recovery, a suppressed hard decline, a compliance-blocked
attempt, a mandate at its cap, and a revoked mandate hard-stopped, all present in
one run.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
from app.engines.mandate_recovery.dataset import DatasetError, load_mandates  # noqa: E402
from app.engines.mandate_recovery.runner import MandateRunner  # noqa: E402
from app.engines.mandate_recovery.schemas import FailureRoute, NextStep  # noqa: E402
from app.models.audit import AuditEntry  # noqa: E402
from app.models.enums import Action, Engine, Outcome, ProvenanceSource  # noqa: E402
from app.models.mandate import MandateCommunication, MandateRecoveryState  # noqa: E402
from app.services.audit_trail import AuditTrail  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402
from app.services.policy_engine import RULE_REGISTRY  # noqa: E402

SAMPLE = REPO_ROOT / "data" / "samples" / "mandates" / "mandates.jsonl"
SEED = 42

#: Citations the engine writes that live outside `RULE_REGISTRY`: the decline
#: taxonomy's own ids and the logged-API-call marker. Both are real named rules.
_NON_POLICY_CITATION_PREFIXES = ("decline-taxonomy:", "razorpay-api:")


def _fresh_db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _offline_agent() -> LLMAgent:
    """No network, ever. The whole suite runs with the provider switched off."""
    return LLMAgent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))


def _run(db: Session, *, batch_id: str = "mr-test-1", now: datetime | None = None):
    return MandateRunner(db, agent=_offline_agent()).run(
        batch_id=batch_id, seed=SEED, dataset=SAMPLE, now=now
    )


@pytest.fixture(scope="module")
def artefacts():
    db = _fresh_db()
    try:
        yield _run(db), db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The run completes, and produces what the phase asks it to demonstrate
# ---------------------------------------------------------------------------


def test_the_batch_completes_with_no_api_key_at_all(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.mandates_ingested == 64
    assert run.summary.llm_fallbacks > 0
    assert run.summary.provenance["model_entries"] == 0


def test_a_soft_decline_was_actually_recovered(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.debits_attempted > 0
    assert run.summary.debits_recovered > 0
    assert run.summary.by_failure_class["SOFT"].amount_recovered_paise > 0


def test_a_hard_decline_was_suppressed_rather_than_retried(artefacts) -> None:
    run, _ = artefacts
    hard = run.summary.by_failure_class["HARD"]
    assert hard.retries_suppressed > 0
    assert hard.debits_attempted == 0
    assert run.summary.wasted_attempts_avoided > 0


def test_a_compliance_gate_refused_at_least_one_attempt(artefacts) -> None:
    """Non-zero is the point. A run with none means the gate never fired."""
    run, _ = artefacts
    assert run.summary.compliance_blocked > 0
    assert "rbi-mandate-rules:A2" in run.summary.compliance_blocked_by_rule


def test_a_mandate_hit_its_cap_and_stopped(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.mandates_at_attempt_cap > 0
    capped = [
        o
        for o in run.outcomes
        if o.schedule.decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
    ]
    assert all(not o.debit_attempted for o in capped)


def test_a_revoked_mandate_was_hard_stopped_and_never_touched_again(artefacts) -> None:
    run, db = artefacts
    revoked = [o for o in run.outcomes if o.schedule.mandate.status == "revoked"]
    assert revoked
    for outcome in revoked:
        assert outcome.schedule.explanation.step is NextStep.HALT
        assert not outcome.debit_attempted
        assert outcome.communication is None
        entries = AuditTrail(db).query(entity_id=outcome.schedule.mandate.mandate_id)
        assert all(e.action != Action.ATTEMPT_CHARGE.value for e in entries)
        assert all(e.action != Action.SEND_DUNNING.value for e in entries)


def test_both_afa_branches_are_exercised(artefacts) -> None:
    run, _ = artefacts
    below = run.summary.afa_branch["at_or_below"]
    above = run.summary.afa_branch["above"]
    assert below.mandates > 0 and above.mandates > 0
    assert below.debits_attempted > 0, "below the threshold, auto-debit is permitted"
    assert above.blocked_for_fresh_afa > 0, "above it, fresh AFA is required each cycle"
    assert above.debits_attempted == 0


def test_at_least_one_model_recommendation_was_overruled(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.provenance["overridden_recommendations"] > 0


# ---------------------------------------------------------------------------
# Audit completeness
# ---------------------------------------------------------------------------


def test_every_entry_cites_a_registered_rule(artefacts) -> None:
    _, db = artefacts
    entries = db.query(AuditEntry).all()
    assert entries
    for entry in entries:
        assert entry.reason_code
        assert entry.rationale.strip()
        assert ":" in entry.authorising_rule
        if not entry.authorising_rule.startswith(_NON_POLICY_CITATION_PREFIXES):
            assert entry.authorising_rule in RULE_REGISTRY, entry.authorising_rule


def test_no_entry_bypassed_the_writer(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.provenance["policy_violations"] == 0


def test_refusals_are_present_and_counted(artefacts) -> None:
    """A batch with zero blocks is suspicious, not clean."""
    run, _ = artefacts
    assert run.summary.outcome_counts.get(Outcome.BLOCKED.value, 0) > 0
    assert run.summary.outcome_counts.get(Outcome.HALTED.value, 0) > 0
    assert run.summary.policy_denials > 0


def test_money_is_booked_once_per_mandate(artefacts) -> None:
    """Engine 1's double-counting bug, asserted away rather than remembered."""
    run, db = artefacts
    entries = db.query(AuditEntry).all()
    per_mandate: dict[str, int] = {}
    for entry in entries:
        if entry.amount_at_risk_paise:
            per_mandate[entry.entity_id] = per_mandate.get(entry.entity_id, 0) + 1
    assert per_mandate
    assert all(count == 1 for count in per_mandate.values())

    booked = sum(e.amount_at_risk_paise for e in entries)
    expected = sum(
        o.schedule.mandate.amount_paise
        for o in run.outcomes
        if o.schedule.classification.route is not FailureRoute.NONE
    )
    assert booked == expected


def test_a_healthy_mandate_puts_no_money_in_the_denominator(artefacts) -> None:
    run, db = artefacts
    healthy = [
        o for o in run.outcomes if o.schedule.classification.route is FailureRoute.NONE
    ]
    assert healthy
    for outcome in healthy:
        entries = [
            e
            for e in db.query(AuditEntry).all()
            if e.entity_id == outcome.schedule.mandate.mandate_id
        ]
        assert all(e.amount_at_risk_paise == 0 for e in entries)


def test_the_summary_is_recomputed_from_the_trail_not_a_counter(artefacts) -> None:
    run, db = artefacts
    base = AuditTrail(db).batch_summary(run.summary.batch_id)
    assert base.amount_at_risk_paise == run.summary.amount_at_risk_paise
    assert base.amount_recovered_paise == run.summary.amount_recovered_paise
    assert base.recovery_rate == run.summary.recovery_rate


def test_the_class_breakdown_adds_up_to_the_whole(artefacts) -> None:
    run, _ = artefacts
    assert (
        sum(b.amount_at_risk_paise for b in run.summary.by_failure_class.values())
        == run.summary.amount_at_risk_paise
    )
    assert (
        sum(b.amount_recovered_paise for b in run.summary.by_failure_class.values())
        == run.summary.amount_recovered_paise
    )


def test_the_addressable_denominator_is_a_subset_never_a_replacement(artefacts) -> None:
    run, _ = artefacts
    assert run.summary.amount_addressable_paise <= run.summary.amount_at_risk_paise
    assert run.summary.addressable_recovery_rate >= run.summary.recovery_rate
    assert run.summary.addressable_definition


def test_every_mandate_has_a_state_row_with_an_explanation(artefacts) -> None:
    run, db = artefacts
    rows = db.query(MandateRecoveryState).all()
    assert len(rows) == run.summary.mandates_ingested
    for row in rows:
        assert row.explanation.strip()
        assert ":" in row.authorising_rule


def test_no_communication_claims_to_have_been_sent(artefacts) -> None:
    """Nothing in this repository dispatches. See ADR 0009."""
    _, db = artefacts
    rows = db.query(MandateCommunication).all()
    assert rows
    assert {r.status for r in rows} <= {"marked_for_delivery", "held", "suppressed"}


def test_a_held_message_carries_the_time_it_becomes_permissible() -> None:
    """QH1: blocked and rescheduled, never dropped."""
    db = _fresh_db()
    try:
        # 03:00 IST — every customer message is inside quiet hours.
        night = datetime(2026, 9, 1, 21, 30, tzinfo=UTC)
        _run(db, batch_id="mr-night", now=night)
        held = [
            c
            for c in db.query(MandateCommunication).all()
            if c.status == "held" and c.held_rule == "policy-bounds:QH1"
        ]
        assert held
        for comm in held:
            assert comm.scheduled_for is not None
            assert comm.scheduled_for > night
    finally:
        db.close()


def test_messages_go_out_when_the_clock_is_inside_the_window() -> None:
    """The contrast that makes the quiet-hours test mean something."""
    db = _fresh_db()
    try:
        noon = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)  # 14:00 IST
        run = _run(db, batch_id="mr-noon", now=noon)
        assert run.summary.communications_drafted > 0
        assert run.summary.communications_held == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reproducibility and the ground-truth boundary
# ---------------------------------------------------------------------------


def test_the_same_seed_produces_the_same_numbers_twice() -> None:
    results = []
    for batch_id in ("mr-repro-a", "mr-repro-b"):
        db = _fresh_db()
        try:
            results.append(_run(db, batch_id=batch_id).summary)
        finally:
            db.close()
    first, second = results
    assert first.amount_recovered_paise == second.amount_recovered_paise
    assert first.debits_attempted == second.debits_attempted
    assert first.compliance_blocked == second.compliance_blocked
    assert first.by_failure_class == second.by_failure_class


def test_engine_code_cannot_open_the_manifest() -> None:
    """Cohorts, notice profiles and registration gaps are what this engine derives."""
    manifest = SAMPLE.with_name("mandates.manifest.json")
    with pytest.raises(DatasetError, match="manifest"):
        load_mandates(manifest)


def test_the_run_clock_comes_from_the_data_not_the_wall_clock(artefacts) -> None:
    """The book is anchored to a fixed `as_of` that is not today."""
    run, _ = artefacts
    assert run.summary.now.year == 2026
    assert run.summary.now < datetime.now(UTC)
    latest_attempt = max(
        a.attempted_at for o in run.outcomes for a in o.schedule.mandate.debit_history
    )
    assert run.summary.now == latest_attempt


def test_the_notice_profiles_the_engine_derived_match_the_book() -> None:
    """Derived from timestamps, and cross-checked against the manifest here only.

    This test is evaluation code, so it may read the manifest. The engine may
    not, and `test_engine_code_cannot_open_the_manifest` enforces that.
    """
    import json

    db = _fresh_db()
    try:
        run = _run(db, batch_id="mr-notice-check")
    finally:
        db.close()
    truth = json.loads(SAMPLE.with_name("mandates.manifest.json").read_text(encoding="utf-8"))
    per_mandate = truth["ground_truth"]["per_mandate"]

    checked = 0
    for outcome in run.outcomes:
        expected = per_mandate[outcome.schedule.mandate.mandate_id][
            "next_debit_notice_profile"
        ]
        if expected == "not_yet_due":
            # The generator distinguishes "no notice yet, and none is due" from
            # "no notice, and one was owed". The engine sees only an absent
            # notice, which is the conservative reading and the right one.
            assert outcome.schedule.notice.status.value == "missing"
            continue
        assert outcome.schedule.notice.status.value == expected
        checked += 1
    assert checked > 40


def test_a_debit_is_never_scheduled_before_its_own_notice(artefacts) -> None:
    """The scheduler cannot create the violation it exists to prevent."""
    run, _ = artefacts
    for outcome in run.outcomes:
        schedule = outcome.schedule
        if schedule.proposed_notice_at is None or schedule.proposed_debit_at is None:
            continue
        lead = schedule.proposed_debit_at - schedule.proposed_notice_at
        assert timedelta(hours=24) <= lead <= timedelta(hours=48)


def test_provenance_is_split_by_source_and_never_blended(artefacts) -> None:
    run, _ = artefacts
    by_source = run.summary.provenance["by_source"]
    assert set(by_source) >= {s.value for s in ProvenanceSource}
    assert by_source[ProvenanceSource.MODEL.value]["entries"] == 0
    assert by_source[ProvenanceSource.DETERMINISTIC.value]["entries"] > 0


def test_the_engine_is_tagged_on_every_entry(artefacts) -> None:
    _, db = artefacts
    engines = {e.engine for e in db.query(AuditEntry).all()}
    assert engines == {Engine.MANDATE_RECOVERY.value}


def test_an_inadmissible_recommendation_falls_back_to_the_template_not_to_silence() -> None:
    """The model recommends a retry the gate forbids; the customer still hears.

    The live run produced eight of these — a model at 0.95-1.0 confidence asking
    to retry a paused mandate, or one blocked on authentication. The refusal is
    recorded, and the templated message goes out in place of the draft that was
    written to announce the retry.
    """
    from app.engines.mandate_recovery import recovery as recovery_module
    from app.engines.mandate_recovery.dunning import TASK_DRAFT_DUNNING
    from app.engines.mandate_recovery.schemas import (
        CallToAction,
        Channel,
        DunningDraft,
        DunningRecommendation,
    )
    from app.models.provenance import Provenance
    from app.services.llm_agent import ReasoningResult

    pushy = DunningDraft(
        channel=Channel.EMAIL,
        subject="We will try again shortly",
        body="Hi, we will retry your payment soon. Please keep funds available.",
        call_to_action=CallToAction.UPDATE_PAYMENT_METHOD,
        recommended_action=DunningRecommendation.SCHEDULE_RETRY,
        confidence=0.99,
        reasoning="a retry will clear this",
    )

    class _PushyAgent:
        provider_enabled = True

        def run(self, task: str, context: dict) -> ReasoningResult:
            assert task == TASK_DRAFT_DUNNING
            return ReasoningResult(
                task=task,
                output=pushy,
                confidence=0.99,
                provenance=Provenance.from_model(
                    provider="test", model="test-model", prompt_version="v1"
                ),
            )

    db = _fresh_db()
    try:
        runner = MandateRunner(db, agent=_offline_agent())
        # Only the drafting call is replaced; classification stays deterministic.
        service_agent = _PushyAgent()
        original = recovery_module.MandateRecoveryService.__init__

        def _patched(self, **kwargs):
            original(self, **{**kwargs, "agent": service_agent})

        recovery_module.MandateRecoveryService.__init__ = _patched
        try:
            run = runner.run(batch_id="mr-pushy", seed=SEED, dataset=SAMPLE)
        finally:
            recovery_module.MandateRecoveryService.__init__ = original

        assert run.summary.provenance["overridden_recommendations"] > 0
        substituted = [
            c
            for c in db.query(MandateCommunication).all()
            if c.overridden_recommendation == Action.SCHEDULE_RETRY.value
        ]
        assert substituted
        for comm in substituted:
            assert "we will retry your payment soon" not in comm.body.lower()
            assert comm.status in {"marked_for_delivery", "held"}
            assert comm.provenance["source"] == ProvenanceSource.DETERMINISTIC.value
    finally:
        db.close()
