"""The synthetic data foundry's own test suite.

Five things are checked, and each one corresponds to a way the evidence base
could quietly stop being evidence:

1. **Reproducibility.** Same seed twice, byte-identical output. Without this
   every number the project reports is unfalsifiable.
2. **Vocabulary conformance.** Every failure reason exists in the decline
   taxonomy, so no record is silently unclassifiable.
3. **Distributions.** Soft declines dominate, mandate amounts populate both sides
   of the AFA threshold, ageing buckets are non-empty, success rate is in band.
4. **Ground-truth separation.** Degradation labels and reply annotations never
   appear in the records the engines read. This is the test that prevents an
   "your agent already knew the answer" question later.
5. **Manifest integrity.** The hash in the manifest is the hash of the output.

The generators live outside `apps/api/`, so the repo root goes on `sys.path`
first — same bootstrap as `scripts/core_loop_demo.py`.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.generators import invoices as invoices_generator  # noqa: E402
from data.generators import mandates as mandates_generator  # noqa: E402
from data.generators import payments as payments_generator  # noqa: E402
from data.generators.common import GeneratedBatch, load_config, sha256_text  # noqa: E402
from data.generators.replies import CATEGORIES  # noqa: E402
from data.generators.retry_model import RetrySuccessModel  # noqa: E402
from data.generators.vocabulary import UNMAPPED_RAW_REASONS  # noqa: E402

from app.services.decline_taxonomy import DeclineCode, classify  # noqa: E402
from app.services.razorpay_client import REASON_MAP  # noqa: E402

SEED = 42
GENERATED_AT = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)

CONFIGS = {
    "payments": "data/generators/configs/payments.default.json",
    "mandates": "data/generators/configs/mandates.default.json",
    "invoices": "data/generators/configs/invoices.default.json",
}
MODULES = {
    "payments": payments_generator,
    "mandates": mandates_generator,
    "invoices": invoices_generator,
}

#: Every field name that carries an answer the engines are supposed to derive.
#: A record containing any of these is leakage, full stop.
GROUND_TRUTH_KEYS = frozenset(
    {
        "cohort",
        "archetype",
        "ageing_bucket",
        "degradation",
        "degradations",
        "degraded",
        "degradation_injected",
        "decoy",
        "decoy_label",
        "is_promise",
        "promise_kept",
        "promised_date",
        "is_dispute",
        "is_conditional",
        "date_confidence",
        "category",
        "language",
        "ground_truth",
        "coverage_forced",
        "afa_side",
        "notice_profile",
        "next_debit_notice_profile",
        "afa_registration_gap",
    }
)


def _generate(dataset: str, seed: int = SEED) -> GeneratedBatch:
    config_path = CONFIGS[dataset]
    return MODULES[dataset].generate(
        load_config(config_path),
        seed,
        config_path=config_path,
        generated_at=GENERATED_AT,
    )


@pytest.fixture(scope="module")
def batches() -> dict[str, GeneratedBatch]:
    """One batch per dataset, generated once for the whole module."""
    return {name: _generate(name) for name in CONFIGS}


def _walk_keys(node: Any) -> set[str]:
    """Every key name anywhere in a nested record."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _walk_keys(item)
    return found


# ---------------------------------------------------------------------------
# 1. Reproducibility — non-negotiable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_same_seed_produces_byte_identical_output(dataset: str) -> None:
    first, second = _generate(dataset), _generate(dataset)
    assert first.payload == second.payload
    assert first.batch_id == second.batch_id
    assert first.manifest["output"]["sha256"] == second.manifest["output"]["sha256"]


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_manifest_is_identical_apart_from_the_wall_clock(dataset: str) -> None:
    """Only `nondeterministic` may differ between two runs of the same world."""
    first = _generate(dataset).manifest
    second = MODULES[dataset].generate(
        load_config(CONFIGS[dataset]),
        SEED,
        config_path=CONFIGS[dataset],
        generated_at=datetime(2030, 1, 1, tzinfo=UTC),
    ).manifest

    assert first["nondeterministic"] != second["nondeterministic"]
    assert {k: v for k, v in first.items() if k != "nondeterministic"} == {
        k: v for k, v in second.items() if k != "nondeterministic"
    }


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_a_different_seed_produces_a_different_batch(dataset: str) -> None:
    """The seed has to actually do something, or reproducibility is trivial."""
    assert _generate(dataset).payload != _generate(dataset, seed=SEED + 1).payload


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_committed_sample_matches_a_fresh_generation(dataset: str) -> None:
    """The batch in `data/samples/` is the batch its manifest claims.

    This is what makes the samples worth committing: a reader regenerates from
    the manifest's seed and diffs, and gets nothing.
    """
    sample_dir = REPO_ROOT / "data" / "samples" / dataset
    manifest = json.loads((sample_dir / f"{dataset}.manifest.json").read_text(encoding="utf-8"))
    payload = (sample_dir / manifest["output"]["file"]).read_text(encoding="utf-8")

    regenerated = _generate(dataset, seed=manifest["seed"])
    assert regenerated.payload == payload
    assert regenerated.batch_id == manifest["batch_id"]


