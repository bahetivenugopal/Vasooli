"""Extraction accuracy against the generator's held-out annotations.

**This is the only module in Engine 3 that reads a manifest.** The understanding
layer does not, cannot, and a test asserts it. Keeping the answer key in the
marking code rather than in the exam is what makes the reported precision and
recall mean anything at all — it is the difference between a measurement and a
claim, and this phase's headline number is exactly that measurement.

Three rules govern how the numbers here are computed, and each exists to stop a
specific way of flattering the engine:

1. **Abstentions are never scored.** Every rate is over *model-handled* replies
   only, with the abstention rate reported beside it and never folded in. An
   abstention counted as a correct negative would let the engine improve its
   accuracy by refusing to answer, which inverts the entire point of having an
   abstention path.
2. **The confusion matrix is reported in full**, not just headline accuracy.
   Precision and recall move in opposite directions here and which one matters
   depends on the claim: a fabricated promise suppresses a legitimate chase, a
   missed promise chases someone who was cooperating.
3. **Date accuracy is split by difficulty.** The corpus contains dates stated in
   four everyday formats and dates that need an anchor resolved ("end of the
   month"). Scoring them together hides which half the engine is good at, and
   the inferable half is the interesting one.

The ground truth's own contract, quoted from the manifest: *"Promise/date/dispute
annotations live here and nowhere else. Engine 3 reads `replies[].text` and must
reach the same conclusions unaided."*
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.engines.receivables.schemas import (
    PROMISE_INTENTS,
    ConfusionMatrix,
    DateAccuracy,
    ExtractionResult,
    ExtractionScore,
    ReplyIntent,
)
from app.engines.receivables.understanding import committed_datetime

SCORING_NOTE = (
    "Every rate is computed over model-handled replies only. Abstentions are "
    "reported separately and never folded into an accuracy figure — an "
    "abstention counted as a correct answer would let the engine improve its "
    "score by refusing to answer."
)


class GroundTruthError(RuntimeError):
    """Raised when a manifest is missing or is not an invoices manifest."""


def load_manifest(path: Path | str) -> dict[str, Any]:
    """Read a generator manifest. Evaluation code only."""
    resolved = Path(path)
    if not resolved.exists():
        raise GroundTruthError(
            f"no manifest at {resolved}. Extraction metrics need the generator's "
            "held-out annotations; without them a run can report what it read but "
            "not whether it read correctly."
        )
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    if manifest.get("dataset") != "invoices":
        raise GroundTruthError(
            f"{resolved} is a {manifest.get('dataset')!r} manifest, not invoices"
        )
    return manifest


def _rate(numerator: int, denominator: int) -> float:
    """Zero denominator means zero, not one.

    A precision of 100% because nothing was predicted is the kind of number that
    gets quoted by accident.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _matrix(pairs: list[tuple[bool, bool]]) -> ConfusionMatrix:
    """A 2x2 over `(predicted, actual)` pairs, with the derived rates."""
    tp = sum(1 for p, a in pairs if p and a)
    fp = sum(1 for p, a in pairs if p and not a)
    tn = sum(1 for p, a in pairs if not p and not a)
    fn = sum(1 for p, a in pairs if not p and a)
    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)
    return ConfusionMatrix(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=(
            round(2 * precision * recall / (precision + recall), 4)
            if precision + recall
            else 0.0
        ),
        accuracy=_rate(tp + tn, len(pairs)),
    )


def _truth_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # pragma: no cover - the generator writes ISO dates
        return None


def _predicted_date(extraction: ExtractionResult) -> date | None:
    committed: datetime | None = committed_datetime(extraction)
    return committed.astimezone(UTC).date() if committed else None


