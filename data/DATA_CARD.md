# Vasooli data card

Every number Vasooli reports is computed from the datasets described here. So
the honesty of this file *is* the honesty of the project. It documents what each
dataset contains, every distribution and why that shape was chosen, where ground
truth lives, the retry-success probability model, and — at the end, plainly —
what these datasets are not.

**All data is synthetic.** No real customer, invoice, mandate or rupee appears
anywhere in this repository. Razorpay is used in test mode only.

---

## 1. The reproducibility contract

| Property | How it is guaranteed |
| --- | --- |
| Same seed + same config = byte-identical output | Every draw comes from a labelled `random.Random` derived from the master seed. No module-level randomness, no `datetime.now()` in generation logic — the clock is an input (`window_start` / `as_of` in the config). |
| The world is readable | Configs are version-controlled JSON in `data/generators/configs/`, and the **resolved config is embedded in every manifest**. You never have to read code to know what was simulated. |
| Batches are self-describing | Each batch writes a manifest: seed, config + fingerprint, generator version, row count, SHA-256 of the output, measured distributions, and ground truth. |
| Ids are reproducible too | `batch_id` is derived from the seed and the config fingerprint (`pay-s42-d5db86f7`), not from a clock or a UUID — so a manifest and an audit trail written days apart still match up. |
| It is tested, not asserted | `apps/api/app/tests/test_data_generators.py` generates each dataset twice and compares bytes, and regenerates each committed sample from its manifest's seed. |

The one non-reproducible value is the wall-clock time a batch was written. It is
quarantined under the manifest's `nondeterministic` key, and a test asserts that
two runs of the same world differ **only** there.

### Regenerating

```bash
python -m data.generators.cli all --seed 42 --out data/samples     # rewrite the committed samples
python -m data.generators.cli payments --seed 42 --out /tmp/check  # or check one
diff /tmp/check/payments.jsonl data/samples/payments/payments.jsonl
```

---

## 2. Where ground truth lives

**Never in a record. Always in the manifest.**

The engines read `<dataset>.jsonl`. They never read `<dataset>.manifest.json`.
That separation is what makes a detection score a measurement rather than a
claim, and it is enforced by a test that walks every field of every record —
including nested ones — and fails if any ground-truth key appears.

| Dataset | Held out in the manifest |
| --- | --- |
| payments | Which corridor was degraded, the window, the configured degraded rate, the observed in-window vs out-of-window rates, and which corridor is a decoy |
| mandates | Each mandate's cohort, its pre-debit notice profile, whether it has an AFA registration gap, which side of the ₹15,000 threshold it sits on |
| invoices | Per reply: is it a promise, is it conditional, is it a dispute, the promised date, how confidently a date can be extracted, and whether the promise was kept |

---

## 3. Payments dataset (Engine 1 — root cause)

`data/samples/payments/` · 589 rows · seed 42 · batch `pay-s42-d5db86f7`

Three days of a mid-size Indian merchant's payment attempts.

### Fields

| Field | Notes |
| --- | --- |
| `attempt_id` | Sequential in time order |
| `batch_id` | On every record, per the `data-synthesizer` contract |
| `created_at` | tz-aware UTC (the diurnal curve is applied in IST, then converted) |
| `customer_id` | Drawn from a pool of 240 |
| `method` | `upi` · `card` · `netbanking` · `wallet` |
| `issuer` / `issuer_name` | Issuing bank or PSP handle |
| `route_id` | Acquirer / rail identifier |
| `amount_paise` | **Integer paise**, whole rupees |
| `status` | `success` / `failed` |
| `failure_reason_code` | On failure only. Strictly a `DeclineCode` from the shared taxonomy |
| `razorpay_reason` | The upstream `error.reason` — **only** where one is doc-verified (see §7) |

### Distributions and why

