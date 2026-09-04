"""The full Engine 3 loop, the extraction scorer, and audit completeness.

Runs against the committed sample ledger with the provider forced off, so every
reply abstains. That is a deliberate choice of fixture: the no-key path is the
one the project promises works, and a suite that only passed with credentials
would be testing the model rather than the engine.

The scorer gets its own block, and it is tested **adversarially** — fed
deliberately wrong readings — because a metric that cannot detect a wrong answer
is a metric that always reports success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.receivables.dataset import (
    DatasetError,
    dataset_anchor,
    load_invoices,
    manifest_for,
    resolve_dataset,
)
from app.engines.receivables.runner import ReceivablesRunner
from app.engines.receivables.schemas import (
    ChaseStep,
    ExtractionResult,
    ReplyIntent,
    ReplyRecommendation,
    ReplyUnderstanding,
)
from app.engines.receivables.scoring import (
    GroundTruthError,
    load_manifest,
    score_extractions,
)
from app.models.audit import AuditEntry
from app.models.enums import Action, Engine, Outcome, ProvenanceSource
from app.models.provenance import Provenance
from app.models.receivables import InvoiceChaseState, InvoiceCommunication, PromiseToPay
from app.services.llm_agent import LLMAgent
from app.services.policy_engine import RULE_REGISTRY
from app.services.razorpay_client import RazorpayClient, RazorpayMode

#: Inside the QH1 outreach window (14:30 IST), so a held reminder in these runs
#: is held by a rule and not by the hour the suite happened to run at.
RUN_NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def deterministic_agent(monkeypatch: pytest.MonkeyPatch) -> LLMAgent:
    """An agent with no provider at all — the no-key path, forced."""
    monkeypatch.setenv("LLM_DETERMINISTIC_ONLY", "true")
    from app.core.config import Settings

    return LLMAgent(settings=Settings(llm_deterministic_only=True, gemini_api_key=""))


@pytest.fixture
def artefacts(db: Session, deterministic_agent: LLMAgent):
    runner = ReceivablesRunner(
        db,
        agent=deterministic_agent,
        razorpay=RazorpayClient(
            audit=__import__(
                "app.services.audit_trail", fromlist=["AuditTrail"]
            ).AuditTrail(db),
            mode=RazorpayMode.SIMULATED,
        ),
    )
    return runner.run(batch_id="rcv-test", seed=42, now=RUN_NOW)


# ---------------------------------------------------------------------------
# Dataset discipline
# ---------------------------------------------------------------------------


def test_engine_code_refuses_a_manifest_path() -> None:
    """The answer key is not the exam.

    The invoices manifest holds the promise, date and dispute annotation for
    every reply. An engine that opened it would make this phase's headline number
    a claim rather than a measurement.
    """
    with pytest.raises(DatasetError, match="manifest"):
        resolve_dataset(manifest_for())


def test_the_run_clock_is_derived_from_the_ledger_not_from_the_wall() -> None:
    """Phase 2's ledger is anchored to a fixed `as_of` that is not today."""
    anchor = dataset_anchor(load_invoices())
    assert anchor.year == 2026
    assert anchor.month == 9
    # At or just after the moment the ledger describes, never days past it.
    assert abs((anchor - datetime(2026, 9, 1, 3, 30, tzinfo=UTC)).days) <= 1


def test_the_anchor_never_precedes_a_recorded_event() -> None:
    """A clock behind the data would make ageing arithmetic go negative."""
    invoices = load_invoices()
    anchor = dataset_anchor(invoices)
    latest = max(
        [c.sent_at for i in invoices for c in i.communications]
        + [r.received_at for i in invoices for r in i.replies]
    )
    assert anchor >= latest


# ---------------------------------------------------------------------------
# The loop completes with no credentials at all
# ---------------------------------------------------------------------------


def test_a_full_batch_completes_with_no_api_key(artefacts) -> None:
    """`CLAUDE.md` non-negotiable #5: a feature, not a degraded mode."""
    summary = artefacts.summary
    assert summary.invoices_ingested == 72
    assert summary.invoices_worklisted > 0
    assert summary.llm_fallbacks > 0
    assert summary.abstentions == summary.llm_fallbacks


