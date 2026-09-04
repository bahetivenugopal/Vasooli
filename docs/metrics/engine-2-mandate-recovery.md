# Engine 2 — measured results

Every number on this page came out of an actual run and is reproducible from its
seed. Nothing here is rounded up, and the things that went badly are on the page
with the things that went well.

Reproduce any of it:

```bash
python scripts/mandate_demo.py --batch-id <a fresh id> --now 2026-09-01T14:00:00+05:30
python scripts/mandate_demo.py --batch-id <a fresh id> --now 2026-09-01T14:00:00+05:30 --deterministic
python scripts/mandate_demo.py --batch-id <a fresh id> --timeline mnd_0002
```

`--now` matters here in a way it did not for Engine 1. Quiet hours
(`policy-bounds:QH1`) are evaluated against the run clock, so a batch executed at
03:00 IST correctly holds every customer message. Both clocks are reported below.

---

## The headline run

**Dataset** `mnd-s42-dabc793d` (64 mandates, 194 debit attempts, seed 42)
**Run** `mr-live-v2`, seed 42, run clock `2026-09-01 08:30 UTC` (14:00 IST)
**Reasoning layer** Gemini `gemini-3.1-flash-lite`, prompt `draft_dunning_message` v1
**Razorpay** simulated mode (deterministic fixtures, ADR 0004)

| | |
| --- | --- |
| Mandates read | 64 |
| With a failed current cycle | 43 |
| Amount at risk | **₹7,92,346.00** |
| Amount recovered | **₹13,362.00** |
| Recovery rate, all money at risk | **1.69%** |
| Amount addressable | **₹43,839.00** |
| Recovery rate, addressable | **30.48%** |

### Read those two rates carefully

**1.69% is the honest headline and it is not an engine failure.** Of the
₹7,92,346 at risk, ₹6,33,997 sits on mandates above the ₹15,000 per-transaction
AFA threshold, where `rbi-mandate-rules:A3` forbids a silent auto-debit outright.
A further ₹53,274 is on revoked mandates, where A4 forbids everything. No
compliant system can auto-recover any of it this cycle. A single blended rate
measures the engine against money nobody is allowed to collect automatically.

**"Addressable" is defined narrowly and stated on every report of it:** the
amount at risk on mandates whose next step the policy engine *permitted to be a
debit* — attempted, or deferred on spacing. It is a subset of the headline
denominator, never a replacement for it, and the definition ships in the API
response and the demo output so it cannot be quoted without its caveat.

**The recovered figure is a sampled draw**, from the retry-success model in
`data/generators/configs/retry_success_model.json`. Those probabilities are
*ours*, not measured from production traffic — Phase 2's data card says so, and
it is the biggest single lever on this number. What it demonstrates is that the
loop runs end to end and is bounded, not how much revenue this would recover from
real traffic. Four debits were permitted; one cleared.

---

## Recovery by failure class — where the story lives

| Class | Mandates | At risk | Recovered | Rate | Retries suppressed |
| --- | ---: | ---: | ---: | ---: | ---: |
| SOFT | 26 | ₹5,66,244.00 | ₹13,362.00 | 2.4% | 15 |
| AMBIGUOUS | 2 | ₹32,401.00 | ₹0.00 | 0.0% | 1 |
| HARD | 5 | ₹64,980.00 | ₹0.00 | 0.0% | 5 |
| POLICY_BLOCK | 3 | ₹75,447.00 | ₹0.00 | 0.0% | 3 |
| TERMINAL_MANDATE | 7 | ₹53,274.00 | ₹0.00 | 0.0% | 0 |

The classes add exactly to the batch totals; a test asserts it.

**Hard, policy-block and terminal recover zero, and that is correct.** A dead
card cannot be charged, an unauthenticated high-value debit cannot fire, and a
revoked mandate must not be touched. The engine's job on those 15 mandates
(₹1,93,701) is to route them correctly, not to recover them — and it did: 5 to
dunning, 3 to authentication, 7 hard-stopped.

**SOFT recovers 2.4%, not the ~30% the retry model would suggest**, because 15 of
the 26 SOFT mandates were suppressed rather than retried — nearly all of them
above the AFA threshold, where a soft decline still cannot be auto-debited. That
is the A3 branch doing its work, and it is the single largest constraint on this
engine's headline number.

---

## Both AFA branches, exercised (`rbi-mandate-rules:A3`)

| | Mandates | At risk | Debits attempted | Auth requests | Blocked for fresh AFA |
| --- | ---: | ---: | ---: | ---: | ---: |
| At or below ₹15,000 | 38 | ₹1,58,349.00 | 4 | 1 | 0 |
| Above ₹15,000 | 26 | ₹6,33,997.00 | **0** | 13 | 11 |

Zero auto-debits above the threshold is the assertion the whole branch exists to
make, and it holds across the batch rather than in a spot check.

---

## Stopped — the compliance evidence

**40 attempts the engine refused to make because a rule forbade it.** Non-zero is
the point: a batch with none means either the data had no edge cases or the gate
is not firing.

