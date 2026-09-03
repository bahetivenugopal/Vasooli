"""Reading the payments dataset.

One rule, and it is the reason this module is separate from `scoring.py`:
**engine code opens `.jsonl` files and never a manifest.** The manifest is where
the generator wrote down which corridors it degraded, and a detector that has
seen that has measured nothing.

`resolve_dataset()` will happily accept a directory and find the `.jsonl` inside
it, and will refuse to hand back a manifest path even if asked directly.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

from app.engines.root_cause.schemas import PaymentAttempt

#: The repo root, four levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATASET = REPO_ROOT / "data" / "samples" / "payments" / "payments.jsonl"
MANIFEST_SUFFIX = ".manifest.json"


class DatasetError(RuntimeError):
    """Raised when the dataset is missing, or when engine code reaches for a manifest."""


def resolve_dataset(path: Path | str | None = None) -> Path:
    """The `.jsonl` file to read.

    Refusing a manifest path here rather than trusting every caller is deliberate:
    the ground-truth separation is the property the whole detection metric rests
    on, and a guard at the door is cheaper than a convention.
    """
    candidate = Path(path) if path else DEFAULT_DATASET
    if candidate.name.endswith(MANIFEST_SUFFIX):
        raise DatasetError(
            f"{candidate.name} is a manifest. Engine code reads records only — "
            "ground truth is for scoring, in `scoring.py`."
        )
    if candidate.is_dir():
        matches = sorted(p for p in candidate.glob("*.jsonl"))
        if not matches:
            raise DatasetError(f"no .jsonl dataset in {candidate}")
        candidate = matches[0]
    if not candidate.exists():
        raise DatasetError(
            f"no payments dataset at {candidate}. Generate one with "
            "`python -m data.generators.cli payments --seed 42`."
        )
    return candidate


def manifest_for(dataset: Path) -> Path:
    """The manifest beside a dataset. **Scoring code only.**"""
    return dataset.with_name(f"{dataset.stem}{MANIFEST_SUFFIX}")


def load_attempts(path: Path | str | None = None) -> list[PaymentAttempt]:
    """Read the payments batch, oldest first."""
    dataset = resolve_dataset(path)
    attempts: list[PaymentAttempt] = []
    with dataset.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            attempts.append(PaymentAttempt.model_validate(json.loads(line)))
    if not attempts:
        raise DatasetError(f"{dataset} contains no records")
    return sorted(
        (a.model_copy(update={"created_at": a.created_at.astimezone(UTC)}) for a in attempts),
        key=lambda a: a.created_at,
    )


def dataset_batch_id(attempts: list[PaymentAttempt]) -> str:
    """The dataset's own batch id, read off the records.

    Distinct from an engine run's `batch_id`: one dataset can feed many runs, so
    conflating the two would make a reported number untraceable to the rows it
    came from. The run stores this in `BatchRun.notes`.
    """
    ids = {a.batch_id for a in attempts}
    if len(ids) != 1:
        raise DatasetError(f"records span more than one dataset batch: {sorted(ids)}")
    return ids.pop()
