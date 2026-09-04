"""Failure-path rehearsal — ten ways this system is meant to survive.

Phase 7 §5.4. The competition's bar explicitly rewards showing a failure handled
gracefully, and a failure path nobody has deliberately triggered is a claim, not
a property. So each of the ten paths is caused on purpose here, and each is
asserted on twice:

1. **The run completed.** A batch that dies halfway proves nothing.
2. **The trail explains what happened.** Surviving quietly is worse than
   failing loudly — an audit trail that cannot say a degradation occurred is one
   that reports degraded output as if it were reasoned.

Path 10 (backend down while the dashboard is open) is a browser-side path and
lives in `apps/web` plus `docs/smoke-checklist.md`; what is asserted here is the
API-side half of it — that a half-finished run does not leave the dashboard
reading a batch that will never complete.

Every test runs with no network. The provider is scripted; Razorpay is in
simulated mode throughout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Settings  # noqa: E402
from app.engines.mandate_recovery.dataset import DatasetError  # noqa: E402
from app.engines.mandate_recovery.runner import MandateRunner  # noqa: E402
from app.engines.receivables.runner import ReceivablesRunner  # noqa: E402
from app.engines.root_cause.runner import RootCauseRunner  # noqa: E402
from app.models.audit import AuditEntry  # noqa: E402
from app.models.batch import BatchRun  # noqa: E402
from app.models.enums import (  # noqa: E402
    Action,
    BatchStatus,
    Engine,
    EntityType,
    Outcome,
    ProvenanceSource,
)
from app.services.audit_validation import check_audit  # noqa: E402
from app.services.llm_agent import (  # noqa: E402
    BACKOFF_DELAYS,
    LLMAgent,
    ProviderResponse,
    ProviderUnavailableError,
    RateLimitError,
    ResponseCache,
)
from app.services.razorpay_client import RazorpayClient  # noqa: E402
from app.services.unified_run import (  # noqa: E402
    UnifiedRunError,
    UnifiedRunner,
    batch_ids_for,
)
from app.tests.test_unified_run import fresh_db  # noqa: E402

SEED = 42
PAYMENTS = REPO_ROOT / "data" / "samples" / "payments" / "payments.jsonl"
MANDATES = REPO_ROOT / "data" / "samples" / "mandates" / "mandates.jsonl"
INVOICES = REPO_ROOT / "data" / "samples" / "invoices" / "invoices.jsonl"


# ---------------------------------------------------------------------------
# Scripted providers
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """A provider that behaves as told, then optionally breaks and stays broken."""

    name = "gemini"

    def __init__(
        self,
        *,
        healthy_calls: int = 0,
        then: Exception | str | None = None,
        text: str = "{}",
    ) -> None:
        self.healthy_calls = healthy_calls
        self.then = then
        self.text = text
        self.calls = 0

    def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]):
        self.calls += 1
        if self.calls <= self.healthy_calls:
            return ProviderResponse(text=self.text, latency_ms=12, tokens=64)
        if isinstance(self.then, Exception):
            raise self.then
        return ProviderResponse(text=str(self.then), latency_ms=12, tokens=64)


def live_settings(**overrides) -> Settings:
    """Settings that *believe* a provider exists, so the fault paths are reachable."""
    base: dict[str, Any] = {
        "gemini_api_key": "test-key",
        "gemini_model": "gemini-3.1-flash-lite",
        "llm_deterministic_only": False,
    }
    return Settings(**{**base, **overrides})


def agent_with(provider, tmp_path: Path, sleeps: list[float] | None = None) -> LLMAgent:
    return LLMAgent(
        settings=live_settings(),
        provider=provider,
        cache=ResponseCache(tmp_path / "cache"),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def entries_for(db: Session, batch_ids: list[str]) -> list[AuditEntry]:
    return list(db.query(AuditEntry).filter(AuditEntry.batch_id.in_(batch_ids)))


def degradations(entries: list[AuditEntry]) -> list[AuditEntry]:
    """Entries whose decision fell back, marked as such rather than inferred."""
    return [e for e in entries if (e.entry_metadata or {}).get("degraded")]


# ---------------------------------------------------------------------------
# 1. Gemini API unavailable mid-batch
# ---------------------------------------------------------------------------


def test_provider_dying_mid_batch_does_not_kill_the_run(tmp_path: Path) -> None:
    """Engine 3 reads 43 replies. The provider is healthy for two, then gone.

    The property: the batch completes, and the trail distinguishes the reasoned
    reads from the ruled ones rather than presenting a uniform result.
    """
    provider = ScriptedProvider(
        healthy_calls=2,
        then=ProviderUnavailableError("connection reset by peer"),
        text=json.dumps(
            {
                "intent": "promise",
                "promise_detected": True,
                "committed_date": "2026-09-10",
                "conditional": False,
                "language": "en",
                "recommended_action": "pause_chase",
                "confidence": 0.9,
                "reasoning": "Customer commits to a date.",
            }
        ),
    )
    db = fresh_db()
    try:
        artefacts = ReceivablesRunner(db, agent=agent_with(provider, tmp_path)).run(
            batch_id="fp-provider-down", seed=SEED, dataset=INVOICES
        )
        run = db.get(BatchRun, "fp-provider-down")
        assert run is not None and run.status == BatchStatus.COMPLETED.value
        assert artefacts.summary.invoices_ingested > 0

        entries = entries_for(db, ["fp-provider-down"])
        sources = {e.provenance["source"] for e in entries}
        assert ProvenanceSource.MODEL.value in sources, "the healthy calls are not visible"
        assert ProvenanceSource.DETERMINISTIC.value in sources, "the fault is not visible"

        fell_back = degradations(entries)
        assert fell_back, "nothing recorded the degradation"
        assert any(
            "provider unavailable" in (e.entry_metadata or {}).get("degradation_reason", "")
            for e in fell_back
        )
        assert check_audit(db, ["fp-provider-down"]).passed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. Rate limit exceeded beyond backoff
# ---------------------------------------------------------------------------


def test_a_rate_limit_beyond_backoff_degrades_after_actually_backing_off(
    tmp_path: Path,
) -> None:
    """The free tier is 15 requests/minute and a 43-reply corpus hits it.

    This happened for real on Phase 5's first live run. What must hold is that
    the backoff is attempted at 1s/2s/4s *and then* the task falls back, rather
    than either giving up immediately or retrying forever.
    """
    sleeps: list[float] = []
    provider = ScriptedProvider(then=RateLimitError("429 quota exceeded"))
    db = fresh_db()
    try:
        ReceivablesRunner(db, agent=agent_with(provider, tmp_path, sleeps)).run(
            batch_id="fp-rate-limited", seed=SEED, dataset=INVOICES
        )
        run = db.get(BatchRun, "fp-rate-limited")
        assert run is not None and run.status == BatchStatus.COMPLETED.value

        assert sleeps[: len(BACKOFF_DELAYS)] == list(BACKOFF_DELAYS)
        entries = entries_for(db, ["fp-rate-limited"])
        assert all(
            e.provenance["source"] == ProvenanceSource.DETERMINISTIC.value for e in entries
        )
        assert any(
            "rate-limited beyond backoff"
            in (e.entry_metadata or {}).get("degradation_reason", "")
            for e in degradations(entries)
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. Deterministic-only forced across a full run
# ---------------------------------------------------------------------------


def test_deterministic_only_completes_a_full_unified_run_and_says_so() -> None:
    """The headline property of the whole design: no keys, full run, honest label."""
    db = fresh_db()
    try:
        config = Settings(gemini_api_key="", llm_deterministic_only=True)
        report = UnifiedRunner(db, agent=LLMAgent(settings=config), config=config).run(
            seed=SEED, run_id="fp-deterministic"
        )
        assert report.overview.entries > 0
        assert report.integrity_passed
        assert report.mode.llm_deterministic_only is True
        assert "DETERMINISTIC ONLY" in report.mode.description
        assert report.overview.by_source[ProvenanceSource.MODEL.value].entries == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. Malformed LLM output
# ---------------------------------------------------------------------------


def test_unparseable_output_is_retried_once_then_falls_back(tmp_path: Path) -> None:
    """Models correct their own shape errors more often than not — but not always."""
    provider = ScriptedProvider(then="not json at all, sorry")
    db = fresh_db()
    try:
        ReceivablesRunner(db, agent=agent_with(provider, tmp_path)).run(
            batch_id="fp-malformed", seed=SEED, dataset=INVOICES
        )
        run = db.get(BatchRun, "fp-malformed")
        assert run is not None and run.status == BatchStatus.COMPLETED.value

        entries = entries_for(db, ["fp-malformed"])
        assert any(
            "unparseable output twice"
            in (e.entry_metadata or {}).get("degradation_reason", "")
            for e in degradations(entries)
        )
        # Two calls per reply: the first attempt and the schema-error retry.
        assert provider.calls >= 2
        assert check_audit(db, ["fp-malformed"]).passed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Razorpay test-mode API error
# ---------------------------------------------------------------------------


def test_a_razorpay_error_is_normalized_and_audited_not_raised(db: Session) -> None:
    from app.services.audit_trail import AuditTrail

    trail = AuditTrail(db)
    trail.start_batch(batch_id="fp-rzp", engine=Engine.CORE, seed=SEED)
    client = RazorpayClient(settings=Settings(razorpay_mode="simulated"), audit=trail)

    result = client.create_order(
        amount_paise=250_000,
        receipt="fp-rzp-1",
        notes={"batch_id": "fp-rzp", "simulate_failure": "insufficient_fund"},
    )
    assert result.ok is False
    assert result.failure is not None

    entry = client.record_call(
        result,
        batch_id="fp-rzp",
        engine=Engine.CORE,
        entity_type=EntityType.PAYMENT,
        entity_id="fp-rzp-1",
    )
    assert entry.outcome == Outcome.FAILURE.value
    assert entry.reason_code == result.failure.normalized_code.value
    assert entry.authorising_rule.startswith("decline-taxonomy:")


def test_an_unrecognised_razorpay_reason_fails_closed_through_unknown(
    db: Session,
) -> None:
    """An unknown scenario key must fail, never silently succeed.

    Phase 1 recorded the trap: a simulated failure fires only when the request
    carries `notes.simulate_failure`. An unknown key producing a *success* would
    make a whole batch's recovery rate meaningless without anything looking wrong.
    """
    from app.services.audit_trail import AuditTrail

    trail = AuditTrail(db)
    trail.start_batch(batch_id="fp-rzp-unknown", engine=Engine.CORE, seed=SEED)
    client = RazorpayClient(settings=Settings(razorpay_mode="simulated"), audit=trail)
    result = client.create_order(
        amount_paise=250_000,
        receipt="fp-rzp-2",
        notes={"batch_id": "fp-rzp-unknown", "simulate_failure": "no_such_reason_at_all"},
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.normalized_code.value == "UNKNOWN"


# ---------------------------------------------------------------------------
# 6. An LLM recommendation that policy denies
# ---------------------------------------------------------------------------


def test_a_confident_wrong_recommendation_is_overruled_and_the_refusal_is_recorded(
    tmp_path: Path,
) -> None:
    """The best evidence in the project that the gate is structural.

    On Phase 4's first live run the model recommended `schedule_retry` on a
    *paused* mandate at confidence 1.00. Here that is forced: every draft
    recommends a retry, at maximum confidence, on every mandate — including the
    revoked and the paused ones. The gate must refuse, cite a specific rule, and
    the customer must still get a message rather than silence.
    """
    provider = ScriptedProvider(
        then=json.dumps(
            {
                "message": "Your payment did not go through. We will retry shortly.",
                "channel": "email",
                "call_to_action": "update_payment_method",
                "recommended_action": "schedule_retry",
                "confidence": 1.0,
                "reasoning": "A retry will obviously work.",
            }
        )
    )
    db = fresh_db()
    try:
        MandateRunner(db, agent=agent_with(provider, tmp_path)).run(
            batch_id="fp-overruled", seed=SEED, dataset=MANDATES
        )
        entries = entries_for(db, ["fp-overruled"])
        overruled = [
            e for e in entries if (e.entry_metadata or {}).get("overridden_recommendation")
        ]
        assert overruled, "no recommendation was overruled"
        for entry in overruled:
            assert entry.authorising_rule
            assert ":" in entry.authorising_rule
            # The citation stays specific. Phase 3 deviation #2 exists because
            # every overruled denial once cited the same generic authority rule,
            # and the trail could not answer "which rule stopped this?".
            assert not entry.authorising_rule.endswith("llm_authority.denied")
        assert check_audit(db, ["fp-overruled"]).passed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. A mandate revoked mid-sequence
# ---------------------------------------------------------------------------


def test_a_revoked_mandate_is_halted_and_never_charged_afterwards() -> None:
    """`rbi-mandate-rules:A4` — an absolute hard stop, checked across every path.

    The dataset's trap (Phase 2): revoked and paused mandates still carry a
    scheduled `next_debit_at`. An engine that filters on `status == "active"`
    before checking hard stops looks correct and proves nothing.
    """
    db = fresh_db()
    try:
        config = Settings(gemini_api_key="", llm_deterministic_only=True)
        MandateRunner(db, agent=LLMAgent(settings=config)).run(
            batch_id="fp-revoked", seed=SEED, dataset=MANDATES
        )
        entries = entries_for(db, ["fp-revoked"])
        halts = [e for e in entries if e.action == Action.HALT_SCHEDULE.value]
        assert halts, "no mandate was hard-stopped"
        assert any(e.authorising_rule == "rbi-mandate-rules:A4" for e in halts)

        # Terminal means terminal, asserted by the validator rather than by hand.
        report = check_audit(db, ["fp-revoked"])
        assert not [v for v in report.violations if v.check == "terminal-is-terminal"]
        assert report.passed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 8. A dispute arriving mid-chase
# ---------------------------------------------------------------------------


def test_a_dispute_freezes_the_chase_and_the_freeze_is_on_the_record(
    tmp_path: Path,
) -> None:
    """`policy-bounds:RL4`. A disputed invoice is a conversation, not a debt.

    Forced here by making every reply a dispute — the corpus's 8 real disputes
    are only visible with a provider, and the point of the rehearsal is the
    handling, not the detection rate.
    """
    provider = ScriptedProvider(
        then=json.dumps(
            {
                "intent": "dispute",
                "promise_detected": False,
                "committed_date": None,
                "conditional": False,
                "dispute_detail": "We were billed twice for this order.",
                "language": "en",
                "recommended_action": "escalate_to_human",
                "confidence": 0.95,
                "reasoning": "The customer disputes the invoice itself.",
            }
        )
    )
    db = fresh_db()
    try:
        artefacts = ReceivablesRunner(db, agent=agent_with(provider, tmp_path)).run(
            batch_id="fp-dispute", seed=SEED, dataset=INVOICES
        )
        assert artefacts.summary.disputes_frozen > 0
        entries = entries_for(db, ["fp-dispute"])
        assert any("RL4" in e.authorising_rule for e in entries), (
            "a dispute froze nothing, or froze it without citing the rule"
        )
        # Nothing was sent to a customer who disputed. Invoices with no reply at
        # all are still chased, correctly: a dispute freezes the ladder for the
        # invoice it was raised on, not for the whole book.
        disputed = {e.entity_id for e in entries if "RL4" in e.authorising_rule}
        assert disputed
        sent_to_disputers = [
            e
            for e in entries
            if e.action == Action.SEND_REMINDER.value
            and (e.entry_metadata or {}).get("kind") == "reminder"
            and e.entity_id in disputed
        ]
        assert not sent_to_disputers, "a reminder went out on a disputed invoice"
        assert check_audit(db, ["fp-dispute"]).passed
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 9. Empty or malformed input batch
# ---------------------------------------------------------------------------


def test_an_empty_dataset_is_refused_with_a_message_that_says_what_to_do(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    db = fresh_db()
    try:
        with pytest.raises(DatasetError, match="contains no records"):
            MandateRunner(
                db, agent=LLMAgent(settings=Settings(gemini_api_key=""))
            ).run(batch_id="fp-empty", seed=SEED, dataset=empty)
        assert db.get(BatchRun, "fp-empty") is None, "an empty batch opened a run row"
    finally:
        db.close()


@pytest.mark.parametrize(
    "content",
    [
        pytest.param('{"not": "a mandate"}\n', id="valid-json-wrong-shape"),
        pytest.param("{oh dear\n", id="not-json-at-all"),
    ],
)
def test_a_malformed_record_names_the_file_and_the_line(
    tmp_path: Path, content: str
) -> None:
    """A raw JSONDecodeError three frames deep is not a clean degradation.

    Failing rather than skipping the line is the fail-closed direction: a run
    that silently drops what it could not read reports a rate over a denominator
    nobody chose.
    """
    good = MANDATES.read_text(encoding="utf-8").splitlines()[0]
    broken = tmp_path / "broken.jsonl"
    broken.write_text(f"{good}\n{content}", encoding="utf-8")

    db = fresh_db()
    try:
        with pytest.raises(DatasetError, match=r"broken\.jsonl line 2"):
            MandateRunner(
                db, agent=LLMAgent(settings=Settings(gemini_api_key=""))
            ).run(batch_id="fp-malformed-input", seed=SEED, dataset=broken)
        assert db.get(BatchRun, "fp-malformed-input") is None
    finally:
        db.close()


def test_a_manifest_handed_to_an_engine_is_refused() -> None:
    """Ground truth is for scoring. The guard is at the door, not in a convention."""
    db = fresh_db()
    try:
        with pytest.raises(Exception, match="manifest"):
            RootCauseRunner(
                db, agent=LLMAgent(settings=Settings(gemini_api_key=""))
            ).run(
                batch_id="fp-manifest",
                seed=SEED,
                dataset=PAYMENTS.with_name("payments.manifest.json"),
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 10. Backend down while the dashboard is open — the API-side half
# ---------------------------------------------------------------------------


def test_a_half_finished_unified_run_never_reaches_the_dashboard() -> None:
    """The browser half of this path is in `docs/smoke-checklist.md`.

    What is asserted here is that a crashed run cannot become the dashboard's
    headline: the overview reads **completed** runs only, and a leg that died is
    marked failed rather than left `running`. A front page quoting a partial
    trail is worse than a front page showing an error.
    """
    from app.services.overview import OverviewService

    db = fresh_db()
    try:
        config = Settings(gemini_api_key="", llm_deterministic_only=True)
        runner = UnifiedRunner(db, agent=LLMAgent(settings=config), config=config)
        runner.run(seed=SEED, run_id="fp-good")

        with pytest.raises(UnifiedRunError):
            runner.run(
                seed=SEED,
                run_id="fp-crashed",
                datasets={Engine.RECEIVABLES: REPO_ROOT / "nope.jsonl"},
            )

        overview = OverviewService(db).summary()
        crashed = batch_ids_for("fp-crashed")
        assert crashed[Engine.RECEIVABLES] not in overview.batch_ids.values()
        assert overview.batch_ids[Engine.RECEIVABLES.value] == (
            batch_ids_for("fp-good")[Engine.RECEIVABLES]
        )
        assert overview.engines_missing == []
    finally:
        db.close()
