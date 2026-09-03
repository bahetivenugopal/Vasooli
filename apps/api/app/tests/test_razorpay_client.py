"""Razorpay client tests. Fixtures and mocks only — no live network calls.

Three things matter here: a non-test key can never start the client, upstream
error reasons normalize to the taxonomy correctly, and simulated mode is
genuinely deterministic rather than merely offline.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.enums import DeclineClass, Engine, EntityType, Outcome
from app.services.decline_taxonomy import DeclineCode
from app.services.razorpay_client import (
    RazorpayClient,
    RazorpayConfigurationError,
    RazorpayMode,
    normalize_error,
    normalize_reason,
)

NOTES = {"batch_id": "batch_test_001"}


def make_settings(**overrides) -> Settings:
    base = {
        "razorpay_key_id": "rzp_test_abc123",
        "razorpay_key_secret": "secret",
        "razorpay_mode": "simulated",
    }
    return Settings(**{**base, **overrides})


@pytest.fixture
def client() -> RazorpayClient:
    return RazorpayClient(settings=make_settings())


# ---------------------------------------------------------------------------
# Key validation — there is no live path in this repository
# ---------------------------------------------------------------------------


def test_a_live_key_is_refused_loudly():
    """Not a warning, not a fallback — a refusal, with instructions to rotate."""
    with pytest.raises(ValueError, match="live Razorpay key"):
        make_settings(razorpay_key_id="rzp_live_abc123")


def test_a_key_with_no_recognised_prefix_is_refused():
    settings = make_settings(razorpay_key_id="sk_test_something", razorpay_mode="test")
    with pytest.raises(RazorpayConfigurationError, match="rzp_test_"):
        RazorpayClient(settings=settings)


def test_test_mode_requires_a_key():
    settings = make_settings(razorpay_key_id="", razorpay_mode="test")
    with pytest.raises(RazorpayConfigurationError, match="RAZORPAY_KEY_ID"):
        RazorpayClient(settings=settings)


def test_test_mode_requires_a_secret():
    settings = make_settings(razorpay_key_secret="", razorpay_mode="test")
    with pytest.raises(RazorpayConfigurationError, match="SECRET"):
        RazorpayClient(settings=settings)


def test_simulated_mode_needs_no_credentials_at_all():
    """A fresh clone with no keys still runs a full batch."""
    client = RazorpayClient(settings=make_settings(razorpay_key_id="", razorpay_key_secret=""))
    assert client.mode is RazorpayMode.SIMULATED
    assert client.create_order(amount_paise=1000, receipt="r", notes=NOTES).ok


def test_an_unknown_mode_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="RAZORPAY_MODE"):
        make_settings(razorpay_mode="live")


# ---------------------------------------------------------------------------
# Error normalization — the one-way boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("insufficient_fund", DeclineCode.INSUFFICIENT_FUNDS),
        ("payment_timed_out", DeclineCode.GATEWAY_TIMEOUT),
        ("gateway_technical_error", DeclineCode.NETWORK_ERROR),
        ("payment_cancelled", DeclineCode.CUSTOMER_CANCELLED),
        ("card_declined", DeclineCode.DO_NOT_HONOUR),
        ("card_disabled_for_online_payments", DeclineCode.CARD_DISABLED_ONLINE),
        ("card_number_invalid", DeclineCode.INVALID_ACCOUNT),
        ("authentication_failed", DeclineCode.AFA_REQUIRED),
    ],
)
def test_verified_reasons_map_to_their_taxonomy_codes(reason, expected):
    """Every row is reproducible with a specific error-scenario test card."""
    assert normalize_reason(reason) is expected


def test_an_unmapped_reason_fails_closed_to_unknown():
    """Never SOFT. Defaulting to soft means defaulting to 'keep charging'."""
    assert normalize_reason("something_new_from_the_issuer") is DeclineCode.UNKNOWN
    assert normalize_reason(None) is DeclineCode.UNKNOWN


def test_card_declined_is_ambiguous_not_soft():
    """The issuer said no and nothing more — a reduced budget, not a full one."""
    from app.services.decline_taxonomy import classify

    assert classify(normalize_reason("card_declined")).decline_class is DeclineClass.AMBIGUOUS


def test_a_razorpay_error_object_normalizes_field_for_field():
    failure = normalize_error(
        {
            "error": {
                "code": "BAD_REQUEST_ERROR",
                "description": "Insufficient balance.",
                "field": None,
                "source": "customer",
                "step": "payment_authentication",
                "reason": "insufficient_fund",
                "metadata": {},
            }
        }
    )
    assert failure.code == "BAD_REQUEST_ERROR"
    assert failure.raw_reason == "insufficient_fund"
    assert failure.normalized_code is DeclineCode.INSUFFICIENT_FUNDS
    assert failure.spec.decline_class is DeclineClass.SOFT
    assert failure.step == "payment_authentication"


def test_a_bare_error_object_normalizes_too():
    """The SDK and raw HTTP disagree about the wrapper; both are accepted."""
    failure = normalize_error({"code": "GATEWAY_ERROR", "reason": "payment_timed_out"})
    assert failure.normalized_code is DeclineCode.GATEWAY_TIMEOUT
    assert failure.spec.reconcile_first, "a timeout may have landed; reconcile first"


# ---------------------------------------------------------------------------
# Simulated mode
# ---------------------------------------------------------------------------


def test_simulated_ids_are_deterministic(client):
    """Same request, same id, every run — that is what makes a batch comparable."""
    first = client.create_order(amount_paise=250_000, receipt="rcpt_1", notes=NOTES)
    second = client.create_order(amount_paise=250_000, receipt="rcpt_1", notes=NOTES)
    assert first.data["id"] == second.data["id"]
    assert first.data["id"].startswith("order_")


def test_different_requests_get_different_ids(client):
    a = client.create_order(amount_paise=250_000, receipt="rcpt_1", notes=NOTES)
    b = client.create_order(amount_paise=250_001, receipt="rcpt_1", notes=NOTES)
    assert a.data["id"] != b.data["id"]


def test_simulated_failures_are_explicitly_requested_never_invented(client):
    """A simulator that invents failures produces a meaningless recovery rate."""
    ok = client.create_order(amount_paise=1000, receipt="r", notes=NOTES)
    assert ok.ok

    failed = client.create_order(
        amount_paise=1000,
        receipt="r",
        notes={**NOTES, "simulate_failure": "insufficient_fund"},
    )
    assert not failed.ok
    assert failed.failure.normalized_code is DeclineCode.INSUFFICIENT_FUNDS
    assert failed.failure.raw_reason == "insufficient_fund"


def test_an_unknown_simulated_scenario_fails_rather_than_silently_succeeding(client):
    result = client.create_order(
        amount_paise=1000, receipt="r", notes={**NOTES, "simulate_failure": "who_knows"}
    )
    assert not result.ok
    assert result.failure.normalized_code is DeclineCode.UNKNOWN


def test_every_simulated_result_names_its_mode(client):
    assert client.create_order(amount_paise=1, receipt="r", notes=NOTES).mode is (
        RazorpayMode.SIMULATED
    )


def test_the_operations_later_phases_need_all_exist(client):
    assert client.fetch_payment("pay_x").ok
    assert client.create_payment_link(amount_paise=1000, description="d", notes=NOTES).ok
    assert client.fetch_subscription("sub_x").ok
    assert client.pause_subscription("sub_x").ok
    assert client.cancel_subscription("sub_x").ok


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_a_call_is_audited_with_its_mode_visible(trail):
    """Simulated mode is explicit in the trail, never silent."""
    trail.start_batch(batch_id="batch_rzp", engine=Engine.ROOT_CAUSE, seed=1)
    client = RazorpayClient(settings=make_settings(), audit=trail)
    result = client.create_order(amount_paise=250_000, receipt="r", notes=NOTES)

    entry = client.record_call(
        result,
        batch_id="batch_rzp",
        engine=Engine.ROOT_CAUSE,
        entity_type=EntityType.PAYMENT,
        entity_id="pay_1",
        amount_at_risk_paise=250_000,
    )
    read = trail.read(entry)
    assert read.outcome is Outcome.SUCCESS
    assert read.metadata["razorpay_mode"] == "simulated"


def test_a_failed_call_is_audited_citing_the_taxonomy_rule(trail):
    trail.start_batch(batch_id="batch_rzp", engine=Engine.ROOT_CAUSE, seed=1)
    client = RazorpayClient(settings=make_settings(), audit=trail)
    result = client.create_order(
        amount_paise=250_000,
        receipt="r",
        notes={**NOTES, "simulate_failure": "card_number_invalid"},
    )
    entry = client.record_call(
        result,
        batch_id="batch_rzp",
        engine=Engine.ROOT_CAUSE,
        entity_type=EntityType.PAYMENT,
        entity_id="pay_1",
    )
    read = trail.read(entry)
    assert read.outcome is Outcome.FAILURE
    assert read.reason_code == DeclineCode.INVALID_ACCOUNT.value
    assert read.authorising_rule == "decline-taxonomy:HARD.INVALID_ACCOUNT"


def test_recording_without_an_audit_trail_is_a_loud_error(client):
    result = client.create_order(amount_paise=1, receipt="r", notes=NOTES)
    with pytest.raises(RazorpayConfigurationError, match="audit trail"):
        client.record_call(
            result,
            batch_id="b",
            engine=Engine.ROOT_CAUSE,
            entity_type=EntityType.PAYMENT,
            entity_id="p",
        )
