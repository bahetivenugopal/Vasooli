# Phase implementation log

Running notes from actually building each phase — written **after** the phase
lands, not before. One section per phase, appended in order, never rewritten.

**What belongs here:** what shipped and what the next phase can rely on, the
deviations from the phase file that only became visible while building, and the
traps a later phase would otherwise walk into.

**What does not:** a file-by-file changelog, anything the code or the ADRs
already say, or a restatement of the phase spec. If it is discoverable by
reading the repo, leave it out — this file is for the things that are not.

> Phase files themselves live alongside this one and are the *plan*. This file is
> the *record*. Where the two disagree, this file says why.

---

## Phase 1 — The Shared Core

**Status:** complete · **Date:** 2026-09-03
**Verified by:** 125 pytest passing, `ruff check` clean, `/docs` renders, core-loop
script runs, broken-key fallback confirmed against the real SDK.

### What now exists

Four services under `apps/api/app/services/`, plus the two supporting modules
that turned out to be genuinely shared rather than engine-specific:

- `policy_engine.py` — 28 registered rules, all six categories
- `audit_trail.py` — append-only writer/reader, batch summary
- `llm_agent.py` — Gemini behind a provider Protocol, cache, backoff, fallbacks
- `razorpay_client.py` — test-mode only, plus deterministic simulated mode
- `decline_taxonomy.py` — the taxonomy as a lookup table
- `reasoning_tasks.py` — the one shared reasoning task and its fallback

Also: `models/` (enums, provenance, audit, batch, recoverable-entity mixin),
`db/types.py`, six read-only audit routes, and `scripts/core_loop_demo.py`.

Engine folders are still empty, as the phase required.

### The interfaces every engine will consume

```python
decision = PolicyEngine().evaluate(PolicyRequest(
    engine, entity, proposed_action, now,
    decline_code=None, is_outreach=False, messages_sent_in_window=0,
    max_attempts_override=None, abstained=False, unevaluable_precondition=None,
))
decision = PolicyEngine().authorize(request, llm_recommendation)  # can only narrow

result = LLMAgent().run("task_name", context)   # never raises for provider problems
trail.record(..., provenance=result.provenance, model_confidence=result.confidence)
trail.resolve_outcome(entry_id, outcome=..., amount_recovered_paise=...)
trail.batch_summary(batch_id)                   # per-source, computed from the log
```

`PolicyDecision.audit_fields()` maps straight onto `audit_trail.record()`. Use it
rather than re-deriving the fields by hand.

### Deviations from the phase file, and why

| # | Deviation | Why it happened |
| --- | --- | --- |
| 1 | **Added a new skill, `policy-bounds`** | Quiet hours, escalating backoff, the hard attempt ceiling and the escalation triggers had no home in `decline-taxonomy` or `rbi-mandate-rules`. Writing them inline would have broken non-negotiable #1 (no invented thresholds). Each bound is now named (`AC1`, `CD2`, `QH1`…), valued, and argued in the skill, and cited by rule id from the engine. **Later phases must cite this skill for any bound the other two do not cover.** |
| 2 | **`amount_paise` split into `amount_at_risk_paise` + `amount_recovered_paise`** | The phase file requires both for headline metrics; the `audit-schema` skill had one field. The skill was updated to match what shipped, so the two do not diverge. |
| 3 | **Append-only got one documented exception** | §5.1 permits resolving a pending outcome; the `audit-schema` skill said entries are *never* updated. Resolved in favour of the phase file, and the skill now documents `resolve_outcome()` as the single guarded transition, with the reasoning (entries are written *before* side effects, so the outcome is genuinely unknown at write time). It refuses an already-resolved entry. |
| 4 | **No `decision_source` field** | The skill is explicit that `provenance` replaces it — "one field, not both". `provenance.source` carries it. The phase file's §5.1 table lists both; the skill won, as §5.1 itself instructs. |
| 5 | **Added `RAZORPAY_MODE` to config and `.env.example`** | §5.4 requires a simulated mode but the env template had no switch for it. Default is `simulated`. |
| 6 | **Fixed IST offset instead of `ZoneInfo("Asia/Kolkata")`** | `tzdata` is absent from a bare Windows Python and the import blew up. IST has observed no DST since 1945, so a fixed `+05:30` is exact and drops a dependency. |
| 7 | **Added `Engine.CORE` and `Action.API_CALL`** | The shared core acts before any engine exists — batch bookkeeping, a logged Razorpay call. Kept distinct so per-engine recovery metrics stay clean. Added to the `audit-schema` skill too. |
| 8 | **Model id stayed `gemini-3.1-flash-lite` (GA), not the `-preview` id the phase named** | Already settled in ADR 0002 before this phase; resolved from `GEMINI_MODEL`, so switching is a one-line config change. Noted here only so nobody "fixes" it later. |

