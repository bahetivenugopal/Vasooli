"""Reply understanding — the reasoning task, its abstention, and its fallback.

This is the one component of the project that no rule engine could replace. A
hedged, code-mixed, human reply either constitutes a commitment or it does not,
and deciding which is a reading task.

**The fallback abstains.** There is no regex, no keyword heuristic, no partial
guess. That is a deliberate asymmetry against Engine 2's templated dunning
fallback, and the reasoning is worth stating plainly: a boring accurate message
costs almost nothing, but a *fabricated promise* suppresses a legitimate chase
and then escalates the customer for breaking a commitment they never made. So
this task degrades to abstention rather than to a cheaper answer — `llm-provider`
-> Per-task fallback behaviour, and `policy-bounds:HE1` for where the abstention
goes.

Three things can produce an abstention, and all three are recorded the same way:

1. The provider is unavailable, rate-limited, or deterministic mode is on.
2. The reading came back below the configured confidence floor.
3. The reading came back **internally contradictory** — an intent of `promise`
   with `promise_detected: false`, or a promise carrying a dispute detail. A
   reading that disagrees with itself is not a reading, and silently
   reconciling it would be the system inventing the answer it refused to guess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.engines.receivables.schemas import (
    PROMISE_INTENTS,
    ExtractionResult,
    Invoice,
    Reply,
    ReplyIntent,
    ReplyUnderstanding,
)
from app.services.llm_agent import (
    REGISTRY,
    FallbackResult,
    LLMAgent,
    ReasoningTask,
    TaskRegistry,
)

TASK_UNDERSTAND_REPLY = "understand_reply"

#: The citation a successfully extracted promise carries.
EXTRACTION_RULE = "policy-bounds:PR1"
#: The citation an abstention carries — it routes to human review.
ABSTENTION_RULE = "policy-bounds:HE1"


def _understand_reply_fallback(context: dict[str, Any]) -> FallbackResult:
    """The registered deterministic fallback: **abstain**.

    Returns no output at all. A caller that treats `output is None` as "no
    promise" rather than as "unknown" would reintroduce exactly the failure this
    fallback exists to prevent, which is why `ExtractionResult.abstained` is a
    field the rest of the engine has to branch on rather than infer.
    """
    return FallbackResult(
        output=None,
        abstained=True,
        rationale=(
            f"Reply {context.get('reply_id', 'unknown')} could not be read by the "
            "reasoning layer. Marked for human review rather than approximated: a "
            "fabricated promise would suppress a legitimate chase and corrupt the "
            "promise register, so this task has no cheaper answer to degrade to."
        ),
        confidence=None,
    )


def build_context(
    *,
    invoice: Invoice,
    reply: Reply,
    now: datetime,
    min_confidence: float,
) -> dict[str, Any]:
    """Everything the prompt needs to read one reply.

    `received_date` and `received_weekday` are supplied explicitly because
    relative-date resolution is the part of this task that goes wrong silently.
    A model resolving "end of the month" against its own idea of today, rather
    than against the day the reply arrived, produces a confidently wrong date
    that looks entirely reasonable in the trail.
    """
    received = reply.received_at.astimezone(UTC)
    return {
        "invoice_id": invoice.invoice_id,
        "customer_name": invoice.customer_name,
        "amount_rupees": f"Rs {invoice.outstanding_paise / 100:,.2f}",
        "due_date": f"{invoice.due_at:%Y-%m-%d}",
        "days_overdue": max((received - invoice.due_at).days, 0),
        "rungs_used": invoice.rungs_used,
        "received_date": f"{received:%Y-%m-%d}",
        "received_weekday": f"{received:%A}",
        "reply_text": reply.text,
        "min_confidence": f"{min_confidence:.2f}",
        # Not rendered into the prompt — carried so the fallback can name the
        # reply it declined to read.
        "reply_id": reply.reply_id,
    }


def _contradiction(understanding: ReplyUnderstanding) -> str | None:
    """Name the self-contradiction in a reading, or None if it is coherent.

    Checked rather than repaired. A reading that disagrees with itself is
    evidence the model did not commit to an answer, and quietly picking the half
    that looks more plausible is the system guessing on its behalf.
    """
    is_promise_intent = understanding.intent in PROMISE_INTENTS
    if is_promise_intent and not understanding.promise_detected:
        return (
            f"intent {understanding.intent.value!r} but promise_detected is false — "
            "the reading does not agree with itself about whether a commitment exists"
        )
    if understanding.promise_detected and not is_promise_intent:
        return (
            f"promise_detected is true but intent is {understanding.intent.value!r}, "
            "which is not a commitment"
        )
    # A dispute carrying `promise_detected` is already caught by the branch
    # above — `DISPUTE` is not a promise intent — so it needs no branch of its
    # own. Noted rather than written, because an unreachable check reads like a
    # guarantee and is not one.
    if (
        understanding.intent is not ReplyIntent.DISPUTE
        and understanding.dispute_detail
        and understanding.dispute_detail.strip()
    ):
        return (
            f"dispute_detail was supplied on a {understanding.intent.value!r} reply, "
            "so it is unclear whether this invoice is being challenged"
        )
    if understanding.committed_date is not None and not understanding.promise_detected:
        return "a committed date was extracted from a reply that commits to nothing"
    if understanding.intent is ReplyIntent.UNCLEAR:
        return "the reading is explicitly unclear"
    return None


def _parse_date(value: str | None, *, anchor: datetime) -> datetime | None:
    """Turn the model's `YYYY-MM-DD` into a tz-aware UTC datetime.

    Anything unparseable becomes `None` — a date the system cannot read is a
    promise with no date, which the PP3 horizon already handles. Raising here
    would turn a soft failure into a dead batch.

    The time of day is taken from the reply, so a commitment made at 18:00 is
    compared against the same hour on the promised day. Otherwise a promise made
    in the evening for "the 14th" would be judged against midnight on the 14th
    and be an hour-accurate promise scored as broken.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    anchored = anchor.astimezone(UTC)
    return parsed.astimezone(UTC).replace(
        hour=anchored.hour, minute=anchored.minute, second=0, microsecond=0
    )