def test_every_reply_abstains_and_none_becomes_a_promise(artefacts) -> None:
    """The whole point of the abstention fallback, asserted over a real corpus.

    43 replies, every one of them unreadable, and zero promises in the register.
    A single fabricated promise here would suppress a legitimate chase.
    """
    assert artefacts.extractions
    assert all(e.abstained for e in artefacts.extractions)
    assert artefacts.assessments == []
    assert artefacts.summary.promises_made == 0


def test_the_worklist_covers_every_chase_step_exactly_once(artefacts) -> None:
    """The counts have to add up, or an invoice fell through a branch."""
    summary = artefacts.summary
    assert sum(summary.chase_steps.values()) == summary.invoices_worklisted
    assert len(artefacts.outcomes) == summary.invoices_worklisted


def test_settled_and_not_yet_due_invoices_are_not_chased(artefacts, db: Session) -> None:
    invoices = {i.invoice_id: i for i in load_invoices()}
    for state in db.scalars(select(InvoiceChaseState)):
        invoice = invoices[state.invoice_id]
        assert not invoice.is_paid
        assert invoice.days_late(RUN_NOW) > 0


# ---------------------------------------------------------------------------
# Audit completeness
# ---------------------------------------------------------------------------


def test_every_entry_cites_a_registered_rule(artefacts, db: Session) -> None:
    """`audit-schema`'s non-negotiable, checked against the registry itself.

    A citation pointing at nothing is worse than no citation, because it looks
    like rigour. The Razorpay call rule is the one legitimate exception: it is a
    record that a call happened, not a policy decision.
    """
    entries = list(db.scalars(select(AuditEntry).where(AuditEntry.batch_id == "rcv-test")))
    assert entries
    for entry in entries:
        assert entry.reason_code
        assert entry.rationale.strip()
        assert ":" in entry.authorising_rule
        if entry.action != Action.API_CALL.value:
            assert entry.authorising_rule in RULE_REGISTRY, entry.authorising_rule


def test_the_batch_reports_no_policy_violations(artefacts) -> None:
    """Non-zero means a row reached the table without going through the writer."""
    assert artefacts.summary.provenance["policy_violations"] == 0


def test_the_batch_contains_refusals(artefacts) -> None:
    """A run with zero blocks is suspicious rather than clean."""
    summary = artefacts.summary
    assert summary.outcome_counts.get(Outcome.ESCALATED.value, 0) > 0


def test_money_is_booked_once_per_invoice(artefacts, db: Session) -> None:
    """Engine 1 learned this the expensive way; Engine 2 made it checkable.

    The same rupees counted on two entries inflates the denominator silently.
    """
    entries = list(db.scalars(select(AuditEntry).where(AuditEntry.batch_id == "rcv-test")))
    booked: dict[str, int] = {}
    for entry in entries:
        if entry.amount_at_risk_paise:
            booked[entry.entity_id] = booked.get(entry.entity_id, 0) + 1
    assert booked
    assert all(count == 1 for count in booked.values())


def test_an_abstention_is_recorded_as_a_deliberate_escalation(
    artefacts, db: Session
) -> None:
    """`audit-schema`: abstained ⇒ action escalate, outcome escalated."""
    entries = [
        e
        for e in db.scalars(select(AuditEntry).where(AuditEntry.batch_id == "rcv-test"))
        if e.provenance.get("abstained")
    ]
    assert entries
    for entry in entries:
        assert entry.action == Action.ESCALATE.value
        assert entry.outcome == Outcome.ESCALATED.value
        assert entry.provenance["source"] == ProvenanceSource.DETERMINISTIC.value


def test_no_communication_is_ever_marked_sent(artefacts, db: Session) -> None:
    """ADR 0009. Nothing in this repository dispatches."""
    statuses = {c.status for c in db.scalars(select(InvoiceCommunication))}
    assert statuses
    assert "sent" not in statuses
    assert statuses <= {"marked_for_delivery", "held", "suppressed"}


