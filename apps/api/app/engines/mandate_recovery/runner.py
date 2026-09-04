"""The batch runner: classify -> plan -> authorize -> act -> audit -> summarize.

One entry point, one honest summary. Every figure in that summary is recomputed
from the audit trail at the end of the run, never accumulated in a counter as the
loop goes — a counter and the trail it claims to summarise can drift, and then
the headline number and its evidence disagree.

The numbers §5.4 singles out are all reported plainly, and two of them are the
ones worth arguing about:

- **Recovery by failure class.** Soft declines recover at a completely different
  rate from hard ones, and a single blended rate hides the fact that makes the
  engine worth building.
- **Retries suppressed, and what blind retrying would have cost.** Reported with
  its baseline stated inline, because "attempts avoided" is a counterfactual and
  a counterfactual with a hidden assumption is a marketing number.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.mandate_recovery.classification import classify_failure
from app.engines.mandate_recovery.config import MandateConfig, default_config
from app.engines.mandate_recovery.dataset import (
    dataset_anchor,
    dataset_batch_id,
    load_mandates,
    resolve_dataset,
)
from app.engines.mandate_recovery.dunning import TASK_DRAFT_DUNNING, register_mandate_tasks
from app.engines.mandate_recovery.recovery import (
    MESSAGE_STEPS,
    MandateOutcome,
    MandateRecoveryService,
    afa_side,
)
from app.engines.mandate_recovery.scheduler import RetryScheduler, ScheduleDecision
from app.engines.mandate_recovery.schemas import (
    AfaBranchBreakdown,
    FailureClassBreakdown,
    FailureRoute,
    Mandate,
    NextStep,
    RunSummary,
)
from app.models.audit import AuditEntry
from app.models.enums import Action, BatchStatus, Engine, Outcome, ProvenanceSource
from app.services.audit_trail import AuditTrail
from app.services.llm_agent import REGISTRY, LLMAgent
from app.services.policy_engine import PolicyEngine
from app.services.razorpay_client import RazorpayClient


@dataclass
class RunArtefacts:
    """Everything one run produced, for tests and the demo script."""

    summary: RunSummary
    outcomes: list[MandateOutcome] = field(default_factory=list)
    schedules: list[ScheduleDecision] = field(default_factory=list)


def _load_retry_model() -> Any:
    from data.generators.retry_model import RetrySuccessModel

    return RetrySuccessModel.load()


def _seeded_rng(seed: int, *labels: str) -> Any:
    from data.generators.common import rng

    return rng(seed, *labels)


class MandateRunner:
    """Runs Engine 2 over one mandate book."""

    def __init__(
        self,
        db: Session,
        *,
        config: MandateConfig | None = None,
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

        if TASK_DRAFT_DUNNING not in REGISTRY.names():
            register_mandate_tasks()
        self._agent = agent or LLMAgent()
        self._scheduler = RetryScheduler(policy=self._policy, config=self._config)

    # --- the loop ----------------------------------------------------------

    def run(
        self,
        *,
        batch_id: str,
        seed: int,
        dataset: Path | str | None = None,
        now: datetime | None = None,
    ) -> RunArtefacts:
        """Ingest, classify, schedule, act, and summarise.

        `now` defaults to the **latest debit actually attempted** in the book, not
        to wall-clock time and not to the latest timestamp in the file. Phase 2's
        mandate batch is anchored to a fixed `as_of` that is not today, and every
        mandate's `next_debit_at` sits in the future relative to it — so the
        latest timestamp in the file is up to 56 hours past the moment the book
        describes, and running against it makes every pre-debit notice look stale
        for the wrong reason. See `dataset.dataset_anchor`.
        """
        dataset_path = resolve_dataset(dataset)
        mandates = load_mandates(dataset_path)
        source_batch = dataset_batch_id(mandates)
        run_now = (now or dataset_anchor(mandates)).astimezone(UTC)

        self._trail.start_batch(
            batch_id=batch_id,
            engine=Engine.MANDATE_RECOVERY,
            seed=seed,
            notes={
                "dataset_batch_id": source_batch,
                "dataset_path": str(dataset_path),
                "now": run_now.isoformat(),
                "config_version": self._config.version,
                "razorpay_mode": self._razorpay.mode.value,
                "llm_provider_enabled": self._agent.provider_enabled,
            },
        )

        recovery = MandateRecoveryService(
            db=self._db,
            trail=self._trail,
            policy=self._policy,
            razorpay=self._razorpay,
            agent=self._agent,
            batch_id=batch_id,
        )

        outcomes: list[MandateOutcome] = []
        schedules: list[ScheduleDecision] = []
        for mandate in mandates:
            schedule = self._plan(mandate, run_now)
            schedules.append(schedule)
            outcomes.append(self._act(recovery, schedule, seed))

        summary = self._summarise(
            batch_id=batch_id,
            dataset_batch_id=source_batch,
            seed=seed,
            now=run_now,
            mandates=mandates,
            outcomes=outcomes,
        )
        self._trail.complete_batch(batch_id, status=BatchStatus.COMPLETED)
        return RunArtefacts(summary=summary, outcomes=outcomes, schedules=schedules)

    # --- one mandate -------------------------------------------------------

    def _plan(self, mandate: Mandate, now: datetime) -> ScheduleDecision:
        failure = mandate.latest_failure
        classification = classify_failure(
            failure.failure_reason_code if failure else None,
            agent=self._agent,
            context={"entity": "mandate", "mandate_id": mandate.mandate_id},
        )
        return self._scheduler.plan(mandate, classification, now=now)

    def _act(
        self, recovery: MandateRecoveryService, schedule: ScheduleDecision, seed: int
    ) -> MandateOutcome:
        """Record the gate's decision, then carry out whatever it permitted."""
        entry_id = recovery.record_decision(schedule)
        outcome = MandateOutcome(schedule=schedule, decision_entry_id=entry_id)
        step = schedule.explanation.step

        if step is NextStep.ATTEMPT_DEBIT:
            recovered, probability = recovery.execute_debit(
                schedule,
                entry_id,
                retry_model=self._retry_model,
                rng=_seeded_rng(
                    seed, "mandate_recovery", "debit", schedule.mandate.mandate_id
                ),
            )
            outcome.debit_attempted = True
            outcome.recovered_paise = recovered
            outcome.retry_probability = probability
        elif step is NextStep.SEND_PRE_DEBIT_NOTICE:
            outcome.communication = recovery.send_notice(schedule)
        elif step in MESSAGE_STEPS:
            drafted = recovery.draft_communication(schedule)
            if drafted is not None:
                outcome.communication = drafted.communication
                outcome.reasoning = drafted.reasoning
                outcome.tone_violations = drafted.tone_violations

        recovery.persist_state(outcome)
        return outcome

    # --- the summary, computed from the trail ------------------------------

    def _summarise(
        self,
        *,
        batch_id: str,
        dataset_batch_id: str,
        seed: int,
        now: datetime,
        mandates: list[Mandate],
        outcomes: list[MandateOutcome],
    ) -> RunSummary:
        entries = list(
            self._db.scalars(select(AuditEntry).where(AuditEntry.batch_id == batch_id))
        )
        base = self._trail.batch_summary(batch_id)
        meta = [(e, e.entry_metadata or {}) for e in entries]

        denials = [e for e in entries if e.outcome in _REFUSAL_OUTCOMES]
        suppressed = [e for e, m in meta if m.get("suppressed_retry")]
        compliance = [e for e, m in meta if m.get("compliance_blocked")]
        overridden = [e for e, m in meta if m.get("overridden_recommendation")]
        fallbacks = [
            e
            for e, m in meta
            if m.get("degraded") and e.provenance.get("source") == ProvenanceSource.DETERMINISTIC.value
        ]
        model_entries = [
            e for e in entries if e.provenance.get("source") == ProvenanceSource.MODEL.value
        ]
        notices = [(e, m) for e, m in meta if e.action == Action.SEND_PRE_DEBIT_NOTICE.value]
        messages = [
            (e, m)
            for e, m in meta
            if e.action == Action.SEND_DUNNING.value or m.get("tone_violations") is not None
        ]

        at_risk_outcomes = [
            o for o in outcomes if o.schedule.classification.route is not FailureRoute.NONE
        ]
        addressable = sum(
            o.schedule.mandate.amount_paise
            for o in at_risk_outcomes
            if o.schedule.explanation.step in _DEBITABLE_STEPS
        )

        return RunSummary(
            batch_id=batch_id,
            dataset_batch_id=dataset_batch_id,
            seed=seed,
            now=now,
            mandates_ingested=len(mandates),
            mandates_with_failed_cycle=len(at_risk_outcomes),
            amount_at_risk_paise=base.amount_at_risk_paise,
            amount_recovered_paise=base.amount_recovered_paise,
            recovery_rate=base.recovery_rate,
            amount_addressable_paise=addressable,
            addressable_recovery_rate=(
                round(base.amount_recovered_paise / addressable, 4) if addressable else 0.0
            ),
            addressable_definition=(
                "amount at risk on mandates whose next step the policy engine "
                "permitted to be a debit this cycle (attempted, or deferred on "
                "spacing). Excludes every mandate a compliance gate, a hard "
                "decline or a hard stop refused — money no compliant system may "
                "collect automatically. Reported beside the headline rate, not "
                "instead of it."
            ),
            by_failure_class=self._by_failure_class(outcomes),
            by_route=dict(
                Counter(o.schedule.classification.route.value for o in outcomes)
            ),
            afa_branch=self._by_afa_branch(outcomes),
            debits_attempted=sum(1 for o in outcomes if o.debit_attempted),
            debits_recovered=sum(1 for o in outcomes if o.recovered_paise > 0),
            retries_suppressed=len(suppressed),
            wasted_attempts_avoided=self._wasted_attempts(outcomes),
            wasted_attempts_baseline=(
                f"a system with no soft/hard split spends the full "
                f"{self._config.blind_retry_attempts}-attempt per-cycle budget "
                f"(rbi-mandate-rules:PartB.max_retries) on every failure; this counts "
                f"the attempts left unspent on mandates where a retry was refused "
                f"permanently. An estimate, not a measurement."
            ),
            compliance_blocked=len(compliance),
            compliance_blocked_by_rule=dict(Counter(e.authorising_rule for e in compliance)),
            notices_sent=sum(1 for e, _ in notices if e.outcome == Outcome.SCHEDULED.value),
            notices_scheduled=sum(
                1
                for o in outcomes
                if o.schedule.explanation.step is NextStep.SEND_PRE_DEBIT_NOTICE
            ),
            notices_held=sum(1 for e, _ in notices if e.outcome == Outcome.BLOCKED.value),
            notice_status_counts=dict(
                Counter(o.schedule.notice.status.value for o in outcomes)
            ),
            communications_drafted=len(messages),
            communications_held=sum(
                1 for _, m in messages if m.get("status") in {"held", "suppressed"}
            ),
            tone_rejections=sum(1 for _, m in messages if m.get("tone_violations")),
            mandates_at_attempt_cap=sum(
                1
                for o in outcomes
                if o.schedule.decision.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
            ),
            mandates_halted=base.halted_count,
            human_escalations=base.escalated_count,
            action_counts=base.action_counts,
            outcome_counts=base.outcome_counts,
            policy_denials=len(denials),
            denials_by_rule=dict(Counter(e.authorising_rule for e in denials)),
            llm_fallbacks=len(fallbacks),
            unknown_declines_classified=sum(
                1 for o in outcomes if o.schedule.classification.reasoned
            ),
            provenance={
                "model_entries": len(model_entries),
                "deterministic_entries": len(entries) - len(model_entries),
                "cache_hits": sum(1 for e in model_entries if e.provenance.get("cache_hit")),
                "abstentions": base.abstained_count,
                "overridden_recommendations": len(overridden),
                "policy_violations": base.policy_violations,
                "by_source": {k: v.model_dump() for k, v in base.by_source.items()},
            },
        )

    def _by_failure_class(
        self, outcomes: list[MandateOutcome]
    ) -> dict[str, FailureClassBreakdown]:
        """Recovery per decline class. Where the story lives.

        Computed from the outcomes rather than from the trail because the class is
        a property of the mandate, not of any one entry — but every money figure
        in it comes off the same entries the batch summary sums, so the parts add
        up to the whole.
        """
        buckets: dict[str, FailureClassBreakdown] = {}
        for outcome in outcomes:
            classification = outcome.schedule.classification
            if classification.route is FailureRoute.NONE:
                continue
            bucket = buckets.setdefault(classification.decline_class, FailureClassBreakdown())
            bucket.mandates += 1
            bucket.amount_at_risk_paise += outcome.schedule.mandate.amount_paise
            bucket.amount_recovered_paise += outcome.recovered_paise
            bucket.debits_attempted += int(outcome.debit_attempted)
            bucket.debits_recovered += int(outcome.recovered_paise > 0)
            bucket.retries_suppressed += int(outcome.schedule.suppressed)
        for bucket in buckets.values():
            bucket.recovery_rate = (
                round(bucket.amount_recovered_paise / bucket.amount_at_risk_paise, 4)
                if bucket.amount_at_risk_paise
                else 0.0
            )
        return dict(sorted(buckets.items()))

    def _by_afa_branch(self, outcomes: list[MandateOutcome]) -> dict[str, AfaBranchBreakdown]:
        """Both sides of the Rs 15,000 threshold, so the branch is visible."""
        buckets = {"at_or_below": AfaBranchBreakdown(), "above": AfaBranchBreakdown()}
        for outcome in outcomes:
            bucket = buckets[afa_side(outcome.schedule.mandate)]
            bucket.mandates += 1
            bucket.debits_attempted += int(outcome.debit_attempted)
            bucket.amount_recovered_paise += outcome.recovered_paise
            if outcome.schedule.classification.route is not FailureRoute.NONE:
                bucket.amount_at_risk_paise += outcome.schedule.mandate.amount_paise
            if outcome.schedule.explanation.step is NextStep.REQUEST_AUTHENTICATION:
                bucket.authentication_requests += 1
            if outcome.schedule.decision.rule_id in {
                "rbi-mandate-rules:A3",
                "rbi-mandate-rules:A5",
            }:
                bucket.blocked_for_fresh_afa += 1
        return buckets

    def _wasted_attempts(self, outcomes: list[MandateOutcome]) -> int:
        """Attempts a blind retrier would have spent and this engine did not.

        The counterfactual is stated on the summary beside the number: a system
        with no soft/hard split spends the full Part B per-cycle budget on every
        failure. For each mandate whose retry was refused *permanently*, this
        counts what was left of that budget.

        Deliberately conservative in two ways. Deferred retries are excluded —
        the revenue is still recoverable there, only the timing was wrong — and
        the baseline is the compliant 3-attempt cap rather than the unbounded
        hammering a system without stopping rules would actually do.
        """
        budget = self._config.blind_retry_attempts
        return sum(
            max(budget - o.schedule.mandate.attempts_in_current_cycle, 0)
            for o in outcomes
            if o.schedule.suppressed
        )


#: Steps that mean the engine was allowed to charge this cycle. The denominator
#: of `addressable_recovery_rate`, kept here so the summary and the tests cannot
#: disagree about what "the engine could act on this" means.
_DEBITABLE_STEPS: frozenset[NextStep] = frozenset({NextStep.ATTEMPT_DEBIT, NextStep.DEFER})


#: The outcomes that mean "the system refused". Held here rather than inline so
#: the summary and `/audit-check` cannot drift on what counts as a refusal.
_REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {Outcome.BLOCKED.value, Outcome.HALTED.value, Outcome.ESCALATED.value}
)