# ---------------------------------------------------------------------------
# 2. Vocabulary conformance
# ---------------------------------------------------------------------------


def _failure_codes(batch: GeneratedBatch) -> list[str]:
    codes: list[str] = []
    for record in batch.records:
        if record.get("failure_reason_code"):
            codes.append(record["failure_reason_code"])
        for attempt in record.get("debit_history", []):
            if attempt.get("failure_reason_code"):
                codes.append(attempt["failure_reason_code"])
    return codes


@pytest.mark.parametrize("dataset", ["payments", "mandates"])
def test_every_failure_reason_is_in_the_taxonomy(
    dataset: str, batches: dict[str, GeneratedBatch]
) -> None:
    known = {code.value for code in DeclineCode}
    emitted = set(_failure_codes(batches[dataset]))
    assert emitted, "a batch with no failures at all would test nothing"
    assert emitted <= known, f"unknown decline codes emitted: {sorted(emitted - known)}"


@pytest.mark.parametrize("dataset", ["payments", "mandates"])
def test_razorpay_reason_round_trips_to_the_same_code(
    dataset: str, batches: dict[str, GeneratedBatch]
) -> None:
    """A populated upstream reason must normalize back to the code beside it.

    Otherwise a simulated replay through `razorpay_client` would relabel the
    failure, and Phase 3/4 would measure recovery against a decline that is not
    the one the record describes.
    """
    for record in batches[dataset].records:
        rows = [record, *record.get("debit_history", [])]
        for row in rows:
            reason, code = row.get("razorpay_reason"), row.get("failure_reason_code")
            if not reason:
                continue
            expected = REASON_MAP.get(reason, DeclineCode.UNKNOWN).value
            assert expected == code, f"{reason!r} normalizes to {expected}, record says {code}"


def test_unknown_path_is_exercised(batches: dict[str, GeneratedBatch]) -> None:
    """At least one decline must be unmappable, and it must fail closed to HARD."""
    unknowns = [
        r
        for r in batches["payments"].records
        if r["failure_reason_code"] == DeclineCode.UNKNOWN.value
    ]
    assert unknowns, "no UNKNOWN declines — the fail-safe path is untested by this batch"
    for record in unknowns:
        assert record["razorpay_reason"] in UNMAPPED_RAW_REASONS
        assert record["razorpay_reason"] not in REASON_MAP


# ---------------------------------------------------------------------------
# 3. Distributions
# ---------------------------------------------------------------------------


def test_payments_success_rate_is_in_the_realistic_band(
    batches: dict[str, GeneratedBatch],
) -> None:
    measured = batches["payments"].manifest["measured"]
    assert 0.85 <= measured["overall_success_rate"] <= 0.92
    rates = {m: s["success_rate"] for m, s in measured["success_rate_by_method"].items()}
    assert len(set(rates.values())) > 1, "methods must not share one baseline"


def test_payments_failures_are_dominated_by_soft_declines(
    batches: dict[str, GeneratedBatch],
) -> None:
    """Soft is the clear majority, per the ~80%-soft research grounding.

    The bound is 0.70 rather than 0.80 because the injected degradations force a
    specific code inside their windows, which pulls the measured mix around at
    a few hundred rows. The manifest reports what actually came out; this test
    only defends the claim the project makes out loud.
    """
    shares = batches["payments"].manifest["measured"]["failure_class_shares"]
    assert shares.get("SOFT", 0) >= 0.70
    assert shares.get("SOFT", 0) > shares.get("HARD", 0) * 3
    assert shares.get("HARD", 0) > 0, "a batch with no hard declines is not realistic"


def test_payments_traffic_is_not_uniform_across_the_day(
    batches: dict[str, GeneratedBatch],
) -> None:
    """The diurnal shape is what makes a count-based detector wrong."""
    assert batches["payments"].manifest["measured"]["diurnal_peak_to_trough_ratio"] >= 5


def test_mandate_amounts_straddle_the_afa_threshold(
    batches: dict[str, GeneratedBatch],
) -> None:
    """`rbi-mandate-rules:A3` — both branches must be genuinely exercised."""
    measured = batches["mandates"].manifest["measured"]
    split = measured["amount_split_at_afa_threshold"]
    assert split["above"] >= 5 and split["at_or_below"] >= 5
    threshold = measured["afa_threshold_paise"]
    assert threshold == 1_500_000

    near = [
        r
        for r in batches["mandates"].records
        if abs(r["amount_paise"] - threshold) <= 600_000
    ]
    assert near, "no mandates near the boundary — the branch is only tested far from it"


