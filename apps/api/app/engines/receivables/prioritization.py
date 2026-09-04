"""Deterministic invoice prioritisation — `policy-bounds:PR1`.

**No model touches this.** Ranking is a weighted sum over four things a
collections team would name out loud: how much is outstanding, how late it is,
how reliably this customer keeps commitments, and how much contact budget is left
this week. There is no judgment here a weighted sum does not capture, and a
reasoned ranking would be unexplainable and unstable across runs for no gain.

Every score ships its own breakdown — each term's raw value, its normalisation,
its weight and its contribution — because a recovery order that a customer could
be shown, and argued with, has to be reproducible.

Two properties worth reading closely:

- **Ranking decides order, never permission.** A rank-1 invoice still passes
  every gate; a rank-70 one is not forbidden, only later in the queue. The
  ordering does have one real consequence, and it is deliberate: the RL3
  per-customer cap is consumed in rank order, so when a customer's weekly budget
  runs out it runs out on their *least* important invoice.
- **An invoice with a live promise is pushed down, not filtered out**
  (`policy-bounds:PR2`). It stays on the worklist with its deprioritisation
  visible, because "we chose not to chase this, and here is the rule" is the
  claim worth making. Filtering it out would make the same decision invisible.
"""

from __future__ import annotations

from app.engines.receivables.config import ReceivablesConfig
from app.engines.receivables.schemas import (
    CustomerReliability,
    Invoice,
    PriorityScore,
    PromiseAssessment,
    ScoreComponent,
)

#: What a deprioritised invoice's score is multiplied by. Not a weight — a
#: demotion, applied after the weighted sum so the components still read
#: truthfully and the reason is a separate, visible field.
DEPRIORITISATION_FACTOR = 0.1


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_invoice(
    *,
    invoice: Invoice,
    config: ReceivablesConfig,
    now,
    max_outstanding_paise: int,
    reliability: CustomerReliability | None,
    promise: PromiseAssessment | None,
    customer_messages_used: int,
) -> PriorityScore:
    """One invoice's priority, with every term of it visible."""
    weights = config.prioritization.weights
    components: list[ScoreComponent] = []

    outstanding = invoice.outstanding_paise
    value_norm = _clamp(outstanding / max_outstanding_paise) if max_outstanding_paise else 0.0
    components.append(
        ScoreComponent(
            name="outstanding_value",
            raw=float(outstanding),
            normalized=round(value_norm, 4),
            weight=weights["outstanding_value"],
            contribution=round(value_norm * weights["outstanding_value"], 4),
            explanation=(
                f"Rs {outstanding / 100:,.2f} outstanding, as a share of the largest "
                f"outstanding invoice in this run. Recoverable rupees lead the "
                "ranking because they are the point."
            ),
        )
    )

    days_late = invoice.days_late(now)
    cap = config.prioritization.days_overdue_cap
    ageing_norm = _clamp(days_late / cap) if cap else 0.0
    components.append(
        ScoreComponent(
            name="days_overdue",
            raw=float(days_late),
            normalized=round(ageing_norm, 4),
            weight=weights["days_overdue"],
            contribution=round(ageing_norm * weights["days_overdue"], 4),
            explanation=(
                f"{days_late} days past due, capped at {cap}. Collectability falls "
                "with age, so ageing is second — but capped, because a 400-day "
                "invoice is not four times more urgent than a 100-day one."
            ),
        )
    )

    if reliability is not None and reliability.reliability is not None:
        unreliability = 1.0 - reliability.reliability
        history_note = (
            f"{reliability.kept} of {reliability.kept + reliability.broken} resolved "
            f"promises kept."
        )
    else:
        unreliability = 1.0 - config.prioritization.unknown_reliability
        history_note = (
            "no resolved promise on record; the neutral prior applies so that no "
            "history neither helps nor hurts."
        )
    components.append(
        ScoreComponent(
            name="customer_unreliability",
            raw=round(unreliability, 4),
            normalized=round(_clamp(unreliability), 4),
            weight=weights["customer_unreliability"],
            contribution=round(_clamp(unreliability) * weights["customer_unreliability"], 4),
            explanation=(
                f"{history_note} A customer who breaks commitments is where attention "
                "pays; one who keeps them does not need chasing to pay."
            ),
        )
    )

    headroom_cap = config.contact_caps.per_customer
    headroom = _clamp((headroom_cap - customer_messages_used) / headroom_cap)
    components.append(
        ScoreComponent(
            name="contact_headroom",
            raw=float(headroom_cap - customer_messages_used),
            normalized=round(headroom, 4),
            weight=weights["contact_headroom"],
            contribution=round(headroom * weights["contact_headroom"], 4),
            explanation=(
                f"{headroom_cap - customer_messages_used} of {headroom_cap} messages "
                "left for this customer this week (policy-bounds:RL3). An invoice we "
                "are not permitted to contact about should not sit at the top of a "
                "worklist a human reads."
            ),
        )
    )

    score = round(sum(c.contribution for c in components), 6)

    deprioritised = promise is not None and promise.suppresses_chase
    if deprioritised:
        assert promise is not None
        score = round(score * DEPRIORITISATION_FACTOR, 6)

    return PriorityScore(
        invoice_id=invoice.invoice_id,
        customer_id=invoice.customer_id,
        score=score,
        components=components,
        deprioritised=deprioritised,
        deprioritised_rule="policy-bounds:PR2" if deprioritised else None,
        deprioritised_reason=(promise.rationale if deprioritised and promise else None),
    )


def build_worklist(
    *,
    invoices: list[Invoice],
    config: ReceivablesConfig,
    now,
    reliability: dict[str, CustomerReliability],
    promises: dict[str, PromiseAssessment],
    customer_messages_used: dict[str, int],
) -> list[PriorityScore]:
    """Rank every chaseable invoice, highest priority first.

    Two exclusions, both deliberate. **Settled invoices** have nothing
    outstanding to prioritise, and `policy-bounds:HS1` makes chasing one a hard
    stop anyway. **Invoices not yet due** are not late: chasing someone before
    their payment terms have run out is not diligence, and a worklist that
    contained them would put the engine's least defensible action at the top of a
    page a human reads.

    Deprioritised invoices do stay on the list, with their reason visible — "we
    chose not to chase this, and here is the rule" is the claim worth making, and
    filtering would make the same decision invisible.

    Ties break on `invoice_id`, so the same seed and the same clock produce the
    same order on every filesystem and every platform.
    """
    chaseable = [
        i
        for i in invoices
        if not i.is_paid and i.outstanding_paise > 0 and i.days_late(now) > 0
    ]
    max_outstanding = max((i.outstanding_paise for i in chaseable), default=0)

    scored = [
        score_invoice(
            invoice=invoice,
            config=config,
            now=now,
            max_outstanding_paise=max_outstanding,
            reliability=reliability.get(invoice.customer_id),
            promise=promises.get(invoice.invoice_id),
            customer_messages_used=customer_messages_used.get(invoice.customer_id, 0),
        )
        for invoice in chaseable
    ]
    scored.sort(key=lambda s: (-s.score, s.invoice_id))
    return [s.model_copy(update={"rank": rank}) for rank, s in enumerate(scored, start=1)]


__all__ = ["DEPRIORITISATION_FACTOR", "build_worklist", "score_invoice"]