| Choice | Value | Why |
| --- | --- | --- |
| Overall success rate | **88.8% measured** (band 85–92%) | The realistic band for Indian online payments. High enough that failures are the exception, low enough that recovery is worth doing. |
| Per-method baselines | UPI 91.5%, card 86.2%, netbanking 88.4%, wallet 90.2% | UPI and cards must **not** share a baseline, or a corridor comparison across methods becomes meaningless. Cards carry the extra authentication and network hops, so they sit lowest. |
| Per-issuer deltas | ±0.03 | Small, so that a corridor's *ordinary* variation is visible and the injected event still has to be separated from it. |
| Failure class mix (configured) | SOFT 0.80 · HARD 0.10 · AMBIGUOUS 0.06 · UNKNOWN 0.04 | The ~80%-soft figure is the research grounding of the whole project (see the `decline-taxonomy` skill). A batch where most declines were hard would flatter Engine 2 by making "stop retrying" the usually-correct answer. |
| Failure class mix (**measured**) | SOFT 0.818 · AMBIGUOUS 0.121 · HARD 0.046 · UNKNOWN 0.015 | Measured, not intended — the injected degradations force a specific code inside their windows, which pulls the mix around at 66 failures. Both numbers are in the manifest; neither is quietly replaced by the other. |
| `DO_NOT_HONOUR` present | yes | The ambiguous case is what makes the taxonomy earn its keep. A batch without it tests nothing interesting. |
| Diurnal curve | 24 IST hourly weights, peak/trough ratio **48×** measured | **Deliberate difficulty.** With traffic this uneven, a naive "failure *count* spiked" detector fires at 20:00 IST on a perfectly healthy corridor. Only a rate-based detector survives. Engine 1 has to be genuinely good rather than trivially correct. |
| Amounts | 4 buckets, ₹49 – ₹99,999 | Right-skewed like real consumer traffic: most attempts small, a thin high-value tail that crosses the ₹50,000 human-review threshold (`policy-bounds:HE3`). |

### Injected degradations (ground truth)

| Label | Corridor | Window (IST) | Configured rate | Observed in window | Same corridor outside |
| --- | --- | --- | --- | --- | --- |
| `hdfc-upi-rail-degradation` | HDFC × UPI, all routes | 30 Aug 09:00–21:00 | 0.34 | 21 attempts, **33.3%** | 66 attempts, 97.0% |
| `card-acquirer-b-degradation` | route `rt_card_acq_2`, all issuers | 31 Aug 14:00–20:00 | 0.48 | 10 attempts, **60.0%** | 48 attempts, 85.4% |

The first is the headline event: an issuer's UPI rail degrading, producing the
same decline codes a single customer's problem would, concentrated in one
corridor. The second is deliberately **harder** — route-level rather than
issuer-level, shorter, milder, and lower volume. Engine 1 may well miss it. A
missed detection reported honestly is worth more than a dataset tuned so nothing
is ever missed.

### The decoy (ground truth)

`mobikwik-wallet-low-volume-decoy` — MOBK × wallet, **3 attempts a day**, baseline
0.74, **no degradation injected**. At that volume ordinary variance produces
stretches that read exactly like an outage. If Engine 1 flags it, that is a false
positive and the project reports it as one.

---

## 4. Mandates dataset (Engine 2 — mandate recovery)

`data/samples/mandates/` · 64 mandates, 194 debit attempts · seed 42 · batch `mnd-s42-dabc793d`

### Fields

Mandate id, customer, `amount_paise` per cycle, frequency, `registered_at`,
`afa_registered`, `mandate_cap_paise`, `status`, `cycle_started_at`,
`attempts_in_current_cycle`, `next_debit_at`, `next_debit_notice_sent_at`, and a
`debit_history` of attempts each carrying `scheduled_at`, `attempted_at`,
`notice_sent_at`, status and failure reason.

### Distributions and why

