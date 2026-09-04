# Results — the measured unified run

> Every number quoted in the video, the README or the submission form comes from
> here. If a figure appears somewhere else in this project and disagrees with
> this file, this file is wrong or that figure is — either way, one of them is a
> bug, not a difference of framing.

Produced by one command:

```bash
python scripts/unified_demo.py --seed 42
```

Reproduce it yourself and you should get this file's numbers exactly. If you
don't, the run says so: the same command runs a metric-integrity audit over its
own output and exits non-zero if anything disagrees.

---

## 1. Provenance of this run

| | |
| --- | --- |
| Unified run id | `unified-s42-c0b13b0b` |
| Per-engine batch ids | `…-rc` · `…-mr` · `…-rcv` |
| Seed | 42 |
| Datasets | `pay-s42-d5db86f7` (589 attempts) · `mnd-s42-dabc793d` (64 mandates) · `inv-s42-680b8a62` (72 invoices) |
| Reasoning | **Live provider** — `gemini-3.1-flash-lite` |
| Razorpay | **Simulated** (deterministic fixtures; test-mode credentials only, never live) |
| Wall time | 9.9s |
| Audited decisions | **348** |
| Schema violations | **0** |
| Integrity verdict | **PASS** on all four checks |

The three engines do **not** share a run clock, and that is deliberate. Each
derives `now` from its own data because each dataset is anchored differently:

| Engine | Run clock (UTC) | Derived from |
| --- | --- | --- |
| Root-Cause | `2026-08-31 18:23` | the last payment attempt in the file |
| Mandate | `2026-08-31 01:09` | the latest debit **actually attempted**, not the latest timestamp |
| Receivables | `2026-09-01 04:00` | the latest observed contact, reply or payment, rounded up to the hour |

Forcing one clock across all three would make two of them wrong, and the failure
is silent — every compliance gate would fire, for the wrong reason, and the run
would still complete.

---

## 2. The headline

> **₹94.90 lakh recovered of ₹2.41 crore at risk — 39.31% — across 348 audited
> decisions with zero schema violations.**

### What that number is

A **breadth** figure. The three engines measure exposure differently — a
degraded corridor's value at risk, a mandate cycle's permitted debit, an overdue
invoice's balance — so summing them says "this system was pointed at three leak
points and here is what came back across all three". It is not a like-for-like
recovery rate, and the dashboard says so on screen rather than in a footnote.

Per-engine figures, each with its own definition, are in §3. Quote those for
anything specific.

### What that number is not

- It is **not** measured against production traffic. The data is synthetic and
  seeded; §7 lists every assumption that bounds what it can mean.
- It is **not** a claim that the engines *caused* every recovered rupee. Engine
  3's contribution is the outstanding value of invoices that settled against a
  commitment it read and tracked — the payments are in the ledger either way,
  and what the engine did was identify which of them honoured a promise. That
  caveat ships inside the API response, not just in this file.
- It is **not** a cherry-picked batch. Seed 42 is the committed default and has
  been since Phase 2. Engine 1's five-seed spread is in
  `docs/metrics/engine-1-root-cause.md`, false positives included.

### Reasoned versus ruled — never blended

| Source | Entries | At risk | Recovered | Rate |
| --- | ---: | ---: | ---: | ---: |
| Model | 90 | ₹1.24 Cr | ₹92.94 L | 74.91% |
| Deterministic | 258 | ₹1.17 Cr | ₹1.96 L | 1.67% |

**The model earns most of the headline, and that is a dependency as much as a
win.** Engines 1 and 2 recover *identical* amounts whether the provider is on or
off — they use the model for diagnosis and drafting, never for the decision that
moves money. Engine 3 does not: no reading means no promise, so with the
provider switched off it recovers **₹0** and the unified headline falls to
**1.32% of ₹1.49 crore**. Both runs are honest; they measure different things.

---

## 3. Per-engine contribution

