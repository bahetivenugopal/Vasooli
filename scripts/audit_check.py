"""`/audit-check`, runnable.

    python scripts/audit_check.py <batch_id> [<batch_id> ...]
    python scripts/audit_check.py --unified <run_id>     # all three legs at once
    python scripts/audit_check.py <batch_id> --dump
    python scripts/audit_check.py --latest               # newest run per engine

Prints the report in the shape `.claude/commands/audit-check.md` specifies, and
**exits non-zero on any violation** — a validation pass that cannot fail a build
is a validation pass nobody notices failing.

The twelve validations live in `app/services/audit_validation.py`, not here, so
this command, the consolidated report and the test suite cannot disagree about
whether a run is clean.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - console plumbing
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

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
from app.models.enums import BatchStatus, Engine  # noqa: E402
from app.services.audit_validation import check_audit, load_entries  # noqa: E402
from app.services.integrity import verify  # noqa: E402
from app.services.unified_run import batch_ids_for  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_ids", nargs="*", help="one or more batch ids")
    parser.add_argument("--unified", help="a unified run id; checks all three legs")
    parser.add_argument(
        "--latest", action="store_true", help="the newest completed run per engine"
    )
    parser.add_argument("--dump", action="store_true", help="print every entry in full")
    parser.add_argument(
        "--integrity",
        action="store_true",
        help="also run the cross-source metric integrity comparison",
    )
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        batch_ids = list(args.batch_ids)
        if args.unified:
            batch_ids.extend(bid for bid in batch_ids_for(args.unified).values())
        if args.latest:
            batch_ids.extend(_latest_batch_ids(db))
        batch_ids = list(dict.fromkeys(batch_ids))

        if not batch_ids:
            parser.error("give at least one batch id, or --unified/--latest")

        missing = [b for b in batch_ids if db.get(BatchRun, b) is None]
        if missing:
            print(f"No such batch run(s): {', '.join(missing)}", file=sys.stderr)
            return 2

        report = check_audit(db, batch_ids)

        print(f"Batch: {', '.join(report.batch_ids)}        Entries: {report.entries}")
        print()
        print(f"PASS/FAIL: {'PASS' if report.passed else 'FAIL'}")
        print()
        print(f"Violations: {len(report.violations)}")
        for violation in report.violations:
            print(f"  {violation}")
        if report.violations:
            print()
            print("  by check:")
            for check, count in sorted(report.violations_by_check.items()):
                print(f"    {check:<24} x{count}")

        _section("Rule citations used", report.rule_citations)
        _section("Provenance split", report.provenance)
        _section("By prompt version", report.prompt_versions)
        _section("Outcomes", report.outcomes)
        _section("Actions", report.actions)

        for note in report.suspicious:
            print(f"\nNOTE: {note}")

        exit_code = 0 if report.passed else 1

        if args.integrity:
            integrity = verify(db, batch_ids)
            print("\nMetric integrity")
            print("----------------")
            for name, ok in integrity.checks.items():
                print(f"  {name:<28}: {'PASS' if ok else 'FAIL'}")
            for label, items in (
                ("Discrepancies", [str(d) for d in integrity.discrepancies]),
                ("Orphan actions", integrity.orphan_actions),
                ("Untraced money", integrity.untraced_money),
            ):
                for item in items:
                    print(f"  {label}: {item}")
            if not integrity.passed:
                exit_code = 1

        if args.dump:
            print("\nEntries")
            print("-------")
            for entry in load_entries(db, batch_ids):
                print(
                    f"  [{entry.id}] {entry.timestamp:%Y-%m-%d %H:%M:%S} "
                    f"{entry.engine}/{entry.entity_id} {entry.action} -> {entry.outcome}"
                )
                print(
                    f"        rule={entry.authorising_rule}  reason={entry.reason_code}  "
                    f"source={entry.provenance.get('source')}"
                )
                print(
                    f"        at_risk={entry.amount_at_risk_paise} "
                    f"recovered={entry.amount_recovered_paise}"
                )
                print(f"        {entry.rationale}")

        return exit_code
    finally:
        db.close()


def _section(title: str, values: dict[str, int]) -> None:
    if not values:
        return
    print(f"\n{title}:")
    for key, count in sorted(values.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {key:<52} x{count}")


def _latest_batch_ids(db) -> list[str]:
    out: list[str] = []
    for member in (Engine.ROOT_CAUSE, Engine.MANDATE_RECOVERY, Engine.RECEIVABLES):
        row = db.scalars(
            select(BatchRun)
            .where(BatchRun.engine == member.value)
            .where(BatchRun.status == BatchStatus.COMPLETED.value)
            .order_by(BatchRun.started_at.desc())
            .limit(1)
        ).first()
        if row is not None:
            out.append(row.batch_id)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
