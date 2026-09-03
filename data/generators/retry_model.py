"""The retry-success probability model — what the generators refuse to decide.

Section 5.5 of the phase brief, and ADR 0005: a generator produces the *world*,
an engine produces the *outcome*. Nothing in `data/samples/` says whether a retry
would have worked. Instead this module exposes a documented, seeded probability
model that an engine samples **at run time**, after it has decided to retry.

Why it matters: if the dataset pre-baked "this retry succeeds", the recovery rate
would be a property of the data rather than of the agent, and the honest answer
to "how do you know the retry would have worked?" would be "we wrote it down".
With this model the answer is: it is a seeded draw against stated per-code
probabilities, every one of them written in `data/DATA_CARD.md` and arguable.

Usage, from an engine (Phases 3-5)::

    model = RetrySuccessModel.load()
    outcome = model.sample(
        rng(batch_seed, "retry", entity_id, str(attempt_no)),
        "INSUFFICIENT_FUNDS",
        hours_since_previous=26.0,
        attempt_number=2,
    )

The `Random` passed in must itself be seeded from the batch seed, or the whole
reproducibility contract stops at this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from app.services.decline_taxonomy import DeclineCode, classify
from data.generators import REPO_ROOT
from data.generators.common import load_config
from data.generators.vocabulary import razorpay_reason_for

DEFAULT_MODEL_PATH = Path("data/generators/configs/retry_success_model.json")


@dataclass(frozen=True)
class RetryOutcome:
    """One sampled retry result, with the probability it was drawn against.

    `probability` is carried so an audit entry can record not just what happened
    but what the odds were — a 0.05 draw that succeeded and a 0.8 draw that
    succeeded are very different evidence about an engine's judgment.
    """

    succeeded: bool
    probability: float
    model_version: str
    #: The code the failure repeats with, when the retry fails. A failed retry
    #: reproduces the original decline; the model does not invent a new one.
    failure_code: str | None = None
    #: `notes.simulate_failure` value for `razorpay_client` simulated mode, when
    #: the code has a doc-verified upstream reason. `None` means this failure
    #: cannot be replayed through the client without inventing an upstream
    #: string — see `vocabulary.razorpay_reason_for`.
    simulate_failure_key: str | None = None


class RetrySuccessModel:
    """Per-decline-code retry success probabilities, loaded from config."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self.model_version: str = config["model_version"]
        self._base: dict[str, float] = config["base_success_probability"]
        self._class_default: dict[str, float] = config["class_default_probability"]
        self._customer_present: dict[str, float] = config.get(
            "customer_present_probability", {}
        )
        self._spacing: list[dict[str, float]] = sorted(
            config["spacing_multipliers"], key=lambda row: row["min_hours"]
        )
        self._attempt_decay: float = config["attempt_decay"]
        self._ceiling: float = config["probability_ceiling"]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MODEL_PATH) -> RetrySuccessModel:
        return cls(load_config(path))

    @property
    def config(self) -> dict[str, Any]:
        """The resolved config, for embedding in a manifest."""
        return self._config

    @property
    def source_path(self) -> str:
        return DEFAULT_MODEL_PATH.as_posix()

    def _spacing_multiplier(self, hours_since_previous: float) -> float:
        multiplier = self._spacing[0]["multiplier"]
        for row in self._spacing:
            if hours_since_previous >= row["min_hours"]:
                multiplier = row["multiplier"]
        return multiplier

    def probability(
        self,
        code: str,
        *,
        hours_since_previous: float,
        attempt_number: int,
        customer_present: bool = False,
    ) -> float:
        """The success probability for one retry, before it is sampled.

        Three effects compose: the code's base rate (does the underlying cause
        clear on its own?), spacing (a retry six minutes later hits the same
        empty balance), and attempt decay (repeated failure is evidence the cause
        is not transient). Hard and terminal classes are hard-zero regardless —
        that is the taxonomy's central claim, and the model must not soften it.
        """
        spec = classify(code)
        base = (
            self._customer_present.get(code)
            if customer_present and code in self._customer_present
            else self._base.get(code, self._class_default.get(spec.decline_class.value, 0.0))
        )
        if base <= 0.0:
            return 0.0
        decayed = base * (self._attempt_decay ** max(0, attempt_number - 1))
        scaled = decayed * self._spacing_multiplier(hours_since_previous)
        return round(min(scaled, self._ceiling), 4)

    def sample(
        self,
        r: Random,
        code: str,
        *,
        hours_since_previous: float,
        attempt_number: int,
        customer_present: bool = False,
    ) -> RetryOutcome:
        """Draw one retry outcome from the model."""
        probability = self.probability(
            code,
            hours_since_previous=hours_since_previous,
            attempt_number=attempt_number,
            customer_present=customer_present,
        )
        succeeded = r.random() < probability
        normalized = DeclineCode(code).value if _is_known(code) else DeclineCode.UNKNOWN.value
        return RetryOutcome(
            succeeded=succeeded,
            probability=probability,
            model_version=self.model_version,
            failure_code=None if succeeded else normalized,
            simulate_failure_key=None if succeeded else razorpay_reason_for(normalized),
        )


def _is_known(code: str) -> bool:
    try:
        DeclineCode(code)
    except ValueError:
        return False
    return True


def resolved_model_path() -> Path:
    return (REPO_ROOT / DEFAULT_MODEL_PATH).resolve()
