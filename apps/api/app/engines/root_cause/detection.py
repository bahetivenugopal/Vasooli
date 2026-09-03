"""Corridor segmentation and degradation detection — deterministic, no model.

The model explains; it does not detect. That split is the point of ADR 0006, and
it is what lets the answer to "how does your detection work?" be a paragraph of
statistics rather than "we asked an LLM". Detection here is reproducible,
testable, and identical on every run of the same data.

Three properties this module exists to guarantee:

1. **It never reads the ground-truth manifest.** The only inputs are payment
   records and the config. A test asserts it, because a detector that has seen
   the answer key measures nothing.
2. **A corridor is compared against its own past**, never a global constant.
   Corridors legitimately differ; a global threshold fires constantly on the low
   ones and never on the high ones (`corridor-detection:BW2`).
3. **Volume before significance.** A window under the minimum-volume guard is
   never flagged, whatever its success rate. This is the guard that keeps the
   Phase 2 decoy corridor quiet (`corridor-detection:MV1` / `MV2`).
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import comb

from app.engines.root_cause.config import CorridorLevel, DetectionConfig
from app.engines.root_cause.schemas import Corridor, Detection, PaymentAttempt


def binomial_at_most(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), computed exactly.

    Exact rather than a normal approximation on purpose: window sizes here are
    single digits to low tens, which is precisely the range where the
    approximation is worst and where an over-confident p-value would turn noise
    into a "detection". `math.comb` is cheap at these sizes.
    """
    if n <= 0:
        return 1.0
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    k = min(k, n)
    return min(1.0, sum(comb(n, i) * (p**i) * ((1 - p) ** (n - i)) for i in range(k + 1)))


def corridor_of(attempt: PaymentAttempt, level: CorridorLevel) -> Corridor | None:
    """The corridor this attempt belongs to at `level`, or None if it cannot be placed.

    A record missing a field the level keys on is skipped rather than bucketed
    under a null — a "corridor" of everything-with-no-issuer is not a corridor,
    and flagging it would be a false positive with a straight face.
    """
    values: dict[str, str | None] = {}
    for field in level.fields:
        value = getattr(attempt, field, None)
        if value is None:
            return None
        values[field] = value
    return Corridor(level=level.name, **values)


def _detection_id(corridor: Corridor, window_start: datetime) -> str:
    """A stable id derived from the corridor and the window it opened on.

    Derived rather than sequential so re-running the same batch produces the same
    detection ids, which is what lets two runs be diffed entry for entry.
    """
    digest = hashlib.sha256(
        f"{corridor.key}|{window_start.isoformat()}".encode()
    ).hexdigest()
    return f"det_{digest[:12]}"


class CorridorDetector:
    """Rolling-window degradation detection over a payments batch."""

    def __init__(self, config: DetectionConfig) -> None:
        self._config = config

    @property
    def config(self) -> DetectionConfig:
        return self._config

    def detect(self, attempts: list[PaymentAttempt]) -> list[Detection]:
        """Every degradation episode in the batch, oldest first.

        Overlapping windows on the same corridor are merged into one episode:
        without that, a twelve-hour outage reports as five separate "detections"
        and the precision figure becomes meaningless.
        """
        if not attempts:
            return []

        ordered = sorted(attempts, key=lambda a: a.created_at)
        detections: list[Detection] = []
        for level in self._config.levels:
            detections.extend(self._detect_at_level(ordered, level))
        return sorted(detections, key=lambda d: (d.window_start, d.corridor.key))

    # --- internals ---------------------------------------------------------

    def _detect_at_level(
        self, ordered: list[PaymentAttempt], level: CorridorLevel
    ) -> list[Detection]:
        buckets: dict[Corridor, list[PaymentAttempt]] = defaultdict(list)
        for attempt in ordered:
            corridor = corridor_of(attempt, level)
            if corridor is not None:
                buckets[corridor].append(attempt)

        first = ordered[0].created_at
        last = ordered[-1].created_at

        detections: list[Detection] = []
        for corridor, rows in buckets.items():
            windows = self._flag_windows(corridor, rows, first, last)
            detections.extend(self._merge(corridor, windows, rows))
        return detections

    def _flag_windows(
        self,
        corridor: Corridor,
        rows: list[PaymentAttempt],
        first: datetime,
        last: datetime,
    ) -> list[tuple[datetime, datetime, float]]:
        """Every window on this corridor that clears volume *and* significance."""
        cfg = self._config
        flagged: list[tuple[datetime, datetime, float]] = []
        start = first
        while start <= last:
            end = start + cfg.window
            window = [r for r in rows if start <= r.created_at < end]
            baseline = [r for r in rows if r.created_at < start]
            start += cfg.stride

            # Volume first, always. `corridor-detection:MV1` / `MV2`.
            if len(window) < cfg.min_window_attempts:
                continue
            if len(baseline) < cfg.min_baseline_attempts:
                continue

            window_rate = sum(1 for r in window if r.succeeded) / len(window)
            baseline_rate = sum(1 for r in baseline if r.succeeded) / len(baseline)
            if baseline_rate <= 0:
                continue

            absolute = baseline_rate - window_rate
            if absolute < cfg.min_absolute_drop:
                continue
            if absolute / baseline_rate < cfg.min_relative_drop:
                continue

            p_value = binomial_at_most(
                sum(1 for r in window if r.succeeded), len(window), baseline_rate
            )
            if p_value > cfg.max_p_value:
                continue

            flagged.append((end - cfg.window, end, p_value))
        return flagged

    def _merge(
        self,
        corridor: Corridor,
        windows: list[tuple[datetime, datetime, float]],
        rows: list[PaymentAttempt],
    ) -> list[Detection]:
        """Collapse overlapping flagged windows into detection episodes."""
        episodes: list[tuple[datetime, datetime, float]] = []
        for start, end, p_value in sorted(windows):
            if episodes and start <= episodes[-1][1]:
                prev_start, prev_end, prev_p = episodes[-1]
                episodes[-1] = (prev_start, max(prev_end, end), min(prev_p, p_value))
            else:
                episodes.append((start, end, p_value))

        return [
            self._build(corridor, start, end, p_value, rows)
            for start, end, p_value in episodes
        ]

    def _build(
        self,
        corridor: Corridor,
        start: datetime,
        end: datetime,
        p_value: float,
        rows: list[PaymentAttempt],
    ) -> Detection:
        window = [r for r in rows if start <= r.created_at < end]
        baseline = [r for r in rows if r.created_at < start]
        failures = [r for r in window if r.failed]

        window_successes = sum(1 for r in window if r.succeeded)
        baseline_successes = sum(1 for r in baseline if r.succeeded)

        cfg = self._config
        return Detection(
            detection_id=_detection_id(corridor, start),
            corridor=corridor,
            window_start=start,
            window_end=end,
            window_attempts=len(window),
            window_successes=window_successes,
            baseline_attempts=len(baseline),
            baseline_successes=baseline_successes,
            observed_success_rate=round(window_successes / len(window), 4),
            baseline_success_rate=round(baseline_successes / len(baseline), 4),
            p_value=round(p_value, 6),
            confidence=round(1.0 - p_value, 6),
            affected_attempts=len(failures),
            value_at_risk_paise=sum(r.amount_paise for r in failures),
            decline_mix=dict(
                Counter(r.failure_reason_code or "UNSPECIFIED" for r in failures)
            ),
            distinct_failing_customers=len({r.customer_id for r in failures}),
            cited_rules=[
                cfg.window_rule,
                cfg.volume_rule,
                cfg.significance_rule,
            ],
        )


