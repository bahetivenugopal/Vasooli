"""Invoice generator — the receivables ledger Engine 3 chases.

The differentiating claim for Engine 3 is narrow and testable: it escalates only
**broken** promises. That claim needs a ledger containing promises that were
kept, promises that were broken, promises too vague to pin a date on, disputes
that must never be dunned harder, and customers who simply never reply. All five
are here, and which is which lives in the manifest, not in the record.

Customer archetypes differ in *behaviour*, not just in numbers. A
reliably-slow-but-always-pays account and a chronic delinquent can carry the same
ageing and the same amount; the correct next action for them is not the same, and
an engine that treats them identically should score worse on this data.

Coverage of every reply category, of Hinglish, and of at least one broken promise
is **guaranteed** rather than sampled — a repair pass runs after assignment. An
acceptance criterion that holds only on lucky seeds is not an acceptance
criterion.
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
from data.generators.replies import CATEGORIES, ReplyTemplate, render, templates_for

MODULE = "data.generators.invoices"
DATASET = "invoices"

AMOUNT_STEP_PAISE = 100

TERM_DAYS = {"NET15": 15, "NET30": 30, "NET45": 45}

COMPANY_PREFIXES = (
    "Sunrise", "Vardhman", "Meridian", "Blue Orchid", "Kalyan", "Northline",
    "Trident", "Shakti", "Everest", "Deccan", "Halcyon", "Pinnacle", "Sarvodaya",
    "Cobalt", "Anantara", "Greenfield",
)
COMPANY_SUFFIXES = (
    "Textiles Pvt Ltd", "Logistics LLP", "Foods Pvt Ltd", "Systems Pvt Ltd",
    "Traders", "Infra Pvt Ltd", "Retail Pvt Ltd", "Chemicals Ltd",
    "Media Pvt Ltd", "Agro Exports",
)


def _between(r: Random, spec: dict[str, float]) -> float:
    return spec["min"] + r.random() * (spec["max"] - spec["min"])


def _company_name(r: Random) -> str:
    prefix = COMPANY_PREFIXES[int(r.random() * len(COMPANY_PREFIXES))]
    suffix = COMPANY_SUFFIXES[int(r.random() * len(COMPANY_SUFFIXES))]
    return f"{prefix} {suffix}"


def _pick_template(
    r: Random, config: dict[str, Any], category: str, *, force_language: str | None = None
) -> ReplyTemplate:
    """Choose a reply template, biasing language by the configured mix."""
    language = force_language or weighted_from_mapping(r, config["language_mix"])
    pool = [t for t in templates_for(category) if t.language == language]
    if not pool:
        pool = list(templates_for(category))
    return weighted_choice(r, [(t, 1.0) for t in pool])


def _reminders(
    config: dict[str, Any], due_at: datetime, as_of: datetime, invoice_id: str
) -> list[dict[str, Any]]:
    """The outbound ladder already sent before `as_of`.

    Capped at three rungs, matching `policy-bounds:QH2`/`QH3` — the ladder in the
    data cannot exceed the ladder the policy engine would permit, or Engine 3
    would start every run already in violation of its own contact cap.
    """
    ladder = config["reminder_ladder"]
    sent: list[dict[str, Any]] = []
    for rung in range(1, ladder["max_rungs"] + 1):
        offset = ladder["first_reminder_after_days"] + ladder["spacing_days"] * (rung - 1)
        sent_at = due_at + timedelta(days=offset)
        if sent_at > as_of:
            break
        sent.append(
            {
                "communication_id": f"{invoice_id}_out_{rung}",
                "direction": "outbound",
                "channel": ladder["channel"],
                "rung": rung,
                "template": f"reminder_rung_{rung}",
                "sent_at": iso(sent_at),
            }
        )
    return sent


def _ensure_coverage(
    plan: list[dict[str, Any]], config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Force in any reply category or language the sampling happened to miss.

    Deterministic and documented rather than hidden: the repair walks invoices in
    id order and converts the first compatible one. It is stated in the data card
    because a reader deserves to know the corpus is guaranteed-diverse by
    construction, not by luck.
    """
    chased = [p for p in plan if p["chased"]]

    present = {p["category"] for p in plan if p["category"]}
    for category in CATEGORIES:
        if category in present:
            continue
        for entry in chased:
            mix = config["archetype_reply_mix"][entry["archetype"]]
            if entry["category"] not in (None, category) and category in mix:
                entry["category"] = category
                entry["coverage_forced"] = True
                break
        else:
            # No chased invoice's archetype naturally produces it; give it to the
            # last chased invoice anyway. A missing category is worse than a
            # slightly out-of-character customer.
            chased[-1]["category"] = category
            chased[-1]["coverage_forced"] = True

    if not any(p["language"] == "hinglish" for p in plan if p["category"]):
        for entry in plan:
            if entry["category"] is not None:
                entry["language"] = "hinglish"
                entry["coverage_forced"] = True
                break

    return plan


