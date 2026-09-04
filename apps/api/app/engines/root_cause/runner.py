"""The batch runner: detect -> diagnose -> authorize -> act -> audit -> summarize.

One entry point, one honest summary. Every figure in that summary is recomputed
from the audit trail at the end of the run — not accumulated in a counter as the
loop goes, because a counter and the trail it claims to summarise can drift, and
then the headline number and its evidence disagree.

The two numbers the phase brief singles out are both reported plainly:

- **Policy denials.** Actions the reasoning layer recommended and the rules
  refused. A non-zero count is the proof that the gate is real, and it belongs in
  the pitch rather than buried.
- **False positives.** Detections the generator's ground truth does not back.
  Reported with the corridor and the numbers behind them, so a reader can judge
  whether the threshold is set sensibly rather than taking a precision figure on
  trust.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.root_cause.config import RootCauseConfig, default_config
from app.engines.root_cause.dataset import (
    dataset_batch_id,
    load_attempts,
    manifest_for,
    resolve_dataset,
)
from app.engines.root_cause.detection import (
    CorridorDetector,
    alternate_routes,
    corridor_of,
)
from app.engines.root_cause.diagnosis import (
    TASK_DIAGNOSE_CORRIDOR,
    build_context,
    register_root_cause_tasks,
)
from app.engines.root_cause.recovery import (
    CorridorOutcome,
    PaymentOutcome,
    RecoveryService,
)
from app.engines.root_cause.schemas import (
    CorridorDiagnosis,
    Detection,
    DetectionScore,
    PaymentAttempt,
    RunSummary,
)
from app.engines.root_cause.scoring import load_manifest, score_detections
from app.models.audit import AuditEntry
from app.models.enums import (
    Action,
    BatchStatus,
    CorridorDetermination,
    Engine,
    Outcome,
    ProvenanceSource,
)
from app.models.root_cause import CorridorDetection, CorridorReroute
from app.services.audit_trail import AuditTrail
from app.services.llm_agent import REGISTRY, LLMAgent, ReasoningResult
from app.services.policy_engine import PolicyEngine
from app.services.razorpay_client import RazorpayClient

#: Imported lazily inside the runner: `data.generators` lives at the repo root
#: and pulling it in at module import would make `app` unusable without it.
RETRY_MODEL_IMPORT = "data.generators.retry_model"

#: What Engine 1's recovery figure means, and the one thing it deliberately does
#: not claim. A module constant because the cross-engine overview quotes it, and
#: because the reroute caveat is the sort of thing that quietly disappears from a
#: pitch if it only lives in a metrics doc.
RECOVERY_DEFINITION = (
    "amount recovered = the value of failed payments that a per-payment retry, "
    "authorised against that payment's own decline class and budget, went on to "
    "settle. Corridor-level actions contribute nothing to it: a reroute is "
    "authorised, bounded and expiring, but the retry-success model has no route "
    "dimension, so crediting rerouted traffic with an uplift would be inventing "
    "the headline number."
)


@dataclass
class RunArtefacts:
    """Everything one run produced, for tests and the demo script."""

    summary: RunSummary
    detections: list[Detection] = field(default_factory=list)
    corridor_outcomes: list[CorridorOutcome] = field(default_factory=list)
    payment_outcomes: list[PaymentOutcome] = field(default_factory=list)
    reroutes: list[CorridorReroute] = field(default_factory=list)


def _load_retry_model() -> Any:
    from data.generators.retry_model import RetrySuccessModel

    return RetrySuccessModel.load()


def _seeded_rng(seed: int, *labels: str) -> Any:
    from data.generators.common import rng

    return rng(seed, *labels)


class RootCauseRunner:
    """Runs Engine 1 over one payments batch."""

    def __init__(
        self,
        db: Session,
        *,
        config: RootCauseConfig | None = None,
        agent: LLMAgent | None = None,
        policy: PolicyEngine | None = None,
        razorpay: RazorpayClient | None = None,
        retry_model: Any | None = None,
    ) -> None:
        self._db = db
        self._config = config or default_config()
        self._trail = AuditTrail(db)
        self._policy = policy or PolicyEngine()
        self._razorpay = razorpay or RazorpayClient(audit=self._trail)
        self._retry_model = retry_model or _load_retry_model()

        if TASK_DIAGNOSE_CORRIDOR not in REGISTRY.names():
            register_root_cause_tasks()
        self._agent = agent or LLMAgent()

    # --- the loop ----------------------------------------------------------

    def run(
        self,
        *,
        batch_id: str,
        seed: int,
        dataset: Path | str | None = None,
        now: datetime | None = None,
        score: bool = True,
    ) -> RunArtefacts:
        """Ingest, detect, diagnose, act, and summarise.

        `now` defaults to the **last attempt in the dataset**, not to wall-clock
        time. Phase 2's batches are anchored to a fixed `as_of` that is not today,
        and that anchor lives in the manifest, which engine code may not read —
        so the run's clock is derived from the records themselves. Running
        against `datetime.now()` would put every cooldown and every window in the
        wrong week.
        """
        dataset_path = resolve_dataset(dataset)
        attempts = load_attempts(dataset_path)
        source_batch = dataset_batch_id(attempts)
        run_now = (now or attempts[-1].created_at).astimezone(UTC)

        self._trail.start_batch(
            batch_id=batch_id,
            engine=Engine.ROOT_CAUSE,
            seed=seed,
            notes={
                "dataset_batch_id": source_batch,
                "dataset_path": str(dataset_path),
                "now": run_now.isoformat(),
                "detection_config_version": self._config.detection.version,
                "razorpay_mode": self._razorpay.mode.value,
                "llm_provider_enabled": self._agent.provider_enabled,
            },
        )

        detector = CorridorDetector(self._config.detection)
        detections = detector.detect(attempts)

        corridor_outcomes = [
            self._handle_detection(detection, attempts, run_now, batch_id, seed)
            for detection in detections
        ]
        reroutes = [o.reroute for o in corridor_outcomes if o.reroute is not None]

        payment_outcomes = self._recover_payments(
            attempts, reroutes, run_now, batch_id, seed
        )

        detection_score = None
        if score:
            detection_score = self._score(detections, dataset_path)

        summary = self._summarise(
            batch_id=batch_id,
            dataset_batch_id=source_batch,
            seed=seed,
            now=run_now,
            attempts=attempts,
            detections=detections,
            corridor_outcomes=corridor_outcomes,
            detection_score=detection_score,
        )
        run = self._trail.complete_batch(batch_id, status=BatchStatus.COMPLETED)
        # The engine's own summary, persisted beside the dataset id that produced
        # it. Unlike the audit trail's `batch_summary()`, this one cannot be
        # recomputed from the entries alone — the per-class and per-branch splits
        # need the run's outcomes and the dataset behind them — so the dashboard
        # can only show it if the run stores it. The money figures inside it were
        # themselves read off the trail at the end of the run, and
        # `/audit/batches/{id}/summary` recomputes those independently, which is
        # what makes the stored copy checkable rather than merely convenient.
        run.notes = {**(run.notes or {}), "run_summary": summary.model_dump(mode="json")}
        self._db.commit()
        return RunArtefacts(
            summary=summary,
            detections=detections,
            corridor_outcomes=corridor_outcomes,
            payment_outcomes=payment_outcomes,
            reroutes=reroutes,
        )

    # --- corridor level ----------------------------------------------------

    def _handle_detection(
        self,
        detection: Detection,
        attempts: list[PaymentAttempt],
        now: datetime,
        batch_id: str,
        seed: int,
    ) -> CorridorOutcome:
        level = next(
            level
            for level in self._config.detection.levels
            if level.name == detection.corridor.level
        )
        context = build_context(detection, attempts, level.fields)
        result = self._agent.run(TASK_DIAGNOSE_CORRIDOR, context)
        diagnosis = _diagnosis_from(result)

        routes = alternate_routes(attempts, detection, before=detection.window_end)
        recovery = self._recovery(batch_id)
        decision = recovery.authorise_corridor_action(
            detection,
            diagnosis,
            now=now,
            alternate_route_available=bool(routes),
            provenance=result.provenance,
        )
        entry_id = recovery.record_corridor_decision(
            detection, diagnosis, decision, result
        )

        reroute = None
        if decision.allowed and decision.action is Action.REROUTE_TRAFFIC and routes:
            reroute = recovery.open_reroute(
                detection,
                to_route_id=str(routes[0]["route_id"]),
                now=now,
                authorising_rule=decision.rule_id,
            )
            self._trail.resolve_outcome(entry_id, outcome=Outcome.SUCCESS)
        elif decision.allowed:
            # A permitted corridor action that is not a reroute — the model chose
            # to do less. Recorded as skipped: deliberate inaction, not a failure.
            self._trail.resolve_outcome(entry_id, outcome=Outcome.SKIPPED)

        self._persist_detection(detection, diagnosis, decision, result, batch_id)
        return CorridorOutcome(
            detection=detection,
            diagnosis=diagnosis,
            reasoning=result,
            decision=decision,
            audit_entry_id=entry_id,
            reroute=reroute,
        )

    def _persist_detection(
        self,
        detection: Detection,
        diagnosis: CorridorDiagnosis,
        decision: Any,
        result: ReasoningResult,
        batch_id: str,
    ) -> None:
        row = CorridorDetection(
            detection_id=detection.detection_id,
            batch_id=batch_id,
            corridor_key=detection.corridor.key,
            corridor_level=detection.corridor.level,
            issuer=detection.corridor.issuer,
            method=detection.corridor.method,
            route_id=detection.corridor.route_id,
            window_start=detection.window_start,
            window_end=detection.window_end,
            window_attempts=detection.window_attempts,
            baseline_attempts=detection.baseline_attempts,
            observed_success_rate=detection.observed_success_rate,
            baseline_success_rate=detection.baseline_success_rate,
            p_value=detection.p_value,
            confidence=detection.confidence,
            affected_attempts=detection.affected_attempts,
            value_at_risk_paise=detection.value_at_risk_paise,
            decline_mix=detection.decline_mix,
            distinct_failing_customers=detection.distinct_failing_customers,
            hypothesis=diagnosis.hypothesis.value,
            determination=diagnosis.determination.value,
            recommended_action=diagnosis.recommended_action.value,
            diagnosis_reasoning=diagnosis.reasoning,
            model_confidence=result.confidence,
            provenance=result.provenance.model_dump(mode="json"),
            authorised_action=decision.action.value,
            authorising_rule=decision.rule_id,
            policy_allowed=decision.allowed,
            overridden_recommendation=decision.overridden_recommendation,
        )
        self._db.merge(row)
        self._db.commit()

    # --- payment level -----------------------------------------------------

    def _recover_payments(
        self,
        attempts: list[PaymentAttempt],
        reroutes: list[CorridorReroute],
        now: datetime,
        batch_id: str,
        seed: int,
    ) -> list[PaymentOutcome]:
        """Every failed attempt in the batch gets a bounded, authorised decision.

        Not only the ones inside a detection window: an individual soft decline
        is recoverable revenue whether or not its corridor was having a bad hour,
        and restricting recovery to detected corridors would make the recovery
        rate a function of the detector's sensitivity.
        """
        recovery = self._recovery(batch_id)
        outcomes: list[PaymentOutcome] = []
        for attempt in (a for a in attempts if a.failed):
            rerouted_to = self._rerouted_to(attempt, reroutes, now)
            outcomes.append(
                recovery.recover_payment(
                    attempt,
                    now=now,
                    retry_model=self._retry_model,
                    rng=_seeded_rng(seed, "root_cause", "retry", attempt.attempt_id),
                    rerouted_to=rerouted_to,
                )
            )
        return outcomes

    def _rerouted_to(
        self, attempt: PaymentAttempt, reroutes: list[CorridorReroute], now: datetime
    ) -> str | None:
        """The route a retry on this attempt would be sent to, if one is in force.

        Recorded on the entry, and deliberately **not** fed into the retry-success
        model: that model has no route dimension, so applying an uplift for a
        reroute would be inventing the headline number rather than measuring it.
        Stated here, in the docs, and in the run summary.
        """
        for level in self._config.detection.levels:
            corridor = corridor_of(attempt, level)
            if corridor is None:
                continue
            for reroute in reroutes:
                if reroute.corridor_key == corridor.key and reroute.is_active(now):
                    return reroute.to_route_id
        return None

    # --- plumbing ----------------------------------------------------------

    def _recovery(self, batch_id: str) -> RecoveryService:
        return RecoveryService(
            db=self._db,
            trail=self._trail,
            policy=self._policy,
            razorpay=self._razorpay,
            config=self._config.recovery,
            batch_id=batch_id,
        )

    def _score(self, detections: list[Detection], dataset: Path) -> DetectionScore | None:
        manifest_path = manifest_for(dataset)
        if not manifest_path.exists():
            return None
        return score_detections(detections, load_manifest(manifest_path))

    # --- the summary, computed from the trail ------------------------------

    def _summarise(
        self,
        *,
        batch_id: str,
        dataset_batch_id: str,
        seed: int,
        now: datetime,
        attempts: list[PaymentAttempt],
        detections: list[Detection],
        corridor_outcomes: list[CorridorOutcome],
        detection_score: DetectionScore | None,
    ) -> RunSummary:
        entries = list(
            self._db.scalars(select(AuditEntry).where(AuditEntry.batch_id == batch_id))
        )
        base = self._trail.batch_summary(batch_id)

        denials = [e for e in entries if e.outcome in _REFUSAL_OUTCOMES]
        suppressed = [e for e in entries if (e.entry_metadata or {}).get("suppressed_retry")]
        deferred = [e for e in entries if (e.entry_metadata or {}).get("deferred_retry")]
        overridden = [
            e for e in entries if (e.entry_metadata or {}).get("overridden_recommendation")
        ]
        fallbacks = [
            e
            for e in entries
            if (e.entry_metadata or {}).get("degraded")
            and e.provenance.get("source") == ProvenanceSource.DETERMINISTIC.value
        ]
        model_entries = [
            e for e in entries if e.provenance.get("source") == ProvenanceSource.MODEL.value
        ]

        return RunSummary(
            batch_id=batch_id,
            dataset_batch_id=dataset_batch_id,
            seed=seed,
            now=now,
            attempts_ingested=len(attempts),
            failed_attempts=sum(1 for a in attempts if a.failed),
            detections=len(detections),
            diagnoses_by_determination=dict(
                Counter(o.diagnosis.determination.value for o in corridor_outcomes)
            ),
            amount_at_risk_paise=base.amount_at_risk_paise,
            amount_recovered_paise=base.amount_recovered_paise,
            recovery_rate=base.recovery_rate,
            action_counts=base.action_counts,
            outcome_counts=base.outcome_counts,
            retries_suppressed=len(suppressed),
            retries_deferred=len(deferred),
            reroutes_authorised=sum(
                1 for o in corridor_outcomes if o.reroute is not None
            ),
            policy_denials=len(denials),
            denials_by_rule=dict(Counter(e.authorising_rule for e in denials)),
            human_escalations=base.escalated_count,
            llm_fallbacks=len(fallbacks),
            provenance={
                "model_entries": len(model_entries),
                "deterministic_entries": len(entries) - len(model_entries),
                "cache_hits": sum(
                    1 for e in model_entries if e.provenance.get("cache_hit")
                ),
                "abstentions": base.abstained_count,
                "overridden_recommendations": len(overridden),
                "policy_violations": base.policy_violations,
                "by_source": {k: v.model_dump() for k, v in base.by_source.items()},
            },
            detection_score=detection_score,
        )


#: The outcomes that mean "the system refused". Held here rather than inline so
#: the summary and `/audit-check` cannot drift apart on what counts as a refusal.
_REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {Outcome.BLOCKED.value, Outcome.HALTED.value, Outcome.ESCALATED.value}
)


def _diagnosis_from(result: ReasoningResult) -> CorridorDiagnosis:
    """Pull the diagnosis out of a reasoning result, failing closed.

    A result with no output at all — which the wrapper's contract does not
    produce, but which a mis-registered fallback could — abstains rather than
    inventing a determination.
    """
    output = result.output
    if isinstance(output, CorridorDiagnosis):
        return output
    from app.engines.root_cause.schemas import RecommendedAction, RootCauseHypothesis

    return CorridorDiagnosis(
        hypothesis=RootCauseHypothesis.INSUFFICIENT_EVIDENCE,
        determination=CorridorDetermination.INSUFFICIENT_EVIDENCE,
        recommended_action=RecommendedAction.ESCALATE,
        confidence=0.0,
        reasoning=(
            "The reasoning layer returned no usable diagnosis "
            f"({result.degradation_reason or 'no reason recorded'}). Escalating."
        ),
    )
