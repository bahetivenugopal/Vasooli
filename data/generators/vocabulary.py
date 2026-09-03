"""The failure vocabulary the generators are allowed to emit.

Two hard rules, both from the `data-synthesizer` agent brief:

1. **Never invent a decline reason.** Every `failure_reason_code` a generator
   writes is a `DeclineCode` member imported from the shared core, so a typo is
   an import-time failure rather than a record the engines silently cannot
   classify.
2. **Keep the upstream link honest.** `razorpay_reason` is populated *only* for
   the eight reasons doc-verified against Razorpay's error-scenario test cards
   (see the `decline-taxonomy` and `razorpay-api` skills). For every other code
   it is `null`, because inventing an upstream string would mean a simulated
   replay normalizes it to `UNKNOWN` and quietly mislabels the failure.

The `UNMAPPED_RAW_REASONS` list is the deliberate exception: strings that are
*meant* to be unrecognised, so the `UNKNOWN` fail-safe path gets exercised by
real data rather than only by a unit test.
"""

from __future__ import annotations

from app.models.enums import DeclineClass
from app.services.decline_taxonomy import DeclineCode, classify
from app.services.razorpay_client import REASON_MAP

#: normalized code -> the verified upstream `error.reason` that produces it.
#: Inverted from `REASON_MAP` so the two can never disagree.
RAZORPAY_REASON_BY_CODE: dict[DeclineCode, str] = {
    code: reason for reason, code in REASON_MAP.items()
}

#: Raw reasons with no entry in `REASON_MAP`, on purpose. A failure carrying one
#: of these normalizes to `UNKNOWN`, which is exactly the path that must be
#: exercised: an unrecognised reason has to fail closed to HARD, and a batch with
#: no unrecognised reasons never proves that it does.
UNMAPPED_RAW_REASONS: tuple[str, ...] = (
    "issuer_node_offline",
    "upi_deemed_pending",
    "bank_reference_mismatch",
)


def razorpay_reason_for(code: str) -> str | None:
    """The upstream reason that reproduces `code`, or `None` if none is verified.

    `None` is meaningful, not missing data: it says this failure cannot be
    replayed through `razorpay_client` in simulated mode without inventing an
    upstream string, so an engine must treat the normalized code as the record of
    what happened rather than round-tripping it.
    """
    try:
        return RAZORPAY_REASON_BY_CODE.get(DeclineCode(code))
    except ValueError:
        return None


def decline_class_of(code: str) -> DeclineClass:
    """The taxonomy class for a code. Used for measured distributions only."""
    return classify(code).decline_class


def validate_codes(codes: object) -> list[str]:
    """Assert every configured code exists in the taxonomy, and return them.

    Called once per generator run, before a single record is produced. A config
    naming a code the engines cannot classify should fail loudly at start, not
    show up three phases later as an unexplained `UNKNOWN` in a metric.
    """
    if not isinstance(codes, list | tuple):
        raise TypeError(f"expected a list of decline codes, got {type(codes).__name__}")
    validated: list[str] = []
    for code in codes:
        try:
            validated.append(DeclineCode(code).value)
        except ValueError as exc:
            raise ValueError(
                f"{code!r} is not in the decline taxonomy. Add it to the "
                "`decline-taxonomy` skill and `services/decline_taxonomy.py` "
                "first — generators never invent reasons."
            ) from exc
    return validated