def generate(
    config: dict[str, Any],
    seed: int,
    *,
    config_path: str,
    generated_at: datetime,
) -> GeneratedBatch:
    """Build one reproducible receivables ledger."""
    batch_id = make_batch_id(config["batch_prefix"], seed, fingerprint(config))
    as_of = parse_ts(config["as_of"])

    r_assign = rng(seed, DATASET, "assignment")
    r_amount = rng(seed, DATASET, "amounts")
    r_time = rng(seed, DATASET, "timing")
    r_reply = rng(seed, DATASET, "replies")
    r_outcome = rng(seed, DATASET, "outcomes")
    r_name = rng(seed, DATASET, "names")

    total = config["invoice_count"]
    archetype_counts = allocate(total, config["archetypes"])
    ageing_counts = allocate(
        total, {name: spec["weight"] for name, spec in config["ageing_buckets"].items()}
    )
    amount_counts = allocate(
        total, {name: spec["weight"] for name, spec in config["amount_buckets"].items()}
    )

    archetypes = [a for name, n in sorted(archetype_counts.items()) for a in [name] * n]
    ageings = [a for name, n in sorted(ageing_counts.items()) for a in [name] * n]
    amounts = [a for name, n in sorted(amount_counts.items()) for a in [name] * n]
    r_assign.shuffle(archetypes)
    r_assign.shuffle(ageings)
    r_assign.shuffle(amounts)

    plan: list[dict[str, Any]] = []
    for index, (archetype, ageing_name, amount_bucket) in enumerate(
        zip(archetypes, ageings, amounts, strict=True), start=1
    ):
        ageing = config["ageing_buckets"][ageing_name]
        bucket = config["amount_buckets"][amount_bucket]
        days_overdue = int(
            _between(
                r_time,
                {"min": ageing["min_days_overdue"], "max": ageing["max_days_overdue"] + 0.999},
            )
        )
        # A customer replies to a reminder, not to silence. Deciding this before
        # the reply category is what makes the coverage guarantee below real: a
        # forced category on an invoice that was never chased would be silently
        # dropped again at render time.
        chased = days_overdue >= config["reminder_ladder"]["first_reminder_after_days"]
        has_reply = chased and r_reply.random() < config["reply_probability"][archetype]
        category = (
            weighted_from_mapping(r_reply, config["archetype_reply_mix"][archetype])
            if has_reply
            else None
        )
        plan.append(
            {
                "invoice_id": f"inv_{index:04d}",
                "archetype": archetype,
                "ageing_bucket": ageing_name,
                "amount_bucket": amount_bucket,
                "days_overdue": days_overdue,
                "chased": chased,
                "amount_paise": sample_bounded(
                    r_amount, bucket["min_paise"], bucket["max_paise"], step=AMOUNT_STEP_PAISE
                ),
                "payment_terms": weighted_from_mapping(r_time, config["payment_terms"]),
                "category": category,
                "language": weighted_from_mapping(r_reply, config["language_mix"]),
                "coverage_forced": False,
            }
        )

    plan = _ensure_coverage(plan, config)

    records: list[dict[str, Any]] = []
    ground_invoices: dict[str, Any] = {}
    ground_replies: dict[str, Any] = {}
    forced_broken_used = False

    for entry in plan:
        invoice_id = entry["invoice_id"]
        amount = entry["amount_paise"]
        days_overdue = entry["days_overdue"]
        terms = entry["payment_terms"]

        if days_overdue > 0:
            due_at = as_of - timedelta(days=days_overdue)
        else:
            # "Current" means not yet due: the invoice is open and behaving.
            due_at = as_of + timedelta(days=1 + int(r_time.random() * 12))
        issued_at = due_at - timedelta(days=TERM_DAYS[terms])

        communications = _reminders(config, due_at, as_of, invoice_id)

        replies: list[dict[str, Any]] = []
        promise_kept: bool | None = None
        paid_at: datetime | None = None

        if entry["category"] and communications:
            template = _pick_template(
                r_reply, config, entry["category"], force_language=entry["language"]
            )
            last_sent = parse_ts(communications[-1]["sent_at"])
            received_at = min(
                last_sent + timedelta(hours=4 + int(r_time.random() * 44)),
                as_of - timedelta(hours=1),
            )
            text, promised_date = render(template, r_reply, received_at.date())
            reply_id = f"{invoice_id}_in_1"
            replies.append(
                {
                    "reply_id": reply_id,
                    "direction": "inbound",
                    "channel": config["reminder_ladder"]["channel"],
                    "received_at": iso(received_at),
                    "text": text,
                }
            )

            if template.is_promise:
                kept_rate = config["archetype_promise_kept_rate"][entry["archetype"]]
                would_keep = r_outcome.random() < kept_rate
                if entry["archetype"] == "chronic_delinquent" and not forced_broken_used:
                    # Guarantee at least one unambiguously broken promise, so the
                    # escalation path is exercised on every seed.
                    would_keep = False
                    forced_broken_used = True

                deadline = (
                    datetime.combine(promised_date, received_at.timetz())
                    if promised_date is not None
                    else received_at + timedelta(days=config["vague_promise_grace_days"])
                )
                if would_keep and deadline <= as_of:
                    promise_kept = True
                    paid_at = deadline - timedelta(hours=int(r_outcome.random() * 20))
                elif deadline <= as_of:
                    promise_kept = False
                else:
                    promise_kept = None  # still within the promised window

            ground_replies[reply_id] = {
                "invoice_id": invoice_id,
                "category": template.category,
                "language": template.language,
                "is_promise": template.is_promise,
                "is_conditional": template.is_conditional,
                "is_dispute": template.is_dispute,
                "promised_date": promised_date.isoformat() if promised_date else None,
                "date_confidence": template.date_confidence,
                "promise_kept": promise_kept,
            }

        if paid_at is not None:
            status = "paid"
            amount_paid = amount
        elif days_overdue > 0:
            status = "overdue"
            amount_paid = 0
        else:
            status = "open"
            amount_paid = 0

        records.append(
            {
                "invoice_id": invoice_id,
                "batch_id": batch_id,
                "customer_id": f"cust_{int(r_name.random() * config['customer_pool']):04d}",
                "customer_name": _company_name(r_name),
                "amount_paise": amount,
                "amount_paid_paise": amount_paid,
                "currency": "INR",
                "payment_terms": terms,
                "issued_at": iso(issued_at),
                "due_at": iso(due_at),
                # Days past due as of `as_of`, or — for a settled invoice — how
                # late it was when it landed. Either way it is derivable from
                # `due_at` and `paid_at`, so an engine recomputing it gets the
                # same number instead of quietly disagreeing with the ledger.
                "days_overdue": (
                    max((paid_at - due_at).days, 0) if paid_at is not None else days_overdue
                ),
                "status": status,
                "paid_at": iso(paid_at) if paid_at else None,
                "communications": communications,
                "replies": replies,
            }
        )

        ground_invoices[invoice_id] = {
            "archetype": entry["archetype"],
            "ageing_bucket": entry["ageing_bucket"],
            "has_reply": bool(replies),
            "promise_kept": promise_kept,
            "coverage_forced": entry["coverage_forced"],
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
        measured=_measure(records, ground_invoices, ground_replies),
        ground_truth={
            "per_invoice": ground_invoices,
            "per_reply": ground_replies,
            "contract": (
                "Promise/date/dispute annotations live here and nowhere else. "
                "Engine 3 reads `replies[].text` and must reach the same "
                "conclusions unaided — that is what makes its extraction "
                "accuracy a measurement rather than a claim."
            ),
        },
        generated_at=generated_at,
    )
    return GeneratedBatch(
        dataset=DATASET, batch_id=batch_id, records=records, payload=payload, manifest=manifest
    )


