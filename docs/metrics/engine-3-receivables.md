# Engine 3 — measured results

Every number on this page came out of an actual run and is reproducible from its
seed. Nothing here is rounded up, and the things that went badly — and the things
that went *suspiciously well* — are on the page with the things that went well.

Reproduce any of it:

```bash
python scripts/receivables_demo.py --batch-id <a fresh id> --now 2026-09-01T09:30:00+05:30
python scripts/receivables_demo.py --batch-id <a fresh id> --now 2026-09-01T03:00:00+05:30
python scripts/receivables_demo.py --batch-id <a fresh id> --deterministic
python scripts/receivables_demo.py --batch-id <a fresh id> --timeline inv_0053
```

`--now` matters more here than anywhere else in the project. Engine 3 is
*entirely* outreach, so a batch run outside the 09:00–21:00 IST window correctly
holds every single reminder. Both clocks are reported below.

---

## The headline run

**Dataset** `inv-s42-680b8a62` (72 invoices, 43 replies, seed 42)
**Run** `rcv-metrics`, seed 42, run clock `2026-09-01 04:00 UTC` (09:30 IST)
**Reasoning layer** Gemini `gemini-3.1-flash-lite`, prompt `understand_reply` v1
**Razorpay** simulated mode (deterministic fixtures, ADR 0004)

| | |
| --- | --- |
| Invoices read | 72 |
| Overdue | 46 |
| Value outstanding | **₹1,35,32,908** |
| Value at risk | **₹2,28,27,113** |
| Value recovered | **₹92,94,205** |
| Recovery rate | **40.72%** |

### Read that rate carefully — the caveat is part of the number

**The engine does not collect any money in this run.** The payments are already
in the ledger. What the engine does is read the replies, extract the commitments,
and decide which of those payments *honoured a commitment it had tracked*.
"Recovered" means exactly that and nothing more, and the definition ships in the
API response body and the demo output so it cannot be quoted without the caveat.

The denominator is equally narrow, and deliberately so: **overdue unpaid
invoices, plus invoices that settled against a commitment this run tracked.** An
invoice that is not yet due is not at risk. An invoice that settled with no
commitment behind it is not this engine's recovery to claim. Both are excluded —
which is why ₹2.28 crore at risk exceeds the ₹1.35 crore outstanding by exactly
the ₹92.9 lakh that came back.

---

## Recovery by ageing bucket

Buckets are derived by the engine from `due_at` and `paid_at`. It never reads the
generator's bucket labels.

| Bucket | Invoices | Outstanding | Recovered | Rate | Reminders | Escalated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| current | 14 | ₹39,05,549 | ₹0 | 0.0% | 0 | 0 |
| 1–30 | 33 | ₹61,49,875 | ₹91,49,981 | **59.8%** | 4 | 6 |
| 31–60 | 13 | ₹34,13,265 | ₹1,44,224 | **4.0%** | 0 | 11 |
| 61–90 | 5 | ₹21,84,585 | ₹0 | **0.0%** | 0 | 5 |
| 90+ | 7 | ₹17,85,183 | ₹0 | **0.0%** | 0 | 7 |

**This is the expected shape and it is worth stating rather than glossing.**
Recovery is concentrated almost entirely in the freshest bucket and collapses to
zero past 60 days. Nothing in the `current` bucket recovers because nothing in it
is late — those invoices are not chased at all.

The escalation column runs the other way, which is the same fact from the other
side: 23 of the 29 escalations sit in the 31+ buckets, because that is where
ladders are exhausted and promises have had time to break.

---

## Extraction accuracy — and why a perfect score is a red flag

**43 replies, 43 scored, 0 abstained (0.0% abstention rate).**
Every rate below is over model-handled replies only.

| Claim | TP | FP | FN | TN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Promise detection | 30 | 0 | 0 | 13 | 100% | 100% | 100% |
| Dispute detection | 8 | 0 | 0 | 35 | 100% | 100% | 100% |
| Conditionality (over promises) | 3 | 0 | 0 | 27 | 100% | 100% | 100% |
| Promise / English | 19 | 0 | 0 | 7 | 100% | 100% | 100% |
| Promise / **Hinglish** | 11 | 0 | 0 | 6 | 100% | 100% | 100% |

**Dates: 30/30 exact.** 14/14 explicit, 6/6 inferable, 10/10 correct nulls.
Zero wrong, zero missed, zero invented.

**Intent confusion** (ground-truth category → what the engine read):