def test_mandate_book_contains_every_state_the_policy_engine_handles(
    batches: dict[str, GeneratedBatch],
) -> None:
    batch = batches["mandates"]
    statuses = {r["status"] for r in batch.records}
    assert {"active", "paused", "revoked"} <= statuses

    cohorts = batch.manifest["ground_truth"]["cohort_counts"]
    for required in ("healthy", "first_failure", "repeat_failure", "cap_reached", "paused"):
        assert cohorts.get(required, 0) >= 1

    at_cap = [r for r in batch.records if r["attempts_in_current_cycle"] >= 3]
    assert at_cap, "no mandate at the Part B attempt cap — the stopping rule is untested"

    revoked = [r for r in batch.records if r["status"] == "revoked"]
    assert revoked
    assert any(
        a["failure_reason_code"] == DeclineCode.MANDATE_REVOKED.value
        for r in revoked
        for a in r["debit_history"]
    )


def test_mandate_notice_windows_cover_every_violation(
    batches: dict[str, GeneratedBatch],
) -> None:
    """A2 is violable in three distinct ways; all three must be present."""
    profiles = batches["mandates"].manifest["ground_truth"][
        "next_debit_notice_profile_counts"
    ]
    for required in ("on_time", "late", "stale", "missing"):
        assert profiles.get(required, 0) >= 1, f"no mandate with a {required} pre-debit notice"


def test_mandate_attempt_counts_agree_with_the_history(
    batches: dict[str, GeneratedBatch],
) -> None:
    """A summary field that disagrees with the detail is a trap, not a feature."""
    for record in batches["mandates"].records:
        current = max((a["cycle"] for a in record["debit_history"]), default=0)
        failed_now = sum(
            1
            for a in record["debit_history"]
            if a["cycle"] == current and a["status"] == "failed"
        )
        assert record["attempts_in_current_cycle"] == failed_now


def test_invoice_ageing_buckets_are_populated_and_front_weighted(
    batches: dict[str, GeneratedBatch],
) -> None:
    buckets = batches["invoices"].manifest["measured"]["ageing_buckets"]
    for name in ("current", "1_30", "31_60", "61_90", "90_plus"):
        assert buckets[name]["invoices"] >= 1, f"ageing bucket {name} is empty"
    # Asserted on counts, which the allocator fixes exactly. Value share points
    # the same way but is noisier, because amount is drawn independently of
    # ageing — a real ledger has no rule saying old invoices are small ones.
    early = buckets["1_30"]["invoices"] + buckets["31_60"]["invoices"]
    late = buckets["61_90"]["invoices"] + buckets["90_plus"]["invoices"]
    assert early > late, "most overdue invoices should sit in the earlier buckets"
    early_value = buckets["1_30"]["value_paise"] + buckets["31_60"]["value_paise"]
    late_value = buckets["61_90"]["value_paise"] + buckets["90_plus"]["value_paise"]
    assert early_value > late_value * 0.8


def test_invoice_replies_cover_every_category_including_hinglish(
    batches: dict[str, GeneratedBatch],
) -> None:
    measured = batches["invoices"].manifest["measured"]
    for category in CATEGORIES:
        assert measured["reply_category_counts"].get(category, 0) >= 1, f"no {category} reply"
    assert measured["reply_language_counts"].get("hinglish", 0) >= 1
    assert measured["disputes"] >= 1


def test_invoices_contain_both_kept_and_broken_promises(
    batches: dict[str, GeneratedBatch],
) -> None:
    """Engine 3 escalates only broken promises, so both must exist to tell apart."""
    promises = batches["invoices"].manifest["measured"]["promises"]
    assert promises["kept"] >= 1
    assert promises["broken"] >= 1
    assert promises["conditional"] >= 1
    assert promises["undateable"] >= 1, "every promise having a clean date makes this a regex task"


def test_invoice_ledger_never_pre_labels_a_dispute(
    batches: dict[str, GeneratedBatch],
) -> None:
    """A `disputed` status would hand Engine 3 the answer it is meant to read."""
    statuses = {r["status"] for r in batches["invoices"].records}
    assert statuses <= {"open", "overdue", "paid"}


# ---------------------------------------------------------------------------
# 4. Ground-truth separation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_records_carry_no_ground_truth_fields(
    dataset: str, batches: dict[str, GeneratedBatch]
) -> None:
    for record in batches[dataset].records:
        leaked = _walk_keys(record) & GROUND_TRUTH_KEYS
        assert not leaked, f"{dataset} record {record} leaks ground truth: {sorted(leaked)}"