### Things a later phase will otherwise get wrong

**The `_permit()` privacy is the safety property.** `authorize()` cannot
construct an allowed decision — only `evaluate()` can, through a module-private
constructor. An engine that reads `llm_agent` output and acts on it *without*
going through `authorize()` bypasses ADR 0003 silently. The tell in the audit
trail is an entry citing no policy rule.

**Reasoning tasks must be registered from the app lifespan**, following the
`register_core_tasks()` pattern in `reasoning_tasks.py`. Startup enforcement only
sees tasks that have actually been registered — a task registered lazily at first
call defeats the whole point of checking at boot.

**Registration loads the prompt file eagerly.** A task whose prompt file is
missing or unversioned fails at boot, not mid-batch. Write the prompt file first.

**Simulated Razorpay failures never happen on their own.** A failure fires only
when the request carries `notes.simulate_failure = "<razorpay reason>"`. Phase 2's
generator must set it from the seed — otherwise every simulated call succeeds and
the recovery rate means nothing. Valid scenario keys are the top-level keys of
`app/services/fixtures/razorpay/error_scenarios.json`; an unknown one produces a
failure through the `UNKNOWN` path rather than a silent success.

**Every Razorpay call needs `notes.batch_id`.** It is the only thing tying a
Razorpay-side object back to a reproducible batch.

**Naive datetimes are rejected at the database column**, not coerced. Use
`app.db.types.utcnow()`. A naive value raises on write.

**Money is integer paise in every signature.** `_rupees()` in the policy engine
is display-only and lives there for rationale strings.

### Bounds now fixed in code (change only via the skill + an ADR)

- Hard attempt ceiling **4** (`policy-bounds:AC1`) — nothing raises it; a
  configured override above it is clamped, not honoured
- Mandate cap **3** / spacing **24h** (`rbi-mandate-rules:PartB`)
- Receivables ladder **3 rungs**, aligned with the QH2 contact cap so the two
  bounds cannot disagree about how often a customer hears from us
- Generic cooldown **6h**, doubling, capped at **72h** (`CD1`/`CD2`)
- Outreach window **09:00–21:00 IST**, contact cap **3 per rolling 7 days**
- AFA threshold **₹15,000** (regulatory, `A3`); high-value human review
  **₹50,000** (ours, `HE3`)
- Unknown-decline classification confidence floor **0.6**

Engine 2's phase should confirm the mandate numbers still read correctly against
`rbi-mandate-rules` Part B before building on them.

### Observations worth carrying forward

- **The live model returned `confidence: 1.0`** on a clear-cut classification.
  If that overconfidence pattern holds, the 0.6 floor will rarely fire. Matters
  for Engine 1, whose fallback depends on ambiguity being *reported* honestly —
  worth checking the actual confidence distribution before trusting it as a
  routing signal.
- **The cache works and is worth relying on.** A repeated identical call was
  served from disk with `cache_hit: true` and no live call. Seeded batch re-runs
  should be nearly free; the pre-demo run does not need to re-spend quota.
- **`batch_summary()` is recomputed from entries every time**, including by the
  API route. The snapshot stored on `BatchRun.summary` is a dashboard
  convenience only — never quote it as a figure.
- **`policy_violations` should always be 0.** A non-zero count means a row
  reached `audit_entries` without going through the writer. Treat it as a bug
  hunt, not a metric.
- A batch with **zero `blocked`/`halted` entries** is suspicious rather than
  clean — it means the gate never fired.

### Not done in this phase (deliberately)

No engines, no synthetic data, no frontend, no engine-specific routes, no
deployment. Commits were left to the user.

---

## Phase 2 — Synthetic Data Foundry

**Status:** complete · **Date:** 2026-09-04
**Verified by:** 171 pytest passing (46 new), `ruff check` clean from the repo
root, all three committed samples regenerating byte-identically from their
manifest's seed.

### What now exists

`data/generators/` — three seeded generators plus the machinery they share:

- `common.py` — labelled RNG streams, IST/UTC time, exact allocation, hashing,
  manifests, `GeneratedBatch`
