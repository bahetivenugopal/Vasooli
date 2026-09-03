# ADR 0003 — The policy engine has final authority over LLM recommendations

**Status:** Accepted
**Date:** 2026-09-03
**Relates to:** ADR 0002 (Gemini provider and deterministic fallbacks)

## Context

Vasooli's central claim is that every recovery action is **bounded, explainable
and provably compliant** — not "an agent that tries stuff". Both halves of the
system can produce something that looks like a decision:

- `llm_agent.py` diagnoses a root cause, classifies an ambiguous decline, drafts
  a message, extracts a promise-to-pay.
- `policy_engine.py` evaluates attempt caps, cooldowns, hard stops, quiet hours,
  amount thresholds and escalation triggers.

If those two are peers, the claim is false. A model that can talk its way past a
retry cap is a model that sets the retry cap, and every bound in the system
becomes a suggestion. This is not a hypothetical: the natural implementation —
ask the model what to do, then check whether it seems reasonable — inverts the
authority without anyone deciding to.

The specific failure this ADR exists to prevent: the model recommends a fifth
retry where the budget is four, and the recommendation is honoured because it
came with a confident rationale.

## Decision

**The model supplies judgment. The policy engine supplies permission.** A model
response can never widen a bound.

This is enforced structurally, not by convention:

1. **The envelope is computed before the recommendation is read.**
   `PolicyEngine.authorize()` calls `evaluate()` first and only then looks at
   the `LLMRecommendation`.
2. **A denial is returned as a denial.** When `evaluate()` refuses, `authorize()`
   returns that refusal with the recommendation attached as
   `overridden_recommendation`. It never rewrites `allowed`.
3. **`authorize()` cannot construct an allowed decision.** `_permit()` is
   module-private and reachable only from `evaluate()`. There is no code path in
   which a recommendation produces a permission that the rules did not already
   grant.
4. **`PolicyDecision` is frozen.** Nothing can flip `allowed` after the fact.
5. **A recommendation may only narrow.** It can choose a different action from
   `permitted_actions` (which always includes escalating and halting — doing
   *less* is never forbidden), and it can propose a *later* retry time. An
   earlier one is discarded in favour of the policy floor.

Every refusal is written to the audit trail citing the rule that refused it and
recording what the model wanted. A blocked attempt is not an absence of activity;
it is the evidence that the gate is real.

`test_llm_recommendation_cannot_override_a_policy_denial` is the single most
important test in the repository, and a parametrised companion asserts the same
property across every denial shape and every possible recommended action.

## Consequences

- The reasoning layer can be swapped, degraded to a deterministic fallback, or
  removed entirely, and the system's bounds do not change. That is why the
  project can complete a full batch run with no API key at all.
- Prompts do not need to be defensive about bounds. A prompt that mentions a
  retry cap is documentation for the model, not enforcement — enforcement is
  somewhere the model cannot reach.
- Every engine gets this property for free by calling `authorize()` rather than
  acting on a reasoning result. An engine that reads `llm_agent` output and acts
  on it directly is a bug, and the audit trail makes it visible: the entry would
  cite no policy rule.
- Adding a rule means adding it to `RULE_REGISTRY` with a description and a
  citation. `_deny()` and `_permit()` both refuse an unregistered rule id, so a
  decision cannot cite authority it does not have.
- The cost is one extra call at each decision site (`evaluate` then `authorize`)
  and a slightly more verbose engine. That is a small price for a safety
  property that is checkable by reading one function.
