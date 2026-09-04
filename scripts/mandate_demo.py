"""Engine 2 end to end, with the numbers printed honestly.

    python scripts/mandate_demo.py
    python scripts/mandate_demo.py --deterministic          # force every fallback
    python scripts/mandate_demo.py --now 2026-09-01T14:00+05:30
    python scripts/mandate_demo.py --timeline mnd_0002      # one mandate, in full

It runs the whole loop over a mandate book — classify, plan, authorise, act,
audit — and prints the summary in the shape `/run-batch-demo` specifies, plus the
things this phase singles out:

- **Recovery by failure class**, because a single blended rate hides the fact the
  engine exists to exploit: soft declines recover, hard ones do not.
- **Two denominators.** The headline rate over everything at risk, and the rate
  over the money the engine was actually permitted to debit. Two thirds of this
  book sits above the Rs 15,000 AFA threshold or behind a hard stop, where no
  compliant system may auto-debit at all.
- **Compliance-blocked attempts**, broken down by the rule that refused. Non-zero
  is the point.

`--now` matters more here than in Engine 1. Quiet hours are evaluated against the
run clock, so a batch executed at 03:00 IST correctly holds every customer
message — which is right, and looks wrong unless you know why.

Writes to the configured database, so everything it prints can be read back
through `/api/v1/mandate-recovery/runs/<batch_id>` and `/api/v1/audit/...`.
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
from app.engines.mandate_recovery import MandateRunner, default_config  # noqa: E402
from app.engines.mandate_recovery.schemas import RunSummary  # noqa: E402
from app.models import (  # noqa: E402,F401 - registers the tables
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
    MandateCommunication,
    MandateRecoveryState,
)
from app.models.enums import EntityType  # noqa: E402
from app.services.audit_trail import AuditTrail  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402

DEFAULT_SEED = 42


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def hours(delta) -> str:
    return f"{delta.total_seconds() / 3600:.0f}h"


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def print_summary(summary: RunSummary, artefacts) -> None:
    config = default_config()

    print("=" * 78)
    print(f"  Batch: {summary.batch_id}    Seed: {summary.seed}    Engine: mandate_recovery")
    print(
        f"  Dataset: {summary.dataset_batch_id}    "
        f"Run clock: {summary.now:%Y-%m-%d %H:%M} UTC"
    )
    print("=" * 78)

    rule("INGEST")
    print(f"  Mandates read              : {summary.mandates_ingested}")
    print(f"  With a failed current cycle: {summary.mandates_with_failed_cycle}")
    print(
        f"  A2 notice window           : "
        f"{hours(config.notice.min_lead)}-{hours(config.notice.max_lead)} before every debit "
        f"({config.notice.rule}, regulatory)"
    )
    print(
        f"  Part B per-cycle bounds    : {config.attempts.max_per_cycle} attempts, "
        f"{hours(config.attempts.min_spacing)} apart ({config.attempts.rule})"
    )

    rule("RECOVERED — two denominators, neither one hidden")
    print(f"  Amount at risk (all)       : {rupees(summary.amount_at_risk_paise)}")
    print(f"  Amount recovered           : {rupees(summary.amount_recovered_paise)}")
    print(f"  Recovery rate (all)        : {summary.recovery_rate:.2%}")
    print(f"  Amount addressable         : {rupees(summary.amount_addressable_paise)}")
    print(f"  Recovery rate (addressable): {summary.addressable_recovery_rate:.2%}")
    print(f"    addressable = {summary.addressable_definition}")

    rule("BY FAILURE CLASS — where the story lives")
    print(
        f"  {'class':<18}{'mandates':>9}{'at risk':>16}{'recovered':>16}"
        f"{'rate':>8}{'suppressed':>12}"
    )
    for name, bucket in summary.by_failure_class.items():
        print(
            f"  {name:<18}{bucket.mandates:>9}{rupees(bucket.amount_at_risk_paise):>16}"
            f"{rupees(bucket.amount_recovered_paise):>16}{bucket.recovery_rate:>8.1%}"
            f"{bucket.retries_suppressed:>12}"
        )

    rule("AFA THRESHOLD — both branches exercised (rbi-mandate-rules:A3)")
    for side, bucket in summary.afa_branch.items():
        print(
            f"  {side:<14} mandates {bucket.mandates:>3}  at risk "
            f"{rupees(bucket.amount_at_risk_paise):>16}  debits {bucket.debits_attempted:>3}  "
            f"auth requests {bucket.authentication_requests:>3}  "
            f"blocked for fresh AFA {bucket.blocked_for_fresh_afa:>3}"
        )

    rule("STOPPED — the compliance evidence")
    print(f"  Compliance-blocked attempts: {summary.compliance_blocked}")
    for rule_id, count in sorted(
        summary.compliance_blocked_by_rule.items(), key=lambda kv: -kv[1]
    ):
        print(f"    - {rule_id:<44} x{count}")
    print(f"  Retries suppressed         : {summary.retries_suppressed}")
    print(
        f"  Wasted attempts avoided    : {summary.wasted_attempts_avoided}  (estimate)"
    )
    print(f"    baseline = {summary.wasted_attempts_baseline}")
    print(f"  Mandates at the attempt cap: {summary.mandates_at_attempt_cap}")
    print(f"  Mandates hard-stopped      : {summary.mandates_halted}")
    print(f"  Human escalations          : {summary.human_escalations}")
    print(f"  Policy denials             : {summary.policy_denials}")
    for rule_id, count in sorted(summary.denials_by_rule.items(), key=lambda kv: -kv[1]):
        print(f"    - {rule_id:<44} x{count}")

    rule("COMMUNICATIONS — drafted, gated, logged, never dispatched")
    print(f"  Pre-debit notices scheduled: {summary.notices_scheduled}")
    print(f"    marked for delivery      : {summary.notices_sent}")
    print(f"    held                     : {summary.notices_held}")
    print(f"  Notice status as found     : {summary.notice_status_counts}")
    print(f"  Customer messages drafted  : {summary.communications_drafted}")
    print(f"    held or suppressed       : {summary.communications_held}")
    print(
        f"  Tone rejections (TN1/TN2)  : {summary.tone_rejections}  "
        "(draft discarded, template used)"
    )

    rule("PROVENANCE — reasoned vs ruled, never blended")
    print(
        f"  Reasoned (source=model)    : {summary.provenance['model_entries']}"
        f"  (cache hits {summary.provenance['cache_hits']})"
    )
    print(f"  Ruled (deterministic)      : {summary.provenance['deterministic_entries']}")
    print(f"  LLM fallbacks taken        : {summary.llm_fallbacks}")
    print(f"  Unknown declines reasoned  : {summary.unknown_declines_classified}")
    print(
        f"  Recommendations overruled  : "
        f"{summary.provenance['overridden_recommendations']}  <- the gate firing"
    )
    print(
        f"  Policy violations          : {summary.provenance['policy_violations']}  (must be 0)"
    )
    for source, figures in sorted(summary.provenance["by_source"].items()):
        print(
            f"    {source:<15} entries {figures['entries']:>4}  "
            f"at risk {rupees(figures['amount_at_risk_paise']):>16}  "
            f"recovered {rupees(figures['amount_recovered_paise']):>16}  "
            f"rate {figures['recovery_rate']:.2%}"
        )

    rule("NEXT STEP PER MANDATE — a sample of the explanations")
    for outcome in _sample_outcomes(artefacts.outcomes):
        e = outcome.schedule.explanation
        when = f" at {e.scheduled_for:%Y-%m-%d %H:%M} UTC" if e.scheduled_for else ""
        print(f"    {e.mandate_id}  {e.step.value}{when}  [{e.rule_id}]")


def _sample_outcomes(outcomes):
    """One example of each distinct next step, so every branch is visible."""
    seen: dict[str, object] = {}
    for outcome in outcomes:
        seen.setdefault(outcome.schedule.explanation.step.value, outcome)
    return [seen[k] for k in sorted(seen)]


def print_timeline(db, batch_id: str, mandate_id: str) -> None:
    """One mandate's full history, the way the demo tells it."""
    trail = AuditTrail(db)
    rule(f"TIMELINE — {mandate_id}")
    entries = [
        e
        for e in trail.entity_timeline(EntityType.MANDATE, mandate_id)
        if e.batch_id == batch_id
    ]
    if not entries:
        print(f"  No entries for {mandate_id} in {batch_id}.")
        return
    for entry in entries:
        print(
            f"  {entry.timestamp:%Y-%m-%d %H:%M} UTC  {entry.action:<22}"
            f"{entry.outcome:<11}{entry.authorising_rule}"
        )
        print(f"      {entry.rationale}")
    comms = db.query(MandateCommunication).filter(
        MandateCommunication.batch_id == batch_id,
        MandateCommunication.mandate_id == mandate_id,
    )
    for comm in comms:
        print(
            f"\n  MESSAGE [{comm.kind}] {comm.status} via {comm.channel}, "
            f"cta={comm.call_to_action}"
        )
        print(f"      subject: {comm.subject}")
        print(f"      body   : {comm.body}")
        if comm.held_rule:
            print(f"      held by: {comm.held_rule}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Engine run id. Defaults to mr-s<seed>-<timestamp>, unique per run.",
    )
    parser.add_argument("--dataset", default=None, help="Path to a mandates .jsonl")
    parser.add_argument(
        "--now",
        default=None,
        help="ISO run clock. Defaults to the latest debit attempted in the book.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Force every reasoning task onto its fallback, as if the quota had run out.",
    )
    parser.add_argument(
        "--timeline",
        default=None,
        help="Also print this mandate's complete timeline after the summary.",
    )
    args = parser.parse_args(argv)

    batch_id = args.batch_id or f"mr-s{args.seed}-{datetime.now(UTC):%Y%m%d%H%M%S}"
    run_now = datetime.fromisoformat(args.now) if args.now else None

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        agent = (
            LLMAgent(settings=Settings(gemini_api_key="", llm_deterministic_only=True))
            if args.deterministic
            else LLMAgent()
        )
        mode = (
            "deterministic (forced)"
            if args.deterministic
            else (
                f"live provider: {settings.gemini_model}"
                if agent.provider_enabled
                else "deterministic (no key configured)"
            )
        )
        print(f"Reasoning layer: {mode}")

        artefacts = MandateRunner(db, agent=agent).run(
            batch_id=batch_id, seed=args.seed, dataset=args.dataset, now=run_now
        )
        print_summary(artefacts.summary, artefacts)
        if args.timeline:
            print_timeline(db, batch_id, args.timeline)
        print(
            f"\nRead it back:  GET /api/v1/mandate-recovery/runs/{batch_id}/mandates"
            f"\n               GET /api/v1/mandate-recovery/runs/{batch_id}/mandates/mnd_0002"
            f"\n               GET /api/v1/audit/batches/{batch_id}/summary"
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
