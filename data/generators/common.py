"""Seeding, timestamps, hashing and manifests — the reproducibility backbone.

Every rule in here exists to defend one property: **same seed + same config =
byte-identical output**. Concretely that means no `random` module-level state, no
`datetime.now()` anywhere in generation logic (time is an input, read from the
config's `as_of`), and no dict iteration order leaking into a draw.

The only non-reproducible value any generator emits is the wall-clock time the
batch was written, and it is quarantined under the manifest's `nondeterministic`
key so the determinism boundary is explicit rather than a footnote.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Any, TypeVar

from data.generators import GENERATOR_VERSION, REPO_ROOT

T = TypeVar("T")

#: Fixed +05:30. India has observed no DST since 1945, so this is exact and
#: avoids `tzdata`, which is absent from a bare Windows Python (PHASE-LOG,
#: phase 1 deviation #6).
IST = timezone(timedelta(hours=5, minutes=30))

MANIFEST_VERSION = 1


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def derive_seed(seed: int, *labels: str) -> int:
    """Derive a stable sub-seed from a master seed and a label path.

    Each generation concern gets its own stream — timestamps, amounts, outcomes,
    reply text — so that adding a draw to one of them cannot shift every
    subsequent value in the others. Without this, appending a single field to a
    record silently changes an entire batch, and "regenerate and diff" stops
    being a usable check.
    """
    material = ":".join([str(seed), *labels]).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")


def rng(seed: int, *labels: str) -> Random:
    """A private, labelled random stream."""
    return Random(derive_seed(seed, *labels))


def weighted_choice(r: Random, options: Sequence[tuple[T, float]]) -> T:
    """Pick one option by weight, using an explicit cumulative walk.

    Deliberately not `random.choices`: this consumes exactly one `random()` draw
    per call, which keeps a stream's consumption pattern stable and therefore
    keeps output reproducible across Python versions.
    """
    total = sum(w for _, w in options)
    if total <= 0:
        raise ValueError("weighted_choice needs at least one positive weight")
    target = r.random() * total
    running = 0.0
    for value, weight in options:
        running += weight
        if target < running:
            return value
    return options[-1][0]


def weighted_from_mapping(r: Random, weights: Mapping[str, float]) -> str:
    """Same, for a name -> weight mapping. Sorted so dict order cannot leak in."""
    return weighted_choice(r, sorted(weights.items()))


def sample_bounded(r: Random, low: int, high: int, *, step: int = 1) -> int:
    """A uniform integer in `[low, high]`, snapped down to `step`.

    Used for money, where the step keeps amounts to whole rupees rather than
    producing paise values no real merchant would ever charge.
    """
    if high < low:
        raise ValueError(f"empty range: [{low}, {high}]")
    raw = low + int(r.random() * (high - low + 1))
    raw = min(raw, high)
    return (raw // step) * step


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def iso(dt: datetime) -> str:
    """Serialize as tz-aware UTC, always. Naive input is a bug, not a default.

    Truncated to whole seconds: sub-second precision on a scheduled debit or a
    reminder is noise that makes records harder to read and implies a precision
    the simulation does not have.
    """
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected — timestamps are tz-aware UTC (CLAUDE.md)")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from a config, rejecting naive values."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"config timestamp {value!r} has no timezone — say Z or +05:30")
    return parsed.astimezone(UTC)


def ist_wall_clock(day: datetime, hour: int, minute: int, second: int) -> datetime:
    """Build a UTC instant from an IST wall-clock time on `day`.

    Traffic shape is a *local* phenomenon — an Indian merchant's lunchtime peak
    is 13:00 IST, not 13:00 UTC — so the diurnal curve is applied in IST and only
    then converted. Storing anything but UTC would violate CLAUDE.md.
    """
    local = day.astimezone(IST)
    return local.replace(hour=hour, minute=minute, second=second, microsecond=0).astimezone(UTC)


def ist_hour(dt: datetime) -> int:
    """The IST hour a UTC instant falls in. Reporting and assertions only."""
    return dt.astimezone(IST).hour


# ---------------------------------------------------------------------------
# Hashing, config loading, output
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """A stable serialization for hashing: sorted keys, no incidental spacing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(obj: Any) -> str:
    """Short content hash of a config, for batch ids and manifests."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:12]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(path: str | Path) -> dict[str, Any]:
    """Read a generator config.

    JSON rather than YAML on purpose: YAML would mean a parser dependency that is
    not in `requirements.txt`, and a config a judge cannot load is worse than one
    with slightly noisier punctuation.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = (REPO_ROOT / resolved).resolve()
    with resolved.open(encoding="utf-8") as fh:
        return json.load(fh)