def test_degradation_ground_truth_exists_and_stays_in_the_manifest(
    batches: dict[str, GeneratedBatch],
) -> None:
    batch = batches["payments"]
    ground = batch.manifest["ground_truth"]
    assert len(ground["degradations"]) >= 1
    assert len(ground["decoys"]) >= 1

    for event in ground["degradations"]:
        observed = event["observed_in_window"]
        outside = event["observed_same_corridor_outside_window"]
        assert observed["attempts"] >= 5, "a degradation nobody could detect is not a test"
        assert observed["success_rate"] < outside["success_rate"]

    for decoy in ground["decoys"]:
        assert decoy["degradation_injected"] is False

    # The labels themselves must appear nowhere in the data file.
    for event in ground["degradations"]:
        assert event["label"] not in batch.payload
    for decoy in ground["decoys"]:
        assert decoy["label"] not in batch.payload


def test_reply_annotations_stay_in_the_manifest(
    batches: dict[str, GeneratedBatch],
) -> None:
    batch = batches["invoices"]
    annotations = batch.manifest["ground_truth"]["per_reply"]
    assert annotations

    reply_ids = {
        reply["reply_id"] for record in batch.records for reply in record["replies"]
    }
    assert set(annotations) == reply_ids

    for reply_id, annotation in annotations.items():
        record = next(
            r for r in batch.records if any(x["reply_id"] == reply_id for x in r["replies"])
        )
        reply = next(x for x in record["replies"] if x["reply_id"] == reply_id)
        assert set(reply) == {"reply_id", "direction", "channel", "received_at", "text"}
        assert annotation["category"] in CATEGORIES


# ---------------------------------------------------------------------------
# 5. Manifest integrity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_manifest_hash_matches_the_output(
    dataset: str, batches: dict[str, GeneratedBatch]
) -> None:
    batch = batches[dataset]
    assert batch.manifest["output"]["sha256"] == sha256_text(batch.payload)
    assert batch.manifest["output"]["rows"] == len(batch.records)
    assert batch.manifest["seed"] == SEED
    assert batch.manifest["config"]["resolved"] == load_config(CONFIGS[dataset])


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_every_record_carries_its_batch_id(
    dataset: str, batches: dict[str, GeneratedBatch]
) -> None:
    batch = batches[dataset]
    assert all(r["batch_id"] == batch.batch_id for r in batch.records)


@pytest.mark.parametrize("dataset", sorted(CONFIGS))
def test_money_is_integer_paise(dataset: str, batches: dict[str, GeneratedBatch]) -> None:
    for record in batches[dataset].records:
        for key, value in record.items():
            if key.endswith("_paise"):
                assert isinstance(value, int), f"{key} is {type(value).__name__}, not int"


# ---------------------------------------------------------------------------
# The retry-success model — generators must not pre-decide outcomes
# ---------------------------------------------------------------------------


def test_retry_model_never_lets_a_hard_decline_succeed() -> None:
    model = RetrySuccessModel.load()
    for code in DeclineCode:
        spec = classify(code)
        if spec.decline_class.value in {"HARD", "TERMINAL_MANDATE", "UNKNOWN"}:
            assert (
                model.probability(code.value, hours_since_previous=72, attempt_number=1) == 0.0
            ), f"{code} must never be retryable-with-hope"


def test_retry_model_rewards_spacing_and_decays_with_attempts() -> None:
    model = RetrySuccessModel.load()
    code = DeclineCode.INSUFFICIENT_FUNDS.value
    immediate = model.probability(code, hours_since_previous=1, attempt_number=1)
    spaced = model.probability(code, hours_since_previous=30, attempt_number=1)
    fourth = model.probability(code, hours_since_previous=30, attempt_number=4)
    assert immediate < spaced
    assert fourth < spaced


def test_retry_model_sampling_is_seeded_and_repeatable() -> None:
    from random import Random

    model = RetrySuccessModel.load()
    first = [
        model.sample(
            Random(7), DeclineCode.INSUFFICIENT_FUNDS.value,
            hours_since_previous=30, attempt_number=2,
        ).succeeded
        for _ in range(5)
    ]
    second = [
        model.sample(
            Random(7), DeclineCode.INSUFFICIENT_FUNDS.value,
            hours_since_previous=30, attempt_number=2,
        ).succeeded
        for _ in range(5)
    ]
    assert first == second


def test_no_dataset_contains_a_retry_outcome() -> None:
    """The data must not answer the question the engines exist to answer."""
    forbidden = {"retry_succeeded", "would_succeed", "recovery_outcome", "retry_outcome"}
    for dataset in CONFIGS:
        batch = _generate(dataset)
        for record in batch.records:
            assert not (_walk_keys(record) & forbidden)
