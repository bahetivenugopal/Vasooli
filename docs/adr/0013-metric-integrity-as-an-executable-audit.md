# ADR 0013 — Metric integrity is an executable audit, not a claim

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 7 (integration)

## Context

This project's central claim is not that it recovers 39% of the money it is
pointed at. It is that **the number can be checked** — every figure recomputed
from an append-only audit trail, every action citing a rule that exists, every
refusal on the record.

Until Phase 7 that claim was defended by convention. `batch_summary()`
recomputes from entries; `services/overview.py` recomputes from entries; the
dashboard computes nothing. All true, all asserted in docstrings, none of it a
property anything enforced. And `/audit-check` — twelve validations that are the
whole evidentiary spine — existed as a **markdown checklist a person walks**.

A hand-walked checklist has a specific failure mode: it cannot say which of the
twelve it skipped. It is fine once and unreliable every time after. Meanwhile
three separate money-accounting bugs had already shipped and been caught by
reading output — Engine 1 counting the same rupees three times (denominator
inflated 86%), Engine 3 booking at-risk and recovered on different entries
(per-source rate of 101.96%), Engine 3's payment-link calls stamped wall-clock
and landing months after the decisions that authorised them.

Three of the same class of bug, in three engines, each found by a human noticing
something looked wrong.

## Decision

**The twelve validations become code, the metric comparison becomes code, and
the consolidated report cannot be produced without both running.**

Three pieces:

1. **`services/audit_validation.py`** — the twelve `/audit-check` validations,
   executable. Three callers share them: the `scripts/audit_check.py` CLI, the
   integrity audit, and the test suite. One implementation means the command, the
   report and the tests cannot disagree about whether a run is clean.

2. **`services/integrity.py`** — recomputes every headline metric a *fourth* time
   from raw `audit_entries` rows, with arithmetic that shares **no code path**
   with `batch_summary()` or `OverviewService`, then compares. If it shared a
   path, the comparison would be a tautology. It also proves the two traceability
   properties no single number can show: no orphan actions, no untraced money.

3. **`scripts/unified_demo.py` exits non-zero when integrity fails.** A
   consolidated report that prints FAIL and exits 0 is a report CI waves through.

### The dashboard is not a fourth source

The comparison is against three things, not four: raw rows, `batch_summary()`,
and the overview response. There is no separate UI number to check, because the
dashboard renders the overview response and performs no arithmetic on a metric.
That was Phase 6's one structural rule, and it is what makes "audit-derived,
API-reported and UI-displayed agree" checkable server-side rather than needing a
browser harness.

### Every check is negatively tested

`test_metric_integrity.py` injects each defect the audit claims to catch and
asserts it is caught: a citation resolving to nothing, a deterministic entry
wearing a model name, an out-of-order timeline, a refilling budget, a charge
after a halt, a secret in a rationale, recovery with no denominator, an action
authorised only by a logging marker.

A validator with no negative tests might be returning `passed=True`
unconditionally, and the entire credibility argument rests on it.

## What building it found

Three defects, which is the argument for having built it:

1. **One column, two budgets.** `attempts_remaining` counts down the debit budget
   *and* the outreach budget on the same entity, because Phase 4's
   `outreach_entity()` presents the message count as the attempt count. A naive
   monotonicity check reported 20 "budgets refilling" that were nothing of the
   sort. The check now separates them by the marker each decision already
   carries; the underlying wrinkle is real and is documented, not hidden.

2. **A malformed dataset record raised a bare `JSONDecodeError`** three frames
   deep, naming neither the file nor the line. Now a `DatasetError` that names
   both — and fails rather than skipping the line, which is the same fail-closed
   direction the policy engine takes. A run that silently drops what it could not
   read reports a rate over a denominator nobody chose.

3. **A leg that died left its `BatchRun` stuck `running` forever**, invisible to
   the overview (which reads completed runs only) while still holding its batch
   id, so the next attempt failed with "that batch id already exists" rather than
   with the actual cause. Now marked `failed`, best-effort, without masking the
   original exception.

None of the three would have been found by reading the code.

## Consequences

**Good**

- "The numbers are consistent" is a test, not a docstring.
- `/audit-check` is now runnable by anyone, including in CI, and fails loudly.
- The report carries its own verdict, so a figure and its check travel together.
- The three money-accounting bug classes this project actually produced now each
  have a check that would have caught them.

**Costs**

- **The audit encodes assumptions that can go stale.**
  `RECOVERY_BEARING_ACTIONS` names the three actions that may carry recovered
  money today. A fourth engine booking money on a fourth action would be reported
  as untraced until someone updates the set. That is the fail-closed direction —
  a new engine gets a loud complaint rather than a silent pass — but it is a
  maintenance edge.
- **`budget_kind()` reads engine metadata.** It distinguishes the two budgets
  using `metadata.kind` versus `metadata.proposed_action`, which are conventions
  three engines happen to share rather than a schema. A test pins that every
  entry reporting a budget carries exactly one marker, so the convention breaks
  loudly. The cleaner fix is an action-kind dimension on the bounds themselves —
  a Phase 1 change nobody could have justified in Phase 1.
- **The unified run takes ~10 seconds instead of ~9.** The audit reads every
  entry several times. At 348 entries that is free; at 348,000 it would not be.

## Related

- ADR 0012 — the unified run this audit runs over
- `.claude/commands/audit-check.md` — the twelve validations, in prose
- `.claude/skills/audit-schema` — the schema the validations enforce
