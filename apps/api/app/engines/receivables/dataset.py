"""Reading the invoice ledger.

Same rule as Engines 1 and 2, and here it matters most: **engine code opens
`.jsonl` files and never a manifest.** The invoices manifest holds the archetype
of every customer and — critically — the promise/date/dispute annotation for
every reply. An engine that has seen those has measured nothing, and the
extraction accuracy this phase reports would be a claim rather than a number.

`resolve_dataset()` accepts a directory and finds the `.jsonl` inside it, and
refuses a manifest path even when asked directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.engines.receivables.schemas import Invoice

#: The repo root, five levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATASET = REPO_ROOT / "data" / "samples" / "invoices" / "invoices.jsonl"
MANIFEST_SUFFIX = ".manifest.json"


class DatasetError(RuntimeError):
    """Raised when the dataset is missing, or when engine code reaches for a manifest."""


def resolve_dataset(path: Path | str | None = None) -> Path:
    """The `.jsonl` file to read."""
    candidate = Path(path) if path else DEFAULT_DATASET
    if candidate.name.endswith(MANIFEST_SUFFIX):
        raise DatasetError(
            f"{candidate.name} is a manifest. Engine code reads records only — the "
            "promise, date and dispute annotations in there are exactly what this "
            "engine is supposed to derive from the reply text for itself."
        )
    if candidate.is_dir():
        matches = sorted(p for p in candidate.glob("*.jsonl"))
        if not matches:
            raise DatasetError(f"no .jsonl dataset in {candidate}")
        candidate = matches[0]
    if not candidate.exists():
        raise DatasetError(
            f"no invoice dataset at {candidate}. Generate one with "
            "`python -m data.generators.cli invoices --seed 42`."
        )
    return candidate


def manifest_for(path: Path | str | None = None) -> Path:
    """The manifest beside a dataset. **Scoring code only** — never the engine.

    Lives here rather than in `scoring.py` so the one legitimate path to a
    manifest is beside the refusal that guards every other one, and a reader
    comparing the two can see the split is deliberate.
    """
    dataset = resolve_dataset(path)
    return dataset.with_name(f"{dataset.stem}{MANIFEST_SUFFIX}")


def load_invoices(path: Path | str | None = None) -> list[Invoice]:
    """Read the ledger, ordered by id so a run is stable across filesystems."""
    dataset = resolve_dataset(path)
    invoices: list[Invoice] = []
    with dataset.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                invoices.append(_to_utc(Invoice.model_validate(json.loads(line))))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise DatasetError(_malformed(dataset, lineno, exc)) from exc
    if not invoices:
        raise DatasetError(f"{dataset} contains no records")
    return sorted(invoices, key=lambda i: i.invoice_id)


def _to_utc(invoice: Invoice) -> Invoice:
    """Normalize every timestamp to UTC on the way in.

    The records already carry a `Z` suffix, but a dataset written by a different
    serialiser would otherwise reach the promise arithmetic in local time — and a
    5h30m error is exactly large enough to move a committed date across the PP1
    grace boundary without looking obviously wrong.
    """
    return invoice.model_copy(
        update={
            "issued_at": invoice.issued_at.astimezone(UTC),
            "due_at": invoice.due_at.astimezone(UTC),
            "paid_at": invoice.paid_at.astimezone(UTC) if invoice.paid_at else None,
            "communications": [
                c.model_copy(update={"sent_at": c.sent_at.astimezone(UTC)})
                for c in invoice.communications
            ],
            "replies": [
                r.model_copy(update={"received_at": r.received_at.astimezone(UTC)})
                for r in invoice.replies
            ],
        }
    )


def dataset_batch_id(invoices: list[Invoice]) -> str:
    """The dataset's own batch id, read off the records.

    Distinct from an engine run's `batch_id`: one dataset feeds many runs, so
    conflating the two makes a reported number untraceable to the rows behind it.
    """
    ids = {i.batch_id for i in invoices}
    if len(ids) != 1:
        raise DatasetError(f"records span more than one dataset batch: {sorted(ids)}")
    return ids.pop()


def dataset_anchor(invoices: list[Invoice]) -> datetime:
    """The instant this ledger was built around, derived from the records.

    Engine 1 could default to the last timestamp in its dataset; Engine 2 could
    not, and neither can this one — but for the opposite reason. An invoice
    ledger's latest timestamp is the most recent *reply*, which by construction
    lands shortly before the generator's `as_of`. That is nearly right, and
    nearly right is where this goes wrong: several invoices carry a `due_at` in
    the future, so a clock derived only from replies can sit before an invoice
    was even due and the ageing arithmetic goes negative.

    The anchor is therefore the latest of every *observed* event — the last
    reply, the last outbound contact, the last payment. It is by construction at
    or just before `as_of`, and never after it, so a run defaults to the moment
    the ledger describes. It stays a defaulting convenience: `run()` takes `now`
    explicitly, and the demo exercises both.
    """
    observed: list[datetime] = []
    for invoice in invoices:
        observed.extend(c.sent_at for c in invoice.communications)
        observed.extend(r.received_at for r in invoice.replies)
        if invoice.paid_at is not None:
            observed.append(invoice.paid_at)
    if not observed:
        # A ledger with no contact and no payment at all: fall back to the last
        # due date, which is the only event the records can still vouch for.
        return max(i.due_at for i in invoices)
    # Round up to the next hour so a run clock never lands exactly on a recorded
    # event, where "at or after" comparisons become a coin toss.
    latest = max(observed)
    return (latest + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)



def _malformed(dataset: Path, lineno: int, exc: Exception) -> str:
    """A malformed record names the file and the line, and stops the run.

    Phase 7 §5.4 rehearses "empty or malformed input batch" as a failure the
    system must degrade cleanly on. A raw `JSONDecodeError` from inside a read
    loop is not clean: it says nothing about which file or which record, and it
    reaches the operator as a traceback through three frames of engine code.

    Failing here rather than skipping the line is deliberate, and is the same
    fail-closed direction the policy engine takes. A run that silently drops the
    records it could not read reports a recovery rate over a denominator nobody
    chose.
    """
    detail = str(exc).splitlines()[0]
    return (
        f"{dataset.name} line {lineno} is not a valid record: {detail}. "
        "Regenerate the dataset with `python -m data.generators.cli` rather than "
        "editing it by hand."
    )


__all__ = [
    "DEFAULT_DATASET",
    "DatasetError",
    "dataset_anchor",
    "dataset_batch_id",
    "load_invoices",
    "manifest_for",
    "resolve_dataset",
]