def score_extractions(
    extractions: list[ExtractionResult], manifest: dict[str, Any]
) -> ExtractionScore:
    """Precision, recall, date accuracy and the confusion matrix, honestly.

    Replies the ground truth does not know about are skipped rather than counted
    as errors — a mismatch between the run's corpus and the manifest's is a
    dataset problem, and silently scoring it as a failure would misattribute it
    to the model.
    """
    truth: dict[str, Any] = manifest.get("ground_truth", {}).get("per_reply", {})

    scored: list[ExtractionResult] = []
    abstained: list[ExtractionResult] = []
    for extraction in extractions:
        if extraction.reply_id not in truth:
            continue
        (abstained if extraction.abstained else scored).append(extraction)

    promise_pairs: list[tuple[bool, bool]] = []
    dispute_pairs: list[tuple[bool, bool]] = []
    conditional_pairs: list[tuple[bool, bool]] = []
    by_language: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    intent_confusion: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[dict[str, object]] = []

    dates = DateAccuracy()
    difficulty: dict[str, Counter[str]] = defaultdict(Counter)

    for extraction in scored:
        gt = truth[extraction.reply_id]
        understanding = extraction.understanding
        assert understanding is not None  # guaranteed by the abstained split

        actual_promise = bool(gt["is_promise"])
        predicted_promise = bool(understanding.promise_detected)
        promise_pairs.append((predicted_promise, actual_promise))
        by_language[str(gt["language"])].append((predicted_promise, actual_promise))

        actual_dispute = bool(gt["is_dispute"])
        predicted_dispute = understanding.intent is ReplyIntent.DISPUTE
        dispute_pairs.append((predicted_dispute, actual_dispute))

        # Conditionality is only a meaningful claim about a reply that actually
        # contains a commitment. Scoring it over non-promises would pad the
        # matrix with true negatives that no reader would call a success.
        if actual_promise or predicted_promise:
            conditional_pairs.append((bool(understanding.conditional), bool(gt["is_conditional"])))

        intent_confusion[_truth_intent(gt)][understanding.intent.value] += 1

        # --- dates, over promises only ------------------------------------
        actual_date = _truth_date(gt.get("promised_date"))
        predicted_date = _predicted_date(extraction)
        confidence_band = str(gt.get("date_confidence", "none"))
        if actual_promise:
            dates.scored += 1
            bucket = difficulty[confidence_band]
            bucket["scored"] += 1
            if actual_date is None and predicted_date is None:
                # Correctly declined to invent a date. Counted as exact: the
                # right answer to "when?" on "next week sometime" is silence.
                dates.exact += 1
                dates.within_one_day += 1
                bucket["exact"] += 1
            elif actual_date is None:
                dates.spurious += 1
                bucket["spurious"] += 1
            elif predicted_date is None:
                dates.missing += 1
                bucket["missing"] += 1
            else:
                delta = abs((predicted_date - actual_date).days)
                if delta == 0:
                    dates.exact += 1
                    dates.within_one_day += 1
                    bucket["exact"] += 1
                elif delta <= 1:
                    dates.within_one_day += 1
                    dates.wrong += 1
                    bucket["within_one_day"] += 1
                else:
                    dates.wrong += 1
                    bucket["wrong"] += 1

        wrong_promise = predicted_promise != actual_promise
        wrong_dispute = predicted_dispute != actual_dispute
        wrong_conditional = actual_promise and bool(understanding.conditional) != bool(
            gt["is_conditional"]
        )
        wrong_date = actual_promise and (
            (actual_date is None) != (predicted_date is None)
            or (
                actual_date is not None
                and predicted_date is not None
                and predicted_date != actual_date
            )
        )
        if wrong_promise or wrong_dispute or wrong_conditional or wrong_date:
            failures.append(
                {
                    "reply_id": extraction.reply_id,
                    "invoice_id": extraction.invoice_id,
                    "language": gt["language"],
                    "category": gt["category"],
                    "text": extraction.text,
                    "truth": {
                        "is_promise": actual_promise,
                        "is_dispute": actual_dispute,
                        "is_conditional": bool(gt["is_conditional"]),
                        "promised_date": gt.get("promised_date"),
                        "date_confidence": confidence_band,
                    },
                    "predicted": {
                        "intent": understanding.intent.value,
                        "promise_detected": predicted_promise,
                        "is_dispute": predicted_dispute,
                        "conditional": bool(understanding.conditional),
                        "committed_date": (
                            predicted_date.isoformat() if predicted_date else None
                        ),
                        "confidence": extraction.confidence,
                    },
                    "wrong": [
                        name
                        for name, flag in (
                            ("promise", wrong_promise),
                            ("dispute", wrong_dispute),
                            ("conditional", wrong_conditional),
                            ("date", wrong_date),
                        )
                        if flag
                    ],
                    "model_reasoning": understanding.reasoning,
                }
            )

    dates.exact_rate = _rate(dates.exact, dates.scored)
    dates.by_difficulty = {k: dict(v) for k, v in sorted(difficulty.items())}

    total = len(scored) + len(abstained)
    return ExtractionScore(
        ground_truth_source=str(manifest.get("batch_id", "unknown")),
        replies_total=total,
        replies_scored=len(scored),
        abstentions=len(abstained),
        abstention_rate=_rate(len(abstained), total),
        promise_detection=_matrix(promise_pairs),
        dispute_detection=_matrix(dispute_pairs),
        conditional_detection=_matrix(conditional_pairs),
        date_accuracy=dates,
        intent_confusion={k: dict(v) for k, v in sorted(intent_confusion.items())},
        by_language={k: _matrix(v) for k, v in sorted(by_language.items())},
        failures=sorted(failures, key=lambda f: str(f["reply_id"])),
        scoring_note=SCORING_NOTE,
    )


def _truth_intent(gt: dict[str, Any]) -> str:
    """The ground truth's own category, as the confusion matrix's row label.

    Reported against the generator's five categories rather than remapped onto
    `ReplyIntent`. The engine's vocabulary is deliberately finer — it separates
    `refusal` and `out_of_office` from `non_response`, which the corpus does not
    — and collapsing its answers to fit the corpus would hide exactly the
    distinctions worth having.
    """
    return str(gt.get("category", "unknown"))


#: Kept so a reader of this module can see that intent labels are *not* scored as
#: a strict match, only reported as a confusion matrix. The engine's vocabulary
#: is a superset of the corpus's; a strict comparison would penalise it for being
#: more precise than the annotations.
INTENT_IS_REPORTED_NOT_SCORED = True

__all__ = [
    "INTENT_IS_REPORTED_NOT_SCORED",
    "PROMISE_INTENTS",
    "SCORING_NOTE",
    "GroundTruthError",
    "load_manifest",
    "score_extractions",
]