| Engine | At risk | Recovered | Rate | Entries |
| --- | ---: | ---: | ---: | ---: |
| 1 · Root-Cause Recovery | ₹5,25,206 | ₹1,82,503 | **34.75%** | 110 |
| 2 · Mandate & Subscription Recovery | ₹7,92,346 | ₹13,362 | **1.69%** | 111 |
| 3 · B2B Receivables Chaser | ₹2,28,27,113 | ₹92,94,205 | **40.72%** | 127 |

### Engine 1 — Root-Cause Recovery

| Metric | Value |
| --- | --- |
| Payment attempts ingested | 589 (66 failed) |
| Corridors flagged | 3 |
| Detection precision / recall | **66.7% / 100%** (2 true positives, 1 false positive, 0 missed) |
| Decoy false positives | **0** — the deliberately noisy corridor never fired |
| Diagnoses | 2 systemic, 1 insufficient evidence |
| Reroutes authorised | 1 |
| Retries suppressed / deferred | 4 / 5 |
| Policy denials | 11 (2 of them overruling the model) |
| Human escalations | 6 |

Recovery definition: the value of failed payments that a **per-payment** retry,
authorised against that payment's own decline class and budget, went on to
settle. Corridor-level actions contribute **nothing** — a reroute is authorised,
bounded and expiring, but the retry-success model has no route dimension, so
crediting rerouted traffic with an uplift would be inventing the headline number.

The false positive is real and is reported rather than tuned away. Full detail,
including the five-seed spread (64% / 70%), is in
[`metrics/engine-1-root-cause.md`](metrics/engine-1-root-cause.md).

### Engine 2 — Mandate & Subscription Recovery

| Metric | Value |
| --- | --- |
| Mandates ingested | 64 (43 with a failed cycle) |
| **Addressable recovery** | **30.48%** of ₹43,839 |
| Debits attempted / recovered | 4 / 1 |
| Compliance-blocked attempts | **40**, across five distinct rules |
| Wasted attempts avoided | 28 (24 retries suppressed) |
| Pre-debit notices sent / scheduled | 19 / 19 |
| Communications drafted / held | 24 / 24 |
| Tone rejections | 1 |
| Mandates hard-stopped | 7 |

**Two denominators, always reported together.** The blended 1.69% is honest and
close to meaningless on its own: roughly 80% of the money at risk sits above the
₹15,000 AFA threshold or behind a hard stop, where *no* compliant system may
auto-debit. The addressable rate — money the policy engine actually permitted a
debit against — is 30.48%. Neither number is quotable without the other, which
is why both ship inside the API response.

The 40 compliance blocks, broken down:

| Rule | Blocked | What it is |
| --- | ---: | --- |
| `rbi-mandate-rules:A2` | 21 | pre-debit notice window not satisfied |
| `rbi-mandate-rules:A3` | 11 | above the ₹15,000 threshold, fresh AFA required |
| `rbi-mandate-rules:PartB.paused` | 5 | mandate paused |
| `rbi-mandate-rules:PartB.notice_staleness` | 2 | notice too old to rely on |
| `rbi-mandate-rules:A1` | 1 | AFA registration gap |

All 24 drafted messages were held: the run clock lands at 06:39 IST, outside the
09:00–21:00 outreach window (`policy-bounds:QH1`), so every message was
rescheduled rather than sent. That is the rule working and it looks like a broken
dunning path — [`metrics/engine-2-mandate-recovery.md`](metrics/engine-2-mandate-recovery.md)
reports a second run at a mid-afternoon clock for exactly this reason.

### Engine 3 — B2B Receivables Chaser

| Metric | Value |
| --- | --- |
| Invoices ingested / overdue | 72 / 46 |
| Outstanding on the book | ₹1,35,32,908 |
| Replies read | 43 (0 abstentions on this corpus) |
| **Promise detection** | precision **1.00**, recall **1.00** (30 TP, 0 FP, 13 TN, 0 FN) |
| **Dispute detection** | precision **1.00**, recall **1.00** (8 TP, 0 FP) |
| **Conditional detection** | precision **1.00**, recall **1.00** (3 TP, 0 FP) |
| **Date accuracy** | **30 of 30 exact** — 14 explicit, 6 inferable, 10 undateable, all correct |
| By language | English 19/19 · Hinglish 11/11 |
| Promises kept / broken / active | 12 / 4 / 14 |
| Reminders sent / held | 4 / 21 |
| Disputes frozen | 8 |
| Invoices deprioritised (live promise) | 12 |
| Escalations | 29 — 17 ladder exhausted, 8 dispute, 4 broken promise |

