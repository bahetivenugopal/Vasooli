---
description: Run a synthetic batch end-to-end through an engine and print the recovery-rate summary
argument-hint: <engine: root_cause|mandate_recovery|receivables|all> [seed]
---

# Run a batch demo

Run a synthetic batch end to end through the chosen engine and print an honest
recovery-rate summary.

**Engine:** `$1` (use `all` to run all three)
**Seed:** `$2` — if omitted, use the project's default demo seed and say which
seed you used.

## Steps

1. **Generate the batch** with `data/generators/`, using the explicit seed.
   Record the `batch_id` and the seed. They travel with every number that follows.
2. **Run the engine(s)** over the batch. Every decision routes through
   `policy_engine.py` and writes an audit entry via `audit_trail.py`.
3. **Compute the summary from the audit trail**, not from a separate counter.
   The headline number and the audit trail must be the same source of truth — if
   they can disagree, the number is not evidence.
4. **Print the summary** in the shape below.
5. **Run `/audit-check` on the batch** and report its result alongside.

## Summary format

```
Batch:  <batch_id>          Seed: <seed>          Engine: <engine>
Entities processed:         <n>

RECOVERED
  Amount recovered:         ₹<x>  (<n> entities)
  Recovery rate:            <pct>%  (recovered / at-risk)
  Amount at risk (total):   ₹<y>

DECISIONS
  Retries scheduled:        <n>
  Retries succeeded:        <n>
  Routed to dunning:        <n>
  Escalated:                <n>

STOPPED — the compliance evidence
  Blocked (precondition):   <n>   ← by rule: <rule id> x<n>, ...
  Halted (terminal):        <n>   ← by rule: <rule id> x<n>, ...
  Budget exhausted:         <n>

CLASSIFICATION
  Soft / Hard / Ambiguous:  <n> / <n> / <n>
  Fail-safe (UNKNOWN→hard): <n>

PROVENANCE — reasoned vs ruled, never blended
  Reasoned (source=model): <n>   (cache hits <n>, live calls <n>)
    mean confidence:       <c>
    recovery rate:         <pct>%
  Ruled (deterministic):   <n>   (abstentions <n>)
    recovery rate:         <pct>%
  Provider / model:        <provider> / <model>
  Prompt versions:         <version> x<n>, ...
```

## Reporting rules — these matter more than the numbers

- **Report the honest number.** Never rerun with different seeds until one looks
  good. If the result is unimpressive, that is the result, and it is a finding
  about the engines worth acting on.
- **Always print the seed and `batch_id`.** A recovery number without them is
  unverifiable and therefore worthless to a judge.
- **Report per `provenance.source`, never blended.** A single recovery rate
  mixing model judgments and deterministic fallbacks implies more than it
  delivers. Print both, split, and say which provider and model produced the
  reasoned half. A run completed entirely on fallbacks is a valid run — label it
  as such rather than presenting it as reasoned.
- **Show the stops as prominently as the recoveries.** Blocked and halted
  outcomes are the proof that escalation is bounded. A run with zero blocks means
  either the batch has no edge cases or the gate is not firing — investigate,
  don't celebrate.
- **If the run errors, report the error.** Do not summarize a partial run as if
  it completed.
- Money as integer paise internally; format to ₹ only for display.
