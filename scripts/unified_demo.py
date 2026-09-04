"""All three engines, one run, one consolidated report.

    python scripts/unified_demo.py                       # the demo path
    python scripts/unified_demo.py --deterministic       # no provider at all
    python scripts/unified_demo.py --seed 7
    python scripts/unified_demo.py --json report.json    # machine-readable too

This is *the* command. Everything else in `scripts/` runs one engine; this runs
the product. It produces one report whose every figure is recomputed from the
audit trail, and whose consolidated section is the **same object** the dashboard's
front page renders — so the console and the dashboard cannot disagree, because
there is one computation with two renderings rather than two computations that
were checked against each other once.

It also prints, without being asked:

- **Which mode the run was in.** Live provider, no key, or deterministic-only.
  A number produced with the model switched off means something different from
  one produced with it on, and the reader should never have to guess which.
- **The integrity verdict.** Every headline metric recomputed a second time from
  raw audit rows and compared against both reporting paths, plus the twelve
  `/audit-check` validations. A consolidated report that cannot say whether its
  own numbers check out is a press release.
- **The refusals.** Policy denials, compliance blocks, suppressions. A run with
  none of them is a run where the gate never fired.
"""

from __future__ import annotations

import argparse
import json
import sys
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
from app.models.enums import Engine  # noqa: E402
from app.models.unified import UnifiedRunReport  # noqa: E402
from app.services.llm_agent import LLMAgent  # noqa: E402
from app.services.unified_run import (  # noqa: E402
    UnifiedRunner,
    unified_run_id,
)

DEFAULT_SEED = 42
WIDTH = 78

ENGINE_TITLES = {
    Engine.ROOT_CAUSE: "ENGINE 1 - ROOT-CAUSE RECOVERY",
    Engine.MANDATE_RECOVERY: "ENGINE 2 - MANDATE & SUBSCRIPTION RECOVERY",
    Engine.RECEIVABLES: "ENGINE 3 - B2B RECEIVABLES CHASER",
}


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def crore_lakh(paise: int) -> str:
    """Indian-scale rendering, for the one figure people say out loud."""
    rupees_value = paise / 100
    if rupees_value >= 1_00_00_000:
        return f"Rs {rupees_value / 1_00_00_000:.2f} Cr"
    if rupees_value >= 1_00_000:
        return f"Rs {rupees_value / 1_00_000:.2f} L"
    return f"Rs {rupees_value:,.2f}"