- `payments.py` · `mandates.py` · `invoices.py` — the three datasets
- `replies.py` — 20 customer-reply templates with per-template ground truth
- `retry_model.py` — the retry-success probability model **the engines sample**
- `vocabulary.py` — the decline vocabulary, imported from the shared core
- `configs/*.json` — the worlds being simulated, in version control
- `cli.py` — `python -m data.generators.cli all --seed 42 --out data/samples`

Committed samples in `data/samples/`: payments 589 rows (`pay-s42-d5db86f7`),
mandates 64 (`mnd-s42-dabc793d`), invoices 72 (`inv-s42-680b8a62`), each with its
manifest. Plus `data/DATA_CARD.md`, `data/generators/README.md`, and ADR 0005.

### What the engines consume

```python
from data.generators.common import load_config          # configs, resolved
from data.generators.retry_model import RetrySuccessModel

model = RetrySuccessModel.load()
outcome = model.sample(seeded_rng, "INSUFFICIENT_FUNDS",
                       hours_since_previous=26.0, attempt_number=2)
# -> .succeeded, .probability, .failure_code, .simulate_failure_key
```

Datasets are read as JSONL. **Engine code must never open a manifest** — that is
where ground truth lives. Scoring and evaluation code reads manifests; engines do
not.

### Deviations from the phase file, and why

| # | Deviation | Why it happened |
| --- | --- | --- |
| 1 | **Configs are JSON, not YAML** | §5.1 allows either. PyYAML is not in `requirements.txt`, and adding a parser dependency so a config can have fewer braces is a bad trade. |
| 2 | **Manifests carry a `nondeterministic` block** | §5.1 wants a timestamp in the manifest *and* byte-identical reproduction. Both hold only if the clock is quarantined. Everything outside that key is a pure function of (seed, config, generator version), and a test asserts exactly that. |
| 3 | **`razorpay_reason` is `null` for most failure codes** | Only eight upstream reasons are doc-verified (`REASON_MAP`). Inventing strings for `ISSUER_UNAVAILABLE`, `CARD_EXPIRED`, `MANDATE_REVOKED` and the rest would make a replayed failure normalize to `UNKNOWN` and mislabel itself. See the trap below — it changes what phase 1's "set `notes.simulate_failure`" note can mean. |
| 4 | **Deterministic coverage repair passes** | Several acceptance criteria are "this case is present": every reply category, Hinglish, a broken promise, an unmappable decline. Sampling makes those hold on lucky seeds only. Records placed this way are flagged `coverage_forced` in the manifest, and the passes are documented in the data card §5. |
| 5 | **Ground truth is descriptive, never a verdict** | The manifest records *how a record was built* — cohort, notice profile, AFA side — and not what the policy engine should decide about it. §5 forbids generators judging data, and an "expected gate" field would have been exactly that. |
| 6 | **Added a repo-root `ruff.toml`** | `apps/api/pyproject.toml` cannot reach `data/` or `scripts/`, so those paths were linted with Ruff's much weaker defaults: `ruff check` at the root passed on code that `ruff check` inside `apps/api` would reject. Equalising them surfaced one genuinely unused import and eight stale `noqa` directives in `scripts/core_loop_demo.py`, now fixed. **Keep the two configs in sync.** |
| 7 | **`data/__init__.py` plus a `sys.path` bootstrap** | Generators import the taxonomy from `apps/api` rather than keeping a second copy of the vocabulary. Same bootstrap `scripts/core_loop_demo.py` already uses. |
| 8 | **A second, deliberately harder degradation** | §5.2 asks for "one or more". The route-level event has 10 in-window attempts at 0.60 against a 0.85 baseline — Engine 1 may well miss it. That is the point: recall below 1.0, reported honestly, beats a dataset where nothing can be missed. |
| 9 | **Invoice status vocabulary excludes `disputed`** | A dispute exists only in the reply text. A ledger flag for it would hand Engine 3 the answer it is meant to extract. A test enforces the vocabulary. |
| 10 | **`days_overdue` on a paid invoice means "days late when it landed"** | Zeroing it would contradict `due_at`/`paid_at`; leaving it as days-since-due would claim a settled invoice is still ageing. Derivable either way, so an engine recomputing it agrees with the ledger. |

### Things a later phase will otherwise get wrong

