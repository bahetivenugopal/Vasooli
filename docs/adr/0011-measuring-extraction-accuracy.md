# ADR 0011 — How promise-extraction accuracy is measured

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 5 (Engine 3)

## Context

Every other number this project reports is an *outcome* metric: a recovery rate,
a count of suppressed messages, a compliance-blocked attempt. Those are honest,
but they are all statements about what the system did, and none of them can be
wrong in the way a machine-learning metric can be wrong.

Promise extraction is different. There is a right answer to *"does this reply
contain a commitment, and to what date?"*, it was written down before the engine
existed, and the engine either matches it or does not. That makes it the one
genuine ML measurement in the build — and the one a technically sharp panelist
will trust more than any recovery percentage, precisely because it is falsifiable.

Which means the measurement itself has to be defensible, not just the result.

## Decision

### The ground truth is held out, structurally

Phase 2's generator writes every reply's annotation — `is_promise`,
`is_dispute`, `is_conditional`, `promised_date`, `date_confidence`, `language` —
into `invoices.manifest.json`, and **never into the record the engine reads**.
The manifest's own contract says so:

> *Promise/date/dispute annotations live here and nowhere else. Engine 3 reads
> `replies[].text` and must reach the same conclusions unaided — that is what
> makes its extraction accuracy a measurement rather than a claim.*

The separation is enforced three ways rather than trusted:

1. `dataset.resolve_dataset()` **raises** when handed a manifest path, even when
   asked directly, and a test asserts it.
2. `scoring.py` is the only module in the engine that opens a manifest, stated in
   its docstring and structurally true — no other module imports `load_manifest`.
3. The invoice ledger's own status vocabulary excludes `disputed` (Phase 2,
   deviation #9). A dispute exists only in the reply text. A ledger flag would
   have handed the engine the answer.

### Abstentions are never scored

Every rate is computed **over model-handled replies only**, with the abstention
count and rate reported beside it and never folded in.

This is not a presentational choice. An abstention counted as a correct negative
would let the engine improve its accuracy by refusing to answer, which inverts
the entire purpose of having an abstention path. An abstention counted as an
error would push the system toward guessing, which is the failure the fallback
exists to prevent. The only honest treatment is to exclude it and report it.

### The confusion matrix is reported in full

Not headline accuracy. Precision and recall move in opposite directions here and
which one matters depends on the claim:

- A **false promise** suppresses a legitimate chase, then escalates the customer
  for breaking a commitment nobody made. Precision protects against this.
- A **missed promise** chases someone who was cooperating. Recall protects
  against this.

Three separate binary claims are scored — promise detection, dispute detection,
conditionality — each with its own 2×2, plus a per-language split so a headline
number cannot hide a model that only reads English.

### Date accuracy is split by difficulty

The corpus contains dates in four everyday formats (`12/09/2026`, `the 14th`,
`9 Sep`, `9th September`), dates that need an anchor resolved against the reply's
own timestamp ("end of the month"), and promises with no extractable date at all.
Scoring them together hides which half the engine is good at.

**A correct null counts as exact.** The right answer to "when?" on *"next week
sometime"* is silence, and inventing a date there is scored as `spurious`. This
does inflate the headline date figure, which is why the by-difficulty breakdown
is printed beside it and the `none` row is labelled.

### Conditionality is scored only over promises

Scoring it across the whole corpus would pad the matrix with true negatives that
no reader would call a success — a dispute is trivially "not a conditional
promise". It is scored over replies where either the truth or the engine says a
commitment exists.

### Intent is reported, never scored as a strict match

The engine's `ReplyIntent` vocabulary is deliberately finer than the corpus's
five categories: it separates `refusal` and `out_of_office` from `non_response`.
A strict comparison would penalise it for being **more** precise than the
annotations. So intent is reported as a confusion matrix — ground-truth category
against what the engine read — and a reader can see the disagreements and judge
them.

### Failures are enumerated, not summarised

Every mismatch is emitted with the reply text, both readings, which claims were
wrong, and the model's own stated reasoning. Those cases are the genuinely
interesting material for the write-up and the interview, and a metric that
reports only a percentage cannot produce them.

## Why the ground truth is trustworthy — and where it is weak

**Trustworthy because:** it was written *before* the engine, by a generator that
does not judge data (Phase 2, deviation #5), it is committed to version control
with the dataset it describes, the seed reproduces it byte-identically, and the
engine is structurally prevented from reading it.

**Weak because — and this is the part that must be said out loud:**

1. **The corpus is 20 reply templates.** Real correspondence has far more
   phrasings, and an extractor could in principle overfit to these. Phase 2's
   data card says so; it is repeated here because it is the single biggest
   caveat on any number this ADR produces.
2. **One reply per invoice.** Promise-then-renege-then-promise-again does not
   exist in the corpus, so the `superseded` path cannot be scored at all.
3. **The annotations are ours.** They are a careful human's reading of templates
   we wrote, not adjudicated commercial correspondence. Where a template is
   genuinely ambiguous, the annotation records what we intended it to mean.
4. **43 scored replies is a small n.** One error moves promise precision by more
   than two points. A confusion matrix at this scale is an indication, not an
   estimate with a confidence interval.

Consequence: **a perfect score on this corpus is a statement about the corpus,
not a production accuracy claim**, and `docs/metrics/engine-3-receivables.md`
says exactly that where the number appears. To test the claim beyond the corpus,
a separate adversarial probe — replies hand-written to bait a false positive,
none of them drawn from the templates — is run and reported there too. That probe
is where the honest weaknesses actually show up.

## Consequences

- The scorer is tested **adversarially** in `test_receivables_runner.py`: fed
  deliberately wrong readings, it must report them as errors. A metric that
  cannot detect a wrong answer is a metric that always reports success, and the
  control case (a perfect reading scoring perfectly) is worthless without it.
- The extraction score is persisted on the batch record's `notes`, because it is
  the one figure in the run that cannot be recomputed from the audit trail — it
  needs the manifest, and nothing serving an API request should reach for one.
- Adding a reply template to the corpus changes both the dataset fingerprint and
  the reported accuracy. Both carry the dataset `batch_id`, so a number can
  always be traced to the rows behind it.