def make_batch_id(prefix: str, seed: int, config_fingerprint: str) -> str:
    """A batch id that is itself reproducible.

    Derived from the seed and the config rather than from a clock or a UUID, so
    two runs of the same world produce the same id — which is what lets a
    manifest and an audit trail written days apart be matched up.
    """
    return f"{prefix}-s{seed}-{config_fingerprint[:8]}"


def serialize_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render records one-per-line.

    JSONL because a few hundred rows stay browsable and diffable on GitHub, and a
    one-record change shows up as a one-line diff instead of a reflowed blob.
    Keys are sorted so two runs cannot differ by field order alone.
    """
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_text_file(path: Path, text: str) -> None:
    """Write with an explicit newline, so Windows checkouts hash identically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_manifest(
    *,
    dataset: str,
    batch_id: str,
    seed: int,
    generator_module: str,
    config_path: str,
    config: Mapping[str, Any],
    output_file: str,
    rows: int,
    output_sha256: str,
    measured: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    """Assemble the manifest that travels with a batch.

    Two properties matter here. First, everything except `nondeterministic` is a
    pure function of (seed, config, generator version) — so a manifest diff that
    is not confined to that key means the *world* changed, not just the clock.
    Second, `ground_truth` lives here and nowhere else: the engines read the
    records, never the manifest, which is what keeps a scored detection honest.
    """
    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset": dataset,
        "batch_id": batch_id,
        "seed": seed,
        "generator": {"module": generator_module, "version": GENERATOR_VERSION},
        "config": {
            "path": config_path,
            "fingerprint": fingerprint(config),
            "resolved": config,
        },
        "output": {"file": output_file, "rows": rows, "sha256": output_sha256},
        "measured": dict(measured),
        "ground_truth": dict(ground_truth),
        "nondeterministic": {"generated_at": iso(generated_at)},
    }


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def share(count: int, total: int) -> float:
    """A measured share, rounded for readability in a manifest."""
    return round(count / total, 4) if total else 0.0


def allocate(total: int, weights: Mapping[str, float], *, minimum: int = 1) -> dict[str, int]:
    """Split `total` across named buckets by weight, deterministically.

    Sampling would leave whether a cohort appears at all up to luck, and several
    acceptance criteria are exactly "this case is present" — a revoked mandate, a
    mandate at the attempt cap, a reply in Hinglish. Largest-remainder allocation
    with a floor of one guarantees coverage instead of hoping for it.
    """
    names = sorted(weights)
    if total < minimum * len(names):
        raise ValueError(
            f"cannot allocate {total} across {len(names)} buckets with a floor of {minimum}"
        )
    remaining = total - minimum * len(names)
    scale = sum(weights[n] for n in names) or 1.0
    exact = {n: remaining * weights[n] / scale for n in names}
    counts = {n: minimum + int(exact[n]) for n in names}
    shortfall = total - sum(counts.values())
    for name in sorted(names, key=lambda n: (-(exact[n] - int(exact[n])), n))[:shortfall]:
        counts[name] += 1
    return counts


@dataclass(frozen=True)
class GeneratedBatch:
    """One dataset plus the manifest that makes it checkable.

    Generation and writing are separate on purpose: the manifest carries the
    output's hash, so the bytes have to exist before the manifest is complete,
    and building both in memory first means a half-written batch never lands on
    disk next to a manifest describing something else.
    """

    dataset: str
    batch_id: str
    records: list[dict[str, Any]]
    payload: str
    manifest: dict[str, Any]

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        """Write `<dataset>.jsonl` and `<dataset>.manifest.json` into `out_dir`."""
        data_path = out_dir / self.manifest["output"]["file"]
        manifest_path = out_dir / f"{self.dataset}.manifest.json"
        write_text_file(data_path, self.payload)
        write_manifest(manifest_path, self.manifest)
        return data_path, manifest_path
