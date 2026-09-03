"""Mandate generator — the book Engine 2 works from.

Every compliance gate in `rbi-mandate-rules` needs data that can trip it, or the
gate is decoration. So the cohorts here are allocated exactly rather than sampled:
each batch is guaranteed to contain a revoked mandate, a mandate sitting at the
Part B attempt cap of 3, a paused one, and debits whose pre-debit notification
window was violated in each of the three ways it can be (sent late, gone stale,
never sent at all).

Amounts straddle the ₹15,000 AFA threshold (`rbi-mandate-rules:A3`) on both
sides, with a deliberate cluster just under and just over it, because a threshold
branch that is only ever exercised far from the boundary has not really been
exercised.

Two traps worth naming, both realistic and both deliberate:

- A **revoked or paused mandate still carries a scheduled next debit.** Real
  schedules are not tidied up the moment a customer cancels. This is exactly the
  case `policy-bounds:HS1` exists for, and a batch where revoked mandates have no
  upcoming debit would never test it.
- A few mandates **never completed AFA registration** (`rbi-mandate-rules:A1`),
  so their debits fail with `AFA_REQUIRED` no matter how often they are retried.

As everywhere in this foundry: nothing here decides whether a retry *would*
succeed. That is `retry_model.py`, sampled by the engine at run time.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from random import Random
from typing import Any

from data.generators.common import (
    GeneratedBatch,
    allocate,
    build_manifest,
    fingerprint,
    iso,
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
    decline_class_of,
    razorpay_reason_for,
    validate_codes,
)

MODULE = "data.generators.mandates"
DATASET = "mandates"

AMOUNT_STEP_PAISE = 100

#: Days in one billing cycle, by frequency. Calendar months are approximated at
#: 30 days: nothing in the policy layer keys off a month boundary, and a real
#: calendar would add a dependency on which month the window happens to land in.
CYCLE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 90}

#: How many failed attempts each cohort has already made in the current cycle.
#: `cap_reached` is 3 because that is the `rbi-mandate-rules` Part B ceiling —
#: those mandates exist so a 4th attempt can be refused on camera.
COHORT_FAILURES = {
    "healthy": 0,
    "notice_violation": 0,
    "first_failure": 1,
    "repeat_failure": 2,
    "cap_reached": 3,
    "revoked_mid_sequence": 2,
    "paused": 1,
}

COHORT_STATUS = {
    "healthy": "active",
    "notice_violation": "active",
    "first_failure": "active",
    "repeat_failure": "active",
    "cap_reached": "active",
    "revoked_mid_sequence": "revoked",
    "paused": "paused",
}

#: The three ways the A2 notification window can be violated. Cycled through
#: rather than sampled, so every batch contains all three.
VIOLATION_PROFILES = ("late", "stale", "missing")

FIRST_NAMES = (
    "Aarav", "Ananya", "Devika", "Farhan", "Ishaan", "Kavya", "Meera", "Nikhil",
    "Priya", "Rohan", "Sanjay", "Sneha", "Tanvi", "Vikram", "Yash", "Zoya",
)
LAST_NAMES = (
    "Agarwal", "Bhat", "Chatterjee", "Desai", "Iyer", "Joshi", "Kulkarni",
    "Menon", "Nair", "Patel", "Rao", "Reddy", "Sharma", "Singh", "Verma",
)


def _validate(config: dict[str, Any]) -> None:
    validate_codes([entry["code"] for entry in config["failure_codes"]])
    validate_codes([config["terminal_failure_code"]])
    for entry in config["failure_codes"]:
        actual = decline_class_of(entry["code"]).value
        if actual != entry["class"]:
            raise ValueError(
                f"config says {entry['code']} is {entry['class']}, the taxonomy "
                f"says {actual}. The taxonomy wins — fix the config."
            )
    if set(config["cohorts"]) != set(COHORT_FAILURES):
        raise ValueError(
            "cohort names in the config must match the cohorts this generator "
            f"knows how to build: {sorted(COHORT_FAILURES)}"
        )


def _hours(r: Random, spec: dict[str, float]) -> float:
    return spec["min"] + r.random() * (spec["max"] - spec["min"])


def _pick_failure_code(
    r: Random, config: dict[str, Any], *, above_threshold: bool, afa_gap: bool
) -> str:
    """One failure reason for a mandate debit.

    Two constraints the payments generator does not have: `AFA_REQUIRED` is only
    reachable above the ₹15,000 threshold (A3), and a mandate that never
    completed registration fails with `AFA_REQUIRED` every time (A1) — retrying
    it forever is the exact waste the taxonomy exists to prevent.
    """
    if afa_gap:
        return "AFA_REQUIRED"
    options = [
        (entry["code"], entry["weight"])
        for entry in config["failure_codes"]
        if above_threshold or not entry.get("requires_above_afa_threshold")
    ]
    return weighted_choice(r, options)


def _notice_sent_at(
    r: Random, config: dict[str, Any], scheduled_at: datetime, profile: str
) -> str | None:
    """When the pre-debit notice went out, for a given profile.

    `on_time` lands inside the 24-48h window of `rbi-mandate-rules:A2`; `late` and
    `stale` land outside it on either side; `missing` and `not_yet_due` are both
    absent, and only the manifest distinguishes them.
    """
    if profile in {"missing", "not_yet_due"}:
        return None
    window = config["notice_windows_hours"][profile]
    return iso(scheduled_at - timedelta(hours=_hours(r, window)))


def _attempt(
    *,
    attempt_no: int,
    cycle: int,
    scheduled_at: datetime,
    notice_sent_at: str | None,
    succeeded: bool,
    failure_code: str | None,
) -> dict[str, Any]:
    return {
        "attempt_no": attempt_no,
        "cycle": cycle,
        "scheduled_at": iso(scheduled_at),
        "attempted_at": iso(scheduled_at),
        "notice_sent_at": notice_sent_at,
        "status": "success" if succeeded else "failed",
        "failure_reason_code": failure_code,
        "razorpay_reason": razorpay_reason_for(failure_code) if failure_code else None,
    }


def generate(
    config: dict[str, Any],
    seed: int,
    *,
    config_path: str,
    generated_at: datetime,
) -> GeneratedBatch:
    """Build one reproducible mandate book."""
    _validate(config)

    batch_id = make_batch_id(config["batch_prefix"], seed, fingerprint(config))
    as_of = parse_ts(config["as_of"])
    threshold = config["afa_threshold_paise"]

    r_assign = rng(seed, DATASET, "assignment")
    r_amount = rng(seed, DATASET, "amounts")
    r_time = rng(seed, DATASET, "timing")
    r_notice = rng(seed, DATASET, "notices")
    r_code = rng(seed, DATASET, "failure_codes")
    r_name = rng(seed, DATASET, "names")

    total = config["mandate_count"]
    cohort_counts = allocate(total, config["cohorts"])
    bucket_counts = allocate(
        total, {name: spec["weight"] for name, spec in config["amount_buckets"].items()}
    )

    slots = [c for cohort, n in sorted(cohort_counts.items()) for c in [cohort] * n]
    buckets = [b for bucket, n in sorted(bucket_counts.items()) for b in [bucket] * n]
    r_assign.shuffle(slots)
    r_assign.shuffle(buckets)

    records: list[dict[str, Any]] = []
    ground: dict[str, Any] = {}
    violation_cursor = 0

    for index, (cohort, bucket_name) in enumerate(zip(slots, buckets, strict=True), start=1):
        mandate_id = f"mnd_{index:04d}"
        bucket = config["amount_buckets"][bucket_name]
        amount = sample_bounded(
            r_amount, bucket["min_paise"], bucket["max_paise"], step=AMOUNT_STEP_PAISE
        )
        above_threshold = amount > threshold
        frequency = weighted_from_mapping(r_time, config["frequencies"])
        cycle_days = CYCLE_DAYS[frequency]

        cap_multiplier = _hours(r_amount, config["mandate_cap_multiplier"])
        mandate_cap = int(round(amount * cap_multiplier / AMOUNT_STEP_PAISE) * AMOUNT_STEP_PAISE)

        registered_at = as_of - timedelta(days=_hours(r_time, config["registration_lookback_days"]))
        # A registration gap is a defect in the mandate's own setup, so it is
        # never given to the healthy cohort — a "healthy" mandate that cannot
        # legally be debited would be a contradiction, not an edge case.
        afa_gap = cohort != "healthy" and r_code.random() < config["afa_registration_gap_rate"]

        history: list[dict[str, Any]] = []
        attempt_no = 0
        # A mandate that never completed AFA registration has no successful
        # history to have — there was never a debit it was allowed to make. Its
        # whole story is the current cycle's blocked attempts.
        past_cycles = 0 if afa_gap else int(_hours(r_time, config["cycle_history_cycles"]))

        for cycle in range(past_cycles):
            attempt_no += 1
            scheduled = as_of - timedelta(days=cycle_days * (past_cycles - cycle))
            profile = weighted_from_mapping(r_notice, config["notice_profiles"])
            history.append(
                _attempt(
                    attempt_no=attempt_no,
                    cycle=cycle + 1,
                    scheduled_at=scheduled,
                    notice_sent_at=_notice_sent_at(r_notice, config, scheduled, profile),
                    succeeded=True,
                    failure_code=None,
                )
            )

        current_cycle = past_cycles + 1
        failures = COHORT_FAILURES[cohort]
        spacing = _hours(r_time, config["retry_spacing_hours"])
        cycle_started_at = as_of - timedelta(hours=spacing * max(failures, 1) + 2)

        for failure_index in range(failures):
            attempt_no += 1
            scheduled = cycle_started_at + timedelta(hours=spacing * failure_index)
            is_last = failure_index == failures - 1
            if cohort == "revoked_mid_sequence" and is_last:
                code = config["terminal_failure_code"]
            else:
                code = _pick_failure_code(
                    r_code, config, above_threshold=above_threshold, afa_gap=afa_gap
                )
            profile = weighted_from_mapping(r_notice, config["notice_profiles"])
            history.append(
                _attempt(
                    attempt_no=attempt_no,
                    cycle=current_cycle,
                    scheduled_at=scheduled,
                    notice_sent_at=_notice_sent_at(r_notice, config, scheduled, profile),
                    succeeded=False,
                    failure_code=code,
                )
            )

        if cohort == "notice_violation":
            offset = _hours(r_time, config["notice_violation_next_debit_offset_hours"])
            next_profile = VIOLATION_PROFILES[violation_cursor % len(VIOLATION_PROFILES)]
            violation_cursor += 1
        else:
            offset = _hours(r_time, config["next_debit_offset_hours"])
            next_profile = weighted_from_mapping(r_notice, config["notice_profiles"])

        next_debit_at = as_of + timedelta(hours=offset)
        # A notice more than 48h ahead of the debit would itself be stale, so a
        # debit that far out simply has no notice yet. That is not a violation,
        # and the manifest records the difference so nobody scores it as one.
        if offset > 48 and next_profile != "missing":
            next_profile = "not_yet_due"

        records.append(
            {
                "mandate_id": mandate_id,
                "batch_id": batch_id,
                "customer_id": f"cust_{int(r_name.random() * config['customer_pool']):04d}",
                "customer_name": (
                    f"{FIRST_NAMES[int(r_name.random() * len(FIRST_NAMES))]} "
                    f"{LAST_NAMES[int(r_name.random() * len(LAST_NAMES))]}"
                ),
                "amount_paise": amount,
                "currency": "INR",
                "frequency": frequency,
                "registered_at": iso(registered_at),
                "afa_registered": not afa_gap,
                "mandate_cap_paise": mandate_cap,
                "status": COHORT_STATUS[cohort],
                "cycle_started_at": iso(cycle_started_at),
                "attempts_in_current_cycle": failures,
                "next_debit_at": iso(next_debit_at),
                "next_debit_notice_sent_at": _notice_sent_at(
                    r_notice, config, next_debit_at, next_profile
                ),
                "debit_history": history,
            }
        )

        ground[mandate_id] = {
            "cohort": cohort,
            "next_debit_notice_profile": next_profile,
            "afa_side": "above" if above_threshold else "below",
            "afa_registration_gap": afa_gap,
            "terminal_failure_code": (
                config["terminal_failure_code"] if cohort == "revoked_mid_sequence" else None
            ),
            "failed_attempts_in_current_cycle": failures,
        }

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
        measured=_measure(records, threshold),
        ground_truth={
            "per_mandate": ground,
            "cohort_counts": dict(sorted(Counter(g["cohort"] for g in ground.values()).items())),
            "next_debit_notice_profile_counts": dict(
                sorted(Counter(g["next_debit_notice_profile"] for g in ground.values()).items())
            ),
            "contract": (
                "Cohort labels, notice profiles and registration gaps are how each "
                "record was built. None of them appear in the records themselves — "
                "Engine 2 has to derive the same conclusions from the timestamps "
                "and statuses, which is the whole point."
            ),
        },
        generated_at=generated_at,
    )
    return GeneratedBatch(
        dataset=DATASET, batch_id=batch_id, records=records, payload=payload, manifest=manifest
    )


def _measure(records: list[dict[str, Any]], threshold: int) -> dict[str, Any]:
    attempts = [a for r in records for a in r["debit_history"]]
    failures = [a for a in attempts if a["status"] == "failed"]
    class_counts = Counter(decline_class_of(a["failure_reason_code"]).value for a in failures)
    lifecycle = {"TERMINAL_MANDATE", "POLICY_BLOCK"}
    issuer_counts = Counter(
        cls for cls in class_counts.elements() if cls not in lifecycle
    )

    return {
        "mandates": len(records),
        "status_counts": dict(sorted(Counter(r["status"] for r in records).items())),
        "frequency_counts": dict(sorted(Counter(r["frequency"] for r in records).items())),
        "afa_threshold_paise": threshold,
        "amount_split_at_afa_threshold": {
            "at_or_below": sum(1 for r in records if r["amount_paise"] <= threshold),
            "above": sum(1 for r in records if r["amount_paise"] > threshold),
        },
        "amount_paise": {
            "min": min(r["amount_paise"] for r in records),
            "max": max(r["amount_paise"] for r in records),
        },
        "mandates_without_afa_registration": sum(1 for r in records if not r["afa_registered"]),
        "mandates_at_or_above_attempt_cap": sum(
            1 for r in records if r["attempts_in_current_cycle"] >= 3
        ),
        "debit_attempts": len(attempts),
        "failed_attempts": len(failures),
        "failure_class_shares": {
            cls: share(count, len(failures)) for cls, count in sorted(class_counts.items())
        },
        # The same split with the cohort-forced lifecycle outcomes set aside —
        # revocations and registration blocks are placed deliberately, so leaving
        # them in makes the *drawn* issuer-decline mix look less soft-dominated
        # than it is. Both numbers are reported; neither replaces the other.
        "issuer_decline_class_shares_excluding_lifecycle": {
            cls: share(count, sum(issuer_counts.values()))
            for cls, count in sorted(issuer_counts.items())
        },
        "failure_code_counts": dict(
            sorted(Counter(a["failure_reason_code"] for a in failures).items())
        ),
        "next_debit_notice_present": sum(
            1 for r in records if r["next_debit_notice_sent_at"] is not None
        ),
    }


__all__ = ["DATASET", "generate"]
