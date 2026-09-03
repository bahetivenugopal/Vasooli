"""Engine 1 end to end, with the numbers printed honestly.

    python scripts/root_cause_demo.py
    python scripts/root_cause_demo.py --deterministic     # force every fallback
    python scripts/root_cause_demo.py --seed 7

It runs the whole loop over a payments batch — detect, diagnose, authorise, act,
audit — and prints the summary in the shape `/run-batch-demo` specifies, plus the
two things that phase brief singles out:

- **Detection performance against ground truth**, false positives included, with
  the corridor and the numbers behind each one. A precision figure with the
  misses hidden is not a finding, it is a claim.
- **Policy denials**, broken down by the rule that refused. A non-zero count is
  the evidence that the gate is real.

Writes to the configured database, so everything it prints can then be read back
through `/api/v1/root-cause/runs/<batch_id>` and `/api/v1/audit/...`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT))

# The Windows console defaults to cp1252, which mangles every en/em dash in a
# rationale into a replacement character. This output is a demo artefact someone
# reads, so it is worth one line to make it render.
if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - console plumbing
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings, settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.engines.root_cause import RootCauseRunner, default_config  # noqa: E402
from app.engines.root_cause.schemas import RunSummary  # noqa: E402
from app.models import (  # noqa: E402,F401 - registers the tables
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
)
from app.services.llm_agent import LLMAgent  # noqa: E402

DEFAULT_SEED = 42


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def print_summary(summary: RunSummary, artefacts) -> None:
    config = default_config()

    print("=" * 78)
    print(f"  Batch: {summary.batch_id}    Seed: {summary.seed}    Engine: root_cause")
    print(f"  Dataset: {summary.dataset_batch_id}    Run clock: {summary.now:%Y-%m-%d %H:%M} UTC")
    print("=" * 78)

    rule("INGEST")
    print(f"  Payment attempts read      : {summary.attempts_ingested}")
    print(f"  Failed attempts            : {summary.failed_attempts}")
    print(
        f"  Corridor levels            : "
        f"{', '.join(level.name for level in config.detection.levels)}"
    )
    print(
        f"  Window / stride            : "
        f"{config.detection.window} rolling, {config.detection.stride} step"
    )
    print(
        f"  Minimum volume             : {config.detection.min_window_attempts} in window, "
        f"{config.detection.min_baseline_attempts} baseline "
        f"({config.detection.volume_rule})"
    )

    rule("RECOVERED")
    print(f"  Amount at risk (total)     : {rupees(summary.amount_at_risk_paise)}")
    print(f"  Amount recovered           : {rupees(summary.amount_recovered_paise)}")
    print(f"  Recovery rate              : {summary.recovery_rate:.2%}")

    rule("DETECTION")
    print(f"  Corridors flagged          : {summary.detections}")
    for detection in artefacts.detections:
        print(
            f"    - {detection.corridor.label:<28} "
            f"{detection.observed_success_rate:>6.1%} vs {detection.baseline_success_rate:>6.1%} "
            f"baseline over {detection.window_attempts:>3} attempts, "
            f"p={detection.p_value:.4f}, at risk {rupees(detection.value_at_risk_paise)}"
        )

    rule("DIAGNOSIS")
    for key, count in sorted(summary.diagnoses_by_determination.items()):
        print(f"  {key:<27}: {count}")
    for outcome in artefacts.corridor_outcomes:
        print(
            f"    - {outcome.detection.corridor.label:<28} "
            f"{outcome.diagnosis.determination.value} / "
            f"{outcome.diagnosis.hypothesis.value} -> recommended "
            f"{outcome.diagnosis.recommended_action.value}"
        )
        print(f"        reasoning : {outcome.diagnosis.reasoning}")
        print(
            f"        policy    : {'ALLOWED' if outcome.decision.allowed else 'DENIED'} "
            f"{outcome.decision.action.value} by {outcome.decision.rule_id}"
        )

    rule("ACTIONS")
    for action, count in sorted(summary.action_counts.items()):
        print(f"  {action:<27}: {count}")
    print(f"  reroutes authorised        : {summary.reroutes_authorised}")
    for reroute in artefacts.reroutes:
        print(
            f"    - {reroute.corridor_key} -> {reroute.to_route_id}, "
            f"expires {reroute.expires_at:%Y-%m-%d %H:%M} UTC ({reroute.expiry_rule})"
        )

    rule("STOPPED — the compliance evidence")
    print(f"  Policy denials             : {summary.policy_denials}")
    for rule_id, count in sorted(summary.denials_by_rule.items(), key=lambda kv: -kv[1]):
        print(f"    - {rule_id:<40} x{count}")
    print(f"  Retries suppressed         : {summary.retries_suppressed}  (permanent — wasted attempts avoided)")
    print(f"  Retries deferred           : {summary.retries_deferred}  (timing only — still recoverable)")
    print(f"  Human escalations          : {summary.human_escalations}")
    print(
        f"  Recommendations overruled  : "
        f"{summary.provenance['overridden_recommendations']}  <- the gate firing"
    )

    rule("PROVENANCE — reasoned vs ruled, never blended")
    print(f"  Reasoned (source=model)    : {summary.provenance['model_entries']}"
          f"  (cache hits {summary.provenance['cache_hits']})")
    print(f"  Ruled (deterministic)      : {summary.provenance['deterministic_entries']}")
    print(f"  LLM fallbacks taken        : {summary.llm_fallbacks}")
    print(f"  Abstentions                : {summary.provenance['abstentions']}")
    print(f"  Policy violations          : {summary.provenance['policy_violations']}  (must be 0)")
    for source, figures in sorted(summary.provenance["by_source"].items()):
        print(
            f"    {source:<15} entries {figures['entries']:>4}  "
            f"at risk {rupees(figures['amount_at_risk_paise']):>16}  "
            f"recovered {rupees(figures['amount_recovered_paise']):>16}  "
            f"rate {figures['recovery_rate']:.2%}"
        )

    score = summary.detection_score
    if score is None:
        rule("GROUND TRUTH")
        print("  No manifest beside the dataset — detection accuracy not scored.")
        return

    rule("DETECTION vs GROUND TRUTH")
    print(f"  Source manifest            : {score.ground_truth_source}")
    print(f"  True positives             : {score.true_positives}")
    print(f"  False positives            : {score.false_positives}")
    print(f"  False negatives            : {score.false_negatives}")
    print(f"  Precision / Recall         : {score.precision:.2%} / {score.recall:.2%}")
    print(f"  Decoy false positives      : {score.decoy_false_positives}  (must be 0)")
    for match in score.matched:
        print(
            f"    HIT  {match['label']:<34} {match['corridor']} "
            f"(detected {match['detection_latency_hours']}h after onset)"
        )
    for miss in score.missed_degradations:
        print(f"    MISS {miss}")
    if score.unmatched_detections:
        print("\n  False positives, reported in full:")
        for fp in score.unmatched_detections:
            print(
                f"    - {fp['corridor']} ({fp['level']}): "
                f"{fp['observed_success_rate']:.1%} vs {fp['baseline_success_rate']:.1%} "
                f"over {fp['window_attempts']} attempts, "
                f"confidence {fp['statistical_confidence']:.4f}"
            )
    else:
        print("\n  No false positives at the configured threshold in this run.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Engine run id. Defaults to rc-s<seed>-<timestamp>, which is unique per run.",
    )
    parser.add_argument("--dataset", default=None, help="Path to a payments .jsonl")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Force every reasoning task onto its fallback, as if the quota had run out.",
    )
    args = parser.parse_args(argv)

    batch_id = args.batch_id or f"rc-s{args.seed}-{datetime.now(UTC):%Y%m%d%H%M%S}"

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        agent = (
            LLMAgent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))
            if args.deterministic
            else LLMAgent()
        )
        mode = "deterministic (forced)" if args.deterministic else (
            f"live provider: {settings.gemini_model}"
            if agent.provider_enabled
            else "deterministic (no key configured)"
        )
        print(f"Reasoning layer: {mode}")

        artefacts = RootCauseRunner(db, agent=agent).run(
            batch_id=batch_id, seed=args.seed, dataset=args.dataset
        )
        print_summary(artefacts.summary, artefacts)
        print(
            f"\nRead it back:  GET /api/v1/root-cause/runs/{batch_id}/detections"
            f"\n               GET /api/v1/audit/batches/{batch_id}/summary"
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
