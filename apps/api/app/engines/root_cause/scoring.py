"""Detection accuracy against the generator's ground truth.

**This is the only module in Engine 1 that reads a manifest.** The detector does
not, cannot, and a test asserts it. Keeping the answer key in the marking code
rather than the exam is what makes the reported precision and recall mean
anything.

Matching is deliberately strict. A ground-truth degradation names the fields that
define it — `{issuer: HDFC, method: upi}` for an issuer rail, `{method: card,
route_id: rt_card_acq_2}` for an acquirer — and a detection matches only if it
names the same values for every one of those fields. A detection that fires on
the right method but localises it to the wrong issuer is a false positive, not a
partial credit. Anything looser would let the engine claim a hit for noticing
that *something* was wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engines.root_cause.schemas import Detection, DetectionScore

CORRIDOR_FIELDS = ("issuer", "method", "route_id")


class GroundTruthError(RuntimeError):
    """Raised when a manifest is missing or is not a payments manifest."""


def load_manifest(path: Path | str) -> dict[str, Any]:
    """Read a generator manifest. Evaluation code only."""
    resolved = Path(path)
    if not resolved.exists():
        raise GroundTruthError(
            f"no manifest at {resolved}. Detection metrics need the generator's "
            "ground truth; without it a run can report what it did but not "
            "whether it was right."
        )
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    if manifest.get("dataset") != "payments":
        raise GroundTruthError(
            f"{resolved} is a {manifest.get('dataset')!r} manifest, not payments"
        )
    return manifest


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _corridor_of(detection: Detection) -> dict[str, str | None]:
    return {
        "issuer": detection.corridor.issuer,
        "method": detection.corridor.method,
        "route_id": detection.corridor.route_id,
    }


def _matches(detection: Detection, truth: dict[str, Any]) -> bool:
    """Does this detection name the same corridor as the ground-truth event?

    Only the fields the ground truth specifies are compared. A `null` there means
    "this dimension is not what defines the event" — an issuer rail degrades on
    every route — so a detection is not penalised for leaving it unset, but it
    *is* penalised for setting it to something the truth did not name.
    """
    corridor = _corridor_of(detection)
    return all(
        corridor.get(field) == value
        for field, value in truth.items()
        if field in CORRIDOR_FIELDS and value is not None
    )


def _overlaps(detection: Detection, start: datetime, end: datetime) -> bool:
    return detection.window_start < end and detection.window_end > start


def score_detections(
    detections: list[Detection], manifest: dict[str, Any]
) -> DetectionScore:
    """True positives, false positives, false negatives and detection latency.

    Latency is measured from the *onset* of the degradation to the *end* of the
    window that first caught it — the earliest moment a detector working on
    completed windows could have known. Measuring from the window's start would
    flatter the engine by crediting it with evidence it had not yet seen.
    """
    truth = manifest.get("ground_truth", {})
    degradations = truth.get("degradations", [])
    decoys = truth.get("decoys", [])

    matched: list[dict[str, object]] = []
    latency: dict[str, float] = {}
    matched_ids: set[str] = set()
    hit_labels: set[str] = set()

    for event in degradations:
        corridor = event.get("corridor", {})
        start = _parse(event["window"]["starts_at"])
        end = _parse(event["window"]["ends_at"])
        label = event["label"]
        for detection in sorted(detections, key=lambda d: d.window_end):
            if not _matches(detection, corridor) or not _overlaps(detection, start, end):
                continue
            matched_ids.add(detection.detection_id)
            if label not in hit_labels:
                hit_labels.add(label)
                latency[label] = round(
                    (detection.window_end - start).total_seconds() / 3600, 2
                )
                matched.append(
                    {
                        "label": label,
                        "detection_id": detection.detection_id,
                        "corridor": detection.corridor.label,
                        "level": detection.corridor.level,
                        "observed_success_rate": detection.observed_success_rate,
                        "baseline_success_rate": detection.baseline_success_rate,
                        "window_attempts": detection.window_attempts,
                        "statistical_confidence": detection.confidence,
                        "detection_latency_hours": latency[label],
                    }
                )

    unmatched = [d for d in detections if d.detection_id not in matched_ids]
    decoy_hits = [
        d
        for d in unmatched
        if any(_matches(d, decoy.get("corridor", {})) for decoy in decoys)
    ]
    missed = [e["label"] for e in degradations if e["label"] not in hit_labels]

    true_positives = len(hit_labels)
    false_positives = len(unmatched)
    false_negatives = len(missed)

    return DetectionScore(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=_ratio(true_positives, true_positives + false_positives),
        recall=_ratio(true_positives, true_positives + false_negatives),
        detection_latency_hours=latency,
        matched=matched,
        unmatched_detections=[
            {
                "detection_id": d.detection_id,
                "corridor": d.corridor.label,
                "level": d.corridor.level,
                "window_start": d.window_start.isoformat(),
                "window_end": d.window_end.isoformat(),
                "window_attempts": d.window_attempts,
                "observed_success_rate": d.observed_success_rate,
                "baseline_success_rate": d.baseline_success_rate,
                "statistical_confidence": d.confidence,
            }
            for d in unmatched
        ],
        missed_degradations=missed,
        decoy_false_positives=len(decoy_hits),
        ground_truth_source=str(manifest.get("batch_id", "unknown")),
    )


def _ratio(numerator: int, denominator: int) -> float:
    """Zero denominator means zero, not one.

    A precision of 100% because nothing was detected is the kind of number that
    gets quoted by accident.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)
