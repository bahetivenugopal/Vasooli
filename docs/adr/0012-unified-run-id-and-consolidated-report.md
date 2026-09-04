# ADR 0012 — One unified run id over three per-engine batch ids

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 7 (integration)

## Context

Phase 7 asks for a single command that runs all three engines and produces one
consolidated report, with

> **one `batch_id` spanning all three engines, so the entire run is auditable as
> a single unit.**

That requirement collides with a decision made in Phase 1 and load-bearing ever
since. `BatchRun.batch_id` is a primary key, one row per engine run, and
`AuditTrail.start_batch()` refuses point-blank to reuse an id:

> *reusing a batch id would merge two runs into one set of metrics*

That refusal is not incidental. Reproducibility in this project rests on every
reported number travelling with the batch id and seed that produced it, and a
batch id that names three different runs cannot do that job.

So three options were on the table:

1. **Literally one `batch_id` for all three engines.** Either drop the primary
   key constraint, or write one `BatchRun` row for the whole unified run.
2. **A parent run id, with per-engine batch ids derived from it.**
3. **Leave it as three unrelated runs** and have the report take three ids.

## Decision

**Option 2.** A unified run has a **run id**; the three per-engine batch ids are
derived from it deterministically, and each engine's `BatchRun.notes` records the
`unified_run_id` so the relationship reads in both directions.

```
unified-s42-c0b13b0b   ->  unified-s42-c0b13b0b-rc
                           unified-s42-c0b13b0b-mr
                           unified-s42-c0b13b0b-rcv
```

The run id is a hash of the seed, so the same command produces the same id on any
machine — reproducibility is worth much less if two people comparing "seed 42"
are comparing runs they cannot name identically. A `salt` parameter exists for
running the same seed twice against one database, rather than inventing a second
id format when that need arrives.

One id still names the whole run. `batch_ids_for(run_id)` turns it back into the
three, `python scripts/audit_check.py --unified <run_id>` validates all three
together, and every read path in `services/unified_run.py` takes the three as one
list. The unit of auditability the phase file asked for exists; it is addressed
by one id and stored under three.

## Why not option 1

Collapsing three engine runs into one `BatchRun` row would have cost the
**per-engine contribution split**, which the entire reporting model rests on:

- The three engines measure exposure differently. A blended rate is only
  defensible *because* each contribution row ships its own recovery definition
  beside its own number. One row has one seed, one dataset, one clock, and no
  place to put three of each.
- `EngineContribution` carries a `dataset_batch_id`, a `seed`, a `status` and a
  run clock **per engine**. All three genuinely differ — the clocks alone span
  more than a day, and each is right for its own data.
- Every engine route (`/api/v1/{engine}/runs/{id}`) is keyed by that engine's own
  batch id. Merging would have meant rewriting three engines' API surfaces at the
  exact moment the phase file says not to refactor.

Dropping the primary-key constraint instead would have removed the guard that
stops two runs merging into one set of metrics — which is the specific accident
the constraint exists to prevent, in the phase whose entire purpose is proving
the numbers are trustworthy.

## Why not option 3

Because "auditable as a single unit" is a real requirement and three unrelated
ids do not satisfy it. A judge asking *"show me that run"* should be able to give
one id. With three unrelated ids, the answer depends on a note somebody kept.

## Consequences

**Good**

- One id addresses the run; three ids store it. Both properties hold.
- A unified run's legs are `BatchRun` rows like any other, so the dashboard, the
  engine routes and `/audit-check` needed no changes to read them.
- `notes["unified_run_id"]` makes the relationship queryable from either end, and
  a leg that dies is marked `failed` carrying it.
- The overview's default view — latest completed run per engine — lands on the
  unified run's three legs with no special-casing, so the console report and the
  dashboard show the same run without anyone selecting it.

**Costs, stated plainly**

- **The parent has no table.** A unified run exists as an id convention plus a
  key in three `notes` blobs. There is no `unified_runs` row to query, no foreign
  key, and nothing stops someone hand-writing a batch id that looks like a leg.
  `report_for()` rebuilds a report from the three legs, which is enough for this
  build and is not a schema.
- **`BatchRun.notes` grows again.** Phase 6 already flagged it as a grab-bag —
  dataset id, dataset path, run clock, config version, Razorpay mode, provider
  flag, the extraction score, the whole `RunSummary`. This adds an eighth key to
  an untyped column. Typing it is the right fix and was not this phase's scope.
- **The id format is a convention, not a constraint.** `-rc` / `-mr` / `-rcv` are
  string suffixes. A batch id containing a suffix by coincidence would confuse
  `batch_ids_for()`. In practice engine batch ids are chosen by the caller and
  this has not bitten; a stricter scheme would need the table above.

## Related

- ADR 0003 — the policy engine has final authority (why per-engine trails matter)
- Phase 1 log — `start_batch()` refusing to reuse an id, and why
- Phase 6 log — `BatchRun.notes` as a grab-bag, flagged then and worse now