| Rule | Refusals | Kind |
| --- | ---: | --- |
| `rbi-mandate-rules:A2` | 21 | Regulatory — no compliant pre-debit notice |
| `rbi-mandate-rules:A3` | 11 | Regulatory — above ₹15,000, no fresh AFA |
| `rbi-mandate-rules:PartB.paused` | 5 | Ours — mandate paused by the customer |
| `rbi-mandate-rules:PartB.notice_staleness` | 2 | Ours — notice over 48h old |
| `rbi-mandate-rules:A1` | 1 | Regulatory — registration AFA never completed |

All policy denials in the run, including the non-compliance ones:

| Rule | Denials |
| --- | ---: |
| `rbi-mandate-rules:A2` | 21 |
| `rbi-mandate-rules:A3` | 11 |
| `rbi-mandate-rules:A4` | 7 |
| `rbi-mandate-rules:PartB.paused` | 5 |
| `decline-taxonomy:budget.HARD` | 2 |
| `rbi-mandate-rules:PartB.notice_staleness` | 2 |
| `rbi-mandate-rules:A1` | 1 |
| `rbi-mandate-rules:PartB.min_spacing` | 1 |
| `rbi-mandate-rules:PartB.max_retries` | 1 |
| **Total** | **51** |

| | |
| --- | --- |
| Retries suppressed (permanent refusals) | 24 |
| Wasted attempts avoided (estimate) | **28** |
| Mandates that hit the attempt cap and stopped | 3 |
| Mandates hard-stopped | 7 |
| Human escalations | 3 |

### The wasted-attempts figure, with its assumption in the open

28 is a **counterfactual**, and a counterfactual with a hidden assumption is a
marketing number. The baseline: *a system with no soft/hard split spends the full
3-attempt per-cycle budget (`rbi-mandate-rules:PartB.max_retries`) on every
failure.* For each of the 24 mandates whose retry was refused permanently, this
counts the attempts left unspent.

Two ways it is deliberately conservative:

- **Deferred retries are excluded.** A cooldown block is not a suppression — the
  revenue is still recoverable, only the timing was wrong. Conflating them would
  have inflated this figure.
- **The baseline is the compliant cap**, not the unbounded hammering a system
  without stopping rules would actually do. A real naive retrier does worse than
  3 attempts.

### Only 3 mandates report hitting the cap, though 7 are at it

The mandate book contains 7 mandates sitting at the Part B ceiling of 3 attempts.
Four of them are refused by an *earlier* gate — A2 or A3 — because the
precondition order follows the skill's own list, which puts the notice and AFA
checks above the budget check. The reasoning, and the case for the other
ordering, is in ADR 0008. Reported here rather than presented as "3 mandates hit
the cap" without the context.

---

## Communications — drafted, gated, logged, never dispatched

Nothing in this repository sends. See ADR 0009.

Both columns are live-provider runs on the same seed and the same book. Only the
run clock differs.

| | `mr-live-v2` (14:00 IST) | `mr-live-default-clock` (06:39 IST) |
| --- | ---: | ---: |
| Pre-debit notices scheduled | 19 | 19 |
| …marked for delivery | 19 | 19 |
| Customer messages drafted | 24 | 24 |
| …held or suppressed | 1 | **24** |
| Tone rejections (TN1/TN2) | 1 | 1 |
| Recommendations overruled | 8 | 24 |

**Every message is held at 06:39 IST**, and that is `policy-bounds:QH1` working:
outside the 09:00–21:00 window a message is blocked, audited as blocked, and
carries the time it becomes permissible. Held, never dropped. 06:39 IST is where
the run clock lands when `--now` is omitted, because the anchor is derived from
the latest debit the book actually attempted — which is why the demo and this
page both pass an explicit clock as well.

**The overruled count means two different things and should be read carefully.**
`PolicyEngine.authorize()` records the model's recommendation as overridden
whenever the decision is a denial, whatever the denial was for. At 14:00 all 8 are
the interesting kind — the model asking for a `schedule_retry` the rules forbid.
At 06:39 the count is 24 because the 16 additional ones are quiet-hours refusals
of messages whose recommendation was perfectly admissible. Only the first kind is
evidence about the model; quoting 24 as "the gate overruling the model" would
overstate it.

**The one tone rejection is real and worth showing.** On `mnd_0045`
(`TRANSACTION_LIMIT_EXCEEDED`, a retryable failure) the model chose
`contact_support` as the call to action. `policy-bounds:TN2` refused it — a
support conversation cannot resolve a limit the customer's own bank sets — the
draft was discarded, and the templated message went out saying what actually
happened and what raising the limit would do.

**Eight model recommendations were overruled.** On eight mandates the model
recommended `schedule_retry` at confidence 0.95–1.00 — for a paused mandate, and
for mandates blocked on authentication. `policy_engine.authorize()` refused every
one: `schedule_retry` is not in the envelope for a message about a failure the
rules will not retry. Because those drafts were *written to announce the retry*
(one model rationale said the message "prepares them for the scheduled retry"),
the draft is discarded and the template substituted, so the customer still hears
what happened. The refusal, the recommendation and the confidence are all on the
audit entry.