def test_a_payment_link_is_created_and_audited(artefacts, db: Session) -> None:
    entries = [
        e
        for e in db.scalars(select(AuditEntry).where(AuditEntry.batch_id == "rcv-test"))
        if e.action == Action.API_CALL.value
    ]
    assert entries
    for entry in entries:
        assert (entry.entry_metadata or {})["operation"] == "payment_link.create"
        assert (entry.entry_metadata or {})["razorpay_mode"] == RazorpayMode.SIMULATED.value


def test_the_run_is_reproducible_from_the_same_seed(
    db: Session, deterministic_agent: LLMAgent
) -> None:
    """Same seed, same clock, same decisions."""
    first = ReceivablesRunner(db, agent=deterministic_agent).run(
        batch_id="rcv-repeat-a", seed=42, now=RUN_NOW
    )
    second = ReceivablesRunner(db, agent=deterministic_agent).run(
        batch_id="rcv-repeat-b", seed=42, now=RUN_NOW
    )
    assert first.summary.chase_steps == second.summary.chase_steps
    assert first.summary.amount_at_risk_paise == second.summary.amount_at_risk_paise
    assert [o.plan.rule_id for o in first.outcomes] == [
        o.plan.rule_id for o in second.outcomes
    ]


def test_the_run_clock_changes_the_answer(db: Session, deterministic_agent: LLMAgent) -> None:
    """03:00 IST holds every reminder. The rule working, and it looks like a bug.

    Worth pinning: it is the single most confusing thing about demoing this
    engine, and a change that quietly made quiet hours stop applying would
    otherwise look like an improvement.
    """
    quiet = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)  # 03:00 IST
    open_hours = ReceivablesRunner(db, agent=deterministic_agent).run(
        batch_id="rcv-open", seed=42, now=RUN_NOW
    )
    held = ReceivablesRunner(db, agent=deterministic_agent).run(
        batch_id="rcv-quiet", seed=42, now=quiet
    )
    assert open_hours.summary.reminders_sent > 0
    assert held.summary.reminders_sent == 0
    assert held.summary.suppressed_by_rule.get("policy-bounds:QH1", 0) > 0
    assert all(
        o.plan.scheduled_for is None or o.plan.scheduled_for > quiet
        for o in held.outcomes
        if o.plan.step is ChaseStep.DEFER
    )


# ---------------------------------------------------------------------------
# The scorer — tested adversarially
# ---------------------------------------------------------------------------


def manifest() -> dict[str, Any]:
    return load_manifest(manifest_for())


def reading(
    reply_id: str,
    invoice_id: str,
    *,
    intent: ReplyIntent,
    promise: bool,
    committed_date: str | None = None,
    conditional: bool = False,
    abstained: bool = False,
) -> ExtractionResult:
    return ExtractionResult(
        invoice_id=invoice_id,
        reply_id=reply_id,
        received_at=RUN_NOW,
        text="stub",
        understanding=(
            None
            if abstained
            else ReplyUnderstanding(
                intent=intent,
                promise_detected=promise,
                committed_date=committed_date,
                conditional=conditional,
                dispute_detail="x" if intent is ReplyIntent.DISPUTE else None,
                language="en",
                recommended_action=ReplyRecommendation.PAUSE_CHASE,
                confidence=0.9,
                reasoning="stub",
            )
        ),
        abstained=abstained,
        provenance=(
            Provenance.deterministic(abstained=True)
            if abstained
            else Provenance.from_model(provider="stub", model="stub-1", prompt_version="v1")
        ),
    )


def truth_for(category: str) -> tuple[str, dict[str, Any]]:
    """One reply of a given category from the held-out annotations."""
    per_reply = manifest()["ground_truth"]["per_reply"]
    for reply_id, gt in sorted(per_reply.items()):
        if gt["category"] == category:
            return reply_id, gt
    raise AssertionError(f"no {category} in the corpus")


