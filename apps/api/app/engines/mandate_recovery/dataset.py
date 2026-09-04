"""Reading the mandate dataset.

Same rule as Engine 1, for the same reason: **engine code opens `.jsonl` files
and never a manifest.** The mandates manifest records each record's cohort, its
notice profile and whether its registration was deliberately broken — an engine
that has seen those has derived nothing.

`resolve_dataset()` accepts a directory and finds the `.jsonl` inside it, and
refuses a manifest path even when asked directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.engines.mandate_recovery.schemas import Mandate

#: The repo root, five levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATASET = REPO_ROOT / "data" / "samples" / "mandates" / "mandates.jsonl"
MANIFEST_SUFFIX = ".manifest.json"


class DatasetError(RuntimeError):
    """Raised when the dataset is missing, or when engine code reaches for a manifest."""


def resolve_dataset(path: Path | str | None = None) -> Path:
    """The `.jsonl` file to read."""
    candidate = Path(path) if path else DEFAULT_DATASET
    if candidate.name.endswith(MANIFEST_SUFFIX):
        raise DatasetError(
            f"{candidate.name} is a manifest. Engine code reads records only — the "
            "cohort labels and notice profiles in there are what this engine is "
            "supposed to derive for itself."
        )
    if candidate.is_dir():
        matches = sorted(p for p in candidate.glob("*.jsonl"))
        if not matches:
            raise DatasetError(f"no .jsonl dataset in {candidate}")
        candidate = matches[0]
    if not candidate.exists():
        raise DatasetError(
            f"no mandate dataset at {candidate}. Generate one with "
            "`python -m data.generators.cli mandates --seed 42`."
        )
    return candidate


def load_mandates(path: Path | str | None = None) -> list[Mandate]:
    """Read the mandate book, ordered by id so a run is stable across filesystems."""
    dataset = resolve_dataset(path)
    mandates: list[Mandate] = []
    with dataset.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                mandates.append(_to_utc(Mandate.model_validate(json.loads(line))))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise DatasetError(_malformed(dataset, lineno, exc)) from exc
    if not mandates:
        raise DatasetError(f"{dataset} contains no records")
    return sorted(mandates, key=lambda m: m.mandate_id)


def _to_utc(mandate: Mandate) -> Mandate:
    """Normalize every timestamp to UTC on the way in.

    The records are written with a `Z` suffix already, but a dataset produced
    with a different serialiser would otherwise reach the notice-window
    arithmetic in local time — where a 5h30m error is exactly large enough to
    move a notice across the A2 boundary without looking obviously wrong.
    """
    return mandate.model_copy(
        update={
            "registered_at": mandate.registered_at.astimezone(UTC),
            "cycle_started_at": mandate.cycle_started_at.astimezone(UTC),
            "next_debit_at": mandate.next_debit_at.astimezone(UTC),
            "next_debit_notice_sent_at": (
                mandate.next_debit_notice_sent_at.astimezone(UTC)
                if mandate.next_debit_notice_sent_at
                else None
            ),
            "debit_history": [
                attempt.model_copy(
                    update={
                        "scheduled_at": attempt.scheduled_at.astimezone(UTC),
                        "attempted_at": attempt.attempted_at.astimezone(UTC),
                        "notice_sent_at": (
                            attempt.notice_sent_at.astimezone(UTC)
                            if attempt.notice_sent_at
                            else None
                        ),
                    }
                )
                for attempt in mandate.debit_history
            ],
        }
    )


def dataset_batch_id(mandates: list[Mandate]) -> str:
    """The dataset's own batch id, read off the records.

    Distinct from an engine run's `batch_id`: one dataset feeds many runs, so
    conflating the two makes a reported number untraceable to the rows behind it.
    """
    ids = {m.batch_id for m in mandates}
    if len(ids) != 1:
        raise DatasetError(f"records span more than one dataset batch: {sorted(ids)}")
    return ids.pop()


def dataset_anchor(mandates: list[Mandate]) -> datetime:
    """The instant this book was built around, derived from the records.

    Engine 1 could default its clock to the last timestamp in the dataset. Engine
    2 cannot: every mandate's `next_debit_at` is in the *future* relative to the
    generator's `as_of`, so the latest timestamp in the file lands up to 56 hours
    past the moment the book describes, and every pre-debit notice looks stale
    against a clock that has already run past its debit.

    The anchor is instead the **latest debit actually attempted**, which is by
    construction a little behind `as_of` and never ahead of it. Erring early is
    the safe direction: a clock that is behind can only make the engine more
    conservative about outreach, and can never fabricate elapsed cooldown it has
    not earned. It is a defaulting convenience — `run()` takes `now` explicitly.
    """
    attempted = [a.attempted_at for m in mandates for a in m.debit_history]
    if attempted:
        return max(attempted)
    return min(m.cycle_started_at for m in mandates)


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
