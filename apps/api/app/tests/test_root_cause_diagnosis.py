"""Diagnosis and the degradation paths that must never abort a batch.

The tests that matter most here are the ones about being *wrong safely*:

- A window dominated by insufficient-funds across many customers must not be
  called a corridor outage. Getting this backwards reroutes live traffic because
  a few dozen people were short of money on the same afternoon.
- Every provider failure mode — unavailable, rate-limited beyond backoff,
  malformed output twice, deterministic-only — lands on the registered fallback,
  is audited as `source: deterministic`, and lets the batch finish.
- Abstention is a real answer, and it routes to a human rather than to a shrug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.engines.root_cause.config import default_config
from app.engines.root_cause.detection import unaffected_corridor_rates
from app.engines.root_cause.diagnosis import (
    TASK_DIAGNOSE_CORRIDOR,
    build_context,
    classify_deterministically,
    register_root_cause_tasks,
)
from app.engines.root_cause.schemas import (
    Corridor,
    CorridorDiagnosis,
    Detection,
    PaymentAttempt,
    RecommendedAction,
    RootCauseHypothesis,
)
from app.models.enums import CorridorDetermination, ProvenanceSource
from app.services.llm_agent import (
    LLMAgent,
    ProviderUnavailableError,
    RateLimitError,
    ResponseCache,
    TaskRegistry,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def diag_config():
    return default_config().diagnosis


def make_detection(
    *,
    decline_mix: dict[str, int],
    distinct_customers: int,
    level: str = "issuer_method",
    issuer: str | None = "HDFC",
    method: str | None = "upi",
    route_id: str | None = None,
    value_at_risk_paise: int = 500_000,
) -> Detection:
    affected = sum(decline_mix.values())
    return Detection(
        detection_id="det_test",
        corridor=Corridor(level=level, issuer=issuer, method=method, route_id=route_id),
        window_start=datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
        window_end=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        window_attempts=affected + 4,
        window_successes=4,
        baseline_attempts=60,
        baseline_successes=57,
        observed_success_rate=0.25,
        baseline_success_rate=0.95,
        p_value=0.0001,
        confidence=0.9999,
        affected_attempts=affected,
        value_at_risk_paise=value_at_risk_paise,
        decline_mix=decline_mix,
        distinct_failing_customers=distinct_customers,
        cited_rules=["corridor-detection:BW1"],
    )


# ---------------------------------------------------------------------------
# The judgment the engine exists to make
# ---------------------------------------------------------------------------


def test_insufficient_funds_across_many_customers_is_not_a_corridor_outage(diag_config):
    """The expensive mistake, made impossible in the deterministic path.

    Twelve people were short of money in the same six hours. That is twelve
    problems, not one, and rerouting traffic on it would help nobody while
    changing routing for everybody.
    """
    detection = make_detection(
        decline_mix={"INSUFFICIENT_FUNDS": 11, "TRANSACTION_LIMIT_EXCEEDED": 1},
        distinct_customers=12,
    )
    diagnosis = classify_deterministically(
        detection.decline_mix,
        affected_attempts=detection.affected_attempts,
        distinct_failing_customers=detection.distinct_failing_customers,
        config=diag_config,
    )
    assert diagnosis.determination is CorridorDetermination.INDIVIDUAL
    assert diagnosis.hypothesis is RootCauseHypothesis.CUSTOMER_SIDE_CONCENTRATION
    assert diagnosis.recommended_action is not RecommendedAction.REROUTE_TRAFFIC


def test_timeouts_concentrated_on_one_bank_are_systemic(diag_config):
    detection = make_detection(
        decline_mix={"ISSUER_UNAVAILABLE": 11, "GATEWAY_TIMEOUT": 2, "INSUFFICIENT_FUNDS": 1},
        distinct_customers=13,
    )
    diagnosis = classify_deterministically(
        detection.decline_mix,
        affected_attempts=detection.affected_attempts,
        distinct_failing_customers=detection.distinct_failing_customers,
        config=diag_config,
    )
    assert diagnosis.determination is CorridorDetermination.SYSTEMIC
    assert diagnosis.recommended_action is RecommendedAction.REROUTE_TRAFFIC


def test_a_shapeless_mix_abstains_rather_than_guessing(diag_config):
    """`insufficient_evidence` is a first-class answer (corridor-detection:DX2)."""
    diagnosis = classify_deterministically(
        {"DO_NOT_HONOUR": 1, "INSUFFICIENT_FUNDS": 1, "TECHNICAL_DECLINE": 1},
        affected_attempts=3,
        distinct_failing_customers=3,
        config=diag_config,
    )
    assert diagnosis.determination is CorridorDetermination.INSUFFICIENT_EVIDENCE
    assert diagnosis.recommended_action is RecommendedAction.ESCALATE
    assert diagnosis.confidence == 0.0


def test_an_empty_window_abstains(diag_config):
    diagnosis = classify_deterministically(
        {}, affected_attempts=0, distinct_failing_customers=0, config=diag_config
    )
    assert diagnosis.determination is CorridorDetermination.INSUFFICIENT_EVIDENCE


def test_many_customer_side_failures_but_one_repeat_customer_is_not_individual(diag_config):
    """The distinct-customer share is load-bearing, not decoration.

    Ten insufficient-funds declines from two customers is one customer's problem
    repeated, not a population-wide coincidence — so the individual reading is
    not supported either, and the classifier abstains.
    """
    diagnosis = classify_deterministically(
        {"INSUFFICIENT_FUNDS": 10},
        affected_attempts=10,
        distinct_failing_customers=2,
        config=diag_config,
    )
    assert diagnosis.determination is CorridorDetermination.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------


def _attempts() -> list[PaymentAttempt]:
    rows: list[PaymentAttempt] = []
    for i in range(20):
        rows.append(
            PaymentAttempt(
                attempt_id=f"att_{i:04d}",
                batch_id="b",
                customer_id=f"cust_{i:04d}",
                created_at=datetime(2026, 8, 30, 7, i, tzinfo=UTC),
                amount_paise=100_000,
                status="failed" if i < 10 else "success",
                method="upi",
                issuer="HDFC" if i < 12 else "SBIN",
                route_id="rt_upi_npci_a",
                failure_reason_code="ISSUER_UNAVAILABLE" if i < 10 else None,
            )
        )
    return rows


def test_the_prompt_renders_with_the_context_the_fallback_also_sees():
    """One context object feeds both paths, so their results stay comparable."""
    detection = make_detection(
        decline_mix={"ISSUER_UNAVAILABLE": 10}, distinct_customers=10
    )
    context = build_context(detection, _attempts(), ("issuer", "method"))

    from app.services.prompts import load_prompt

    rendered = load_prompt(TASK_DIAGNOSE_CORRIDOR).render(**context)
    assert "HDFC x upi" in rendered
    assert "ISSUER_UNAVAILABLE: 10" in rendered
    assert "SBIN x upi" in rendered, "peer corridors must reach the prompt"
    assert context["_decline_mix_raw"] == {"ISSUER_UNAVAILABLE": 10}


def test_peer_rates_exclude_the_degraded_corridor_itself():
    detection = make_detection(
        decline_mix={"ISSUER_UNAVAILABLE": 10}, distinct_customers=10
    )
    peers = unaffected_corridor_rates(_attempts(), detection, ("issuer", "method"))
    assert all(row["corridor"] != "HDFC x upi" for row in peers)


# ---------------------------------------------------------------------------
# Degradation paths — every one of them, and none of them fatal
# ---------------------------------------------------------------------------


class _Provider:
    """A provider that fails in one specific, chosen way."""

    name = "gemini"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]):
        self.calls += 1
        if self.behaviour == "unavailable":
            raise ProviderUnavailableError("network is down")
        if self.behaviour == "rate_limited":
            raise RateLimitError("429 resource_exhausted")
        if self.behaviour == "malformed":
            from app.services.llm_agent import ProviderResponse

            return ProviderResponse(text="{not json at all", tokens=1, latency_ms=1)
        raise AssertionError(f"unknown behaviour {self.behaviour}")


@pytest.fixture
def registry() -> TaskRegistry:
    reg = TaskRegistry()
    register_root_cause_tasks(reg)
    return reg


def _agent(provider, registry, tmp_path, **settings_overrides) -> LLMAgent:
    from app.core.config import Settings

    settings = Settings(gemini_api_key="test-key", **settings_overrides)
    return LLMAgent(
        settings=settings,
        provider=provider,
        cache=ResponseCache(tmp_path / "cache"),
        registry=registry,
        sleep=lambda _seconds: None,
    )


@pytest.mark.parametrize("behaviour", ["unavailable", "rate_limited", "malformed"])
def test_every_provider_failure_falls_back_and_is_audited_as_deterministic(
    behaviour, registry, tmp_path
):
    """API error, rate limit beyond backoff, and unparseable output twice.

    None of them raise, all of them produce a usable diagnosis, and every one is
    marked `source: deterministic` so no fallback can be mistaken for reasoning.
    """
    provider = _Provider(behaviour)
    agent = _agent(provider, registry, tmp_path)
    detection = make_detection(
        decline_mix={"ISSUER_UNAVAILABLE": 9, "GATEWAY_TIMEOUT": 1}, distinct_customers=10
    )
    result = agent.run(
        TASK_DIAGNOSE_CORRIDOR, build_context(detection, _attempts(), ("issuer", "method"))
    )

    assert result.degraded
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC
    assert result.provenance.model is None
    assert result.provenance.latency_ms is None
    assert result.degradation_reason
    assert isinstance(result.output, CorridorDiagnosis)
    assert result.output.determination is CorridorDetermination.SYSTEMIC

    if behaviour == "rate_limited":
        assert provider.calls == 4, "1s / 2s / 4s, then the fallback"
    if behaviour == "malformed":
        assert provider.calls == 2, "one reparse attempt, then the fallback"


def test_deterministic_only_mode_never_calls_the_provider(registry, tmp_path):
    provider = _Provider("unavailable")
    agent = _agent(provider, registry, tmp_path, llm_deterministic_only=True)
    detection = make_detection(decline_mix={"ISSUER_UNAVAILABLE": 8}, distinct_customers=8)
    result = agent.run(
        TASK_DIAGNOSE_CORRIDOR, build_context(detection, _attempts(), ("issuer", "method"))
    )
    assert provider.calls == 0
    assert result.degraded
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC


def test_an_abstaining_fallback_marks_provenance_abstained(registry, tmp_path):
    """Abstention is recorded, not inferred. `policy-bounds:HE1` keys off it."""
    agent = _agent(_Provider("unavailable"), registry, tmp_path)
    detection = make_detection(
        decline_mix={"DO_NOT_HONOUR": 1, "INSUFFICIENT_FUNDS": 1, "TECHNICAL_DECLINE": 1},
        distinct_customers=3,
    )
    result = agent.run(
        TASK_DIAGNOSE_CORRIDOR, build_context(detection, _attempts(), ("issuer", "method"))
    )
    assert result.abstained
    assert result.provenance.abstained is True
    assert result.output.determination is CorridorDetermination.INSUFFICIENT_EVIDENCE


def test_a_low_confidence_model_answer_is_discarded_for_the_fallback(registry, tmp_path):
    """`corridor-detection:DX1`: a model unsure about a corridor does not reroute it."""

    class _UnsureProvider:
        name = "gemini"
        calls = 0

        def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]):
            from app.services.llm_agent import ProviderResponse

            type(self).calls += 1
            payload = CorridorDiagnosis(
                hypothesis=RootCauseHypothesis.ISSUER_OUTAGE,
                determination=CorridorDetermination.SYSTEMIC,
                recommended_action=RecommendedAction.REROUTE_TRAFFIC,
                confidence=0.2,
                reasoning="Not really sure about this one.",
            )
            return ProviderResponse(text=payload.model_dump_json(), tokens=1, latency_ms=1)

    agent = _agent(_UnsureProvider(), registry, tmp_path)
    detection = make_detection(
        decline_mix={"DO_NOT_HONOUR": 2, "INSUFFICIENT_FUNDS": 1}, distinct_customers=3
    )
    result = agent.run(
        TASK_DIAGNOSE_CORRIDOR, build_context(detection, _attempts(), ("issuer", "method"))
    )
    assert result.degraded
    assert "confidence" in (result.degradation_reason or "")
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC


def test_the_task_is_registered_with_a_fallback_and_a_versioned_prompt(registry):
    """A task without a fallback fails at startup, not mid-batch."""
    task = registry.get(TASK_DIAGNOSE_CORRIDOR)
    assert task.fallback is not None
    assert task.prompt().version
    assert task.min_confidence == default_config().diagnosis.min_confidence
    registry.verify_all_have_fallbacks()