def test_the_scorer_rejects_a_manifest_for_another_dataset() -> None:
    from app.engines.mandate_recovery.dataset import DEFAULT_DATASET as MANDATES

    with pytest.raises(GroundTruthError, match="not invoices"):
        load_manifest(MANDATES.with_name("mandates.manifest.json"))


def test_a_perfect_reading_scores_perfectly() -> None:
    """The control. Without this, the adversarial cases prove nothing."""
    per_reply = manifest()["ground_truth"]["per_reply"]
    readings = [
        reading(
            reply_id,
            gt["invoice_id"],
            intent=(
                ReplyIntent.DISPUTE
                if gt["is_dispute"]
                else ReplyIntent.PROMISE
                if gt["is_promise"]
                else ReplyIntent.NON_RESPONSE
            ),
            promise=gt["is_promise"],
            committed_date=gt["promised_date"],
            conditional=gt["is_conditional"],
        )
        for reply_id, gt in sorted(per_reply.items())
    ]
    score = score_extractions(readings, manifest())
    assert score.promise_detection.precision == 1.0
    assert score.promise_detection.recall == 1.0
    assert score.dispute_detection.f1 == 1.0
    assert score.date_accuracy.exact_rate == 1.0
    assert score.failures == []


def test_a_fabricated_promise_shows_up_as_a_false_positive() -> None:
    """The failure mode the whole abstention design exists to prevent."""
    reply_id, gt = truth_for("dispute")
    score = score_extractions(
        [
            reading(
                reply_id,
                gt["invoice_id"],
                intent=ReplyIntent.PROMISE,
                promise=True,
                committed_date="2026-09-30",
            )
        ],
        manifest(),
    )
    assert score.promise_detection.false_positives == 1
    assert score.promise_detection.precision == 0.0
    assert score.dispute_detection.false_negatives == 1
    assert score.failures
    assert "promise" in score.failures[0]["wrong"]


def test_a_missed_promise_shows_up_as_a_false_negative() -> None:
    reply_id, gt = truth_for("explicit_promise")
    score = score_extractions(
        [reading(reply_id, gt["invoice_id"], intent=ReplyIntent.NON_RESPONSE, promise=False)],
        manifest(),
    )
    assert score.promise_detection.false_negatives == 1
    assert score.promise_detection.recall == 0.0


def test_a_wrong_date_is_not_forgiven() -> None:
    reply_id, gt = truth_for("explicit_promise")
    wrong = (
        datetime.fromisoformat(gt["promised_date"]) + timedelta(days=9)
    ).date().isoformat()
    score = score_extractions(
        [
            reading(
                reply_id,
                gt["invoice_id"],
                intent=ReplyIntent.PROMISE,
                promise=True,
                committed_date=wrong,
            )
        ],
        manifest(),
    )
    assert score.date_accuracy.wrong == 1
    assert score.date_accuracy.exact_rate == 0.0
    assert "date" in score.failures[0]["wrong"]


def test_inventing_a_date_on_an_undateable_promise_is_counted_as_spurious() -> None:
    """"Next week sometime" has no right answer, and pretending otherwise is an error."""
    per_reply = manifest()["ground_truth"]["per_reply"]
    reply_id, gt = next(
        (rid, g)
        for rid, g in sorted(per_reply.items())
        if g["is_promise"] and g["date_confidence"] == "none"
    )
    score = score_extractions(
        [
            reading(
                reply_id,
                gt["invoice_id"],
                intent=ReplyIntent.PROMISE,
                promise=True,
                committed_date="2026-09-15",
            )
        ],
        manifest(),
    )
    assert score.date_accuracy.spurious == 1


def test_a_conditional_promise_read_as_firm_is_an_error() -> None:
    """`policy-bounds:PP2`'s failure mode, made measurable."""
    reply_id, gt = truth_for("conditional_promise")
    score = score_extractions(
        [
            reading(
                reply_id,
                gt["invoice_id"],
                intent=ReplyIntent.PROMISE,
                promise=True,
                conditional=False,
            )
        ],
        manifest(),
    )
    assert score.conditional_detection.false_negatives == 1
    assert "conditional" in score.failures[0]["wrong"]