**The datasets are anchored to a fixed `as_of`, and it is not today.** Payments
covers 29–31 Aug 2026 IST; mandates and invoices are built around
`2026-09-01T09:00+05:30`. An engine run against `datetime.now()` sees every
pre-debit notice as stale and every next debit as long overdue, so the compliance
gates fire for the wrong reason. **Engine runs must take `now` as an input,
defaulting to the dataset's `as_of`** — it is in `manifest.config.resolved.as_of`
(or `window_start` for payments). This is the single most likely way to waste an
afternoon in Phases 3–5.

**A failure with `razorpay_reason: null` cannot be replayed through
`razorpay_client`.** Phase 1's note — "the generator must set
`notes.simulate_failure` or every simulated call succeeds" — holds only for the
eight verified reasons. For everything else the engine must record the failure
from the record's `failure_reason_code` directly rather than round-tripping it,
or the decline silently becomes `UNKNOWN`. `RetryOutcome.simulate_failure_key` is
`None` in exactly those cases; treat `None` as "do not call the client expecting
a failure", never as "call it and hope".

**Two different things are called a batch id.** `pay-s42-d5db86f7` identifies a
*dataset*; `BatchRun.batch_id` identifies an *engine run*. One dataset can feed
many runs, so they must not be conflated. Put the dataset batch id in
`BatchRun.notes` so a reported number can be traced back to the rows it came from.

**`retry_model.py` lives in `data/generators/`, so engines will import the data
package.** Deliberate — the model is a documented data assumption, not engine
logic — but it means `apps/api` code depends on a repo-root package. If that
becomes awkward, move the module into `app/services/` and leave the JSON where it
is. Do not fork the values.

**Degradation windows are absolute timestamps.** Change `window_start` or `days`
in the payments config without moving them and the batch quietly contains nothing
to detect. `ground_truth.degradations[].observed_in_window.attempts` is where you
would notice.

**Mandate `attempts_in_current_cycle` counts failures in the latest cycle only**,
and a test keeps it consistent with `debit_history`. Healthy mandates have 0.

**Revoked and paused mandates still carry a scheduled `next_debit_at`.** That is
the `policy-bounds:HS1` trap, on purpose. An engine that filters on
`status == "active"` before checking hard stops will look correct and prove
nothing.

### Measured distributions in the committed samples (seed 42)

Not the intended figures — the measured ones, read from the manifests:

- **Payments:** 589 attempts, 88.8% success (band 85–92%); per-method 86–93%.
  66 failures: SOFT 81.8%, AMBIGUOUS 12.1%, HARD 4.6%, UNKNOWN 1.5%. Diurnal
  peak/trough **48×**. The HDFC×UPI degradation shows 21 in-window attempts at
  33.3% against 97.0% for the same corridor outside the window. The decoy: 9
  attempts, 55.6%, nothing injected. 34 of 66 failures are replayable through a
  verified fixture.
- **Mandates:** 64 mandates (52 active, 7 revoked, 5 paused), 194 debit attempts,
  74 failures. 38 at or below and 26 above ₹15,000. 7 sitting at the Part B
  attempt cap, 3 with an AFA registration gap. All four notice profiles present
  (on_time / late / stale / missing). Issuer-decline mix with the placed
  lifecycle outcomes excluded: SOFT 78.7%, HARD 11.5%, AMBIGUOUS 9.8%.
- **Invoices:** 72 invoices (46 overdue, 12 paid, 14 open), 43 replies across all
  five categories, 26 English / 17 Hinglish, 8 disputes. 30 promises: **12 kept,
  8 broken, 10 still pending**; 14 with an explicit date, 6 inferable, 10
  undateable. The ladder never exceeds 3 rungs.

The soft share lands at 81.8% rather than the configured 80% because the injected
degradations force a specific code inside their windows, which moves the mix
visibly at 66 failures. Both the configured and the measured numbers are in the
manifest, and the distribution test asserts the claim the project actually makes
out loud — soft is the clear majority — rather than the config value.

### Assumptions worth repeating

All are in `data/DATA_CARD.md` §9, but three bound what Phases 3–5 can honestly
claim:

1. **The retry-success probabilities are ours**, not measured from production
   traffic. They are the biggest single lever on the headline recovery number.
2. **One reply per invoice.** Promise-then-renege-then-promise-again does not
   exist in this corpus, so Engine 3 cannot be scored on it.
3. **Reply text is templated** (20 templates). It covers the phrasings that
   matter, but an extractor could in principle overfit to it.

### Not done in this phase (deliberately)

