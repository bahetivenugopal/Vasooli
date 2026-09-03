"""Payments generator — the stream Engine 1 watches.

Produces a time-ordered stream of payment attempts across a simulated merchant's
traffic, with three properties that make the detection problem real:

- **A diurnal curve.** Attempts are not uniform across the day, so a naive
  "failure *count* spiked" detector fires at 20:00 IST on a perfectly healthy
  corridor. Only a rate-based detector survives this data. That difficulty is
  deliberate.
- **Injected degradation.** One or more (issuer x method) or (route) corridors
  have their success rate collapse for a bounded window. Which corridor, which
  window, and what the degraded rate was live in the **manifest only** — never in
  a record — so Engine 1's detection can be scored against ground truth it could
  not have read.
- **A decoy.** A genuinely low-volume corridor where ordinary variance looks like
  degradation. Flagging it is a false positive, and the honest thing to do is
  report that it happened rather than remove the trap.

Nothing here decides whether a *retry* would succeed — see `retry_model.py` and
ADR 0005. This module generates the world, not the outcome.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from random import Random
from typing import Any

from app.models.enums import DeclineClass
from data.generators.common import (
    GeneratedBatch,
    build_manifest,
    fingerprint,
    iso,
    ist_hour,
    ist_wall_clock,
    make_batch_id,
    parse_ts,
    rng,
    sample_bounded,
    serialize_jsonl,
    sha256_text,
    share,
    weighted_choice,
    weighted_from_mapping,
)
from data.generators.vocabulary import (
    UNMAPPED_RAW_REASONS,
    decline_class_of,
    razorpay_reason_for,
    validate_codes,
)

MODULE = "data.generators.payments"
DATASET = "payments"

#: Amounts are whole rupees. A merchant charging 43,271 paise does not exist.
AMOUNT_STEP_PAISE = 100


# ---------------------------------------------------------------------------
# Config validation — fail before a single record is written
# ---------------------------------------------------------------------------


def _validate(config: dict[str, Any]) -> None:
    """Reject a config that would silently produce unusable data.

    Every check here corresponds to a way a batch can look fine and be worthless:
    a decline code the engines cannot classify, a class the method mix can never
    populate, or a diurnal curve that is not actually a day.
    """
    validate_codes([entry["code"] for entry in config["failure_codes"]])
    validate_codes(
        [
            d["dominant_failure_code"]
            for d in config["degradations"]
            if d.get("dominant_failure_code")
        ]
    )

    if len(config["diurnal_weights_ist"]) != 24:
        raise ValueError("diurnal_weights_ist must have exactly 24 entries, one per IST hour")

    methods = sorted(config["methods"])
    for cls, weight in config["failure_class_mix"].items():
        if weight <= 0 or cls == DeclineClass.UNKNOWN.value:
            continue
        for method in methods:
            if not _codes_for(config, cls, method):
                raise ValueError(
                    f"failure_class_mix gives {cls} a positive weight but no "
                    f"failure_codes entry covers method {method!r} — that draw "
                    "would have nothing to pick."
                )

    for entry in config["failure_codes"]:
        actual = decline_class_of(entry["code"]).value
        if actual != entry["class"]:
            raise ValueError(
                f"config says {entry['code']} is {entry['class']}, the taxonomy "
                f"says {actual}. The taxonomy wins — fix the config."
            )


def _codes_for(config: dict[str, Any], decline_class: str, method: str) -> list[tuple[str, float]]:
    return [
        (entry["code"], entry["weight"])
        for entry in config["failure_codes"]
        if entry["class"] == decline_class and method in entry["methods"]
    ]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _pick_failure(
    r_class: Random,
    r_code: Random,
    config: dict[str, Any],
    method: str,
    *,
    forced_code: str | None = None,
) -> tuple[str, str | None]:
    """Choose `(normalized_code, razorpay_reason)` for one failed attempt.

    `razorpay_reason` is `None` unless the code has a doc-verified upstream
    reason — see `vocabulary.py`. The UNKNOWN branch is the opposite case: a raw
    reason on purpose absent from the mapping table, so the fail-safe path gets
    exercised by data rather than only by a unit test.
    """
    if forced_code is not None:
        return forced_code, razorpay_reason_for(forced_code)

    decline_class = weighted_from_mapping(r_class, config["failure_class_mix"])
    if decline_class == DeclineClass.UNKNOWN.value:
        reasons = config.get("unknown_raw_reasons") or list(UNMAPPED_RAW_REASONS)
        raw = weighted_choice(r_code, [(reason, 1.0) for reason in sorted(reasons)])
        return DeclineClass.UNKNOWN.value, raw

    options = _codes_for(config, decline_class, method)
    code = weighted_choice(r_code, options)
    return code, razorpay_reason_for(code)


def _degradation_for(
    config: dict[str, Any], method: str, issuer: str, route_id: str, at: datetime
) -> dict[str, Any] | None:
    """The injected event covering this attempt, if any.

    A degradation matches on the dimensions it names and ignores the ones it
    leaves `null`: an issuer-level event covers every route, a route-level event
    covers every issuer. That is what makes the two events in the default config
    genuinely different detection problems.
    """
    for event in config["degradations"]:
        if event["method"] != method:
            continue
        if event.get("issuer") and event["issuer"] != issuer:
            continue
        if event.get("route_id") and event["route_id"] != route_id:
            continue
        if parse_ts(event["starts_at"]) <= at < parse_ts(event["ends_at"]):
            return event
    return None


def _attempt_timestamps(r: Random, config: dict[str, Any]) -> list[datetime]:
    """Every attempt instant in the window, shaped by the IST diurnal curve."""
    start = parse_ts(config["window_start"])
    weights = list(enumerate(config["diurnal_weights_ist"]))
    per_day = config["attempts_per_day"]
    jitter = config.get("attempts_per_day_jitter", 0.0)

    stamps: list[datetime] = []
    for day_index in range(config["days"]):
        day = start + timedelta(days=day_index)
        count = per_day
        if jitter:
            count = int(round(per_day * (1.0 + (r.random() * 2 - 1) * jitter)))
        for _ in range(max(count, 0)):
            hour = weighted_choice(r, weights)
            stamps.append(ist_wall_clock(day, hour, int(r.random() * 60), int(r.random() * 60)))
    return stamps


def _decoy_timestamps(r: Random, config: dict[str, Any], decoy: dict[str, Any]) -> list[datetime]:
    start = parse_ts(config["window_start"])
    weights = list(enumerate(config["diurnal_weights_ist"]))
    stamps: list[datetime] = []
    for day_index in range(config["days"]):
        day = start + timedelta(days=day_index)
        for _ in range(decoy["attempts_per_day"]):
            hour = weighted_choice(r, weights)
            stamps.append(ist_wall_clock(day, hour, int(r.random() * 60), int(r.random() * 60)))
    return stamps


def _ensure_unknown_coverage(raw: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """Guarantee at least one unmappable decline reason in the batch.

    The `UNKNOWN` fail-safe — an unrecognised reason is treated as HARD, never as
    soft — is one of the taxonomy's load-bearing claims. At a few hundred rows the
    configured 4% UNKNOWN share can easily produce zero of them on a given seed,
    and a claim that no batch exercises is a claim nobody has checked.

    So: if the draw produced none, the **last** failed attempt in the stream is
    converted. Deterministic, documented in the data card, and it moves one
    record rather than reshaping a distribution.
    """
    if any(row["failure_reason_code"] == DeclineClass.UNKNOWN.value for row in raw):
        return
    reasons = sorted(config.get("unknown_raw_reasons") or UNMAPPED_RAW_REASONS)
    for row in reversed(raw):
        if not row["succeeded"]:
            row["failure_reason_code"] = DeclineClass.UNKNOWN.value
            row["razorpay_reason"] = reasons[0]
            return


def generate(
    config: dict[str, Any],
    seed: int,
    *,
    config_path: str,
    generated_at: datetime,
) -> GeneratedBatch:
    """Build one reproducible payments batch."""
    _validate(config)

    batch_id = make_batch_id(config["batch_prefix"], seed, fingerprint(config))

    r_time = rng(seed, DATASET, "timestamps")
    r_route = rng(seed, DATASET, "routing")
    r_amount = rng(seed, DATASET, "amounts")
    r_outcome = rng(seed, DATASET, "outcomes")
    r_class = rng(seed, DATASET, "failure_class")
    r_code = rng(seed, DATASET, "failure_code")
    r_customer = rng(seed, DATASET, "customers")

    method_weights = {name: spec["weight"] for name, spec in config["methods"].items()}
    pool = config["customer_pool"]

    raw: list[dict[str, Any]] = []

    for stamp in _attempt_timestamps(r_time, config):
        method = weighted_from_mapping(r_route, method_weights)
        spec = config["methods"][method]
        bank = weighted_choice(r_route, [(b, b["weight"]) for b in spec["banks"]])
        route = weighted_choice(r_route, [(rt, rt["weight"]) for rt in spec["routes"]])
        bucket = weighted_choice(
            r_amount, [(b, b["weight"]) for b in config["amount_buckets"]]
        )
        amount = sample_bounded(
            r_amount, bucket["min_paise"], bucket["max_paise"], step=AMOUNT_STEP_PAISE
        )

        event = _degradation_for(config, method, bank["code"], route["id"], stamp)
        if event is not None:
            success_rate = event["degraded_success_rate"]
        else:
            success_rate = spec["baseline_success_rate"] + bank["success_delta"]

        succeeded = r_outcome.random() < success_rate
        forced = None
        if not succeeded and event is not None and r_code.random() < event["dominant_share"]:
            forced = event["dominant_failure_code"]

        code, razorpay_reason = (
            (None, None)
            if succeeded
            else _pick_failure(r_class, r_code, config, method, forced_code=forced)
        )

        raw.append(
            {
                "created_at": stamp,
                "customer_id": f"cust_{int(r_customer.random() * pool):05d}",
                "method": method,
                "issuer": bank["code"],
                "issuer_name": bank["name"],
                "route_id": route["id"],
                "amount_paise": amount,
                "succeeded": succeeded,
                "failure_reason_code": code,
                "razorpay_reason": razorpay_reason,
                "_corridor_kind": "primary",
                "_decoy_label": None,
            }
        )

    for decoy in config.get("decoys", []):
        r_decoy = rng(seed, DATASET, "decoy", decoy["label"])
        for stamp in _decoy_timestamps(r_decoy, config, decoy):
            bucket = weighted_choice(
                r_amount, [(b, b["weight"]) for b in config["amount_buckets"]]
            )
            amount = sample_bounded(
                r_amount, bucket["min_paise"], bucket["max_paise"], step=AMOUNT_STEP_PAISE
            )
            succeeded = r_outcome.random() < decoy["baseline_success_rate"]
            code, razorpay_reason = (
                (None, None)
                if succeeded
                else _pick_failure(r_class, r_code, config, decoy["method"])
            )
            raw.append(
                {
                    "created_at": stamp,
                    "customer_id": f"cust_{int(r_customer.random() * pool):05d}",
                    "method": decoy["method"],
                    "issuer": decoy["issuer"],
                    "issuer_name": decoy["issuer_name"],
                    "route_id": decoy["route_id"],
                    "amount_paise": amount,
                    "succeeded": succeeded,
                    "failure_reason_code": code,
                    "razorpay_reason": razorpay_reason,
                    "_corridor_kind": "decoy",
                    "_decoy_label": decoy["label"],
                }
            )

    # Sort by instant, then by every field, so ties resolve identically on every
    # machine — an unstable sort here would break byte-identical reproduction.
    raw.sort(
        key=lambda row: (
            row["created_at"],
            row["customer_id"],
            row["method"],
            row["amount_paise"],
        )
    )

    _ensure_unknown_coverage(raw, config)

    records: list[dict[str, Any]] = []
    for index, row in enumerate(raw, start=1):
        records.append(
            {
                "attempt_id": f"att_{index:05d}",
                "batch_id": batch_id,
                "created_at": iso(row["created_at"]),
                "customer_id": row["customer_id"],
                "method": row["method"],
                "issuer": row["issuer"],
                "issuer_name": row["issuer_name"],
                "route_id": row["route_id"],
                "amount_paise": row["amount_paise"],
                "currency": "INR",
                "status": "success" if row["succeeded"] else "failed",
                "failure_reason_code": row["failure_reason_code"],
                "razorpay_reason": row["razorpay_reason"],
            }
        )

    payload = serialize_jsonl(records)
    manifest = build_manifest(
        dataset=DATASET,
        batch_id=batch_id,
        seed=seed,
        generator_module=MODULE,
        config_path=config_path,
        config=config,
        output_file=f"{DATASET}.jsonl",
        rows=len(records),
        output_sha256=sha256_text(payload),
        measured=_measure(records),
        ground_truth=_ground_truth(config, raw),
        generated_at=generated_at,
    )
    return GeneratedBatch(
        dataset=DATASET, batch_id=batch_id, records=records, payload=payload, manifest=manifest
    )


# ---------------------------------------------------------------------------
# Measurement — what the batch actually contains, not what was intended
# ---------------------------------------------------------------------------


def _measure(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Distributions computed from the output, so the manifest cannot flatter it.

    Everything here is measured after the fact. If a configured 80% soft share
    comes out at 76%, the manifest says 76% — the intended figure is in the
    config, a few lines above it, for anyone who wants to compare.
    """
    total = len(records)
    failures = [r for r in records if r["status"] == "failed"]

    by_method: dict[str, dict[str, Any]] = {}
    for method in sorted({r["method"] for r in records}):
        rows = [r for r in records if r["method"] == method]
        wins = sum(1 for r in rows if r["status"] == "success")
        by_method[method] = {
            "attempts": len(rows),
            "success_rate": share(wins, len(rows)),
        }

    class_counts = Counter(decline_class_of(r["failure_reason_code"]).value for r in failures)
    hour_counts = Counter(ist_hour(parse_ts(r["created_at"])) for r in records)

    return {
        "attempts": total,
        "successes": total - len(failures),
        "failures": len(failures),
        "overall_success_rate": share(total - len(failures), total),
        "success_rate_by_method": by_method,
        "failure_class_shares": {
            cls: share(count, len(failures)) for cls, count in sorted(class_counts.items())
        },
        "failure_code_counts": dict(
            sorted(Counter(r["failure_reason_code"] for r in failures).items())
        ),
        "failures_replayable_via_razorpay_fixture": sum(
            1 for r in failures if r["razorpay_reason"] and r["failure_reason_code"] != "UNKNOWN"
        ),
        "attempts_by_ist_hour": {str(h): hour_counts.get(h, 0) for h in range(24)},
        "diurnal_peak_to_trough_ratio": round(
            max(hour_counts.values()) / max(min(hour_counts.values()), 1), 2
        ),
    }