**A 100% score is a finding, not a result.** It is a statement about a
20-template corpus, and an extractor could in principle overfit to it. The real
evidence is the **adversarial probe** — twelve replies hand-written to bait a
false positive, none of them from the templates — where the model produced zero
fabricated promises, three honest abstentions, correctly resolved "next Friday"
from a Friday, and correctly ignored a third party's promised date. That probe is
in [`metrics/engine-3-receivables.md`](metrics/engine-3-receivables.md) and is
better material than the 100%.

---

## 4. The trust strip — evidence the bounds are real

A recovery number with no refusals beside it is a system that never said no.

| Count | Value | What it means |
| --- | ---: | --- |
| Policy denials | **95** | actions the system proposed and its own rules refused |
| Compliance-blocked | **40** | debits an RBI rule specifically forbade |
| Retries suppressed | **28** | attempts not made because they would have been wasted |
| Messages suppressed | **8** | outreach withheld — every one a disputed invoice |
| Human escalations | **38** | handed to a person rather than acted on |
| Deterministic fallbacks | **1** | a reasoning task that took its registered fallback |
| Abstentions | **1** | the model declined to answer and said so |

Denials by rule, top six:

```
policy-bounds:QH1               x24   outside the outreach window
rbi-mandate-rules:A2            x21   pre-debit notice window
rbi-mandate-rules:A3            x11   AFA threshold
policy-bounds:RL4                x8   dispute freezes the chase
rbi-mandate-rules:A4             x7   revoked mandate — absolute hard stop
decline-taxonomy:budget.HARD     x5   hard decline, retry budget spent
```

The one deterministic fallback is the most interesting entry in the run: on a
genuinely ambiguous corridor the model returned **confidence 0.45**, below the
0.60 floor for that task, so the deterministic classifier took over and the
result is audited as `source: deterministic`. Nobody arranged that.

**`policy_violations` is 0.** A non-zero value would mean a row reached
`audit_entries` without going through the writer — a bug hunt, not a metric.

---

## 5. Methodology — how each figure is produced

1. **Every figure is recomputed from the audit trail at read time.** No counter,
   no accumulated total, no snapshot. `BatchRun.summary` exists as a dashboard
   convenience and is never quoted.
2. **The console report and the dashboard are one computation.**
   `scripts/unified_demo.py` renders the object `services/overview.py` produces,
   and `/api/v1/overview` serves the same object to the browser. They cannot
   disagree, because there is nothing to disagree.
3. **The dashboard computes nothing.** Not one arithmetic operation on a metric
   happens in the browser. Two places that compute a number are two numbers that
   eventually disagree, and one of them disagrees on camera.
4. **Money is integer paise everywhere**, formatted to ₹ at display only, in
   exactly one utility per side of the wire.
5. **The run audits itself.** Before printing, it recomputes every headline
   metric a *fourth* time from raw `audit_entries` rows, using arithmetic that
   shares no code path with the summary or the overview, and compares. It also
   runs all twelve `/audit-check` validations and the two traceability checks. A
   discrepancy fails the command.

### The four integrity checks, and what each proves

| Check | Proves |
| --- | --- |
| `audit_schema_validations` | every entry has a reason code, a citation that resolves to a real rule, complete provenance, an ordered timeline, integer paise, and no secrets |
| `cross_source_metrics_agree` | the raw recomputation, `batch_summary()` and the dashboard's overview report identical figures per batch, per engine, per source |
| `no_orphan_actions` | every recovery action cites a policy decision — not merely a logging marker |
| `no_untraced_money` | every recovered rupee sits on an entry that could recover it, was booked at risk first, and carries a success outcome |