def rule(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def wrap(text: str, indent: str = "  ") -> None:
    import textwrap

    for line in textwrap.wrap(text, width=WIDTH - len(indent)):
        print(f"{indent}{line}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def print_report(report: UnifiedRunReport) -> None:
    print("=" * WIDTH)
    print(f"  VASOOLI - UNIFIED RUN  {report.run_id}")
    print(f"  Seed {report.seed}    {report.elapsed_seconds:.1f}s    "
          f"{report.completed_at:%Y-%m-%d %H:%M} UTC")
    print("=" * WIDTH)

    rule("MODE")
    wrap(report.mode.description)

    overview = report.overview

    rule("HEADLINE - all three engines, one run")
    print(f"  Value at risk              : {crore_lakh(overview.amount_at_risk_paise)}"
          f"   ({rupees(overview.amount_at_risk_paise)})")
    print(f"  Value recovered            : {crore_lakh(overview.amount_recovered_paise)}"
          f"   ({rupees(overview.amount_recovered_paise)})")
    print(f"  Recovery rate              : {overview.recovery_rate:.2%}")
    print(f"  Audited decisions          : {overview.entries}")
    print(f"  Schema violations          : {overview.policy_violations}")
    print()
    wrap(overview.blended_caveat)

    rule("PER-ENGINE CONTRIBUTION")
    print(f"  {'Engine':<34}{'At risk':>16}{'Recovered':>16}{'Rate':>9}")
    for record in report.engines:
        label = ENGINE_TITLES[record.engine].split(" - ", 1)[1].title()
        print(
            f"  {label:<34}{crore_lakh(record.amount_at_risk_paise):>16}"
            f"{crore_lakh(record.amount_recovered_paise):>16}"
            f"{record.recovery_rate:>9.2%}"
        )
    print()
    for contribution in overview.by_engine:
        print(f"  {contribution.label} — what its recovery figure means:")
        wrap(contribution.recovery_definition, indent="      ")
        print()

    rule("REASONED VS RULED - never blended into one figure")
    for source, bucket in sorted(overview.by_source.items()):
        if bucket.entries == 0:
            continue
        print(
            f"  {source:<16} {bucket.entries:>4} entries   "
            f"at risk {crore_lakh(bucket.amount_at_risk_paise):>13}   "
            f"recovered {crore_lakh(bucket.amount_recovered_paise):>13}   "
            f"{bucket.recovery_rate:>7.2%}"
        )

    rule("TRUST - the counts that prove the bounds are real")
    for metric in overview.trust:
        print(f"  {metric.label:<28}: {metric.value}")
        if metric.by_rule:
            for citation, count in sorted(
                metric.by_rule.items(), key=lambda kv: (-kv[1], kv[0])
            )[:6]:
                print(f"      {citation:<44} x{count}")

    rule("EACH ENGINE'S OWN HEADLINE METRICS")
    for record in report.engines:
        print(f"\n  {ENGINE_TITLES[record.engine]}")
        print(f"    batch {record.batch_id}   dataset {record.dataset_batch_id}")
        if record.run_clock is not None:
            print(f"    run clock {record.run_clock:%Y-%m-%d %H:%M} UTC "
                  f"(derived from this engine's own data)")
        for label, value in _engine_headline_lines(record.engine, record.headline):
            print(f"    {label:<34}: {value}")

    rule("RUN PROVENANCE")
    for record in report.engines:
        print(f"  {record.engine.value:<20} {record.batch_id:<30} "
              f"{record.elapsed_seconds:>7.1f}s")
    print(f"\n  Reproduce: python scripts/unified_demo.py --seed {report.seed}")
    print(f"  Dashboard: http://127.0.0.1:3000  (overview reads exactly these "
          f"{len(report.batch_ids)} runs)")

    print_integrity(report)


def _engine_headline_lines(engine: Engine, headline: dict) -> list[tuple[str, object]]:
    """The few figures per engine worth printing in a cross-engine report.

    Deliberately a short list. The full `RunSummary` is in the JSON output and on
    each engine's dashboard page; a consolidated report that reprints every field
    of three summaries is a report nobody reads to the end.
    """
    if not headline:
        return [("(no stored run summary)", "")]

    def get(key: str, default: object = "-") -> object:
        return headline.get(key, default)

    if engine is Engine.ROOT_CAUSE:
        score = headline.get("detection_score") or {}
        return [
            ("Payment attempts ingested", get("attempts_ingested")),
            ("Failed attempts", get("failed_attempts")),
            ("Corridors flagged", get("detections")),
            (
                "Detection precision / recall",
                f"{score.get('precision', 0):.0%} / {score.get('recall', 0):.0%}"
                if score
                else "-",
            ),
            ("Reroutes authorised", get("reroutes_authorised")),
            ("Retries suppressed / deferred", f"{get('retries_suppressed')} / {get('retries_deferred')}"),
            ("Policy denials", get("policy_denials")),
        ]
    if engine is Engine.MANDATE_RECOVERY:
        return [
            ("Mandates ingested", get("mandates_ingested")),
            ("With a failed cycle", get("mandates_with_failed_cycle")),
            (
                "Addressable recovery",
                f"{headline.get('addressable_recovery_rate', 0):.2%} of "
                f"{crore_lakh(int(headline.get('amount_addressable_paise', 0)))}",
            ),
            ("Debits attempted / recovered", f"{get('debits_attempted')} / {get('debits_recovered')}"),
            ("Compliance-blocked attempts", get("compliance_blocked")),
            ("Wasted attempts avoided", get("wasted_attempts_avoided")),
            ("Tone rejections", get("tone_rejections")),
            ("Mandates halted", get("mandates_halted")),
        ]
    extraction = headline.get("extraction") or {}
    promise = (extraction.get("promise_detection") or {}) if extraction else {}
    return [
        ("Invoices ingested / overdue", f"{get('invoices_ingested')} / {get('invoices_overdue')}"),
        (
            "Promise extraction P / R",
            f"{promise.get('precision', 0):.0%} / {promise.get('recall', 0):.0%} "
            f"over {extraction.get('replies_scored', 0)} replies"
            if promise
            else "-",
        ),
        (
            "Abstentions on reading a reply",
            f"{extraction.get('abstentions', 0)} of "
            f"{extraction.get('replies_total', 0)}",
        ),
        (
            "Promises kept / broken / active",
            f"{get('promises_kept')} / {get('promises_broken')} / {get('promises_active')}",
        ),
        ("Reminders sent / held", f"{get('reminders_sent')} / {get('reminders_held')}"),
        ("Disputes frozen", get("disputes_frozen")),
        ("Invoices deprioritised", get("invoices_deprioritised")),
        ("Escalations", get("escalations")),
        ("Abstentions", get("abstentions")),
    ]


def print_integrity(report: UnifiedRunReport) -> None:
    rule("METRIC INTEGRITY - three sources, one answer")
    detail = report.integrity_detail
    print(f"  Entries checked            : {detail.get('entries_checked', 0)}")
    for name, ok in report.integrity_checks.items():
        print(f"  {name:<28}: {'PASS' if ok else 'FAIL'}")
    print(f"\n  VERDICT: {'PASS' if report.integrity_passed else 'FAIL'}")

    for key, title in (
        ("schema_violations", "Audit schema violations"),
        ("discrepancies", "Cross-source discrepancies"),
        ("orphan_actions", "Orphan actions"),
        ("untraced_money", "Untraced money"),
    ):
        items = detail.get(key) or []
        if items:
            print(f"\n  {title} ({len(items)}):")
            for item in items[:12]:
                wrap(str(item), indent="      ")
            if len(items) > 12:
                print(f"      ... and {len(items) - 12} more")

    for note in detail.get("suspicious") or []:
        print(f"\n  NOTE: {note}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="force every reasoning task onto its registered fallback",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="override the derived unified run id (default: derived from the seed)",
    )
    parser.add_argument(
        "--salt",
        default=None,
        help="distinguish a repeat run of the same seed on the same database",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write the full report as JSON to this path",
    )
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)

    config = (
        Settings(gemini_api_key="", llm_deterministic_only=True)
        if args.deterministic
        else settings
    )
    agent = LLMAgent(settings=config)

    run_id = args.run_id or unified_run_id(args.seed, salt=args.salt)

    db = SessionLocal()
    try:
        runner = UnifiedRunner(db, agent=agent, config=config)
        report = runner.run(
            seed=args.seed,
            run_id=run_id,
            progress=lambda e, phase: (
                print(f"  ... {e.value} {phase}", file=sys.stderr) if phase == "starting" else None
            ),
        )
    finally:
        db.close()

    print_report(report)

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        print(f"\n  JSON report written to {args.json}")

    # A failed integrity check must fail the command. A consolidated report that
    # prints FAIL and exits 0 is a report a CI job would wave through.
    return 0 if report.integrity_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
