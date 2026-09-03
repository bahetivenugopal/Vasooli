"""The reasoning layer — Google Gemini behind a thin provider interface.

Every engine calls a model through this wrapper and never through the SDK. The
wrapper exists to make four guarantees, each of which is a demo failure waiting
to happen otherwise:

1. **Structured output, validated.** Unparseable output is retried once with the
   validation error fed back, then falls back. Raw model text never reaches a
   decision, a database column or a customer message.
2. **Response caching**, keyed on resolved prompt + model + prompt version. Not
   an optimisation — it is what stops development re-runs and the pre-demo seed
   run from consuming the daily quota, and what makes a batch reproducible
   without re-spending it.
3. **Rate-limit backoff** at 1s, 2s, 4s, then fallback. Free-tier limits bite per
   minute as well as per day, and a tight batch loop will find the per-minute one.
4. **A registered deterministic fallback per task, enforced at startup.**
   Discovering a missing fallback mid-demo, at the moment the quota runs out, is
   precisely the failure this design prevents.

The boundary that matters most: this module supplies **judgment**.
`policy_engine.py` supplies **permission**. A model response can never widen a
bound. See ADR 0002 and ADR 0003.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.models.provenance import Provenance
from app.services.prompts import Prompt, load_prompt

logger = logging.getLogger(__name__)

PROVIDER_NAME = "gemini"

#: `llm-provider` -> Rate-limit backoff. Three delays, then take the fallback.
#: A batch run that hangs proves nothing, so this never retries indefinitely.
BACKOFF_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Any provider-side failure. Never escapes `LLMAgent.run()` — it degrades."""


class RateLimitError(ProviderError):
    """A 429. Retried with increasing delay before being treated as a failure."""


class ProviderUnavailableError(ProviderError):
    """No key, network failure, timeout, or an SDK error we cannot interpret."""


class TaskRegistrationError(RuntimeError):
    """Raised at startup when a reasoning task has no deterministic fallback.

    Deliberately a startup failure and not a runtime one: a missing fallback
    discovered at runtime is a missing fallback discovered during the demo.
    """


# ---------------------------------------------------------------------------
# The provider interface — one interface, one implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderResponse:
    """What a provider returns, stripped to what this wrapper needs."""

    text: str
    tokens: int | None = None
    latency_ms: int | None = None


class ReasoningProvider(Protocol):
    """The whole provider surface. Kept tiny on purpose.

    No registry, no plugin machinery, no provider discovery. The interface
    exists so the provider is swappable and mockable — not to support providers
    that do not exist.
    """

    name: str

    def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]) -> ProviderResponse:
        """Return raw text for `prompt`, or raise a `ProviderError`."""
        ...


class GeminiProvider:
    """The one implementation. Imports the SDK lazily.

    Lazy because the module must import cleanly with no key and no SDK usable —
    the project is required to complete a full batch run with no credentials at
    all, and an import-time client would break that at the door.
    """

    name = PROVIDER_NAME

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ProviderUnavailableError("no GEMINI_API_KEY configured")
        self._api_key = api_key
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise ProviderUnavailableError(f"google-genai is not importable: {exc}") from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, *, prompt: str, model: str, json_schema: dict[str, Any]) -> ProviderResponse:
        client = self._get_client()
        started = time.monotonic()
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": json_schema,
                },
            )
        except Exception as exc:
            if _looks_like_rate_limit(exc):
                raise RateLimitError(str(exc)) from exc
            raise ProviderUnavailableError(str(exc)) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        text = getattr(response, "text", None)
        if not text:
            raise ProviderUnavailableError("provider returned an empty response")
        return ProviderResponse(text=text, tokens=_extract_tokens(response), latency_ms=latency_ms)