def understand_reply(
    *,
    invoice: Invoice,
    reply: Reply,
    agent: LLMAgent,
    now: datetime,
    min_confidence: float,
) -> ExtractionResult:
    """Read one reply, or refuse to.

    Never raises: every path returns an `ExtractionResult` carrying either a
    reading or an abstention, because a batch that dies halfway through the
    corpus proves nothing about extraction accuracy.
    """
    context = build_context(
        invoice=invoice, reply=reply, now=now, min_confidence=min_confidence
    )
    result = agent.run(TASK_UNDERSTAND_REPLY, context)
    output = result.output

    if result.abstained or not isinstance(output, ReplyUnderstanding):
        return ExtractionResult(
            invoice_id=invoice.invoice_id,
            reply_id=reply.reply_id,
            received_at=reply.received_at,
            text=reply.text,
            understanding=None,
            abstained=True,
            provenance=result.provenance,
            abstention_reason=(
                result.degradation_reason or "the reasoning layer returned no reading"
            ),
            confidence=result.confidence,
            degraded=result.degraded,
            degradation_reason=result.degradation_reason,
        )

    contradiction = _contradiction(output)
    if contradiction is not None:
        return ExtractionResult(
            invoice_id=invoice.invoice_id,
            reply_id=reply.reply_id,
            received_at=reply.received_at,
            text=reply.text,
            understanding=None,
            abstained=True,
            provenance=result.provenance.model_copy(update={"abstained": True}),
            abstention_reason=f"incoherent reading: {contradiction}",
            confidence=result.confidence,
            degraded=result.degraded,
            degradation_reason=result.degradation_reason,
        )

    return ExtractionResult(
        invoice_id=invoice.invoice_id,
        reply_id=reply.reply_id,
        received_at=reply.received_at,
        text=reply.text,
        understanding=output,
        abstained=False,
        provenance=result.provenance,
        confidence=result.confidence,
        degraded=result.degraded,
        degradation_reason=result.degradation_reason,
    )


def committed_datetime(extraction: ExtractionResult) -> datetime | None:
    """The absolute commitment date from a reading, anchored to the reply."""
    if extraction.understanding is None:
        return None
    return _parse_date(
        extraction.understanding.committed_date, anchor=extraction.received_at
    )


def month_end(anchor: datetime) -> datetime:
    """Last day of `anchor`'s month, same time of day. Used by the tests.

    Kept here rather than in a test helper because it is the definition
    `policy-bounds` and the prompt both rely on for "end of the month", and a
    second copy in the suite could drift from the one the prompt describes.
    """
    first_next = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def register_receivables_tasks(registry: TaskRegistry = REGISTRY) -> None:
    """Register Engine 3's reasoning task.

    Called from the app lifespan alongside the other engines'. Registration loads
    the prompt file eagerly, so a missing or unversioned prompt fails at boot
    rather than mid-batch.
    """
    from app.engines.receivables.config import default_config

    registry.register(
        ReasoningTask(
            name=TASK_UNDERSTAND_REPLY,
            prompt_name=TASK_UNDERSTAND_REPLY,
            response_model=ReplyUnderstanding,
            fallback=_understand_reply_fallback,
            description=(
                "Read one customer reply to an overdue-invoice reminder: intent, "
                "promise and its date, conditionality, dispute. Abstains rather "
                "than guessing."
            ),
            min_confidence=default_config().understanding_min_confidence,
        )
    )


__all__ = [
    "ABSTENTION_RULE",
    "EXTRACTION_RULE",
    "TASK_UNDERSTAND_REPLY",
    "build_context",
    "committed_datetime",
    "month_end",
    "register_receivables_tasks",
    "understand_reply",
]