def _corridor_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for r in rows if r["succeeded"])
    return {"attempts": len(rows), "successes": wins, "success_rate": share(wins, len(rows))}


def _ground_truth(config: dict[str, Any], raw: list[dict[str, Any]]) -> dict[str, Any]:
    """The answers Engine 1 is scored against — manifest only, never a record.

    This block is the reason the payments dataset can produce a precision/recall
    figure rather than a screenshot. It is also the reason the ground-truth
    separation test exists: if any of this leaked into a record, the engine would
    be reading the answer key.
    """
    events: list[dict[str, Any]] = []
    for event in config["degradations"]:
        starts, ends = parse_ts(event["starts_at"]), parse_ts(event["ends_at"])
        in_window, outside = [], []
        for row in raw:
            if row["method"] != event["method"]:
                continue
            if event.get("issuer") and row["issuer"] != event["issuer"]:
                continue
            if event.get("route_id") and row["route_id"] != event["route_id"]:
                continue
            (in_window if starts <= row["created_at"] < ends else outside).append(row)
        events.append(
            {
                "label": event["label"],
                "corridor": {
                    "method": event["method"],
                    "issuer": event.get("issuer"),
                    "route_id": event.get("route_id"),
                },
                "window": {"starts_at": event["starts_at"], "ends_at": event["ends_at"]},
                "configured_degraded_success_rate": event["degraded_success_rate"],
                "dominant_failure_code": event["dominant_failure_code"],
                "observed_in_window": _corridor_stats(in_window),
                "observed_same_corridor_outside_window": _corridor_stats(outside),
                "note": event.get("note", ""),
            }
        )

    decoys: list[dict[str, Any]] = []
    for decoy in config.get("decoys", []):
        rows = [r for r in raw if r["_decoy_label"] == decoy["label"]]
        decoys.append(
            {
                "label": decoy["label"],
                "corridor": {
                    "method": decoy["method"],
                    "issuer": decoy["issuer"],
                    "route_id": decoy["route_id"],
                },
                "degradation_injected": False,
                "configured_baseline_success_rate": decoy["baseline_success_rate"],
                "observed": _corridor_stats(rows),
                "worst_6h_window_success_rate": _worst_window_rate(rows, hours=6),
                "note": decoy.get("note", ""),
            }
        )

    return {
        "degradations": events,
        "decoys": decoys,
        "contract": (
            "Engines read the .jsonl records only. Nothing in this block appears "
            "in any record — see the ground-truth separation test in "
            "apps/api/app/tests/test_data_generators.py."
        ),
    }