def _looks_like_rate_limit(exc: Exception) -> bool:
    """Detect a 429 without depending on an exception class the SDK may rename.

    Structural detection first (a numeric status code), string matching second.
    Getting this wrong in the safe direction just means one fewer retry before
    the fallback, which is an acceptable degradation.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text


def _extract_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    total = getattr(usage, "total_token_count", None)
    return int(total) if total is not None else None


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------


class ResponseCache:
    """Disk cache keyed on resolved prompt + model + prompt version.

    A cached result is still `source: model` with `cache_hit: true` — hiding
    that a result came from cache would misrepresent how a metric was produced.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(*, prompt: str, model: str, prompt_version: str) -> str:
        digest = hashlib.sha256()
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(model.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(prompt_version.encode("utf-8"))
        return digest.hexdigest()

    def get(self, key: str) -> str | None:
        entry = self._path / f"{key}.json"
        if not entry.exists():
            return None
        try:
            return json.loads(entry.read_text(encoding="utf-8"))["text"]
        except (OSError, ValueError, KeyError):
            # A corrupt cache entry is a cache miss, never a crash. The worst
            # outcome is one live call that should have been free.
            logger.warning("discarding unreadable cache entry %s", key)
            return None

    def put(self, key: str, text: str) -> None:
        try:
            (self._path / f"{key}.json").write_text(
                json.dumps({"text": text}), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - disk problems shouldn't kill a batch
            logger.warning("could not write cache entry %s", key)


# ---------------------------------------------------------------------------
# Tasks and their fallbacks
# ---------------------------------------------------------------------------


class FallbackResult(BaseModel):
    """What a deterministic fallback produces.

    `abstained` is the important field. Some tasks degrade to something boring;
    others must refuse. A fallback that guesses where guessing is expensive is
    worse than no fallback at all, because it looks like an answer.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output: BaseModel | None = None
    abstained: bool = False
    rationale: str
    confidence: float | None = None


#: A fallback takes the same context the prompt would have received.
FallbackFn = Callable[[dict[str, Any]], FallbackResult]


@dataclass(frozen=True)
class ReasoningTask:
    """One registered reasoning task.

    A task cannot exist without a fallback: the constructor of the registry
    refuses it, at startup.
    """

    name: str
    prompt_name: str
    response_model: type[BaseModel]
    fallback: FallbackFn
    description: str = ""
    #: Below this, the answer is treated as unreliable and the fallback applies.
    #: Named per task because "how sure is sure enough" is task-specific.
    min_confidence: float = 0.0

    def prompt(self) -> Prompt:
        return load_prompt(self.prompt_name)


@dataclass
class TaskRegistry:
    """Every reasoning task in the system, and the guarantee that each has an exit."""

    _tasks: dict[str, ReasoningTask] = field(default_factory=dict)

    def register(self, task: ReasoningTask) -> ReasoningTask:
        if task.fallback is None:  # pragma: no cover - typing makes this unreachable
            raise TaskRegistrationError(
                f"task {task.name!r} has no deterministic fallback. Every reasoning "
                "task must register one — see the `llm-provider` skill."
            )
        # Fail early on a prompt that does not exist, rather than at the first
        # call, which in a batch run could be minutes in.
        task.prompt()
        self._tasks[task.name] = task
        return task

    def get(self, name: str) -> ReasoningTask:
        if name not in self._tasks:
            raise TaskRegistrationError(
                f"unknown reasoning task {name!r}. Register it before use — "
                f"known tasks: {sorted(self._tasks)}"
            )
        return self._tasks[name]

    def names(self) -> list[str]:
        return sorted(self._tasks)

    def verify_all_have_fallbacks(self) -> None:
        """Startup check. Called from the FastAPI lifespan.

        This is the load-bearing enforcement point from ADR 0002: a task without
        a fallback fails when the app boots, not when the quota runs out.
        """
        missing = [name for name, task in self._tasks.items() if task.fallback is None]
        if missing:
            raise TaskRegistrationError(
                f"reasoning tasks without a deterministic fallback: {missing}. "
                "The project must complete a full batch run with no API key at all."
            )


#: The process-wide registry. Engines register their tasks at import time.
REGISTRY = TaskRegistry()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class ReasoningResult(BaseModel):
    """A judgment plus the story of how it was produced.

    `provenance` is not optional and never absent: nothing model-derived exists
    anywhere in this system without it.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: str
    output: BaseModel | None
    provenance: Provenance
    confidence: float | None = None
    #: True whenever the model path was not used or did not succeed.
    degraded: bool = False
    #: Why the fallback fired. Null on the happy path; always populated when
    #: `degraded` is true, so a run's degradations are legible after the fact.
    degradation_reason: str | None = None

    @property
    def abstained(self) -> bool:
        return self.provenance.abstained


class StructuredOutput(BaseModel):
    """Base for task response schemas.

    `confidence` lives here because the taxonomy's fail-safe path keys off it:
    a low-confidence classification is treated as HARD.
    """

    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class LLMAgent:
    """The single entry point to the reasoning layer.

    Takes its provider and its clock by injection so the whole degradation
    matrix — rate limits, malformed output, timeouts, forced deterministic mode
    — is testable without a network.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        provider: ReasoningProvider | None = None,
        cache: ResponseCache | None = None,
        registry: TaskRegistry | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or default_settings
        self._registry = registry or REGISTRY
        self._sleep = sleep
        self._cache = cache if cache is not None else ResponseCache(
            self._settings.resolved_llm_cache_path
        )

        if provider is not None:
            self._provider: ReasoningProvider | None = provider
        elif self._settings.llm_provider_enabled:
            try:
                self._provider = GeminiProvider(self._settings.gemini_api_key)
            except ProviderUnavailableError as exc:
                # Not fatal. A missing or broken credential is deterministic
                # mode, not an error — that is the whole point of the design.
                logger.warning("provider unavailable at startup, running deterministic: %s", exc)
                self._provider = None
        else:
            self._provider = None

    @property
    def provider_enabled(self) -> bool:
        """Whether a live call would even be attempted."""
        return self._provider is not None and self._settings.llm_provider_enabled

    def run(self, task_name: str, context: dict[str, Any]) -> ReasoningResult:
        """Run one reasoning task. Never raises for provider problems.

        Every path out of this method returns a `ReasoningResult` with
        provenance attached — including every failure path, because a batch that
        dies halfway proves nothing.
        """
        task = self._registry.get(task_name)

        if not self.provider_enabled:
            reason = (
                "LLM_DETERMINISTIC_ONLY is set"
                if self._settings.llm_deterministic_only
                else "no provider configured"
            )
            return self._fallback(task, context, reason)

        prompt = task.prompt()
        rendered = prompt.render(**context)
        model = self._settings.gemini_model
        cache_key = ResponseCache.key(
            prompt=rendered, model=model, prompt_version=prompt.version
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            parsed = self._parse(task, cached)
            if parsed is not None:
                return self._model_result(
                    task, parsed, prompt, model, cache_hit=True, latency_ms=None, tokens=None
                )
            # A cached response that no longer validates means the schema moved
            # under it. Fall through to a live call rather than trusting it.
            logger.warning("cached response for %s failed validation; ignoring", task.name)

        text, latency_ms, tokens, failure = self._call_with_backoff(
            rendered, model, task.response_model
        )
        if failure is not None:
            return self._fallback(task, context, failure)

        parsed = self._parse(task, text)
        if parsed is None:
            # One retry, with the validation error fed back. Models correct
            # their own shape errors more often than not, and a second failure
            # is a strong signal that the schema and the prompt disagree.
            retry_prompt = (
                f"{rendered}\n\nYour previous response could not be parsed against the "
                f"required JSON schema. Return only valid JSON matching it exactly."
            )
            text, latency_ms, tokens, failure = self._call_with_backoff(
                retry_prompt, model, task.response_model
            )
            if failure is not None:
                return self._fallback(task, context, failure)
            parsed = self._parse(task, text)
            if parsed is None:
                return self._fallback(task, context, "unparseable output twice")

        confidence = getattr(parsed, "confidence", None)
        if confidence is not None and confidence < task.min_confidence:
            return self._fallback(
                task,
                context,
                f"model confidence {confidence:.2f} below the {task.min_confidence:.2f} "
                f"floor for this task",
            )

        self._cache.put(cache_key, text)
        return self._model_result(
            task, parsed, prompt, model, cache_hit=False, latency_ms=latency_ms, tokens=tokens
        )

    # --- internals ---------------------------------------------------------

    def _call_with_backoff(
        self, prompt: str, model: str, response_model: type[BaseModel]
    ) -> tuple[str, int | None, int | None, str | None]:
        """Call the provider, retrying 429s at 1s / 2s / 4s.

        Returns `(text, latency_ms, tokens, failure_reason)`. A non-null failure
        reason means the caller should degrade — it never means "raise".
        """
        assert self._provider is not None  # guarded by `provider_enabled`
        schema = response_model.model_json_schema()

        for attempt, delay in enumerate((*BACKOFF_DELAYS, None)):
            try:
                response = self._provider.generate(
                    prompt=prompt, model=model, json_schema=schema
                )
            except RateLimitError as exc:
                if delay is None:
                    return "", None, None, f"rate-limited beyond backoff: {exc}"
                logger.warning(
                    "rate limited (attempt %d), backing off %.0fs", attempt + 1, delay
                )
                self._sleep(delay)
                continue
            except ProviderError as exc:
                return "", None, None, f"provider unavailable: {exc}"
            return response.text, response.latency_ms, response.tokens, None

        return "", None, None, "rate-limited beyond backoff"  # pragma: no cover

    def _parse(self, task: ReasoningTask, text: str) -> BaseModel | None:
        """Validate raw text against the task's schema. None means unparseable."""
        try:
            return task.response_model.model_validate_json(text)
        except ValidationError as exc:
            logger.warning("task %s returned invalid output: %s", task.name, exc)
            return None

    def _model_result(
        self,
        task: ReasoningTask,
        output: BaseModel,
        prompt: Prompt,
        model: str,
        *,
        cache_hit: bool,
        latency_ms: int | None,
        tokens: int | None,
    ) -> ReasoningResult:
        return ReasoningResult(
            task=task.name,
            output=output,
            confidence=getattr(output, "confidence", None),
            provenance=Provenance.from_model(
                provider=PROVIDER_NAME,
                model=model,
                prompt_version=prompt.version,
                cache_hit=cache_hit,
                latency_ms=latency_ms,
                tokens=tokens,
            ),
        )

    def _fallback(
        self, task: ReasoningTask, context: dict[str, Any], reason: str
    ) -> ReasoningResult:
        """Take the task's registered deterministic fallback.

        Logged at warning level so a run's degradations are visible in the log
        as well as in the audit trail — the two together are what make a mixed
        run honest rather than merely completed.
        """
        logger.warning("task %s degrading to deterministic fallback: %s", task.name, reason)
        result = task.fallback(context)
        return ReasoningResult(
            task=task.name,
            output=result.output,
            confidence=result.confidence,
            provenance=Provenance.deterministic(abstained=result.abstained),
            degraded=True,
            degradation_reason=f"{reason}; fallback: {result.rationale}",
        )