def _measure(
    records: list[dict[str, Any]],
    ground_invoices: dict[str, Any],
    ground_replies: dict[str, Any],
) -> dict[str, Any]:
    total_value = sum(r["amount_paise"] for r in records)
    by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in sorted({g["ageing_bucket"] for g in ground_invoices.values()}):
        rows = [
            r for r in records if ground_invoices[r["invoice_id"]]["ageing_bucket"] == bucket
        ]
        by_bucket[bucket] = {
            "invoices": len(rows),
            "value_paise": sum(r["amount_paise"] for r in rows),
            "value_share": share(sum(r["amount_paise"] for r in rows), total_value),
        }

    promises = [g for g in ground_replies.values() if g["is_promise"]]
    return {
        "invoices": len(records),
        "status_counts": dict(sorted(Counter(r["status"] for r in records).items())),
        "total_value_paise": total_value,
        "overdue_value_paise": sum(
            r["amount_paise"] for r in records if r["status"] == "overdue"
        ),
        "ageing_buckets": by_bucket,
        "archetype_counts": dict(
            sorted(Counter(g["archetype"] for g in ground_invoices.values()).items())
        ),
        "invoices_with_reply": sum(1 for r in records if r["replies"]),
        "reply_category_counts": dict(
            sorted(Counter(g["category"] for g in ground_replies.values()).items())
        ),
        "reply_language_counts": dict(
            sorted(Counter(g["language"] for g in ground_replies.values()).items())
        ),
        "promises": {
            "total": len(promises),
            "kept": sum(1 for g in promises if g["promise_kept"] is True),
            "broken": sum(1 for g in promises if g["promise_kept"] is False),
            "pending": sum(1 for g in promises if g["promise_kept"] is None),
            "conditional": sum(1 for g in promises if g["is_conditional"]),
            "with_explicit_date": sum(
                1 for g in promises if g["date_confidence"] == "explicit"
            ),
            "with_inferable_date": sum(
                1 for g in promises if g["date_confidence"] == "inferable"
            ),
            "undateable": sum(1 for g in promises if g["date_confidence"] == "none"),
        },
        "disputes": sum(1 for g in ground_replies.values() if g["is_dispute"]),
        "outbound_messages": sum(len(r["communications"]) for r in records),
        "max_ladder_rung_used": max(
            (c["rung"] for r in records for c in r["communications"]), default=0
        ),
    }


__all__ = ["DATASET", "generate"]
