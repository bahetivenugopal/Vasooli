"""The customer-reply corpus Engine 3's promise-to-pay extraction runs on.

This is the most load-bearing part of the invoice dataset. Engine 3's claim is
that it reads a free-text reply, decides whether it contains a commitment, pins
the date, and escalates only when that commitment is *broken*. A corpus of
"I will pay on 12/09/2026" would make that a regex exercise and prove nothing.

So the corpus deliberately contains:

- **Explicit promises** with a real date, in several formats — an extractor that
  handles one date format is not an extractor.
- **Vague promises** where a careful human can still infer an anchor ("end of the
  month") and vaguer ones where they honestly cannot ("next week sometime").
  Ground truth records which is which via `date_confidence`, so extraction can be
  scored without pretending the ambiguous cases have a right answer.
- **Conditional promises** — "once our client pays us" — which are commitments to
  intent, not to a date. Treating these as firm promises is the failure mode
  worth catching.
- **Disputes**, which must never be dunned harder; they escalate to a human.
- **Non-responses**: out-of-office and auto-acknowledgements, which look like
  replies and contain nothing.
- **Hinglish and code-mixed replies**, because this is an Indian B2B context and
  half of real collections correspondence looks like this.

Every template carries its own annotation. That annotation goes to the manifest,
never to the record — see the ground-truth separation test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from random import Random

#: Categories a reply can belong to. `non_response` is a reply that arrived and
#: said nothing — distinct from an invoice with no reply at all, which is silence.
CATEGORIES = (
    "explicit_promise",
    "vague_promise",
    "conditional_promise",
    "dispute",
    "non_response",
)


@dataclass(frozen=True)
class ReplyTemplate:
    """One reply, with the annotation a careful human would produce for it."""

    category: str
    language: str
    #: `{date}` is substituted when `date_style == "explicit"`.
    text: str
    is_promise: bool
    is_dispute: bool
    is_conditional: bool
    #: "explicit" — a date is stated. "inferable" — a human can pin it (month
    #: end). "none" — no date can honestly be extracted.
    date_confidence: str
    #: How far out the promised date lands, for explicit promises.
    offset_days: int = 0
    date_style: str = "none"


def _t(
    category: str,
    language: str,
    text: str,
    *,
    is_promise: bool = False,
    is_dispute: bool = False,
    is_conditional: bool = False,
    date_confidence: str = "none",
    offset_days: int = 0,
    date_style: str = "none",
) -> ReplyTemplate:
    return ReplyTemplate(
        category=category,
        language=language,
        text=text,
        is_promise=is_promise,
        is_dispute=is_dispute,
        is_conditional=is_conditional,
        date_confidence=date_confidence,
        offset_days=offset_days,
        date_style=date_style,
    )


TEMPLATES: tuple[ReplyTemplate, ...] = (
    # --- Explicit promises, English ---------------------------------------
    _t(
        "explicit_promise",
        "en",
        "Thanks for the follow-up. We have queued this for payment and will "
        "process it on {date}.",
        is_promise=True,
        date_confidence="explicit",
        offset_days=6,
        date_style="explicit",
    ),
    _t(
        "explicit_promise",
        "en",
        "Apologies for the delay — our finance head was travelling. The transfer "
        "will go out on {date} without fail.",
        is_promise=True,
        date_confidence="explicit",
        offset_days=9,
        date_style="explicit",
    ),
    _t(
        "explicit_promise",
        "en",
        "Payment run is on {date}. Your invoice is in that batch, you should see "
        "the credit the same evening.",
        is_promise=True,
        date_confidence="explicit",
        offset_days=4,
        date_style="explicit",
    ),
    # --- Explicit promises, Hinglish --------------------------------------
    _t(
        "explicit_promise",
        "hinglish",
        "Sir humne payment {date} ko schedule kar di hai, accounts team confirm "
        "kar degi.",
        is_promise=True,
        date_confidence="explicit",
        offset_days=5,
        date_style="explicit",
    ),
    _t(
        "explicit_promise",
        "hinglish",
        "Thoda delay ho gaya, sorry. {date} tak payment definitely clear kar "
        "denge, aap tension mat lijiye.",
        is_promise=True,
        date_confidence="explicit",
        offset_days=8,
        date_style="explicit",
    ),
    # --- Vague promises ----------------------------------------------------
    _t(
        "vague_promise",
        "en",
        "Noted. This should be sorted by the end of the month from our side.",
        is_promise=True,
        date_confidence="inferable",
        date_style="month_end",
    ),
    _t(
        "vague_promise",
        "en",
        "We are on it. Expect the payment next week sometime, our accounts team "
        "is catching up on a backlog.",
        is_promise=True,
        date_confidence="none",
    ),
    _t(
        "vague_promise",
        "hinglish",
        "Payment process mein hai bhai, 2-3 din mein credit ho jayega most "
        "probably.",
        is_promise=True,
        date_confidence="none",
    ),
    _t(
        "vague_promise",
        "hinglish",
        "Abhi thoda funds tight hai. Month end tak dekhta hoon, kuch na kuch "
        "arrange kar lenge.",
        is_promise=True,
        date_confidence="inferable",
        date_style="month_end",
    ),
    # --- Conditional promises ---------------------------------------------
    _t(
        "conditional_promise",
        "en",
        "We can release this once our client settles their own invoice with us. "
        "Should not be long, but I cannot commit to a date yet.",
        is_promise=True,
        is_conditional=True,
        date_confidence="none",
    ),
    _t(
        "conditional_promise",
        "en",
        "Subject to the GST refund coming through this quarter, we will clear "
        "the outstanding in full.",
        is_promise=True,
        is_conditional=True,
        date_confidence="none",
    ),
    _t(
        "conditional_promise",
        "hinglish",
        "Client se payment aate hi turant aapka clear kar denge, abhi date nahi "
        "bata sakta.",
        is_promise=True,
        is_conditional=True,
        date_confidence="none",
    ),
    # --- Disputes ----------------------------------------------------------
    _t(
        "dispute",
        "en",
        "This invoice was already paid on the 14th by NEFT. Please check with "
        "your bank before sending further reminders.",
        is_dispute=True,
    ),
    _t(
        "dispute",
        "en",
        "The amount is wrong. We were quoted a lower rate and the GST split does "
        "not match our purchase order. Sending our PO copy for reference.",
        is_dispute=True,
    ),
    _t(
        "dispute",
        "hinglish",
        "Ye invoice toh already paid hai, humare accounts se UTR bhi mil gaya "
        "tha. Please apne end pe check karwa lijiye.",
        is_dispute=True,
    ),
    _t(
        "dispute",
        "en",
        "We never received the second half of this delivery, so this invoice is "
        "on hold until the shortfall is resolved.",
        is_dispute=True,
    ),
    # --- Non-responses -----------------------------------------------------
    _t(
        "non_response",
        "en",
        "I am out of office until further notice with limited access to email. "
        "For urgent matters please contact the front desk.",
    ),
    _t(
        "non_response",
        "en",
        "Thank you for your email. Your message has been received and assigned "
        "ticket AP-4471. Our accounts payable team will revert.",
    ),
    _t(
        "non_response",
        "hinglish",
        "Received, dekhta hoon. Abhi meeting mein hoon.",
    ),
)


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def _month_end(anchor: date) -> date:
    first_next = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def render(
    template: ReplyTemplate, r: Random, received_on: date
) -> tuple[str, date | None]:
    """Render a template and return `(text, promised_date)`.

    The date is written in one of several everyday formats, chosen by the seeded
    stream. An extractor that only handles ISO dates should fail on this corpus,
    and it is better to find that out here than in front of a panel.
    """
    if template.date_style == "month_end":
        return template.text, _month_end(received_on)

    if template.date_style != "explicit":
        return template.text, None

    promised = received_on + timedelta(days=template.offset_days)
    formats = (
        f"{_ordinal(promised.day)} {promised.strftime('%B')}",
        promised.strftime("%d/%m/%Y"),
        f"the {_ordinal(promised.day)}",
        promised.strftime("%d %b"),
    )
    rendered = template.text.replace("{date}", formats[int(r.random() * len(formats))])
    return rendered, promised


def templates_for(category: str) -> tuple[ReplyTemplate, ...]:
    return tuple(t for t in TEMPLATES if t.category == category)


__all__ = ["CATEGORIES", "TEMPLATES", "ReplyTemplate", "render", "templates_for"]
