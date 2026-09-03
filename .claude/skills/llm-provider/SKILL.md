---
name: llm-provider
description: The reasoning layer — Google Gemini behind a thin provider interface, response caching, rate-limit backoff, the deterministic fallback contract every reasoning task must satisfy, prompt versioning, and the provenance object attached to every model-derived result. Load BEFORE writing any code that calls a model, registers a reasoning task, or reads a reasoning result.
---

# LLM Provider

Any code that calls a model reads this first.

## Provider

| | |
| --- | --- |
| Provider | Google Gemini |
| SDK | `google-genai` (2.x — Gemini 3 features need ≥ 1.51.0) |
| Import | `from google import genai` |
| Model | Resolved from `GEMINI_MODEL`. Default `gemini-3.1-flash-lite` |
| Wrapper | `apps/api/app/services/llm_agent.py` |
| Prompts | `apps/api/app/services/prompts/` |

> **Model-id note.** The change request specified
> `gemini-3.1-flash-lite-preview`. We default to its GA counterpart
> `gemini-3.1-flash-lite` instead, because a `-preview` id can be retired
> without notice and taking that risk buys nothing here.
>
> **Corrected 2026-09-03.** An earlier revision of this note claimed the preview
> id was shut down and that calls to it fail. That was wrong. Verified live
> against this project's key on 2026-09-03: `gemini-3.1-flash-lite-preview` is
> still listed by `models.list()` and still returns a normal response. The
> default is unchanged — GA is still the right call — but the *reason* is
> stability, not unavailability. Left visible rather than quietly edited,
> because "verify, do not remember" applies to this file too.
>
> Because the model is resolved from config, switching is a one-line change.

**Never hardcode a model at a call site.** It comes from `settings.gemini_model`,
always, so there is exactly one place to change it and exactly one value flowing
into provenance.

## The abstraction — keep it small

One interface, one implementation. Engines call the interface; **engines never
touch the SDK directly**.

No registry, no plugin machinery, no provider-discovery layer. The interface
exists so the provider is swappable and mockable, not to support providers we do
not have. If a second provider is ever genuinely needed, adding it then is
cheaper than carrying the machinery now.

## Config

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Provider credential. **Blank is valid** — it means deterministic mode |
| `GEMINI_MODEL` | Defaults to `gemini-3.1-flash-lite` |
| `LLM_DETERMINISTIC_ONLY` | When true, skip the provider entirely and use registered fallbacks throughout |
| `LLM_CACHE_PATH` | Where cached responses are persisted. Relative paths anchor to `apps/api/` |

`settings.llm_provider_enabled` is the single check: it is true only when a key
is present **and** deterministic mode is off. A missing key is treated as
deterministic mode, never as an error — a fresh clone with no credentials must
complete a full batch run.

## Four wrapper capabilities

### 1. Response caching

Key on a hash of **the resolved prompt + the model + the prompt version**.
Persist to disk under `LLM_CACHE_PATH`.

This is what stops development re-runs and pre-demo seed runs from consuming the
daily free-tier quota, and what makes a batch reproducible without re-spending
it. A seeded batch re-run should be nearly free.

Record every hit as `cache_hit: true` in provenance — a cached result is still
model-derived, and hiding that it came from cache would misrepresent how a
metric was produced.

### 2. Rate-limit backoff

Retry `429` with increasing delay — **1s, 2s, 4s** — before treating the call as
failed. Free-tier limits apply **per minute as well as per day**, and a tight
batch loop will hit the per-minute one long before the daily one.

After the third delay, stop and take the fallback. Do not retry indefinitely; a
batch run that hangs proves nothing.

> Check current free-tier RPM/RPD limits against Google's live pricing docs
> before tuning batch concurrency. Do not code against remembered numbers.

### 3. The deterministic fallback contract

> **Every reasoning task must register a fallback. A task without one fails at
> startup, not at runtime.**

This is the rule that makes the whole system demonstrable. Startup-time
enforcement is deliberate: discovering a missing fallback mid-demo, when the
quota runs out, is exactly the failure this design exists to prevent.