| Choice | Value | Why |
| --- | --- | --- |
| Amounts straddle ₹15,000 | 38 at/below, 26 above (measured) | `rbi-mandate-rules:A3`. Two of the four amount buckets sit deliberately *just* either side of the threshold, because a branch only exercised far from a boundary has not really been exercised. |
| Cohorts | allocated exactly, not sampled | Several acceptance criteria are "this case is present". A revoked mandate that shows up only on lucky seeds is not a test. Measured: healthy 17, first_failure 14, repeat_failure 10, cap_reached 7, revoked 7, paused 5, notice_violation 4. |
| Attempt cap cases | 7 mandates at 3 failed attempts | `rbi-mandate-rules` Part B caps a cycle at 3. These exist so the 4th attempt can be refused on camera. |
| Retry spacing in history | 24–34h | Part B requires ≥24h, because anything shorter cannot satisfy the A2 notification window for the retry itself. |
| Pre-debit notices | on_time 24–48h · late 2–23h · stale 50–96h · missing | A2 is violable in three distinct ways and all three are present in every batch. `not_yet_due` is a fourth, non-violating state: a debit more than 48h out simply has no notice yet, and the manifest distinguishes it so nobody scores it as a violation. |
| Failure mix (all attempts) | SOFT 0.649 · HARD 0.095 · TERMINAL 0.095 · AMBIGUOUS 0.081 · POLICY_BLOCK 0.081 | Lifecycle outcomes (revocations, registration blocks) are *placed*, not drawn, so they inflate the non-soft share. |
| Failure mix (**issuer declines only**) | SOFT 0.787 · HARD 0.115 · AMBIGUOUS 0.098 | The drawn mix, once the placed lifecycle outcomes are set aside. This is the number comparable with the payments dataset. Both appear in the manifest. |
| AFA registration gaps | 3 mandates | `rbi-mandate-rules:A1` — a mandate that never completed registration fails with `AFA_REQUIRED` forever. Retrying it burns budget for a guaranteed-zero return, which is exactly the waste the taxonomy exists to stop. Such mandates have **no** successful history, because there was never a debit they were allowed to make. |

### Two deliberate traps

- **Revoked and paused mandates still carry a scheduled `next_debit_at`.** Real
  schedules are not tidied up the moment a customer cancels. This is precisely
  what `policy-bounds:HS1` exists for, and a book where revoked mandates had no
  upcoming debit would never test it.
- **`attempts_in_current_cycle` is a summary of the history, and agrees with
  it.** A test asserts the two never diverge, so an engine may trust either.

---

## 5. Invoices dataset (Engine 3 — receivables)

`data/samples/invoices/` · 72 invoices, 43 replies, 150 outbound messages · seed 42 · batch `inv-s42-680b8a62`

### Fields

Invoice id, customer id and name, `amount_paise`, `amount_paid_paise`,
`payment_terms`, `issued_at`, `due_at`, `days_overdue`, `status`, `paid_at`, a
`communications` ladder, and `replies` carrying **only** `reply_id`, direction,
channel, `received_at` and `text`.

`days_overdue` is days past due as of `as_of`, or — for a settled invoice — how
late it was when it landed. Either way it is derivable from `due_at` and
`paid_at`, so an engine recomputing it agrees with the ledger.

### Distributions and why

| Choice | Value | Why |
| --- | --- | --- |
| Ageing buckets | current 14 · 1–30 24 · 31–60 16 · 61–90 10 · 90+ 8 | Front-weighted, as a real receivables book is: most overdue paper is recently overdue. Amount is drawn *independently* of ageing, so value share is noisier than count share — a real ledger has no rule saying old invoices are small ones. |
| Amounts | ₹15,000 – ₹25,00,000 | B2B invoice scale, well above the consumer amounts in the payments dataset. |
| Status vocabulary | `open` · `overdue` · `paid` only | There is deliberately **no `disputed` status.** A dispute exists only in the reply text; giving the ledger a flag for it would hand Engine 3 the answer it is supposed to read. |
| Reminder ladder | max 3 rungs, 7 days apart, first at due+3 | Matches `policy-bounds:QH2`/`QH3`. The ladder already in the data cannot exceed the ladder the policy engine would permit, or Engine 3 starts every run in violation of its own contact cap. |
| Archetypes | reliable_slow 19 · first_time_late 17 · chronic_delinquent 16 · disputing 10 · silent 10 | They differ in **behaviour**, not just in numbers. A reliably-slow payer and a chronic delinquent can carry identical ageing and identical amounts; the correct next action for them is not the same. |
| Promise-kept rates by archetype | 0.9 / 0.6 / 0.1 / 0.0 / 0.0 | What makes the archetypes mean something. Measured outcome: **12 kept, 8 broken, 10 still pending.** Both kept and broken must exist, or "escalate only broken promises" is untestable. |

### The reply corpus