def test_abstentions_are_never_folded_into_an_accuracy_figure() -> None:
    """The rule that stops the engine improving its score by refusing to answer."""
    reply_id, gt = truth_for("explicit_promise")
    score = score_extractions(
        [reading(reply_id, gt["invoice_id"], intent=ReplyIntent.UNCLEAR, promise=False,
                 abstained=True)],
        manifest(),
    )
    assert score.replies_total == 1
    assert score.replies_scored == 0
    assert score.abstentions == 1
    assert score.abstention_rate == 1.0
    # Nothing was answered, so nothing is claimed — not even a flattering zero
    # denominator dressed up as a perfect score.
    assert score.promise_detection.true_positives == 0
    assert score.promise_detection.precision == 0.0


def test_hinglish_replies_are_scored_as_their_own_cohort() -> None:
    """Half of real Indian B2B collections correspondence looks like this.

    Reported separately so a headline number cannot hide a model that only reads
    English.
    """
    per_reply = manifest()["ground_truth"]["per_reply"]
    hinglish = {rid for rid, g in per_reply.items() if g["language"] == "hinglish"}
    assert len(hinglish) >= 10
    readings = [
        reading(
            rid,
            per_reply[rid]["invoice_id"],
            intent=(
                ReplyIntent.DISPUTE
                if per_reply[rid]["is_dispute"]
                else ReplyIntent.PROMISE
                if per_reply[rid]["is_promise"]
                else ReplyIntent.NON_RESPONSE
            ),
            promise=per_reply[rid]["is_promise"],
            committed_date=per_reply[rid]["promised_date"],
            conditional=per_reply[rid]["is_conditional"],
        )
        for rid in sorted(hinglish)
    ]
    score = score_extractions(readings, manifest())
    assert "hinglish" in score.by_language
    assert score.by_language["hinglish"].accuracy == 1.0


def test_a_reply_the_ground_truth_does_not_know_is_skipped_not_failed() -> None:
    """A corpus mismatch is a dataset problem, not a model error."""
    score = score_extractions(
        [reading("no_such_reply", "inv_9999", intent=ReplyIntent.PROMISE, promise=True)],
        manifest(),
    )
    assert score.replies_total == 0
    assert score.failures == []


# ---------------------------------------------------------------------------
# The promise register, over the loop
# ---------------------------------------------------------------------------


def test_the_promise_register_is_empty_when_every_reply_abstained(
    artefacts, db: Session
) -> None:
    """An abstention never reaches the register. That is the contract."""
    assert list(db.scalars(select(PromiseToPay))) == []


def test_every_chase_state_carries_its_score_breakdown(artefacts, db: Session) -> None:
    """`policy-bounds:PR1`: a ranking a customer could be shown and argued with."""
    states = list(db.scalars(select(InvoiceChaseState)))
    assert states
    for state in states:
        components = state.score_breakdown["components"]
        assert {c["name"] for c in components} == {
            "outstanding_value",
            "days_overdue",
            "customer_unreliability",
            "contact_headroom",
        }
        assert all(c["explanation"] for c in components)
        assert state.score_breakdown["rule"] == "policy-bounds:PR1"


def test_ranks_are_dense_and_ordered(artefacts) -> None:
    ranks = [s.rank for s in artefacts.worklist]
    assert ranks == list(range(1, len(ranks) + 1))
    scores = [s.score for s in artefacts.worklist]
    assert scores == sorted(scores, reverse=True)


def test_engine_and_entity_type_are_right_on_every_entry(artefacts, db: Session) -> None:
    entries = list(db.scalars(select(AuditEntry).where(AuditEntry.batch_id == "rcv-test")))
    assert all(e.engine == Engine.RECEIVABLES.value for e in entries)
    assert all(e.entity_type == "invoice" for e in entries)