Fallbacks fire when:

1. The provider is unavailable
2. Rate-limited beyond backoff
3. Output is unparseable **twice**
4. `LLM_DETERMINISTIC_ONLY` is set (or no key is configured)

Every fallback writes an audit entry marked `source: deterministic`.

### 4. Prompt versioning

Prompts are **version-controlled files** under `services/prompts/`, each carrying
a version identifier that flows into provenance. Never inline a prompt string at
a call site.

Any metric can then be traced to the prompt that produced it — which matters
because a prompt edit silently changes results that were already reported.

## Provenance

Every reasoning result carries a structured provenance object, propagated into
the audit trail, API responses, and the dashboard.

| Field | Purpose |
| --- | --- |
| `source` | `model` or `deterministic`. **The most important field** — it says whether a judgment was *reasoned* or *ruled* |
| `provider` | Exactly what produced it |
| `model` | Exactly which model |
| `cache_hit` | Whether it came from cache rather than a live call |
| `prompt_version` | Which prompt produced it |
| `abstained` | Whether the task declined to answer and routed to human review |
| `latency_ms` | Null for deterministic results |
| `tokens` | Null for deterministic results |

> **Nothing model-derived may appear anywhere in the system without this object
> attached.** Not in an API response, not on the dashboard, not in a metric.

Schema details in the `audit-schema` skill. The two files must agree exactly.

### Reporting rule

**Metrics are reported per source.** Never blend model-derived and rule-derived
results into a single figure that implies more than it delivers.

"84% recovery rate" where half the decisions were static templates is not a
false number, but it is a misleading one. Report both, split.

## Per-task fallback behaviour

The differences here are deliberate: **degrade to something boring where boring
is safe, degrade to abstention where being wrong is expensive.**

| Task | Fallback |
| --- | --- |
| **Root-cause diagnosis** (Engine 1) | Classify from the decline-code distribution: concentrated on one corridor and dominated by timeout/gateway-class reasons → **systemic**; spread across many customers with fund/account-class reasons → **individual**; anything else → **insufficient evidence, escalate to human review**. Bias toward escalation under ambiguity |
| **Dunning drafting** (Engine 2) | Static per-failure-class templates with slot-filled variables. A plain, accurate message costs almost nothing in quality |
| **Promise extraction** (Engine 3) | **Abstain.** Mark for human review, set `abstained: true`. No regex, no keyword heuristic, no partial guess |

**Why Engine 3 abstains rather than guessing:** fabricating a promise-to-pay
would suppress a legitimate chase and corrupt the promise register. A wrong
promise is worse than no promise, so the fallback refuses instead of
approximating. An abstention is recorded as `action: escalate`,
`outcome: escalated`, with `provenance.abstained = true`.

Engines are built in later phases; each phase file specifies its own fallback.
Register it when the engine lands — never ship a reasoning task without one.

## Structured output

Ask for structured output and **validate it against a Pydantic v2 model** before
it touches anything else. Unparseable output is retried **once**; a second
failure takes the fallback.

Never let raw model text flow into a decision, a database column, or a customer
message unvalidated. The provider proposes; the schema and `policy_engine.py`
decide what is admissible.

## The boundary that matters most

`llm_agent.py` supplies **judgment**. `policy_engine.py` supplies **permission**.

A model response can never widen a bound. If the model recommends a fifth retry
where the budget is four, the answer is no — and the refusal is audited. The
provider being swapped from one vendor to another changes nothing about this;
it is the reason the system is defensible rather than merely automated.

## Checklist for any new reasoning task

1. Prompt lives in `services/prompts/`, with a version id.
2. Output has a Pydantic v2 schema.
3. A deterministic fallback is **registered** — startup fails without one.
4. The fallback's behaviour is deliberate: boring where safe, abstaining where
   being wrong is expensive.
5. Results carry provenance, always.
6. It works with `LLM_DETERMINISTIC_ONLY=true`, and you ran it that way.