43 replies across five categories, in English and Hinglish (26 / 17 measured):

| Category | Count | What it tests |
| --- | --- | --- |
| `explicit_promise` | 14 | Date extraction, across four everyday date formats (`6th September`, `06/09/2026`, `the 2nd`, `25 Aug`) — an extractor that handles one format is not an extractor |
| `vague_promise` | 13 | "end of the month" (a human *can* pin it → `date_confidence: inferable`) vs "next week sometime" (a human honestly cannot → `none`) |
| `conditional_promise` | 3 | "once our client pays us" — a commitment to intent, not to a date. Treating these as firm promises is the failure worth catching |
| `dispute` | 8 | Must never be dunned harder; escalates to a human |
| `non_response` | 5 | Out-of-office and auto-acknowledgements: replies that arrived and said nothing |

Ground truth per reply: `is_promise`, `is_conditional`, `is_dispute`,
`promised_date`, `date_confidence`, `promise_kept`. `date_confidence` exists so
extraction can be scored **without pretending the ambiguous cases have a right
answer** — an extractor should not be penalised for declining to date "next week
sometime", and should be for missing "on the 12th".

### Coverage guarantees (stated, not hidden)

After archetype-driven assignment, a deterministic repair pass forces in any
reply category or language the sampling happened to miss, and guarantees at least
one unambiguously broken promise. Records touched this way are flagged
`coverage_forced` in the manifest. The alternative — acceptance criteria that
hold only on lucky seeds — is worse.

The same idea applies once in the payments generator: if a batch drew zero
unmappable decline reasons, the last failed attempt is converted to one, so the
`UNKNOWN` fail-safe is exercised by data on every seed.

---

## 6. The retry-success probability model

**Generators produce the world; engines produce the outcome.** No dataset
contains a retry outcome, and a test asserts none ever will. See ADR 0005.

`data/generators/configs/retry_success_model.json`, sampled at engine run time by
`data.generators.retry_model.RetrySuccessModel`, seeded from the batch seed.

```
p = base(code) × attempt_decay^(attempt-1) × spacing_multiplier(hours_since_previous)
    capped at 0.95
```

| Decline code | Base p | Reasoning |
| --- | --- | --- |
| `INSUFFICIENT_FUNDS` | 0.42 | The classic recoverable soft decline — balances get topped up. Spacing matters far more than speed. |
| `ISSUER_UNAVAILABLE` | 0.62 | The cause is on the issuer's side and usually clears within hours. Highest base rate in the model. |
| `NETWORK_ERROR` / `GATEWAY_TIMEOUT` | 0.58 / 0.55 | Transport-level, usually transient. Both are `reconcile_first` in the taxonomy — the original may have landed. |
| `TRANSACTION_LIMIT_EXCEEDED` | 0.50 | Clears with the next day's limit window, which is why the 0–6h spacing multiplier is punitive. |
| `AUTH_TIMEOUT` / `CUSTOMER_CANCELLED` | 0.34 / 0.22 | Intent, not capability, is the blocker. Low as a silent retry; both have a *customer-present* rate instead (0.55 / 0.40). |
| `DO_NOT_HONOUR` / `TECHNICAL_DECLINE` | 0.18 / 0.24 | Ambiguous by definition — the issuer did not say why. Deliberately low, matching the reduced retry budget. |
| Every HARD, TERMINAL_MANDATE, POLICY_BLOCK and UNKNOWN code | **0.00** | Hard zero at every spacing and attempt number. If retrying a `CARD_EXPIRED` could succeed here, the soft/hard distinction — the project's central idea — would stop being load-bearing. |
| `AFA_REQUIRED`, customer-present | 0.60 | A silent retry reproduces the decline forever (A5); an authentication flow is the only thing that can work. |
| `PRE_DEBIT_NOTICE_MISSING`, after notice | 0.72 | Our own precondition failed; sending the notice and waiting out the window fixes it. |

| Modifier | Value | Reasoning |
| --- | --- | --- |
| Spacing multiplier | 0–6h: ×0.30 · 6–24h: ×0.70 · 24–72h: ×1.00 · 72h+: ×1.10 | A retry six minutes later hits the same empty balance. This is what makes correct scheduling pay off rather than being merely tidy. |
| Attempt decay | ×0.8 per attempt | Repeated failure is evidence the cause is not transient. Pushes the reported recovery rate **down**. |
| Ceiling | 0.95 | Nothing is certain. Also pushes the number down. |

