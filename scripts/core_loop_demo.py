"""The core loop of the entire product, end to end, in one file.

    python scripts/core_loop_demo.py

It does four things:

1. Opens a batch run with a seed.
2. Asks the policy engine for a decision on an entity that has spent its budget.
3. Gets refused, by a named rule, with a reason.
4. Writes the refusal to the audit trail and reads it back.

Then it does the same thing again with a model recommendation attached, to show
that a recommendation cannot widen a bound.

If this works, everything downstream is assembly: the three engines differ only
in what data they ingest and which bounded actions they may call.

Writes to the configured database, so the entries it produces can then be read
through the API at `/api/v1/audit/batches/<batch_id>/summary`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Run from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import AuditEntry, BatchRun  # noqa: F401 - registers tables
from app.models.entity import RecoverableEntity
from app.models.enums import Action, Engine, EntityType
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail
from app.services.decline_taxonomy import DeclineCode
from app.services.policy_engine import (
    HARD_ATTEMPT_CEILING,
    LLMRecommendation,
    PolicyEngine,
    PolicyRequest,
)

SEED = 42


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def show_decision(label: str, decision) -> None:
    print(f"\n  {label}")
    print(f"    allowed          : {decision.allowed}")
    print(f"    action           : {decision.action.value}")
    print(f"    authorising rule : {decision.rule_id}")
    print(f"    reason code      : {decision.reason_code}")
    print(f"    rationale        : {decision.rationale}")
    if decision.overridden_recommendation:
        print(f"    model wanted     : {decision.overridden_recommendation}  <- overruled")


def main() -> int:
    Base.metadata.create_all(bind=engine)
    now = datetime.now(UTC)
    batch_id = f"demo_{now:%Y%m%d_%H%M%S}"

    db = SessionLocal()
    trail = AuditTrail(db)
    policy = PolicyEngine()

    print("=" * 74)
    print("Vasooli — shared core loop")
    print("=" * 74)

    run = trail.start_batch(batch_id=batch_id, engine=Engine.MANDATE_RECOVERY, seed=SEED)
    print(f"\n1. Batch opened: {run.batch_id}  (engine={run.engine}, seed={run.seed})")

    # An entity that has already used every attempt it is entitled to.
    entity = RecoverableEntity(
        entity_id="sub_demo_0001",
        entity_type=EntityType.MANDATE,
        amount_paise=1_299_00,  # Rs 1,299.00
        attempt_count=HARD_ATTEMPT_CEILING,
        last_attempt_at=now - timedelta(days=2),
    )
    request = PolicyRequest(
        engine=Engine.MANDATE_RECOVERY,
        entity=entity,
        proposed_action=Action.ATTEMPT_CHARGE,
        now=now,
        decline_code=DeclineCode.INSUFFICIENT_FUNDS,
    )
    print(
        f"\n2. Asking the policy engine about {entity.entity_id} "
        f"({rupees(entity.amount_paise)}, {entity.attempt_count} attempts already made)"
    )

    decision = policy.evaluate(request)
    show_decision("3. Policy engine says:", decision)

    entry = trail.record(
        batch_id=batch_id,
        engine=Engine.MANDATE_RECOVERY,
        entity_type=EntityType.MANDATE,
        entity_id=entity.entity_id,
        action=decision.action,
        outcome=decision.outcome,
        reason_code=decision.reason_code,
        authorising_rule=decision.rule_id,
        rationale=decision.rationale,
        provenance=Provenance.deterministic(),
        amount_at_risk_paise=entity.amount_paise,
        attempts_remaining=decision.attempts_remaining,
    )

    stored = trail.read(trail.get(entry.id))
    print("\n4. Audit entry written and read back:")
    print(f"    id               : {stored.id}")
    print(f"    timestamp        : {stored.timestamp.isoformat()}")
    print(f"    action / outcome : {stored.action.value} / {stored.outcome.value}")
    print(f"    authorising rule : {stored.authorising_rule}")
    print(f"    reasoning        : {stored.rationale}")
    print(f"    provenance       : source={stored.provenance.source.value}")
    print(f"    amount at risk   : {rupees(stored.amount_at_risk_paise)}")

    # The boundary, made visible: a model recommends the thing the rules forbid.
    print("\n5. Now with the reasoning layer recommending a retry anyway:")
    recommendation = LLMRecommendation(
        action=Action.ATTEMPT_CHARGE,
        rationale="The customer's balance usually recovers after payday; one more try.",
        confidence=0.88,
        provenance=Provenance.from_model(
            provider="gemini", model="gemini-3.1-flash-lite", prompt_version="v1"
        ),
    )
    authorized = policy.authorize(request, recommendation)
    show_decision("   Policy engine still says:", authorized)

    trail.record(
        batch_id=batch_id,
        engine=Engine.MANDATE_RECOVERY,
        entity_type=EntityType.MANDATE,
        entity_id=entity.entity_id,
        action=authorized.action,
        outcome=authorized.outcome,
        reason_code=authorized.reason_code,
        authorising_rule=authorized.rule_id,
        rationale=authorized.rationale,
        provenance=recommendation.provenance,
        model_confidence=recommendation.confidence,
        amount_at_risk_paise=entity.amount_paise,
        metadata={"overridden_recommendation": authorized.overridden_recommendation},
    )

    completed = trail.complete_batch(batch_id)
    summary = trail.batch_summary(batch_id)
    print("\n6. Batch summary, computed from the log:")
    print(f"    entries          : {summary.entries}")
    print(f"    at risk          : {rupees(summary.amount_at_risk_paise)}")
    print(f"    recovered        : {rupees(summary.amount_recovered_paise)}")
    print(f"    recovery rate    : {summary.recovery_rate:.0%}")
    print(f"    escalated        : {summary.escalated_count}")
    print(f"    policy violations: {summary.policy_violations}")
    for source, breakdown in summary.by_source.items():
        print(f"    by source        : {source:<14} {breakdown.entries} entries")

    print(f"\n   Batch {completed.batch_id} closed as {completed.status}.")
    print(f"   Read it back at: /api/v1/audit/batches/{batch_id}/summary")
    print("=" * 74)

    db.close()
    return 0 if not authorized.allowed and summary.policy_violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
