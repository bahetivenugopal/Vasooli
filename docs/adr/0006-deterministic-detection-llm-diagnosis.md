# ADR 0006 — Detection is deterministic; diagnosis is model-driven

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 3 (Engine 1)

## Context

Engine 1 has to answer two different questions about a payment corridor:

1. **Did this corridor's success rate actually drop?**
2. **Why, and is it one corridor's problem or many customers' problems?**

They look like one question and are not. The first has a correct answer that a
statistician can check. The second is a judgment call under genuine ambiguity —
`INSUFFICIENT_FUNDS` from thirty customers in one afternoon and
`ISSUER_UNAVAILABLE` from thirty customers in one afternoon produce nearly
identical rows and demand opposite responses.

The obvious cheap design is to hand both to the model: give it the window, ask
"is anything wrong and what?". The obvious safe design is to hand both to rules:
threshold the success rate, look up the dominant decline class, done.

Both are wrong here, for opposite reasons.

## Decision

**Detection is deterministic and statistical. Diagnosis is model-driven. The
boundary sits exactly between "did it drop" and "why did it drop".**

Concretely:

- `detection.py` segments the stream by configured corridor levels, computes
  rolling success rates against each corridor's own prior baseline, applies a
  minimum-volume guard, and tests significance with an exact one-sided binomial
  tail. No model is involved and none can be. It produces a `Detection` with
  numbers and a p-value.
- `diagnosis.py` takes that detection, the decline distribution inside the
  window, and the success rates of comparable corridors over the same window,
  and asks the model for a hypothesis, a systemic-vs-individual determination,
  a recommended action from an enumerated set, and its stated reasoning.
- `policy_engine.py` decides what any of that permits. The model never sets a
  bound; see ADR 0003.

## Why the boundary is there and not somewhere else

**Detection must be reproducible, and a model is not.** Every number this
project reports carries a seed and a batch id. A detection layer that gave
slightly different answers on re-run would make the recovery rate, the precision
figure and the detection latency all unverifiable — and those are the figures
the submission stands on. The detector is a pure function of (records, config),
and a test asserts that running it twice produces identical detection ids.

**Detection must be explainable in a sentence.** "Nineteen attempts on HDFC's
UPI corridor succeeded 31.6% of the time against that corridor's own 97.3%
baseline over the preceding day; the probability of seeing that few successes by
chance is under 1 in 10,000" is an answer a judge can push on. "The model
thought it looked bad" is not.

**Detection is also where a model is worst.** Spotting a rate change in a small
sample is exactly the task where a language model's calibration is unreliable and
where a two-line statistical test is exactly right. Using a model here would be
using the expensive, non-deterministic tool for the part of the problem that has
a closed-form answer.

**Diagnosis is where rules are worst.** The systemic-vs-individual call depends
on the interaction of the decline mix, how concentrated the failures are across
customers, and whether comparable corridors are simultaneously fine. A rule table
can encode a first approximation of that — and it does, as the fallback — but it
cannot weigh evidence that points both ways, and it produces a confidently wrong
answer whenever the shape is unusual. That is the shape of problem a reasoning
layer is genuinely good at, and it is the only place in this engine where one is
used.

**The engine still works with the model switched off.** The deterministic
fallback classifies from the decline distribution alone and biases toward
escalation where the evidence is unclear. A full batch completes with no API key
at all, which is a requirement (`CLAUDE.md` non-negotiable #5) and not a
degraded mode.

## Consequences

**Good.**

- Detection metrics are meaningful, because the detector could not have seen the
  ground truth and cannot vary between runs.
- The two confidences stay separate and are never averaged: the detection's
  confidence is statistical (`1 − p`), the diagnosis's is the model's own. Mixing
  them would produce a number that means nothing.
- Model spend is bounded by the number of *detections*, not the number of
  payments — three live calls on a 589-attempt batch, and zero on a re-run
  because the response cache serves them.
- When the model is unavailable, the thing that degrades is the *explanation*,
  not the ability to notice a problem.

**Costs, accepted.**

- The detector cannot notice a degradation that is invisible in success rates —
  latency creep, partial settlement failures, a corridor that fails only for one
  amount band. Adding those means adding statistics, not asking the model.
- The threshold has to be chosen, and any choice is arguable. ADR 0007 makes
  that argument explicit rather than burying it.
- A false-positive detection reaches the diagnosis layer and can be diagnosed
  systemic, because the model is answering "why did this corridor degrade?" and
  is not asked to second-guess whether it did. In the seed-42 run this happens
  once: a netbanking route with three rail-side failures out of seven attempts,
  which the model reads as a route problem. The policy engine then refuses the
  reroute for an unrelated reason (there is no alternate netbanking route), which
  is luck rather than design. **A future phase should give the diagnosis layer an
  explicit way to say "this detection is not real"** — the enumerated
  determination set has room for it.

## Alternatives rejected

**Model-driven detection.** Cheaper to build, impossible to defend. Every
reported detection metric becomes a statement about one particular run.

**Rules-only diagnosis.** Would have shipped, and would have been the more
honest choice if the fallback were the only path — but it makes the central
claim of the pitch ("a real model reasoning call, never a hardcoded if/else")
false. The fallback exists precisely so that claim can be true *and* the batch
can always complete.

**One combined model call producing both.** Merges a reproducible number and a
judgment into one output where neither can be checked separately, and makes the
detection metric a property of the prompt.
