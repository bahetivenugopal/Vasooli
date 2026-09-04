"""Engine 3 end to end, with the numbers printed honestly.

    python scripts/receivables_demo.py
    python scripts/receivables_demo.py --deterministic       # force every abstention
    python scripts/receivables_demo.py --now 2026-09-01T03:00+05:30
    python scripts/receivables_demo.py --timeline inv_0001   # one invoice, in full

It runs the whole loop over an invoice ledger — read replies, track promises,
prioritise, chase, audit — and prints the summary in the shape `/run-batch-demo`
specifies, plus the two things this phase singles out:

- **Extraction accuracy against held-out ground truth**, as a confusion matrix
  with the abstention rate reported *beside* it and never folded in. Reported
  over model-handled replies only.
- **Messages suppressed**, split by the rule that suppressed them. This is the
  direct measure of harassment avoided, and it is the number that makes "we do
  not chase people who are cooperating" a claim you can check.

`--now` matters more here than anywhere else in the project. Engine 3 is
*entirely* outreach, so a batch executed outside 09:00-21:00 IST correctly holds
every single reminder. That is the rule working, and it looks like a broken
engine unless you know why — so the demo prints the clock next to every figure.

Writes to the configured database, so everything it prints can be read back
through `/api/v1/receivables/runs/<batch_id>` and `/api/v1/audit/...`.
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

from app.core.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.engines.receivables import ReceivablesRunner, default_config  # noqa: E402
from app.engines.receivables.schemas import RunSummary  # noqa: E402
from app.models import (  # noqa: E402,F401 - registers the tables
    AuditEntry,
    BatchRun,
    CorridorDetection,
    CorridorReroute,
    InvoiceChaseState,
    InvoiceCommunication,
    MandateCommunication,
    MandateRecoveryState,
    PromiseToPay,
)
from app.models.enums import EntityType  # noqa: E402
from app.services.audit_trail import AuditTrail  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402
from app.services.policy_engine import OUTREACH_TZ  # noqa: E402

DEFAULT_SEED = 42


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def matrix_row(name: str, m) -> str:
    return (
        f"  {name:<24} TP {m.true_positives:>3}  FP {m.false_positives:>3}  "
        f"FN {m.false_negatives:>3}  TN {m.true_negatives:>3}   "
        f"P {m.precision:>6.1%}  R {m.recall:>6.1%}  F1 {m.f1:>6.1%}"
    )


def print_summary(summary: RunSummary, artefacts) -> None:
    config = default_config()
    local = summary.now.astimezone(OUTREACH_TZ)

    print("=" * 92)
    print(f"  Batch: {summary.batch_id}    Seed: {summary.seed}    Engine: receivables")
    print(
        f"  Dataset: {summary.dataset_batch_id}    "
        f"Run clock: {summary.now:%Y-%m-%d %H:%M} UTC ({local:%H:%M} IST)"
    )
    print("=" * 92)

    rule("INGEST")
    print(f"  Invoices read              : {summary.invoices_ingested}")
    print(f"  Overdue                    : {summary.invoices_overdue}")
    print(f"  On the worklist            : {summary.invoices_worklisted}")
    print(
        f"  Ladder                     : {len(config.ladder.rungs)} rungs "
        f"({', '.join(r.name for r in config.ladder.rungs)}), "
        f"{config.ladder.min_interval.total_seconds() / 3600:.0f}h apart "
        f"({config.ladder.rule})"
    )
    print(
        f"  Contact caps               : {config.contact_caps.per_invoice}/invoice, "
        f"{config.contact_caps.per_customer}/customer per "
        f"{config.contact_caps.window.days} days ({config.contact_caps.rule})"
    )

    rule("RECOVERED — with the caveat attached, not in a footnote")
    print(f"  Value outstanding          : {rupees(summary.amount_outstanding_paise)}")
    print(f"  Value at risk              : {rupees(summary.amount_at_risk_paise)}")
    print(f"  Value recovered            : {rupees(summary.amount_recovered_paise)}")
    print(f"  Recovery rate              : {summary.recovery_rate:.2%}")
    print(f"    recovered = {summary.recovery_definition}")

    rule("BY AGEING BUCKET — older recovers worse, and that is reported not hidden")
    print(
        f"  {'bucket':<12}{'invoices':>9}{'outstanding':>18}{'recovered':>18}"
        f"{'rate':>8}{'reminders':>11}{'escalated':>11}"
    )
    for name, bucket in summary.by_ageing_bucket.items():
        print(
            f"  {name:<12}{bucket.invoices:>9}"
            f"{rupees(bucket.amount_outstanding_paise):>18}"
            f"{rupees(bucket.amount_recovered_paise):>18}"
            f"{bucket.recovery_rate:>8.1%}{bucket.reminders_sent:>11}"
            f"{bucket.escalations:>11}"
        )

    print_extraction(summary)

    rule("PROMISES — the register")
    print(f"  Extracted                  : {summary.promises_made}")
    print(f"    kept                     : {summary.promises_kept}")
    print(f"    broken                   : {summary.promises_broken}")
    print(f"    still active             : {summary.promises_active}")
    print(f"    superseded               : {summary.promises_superseded}")
    print(
        f"    conditional              : {summary.promises_conditional}  "
        f"(policy-bounds:PP2 — never scored against a date they did not give)"
    )
    print(
        f"    no extractable date      : {summary.promises_undateable}  "
        f"(policy-bounds:PP3 — {config.promises.undateable_horizon.days}-day horizon)"
    )

    reliable = [
        r for r in summary.customer_reliability.values() if r.reliability is not None
    ]
    if reliable:
        rule("CUSTOMER RELIABILITY — what feeds the next run's prioritisation")
        for entry in sorted(reliable, key=lambda r: (r.reliability or 0, r.customer_id))[:8]:
            print(
                f"  {entry.customer_id:<12} {entry.kept} kept / {entry.broken} broken "
                f"-> {entry.reliability:.0%} reliable"
            )
        no_history = len(summary.customer_reliability) - len(reliable)
        print(
            f"  ({no_history} more customers have promises on record but none resolved "
            "yet — reported as no history, never as a default score)"
        )

    rule("CHASED")
    for step, count in summary.chase_steps.items():
        print(f"  {step:<26} {count:>3}")
    print(f"  Reminders marked for send  : {summary.reminders_sent}")
    for rung, count in sorted(summary.reminders_by_rung.items()):
        print(f"    - {rung:<22} x{count}")
    print(
        f"  Payment links created      : {summary.payment_links_created} "
        f"(failed: {summary.payment_links_failed})"
    )

    rule("STOPPED — the harassment-avoided evidence")
    print(f"  Messages suppressed        : {summary.messages_suppressed}")
    for rule_id, count in sorted(summary.suppressed_by_rule.items(), key=lambda kv: -kv[1]):
        print(f"    - {rule_id:<40} x{count}")
    print(
        f"  Invoices deprioritised     : {summary.invoices_deprioritised}  "
        "(policy-bounds:PR2 — a live promise, so we did not chase)"
    )
    print(f"  Disputes frozen            : {summary.disputes_frozen}  (policy-bounds:RL4)")
    print(f"  Reminders held/withheld    : {summary.reminders_held}")
    print(f"  Policy denials             : {summary.policy_denials}")
    for rule_id, count in sorted(summary.denials_by_rule.items(), key=lambda kv: -kv[1]):
        print(f"    - {rule_id:<40} x{count}")

    rule("ESCALATED — only on evidence (policy-bounds:RL5)")
    print(f"  Escalations                : {summary.escalations}")
    for trigger, count in sorted(summary.escalations_by_trigger.items(), key=lambda kv: -kv[1]):
        print(f"    - {trigger:<40} x{count}")

    rule("PROVENANCE — reported per source, never blended")
    provenance = summary.provenance
    print(f"  Model-derived entries      : {provenance['model_entries']}")
    print(f"  Rule-derived entries       : {provenance['deterministic_entries']}")
    print(f"  Cache hits                 : {provenance['cache_hits']}")
    print(f"  Abstentions                : {summary.abstentions}")
    print(f"  LLM fallbacks              : {summary.llm_fallbacks}")
    print(f"  Policy violations          : {provenance['policy_violations']}  (must be 0)")
    for source, bucket in sorted(provenance["by_source"].items()):
        print(
            f"    {source:<14} entries {bucket['entries']:>4}  "
            f"at risk {rupees(bucket['amount_at_risk_paise']):>18}  "
            f"recovered {rupees(bucket['amount_recovered_paise']):>18}  "
            f"rate {bucket['recovery_rate']:.1%}"
        )


def print_extraction(summary: RunSummary) -> None:
    score = summary.extraction
    rule("EXTRACTION ACCURACY — measured against held-out ground truth")
    print(f"  Ground truth               : {score.ground_truth_source}")
    print(
        f"  Replies                    : {score.replies_total} total, "
        f"{score.replies_scored} scored, {score.abstentions} abstained "
        f"({score.abstention_rate:.1%})"
    )
    print(f"    {score.scoring_note}")
    if not score.replies_scored:
        print("  Nothing was scored: every reply abstained, so nothing is claimed.")
        return

    print()
    print(matrix_row("promise detection", score.promise_detection))
    print(matrix_row("dispute detection", score.dispute_detection))
    print(matrix_row("conditional (promises)", score.conditional_detection))
    for language, matrix in sorted(score.by_language.items()):
        print(matrix_row(f"promise / {language}", matrix))

    dates = score.date_accuracy
    print()
    print(
        f"  Dates: {dates.exact}/{dates.scored} exact ({dates.exact_rate:.1%}), "
        f"{dates.wrong} wrong, {dates.missing} missed, {dates.spurious} invented"
    )
    for difficulty, counts in dates.by_difficulty.items():
        print(
            f"    - {difficulty:<12} {counts.get('exact', 0)}/{counts.get('scored', 0)} exact"
            + (
                "   (a correct null: the right answer to \"when?\" is silence)"
                if difficulty == "none"
                else ""
            )
        )

    print("\n  Intent confusion (ground-truth category -> what the engine read):")
    for category, predictions in score.intent_confusion.items():
        rendered = ", ".join(f"{k} x{v}" for k, v in sorted(predictions.items()))
        print(f"    {category:<22} -> {rendered}")

    if score.failures:
        print(f"\n  Failures ({len(score.failures)}) — the interesting part:")
        for failure in score.failures[:6]:
            print(
                f"    {failure['reply_id']} [{failure['language']}/{failure['category']}] "
                f"wrong: {', '.join(failure['wrong'])}"
            )
            print(f"      text : {str(failure['text'])[:110]}")
            print(f"      truth: {failure['truth']}")
            print(f"      read : {failure['predicted']}")
    else:
        print(
            "\n  No failures on this corpus. Read that as a statement about the "
            "corpus — 20 reply templates, one reply per invoice — not as a "
            "production accuracy claim. See docs/metrics/engine-3-receivables.md."
        )


def print_timeline(db, batch_id: str, invoice_id: str) -> None:
    """One invoice, start to finish. The story the demo tells."""
    trail = AuditTrail(db)
    state = db.get(InvoiceChaseState, (batch_id, invoice_id))
    if state is None:
        print(f"no invoice {invoice_id} in run {batch_id}")
        return

    print("\n" + "=" * 92)
    print(f"  TIMELINE  {invoice_id}  —  {state.customer_name}")
    print("=" * 92)
    print(
        f"  {rupees(state.amount_paise)}, due {state.due_at:%d %b %Y}, "
        f"{state.days_overdue} days overdue, bucket {state.ageing_bucket}"
    )
    print(f"  Priority rank {state.priority_rank}  score {state.priority_score:.4f}")
    for component in state.score_breakdown["components"]:
        print(
            f"    {component['name']:<24} {component['normalized']:>6.3f} "
            f"x {component['weight']:.2f} = {component['contribution']:.4f}"
        )
    if state.deprioritised:
        print(f"    DEPRIORITISED by {state.score_breakdown['deprioritised_rule']}")

    print("\n  Decisions, oldest first:")
    for entry in trail.entity_timeline(EntityType.INVOICE, invoice_id):
        if entry.batch_id != batch_id:
            continue
        meta = entry.entry_metadata or {}
        print(
            f"\n    [{entry.timestamp:%Y-%m-%d %H:%M}] {entry.action} -> {entry.outcome}"
            f"   ({entry.authorising_rule})"
        )
        if meta.get("reply_text"):
            print(f'      customer said: "{meta["reply_text"]}"')
            print(f"      read as      : {meta.get('intent')} ({meta.get('language')})")
        print(f"      {entry.rationale}")
        if entry.provenance.get("source") == "model":
            print(
                f"      provenance   : {entry.provenance['model']} "
                f"({entry.provenance['prompt_version']}, "
                f"cache_hit={entry.provenance['cache_hit']}), "
                f"confidence {entry.model_confidence}"
            )

    promises = (
        db.query(PromiseToPay)
        .filter(PromiseToPay.batch_id == batch_id, PromiseToPay.invoice_id == invoice_id)
        .all()
    )
    if promises:
        print("\n  Promise register:")
        for promise in promises:
            committed = (
                f"{promise.committed_date:%Y-%m-%d}" if promise.committed_date else "no date"
            )
            print(
                f"    {promise.status.upper():<11} committed {committed}"
                f"{'  (conditional)' if promise.conditional else ''}"
                f"   [{promise.status_rule}]"
            )
            print(f"      {promise.status_rationale}")

    messages = (
        db.query(InvoiceCommunication)
        .filter(
            InvoiceCommunication.batch_id == batch_id,
            InvoiceCommunication.invoice_id == invoice_id,
        )
        .all()
    )
    if messages:
        print("\n  Messages (drafted, gated, logged — never sent):")
        for message in messages:
            print(f"    [{message.status}] rung {message.rung_number} ({message.rung})")
            if message.held_rule:
                print(f"      held by {message.held_rule}")
            print(f"      subject: {message.subject}")
            print(f"      body   : {message.body}")
            if message.payment_link_url:
                print(f"      link   : {message.payment_link_url}")

    print(f"\n  NEXT: {state.next_step} — {state.explanation}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--dataset", default=None)
    parser.add_argument(
        "--now",
        default=None,
        help="Run clock, ISO 8601. Defaults to the ledger's own latest observed event.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Force the no-provider path: every reply abstains, and the batch still completes.",
    )
    parser.add_argument("--timeline", default=None, help="Print one invoice in full, then exit.")
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Skip the ground-truth comparison (for a dataset with no manifest).",
    )
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        agent = (
            LLMAgent(settings=Settings(llm_deterministic_only=True, gemini_api_key=""))
            if args.deterministic
            else LLMAgent()
        )
        suffix = "det" if args.deterministic else "live"
        batch_id = args.batch_id or f"rcv-{args.seed}-{suffix}-{datetime.now(UTC):%H%M%S}"
        now = datetime.fromisoformat(args.now).astimezone(UTC) if args.now else None

        artefacts = ReceivablesRunner(db, agent=agent).run(
            batch_id=batch_id,
            seed=args.seed,
            dataset=args.dataset,
            now=now,
            score=not args.no_score,
        )
        print_summary(artefacts.summary, artefacts)
        if args.timeline:
            print_timeline(db, batch_id, args.timeline)
        print(f"\nRead it back: /api/v1/receivables/runs/{batch_id}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