#: A "worst window" of one attempt is not evidence of anything, so the decoy
#: statistic ignores windows thinner than this.
MIN_WINDOW_ATTEMPTS = 3


def _worst_window_rate(rows: list[dict[str, Any]], *, hours: int) -> dict[str, Any]:
    """The decoy's most incriminating-looking stretch.

    Recorded because it is the number that would tempt a count-based detector:
    on a corridor doing three attempts a day, a run of failures is unremarkable
    variance that reads exactly like an outage.
    """
    if not rows:
        return {"attempts": 0, "success_rate": 0.0, "window_starts_at": None}
    ordered = sorted(rows, key=lambda r: r["created_at"])
    span = timedelta(hours=hours)
    worst: dict[str, Any] = {"attempts": 0, "success_rate": 1.0, "window_starts_at": None}
    for anchor in ordered:
        edge = anchor["created_at"]
        window = [r for r in ordered if edge <= r["created_at"] < edge + span]
        if len(window) < MIN_WINDOW_ATTEMPTS:
            continue
        rate = share(sum(1 for r in window if r["succeeded"]), len(window))
        if rate < worst["success_rate"] or (
            rate == worst["success_rate"] and len(window) > worst["attempts"]
        ):
            worst = {
                "attempts": len(window),
                "success_rate": rate,
                "window_starts_at": iso(anchor["created_at"]),
            }
    return worst


__all__ = ["DATASET", "generate"]
