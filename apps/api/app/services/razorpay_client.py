"""The Razorpay client — test mode only, with a deterministic simulated mode.

Two hard rules and one design decision.

**Test mode only, always.** A key that does not start with `rzp_test_` fails
loudly, at construction. There is no live code path in this repository, and
there is deliberately no configuration that could create one.

**Normalize at the boundary.** Razorpay's `error.reason` becomes a
`decline-taxonomy` code exactly once — here. Engines never see a raw upstream
string, and never see a raw SDK exception.

**Simulated mode is a first-class mode, not a shortcut.** A batch run that
depends on live network conditions is not reproducible, and a demo that depends
on them is a demo that can fail for reasons unrelated to the product. Simulated
mode is deterministic, fixture-driven, and *visible in the audit trail* on every
call — never silent. See ADR 0004.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.models.enums import Action, Engine, EntityType, Outcome
from app.models.provenance import Provenance
from app.services.audit_trail import AuditTrail
from app.services.decline_taxonomy import DeclineCode, DeclineSpec, classify

logger = logging.getLogger(__name__)

TEST_KEY_PREFIX = "rzp_test_"
LIVE_KEY_PREFIX = "rzp_live_"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "razorpay"

#: `razorpay-api` -> Non-negotiables: every call carries `notes.batch_id`, the
#: only way to tie a Razorpay-side object back to a reproducible batch.
BATCH_NOTE_KEY = "batch_id"

#: In simulated mode, the request note that selects a failure scenario. The
#: synthetic data generator (Phase 2) sets it; nothing infers it.
SIMULATE_NOTE_KEY = "simulate_failure"

#: Audit rule id for a raw API interaction. Not a policy decision — a record
#: that a call happened and what came back.
API_CALL_RULE = "razorpay-api:call.logged"


class RazorpayMode(StrEnum):
    """How calls are fulfilled. Always recorded on the audit entry."""

    #: Deterministic, offline, fixture-driven. The default.
    SIMULATED = "simulated"
    #: Real HTTP calls against Razorpay **test mode**. Never live.
    TEST = "test"


class RazorpayConfigurationError(RuntimeError):
    """Raised for a missing, malformed, or non-test key. Fails at construction."""


@dataclass(frozen=True)
class RazorpayFailure:
    """The one internal error shape. No SDK exception escapes past this.

    `normalized_code` is what engines branch on; `raw_reason` is kept so a
    mapping gap is visible in the trail rather than invisible in a log.
    """

    code: str
    description: str
    raw_reason: str | None
    normalized_code: DeclineCode
    source: str | None = None
    step: str | None = None
    field: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def spec(self) -> DeclineSpec:
        return classify(self.normalized_code)


@dataclass(frozen=True)
class RazorpayResult:
    """The one internal result shape. Never raises past the client."""

    ok: bool
    operation: str
    mode: RazorpayMode
    data: dict[str, Any] | None = None
    failure: RazorpayFailure | None = None


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------

#: Verified against Razorpay's error-scenario test cards on 2026-09-03 — see the
#: `decline-taxonomy` and `razorpay-api` skills. Anything absent here takes the
#: `UNKNOWN` path **by design**: an unrecognised reason must never default to
#: SOFT, because that means defaulting to "keep charging this customer".
REASON_MAP: dict[str, DeclineCode] = {
    "insufficient_fund": DeclineCode.INSUFFICIENT_FUNDS,
    "payment_timed_out": DeclineCode.GATEWAY_TIMEOUT,
    "gateway_technical_error": DeclineCode.NETWORK_ERROR,
    "payment_cancelled": DeclineCode.CUSTOMER_CANCELLED,
    "card_declined": DeclineCode.DO_NOT_HONOUR,
    "card_disabled_for_online_payments": DeclineCode.CARD_DISABLED_ONLINE,
    "card_number_invalid": DeclineCode.INVALID_ACCOUNT,
    "authentication_failed": DeclineCode.AFA_REQUIRED,
}


def normalize_reason(reason: str | None) -> DeclineCode:
    """Map an upstream `error.reason` onto a taxonomy code, failing closed."""
    if not reason:
        return DeclineCode.UNKNOWN
    return REASON_MAP.get(reason.strip().lower(), DeclineCode.UNKNOWN)


def normalize_error(payload: dict[str, Any]) -> RazorpayFailure:
    """Turn a Razorpay error object into the internal failure shape.

    Accepts both the wrapped `{"error": {...}}` form and a bare error object,
    because the SDK and raw HTTP disagree about which one you get.
    """
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    reason = error.get("reason")
    return RazorpayFailure(
        code=error.get("code") or "UNKNOWN_ERROR",
        description=error.get("description") or "No description supplied by the provider.",
        raw_reason=reason,
        normalized_code=normalize_reason(reason),
        source=error.get("source"),
        step=error.get("step"),
        field=error.get("field"),
        metadata=error.get("metadata"),
    )


# ---------------------------------------------------------------------------
# Fixtures for simulated mode
# ---------------------------------------------------------------------------


@lru_cache
def _load_fixtures() -> dict[str, Any]:
    path = FIXTURE_DIR / "error_scenarios.json"
    if not path.exists():
        raise RazorpayConfigurationError(
            f"simulated mode needs fixtures at {path}. Simulated mode is a real "
            "mode, not a stub — it must be as reproducible as the fixtures allow."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _deterministic_id(prefix: str, payload: dict[str, Any]) -> str:
    """A stable Razorpay-shaped id derived from the request.

    Same request, same id, every run. That is what lets a seeded batch be
    re-run and compared entry for entry.
    """
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{prefix}{digest[:14]}"


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class RazorpayClient:
    """A thin, well-typed wrapper over the Razorpay test-mode API.

    Every method returns a `RazorpayResult` and never raises for an API-level
    problem. A crashed batch run proves nothing; a batch of recorded failures
    proves quite a lot.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        audit: AuditTrail | None = None,
        mode: RazorpayMode | None = None,
    ) -> None:
        self._settings = settings or default_settings
        self._audit = audit
        self._mode = mode or RazorpayMode(self._settings.razorpay_mode)
        self._sdk: Any | None = None

        if self._mode is RazorpayMode.TEST:
            self._require_test_key()

    # --- key validation ----------------------------------------------------

    def _require_test_key(self) -> None:
        """Fail loudly rather than let a live key path exist at all."""
        key = self._settings.razorpay_key_id
        if not key:
            raise RazorpayConfigurationError(
                "RAZORPAY_KEY_ID is not set. Either configure a test key or run "
                f"with RAZORPAY_MODE={RazorpayMode.SIMULATED.value}."
            )
        if key.startswith(LIVE_KEY_PREFIX):
            raise RazorpayConfigurationError(
                "A live Razorpay key was supplied. Vasooli is test-mode only. "
                "Rotate that key now — it has been exposed to this process."
            )
        if not key.startswith(TEST_KEY_PREFIX):
            raise RazorpayConfigurationError(
                f"RAZORPAY_KEY_ID must start with {TEST_KEY_PREFIX!r}; got "
                f"{key[:12]!r}. Refusing to start against an unrecognised key."
            )
        if not self._settings.razorpay_key_secret:
            raise RazorpayConfigurationError("RAZORPAY_KEY_SECRET is not set")

    @property
    def mode(self) -> RazorpayMode:
        return self._mode

    def _client(self) -> Any:
        """Lazily construct the SDK client. Only ever reached in TEST mode."""
        if self._sdk is None:
            import razorpay

            self._sdk = razorpay.Client(
                auth=(self._settings.razorpay_key_id, self._settings.razorpay_key_secret)
            )
        return self._sdk

    # --- the operations later phases need ---------------------------------

    def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, Any], currency: str = "INR"
    ) -> RazorpayResult:
        """Create an order. Engine 1's payment-attempt primitive."""
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "notes": notes,
        }
        return self._execute("order.create", payload, id_prefix="order_")

    def fetch_payment(self, payment_id: str) -> RazorpayResult:
        """Fetch one payment's current status. The reconciliation primitive.

        `GATEWAY_TIMEOUT` and `NETWORK_ERROR` both mean the original debit may
        have landed, so nothing retries without calling this first.
        """
        return self._execute("payment.fetch", {"id": payment_id}, id_prefix="pay_")

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        description: str,
        notes: dict[str, Any],
        currency: str = "INR",
    ) -> RazorpayResult:
        """Create a payment link — the customer-present recovery instrument.

        Used by Engines 2 and 3 whenever a silent retry is not permitted: a dead
        instrument, an AFA requirement, an overdue invoice.
        """
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "description": description,
            "notes": notes,
        }
        return self._execute("payment_link.create", payload, id_prefix="plink_")

    def fetch_subscription(self, subscription_id: str) -> RazorpayResult:
        """Fetch a subscription/mandate. Engine 2's state check."""
        return self._execute(
            "subscription.fetch", {"id": subscription_id}, id_prefix="sub_"
        )

    def cancel_subscription(self, subscription_id: str) -> RazorpayResult:
        """Cancel a mandate. **Terminal** — prefer `pause_subscription` to stop
        a schedule without destroying the mandate."""
        return self._execute(
            "subscription.cancel", {"id": subscription_id}, id_prefix="sub_"
        )

    def pause_subscription(self, subscription_id: str) -> RazorpayResult:
        """Pause a mandate — the compliant way to stop a schedule reversibly."""
        return self._execute(
            "subscription.pause", {"id": subscription_id}, id_prefix="sub_"
        )

    # --- execution ---------------------------------------------------------

    def _execute(
        self, operation: str, payload: dict[str, Any], *, id_prefix: str
    ) -> RazorpayResult:
        if self._mode is RazorpayMode.SIMULATED:
            return self._simulate(operation, payload, id_prefix=id_prefix)
        return self._call_live_test_api(operation, payload)

    def _simulate(
        self, operation: str, payload: dict[str, Any], *, id_prefix: str
    ) -> RazorpayResult:
        """Fulfil a call from fixtures, deterministically.

        A failure happens only when the request explicitly asks for one via
        `notes.simulate_failure`. Nothing is inferred and nothing is random: a
        simulator that invents failures produces a recovery rate that means
        nothing, because nobody can say what it was measured against.
        """
        scenario = (payload.get("notes") or {}).get(SIMULATE_NOTE_KEY)
        if scenario:
            fixtures = _load_fixtures()
            error = fixtures.get(scenario)
            if error is None:
                # An unknown scenario is itself a failure, mapped through the
                # UNKNOWN path — never silently a success.
                logger.warning("no fixture for simulated scenario %r", scenario)
                error = {
                    "code": "BAD_REQUEST_ERROR",
                    "description": f"No fixture for simulated scenario {scenario!r}.",
                    "reason": scenario,
                }
            return RazorpayResult(
                ok=False,
                operation=operation,
                mode=self._mode,
                failure=normalize_error({"error": error}),
            )

        return RazorpayResult(
            ok=True,
            operation=operation,
            mode=self._mode,
            data={
                "id": payload.get("id") or _deterministic_id(id_prefix, payload),
                "status": "created",
                "simulated": True,
                **{k: v for k, v in payload.items() if k != "id"},
            },
        )

    def _call_live_test_api(self, operation: str, payload: dict[str, Any]) -> RazorpayResult:
        """Dispatch to the SDK, catching everything.

        `razorpay-api` -> Non-negotiables #1: never let a Razorpay exception
        escape uncaught. Catch, normalize, return a typed result.
        """
        resource, _, method = operation.partition(".")
        try:
            client = self._client()
            handler = getattr(getattr(client, resource), method)
            data = handler(payload["id"]) if "id" in payload else handler(payload)
        except Exception as exc:
            payload_error = getattr(exc, "error", None)
            if isinstance(payload_error, dict):
                failure = normalize_error(payload_error)
            else:
                failure = RazorpayFailure(
                    code="TRANSPORT_ERROR",
                    description=str(exc),
                    raw_reason=None,
                    normalized_code=DeclineCode.UNKNOWN,
                )
            return RazorpayResult(
                ok=False, operation=operation, mode=self._mode, failure=failure
            )
        return RazorpayResult(ok=True, operation=operation, mode=self._mode, data=data)

    # --- audit -------------------------------------------------------------

    def record_call(
        self,
        result: RazorpayResult,
        *,
        batch_id: str,
        engine: Engine,
        entity_type: EntityType,
        entity_id: str,
        amount_at_risk_paise: int = 0,
        timestamp: datetime | None = None,
    ) -> Any:
        """Write a call and its result to the audit trail.

        The mode goes into the entry's metadata on every call, which is what
        keeps simulated mode explicit and visible rather than silent. A reader
        can always tell whether a number came from a real test-mode call.

        `timestamp` is the **run clock**, and passing it matters more than it
        looks. Every batch in this project runs against a fixed historical
        `as_of`, so an entry stamped with `utcnow()` lands months after the
        decision that authorised it — the entity's timeline stops being ordered,
        and the trail claims a Razorpay call happened long after the action it
        was part of. Left optional so an ad-hoc call outside a batch still
        records honestly as having happened now.
        """
        if self._audit is None:
            raise RazorpayConfigurationError(
                "this client was constructed without an audit trail; pass one to "
                "record calls (razorpay-api -> Non-negotiables #3)"
            )
        failure = result.failure
        return self._audit.record(
            batch_id=batch_id,
            engine=engine,
            entity_type=entity_type,
            entity_id=entity_id,
            action=Action.API_CALL,
            outcome=Outcome.SUCCESS if result.ok else Outcome.FAILURE,
            reason_code=failure.normalized_code.value if failure else "OK",
            authorising_rule=failure.spec.rule_id if failure else API_CALL_RULE,
            rationale=(
                f"{result.operation} in {result.mode.value} mode: "
                + (
                    f"{failure.code} / {failure.raw_reason} -> "
                    f"{failure.normalized_code.value}"
                    if failure
                    else "succeeded"
                )
            ),
            provenance=Provenance.deterministic(),
            amount_at_risk_paise=amount_at_risk_paise,
            timestamp=timestamp,
            metadata={
                "razorpay_mode": result.mode.value,
                "operation": result.operation,
                "raw_reason": failure.raw_reason if failure else None,
            },
        )