No detection logic, no recovery logic, no scoring — generators produce data and
never judge it. No engine code, no frontend, no deployment. Commits were left to
the user.

---

## Phase 3 — Engine 1: Root-Cause Recovery

**Status:** complete · **Date:** 2026-09-04
**Verified by:** 242 pytest passing (71 new), `ruff check` clean from the repo
root *and* from `apps/api/`, a full live-provider batch run, a full no-provider
run, and `/audit-check` passing with 0 violations over 110 entries.

### What now exists

`apps/api/app/engines/root_cause/` — the first engine, in seven modules that map
one-to-one onto the loop:

- `config.py` + `config.json` — corridor levels and thresholds, each citing a rule
- `dataset.py` — reads the payments `.jsonl`, **refuses a manifest path**
- `detection.py` — segmentation and statistics. No model, ever
- `diagnosis.py` — the `diagnose_corridor` task, prompt, and fallback
- `recovery.py` — the four bounded actions, each authorised first
- `scoring.py` — precision/recall/latency vs ground truth. The **only** module
  that opens a manifest
- `runner.py` — the loop, and a summary recomputed from the audit trail

Plus: a new `corridor-detection` skill, four new policy rules, two new tables
(`corridor_detections`, `corridor_reroutes`), eight API routes under
`/api/v1/root-cause/`, `scripts/root_cause_demo.py`, ADRs 0006 and 0007, and
`docs/metrics/engine-1-root-cause.md`.

### The measured result, in one line

Recall 100% / precision 66.7% on seed 42 (2 of 2 ground-truth events found, one
honest false positive), 64% / 70% across five seeds, decoy never fires. Recovery
rate 34.75% of Rs 5,25,206 at risk. 11 policy denials, 2 of them overruling the
model. Full numbers, including what underperformed, are in
`docs/metrics/engine-1-root-cause.md` — **quote that file, not this line.**

### Deviations from the phase file, and why

| # | Deviation | Why it happened |
| --- | --- | --- |
| 1 | **Added a new skill, `corridor-detection`** | Section 5.1 requires a named minimum-volume threshold, a corridor definition, and a reroute expiry. None of `decline-taxonomy`, `rbi-mandate-rules` or `policy-bounds` covers any of them, and writing them inline would have broken non-negotiable #1. Same precedent as Phase 1's `policy-bounds`. Rule ids: `CS1`, `BW1/2`, `MV1/2`, `ST1/2`, `DX1/2`, `RR1/2/3`, `ES1`. |
| 2 | **`authorize()` no longer overwrites `rule_id` with `policy_engine:llm_authority.denied`** | Found while reading the first real batch: *every* overruled denial cited that same generic string, so the trail could not answer "which rule stopped this?". The citation now stays specific (`policy-bounds:HE1`, `corridor-detection:RR3`, an attempt cap) and the authority rule moves to a new `PolicyDecision.authority_rule_id`, surfaced through `override_metadata()` into audit metadata. **This changed a Phase 1 test**, deliberately. |
| 3 | **Corridor and `api_call` entries carry `amount_at_risk_paise = 0`** | The first run reported Rs 9,74,580 at risk against a true Rs 5,25,206 — the same money counted on the payment entry, again on the corridor covering it, and a third time on the Razorpay call. The money lives on the payment entry only; a corridor's value at risk goes in metadata. **Engines 2 and 3 must follow this or the batch denominator inflates the same way.** |
| 4 | **"Suppressed" and "deferred" are separate counts** | Section 5.4 asks for suppressions as a headline number. A cooldown block is not a suppression — the revenue is still recoverable, only the timing was wrong. Conflating them would have reported 9 "wasted attempts avoided" where the honest figure is 4. |
| 5 | **`issuer_method_route` segmentation is in the config but disabled** | Section 5.1 wants granularity discussable rather than assumed. At 589 attempts it splits the stream into 37 buckets that never clear the baseline guard, so it detects nothing and adds 37 more chances at a false positive. Left visible and off, with the reasoning in ADR 0007. |
| 6 | **A reroute does not change any retry's success probability** | Section 5.3 wants rerouting as a real action, and it is — authorised, bounded, expiring, audited. But `retry_model.py` has no route dimension, so an uplift for "we rerouted" would be inventing the headline number. The reroute is recorded on affected retries as `rerouted_to` and contributes **zero** to the recovery figure. Stated in the metrics doc rather than buried. |
| 7 | **The unknown-decline reasoning task is not called by this engine** | The phase file does not ask for it, and the only honest wiring (letting a classification change the retry budget) would have needed an invented number for the post-classification cap. `UNKNOWN` fails closed to a budget of 1 through the taxonomy, which is the documented behaviour. Engine 2 should revisit — it has more unknowns to classify. |
| 8 | **`PolicyRequest` gained two Engine-1-specific fields** | `corridor_determination` and `alternate_route_available`. Enforcing RR2/RR3 inside the engine instead would have made them intentions rather than gates — an engine can always forget to check. Same shape as the existing `is_outreach` / `abstained` fields. |