| Corpus category | Engine read |
| --- | --- |
| explicit_promise (14) | promise ×14 |
| vague_promise (13) | promise ×13 |
| conditional_promise (3) | promise ×3 *(all three also flagged `conditional`)* |
| dispute (8) | dispute ×8 |
| non_response (5) | non_response ×2, **out_of_office ×3** |

### Do not quote 100% without the next three paragraphs

The phase brief warned that *"an honest 84% with a discussion of failure cases is
far stronger than a suspicious 99%"*, and this is a suspicious 100%. It is a real
measurement against genuinely held-out annotations — the scorer is tested
adversarially, and fed deliberately wrong readings it reports them as errors —
but **it is a statement about this corpus, not a production accuracy claim.**

The corpus is **20 reply templates**, one reply per invoice, written by us. It
covers the phrasings that matter — four date formats, month-end anchoring,
code-mixed Hinglish, conditional hedges, disputes, auto-replies — but it is 43
scored items drawn from 20 patterns, and a modern model finds it easy. ADR 0011
lists the weaknesses in full.

Two of the "perfect" figures deserve deflating specifically:

- **10 of the 30 exact dates are correct nulls.** The right answer to "when?" on
  *"next week sometime"* is silence, and the engine returned silence. That counts
  as exact — correctly declining to invent a date is the behaviour we want — but
  the genuinely hard half is the 20 dated promises, of which 14 were explicit and
  6 needed an anchor resolved.
- **The `out_of_office ×3` row is not an error.** The engine's vocabulary is
  finer than the corpus's: it separates `out_of_office` and `refusal` from
  `non_response`, and all three of those replies genuinely are out-of-office
  auto-replies. This is why intent is *reported* as a confusion matrix and never
  scored as a strict match — a strict comparison would penalise the engine for
  being more precise than the annotations.

---

## The adversarial probe — where the real evidence is

Because the corpus produced no failures, twelve replies were hand-written to bait
a false positive. **None are drawn from the templates.** All were sent to the live
model with a reply date of **Friday 28 August 2026**, so relative expressions had
an unambiguous right answer.

| Bait | Read as | Promise? | Date | Conf |
| --- | --- | --- | --- | ---: |
| `.` (empty) | **ABSTAINED** | — | — | 0.20 |
| "ok" | non_response | no | — | 0.90 |
| "We will NOT be paying this until the shortfall is fixed. Do not expect payment on the 5th or any other date." | dispute | no | — | 1.00 |
| "If we were going to pay, it would be around the 10th, but I cannot say that we are." | **ABSTAINED** | — | — | — |
| "Our vendor promised us they'd pay on 3rd September. That has nothing to do with your invoice." | non_response | no | — | 1.00 |
| "We paid this on the 12th of August already." | dispute | no | — | 1.00 |
| "Can you resend the invoice with the GST breakup?" | information_request | no | — | 1.00 |
| "Our payment run happens on the 15th of every month. Just so you know." | non_response | no | — | 0.90 |
| "Bhai ye payment abhi nahi ho payega, upar se approval nahi mila hai. Date nahi de sakta." | refusal | no | — | 1.00 |
| "Dekhte hain." | non_response | no | — | 0.95 |
| "Somewhere between the 2nd and the 9th, hard to say." | **ABSTAINED** | — | — | 0.50 |
| "We'll release it next Friday without fail." | promise | **yes** | **2026-09-04** | 1.00 |

**Zero fabricated promises across twelve baits.** Three abstentions, all from the
model's own honestly low confidence rather than from a provider failure. The two
strongest results:

- **The third-party date.** "Our vendor promised us they'd pay on 3rd September"
  contains a promise and a date, neither of which belongs to this invoice. The
  engine read it as `non_response` and extracted nothing. A keyword or regex
  approach fails this outright, which is the clearest single argument for why
  this task is a model call.
- **"next Friday" from a Friday.** Resolved to 2026-09-04, correctly reading
  "next Friday" as the following week rather than the day it was written. The
  reasoning it recorded says so explicitly: *"Since the reply was sent on Friday,
  2026-08-28, 'next Friday' refers to the 4th."*

One result is arguable rather than clean: **the negated refusal was read as
`dispute`, not `refusal`.** "We will NOT be paying this until the shortfall is
fixed" is a refusal grounded in a dispute, and both readings are defensible. The
consequence is not harmful — `dispute` is the *more* conservative route, freezing
the chase under `RL4` where `refusal` would have let the ladder continue — so the
error, if it is one, falls in the safe direction. It is on this page because it is
the only debatable reading in the probe.

