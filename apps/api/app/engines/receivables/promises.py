"""Promise-to-pay tracking, and the explicit broken-promise check.

The engine's differentiator lives here, and it is worth stating plainly: *the
system does not chase people who are cooperating; it chases the ones who
committed and did not follow through.*

That claim rests on four decisions, all of which are rules rather than code
conventions:

- **A promise is kept, broken, active or superseded — never "probably fine".**
  `policy-bounds:PP1` gives 48 hours of grace past the committed date before
  anything is called broken, because a payment initiated on the promised day does
  not necessarily land on it.
- **A conditional promise is not a firm one** (`policy-bounds:PP2`). It is never
  scored kept or broken against a date it did not give, and it cannot trigger an
  escalation. Conflating the two is the most likely way this feature is quietly
  wrong in both directions.
- **A promise with no date still buys room** (`policy-bounds:PP3`) — seven days
  from the reply, then the ladder resumes. A pause, never a stop.
- **The check is explicit.** `assess()` is called on every promise every run and
  returns a status with a citation. A promise that decays into "broken" as a side
  effect of something else is a promise nobody can audit.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.engines.receivables.config import ReceivablesConfig
from app.engines.receivables.schemas import (
    CustomerReliability,
    ExtractionResult,
    Invoice,
    PromiseAssessment,
    PromiseStatus,
)
from app.engines.receivables.understanding import committed_datetime

#: Citations for each status transition. Held in one place so the trail and the
#: register cannot disagree about which rule decided a promise's fate.
KEPT_RULE = "policy-bounds:PP1"
BROKEN_RULE = "policy-bounds:PP1"
ACTIVE_DATED_RULE = "policy-bounds:PP1"
ACTIVE_UNDATEABLE_RULE = "policy-bounds:PP3"
CONDITIONAL_RULE = "policy-bounds:PP2"
SUPERSEDED_RULE = "policy-bounds:PP2"


def assess(
    *,
    invoice: Invoice,
    extraction: ExtractionResult,
    config: ReceivablesConfig,
    now: datetime,
) -> PromiseAssessment | None:
    """Where one extracted commitment stands as of `now`.

    Returns `None` when the reading contains no promise at all — including for an
    abstention, which is *not* the same as "no promise" and is handled by the
    caller under `policy-bounds:HE1`. Conflating the two here would let a reply
    the model refused to read quietly become a reply with nothing in it.
    """
    understanding = extraction.understanding
    if understanding is None or not understanding.promise_detected:
        return None

    committed = committed_datetime(extraction)
    conditional = understanding.conditional
    paid_at = invoice.paid_at

    def build(
        status: PromiseStatus, rule_id: str, rationale: str, review_at: datetime | None
    ) -> PromiseAssessment:
        return PromiseAssessment(
            invoice_id=invoice.invoice_id,
            reply_id=extraction.reply_id,
            customer_id=invoice.customer_id,
            received_at=extraction.received_at,
            committed_date=committed,
            committed_amount_paise=understanding.committed_amount_paise,
            conditional=conditional,
            condition_detail=understanding.condition_detail,
            status=status,
            rule_id=rule_id,
            rationale=rationale,
            review_at=review_at,
            # Suppression ends at the review moment, whatever the status says.
            # A conditional or undateable promise stays `active` forever because
            # it has no date it could break — but PP3 gives it seven days of
            # room, not indefinite shelter, and reading suppression off the
            # status alone parked those invoices permanently.
            suppresses_chase=(
                status is PromiseStatus.ACTIVE
                and (review_at is None or now < review_at)
            ),
            confidence=extraction.confidence,
            reasoning=understanding.reasoning,
        )

    # --- 1. A conditional promise, whatever date it may have mentioned ------
    #
    # PP2 first, deliberately. A conditional promise that happens to name a date
    # would otherwise be scored broken against a date the customer explicitly
    # made contingent, which is the exact conflation the rule forbids.
    if conditional:
        horizon = extraction.received_at + config.promises.undateable_horizon
        if paid_at is not None:
            return build(
                PromiseStatus.KEPT,
                CONDITIONAL_RULE,
                (
                    f"Conditional commitment ({understanding.condition_detail or 'unstated condition'}) "
                    f"and the invoice settled on {paid_at:%Y-%m-%d}. Recorded as kept, "
                    "but never scored against a date it did not give."
                ),
                None,
            )
        if now >= horizon:
            return build(
                PromiseStatus.ACTIVE,
                CONDITIONAL_RULE,
                (
                    "Conditional commitment to intent, not to a date. Its "
                    f"{config.promises.undateable_horizon.days}-day review horizon has "
                    "passed, so the ladder resumes — but PP2 forbids calling this "
                    "broken, because no date was ever committed to break."
                ),
                horizon,
            )
        return build(
            PromiseStatus.ACTIVE,
            CONDITIONAL_RULE,
            (
                f"Conditional on {understanding.condition_detail or 'an external event'}. "
                f"Held until {horizon:%Y-%m-%d %H:%M} UTC under the PP3 horizon; a "
                "conditional promise can never be broken, only exhausted."
            ),
            horizon,
        )

    # --- 2. A firm promise with a date -------------------------------------
    if committed is not None:
        deadline = committed + config.promises.grace
        if paid_at is not None:
            kept = paid_at <= deadline
            return build(
                PromiseStatus.KEPT if kept else PromiseStatus.BROKEN,
                KEPT_RULE if kept else BROKEN_RULE,
                (
                    f"Committed to {committed:%Y-%m-%d}; payment landed "
                    f"{paid_at:%Y-%m-%d}"
                    + (
                        f", inside the {_hours(config)}h grace. Kept."
                        if kept
                        else f", past the {_hours(config)}h grace. Broken."
                    )
                ),
                None,
            )
        if now > deadline:
            return build(
                PromiseStatus.BROKEN,
                BROKEN_RULE,
                (
                    f"Committed to {committed:%Y-%m-%d} and nothing has been received "
                    f"{_hours(config)}h past it. The grace window exists so settlement "
                    "lag is not mistaken for a broken promise; this is past it."
                ),
                deadline,
            )
        return build(
            PromiseStatus.ACTIVE,
            ACTIVE_DATED_RULE,
            (
                f"Committed to {committed:%Y-%m-%d}, still inside its window "
                f"(reviewable {deadline:%Y-%m-%d %H:%M} UTC). The customer has done "
                "what was asked; chasing them now is what makes automated "
                "collections feel like harassment."
            ),
            deadline,
        )

    # --- 3. A firm promise with no extractable date ------------------------
    horizon = extraction.received_at + config.promises.undateable_horizon
    if paid_at is not None:
        return build(
            PromiseStatus.KEPT,
            ACTIVE_UNDATEABLE_RULE,
            (
                f"Committed without naming a date; the invoice settled on "
                f"{paid_at:%Y-%m-%d}. Kept."
            ),
            None,
        )
    if now >= horizon:
        return build(
            PromiseStatus.ACTIVE,
            ACTIVE_UNDATEABLE_RULE,
            (
                "Committed without naming a date. The "
                f"{config.promises.undateable_horizon.days}-day PP3 horizon has passed, "
                "so the ladder resumes — but a promise with no date cannot be broken, "
                "so this never escalates on its own."
            ),
            horizon,
        )
    return build(
        PromiseStatus.ACTIVE,
        ACTIVE_UNDATEABLE_RULE,
        (
            "Committed without naming a date. Deprioritised until "
            f"{horizon:%Y-%m-%d %H:%M} UTC under PP3: a commitment with no date still "
            "deserves some room, or the system punishes honesty about uncertainty."
        ),
        horizon,
    )


def supersede(
    assessments: list[PromiseAssessment],
) -> list[PromiseAssessment]:
    """Mark all but the latest promise on an invoice as superseded.

    A later reply revising an earlier commitment is the documented `superseded`
    case. Phase 2's corpus carries **one reply per invoice**, so on the committed
    sample this returns its input unchanged and the run honestly reports zero
    supersessions. Implemented anyway, and covered by a unit test rather than by
    the batch: a status the register can hold but the code cannot produce is a
    schema that lies about what it tracks.
    """
    by_invoice: dict[str, list[PromiseAssessment]] = defaultdict(list)
    for assessment in assessments:
        by_invoice[assessment.invoice_id].append(assessment)

    resolved: list[PromiseAssessment] = []
    for invoice_id, group in by_invoice.items():
        ordered = sorted(group, key=lambda a: a.received_at)
        latest = ordered[-1]
        for earlier in ordered[:-1]:
            resolved.append(
                earlier.model_copy(
                    update={
                        "status": PromiseStatus.SUPERSEDED,
                        "suppresses_chase": False,
                        "rule_id": SUPERSEDED_RULE,
                        "rationale": (
                            f"Revised by a later reply on {latest.received_at:%Y-%m-%d} "
                            f"for invoice {invoice_id}. Superseded rather than broken: "
                            "the customer replaced the commitment, they did not "
                            "abandon it."
                        ),
                    }
                )
            )
        resolved.append(latest)
    return sorted(resolved, key=lambda a: (a.invoice_id, a.received_at))


def reliability(assessments: list[PromiseAssessment]) -> dict[str, CustomerReliability]:
    """How often each customer's commitments hold.

    Only **resolved** promises count — kept or broken. An active promise has not
    happened yet and a superseded one was replaced, so counting either would let
    a customer's score move without their behaviour changing. A customer with no
    resolved promise gets `reliability: None`, not a default: no history is not
    the same as bad history, and a default would fabricate the difference.

    This is the engine's one visible piece of learning from behaviour, and it
    feeds prioritisation on the next run.
    """
    tallies: dict[str, dict[str, int]] = defaultdict(lambda: {"made": 0, "kept": 0, "broken": 0})
    for assessment in assessments:
        bucket = tallies[assessment.customer_id]
        bucket["made"] += 1
        if assessment.status is PromiseStatus.KEPT:
            bucket["kept"] += 1
        elif assessment.status is PromiseStatus.BROKEN:
            bucket["broken"] += 1

    out: dict[str, CustomerReliability] = {}
    for customer_id, bucket in tallies.items():
        resolved = bucket["kept"] + bucket["broken"]
        out[customer_id] = CustomerReliability(
            customer_id=customer_id,
            promises_made=bucket["made"],
            kept=bucket["kept"],
            broken=bucket["broken"],
            reliability=round(bucket["kept"] / resolved, 4) if resolved else None,
        )
    return dict(sorted(out.items()))


def _hours(config: ReceivablesConfig) -> int:
    return int(config.promises.grace.total_seconds() // 3600)


__all__ = [
    "ACTIVE_DATED_RULE",
    "ACTIVE_UNDATEABLE_RULE",
    "BROKEN_RULE",
    "CONDITIONAL_RULE",
    "KEPT_RULE",
    "SUPERSEDED_RULE",
    "assess",
    "reliability",
    "supersede",
]
