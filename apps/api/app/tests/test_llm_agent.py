"""LLM wrapper tests — the whole degradation matrix, with no network.

The property under test throughout: **a batch always completes.** Whatever the
provider does — succeeds, rate-limits forever, returns garbage twice, times out,
or is absent entirely — `run()` returns a `ReasoningResult` with provenance
attached, and the caller carries on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.config import Settings
from app.models.enums import DeclineClass, ProvenanceSource
from app.services.llm_agent import (
    BACKOFF_DELAYS,
    FallbackResult,
    LLMAgent,
    ProviderResponse,
    ProviderUnavailableError,
    RateLimitError,
    ReasoningTask,
    ResponseCache,
    StructuredOutput,
    TaskRegistrationError,
    TaskRegistry,
)
from app.services.reasoning_tasks import (
    TASK_CLASSIFY_UNKNOWN_DECLINE,
    DeclineClassification,
    register_core_tasks,
)

VALID_JSON = '{"decline_class": "SOFT", "confidence": 0.91, "reasoning": "Balance-related."}'

CONTEXT: dict[str, Any] = {
    "normalized_code": "UNKNOWN",
    "raw_reason": "issuer_says_no",
    "description": "Declined.",
    "method": "card",
    "amount_paise": 250_000,
    "attempt_count": 1,
}


class FakeProvider:
    """A scripted provider. Each entry is either a response or an exception."""

    name = "gemini"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]) -> ProviderResponse:
        self.calls += 1
        item = self.script.pop(0) if self.script else ProviderResponse(text=VALID_JSON)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def registry() -> TaskRegistry:
    reg = TaskRegistry()
    register_core_tasks(reg)
    return reg


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "llm_cache")


def make_settings(**overrides) -> Settings:
    base = {
        "gemini_api_key": "test-key",
        "gemini_model": "gemini-3.1-flash-lite",
        "llm_deterministic_only": False,
    }
    return Settings(**{**base, **overrides})


def make_agent(provider, *, registry, cache, settings=None, sleeps=None) -> LLMAgent:
    return LLMAgent(
        settings=settings or make_settings(),
        provider=provider,
        cache=cache,
        registry=registry,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


# ---------------------------------------------------------------------------
# Startup enforcement
# ---------------------------------------------------------------------------


def test_an_unknown_task_is_a_loud_error(registry):
    with pytest.raises(TaskRegistrationError, match="unknown reasoning task"):
        registry.get("no_such_task")


def test_registration_fails_early_on_a_missing_prompt_file(registry):
    """A prompt discovered missing at call time is discovered mid-batch."""
    from app.services.prompts import PromptError

    with pytest.raises(PromptError):
        registry.register(
            ReasoningTask(
                name="ghost",
                prompt_name="no_such_prompt",
                response_model=DeclineClassification,
                fallback=lambda ctx: FallbackResult(rationale="x"),
            )
        )


def test_every_registered_task_passes_the_startup_fallback_check(registry):
    registry.verify_all_have_fallbacks()
    assert TASK_CLASSIFY_UNKNOWN_DECLINE in registry.names()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_valid_structured_output_parses_and_carries_model_provenance(registry, cache):
    agent = make_agent(FakeProvider([ProviderResponse(text=VALID_JSON, tokens=120, latency_ms=340)]),
                       registry=registry, cache=cache)
    result = agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)

    assert isinstance(result.output, DeclineClassification)
    assert result.output.decline_class is DeclineClass.SOFT
    assert result.provenance.source is ProvenanceSource.MODEL
    assert result.provenance.model == "gemini-3.1-flash-lite"
    assert result.provenance.prompt_version == "v1"
    assert result.provenance.tokens == 120
    assert not result.degraded


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_a_cache_hit_is_served_without_a_live_call_and_marked_as_such(registry, cache):
    first = FakeProvider([ProviderResponse(text=VALID_JSON)])
    make_agent(first, registry=registry, cache=cache).run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)
    assert first.calls == 1

    second = FakeProvider([])
    result = make_agent(second, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert second.calls == 0, "a cache hit made a live call"
    assert result.provenance.cache_hit is True
    # A cached result is still model-derived. Hiding that would misrepresent
    # how the metric it feeds was produced.
    assert result.provenance.source is ProvenanceSource.MODEL


def test_the_cache_key_covers_prompt_model_and_version():
    a = ResponseCache.key(prompt="p", model="m", prompt_version="v1")
    assert a != ResponseCache.key(prompt="p2", model="m", prompt_version="v1")
    assert a != ResponseCache.key(prompt="p", model="m2", prompt_version="v1")
    assert a != ResponseCache.key(prompt="p", model="m", prompt_version="v2")


def test_a_corrupt_cache_entry_is_a_miss_not_a_crash(registry, cache, tmp_path):
    provider = FakeProvider([ProviderResponse(text=VALID_JSON)])
    agent = make_agent(provider, registry=registry, cache=cache)
    agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)

    for entry in (tmp_path / "llm_cache").glob("*.json"):
        entry.write_text("not json at all", encoding="utf-8")

    provider2 = FakeProvider([ProviderResponse(text=VALID_JSON)])
    result = make_agent(provider2, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert provider2.calls == 1
    assert not result.degraded


# ---------------------------------------------------------------------------
# Malformed output
# ---------------------------------------------------------------------------


def test_malformed_output_is_retried_once_then_succeeds(registry, cache):
    provider = FakeProvider(
        [ProviderResponse(text="}{ not json"), ProviderResponse(text=VALID_JSON)]
    )
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert provider.calls == 2
    assert not result.degraded
    assert result.provenance.source is ProvenanceSource.MODEL


def test_malformed_output_twice_falls_back_deterministically(registry, cache):
    provider = FakeProvider(
        [ProviderResponse(text="}{ nope"), ProviderResponse(text="still not json")]
    )
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert provider.calls == 2
    assert result.degraded
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC
    assert "unparseable output twice" in result.degradation_reason
    # decline-taxonomy fail-safe: unknown becomes HARD, never SOFT.
    assert result.output.decline_class is DeclineClass.HARD


def test_output_that_violates_the_schema_is_not_trusted(registry, cache):
    """A confidence of 3.0 is not a confidence. Validation catches it."""
    bad = '{"decline_class": "SOFT", "confidence": 3.0, "reasoning": "sure"}'
    provider = FakeProvider([ProviderResponse(text=bad), ProviderResponse(text=bad)])
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert result.degraded


# ---------------------------------------------------------------------------
# Rate limits, failures, timeouts
# ---------------------------------------------------------------------------


def test_a_429_is_retried_with_increasing_delay_then_succeeds(registry, cache):
    sleeps: list[float] = []
    provider = FakeProvider(
        [RateLimitError("429"), RateLimitError("429"), ProviderResponse(text=VALID_JSON)]
    )
    result = make_agent(provider, registry=registry, cache=cache, sleeps=sleeps).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert sleeps == [BACKOFF_DELAYS[0], BACKOFF_DELAYS[1]]
    assert not result.degraded


def test_rate_limited_beyond_backoff_falls_back(registry, cache):
    sleeps: list[float] = []
    provider = FakeProvider([RateLimitError("429")] * 10)
    result = make_agent(provider, registry=registry, cache=cache, sleeps=sleeps).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert sleeps == list(BACKOFF_DELAYS), "backoff did not stop at three delays"
    assert result.degraded
    assert "rate-limited beyond backoff" in result.degradation_reason
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC


def test_an_api_failure_falls_back_and_logs_the_degradation(registry, cache):
    provider = FakeProvider([ProviderUnavailableError("connection reset")])
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert result.degraded
    assert "connection reset" in result.degradation_reason
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC


def test_a_timeout_falls_back(registry, cache):
    provider = FakeProvider([ProviderUnavailableError("deadline exceeded")])
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert result.degraded
    assert result.output is not None, "a fallback still has to produce an answer"


def test_low_confidence_takes_the_fail_safe_path(registry, cache):
    """decline-taxonomy: if confidence is low, treat as HARD."""
    unsure = '{"decline_class": "SOFT", "confidence": 0.2, "reasoning": "no idea really"}'
    provider = FakeProvider([ProviderResponse(text=unsure)])
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert result.degraded
    assert result.output.decline_class is DeclineClass.HARD


# ---------------------------------------------------------------------------
# Deterministic-only mode
# ---------------------------------------------------------------------------


def test_forced_deterministic_mode_bypasses_the_provider_entirely(registry, cache):
    provider = FakeProvider([ProviderResponse(text=VALID_JSON)])
    agent = make_agent(
        provider,
        registry=registry,
        cache=cache,
        settings=make_settings(llm_deterministic_only=True),
    )
    result = agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)
    assert provider.calls == 0, "deterministic-only mode still called the provider"
    assert result.degraded
    assert "LLM_DETERMINISTIC_ONLY" in result.degradation_reason
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC


def test_no_api_key_is_deterministic_mode_not_an_error(registry, cache):
    """A fresh clone with no credentials completes a full run. That is the design."""
    agent = LLMAgent(
        settings=make_settings(gemini_api_key=""),
        cache=cache,
        registry=registry,
    )
    assert not agent.provider_enabled
    result = agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)
    assert result.degraded
    assert result.output.decline_class is DeclineClass.HARD


def test_a_broken_api_key_degrades_rather_than_crashing(registry, cache):
    """Deliberately breaking the key must not take the batch down."""
    provider = FakeProvider([ProviderUnavailableError("API key not valid")])
    result = make_agent(provider, registry=registry, cache=cache).run(
        TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT
    )
    assert result.degraded
    assert result.provenance.source is ProvenanceSource.DETERMINISTIC
    assert result.provenance.model is None
    assert result.provenance.latency_ms is None


# ---------------------------------------------------------------------------
# Fallback contract shapes
# ---------------------------------------------------------------------------


def test_an_abstaining_fallback_marks_provenance_and_returns_no_output(registry, cache):
    """The Engine 3 shape: refuse rather than guess, and say so."""

    class Extraction(StructuredOutput):
        promised_amount_paise: int

    registry.register(
        ReasoningTask(
            name="abstaining_task",
            prompt_name=TASK_CLASSIFY_UNKNOWN_DECLINE,
            response_model=Extraction,
            fallback=lambda ctx: FallbackResult(
                output=None,
                abstained=True,
                rationale="a fabricated promise would corrupt the register",
            ),
        )
    )
    agent = make_agent(
        FakeProvider([ProviderUnavailableError("down")]), registry=registry, cache=cache
    )
    result = agent.run("abstaining_task", CONTEXT)
    assert result.abstained
    assert result.output is None
    assert result.provenance.abstained is True


def test_a_result_always_carries_provenance(registry, tmp_path: Path):
    """Nothing model-derived exists in this system without it — no exceptions."""
    scripts = {
        "ok": [ProviderResponse(text=VALID_JSON)],
        "rate_limited": [RateLimitError("429")] * 5,
        "unavailable": [ProviderUnavailableError("boom")],
        "garbage": [ProviderResponse(text="junk"), ProviderResponse(text="junk")],
    }
    for name, script in scripts.items():
        # A cache per case, so one case's stored response cannot answer another's.
        agent = make_agent(
            FakeProvider(script),
            registry=registry,
            cache=ResponseCache(tmp_path / name),
        )
        result = agent.run(TASK_CLASSIFY_UNKNOWN_DECLINE, CONTEXT)
        assert isinstance(result.provenance.source, ProvenanceSource)
        assert result.provenance.provider


def test_the_response_model_is_a_pydantic_schema(registry):
    task = registry.get(TASK_CLASSIFY_UNKNOWN_DECLINE)
    assert issubclass(task.response_model, BaseModel)
    assert "decline_class" in task.response_model.model_json_schema()["properties"]