All four pass on this run. Each is negatively tested in
`app/tests/test_metric_integrity.py` — the defect injected, the check asserted to
catch it — because a validator that has only ever seen clean data is a validator
nobody has tested.

---

## 6. Reproducibility

```bash
python scripts/unified_demo.py --seed 42                 # this run
python scripts/unified_demo.py --seed 42 --deterministic # with no provider at all
python scripts/audit_check.py --unified <run_id> --integrity
```

- **The run id is derived from the seed**, so two people comparing seed 42 are
  comparing the same run rather than two runs that happen to share a number.
- **The same seed reproduces identical results.** Verified twice: in the test
  suite against a fingerprint of every metric, and by re-running the live path
  with a salt and diffing the reports. Batch ids and wall times differ; every
  number is identical.
- **Datasets regenerate byte-identically** from their manifest's seed
  (`python -m data.generators.cli all --seed 42`).
- **Engine 1 stamps its audit entries with wall-clock time**, so entry timestamps
  are *not* reproducible. This is a known defect, recorded in the phase log,
  and is why determinism is asserted over metrics rather than over rows.

---

## 7. The no-provider run — same command, no keys

```bash
python scripts/unified_demo.py --seed 42 --deterministic
```

| | Live provider | No provider |
| --- | ---: | ---: |
| At risk | ₹2.41 Cr | ₹1.49 Cr |
| Recovered | ₹94.90 L | ₹1.96 L |
| Rate | 39.31% | 1.32% |
| Audited decisions | 348 | 318 |
| Integrity verdict | PASS | PASS |
| Engine 1 recovered | ₹1,82,503 | ₹1,82,503 — *identical* |
| Engine 2 recovered | ₹13,362 | ₹13,362 — *identical* |
| Engine 3 recovered | ₹92,94,205 | ₹0 |

This is the design working, not degrading. Engines 1 and 2 recover the same money
either way because the model never decides a debit there — it diagnoses and it
drafts, and the policy engine decides. Engine 3's number depends entirely on the
model, because reading a customer's sentence *is* the task; with no reader there
is no promise to track, so the denominator shrinks and the numerator goes to
zero. Both runs report which mode they are in, on the first screen, unprompted.

---

## 8. Honest limitations on these numbers

Stated here rather than left for a reader to find:

1. **All data is synthetic and seeded.** No real customer, invoice or rupee is
   involved. Razorpay is in simulated mode; test-mode credentials only, ever.
2. **Retry outcomes are drawn from a probability model we wrote**
   (`data/generators/retry_model.py`, ADR 0005). It is the single biggest lever
   on Engine 1's and Engine 2's recovery numbers, and it is an assumption, not a
   measurement.
3. **No communication is ever dispatched.** Every dunning message and reminder is
   drafted, tone-validated, gated and recorded — and then stops. ADR 0009.
4. **A reroute contributes zero recovery.** It is authorised, bounded and
   audited, but the retry model has no route dimension.
5. **One reply per invoice.** Promise → renege → promise-again does not exist in
   this corpus, so Engine 3 cannot be scored on it.
6. **Engine 3's reply corpus is 20 templates.** The 100% extraction score is a
   statement about that corpus. The adversarial probe is the evidence that
   generalises further; it is still only twelve replies.
7. **Neither contact cap ever fired in a batch run.** The ledger arrives with
   most ladders already spent, so QH2 and RL3 are covered by unit tests against
   the policy engine directly — tested, not demonstrated.
8. **`classify_unknown_decline` fires zero times on this book.** It is wired and
   unit-tested; every failure code in the corpus is already in the taxonomy.
9. **This is a prototype built in about thirty hours.** No auth, no
   multi-tenancy, no deployment, SQLite, one process.

---

*Generated from `unified-s42-c0b13b0b`. Re-run `python scripts/unified_demo.py
--seed 42` to reproduce every figure above.*