---

## Promise register

| | |
| --- | --- |
| Commitments extracted | **30** |
| Kept | 12 |
| Broken | **4** |
| Still active | 14 |
| Superseded | **0** |
| Conditional | 3 |
| No extractable date | 10 |

**Superseded is 0 and will always be 0 on this corpus.** Phase 2's ledger carries
one reply per invoice, so promise-then-revise cannot occur. The status is
implemented and unit-tested rather than left as a schema that lies about what it
tracks — reported as zero rather than quietly omitted.

**Customer reliability** is computed over *resolved* promises only — an active
promise has not happened yet, a superseded one was replaced, and counting either
would move a customer's score without their behaviour changing. Three customers
score 0% (one broken promise each), one scores 50%, eight score 100%. Six more
have promises on record but none resolved, and are reported as **no history**
rather than given a default — no history is not the same as bad history.

---

## Stopped — the harassment-avoided evidence

At **09:30 IST** (inside the outreach window):

| | | |
| --- | ---: | --- |
| Messages suppressed | **8** | all `policy-bounds:RL4`, dispute freeze |
| Invoices deprioritised | **12** | `policy-bounds:PR2`, live promise |
| Disputes frozen | 8 | `policy-bounds:RL4` |
| Reminders withheld in total | 21 | includes deferrals and holds |
| Policy denials | 9 | RL4 ×8, RL2 ×1 |

At **03:00 IST** (outside the window), the same batch:

| | | |
| --- | ---: | --- |
| Messages suppressed | **12** | RL4 ×8, **QH1 ×4** |
| Reminders marked for send | **0** | every one held and rescheduled |
| Policy denials | 14 | RL4 ×8, QH1 ×4, RL2 ×2 |

**The clock changes the answer, and correctly.** Every reminder is held at 03:00
and each carries the moment it becomes permissible — `QH1` blocks and reschedules,
it never drops. This is the single most confusing thing about demoing this engine:
it is the rule working and it looks like a broken engine.

### The two caps that did *not* fire, stated honestly

**Neither contact cap fired in any batch run.** `QH2` (3 per invoice per 7 days)
and `RL3` (4 per customer per 7 days) were never reached, because the ledger's own
history had already spent most ladders — only 4 reminders were authorised across
46 overdue invoices, so there was never enough outbound volume to reach a cap.

Both are covered by unit tests against `PolicyEngine` directly, including the
ordering property that RL3 is checked before QH2. But **on this dataset they are
tested, not demonstrated**, and that distinction belongs on this page rather than
in a footnote. Engine 2 reported `classify_unknown_decline` firing zero times for
the same reason and in the same way.

---

## Escalations, by cause

| Cause | Count |
| --- | ---: |
| Ladder exhausted | 17 |
| Dispute | 8 |
| **Broken promise** | **4** |
| Abstention | 0 |

**The 4 broken-promise escalations are the differentiator, and 4 is a small
number.** It is also the honest one: only 4 of the 30 tracked commitments had
both a resolvable date and a date that had passed without payment. The other side
of the same coin is the 12 invoices deprioritised because their customer had a
live promise — under a timer-based ladder, all 12 would have been chased. That
comparison is the claim worth making, not the raw escalation count.

---

## Provenance — reported per source, never blended

| Source | Entries | At risk | Recovered | Rate |
| --- | ---: | ---: | ---: | ---: |
| model | 73 | ₹1,24,06,373 | ₹92,94,205 | 74.9% |
| deterministic | 54 | ₹1,04,20,740 | ₹0 | 0.0% |

**Unlike Engines 1 and 2, the reasoning layer here genuinely earns the recovery
figure.** In both earlier engines the live run and the deterministic run recovered
*identical* amounts, because the model drafted messages and the rules decided the
money. Here the model reads the commitment, and without a reading there is no
promise to be kept — so a promise entry carries the extraction's provenance and
this engine's recovered rupees sit in the reasoned bucket.

The no-provider run makes the dependency concrete: **43 abstentions, 0 promises,
₹0 recovered, and the batch still completes** with 97 audit entries and a clean
audit check. That is the fallback contract working, not a degraded mode — but it
is also the clearest possible statement that this engine's recovery number is
model-derived.

**Cache hits were 73/73** on the second live run. Seeded re-runs are effectively
free, with the caveat Phase 3 recorded: a result rejected by the confidence floor
is not cached, so the adversarial probe's three abstentions re-call every time.

