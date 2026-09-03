"""Reasoning tasks that belong to the shared core.

Engine-specific tasks — root-cause diagnosis, dunning drafting, promise
extraction — are registered by their own engines in their own phases. Only one
task is genuinely shared, and it is shared because `decline-taxonomy` routes to
it from three places: an unrecognised decline reason has to be classified before
any engine can decide anything about it.

Every task here follows the same contract: a versioned prompt file, a Pydantic
response schema, and a **registered deterministic fallback**. There is no fourth
option and no task without a fallback.
"""

from __future__ import annotations

from typing import Any

from app.models.enums import Action, DeclineClass, Engine, EntityType, Outcome
from app.services.audit_trail import AuditTrail
from app.services.llm_agent import (
    REGISTRY,
    FallbackResult,
    ReasoningResult,
    ReasoningTask,
    StructuredOutput,
    TaskRegistry,
)

TASK_CLASSIFY_UNKNOWN_DECLINE = "classify_unknown_decline"

#: Below this, the taxonomy's fail-safe applies and the decline is treated as
#: HARD. From `decline-taxonomy` -> "Handling unknown reasons": *if confidence is
#: low, treat as HARD.* The number itself is the Vasooli reading of "low".
MIN_CLASSIFICATION_CONFIDENCE = 0.6


class DeclineClassification(StructuredOutput):
    """What the model is allowed to say about an unrecognised decline.

    Note what is absent: no action, no retry count, no schedule. The model
    classifies; `policy_engine.py` decides what that classification permits.
    """

    decline_class: DeclineClass


def _classify_unknown_decline_fallback(context: dict[str, Any]) -> FallbackResult:
    """Treat an unclassifiable decline as HARD.

    `decline-taxonomy` is explicit that an unrecognised reason must never
    default to SOFT: defaulting to soft means defaulting to "keep charging this
    customer", which is the exact failure the taxonomy exists to prevent.

    This is the "rule-based approximation available, bias toward the safe side"
    shape of fallback — not an abstention, because a safe and accurate default
    genuinely exists here. Stopping is always a defensible answer.
    """
    raw = context.get("raw_reason", "unknown")
    return FallbackResult(
        output=DeclineClassification(
            decline_class=DeclineClass.HARD,
            confidence=0.0,
            reasoning=(
                f"Reason {raw!r} could not be classified by the reasoning layer. "
                "Treated as HARD under the taxonomy's fail-safe: the safe direction "
                "of error is to stop, never to keep retrying."
            ),
        ),
        rationale="unknown decline treated as HARD (decline-taxonomy fail-safe)",
        confidence=0.0,
    )


def register_core_tasks(registry: TaskRegistry = REGISTRY) -> None:
    """Register the shared core's reasoning tasks.

    Idempotent, so importing this from both the app lifespan and a test fixture
    is safe.
    """
    registry.register(
        ReasoningTask(
            name=TASK_CLASSIFY_UNKNOWN_DECLINE,
            prompt_name=TASK_CLASSIFY_UNKNOWN_DECLINE,
            response_model=DeclineClassification,
            fallback=_classify_unknown_decline_fallback,
            description=(
                "Classify a decline reason the taxonomy does not recognise. "
                "Cited by policy_engine, Engine 1 and Engine 2."
            ),
            min_confidence=MIN_CLASSIFICATION_CONFIDENCE,
        )
    )


def record_reasoning(
    trail: AuditTrail,
    result: ReasoningResult,
    *,
    batch_id: str,
    engine: Engine,
    entity_type: EntityType,
    entity_id: str,
    action: Action,
    reason_code: str,
    authorising_rule: str,
    fallback_rule: str | None = None,
    amount_at_risk_paise: int = 0,
) -> Any:
    """Write a reasoning result to the audit trail with its provenance intact.

    One helper rather than a hand-rolled `record()` call at each site, so a
    reasoning result cannot be logged without saying whether it was reasoned or
    ruled — and so a degradation is never silently dropped.

    An abstention is recorded as an escalation, per `audit-schema`: it is a
    *successful* outcome for a task where guessing would be worse, and the trail
    should show it as deliberate rather than as a failure.

    `fallback_rule` is the rule a degraded result cites instead — for the
    unknown-decline task that is the taxonomy's fail-safe. A caller that omits it
    keeps citing the same rule, which stays truthful because the fallback still
    operated under the bound that authorised the attempt.
    """
    rationale = getattr(result.output, "reasoning", None) or (
        result.degradation_reason or "no reasoning recorded"
    )
    if result.degraded and result.degradation_reason:
        rationale = f"{rationale} [degraded: {result.degradation_reason}]"

    return trail.record(
        batch_id=batch_id,
        engine=engine,
        entity_type=entity_type,
        entity_id=entity_id,
        action=Action.ESCALATE if result.abstained else action,
        outcome=Outcome.ESCALATED if result.abstained else Outcome.SUCCESS,
        reason_code=reason_code,
        authorising_rule=(fallback_rule or authorising_rule) if result.degraded else authorising_rule,
        rationale=rationale,
        provenance=result.provenance,
        model_confidence=result.confidence,
        amount_at_risk_paise=amount_at_risk_paise,
        metadata={"task": result.task, "degraded": result.degraded},
    )
