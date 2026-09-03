# ADR 0005 — Retry outcomes come from a documented probability model, not from the data

**Status:** Accepted
**Date:** 2026-09-04

## Context

Vasooli's headline claim is a **recovery rate**: of the revenue that was at risk,
how much did the agent get back. That number is only meaningful if it is a
consequence of the agent's decisions. Two ways of producing it are not:

1. **Pre-baking the answer.** The generator decides, at generation time, that
   attempt #2 on this failed payment succeeds. The engine then "recovers" it by
   retrying, and the recovery rate measures nothing but how generous the
   generator was. Worse, the temptation to nudge that generosity is invisible in
   the output — the data looks identical either way.
2. **Letting the simulator invent failures.** If `razorpay_client` in simulated
   mode failed calls on its own, the recovery rate would be measured against a
   distribution nobody wrote down. ADR 0004 already closes this: a simulated call
   fails **only** when the request asks it to.

But something has to decide whether a retry works. A hackathon build cannot
observe real retries, and pretending otherwise would be worse than any modelling
assumption.

The question a panelist will ask is exactly: *"how do you know the retry would
have worked?"* The answer has to be a straight one.

## Decision

**Generators produce the world; engines produce the outcome.**

- No dataset in `data/samples/` contains a retry outcome. There is no
  `retry_succeeded` field, and a test asserts there never will be.
- Retry success is modelled explicitly in
  `data/generators/configs/retry_success_model.json` and sampled at **engine run
  time**, seeded from the batch seed, through
  `data.generators.retry_model.RetrySuccessModel`.
- The model is a function of three things, each with a stated rationale in
  `data/DATA_CARD.md`: the **decline code** (does the underlying cause clear on
  its own?), the **spacing** since the previous attempt (a retry six minutes
  later hits the same empty balance), and the **attempt number** (repeated
  failure is evidence the cause is not transient).
- Hard, terminal-mandate and unknown classes are **hard zero**, at every spacing
  and every attempt number. That is the taxonomy's central claim, and the model
  must not soften it: if retrying a `CARD_EXPIRED` could succeed in our
  simulation, the soft/hard distinction would stop being load-bearing.
- Every sampled outcome carries the probability it was drawn against, so an audit
  entry can record not just what happened but what the odds were.

## Consequences

- The recovery rate becomes an honest consequence of the engine's decisions.
  Retrying a hard decline earns nothing, retrying too soon earns less, and
  spacing retries correctly earns more — which is precisely the judgment the
  policy layer exists to make.
- **The numbers depend on a stated assumption**, and that is a real limitation
  rather than a hidden one. Anyone can open the model file, disagree with 0.42
  for `INSUFFICIENT_FUNDS`, change it, re-run the batch, and see the recovery
  rate move. That is a stronger position than an unfalsifiable figure.
- The model's values are grounded in the *direction* of published dunning
  practice — soft declines recover meaningfully on a spaced retry, hard declines
  do not recover at all — but the specific probabilities are **ours**, chosen to
  be conservative. They are not measured from production traffic, and the data
  card says so in as many words.
- Changing the model changes reported numbers, so the file is version-controlled,
  carries a `model_version`, and is embedded in the batch manifest of any run
  that used it. A figure and the assumptions behind it travel together.
- Two conservative choices are worth naming because they cost us on paper:
  probability **decays** with attempt number, and it is capped at 0.95. Both push
  the reported recovery rate down, not up.

## Alternatives considered

- **Pre-baked outcomes in the dataset.** Simpler to implement and impossible to
  defend. Rejected on the reasoning above.
- **A fixed success probability for every retry.** Honest, but it erases the
  soft/hard distinction, which is the single most important idea in the project.
- **No simulation at all — report attempts, not recoveries.** Defensible, and
  genuinely tempting. Rejected because "we retried 41 payments" tells a judge
  nothing about whether the agent's *choices* were good, and the whole point of
  the policy layer is that the choices are what differ.