---

## Audit integrity

`/audit-check` on `rcv-final` (live) and `rcv-det` (no provider):

```
Batch: rcv-final        Entries: 127      PASS      Violations: 0
Batch: rcv-det          Entries:  97      PASS      Violations: 0
```

Rule citations on the live run:

| Rule | Count |
| --- | ---: |
| `policy-bounds:PR1` | 43 |
| `policy-bounds:RL5` | 21 |
| `policy-bounds:PP1` | 20 |
| `policy-bounds:PR2` | 12 |
| `policy-bounds:RL4` | 8 |
| `policy-bounds:PP3` | 7 |
| `policy_engine:permitted` | 4 |
| `razorpay-api:call.logged` | 4 |
| `policy-bounds:RL1` | 4 |
| `policy-bounds:PP2` | 3 |
| `policy-bounds:RL2` | 1 |

`policy_violations` is 0 and refusal outcomes are 30 — the shape Phase 1 said to
expect. A batch with zero blocks would be suspicious rather than clean.

**Razorpay:** 4 test-mode payment links created and audited, 0 failures, all in
simulated mode and marked as such on every entry. A link failure degrades the
message to one without a link rather than aborting the chase; that path is
covered by the tone gate (a message may not ask a customer to "pay via link" when
it carries no link) but was not exercised by a batch, because no link creation
failed.

---

## An example timeline — `inv_0053`

The story the demo tells, end to end, from one API call.

```
inv_0053 — Greenfield Infra Pvt Ltd
₹8,41,889, due 26 Mar 2026, 159 days overdue, bucket 90_plus
Priority rank 2 of 46, score 0.7671
    outstanding_value        0.406 × 0.35 = 0.1421
    days_overdue             1.000 × 0.30 = 0.3000
    customer_unreliability   1.000 × 0.25 = 0.2500
    contact_headroom         0.750 × 0.10 = 0.0750

[2026-04-13 16:30]  classify_decline → success        (policy-bounds:PR1)
  customer said: "Sir humne payment the 18th ko schedule kar di hai,
                  accounts team confirm kar degi."
  read as      : promise (hinglish)
  "The customer explicitly states they have scheduled the payment for the
   18th. Given the reply date is April 13, 2026, 'the 18th' refers to
   April 18, 2026."
  provenance   : gemini-3.1-flash-lite (v1), confidence 1.0

[2026-04-13 16:30]  record_promise_to_pay → failure   (policy-bounds:PP1)
  "Committed to 2026-04-18 and nothing has been received 48h past it. The
   grace window exists so settlement lag is not mistaken for a broken
   promise; this is past it."

[2026-09-01 04:00]  escalate → escalated              (policy-bounds:RL5)
  "Escalating on a broken promise: the customer committed to a date and
   nothing arrived by it, past the grace window."
```

Four things a panelist can check in that one response: a **Hinglish** reply read
correctly; a **relative date** ("the 18th") resolved against the *reply's* own
date rather than run time; a **broken promise** detected by an explicit check
with a named rule; and an escalation citing the rule that **permitted** it,
stating its evidence in words. The customer's own text is in the trail verbatim,
so the reading can be disagreed with.

---

## What underperformed, and what is missing

1. **Only 4 reminders were authorised across 46 overdue invoices.** The ledger
   arrives with most ladders already spent — 17 invoices are at rung 3 — so the
   engine's dominant action is escalation, not chasing. That is correct
   behaviour on this data and it does make the chase ladder look
   under-exercised. A ledger with fresher invoices would exercise it harder.
2. **The contact caps were never reached** (above). Tested, not demonstrated.
3. **Zero abstentions on the live corpus.** The model was confident on all 43
   replies and none fell below the 0.6 floor. The abstention path is
   demonstrated by the no-provider run (43/43), the incoherence guard (unit
   tests), and the adversarial probe (3 of 12) — but not by the live batch, and
   that is worth knowing before claiming the floor is doing work.
4. **`superseded` is structurally unreachable** on this corpus (above).
5. **No payment-link failure occurred**, so the documented degradation path is
   test-covered rather than run-demonstrated.
6. **The tone gate never rejected anything.** Engine 3's reminders are
   deterministic templates, so TN1/TN2 are asserted over every rung by a
   parametrised test rather than catching a live model. Engine 2 is where the
   tone gate has a real rejection to show.