**These probabilities are ours.** They follow the direction of published dunning
practice — soft declines recover meaningfully on a spaced retry, hard declines do
not recover at all — but they are not measured from production traffic. Anyone
who disagrees with 0.42 can change the file, re-run, and watch the recovery rate
move. That is the point of putting them in a config instead of in code.

---

## 7. The upstream-reason link, and its honest gap

Where a failure's normalized code has a **doc-verified** Razorpay `error.reason`
(the eight error-scenario test cards in the `razorpay-api` skill), the record
carries it in `razorpay_reason`, so the failure can be replayed through
`razorpay_client` in simulated mode and normalizes back to the *same* code. A
test asserts that round-trip.

Where no verified upstream reason exists — `ISSUER_UNAVAILABLE`, `CARD_EXPIRED`,
`ACCOUNT_CLOSED`, `MANDATE_REVOKED` and others — `razorpay_reason` is **null**.
That is meaningful, not missing: inventing an upstream string would make a
replayed failure normalize to `UNKNOWN` and quietly mislabel it. In the sample
payments batch, 34 of 66 failures are replayable this way.

The exception is deliberate: `UNKNOWN`-class failures carry a raw reason that is
*intentionally* absent from the mapping table (`issuer_node_offline`,
`upi_deemed_pending`, `bank_reference_mismatch`), because the fail-safe path —
an unrecognised reason is treated as HARD, never as soft — needs data to exercise
it.

---

## 8. What informed these distributions

- **Soft/hard split (~80% soft), and "treating declines identically" as the
  central failure mode** — the project brief's research grounding, encoded in the
  `decline-taxonomy` skill.
- **Decline-code vocabulary and the upstream mapping** — Razorpay's documented
  error-scenario test cards, verified 2026-09-03, in the `razorpay-api` skill.
- **Mandate constraints** — the RBI e-mandate / AFA framework as recorded in
  `rbi-mandate-rules` Part A: one-time AFA at registration (A1), a pre-debit
  notification 24–48h before every debit (A2), the ₹15,000 per-transaction
  threshold (A3), revocation as an absolute hard stop (A4), and missing AFA as
  its own failure class (A5).
- **Ladder and contact caps** — `policy-bounds` QH2/QH3.
- **Everything else** — traffic shape, bank mix, archetype behaviour, reply
  phrasing — is a plausible-but-invented modelling choice, listed above so it can
  be argued with.

## 9. Limitations, stated plainly

1. **This is synthetic data, not sampled production data.** It is realistic in
   shape because the shapes were chosen from documented domain facts, not because
   anything here was observed.
2. **The retry-success model is an assumption** (§6, ADR 0005). It is the single
   biggest lever on the headline recovery number.
3. **Small n.** The committed samples are sized to stay browsable on GitHub: 589
   payment attempts (66 failures), 64 mandates, 72 invoices (43 replies). Every
   measured share carries the noise you would expect at that scale — the
   route-level degradation has only 10 in-window attempts. Increase the counts in
   the configs for a bigger run; the reproducibility contract is unchanged.
4. **The corridor degradations are step functions**, not the ragged ramps a real
   outage produces. This makes detection somewhat easier than reality.
5. **One reply per invoice.** Real collections threads have several, and
   promise-then-renege-then-promise-again is a case this corpus does not contain.
6. **Reply text is templated**, from a corpus of 20 templates with substituted
   dates. It covers the phrasings that matter but not the long tail of how people
   actually write, and an extractor could in principle overfit to it. The
   Hinglish is written to be representative, not exhaustive.
7. **Calendar months are approximated at 30 days** in mandate cycles. Nothing in
   the policy layer keys off a month boundary, but a real biller's calendar would.
8. **No partial payments, no credit notes, no FX, no multi-currency.** All
   amounts are INR integer paise.
9. **Coverage repair passes** (§5) mean a small number of records are placed
   rather than drawn. They are flagged in the manifest.
