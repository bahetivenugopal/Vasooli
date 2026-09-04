"""Reply understanding: abstention, coherence, Hinglish, and relative dates.

The adversarial block is the important one. `policy-bounds` and the
`llm-provider` fallback contract both rest on a single claim — *this task never
fabricates a promise* — and a claim that is only true because the model happened
to behave is not a claim. These tests pin the paths the engine controls: what
happens when the provider is gone, when the reading contradicts itself, and when
a date cannot be resolved.

No network. Every model response here is a stub, so the suite tests the engine's
handling of a reading rather than the model's ability to produce one. Whether the
live model actually resists the bait is a *measurement*, and it lives in
`docs/metrics/engine-3-receivables.md` where a measurement belongs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.engines.receivables.config import default_config
from app.engines.receivables.schemas import (
    ExtractionResult,
    Invoice,
    Reply,
    ReplyIntent,
    ReplyRecommendation,
    ReplyUnderstanding,
)
from app.engines.receivables.understanding import (
    TASK_UNDERSTAND_REPLY,
    _understand_reply_fallback,
    build_context,
    committed_datetime,
    month_end,
    register_receivables_tasks,
    understand_reply,
)
from app.models.enums import ProvenanceSource
from app.models.provenance import Provenance
from app.services.llm_agent import ReasoningResult

#: A Friday, so "next Friday" has an unambiguous right answer.
REPLY_AT = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_receivables_tasks()


def make_invoice(**overrides: Any) -> Invoice:
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


def make_reply(text: str, *, received_at: datetime = REPLY_AT) -> Reply:
    return Reply(
        reply_id="inv_0001_in_1",
        direction="inbound",
        channel="email",
        received_at=received_at,
        text=text,
    )


class StubAgent:
    """An `LLMAgent` stand-in that returns whatever the test hands it."""

    def __init__(self, output: ReplyUnderstanding | None, *, abstain: bool = False) -> None:
        self._output = output
        self._abstain = abstain
        self.calls: list[dict[str, Any]] = []

    provider_enabled = True

    def run(self, task: str, context: dict[str, Any]) -> ReasoningResult:
        self.calls.append(context)
        if self._abstain:
            return ReasoningResult(
                task=task,
                output=None,
                provenance=Provenance.deterministic(abstained=True),
                degraded=True,
                degradation_reason="stubbed provider outage",
            )
        return ReasoningResult(
            task=task,
            output=self._output,
            confidence=self._output.confidence if self._output else None,
            provenance=Provenance.from_model(
                provider="stub", model="stub-1", prompt_version="v1"
            ),
        )


def understanding(**overrides: Any) -> ReplyUnderstanding:
    base: dict[str, Any] = {
        "intent": ReplyIntent.PROMISE,
        "promise_detected": True,
        "committed_date": "2026-09-04",
        "conditional": False,
        "language": "en",
        "recommended_action": ReplyRecommendation.PAUSE_CHASE,
        "confidence": 0.9,
        "reasoning": "stubbed",
    }
    return ReplyUnderstanding.model_validate(base | overrides)


def read(stub: StubAgent, text: str = "We will pay on 4 Sep.") -> ExtractionResult:
    return understand_reply(
        invoice=make_invoice(),
        reply=make_reply(text),
        agent=stub,  # type: ignore[arg-type]
        now=REPLY_AT,
        min_confidence=default_config().understanding_min_confidence,
    )


# ---------------------------------------------------------------------------
# The fallback abstains. This is the whole contract.
# ---------------------------------------------------------------------------


def test_the_registered_fallback_abstains_and_produces_no_reading() -> None:
    """`llm-provider` -> Engine 3: no regex, no keyword heuristic, no guess."""
    result = _understand_reply_fallback({"reply_id": "inv_0001_in_1"})
    assert result.abstained is True
    assert result.output is None
    assert result.confidence is None


def test_a_provider_outage_abstains_rather_than_reading_anything() -> None:
    extraction = read(StubAgent(None, abstain=True))
    assert extraction.abstained is True
    assert extraction.understanding is None
    assert extraction.intent is ReplyIntent.UNCLEAR
    assert extraction.provenance.abstained is True
    assert extraction.provenance.source is ProvenanceSource.DETERMINISTIC


def test_an_abstention_is_not_a_dispute() -> None:
    """Two different rules, two different meanings.

    An abstention routes to a human under HE1; a dispute freezes the invoice
    under RL4. Conflating them would make the dispute count a measure of model
    failure.
    """
    extraction = read(StubAgent(None, abstain=True))
    assert extraction.is_dispute is False


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"intent": ReplyIntent.PROMISE, "promise_detected": False}, "does not agree with itself"),
        (
            {"intent": ReplyIntent.NON_RESPONSE, "promise_detected": True},
            "not a commitment",
        ),
        (
            # A dispute that also claims a promise falls into the same branch:
            # `dispute` is not a promise intent.
            {"intent": ReplyIntent.DISPUTE, "promise_detected": True},
            "not a commitment",
        ),
        (
            {
                "intent": ReplyIntent.PROMISE,
                "promise_detected": True,
                "dispute_detail": "amount is wrong",
            },
            "dispute_detail was supplied",
        ),
        (
            {
                "intent": ReplyIntent.NON_RESPONSE,
                "promise_detected": False,
                "committed_date": "2026-09-04",
            },
            "commits to nothing",
        ),
        (
            {
                "intent": ReplyIntent.UNCLEAR,
                "promise_detected": False,
                "committed_date": None,
            },
            "explicitly unclear",
        ),
    ],
)
def test_an_incoherent_reading_abstains_instead_of_being_reconciled(
    overrides: dict[str, Any], expected_fragment: str
) -> None:
    """A reading that disagrees with itself is not a reading.

    Picking the half that looks more plausible would be the system guessing on
    the model's behalf, which is the one thing this task must not do.
    """
    extraction = read(StubAgent(understanding(**overrides)))
    assert extraction.abstained is True
    assert extraction.understanding is None
    assert expected_fragment in (extraction.abstention_reason or "")


def test_a_coherent_reading_is_kept_with_its_provenance() -> None:
    extraction = read(StubAgent(understanding()))
    assert extraction.abstained is False
    assert extraction.provenance.source is ProvenanceSource.MODEL
    assert extraction.provenance.model == "stub-1"


# ---------------------------------------------------------------------------
# No fabricated promises — the adversarial block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        ".",
        "ok",
        "Dekhte hain.",
        "Our vendor promised us they'd pay on 3rd September.",
        "If we were going to pay it would be around the 10th.",
    ],
)
def test_an_unreadable_reply_never_yields_a_confident_promise(text: str) -> None:
    """Bait designed to produce a false positive, with the provider gone.

    The engine's guarantee under degradation is absolute and has nothing to do
    with how clever the model is: with no reading available, there is no promise,
    whatever the text says. The live model's own resistance to this bait is
    measured separately and reported in the metrics doc.
    """
    extraction = read(StubAgent(None, abstain=True), text=text)
    assert extraction.abstained is True
    assert extraction.understanding is None
    assert committed_datetime(extraction) is None


def test_a_low_confidence_reading_never_reaches_the_engine() -> None:
    """The floor is enforced by `LLMAgent`, so the task degrades before us.

    Asserted through the registry rather than by re-implementing the check: a
    test that re-derived the floor could pass while the wrapper used a different
    one.
    """
    from app.services.llm_agent import REGISTRY

    task = REGISTRY.get(TASK_UNDERSTAND_REPLY)
    assert task.min_confidence == default_config().understanding_min_confidence
    assert task.min_confidence > 0.0
    assert task.fallback({"reply_id": "x"}).abstained is True


# ---------------------------------------------------------------------------
# Relative-date resolution
# ---------------------------------------------------------------------------


def test_a_committed_date_is_anchored_to_the_reply_not_to_run_time() -> None:
    """The subtle one: an evening promise judged against midnight is a bug.

    A commitment made at 18:00 for "the 14th" compared against 00:00 on the 14th
    would be an hour-accurate promise scored as broken.
    """
    evening = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
    extraction = understand_reply(
        invoice=make_invoice(),
        reply=make_reply("We will pay on 4 Sep.", received_at=evening),
        agent=StubAgent(understanding(committed_date="2026-09-04")),  # type: ignore[arg-type]
        now=REPLY_AT,
        min_confidence=0.6,
    )
    committed = committed_datetime(extraction)
    assert committed is not None
    assert (committed.year, committed.month, committed.day) == (2026, 9, 4)
    assert (committed.hour, committed.minute) == (18, 30)


def test_an_unparseable_date_degrades_to_no_date_not_to_a_dead_batch() -> None:
    """A date the system cannot read is a promise with no date — PP3 handles it."""
    extraction = read(StubAgent(understanding(committed_date="next Friday")))
    assert extraction.abstained is False
    assert committed_datetime(extraction) is None


def test_month_end_resolves_against_the_reply_month() -> None:
    """The anchor the prompt describes, defined once so the two cannot drift."""
    assert month_end(datetime(2026, 8, 28, tzinfo=UTC)).day == 31
    assert month_end(datetime(2026, 9, 3, tzinfo=UTC)).day == 30
    assert month_end(datetime(2026, 2, 10, tzinfo=UTC)).day == 28


def test_the_prompt_context_carries_the_reply_date_and_weekday() -> None:
    """The whole defence against resolving "next Friday" against the wrong week.

    A model given only the text has no way to resolve a relative expression, and
    will confidently resolve it against something. This asserts the anchor is
    actually handed over.
    """
    context = build_context(
        invoice=make_invoice(),
        reply=make_reply("next Friday"),
        now=REPLY_AT + timedelta(days=30),
        min_confidence=0.6,
    )
    assert context["received_date"] == "2026-08-28"
    assert context["received_weekday"] == "Friday"


# ---------------------------------------------------------------------------
# Hinglish
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent", "is_promise"),
    [
        (
            "Sir humne payment 4 Sep ko schedule kar di hai, accounts team confirm kar degi.",
            ReplyIntent.PROMISE,
            True,
        ),
        (
            "Ye invoice toh already paid hai, humare accounts se UTR bhi mil gaya tha.",
            ReplyIntent.DISPUTE,
            False,
        ),
        ("Received, dekhta hoon. Abhi meeting mein hoon.", ReplyIntent.NON_RESPONSE, False),
    ],
)
def test_a_hinglish_reading_flows_through_the_engine_unchanged(
    text: str, intent: ReplyIntent, is_promise: bool
) -> None:
    """The engine must not treat a code-mixed reading as second-class.

    Whether the *model* reads Hinglish correctly is measured against the corpus,
    where 17 of 43 replies are code-mixed. What this pins is that nothing
    downstream — the dispute freeze, the promise register, the language field —
    behaves differently because the reply was not in English.
    """
    stub = StubAgent(
        understanding(
            intent=intent,
            promise_detected=is_promise,
            committed_date="2026-09-04" if is_promise else None,
            language="hinglish",
        )
    )
    extraction = read(stub, text=text)
    assert extraction.abstained is False
    assert extraction.intent is intent
    assert extraction.is_dispute is (intent is ReplyIntent.DISPUTE)
    assert extraction.understanding is not None
    assert extraction.understanding.language == "hinglish"
    assert extraction.text == text
