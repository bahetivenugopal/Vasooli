"""Root-cause diagnosis — the one place in Engine 1 where a model earns its place.

The detector says *that* a corridor degraded. This layer says *why*, and makes
the judgment the whole engine turns on: **systemic or individual?** The same
decline code appears in both cases and demands opposite responses, which is
exactly the shape of problem a rule table handles badly and a reasoning layer
handles well.

Three things keep it honest:

1. **Structured output only.** The model picks from enumerated hypotheses,
   determinations and recommended actions. It never writes an action string, and
   it never sets a bound.
2. **Abstention is first-class** (`corridor-detection:DX2`). `insufficient_evidence`
   is one of the three determinations, not an error path, and it routes the
   corridor to a human under `policy-bounds:HE1`.
3. **A registered deterministic fallback** that classifies from the decline
   distribution alone and biases toward escalation. Under degraded reasoning,
   doing less is the correct behaviour rather than a limitation.

The fallback fires on every path the `llm-provider` skill lists: no provider, a
rate limit beyond backoff, unparseable output twice, deterministic-only mode, or
a confidence below `corridor-detection:DX1`.
"""

from __future__ import annotations

from typing import Any

from app.engines.root_cause.config import DiagnosisConfig, default_config
from app.engines.root_cause.detection import unaffected_corridor_rates
from app.engines.root_cause.schemas import (
    CorridorDiagnosis,
    Detection,
    PaymentAttempt,
    RecommendedAction,
    RootCauseHypothesis,
)
from app.models.enums import CorridorDetermination
from app.services.llm_agent import (
    REGISTRY,
    FallbackResult,
    ReasoningTask,
    StructuredOutput,
    TaskRegistry,
)

TASK_DIAGNOSE_CORRIDOR = "diagnose_corridor"

#: Rail-side reasons. A concentration of these on one corridor is the signature
#: of a degraded rail — from `decline-taxonomy`, where `ISSUER_UNAVAILABLE` is
#: explicitly called out as "the classic corridor-wide failure".
SYSTEMIC_REASONS: frozenset[str] = frozenset(
    {
        "ISSUER_UNAVAILABLE",
        "GATEWAY_TIMEOUT",
        "NETWORK_ERROR",
        "TECHNICAL_DECLINE",
    }
)

#: Customer- or instrument-side reasons. Many of these across many distinct
#: customers is a coincidence of individual failures, not an outage.
INDIVIDUAL_REASONS: frozenset[str] = frozenset(
    {
        "INSUFFICIENT_FUNDS",
        "TRANSACTION_LIMIT_EXCEEDED",
        "CARD_EXPIRED",
        "ACCOUNT_CLOSED",
        "INVALID_ACCOUNT",
        "CARD_DISABLED_ONLINE",
        "CARD_LOST_OR_STOLEN",
        "INTERNATIONAL_NOT_ALLOWED",
        "AUTH_TIMEOUT",
        "CUSTOMER_CANCELLED",
    }
)