### Things a later phase will otherwise get wrong

**`_permit()` is still the only constructor of an allowed decision, and
`authorize()` still cannot reach it.** Deviation #2 changed which rule a denial
cites, not who can permit. Do not "simplify" `authority_rule_id` back into
`rule_id`.

**The run's `now` comes from the data, not the clock.** `RootCauseRunner.run()`
defaults `now` to the last attempt's timestamp. Phase 2's warning about the fixed
`as_of` is real, and this is how Engine 1 answered it — engines may not read the
manifest, so the anchor has to be derived from the records. Engines 2 and 3 have
a harder version of the same problem: their datasets are built around
`2026-09-01T09:00+05:30` and a mandate's `next_debit_at` is in the *future*
relative to it, so "last record timestamp" will not be the right default there.
**Take `now` as an explicit input and default it deliberately.**

**A detection is scored against ground truth by strict corridor match.**
`scoring.py` compares only the fields the manifest's corridor names, and a
detection that sets a field the truth left null does not match. A detection on
the right method but the wrong issuer is a false positive, not partial credit.
Do not loosen this to make a number look better.

**Every corridor-level decision goes through `authorize()`; every payment-level
one through `evaluate()`.** The corridor is where a model recommendation exists;
per-payment recovery is entirely rule-driven and calls no model. That is why the
live run and the `--deterministic` run produce *identical* recovery figures — say
so when quoting them together, or the model appears to have earned the number.

**`RecoveryService` takes the `Session` as well as the `AuditTrail`.** It writes
reroute rows through the same session the trail commits on, so the directive and
the entry authorising it land together. Do not reach into `trail._db`.

**Two things are called confidence and they are never combined.**
`Detection.confidence` is `1 - p_value`, statistical. `model_confidence` is the
model's own. Averaging them would produce a number that means nothing; the run
summary reports them separately, and so should the dashboard.

**Extra seeds are generated inside the test run, not committed.**
`test_root_cause_detection.py` regenerates seeds 7/13/101/2026 through the same
CLI path the foundry uses. That keeps the diff small, but it means those tests do
real work — if a generator signature changes, they break first.

### Observations worth carrying forward

- **Phase 1's "the model always returns confidence 1.0" worry does not hold.** On
  the genuinely ambiguous corridor the live model returned **0.55**, below the 0.6
  floor, so the deterministic classifier took over — an unforced demonstration of
  `corridor-detection:DX1` firing. The confidence signal carries information. Still
  worth watching over more than three calls.
- **A result rejected by the confidence floor is not cached.** `LLMAgent.run()`
  returns the fallback before `cache.put()`, so that call is re-made on every run.
  Not wrong — caching a rejected answer is arguable — but "a seeded re-run is free"
  has an exception, and the pre-demo run should not assume zero live calls.
- **The false-positive detection was still diagnosed `systemic`.** The diagnosis
  layer is asked *why* a corridor degraded and has no way to say "it did not". It
  was refused for an unrelated reason (`RR3`, no alternate route) — luck, not
  design. Adding a `not_degraded` determination is cheap and would make the
  refusal principled. Flagged in ADR 0006's consequences.
- **The engine imports `data.generators.retry_model`**, exactly as Phase 2
  predicted. It works, via a lazy import inside `runner.py` so `app` still imports
  cleanly without the repo root on `sys.path`. Left where it is.
- **`policy_violations` stayed 0 and `blocked` / `halted` / `escalated` stayed
  non-zero** on every run, which is the shape Phase 1 said to expect.
- **The Gemini SDK prints an AFC advisory to stderr on every call.** Cosmetic,
  from `google-genai`, not from our code. Ignore it, or silence it in Phase 8.

### Not done in this phase (deliberately)

No mandate or subscription logic, no invoice logic, no UI, no live Razorpay
traffic (simulated mode throughout, and the mode is on every `api_call` entry).
Commits were left to the user.
