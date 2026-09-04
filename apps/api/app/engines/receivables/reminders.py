"""Reminder drafting — deterministic rung templates, tone-validated.

**No model writes these.** The phase asks for exactly one reasoning task in this
engine, and reply understanding is it; a second one drafting reminders would add
a live call per invoice for text that has three tones and one call to action per
rung. So the rungs are templates, and the interesting property is that they still
pass the same gate a model's output would:

- **TN1** — the shared forbidden-language scan in `services/tone.py`. A template
  that a rejection would fall back *to* must satisfy the constraints itself, and
  Engine 2 learned that the expensive way: its first deterministic run produced
  nine tone rejections against its own templates. A parametrised test walks every
  rung here for the same reason.
- **TN2** — a remedy the customer can actually complete. `PERMITTED_CTA` is the
  receivables version of the same map: an invoice reminder can ask for a payment,
  a date, remittance advice, or a conversation with accounts, and nothing else.

The escalation ladder's tone escalates; the ladder's *claims* do not. A formal
notice is more direct than a gentle reminder and still threatens nothing, invents
no deadline and asserts no consequence — that is the whole point of TN1 applying
to every rung equally rather than loosening as the ladder climbs.
"""

from __future__ import annotations

from datetime import datetime

from app.engines.receivables.config import ReceivablesConfig, Rung
from app.engines.receivables.schemas import (
    Invoice,
    ReminderCallToAction,
    ReminderDraft,
)
from app.services.tone import scan_forbidden

#: `policy-bounds:TN2`, for invoice reminders. Deliberately narrow: everything a
#: receivables reminder may ask for is something the recipient can do from their
#: own accounts-payable desk today.
PERMITTED_CTA: frozenset[ReminderCallToAction] = frozenset(ReminderCallToAction)

#: The rule a rendered reminder cites when it goes out.
REMINDER_RULE = "policy-bounds:RL1"
#: The rule a rejected draft cites. Kept for symmetry with Engine 2's TN3 path.
TEMPLATE_RULE = "policy-bounds:TN3"


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _body(
    *,
    rung: Rung,
    invoice: Invoice,
    now: datetime,
    payment_link_url: str | None,
) -> tuple[str, str]:
    """Subject and body for one rung. Returns `(subject, body)`.

    Every one of these states the same three facts — which invoice, how much, how
    late — and differs only in directness. Nothing escalates into a claim about
    consequences, because there are none to claim.
    """
    days_late = invoice.days_late(now)
    amount = _rupees(invoice.outstanding_paise)
    link_line = (
        f" You can settle it here: {payment_link_url}"
        if payment_link_url
        else " If it is easier, reply and we will send a payment link."
    )

    if rung.name == "gentle_reminder":
        return (
            f"Invoice {invoice.invoice_id} — {amount} outstanding",
            (
                f"Hi {invoice.customer_name}, invoice {invoice.invoice_id} for {amount} "
                f"came due on {invoice.due_at:%d %b %Y} and is showing as unpaid "
                f"{days_late} days on. It may well already be in your payment run — "
                f"if so, do let us know the date and we will stop chasing.{link_line}"
            ),
        )
    if rung.name == "firm_follow_up":
        return (
            f"Following up: invoice {invoice.invoice_id}, {amount} outstanding",
            (
                f"Hi {invoice.customer_name}, we have not yet been able to reconcile "
                f"payment for invoice {invoice.invoice_id} ({amount}), now {days_late} "
                f"days past its {invoice.due_at:%d %b %Y} due date. Could you confirm "
                f"when it is scheduled, or let us know if something is holding it "
                f"up?{link_line}"
            ),
        )
    return (
        f"Invoice {invoice.invoice_id} — {amount}, {days_late} days outstanding",
        (
            f"Dear {invoice.customer_name}, invoice {invoice.invoice_id} for {amount} "
            f"remains outstanding {days_late} days past its due date of "
            f"{invoice.due_at:%d %b %Y}. We would like to close this off. Please "
            f"either settle it or tell us what is preventing payment, and we will "
            f"take it from there with your accounts team.{link_line}"
        ),
    )


def render(
    *,
    rung: Rung,
    invoice: Invoice,
    now: datetime,
    config: ReceivablesConfig,
    payment_link_url: str | None = None,
    payment_link_id: str | None = None,
) -> ReminderDraft:
    """The reminder for one rung of one invoice."""
    subject, body = _body(
        rung=rung, invoice=invoice, now=now, payment_link_url=payment_link_url
    )
    # A message with no link cannot ask the customer to use one. The rung's
    # configured call to action is the intent; this is TN2 applied to what was
    # actually rendered, which is the only version that can be checked.
    cta = rung.call_to_action
    if cta is ReminderCallToAction.PAY_VIA_LINK and payment_link_url is None:
        cta = ReminderCallToAction.CONFIRM_PAYMENT_DATE
    return ReminderDraft(
        rung=rung.name,
        rung_number=rung.number,
        channel=rung.channel,
        subject=subject,
        body=body,
        call_to_action=cta,
        payment_link_url=payment_link_url,
        payment_link_id=payment_link_id,
    )


def validate_tone(draft: ReminderDraft) -> list[dict[str, str]]:
    """Check a rendered reminder against TN1 and TN2. Empty means it may proceed.

    Returns every violation rather than the first, so an audit entry can say what
    was wrong with the message instead of only that something was.
    """
    violations = [
        {"rule_id": "policy-bounds:TN1", "detail": detail}
        for detail in scan_forbidden(f"{draft.subject}\n{draft.body}")
    ]
    if draft.call_to_action not in PERMITTED_CTA:
        violations.append(
            {
                "rule_id": "policy-bounds:TN2",
                "detail": (
                    f"call to action {draft.call_to_action.value!r} is not something an "
                    f"invoice reminder may ask for; permitted: "
                    f"{sorted(c.value for c in PERMITTED_CTA)}"
                ),
            }
        )
    if draft.call_to_action is ReminderCallToAction.PAY_VIA_LINK and not draft.payment_link_url:
        violations.append(
            {
                "rule_id": "policy-bounds:TN2",
                "detail": (
                    "the message asks the customer to pay via a link and carries no "
                    "link — a remedy they cannot complete"
                ),
            }
        )
    return violations


__all__ = [
    "PERMITTED_CTA",
    "REMINDER_RULE",
    "TEMPLATE_RULE",
    "render",
    "validate_tone",
]