That is the clearest evidence in this engine that the gate is real: a confident
model asking to charge a mandate the customer paused, refused on the record.

---

## Provenance — reasoned vs ruled, never blended

| | Live run | Deterministic run |
| --- | ---: | ---: |
| Entries, `source: model` | 15 | 0 |
| Entries, `source: deterministic` | 96 | 111 |
| LLM fallbacks taken | 0 | 24 |
| Recommendations overruled | 8 | 0 |
| Unknown declines sent to the reasoning layer | 0 | 0 |
| Policy violations | 0 | 0 |
| Amount recovered | ₹13,362.00 | ₹13,362.00 |

**The two runs recover exactly the same money.** The model drafts messages; it
does not decide debits. Every scheduling decision in this engine is rule-driven,
so a live run and a no-key run produce identical recovery figures. Say that
whenever the two are quoted together, or the model appears to have earned the
number.

**Zero unknown declines were classified.** Phase 3 left the shared
`classify_unknown_decline` task unwired and flagged it for Engine 2. It is wired
here — an unrecognised failure code routes to it and falls back to HARD — and on
this book it fires **zero** times, because every code in it is in the taxonomy.
Reported as zero rather than quietly omitted; the path is covered by a unit test,
not by the batch.

**15 model entries, all cache hits.** The run was re-executed after an identical
earlier run, so the disk cache served every call. A cached result is still
`source: model` with `cache_hit: true`.

---

## `/audit-check`

Run over both batches, 111 entries each:

```
Batch: mr-live-v2        Entries: 111
PASS/FAIL: PASS
Violations: 0

Batch: mr-det-v2         Entries: 111
PASS/FAIL: PASS
Violations: 0
```

Outcomes across the live batch: `blocked 41 · scheduled 42 · pending 9 ·
halted 7 · failure 6 · escalated 3 · success 2 · skipped 1`.

Actions: `block_attempt 41 · send_dunning 23 · send_pre_debit_notice 19 ·
schedule_retry 9 · halt_schedule 7 · attempt_charge 4 · api_call 4 · escalate 4`.

---

## An example timeline — `mnd_0002`

The single-mandate route the demo leans on, in full:

```
2026-09-01 08:30 UTC  block_attempt   blocked    rbi-mandate-rules:A3
    Rs 19,322.00 is above the Rs 15,000.00 per-transaction threshold, so this
    cycle needs fresh AFA. It cannot be silently auto-debited; the customer has
    to authenticate first (A5: a silent retry reproduces this decline forever).

2026-09-01 08:30 UTC  send_dunning    scheduled  policy_engine:permitted
    The transaction exceeded the Rs 15,000 threshold, requiring fresh AFA as per
    RBI rules. Since the failure was a gateway timeout, we must ask the customer
    to re-authenticate to clear the mandate.

MESSAGE [request_authentication] marked_for_delivery via email
    cta: complete_authentication
    subject: Action required: Please authenticate your recurring payment
    body: Hi Yash, we attempted to process your quarterly payment of
      Rs 19,322.00, but the connection timed out. Because this payment amount
      requires a fresh authentication step, we need you to complete a quick
      verification to proceed with your subscription. …
```

Two attempts refused, one message drafted, every step naming its rule —
`GET /api/v1/mandate-recovery/runs/{batch_id}/mandates/mnd_0002` returns all of
it in one response.

---

## What the engine derived correctly, checked against ground truth

The generator's manifest records each mandate's pre-debit notice profile. Engine
code never opens it; the engine derives the same label from timestamps.
`test_the_notice_profiles_the_engine_derived_match_the_book` cross-checks the two
and finds **54 of 64 exact matches**, with the remaining 10 being the
`not_yet_due` profile — where the generator knows a notice was not yet owed and
the engine, seeing only an absent notice, records `missing`. That is the
conservative reading and the right one: an engine cannot know a notice is not
yet due without assuming its own schedule is correct.

---

## What underperformed, and why

**The blended recovery rate is low and will stay low on this dataset.** 80% of
the money at risk is behind a compliance gate. Widening the amount distribution
below ₹15,000 would raise it, but that would be tuning the data to flatter the
engine — the honest fix is to report both denominators, which is what we do.

**Only 4 of 17 retryable mandates received a debit this cycle.** Seven are
waiting on a compliant pre-debit notice (A2's prescribed response), one is
deferred on spacing, five are permanently blocked. That is the compliance
schedule working, not a bug — but it means one run of this engine looks quiet.
A multi-cycle simulation would show the notices landing and the debits following;
it is not built, and no number here should be read as if it were.

**One debit in four cleared.** With `INSUFFICIENT_FUNDS` at a base probability of
0.42, decayed for attempt number and scaled by spacing, the expected clearance is
roughly one in three. One in four on four draws is inside the noise, and four
draws is far too small a sample to say anything about the model.

**`fresh_afa_completed` is always false.** Nothing in this build performs a
customer authentication, so no above-threshold mandate can ever progress past A3
within a run. The authentication request is drafted and gated; what happens after
it is out of scope.
