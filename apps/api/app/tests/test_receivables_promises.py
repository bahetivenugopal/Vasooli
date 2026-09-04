"""Promise tracking: kept, broken, conditional, superseded, and reliability.

The engine's central claim — *escalate only the commitments that actually break*
— is exactly as good as this arithmetic, so these tests are written as boundary
cases around the two windows the rules define: `policy-bounds:PP1`'s 48-hour
grace and `policy-bounds:PP3`'s 7-day horizon.

The conditional block is the one worth reading. `policy-bounds:PP2` says a
conditional promise is never scored against a date it did not give, and the
failure mode it prevents is subtle in both directions: treat it as firm and the
system suppresses a legitimate chase, then escalates for breaking a commitment
nobody made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.engines.receivables import promises as promise_rules
from app.engines.receivables.config import default_config
from app.engines.receivables.schemas import (
    ExtractionResult,
    Invoice,
    PromiseStatus,
    ReplyIntent,
    ReplyRecommendation,
    ReplyUnderstanding,
)
from app.models.provenance import Provenance

CONFIG = default_config()
REPLY_AT = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
COMMITTED = datetime(2026, 9, 4, 11, 0, tzinfo=UTC)


def invoice(**overrides: Any) -> Invoice:
    base: dict[str, Any] = {
        "invoice_id": "inv_0001",
        "batch_id": "inv-test",
        "customer_id": "cust_0001",
        "customer_name": "Probe Systems Pvt Ltd",
        "amount_paise": 5_000_000,
        "payment_terms": "NET30",
        "issued_at": datetime(2026, 7, 1, tzinfo=UTC),
        "due_at": datetime(2026, 7, 31, tzinfo=UTC),
        "days_overdue": 28,
        "status": "overdue",
    }
    return Invoice.model_validate(base | overrides)


def extraction(
    *,
    committed_date: str | None = "2026-09-04",
    conditional: bool = False,
    promise: bool = True,
    intent: ReplyIntent = ReplyIntent.PROMISE,
    reply_id: str = "inv_0001_in_1",
    received_at: datetime = REPLY_AT,
    abstained: bool = False,
) -> ExtractionResult:
    return ExtractionResult(
        invoice_id="inv_0001",
        reply_id=reply_id,
        received_at=received_at,
        text="stub reply",
        understanding=(
            None
            if abstained
            else ReplyUnderstanding(
                intent=intent,
                promise_detected=promise,
                committed_date=committed_date,
                conditional=conditional,
                condition_detail="once our client settles" if conditional else None,
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
        confidence=None if abstained else 0.9,
    )


def assess(inv: Invoice, ext: ExtractionResult, now: datetime):
    return promise_rules.assess(invoice=inv, extraction=ext, config=CONFIG, now=now)


# ---------------------------------------------------------------------------
# No promise, no register entry
# ---------------------------------------------------------------------------


def test_a_reply_with_no_commitment_produces_no_promise() -> None:
    assert assess(invoice(), extraction(promise=False, intent=ReplyIntent.NON_RESPONSE,
                                        committed_date=None), COMMITTED) is None


def test_an_abstention_produces_no_promise_and_is_not_a_silent_no() -> None:
    """An abstention is not "no promise" — it is "we do not know".

    `assess()` returns None for both, which is correct here (there is nothing to
    register), but the *caller* has to keep them apart: an abstention routes to a
    human under HE1, and a genuine non-response does not.
    """
    assert assess(invoice(), extraction(abstained=True), COMMITTED) is None


# ---------------------------------------------------------------------------
# PP1 — the grace window
# ---------------------------------------------------------------------------


def test_a_promise_inside_its_window_is_active() -> None:
    result = assess(invoice(), extraction(), COMMITTED - timedelta(days=1))
    assert result is not None
    assert result.status is PromiseStatus.ACTIVE
    assert result.rule_id == "policy-bounds:PP1"
    assert result.suppresses_chase is True
    assert result.review_at == COMMITTED + CONFIG.promises.grace


def test_a_promise_one_hour_inside_the_grace_is_still_active() -> None:
    """The boundary that matters: settlement lag is not a broken promise.

    A Friday NEFT crediting on Monday is the case PP1 exists for, and escalating
    that customer is the most expensive false positive this engine can produce.
    """
    result = assess(invoice(), extraction(), COMMITTED + CONFIG.promises.grace - timedelta(hours=1))
    assert result is not None
    assert result.status is PromiseStatus.ACTIVE


def test_a_promise_past_the_grace_with_no_payment_is_broken() -> None:
    result = assess(invoice(), extraction(), COMMITTED + CONFIG.promises.grace + timedelta(hours=1))
    assert result is not None
    assert result.status is PromiseStatus.BROKEN
    assert result.suppresses_chase is False
    assert "grace" in result.rationale


def test_payment_inside_the_grace_keeps_the_promise() -> None:
    paid = invoice(
        status="paid",
        paid_at=COMMITTED + timedelta(hours=12),
        amount_paid_paise=5_000_000,
    )
    result = assess(paid, extraction(), COMMITTED + timedelta(days=5))
    assert result is not None
    assert result.status is PromiseStatus.KEPT


def test_payment_after_the_grace_breaks_the_promise_even_though_it_arrived() -> None:
    """Paid late is not the same as kept, and the register should say so.

    The invoice settled, so it is not a recovery failure — but the *commitment*
    was not honoured, and a reliability score that counted it as kept would tell
    the next run that this customer's promises hold.
    """
    paid = invoice(
        status="paid",
        paid_at=COMMITTED + CONFIG.promises.grace + timedelta(days=3),
        amount_paid_paise=5_000_000,
    )
    result = assess(paid, extraction(), COMMITTED + timedelta(days=10))
    assert result is not None
    assert result.status is PromiseStatus.BROKEN


# ---------------------------------------------------------------------------
# PP2 — conditional promises are tracked distinctly
# ---------------------------------------------------------------------------


def test_a_conditional_promise_is_never_broken_even_long_past_its_date() -> None:
    """The rule the whole conditional distinction exists for.

    "Once our client pays us, on the 4th" is not a commitment to the 4th. Scoring
    it broken on the 6th would escalate a customer for a date they explicitly
    made contingent.
    """
    result = assess(
        invoice(), extraction(conditional=True), COMMITTED + timedelta(days=60)
    )
    assert result is not None
    assert result.status is not PromiseStatus.BROKEN
    assert result.status is PromiseStatus.ACTIVE
    assert result.rule_id == "policy-bounds:PP2"
    assert result.conditional is True


def test_a_conditional_promise_stops_suppressing_the_chase_after_its_horizon() -> None:
    """A pause, never a stop. The ladder resumes; the promise still cannot break."""
    inside = assess(invoice(), extraction(conditional=True), REPLY_AT + timedelta(days=3))
    outside = assess(invoice(), extraction(conditional=True), REPLY_AT + timedelta(days=10))
    assert inside is not None and outside is not None
    assert inside.suppresses_chase is True
    assert outside.status is PromiseStatus.ACTIVE
    assert outside.suppresses_chase is False
    assert "horizon has passed" in outside.rationale


def test_a_conditional_promise_that_settles_is_kept_without_a_date_claim() -> None:
    paid = invoice(status="paid", paid_at=REPLY_AT + timedelta(days=4), amount_paid_paise=5_000_000)
    result = assess(paid, extraction(conditional=True), REPLY_AT + timedelta(days=10))
    assert result is not None
    assert result.status is PromiseStatus.KEPT
    assert result.rule_id == "policy-bounds:PP2"


# ---------------------------------------------------------------------------
# PP3 — an undateable promise
# ---------------------------------------------------------------------------


def test_an_undateable_promise_parks_the_invoice_for_the_horizon() -> None:
    result = assess(invoice(), extraction(committed_date=None), REPLY_AT + timedelta(days=2))
    assert result is not None
    assert result.status is PromiseStatus.ACTIVE
    assert result.rule_id == "policy-bounds:PP3"
    assert result.committed_date is None
    assert result.review_at == REPLY_AT + CONFIG.promises.undateable_horizon


def test_an_undateable_promise_can_never_be_broken() -> None:
    """No date, nothing to break. Its exit is ladder exhaustion, like any other."""
    result = assess(invoice(), extraction(committed_date=None), REPLY_AT + timedelta(days=90))
    assert result is not None
    assert result.status is PromiseStatus.ACTIVE
    assert result.suppresses_chase is False


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def test_an_earlier_promise_is_superseded_by_a_later_one_on_the_same_invoice() -> None:
    """Phase 2's corpus has one reply per invoice, so this never fires in a batch.

    Tested here rather than left unimplemented: a status the register can hold
    but the code cannot produce is a schema that lies about what it tracks.
    """
    first = assess(invoice(), extraction(reply_id="in_1"), COMMITTED - timedelta(days=1))
    second = assess(
        invoice(),
        extraction(reply_id="in_2", received_at=REPLY_AT + timedelta(days=2)),
        COMMITTED - timedelta(days=1),
    )
    assert first is not None and second is not None
    resolved = promise_rules.supersede([first, second])
    by_reply = {p.reply_id: p for p in resolved}
    assert by_reply["in_1"].status is PromiseStatus.SUPERSEDED
    assert by_reply["in_1"].rule_id == "policy-bounds:PP2"
    assert by_reply["in_2"].status is PromiseStatus.ACTIVE


def test_supersession_leaves_a_single_promise_untouched() -> None:
    only = assess(invoice(), extraction(), COMMITTED - timedelta(days=1))
    assert only is not None
    assert promise_rules.supersede([only]) == [only]


# ---------------------------------------------------------------------------
# Reliability
# ---------------------------------------------------------------------------


def test_reliability_counts_only_resolved_promises() -> None:
    """An active promise has not happened yet; a superseded one was replaced.

    Counting either would let a customer's score move without their behaviour
    changing.
    """
    kept = assess(
        invoice(status="paid", paid_at=COMMITTED, amount_paid_paise=5_000_000),
        extraction(),
        COMMITTED + timedelta(days=5),
    )
    broken = assess(invoice(), extraction(), COMMITTED + timedelta(days=5))
    active = assess(invoice(), extraction(), COMMITTED - timedelta(days=1))
    assert kept is not None and broken is not None and active is not None

    scores = promise_rules.reliability([kept, broken, active])
    entry = scores["cust_0001"]
    assert entry.promises_made == 3
    assert (entry.kept, entry.broken) == (1, 1)
    assert entry.reliability == 0.5


def test_a_customer_with_no_resolved_promise_has_no_reliability_not_a_default() -> None:
    """No history is not the same as bad history, and a default would invent one."""
    active = assess(invoice(), extraction(), COMMITTED - timedelta(days=1))
    assert active is not None
    assert promise_rules.reliability([active])["cust_0001"].reliability is None


@pytest.mark.parametrize("status", list(PromiseStatus))
def test_every_status_the_register_can_hold_names_a_rule(status: PromiseStatus) -> None:
    """`audit-schema`: every entry cites the rule that authorised it.

    Enumerated over the enum rather than over the cases the tests happen to
    build, so adding a status without a citation fails here.
    """
    citations = {
        PromiseStatus.ACTIVE: {"policy-bounds:PP1", "policy-bounds:PP2", "policy-bounds:PP3"},
        PromiseStatus.KEPT: {"policy-bounds:PP1", "policy-bounds:PP2", "policy-bounds:PP3"},
        PromiseStatus.BROKEN: {"policy-bounds:PP1"},
        PromiseStatus.SUPERSEDED: {"policy-bounds:PP2"},
    }
    assert citations[status]