def unaffected_corridor_rates(
    attempts: list[PaymentAttempt],
    detection: Detection,
    level_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    """Success rates of *other* corridors over the same window.

    Handed to the diagnosis layer so the model can answer the question that
    actually separates systemic from individual: is everything else fine right
    now, or is the whole platform having a bad hour?
    """
    window = [
        a
        for a in attempts
        if detection.window_start <= a.created_at < detection.window_end
    ]
    buckets: dict[tuple[str | None, ...], list[PaymentAttempt]] = defaultdict(list)
    for attempt in window:
        key = tuple(getattr(attempt, field, None) for field in level_fields)
        if any(part is None for part in key):
            continue
        buckets[key].append(attempt)

    target = tuple(getattr(detection.corridor, field) for field in level_fields)
    rows: list[dict[str, object]] = []
    for key, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if key == target:
            continue
        rows.append(
            {
                "corridor": " x ".join(str(part) for part in key),
                "attempts": len(group),
                "success_rate": round(
                    sum(1 for r in group if r.succeeded) / len(group), 4
                ),
            }
        )
    return rows


def alternate_routes(
    attempts: list[PaymentAttempt], detection: Detection, before: datetime
) -> list[dict[str, object]]:
    """Routes for this corridor's method other than the degraded one, best first.

    Ranked by success rate on evidence available *before* the reroute is
    authorised — using the whole dataset would rank routes on traffic that had
    not happened yet, which is the same mistake as reading the manifest.

    An empty list is a meaningful answer: `corridor-detection:RR3` fails a
    reroute closed when there is nowhere to send traffic.
    """
    method = detection.corridor.method
    degraded = detection.corridor.route_id
    if method is None:
        return []

    buckets: dict[str, list[PaymentAttempt]] = defaultdict(list)
    for attempt in attempts:
        if attempt.method != method or attempt.route_id is None:
            continue
        if attempt.created_at >= before:
            continue
        buckets[attempt.route_id].append(attempt)

    candidates = [
        {
            "route_id": route_id,
            "attempts": len(rows),
            "success_rate": round(sum(1 for r in rows if r.succeeded) / len(rows), 4),
        }
        for route_id, rows in buckets.items()
        if route_id != degraded and rows
    ]
    if degraded is None:
        # An issuer-level corridor spans every route. Rerouting means moving that
        # issuer's traffic to whichever route is doing best, so the degraded
        # route is not excluded — but a corridor with only one route to choose
        # from still has nowhere to go, and RR3 will refuse it.
        candidates = [c for c in candidates if len(buckets) > 1]

    return sorted(
        candidates, key=lambda c: (-float(c["success_rate"]), str(c["route_id"]))
    )


def window_failures(
    attempts: list[PaymentAttempt], detection: Detection
) -> list[PaymentAttempt]:
    """The failed attempts a detection concerns."""
    return [
        a
        for a in attempts
        if a.failed
        and detection.corridor.matches(a)
        and detection.window_start <= a.created_at < detection.window_end
    ]


def batch_span(attempts: list[PaymentAttempt]) -> tuple[datetime, datetime]:
    """First and last attempt timestamps. The engine's clock comes from here.

    Phase 2's datasets are anchored to a fixed `as_of` that is *not* today, and
    `as_of` lives in the manifest, which engine code may not read. Deriving the
    run's `now` from the records themselves keeps the ground-truth separation
    intact and still puts the engine in the same week as the data.
    """
    ordered = sorted(a.created_at for a in attempts)
    return ordered[0], ordered[-1]


def default_window_hours(config: DetectionConfig) -> float:
    return config.window / timedelta(hours=1)
