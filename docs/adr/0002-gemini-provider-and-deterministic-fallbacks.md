# ADR 0002 — Gemini behind a provider interface, with deterministic fallbacks

**Status:** Accepted
**Date:** 2026-09-03
**Supersedes:** the "Agent/reasoning layer" row of the original stack table in
`docs/phases/00-init.md`

## Context

The original stack specified Anthropic Claude via the Messages API as the runtime
reasoning layer. Two problems emerged:

1. **A hackathon demo cannot depend on a live provider.** A rate limit, an
   expired key, or a network failure mid-demo takes the whole run down. The
   submission's central claim is that recovery actions are *bounded and
   provable* — a batch that dies halfway proves nothing.
2. **Free-tier quotas are consumed by iteration.** Development re-runs and
   pre-demo seed runs burn the same daily quota the demo needs.

## Decision

### 1. Provider: Google Gemini

Runtime reasoning moves to **Google Gemini** via the `google-genai` SDK.

Claude Code remains the *development* tool. Only the **product's runtime
reasoning provider** changes. These are separate things and the docs keep them
separate — the build-workflow story is legitimately about Claude Code.

**Model id deviation.** The change request named
`gemini-3.1-flash-lite-preview`. That model was **shut down on 2026-05-25**
(confirmed against Google's model documentation), so calls to it fail outright.
The default is its GA successor **`gemini-3.1-flash-lite`**. Because the model is
resolved from `GEMINI_MODEL` and never hardcoded at a call site, reverting is a
one-line config change — but it should not be pointed back at a dead preview id.

### 2. The wrapper sits behind a thin interface

`services/llm_agent.py` (renamed from the planned `claude_agent.py`) exposes a
small provider interface with a **single** Gemini implementation. Engines call
the interface and never touch the SDK.

Deliberately minimal: **one interface, one implementation, no registry, no plugin
machinery.** The interface exists to make the provider swappable and mockable in
tests — this migration is itself the evidence that swappability was worth having.
It does not exist to support providers we do not have. Adding a second provider
later is cheaper than carrying discovery machinery now.

### 3. Every reasoning task registers a deterministic fallback

**Enforced at startup, not at runtime.** A task without a registered fallback
fails when the app boots.

This is the load-bearing decision. Startup enforcement is the point: discovering
a missing fallback mid-demo, at the moment the quota runs out, is precisely the
failure this design exists to prevent. Runtime discovery would make the guarantee
conditional on which code paths happened to execute.

Fallbacks fire when the provider is unavailable, rate-limited beyond backoff,
returns unparseable output twice, or when `LLM_DETERMINISTIC_ONLY` is set. A
missing API key is treated as deterministic mode, **not** an error — a fresh
clone with no credentials completes a full batch run.

Supporting mechanisms: response caching keyed on prompt + model + prompt version,
`429` backoff at 1s/2s/4s, and versioned prompt files under `services/prompts/`.

### 4. Fallback behaviour differs by task, on purpose

> Degrade to something boring where boring is safe. Degrade to abstention where
> being wrong is expensive.

| Task | Fallback | Why this one |
| --- | --- | --- |
| Root-cause diagnosis | Classify from the decline-code distribution; escalate under ambiguity | The distribution genuinely carries signal — concentration on one corridor with timeout/gateway reasons really is evidence of a systemic fault. Biasing toward escalation keeps the error safe |
| Dunning drafting | Static per-failure-class templates, slot-filled | A plain, accurate message costs almost nothing in quality. This is the "boring is safe" case |
| Promise extraction | **Abstain**, flag for human review, `abstained: true` | The only task where a wrong answer is worse than no answer. A fabricated promise-to-pay would suppress a legitimate chase and corrupt the promise register. No regex, no keyword heuristic, no partial guess |

A uniform fallback policy would have to pick one of these behaviours for all
three, and any single choice is wrong for at least one task.

### 5. Provenance on everything

Every reasoning result carries `source` (`model` | `deterministic`), `provider`,
`model`, `cache_hit`, `prompt_version`, `abstained`, `latency_ms`, `tokens`.
This replaces the earlier `decision_source` field — one field, not both.

**Metrics are reported per source.** A recovery rate blending model judgments and
static templates is not false, but as a single figure it implies more than it
delivers.

## Consequences

- The project runs end to end **with no API keys at all**. This is a design goal
  and a demo asset, not a degraded mode.
- Every metric is traceable to the prompt version and the source that produced
  it.
- `anthropic` is removed from `requirements.txt`; `google-genai==2.22.0` added.
- Config gains `GEMINI_API_KEY`, `GEMINI_MODEL`, `LLM_DETERMINISTIC_ONLY`,
  `LLM_CACHE_PATH`; `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are gone.
- Startup-time fallback enforcement means adding a reasoning task without a
  fallback is a boot failure. That is intended friction.
- The reasoning layer is now the one component with a mandatory offline path, so
  it is also the one that must be tested offline. Tests mock the interface and
  never make live calls.