#: The citation a diagnosis carries when the deterministic classifier produced it.
FALLBACK_RULE = "corridor-detection:DX1"
#: The citation a diagnosis carries when the model produced it.
DIAGNOSIS_RULE = "corridor-detection:DX2"


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def build_context(
    detection: Detection,
    attempts: list[PaymentAttempt],
    level_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Everything the prompt needs, rendered as plain strings.

    Built here rather than inside the agent so the *same* context object feeds
    both the prompt and the deterministic fallback. If the two saw different
    evidence, a fallback result and a model result would not be comparable, and
    the per-source metric split would be meaningless.
    """
    peers = unaffected_corridor_rates(attempts, detection, level_fields)
    peer_lines = (
        "\n".join(
            f"  - {row['corridor']}: {row['success_rate']:.2%} over {row['attempts']} attempts"
            for row in peers[:8]
        )
        or "  (none with traffic in this window)"
    )
    mix_lines = (
        "\n".join(
            f"  - {code}: {count}"
            for code, count in sorted(
                detection.decline_mix.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )
        or "  (no failures recorded in window)"
    )
    return {
        "detection_id": detection.detection_id,
        "corridor_label": detection.corridor.label,
        "corridor_level": detection.corridor.level,
        "window_start": detection.window_start.strftime("%Y-%m-%d %H:%M"),
        "window_end": detection.window_end.strftime("%Y-%m-%d %H:%M"),
        "window_attempts": detection.window_attempts,
        "observed_success_rate": f"{detection.observed_success_rate:.2%}",
        "baseline_success_rate": f"{detection.baseline_success_rate:.2%}",
        "baseline_attempts": detection.baseline_attempts,
        "affected_attempts": detection.affected_attempts,
        "distinct_failing_customers": detection.distinct_failing_customers,
        "value_at_risk_rupees": _rupees(detection.value_at_risk_paise),
        "statistical_confidence": f"{detection.confidence:.4f}",
        "decline_mix": mix_lines,
        "peer_corridors": peer_lines,
        # Not rendered into the prompt — carried so the fallback reasons over the
        # same numbers the model saw.
        "_decline_mix_raw": dict(detection.decline_mix),
        "_affected_attempts": detection.affected_attempts,
        "_distinct_failing_customers": detection.distinct_failing_customers,
        "_peer_rates": [row["success_rate"] for row in peers],
    }


def classify_deterministically(
    decline_mix: dict[str, int],
    *,
    affected_attempts: int,
    distinct_failing_customers: int,
    config: DiagnosisConfig,
) -> CorridorDiagnosis:
    """The registered fallback's logic, exposed for direct testing.

    Exactly the shape the `llm-provider` skill specifies for this task:
    concentrated on rail-side reasons -> systemic; spread across distinct
    customers with fund/account reasons -> individual; anything else ->
    insufficient evidence and escalate.

    The bias is deliberate. Under degraded reasoning the system should do less,
    and abstaining costs one human glance where a wrong systemic call reroutes
    live traffic.
    """
    total = sum(decline_mix.values())
    if total <= 0:
        return CorridorDiagnosis(
            hypothesis=RootCauseHypothesis.INSUFFICIENT_EVIDENCE,
            determination=CorridorDetermination.INSUFFICIENT_EVIDENCE,
            recommended_action=RecommendedAction.ESCALATE,
            confidence=0.0,
            reasoning=(
                "No decline reasons were recorded inside the window, so there is "
                "nothing to classify. Escalating rather than guessing."
            ),
        )

    systemic = sum(count for code, count in decline_mix.items() if code in SYSTEMIC_REASONS)
    individual = sum(
        count for code, count in decline_mix.items() if code in INDIVIDUAL_REASONS
    )
    systemic_share = systemic / total
    individual_share = individual / total
    customer_share = distinct_failing_customers / max(affected_attempts, 1)

    if systemic_share >= config.systemic_reason_share:
        return CorridorDiagnosis(
            hypothesis=RootCauseHypothesis.ISSUER_OUTAGE,
            determination=CorridorDetermination.SYSTEMIC,
            recommended_action=RecommendedAction.REROUTE_TRAFFIC,
            confidence=round(systemic_share, 4),
            reasoning=(
                f"{systemic}/{total} declines in the window are rail-side reasons "
                f"({systemic_share:.0%}), which is the signature of a degraded "
                "corridor rather than a coincidence of customer-side failures. "
                "Classified without a model — the reasoning layer was unavailable."
            ),
        )

    if (
        individual_share >= config.individual_reason_share
        and customer_share >= config.distinct_customer_share
    ):
        return CorridorDiagnosis(
            hypothesis=RootCauseHypothesis.CUSTOMER_SIDE_CONCENTRATION,
            determination=CorridorDetermination.INDIVIDUAL,
            recommended_action=RecommendedAction.SCHEDULE_RETRY,
            confidence=round(min(individual_share, customer_share), 4),
            reasoning=(
                f"{individual}/{total} declines are customer- or instrument-side "
                f"({individual_share:.0%}), spread across "
                f"{distinct_failing_customers} distinct customers over "
                f"{affected_attempts} failures. That is many independent problems, "
                "not one corridor problem. Classified without a model."
            ),
        )

    return CorridorDiagnosis(
        hypothesis=RootCauseHypothesis.INSUFFICIENT_EVIDENCE,
        determination=CorridorDetermination.INSUFFICIENT_EVIDENCE,
        recommended_action=RecommendedAction.ESCALATE,
        confidence=0.0,
        reasoning=(
            f"The decline mix has no clear shape: {systemic_share:.0%} rail-side, "
            f"{individual_share:.0%} customer-side over {total} failures. Neither "
            "reading is supported, so this escalates to human review rather than "
            "guessing. Classified without a model."
        ),
    )


def _diagnose_corridor_fallback(context: dict[str, Any]) -> FallbackResult:
    """The registered deterministic fallback for `diagnose_corridor`.

    Not an abstention wholesale: where the decline distribution genuinely settles
    the question, a rule-based answer is accurate and stopping there would throw
    away real information. Where it does not, this abstains — which is what the
    `llm-provider` skill means by "bias toward escalation under ambiguity".
    """
    config = default_config().diagnosis
    diagnosis = classify_deterministically(
        dict(context.get("_decline_mix_raw") or {}),
        affected_attempts=int(context.get("_affected_attempts") or 0),
        distinct_failing_customers=int(context.get("_distinct_failing_customers") or 0),
        config=config,
    )
    abstained = diagnosis.determination is CorridorDetermination.INSUFFICIENT_EVIDENCE
    return FallbackResult(
        output=diagnosis,
        abstained=abstained,
        rationale=(
            "abstained to human review (corridor-detection:DX2)"
            if abstained
            else f"classified {diagnosis.determination.value} from the decline distribution"
        ),
        confidence=diagnosis.confidence,
    )


def register_root_cause_tasks(registry: TaskRegistry = REGISTRY) -> None:
    """Register Engine 1's reasoning tasks.

    Called from the app lifespan alongside `register_core_tasks()`. Registration
    loads the prompt file eagerly, so a missing or unversioned prompt fails at
    boot rather than mid-batch.
    """
    registry.register(
        ReasoningTask(
            name=TASK_DIAGNOSE_CORRIDOR,
            prompt_name=TASK_DIAGNOSE_CORRIDOR,
            response_model=CorridorDiagnosis,
            fallback=_diagnose_corridor_fallback,
            description=(
                "Diagnose why a payment corridor degraded, and judge whether the "
                "cause is systemic or a coincidence of individual failures."
            ),
            min_confidence=default_config().diagnosis.min_confidence,
        )
    )


__all__ = [
    "DIAGNOSIS_RULE",
    "FALLBACK_RULE",
    "INDIVIDUAL_REASONS",
    "SYSTEMIC_REASONS",
    "TASK_DIAGNOSE_CORRIDOR",
    "StructuredOutput",
    "build_context",
    "classify_deterministically",
    "register_root_cause_tasks",
]
