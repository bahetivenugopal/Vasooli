# Vasooli — the complete walkthrough

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**This is the exhaustive tour.** Every screen, every panel on it, every number it
prints — what it shows, what the figure means on the measured run, and why the
panel exists at all. Nothing is summarised away.

It is the companion to [`REVIEWER-GUIDE.md`](REVIEWER-GUIDE.md), not a
replacement for it:

| Read this | If you want |
| --- | --- |
| [`REVIEWER-GUIDE.md`](REVIEWER-GUIDE.md) | To get it running and form a judgement in ten minutes |
| **This file** | To understand the product end to end — every panel, and the reason behind it |

You do not have to run anything to read this. It is written so it makes sense
with the product open beside you *or* on its own, and it names the exact screen
and panel for every claim, so anything here can be checked.

Every Vasooli figure comes from run **`unified-s42-c0b13b0b`, seed 42**, recorded
in [`RESULTS.md`](RESULTS.md). **That file is the single source of truth** — if
this document and `RESULTS.md` disagree, this document is wrong.

---

## Contents

- [0 · How to follow along](#0--how-to-follow-along)
- [1 · One seeded command](#1--one-seeded-command)
- [2 · `/` — the recovery overview](#2---the-recovery-overview)
- [3 · `/engines/root-cause` — Engine 1](#3-enginesroot-cause--engine-1)
- [4 · `/engines/mandate-recovery` — Engine 2](#4-enginesmandate-recovery--engine-2)
- [5 · `/engines/receivables` — Engine 3](#5-enginesreceivables--engine-3)
- [6 · The dispute path](#6--the-dispute-path)
- [7 · `/audit` — the audit trail](#7-audit--the-audit-trail)
- [8 · Proving it](#8--proving-it)
- [9 · The decision loop, in one pass](#9--the-decision-loop-in-one-pass)
- [Sources](#sources)

---

## 0 · How to follow along

### The surfaces, in the order this document walks them

| # | Surface | Covered in |
|---|---|---|
| 1 | The console output of `scripts/unified_demo.py` | §1, §8 |
| 2 | `127.0.0.1:3000/` | §2 |
| 3 | `127.0.0.1:3000/engines/root-cause` | §3 |
| 4 | `127.0.0.1:3000/engines/mandate-recovery` | §4 |
| 5 | `127.0.0.1:3000/engines/receivables` | §5, §6 |
| 6 | `127.0.0.1:3000/audit` | §7 |

Timelines are reached from a table row's **`Timeline →`** link and left with
**Back**, which returns you to the exact table you came from. Setup instructions
are in [`REVIEWER-GUIDE.md` §2](REVIEWER-GUIDE.md#2-run-it-locally) — use
`127.0.0.1`, never `localhost`.

### The numbers, all in one place

| | |
|---|---|
| Headline | **₹94.90 L** recovered of **₹2.41 Cr** at risk · **39.31%** · **348** decisions · **0** schema violations |
| Engine 1 | ₹1,82,503 of ₹5,25,206 · **34.75%** · 110 decisions |
| Engine 2 | ₹13,362 of ₹7,92,346 · **1.69% blended / 30.48% addressable** · 111 decisions |
| Engine 3 | ₹92,94,205 of ₹2,28,27,113 · **40.72%** · 127 decisions |
| Reasoned vs ruled | model 90 entries · deterministic 258. Never blended |
| Trust strip | 95 denials · 40 compliance blocks · 28 retries suppressed · 8 messages suppressed · 38 escalations · 1 fallback · 1 abstention |
| Engine 1 detection | precision **66.7%**, recall **100%** — 2 TP, 1 FP, 0 missed, 0 decoys |
| Engine 2 blocks | A2 ×21 · A3 ×11 · PartB.paused ×5 · PartB.notice_staleness ×2 · A1 ×1 |
| Engine 3 extraction | 43 replies · dates 30/30 exact · English 19/19 · Hinglish 11/11 |
| No-provider run | ₹1.96 L of ₹1.49 Cr · Engines 1 and 2 **identical** · Engine 3 ₹0 |

### Two figures that must never travel alone

These are the two easiest numbers in the project to misread, so they are stated
here before either appears below.

1. **1.69% is never quotable without 30.48% addressable.** Roughly 80% of
   Engine 2's at-risk money sits above the ₹15,000 AFA threshold or behind a hard
   stop, where **no compliant system in India may debit at all**. The blended rate
   alone describes the regulation, not the engine.
2. **"100% extraction" is a finding, not a result.** It is a statement about a
   20-template corpus. The evidence that generalises is the adversarial probe —
   twelve hand-written replies built to bait a false positive, producing zero
   fabricated promises and three honest abstentions.

Both are covered in full at §4.1 and §5.2.

---

## 1 · One seeded command

Everything in this document came out of a single command:

```bash
python scripts/unified_demo.py --seed 42
```

**The header, at the top of the output.** One unified run id, the seed, and the
three datasets it built — 589 payment attempts, 64 mandates, 72 invoices, each
with its own dataset id and row count.

Same seed, same numbers, every time. And the run id is *derived* from the seed,
so two people comparing seed 42 are comparing the same run, rather than two runs
that happen to share a number.

**The consolidated report, at the bottom.** The same report object the dashboard
is about to render, printed to the console. Served two ways from one source. **If
the console and the browser ever disagree, that is a bug, not rounding.**

> **Why it matters:** a synthetic result means nothing unless it is reproducible.
> The seed, the derived run id and the byte-identical datasets are what make every
> number below checkable rather than assertable.

---

## 2 · `/` — the recovery overview

The one screen built to answer a single question in about three seconds: *how
much was at risk, how much came back, and can I trust it?*

### 2.1 · The headline card

**What it shows.** ₹94.90 L recovered out of ₹2.41 Cr at risk — a **39.31%**
recovery rate. To the right: **348** audited decisions, 3 of 3 engines reporting,
and **0** schema violations.

**Schema violations is not a performance metric — it is an alarm.** It counts
rows that reached the audit table without going through the writer. Non-zero
means a bug to hunt, not a number to discuss.

**The caveat line underneath tells you what the headline is *not*.** It is a
**breadth** figure. The three engines measure exposure completely differently — a
corridor's value at risk, a mandate cycle's permitted debit, an invoice's
outstanding balance — so summing them says *"we pointed this at three leak points,
and here is what came back across all three."* It is not a like-for-like rate.

**The badges.** One batch id per engine. Every number on the page traces back to
one of those three.

> **Why it exists:** that caveat sentence lives inside the API response, not in
> the UI's markup. **The dashboard cannot tell a nicer story than the trail
> supports** — the honest framing travels with the data.

### 2.2 · How each engine contributed

Three cards, because the blended figure on its own would be doing far too much
work.

| Engine | Recovered | At risk | Rate | Decisions |
|---|---|---|---|---|
| Root cause | ₹1,82,503 | ₹5,25,206 | 34.75% | 110 |
| Mandate recovery | ₹13,362 | ₹7,92,346 | **1.69% blended / 30.48% addressable** | 111 |
| Receivables | ₹92,94,205 | ₹2,28,27,113 | 40.72% | 127 |

Most of the headline lives in receivables. Mandate recovery's 1.69% is close to
meaningless in isolation — most of that money is legally undebitable, and against
the money the engine was actually *permitted* to touch, it is 30.48%. Both
denominators sit side by side on that engine's page (§4.1).

Each card also carries **its own definition of what "recovered" means for that
engine**, plus the seed and the dataset id it ran against.

> **Why it exists:** three engines with three definitions of recovery is the
> honest situation. Hiding it behind one averaged number is the easiest thing a
> reviewer could catch us doing.

### 2.3 · Recovery rate by engine

The chart plots **rate, not rupees** — deliberately. The three engines differ by
three orders of magnitude, so a money chart would draw receivables as one tall bar
and the other two as a flat line along the axis. The absolute rupees are on the
cards above. Each rate means exactly what that engine's own definition says it
means, and nothing more.

### 2.4 · What the system refused to do — the trust strip

**The most important block on the page.** These are actions an engine proposed and
its own rules stopped.

| Count | What it is |
|---:|---|
| 95 | policy denials |
| 40 | compliance-blocked debits an RBI rule specifically forbade |
| 28 | retries suppressed — attempts not made because they would have been wasted |
| 8 | messages withheld |
| 38 | handed to a human rather than acted on |
| 1 | reasoning task that took its registered deterministic fallback |
| 1 | abstention — the model declined to answer, and said so |

**Non-zero is the good outcome here.** A run with none of these is a run where the
gate never fired, which means either the data was unrealistically clean or the
gate is not real.

**Each of these is classified, not just counted.** Click the *Policy denials* tile
and the by-rule breakdown expands underneath: quiet hours ×24, the pre-debit
notice window ×21, the AFA threshold ×11, dispute freezes ×8, revoked mandates ×7,
spent retry budget ×5. Every one of those is a named rule in a skill file with a
written rationale — not a number somebody picked.

> **Why it exists:** this is the differentiating panel on the whole screen. Most
> dashboards report what they did. **Reporting what you refused to do, itemised by
> rule, is the claim that the bounds are structural rather than decorative.**

### 2.5 · Recent decisions

The latest entries from each engine, each naming the rule that authorised it and
who made the judgment: **violet where a model reasoned it, slate where a rule
decided it**. Those two are never mixed into one figure anywhere in this project.

It is grouped per engine rather than newest-first, because the three engines do
not share a run clock — each derives "now" from its own dataset. A plain sort by
timestamp would only show you whichever engine happened to run last.

To the right, a fresh run can be triggered straight from the browser. Doing so
writes a new batch, and every number in this document moves.

---

## 3 · `/engines/root-cause` — Engine 1

**The leak:** a payment corridor degrades and nobody notices.

This engine watches payment attempts corridor by corridor — issuer, method,
route — and when one degrades it works out whether this is *one customer's
problem* or *the corridor's*. Same decline code, opposite correct answers: get it
backwards and you either harass a customer or ignore a bank outage.

### 3.1 · The run picker and the shared headline strip

The top of every engine page is the same two things:

- a **run picker** naming the selected run, its seed and its dataset, so you
  always know which batch you are looking at;
- **four figures reported identically across all three engines** — recovered, at
  risk, audited decisions (with blocked / halted / escalated underneath), and
  schema violations — so you do not have to relearn where the headline is when you
  move between engines.

Schema violations: 0, again.

### 3.2 · Corridor health

**First row.** 589 attempts ingested, 66 of them failed. **Three degradations
detected** — and the caption on that card says *statistical only, no model
involved in detection*. **One reroute authorised**, and that card states outright
that reroutes contribute **zero** to the recovery number. **11 policy denials**, 6
of them escalated to a human.

**Second row.** **4 retries suppressed** — permanent refusals, a dead instrument or
a spent budget — versus **5 retries deferred**, which is timing only, where the
revenue is still recoverable. Two different things; folding them together would
overstate what was stopped. **1 deterministic fallback**, which is the most
interesting thing in this run (§3.5). And the **run clock**, derived from the last
attempt in the dataset rather than wall-clock time, so a run recorded next month
behaves identically.

### 3.3 · Detection performance against ground truth

The detector is scored against the generator's own ground truth, and scored
**strictly**: a detection on the right method but the wrong issuer is a false
positive, not partial credit.

| | |
|---|---|
| Recall | **100%** — both injected degradations found |
| Precision | **66.7%** — one false positive |
| Decoy false positives | **0** — a corridor the manifest marks as bait; it never fired |
| Missed degradations | none on this run |

**The false positive gets its own card, the same size as the wins, and it is not
netted out.** It is printed in full so it can be checked: netbanking on the direct
route, 57% success over 7 attempts against a 95.5% baseline, p = 0.0029.

Statistically that is a real drop. The generator did not inject anything there, so
it is a false positive and it is counted as one. **Tightening the threshold to
remove it also removes the route-level true positive** — that is the trade, and it
is written up in an ADR rather than quietly made.

### 3.4 · Diagnoses and denials

**Left — diagnoses by determination.** Systemic, individual, or *not enough
evidence to say*. That third bar is the one that matters: **"insufficient
evidence" is a first-class answer here**, because a reasoning layer that always
sounds confident will eventually be confidently wrong. This run: 2 systemic, 1
insufficient evidence.

**Right — denials by rule.** 5 retries stopped inside their six-hour cooldown, 3
on a dead instrument with the budget spent, 1 unrecognised decline that failed
closed to hard, 1 reroute with nowhere to send traffic, and 1 abstaining diagnosis
routed to a human.

### 3.5 · Detections and what followed

**The table that ties the page together: three detections, and what happened after
each one.** Watch the Diagnosis column — these three rows are not the same kind of
decision, and that is the point.

**Row one — HDFC on UPI.** 31.6% success against a 97.3% baseline, 19 attempts in
the window, p < 0.0001, ₹83,000 at risk, 13 distinct customers failing. Diagnosis:
**systemic, issuer outage**, and the badge reads **reasoned**, in violet — a live
Gemini call. It saw that 11 of the 13 failures were `ISSUER_UNAVAILABLE` while
ICICI and SBI on the same method stayed healthy, and concluded the bank's rail was
the problem, not the customers. The policy engine permitted a reroute under
`corridor-detection:RR2`.

**Row two — netbanking on the direct route.** The false positive from §3.3. The
model diagnosed it as a route problem and recommended a reroute as well — and the
policy engine **refused**: `corridor-detection:RR3`, there is no alternate
netbanking route to send traffic to, so a reroute has nowhere to go. The overruled
recommendation is stated in the action column. **The wrong detection did not turn
into a wrong action.**

**Row three — card on acquirer two.** Diagnosis: **insufficient evidence** — and
this badge reads **deterministic**, not reasoned. That is the fallback. The model
did answer, but below the 0.6 confidence floor for that task, so the answer was
discarded, the deterministic classifier took over, reached "insufficient
evidence", and escalated to a human under `policy-bounds:HE1`.

**Nobody staged that.** It is the first time in this project the model reported
genuine uncertainty instead of returning 1.00, and it is the evidence that the
confidence signal carries information rather than being ceremonial.

> Three rows, three different paths: one reasoned and permitted, one reasoned and
> overruled, one where the reasoning layer was rejected by its own confidence gate
> and a rule finished the job. **Every one audited with the rule that decided it.**

> **A note on the confidence value.** `RESULTS.md` records **0.45** for this run;
> the Engine 1 metrics page records **0.55** from a solo run. Both are under the
> 0.6 floor. Read whatever the badge on screen actually shows.

### 3.6 · The corridor timeline

Reached from the **Timeline** link on row one (HDFC × UPI).

**Top row.** The corridor, the observed rate against the baseline, and the
**statistical** confidence with its p-value — and that card explicitly says this is
never mixed with the model's own confidence, because they are different quantities
and averaging them would be meaningless. Value at risk on the right.

**"What the reasoning layer concluded."** The determination, the hypothesis, the
action it recommended, and the provenance badge — then **the model's actual words,
verbatim, out of the audit trail**. You can read them and disagree with them.

**"What the policy engine allowed."** Separately, and deliberately in its own box.
Decision: permitted. Authorised action: reroute traffic. The authorising rule.
Overruled recommendation: nothing here, because on this one the model and the
rules agreed. Then the window, the failing-customer count, and the decline mix
broken out by code.

**Two boxes, deliberately. Judgment on top, permission below. They are never the
same box in this product.**

At the bottom, every decision filed against this corridor in order, with the
legend: **violet circle**, a model produced the judgment; **slate shield**, a rule
did.

**The same two boxes on row two tell the opposite story:** the model says reroute,
the policy box says *refused*, rule `RR3`, overruled recommendation
`reroute_traffic`. **That is the whole design on one screen — a recommendation
cannot authorise what a rule forbids.**

---

## 4 · `/engines/mandate-recovery` — Engine 2

**The leak:** a recurring debit bounces and the cron retries all of them blindly.

Failed recurring charges, classified soft versus hard, retried inside RBI's actual
compliance windows — and hard declines routed straight to dunning instead of
burning retry attempts.

### 4.1 · Two denominators, both reported

**The first thing this page shows, on purpose, is two denominators together.**

- **Blended recovery rate: 1.69%.** On its own that reads like a broken engine. It
  is not — roughly 80% of that money sits above the ₹15,000 AFA threshold or
  behind a hard stop, and **no compliant system in India may auto-debit any of
  it**.
- **Addressable recovery rate: 30.48%** — against the money the policy engine
  actually permitted a debit against.

**Neither is quotable without the other**, which is why they ship in the same API
response and sit in the same card: you literally cannot put one on this page
without the other being on screen. "Addressable" is defined underneath in a
sentence, so it can be checked rather than trusted.

**The figure to lead with is the third one: 40 compliance-blocked attempts, across
five distinct rules.**

### 4.2 · The four cards

- **24 retries suppressed** — hard and terminal declines a blind retrier would
  have fired anyway.
- **28 wasted attempts avoided**, with the baseline it is measured against stated
  on the card rather than implied.
- **7 mandates hard-stopped.**
- **24 messages drafted, 24 held** — one of them rejected by the tone gate.

All 24 held is **a rule working, not a broken dunning path**: this run's clock
lands at 06:39, outside the 09:00–21:00 outreach window, so every message was
rescheduled rather than sent. **Held, never dropped** — each one carries the time
it becomes permissible.

### 4.3 · The four tabs

Four tabs, four different questions.

**Tab 1 — By failure class (default).** Soft declines and hard declines recover at
completely different rates, and a single blended figure hides the exact fact that
makes this engine worth building. Chart on top, then the same data as a table:
mandates, at risk, recovered, rate, debits attempted versus recovered, and retries
suppressed, per class.

**Tab 2 — AFA threshold.** A regulatory branch, not a product one —
`rbi-mandate-rules:A3`. Above ₹15,000 a debit needs fresh authentication, so
nobody is allowed to simply retry it. Both sides of that line, with mandates,
money, debits attempted, authentication requests, and how many were blocked
pending fresh AFA. **This table is the thing that stops the blended rate reading as
failure.**

**Tab 3 — Compliance blocks.** Every bar is an auto-debit this engine declined to
even attempt because a precondition was not met.

| Rule | Blocks |
|---|---:|
| `A2` — pre-debit notice window | 21 |
| `A3` — AFA threshold | 11 |
| `PartB.paused` — paused mandate | 5 |
| `PartB.notice_staleness` — notice too stale to rely on | 2 |
| `A1` — AFA registration gap | 1 |
| **Total** | **40** |

**Every one of them is an attempt a blind retrier makes, and this one does not.**

**Tab 4 — Pre-debit notices.** The caption is the interesting part: this is derived
from the ledger's own timestamps against the 24–48h window. **The engine never
reads the generator's labels** — it reaches the same conclusion unaided, which is
what makes this a measurement rather than marking our own homework. 19 notices
marked for delivery, 19 scheduled, and the rest held outside the outreach window.

### 4.4 · The retry schedule

Every mandate in the run, with its decided next step and the citation behind it:
the amount and which side of the AFA line it falls on, the decline code and where
that routed it, notice status, the next step, when it is scheduled, how many
attempts are left — and the rule, with a plain-English explanation beside it.

**A refusal names the rule that refused just as specifically as a permission
does.** That is what the red **compliance block** badges are.

### 4.5 · A blocked mandate, end to end

**`mnd_0002` — Yash Reddy, ₹19,322, quarterly.**

**The header.** The amount, and the fact that it is *above* the ₹15,000 threshold.
Mandate status active, AFA registered yes. The failure — a gateway timeout,
classified, and where that classification routed it. Pre-debit notice status with
the actual lead time in hours. Next step, when, the authorising rule, attempts
remaining — and the compliance-block badge.

**So: this one failed on a timeout, which is a perfectly retryable kind of
failure. And the retry is still refused**, because the amount is above the
threshold and `rbi-mandate-rules:A3` says that cycle needs fresh authentication
first. A silent retry here reproduces the same decline forever and burns the
attempt budget doing it.

**The drafted message.** Read the line above it: **none were dispatched**. Nothing
in this project sends anything to a real person. The drafts are shown so the tone
gate's work is visible. This one is an authentication request — subject line, full
body, call to action — then the model's reasoning for choosing it, verbatim. The
badges tell you its status and which rule is holding it.

**The bottom of the page.** Every decision on this mandate in order, same legend:
violet for model, slate for rule, every step naming what authorised it.

**`mnd_0006` — Zoya Bhat, ₹44,901, paused. This is the one that makes the
argument.** (If that row does not show it, `mnd_0012` and `mnd_0018` are the same
shape.)

This mandate is **paused by the customer**. The model looked at the failure and
**recommended scheduling a retry, at very high confidence** — and the policy engine
**said no**, under `rbi-mandate-rules` Part B, paused mandate. The overruled
recommendation is stated explicitly on the record, next to the confidence the
model had when it made it. And because that draft had been written to *announce*
the retry, **the draft was discarded and the deterministic template substituted**,
so the customer still hears what actually happened rather than being left with
nothing.

> **This is the rule in the project worth judging: the model supplies judgment, the
> policy engine supplies permission. It can narrow what the model asked for. It can
> never widen it.** And when it overrules, the recommendation, the confidence and
> the refusal all stay on the entry.

> **A note on the overruled counter.** Eight recommendations were overruled in this
> engine at a mid-afternoon clock; at this run's 06:39 clock the counter reads 24,
> because sixteen of those are quiet-hours refusals of perfectly admissible
> messages. **Only the first kind is evidence about the model** — 24 is not "the
> gate overruling the model". The individual mandate above is the honest exhibit.

---

## 5 · `/engines/receivables` — Engine 3

**The leak:** a B2B invoice goes overdue and just sits there.

It ranks overdue invoices, chases them on a capped ladder that never turns into
harassment, reads free-text customer replies for promise-to-pay commitments, and
escalates only **broken** promises.

**This is also the one engine that genuinely needs the model.** With no provider
at all, Engines 1 and 2 recover exactly the same money — they use the model to
diagnose and to draft, never to decide a debit. **This engine recovers zero,
because reading a customer's sentence *is* the task.**

### 5.1 · The four cards and the definition

- **72 invoices**, 46 overdue and worklisted.
- **30 promises tracked** — 12 kept, 4 broken, 14 still live.
- **8 messages suppressed** — quiet hours, contact caps and dispute freezes. The
  direct measure of harassment avoided.
- **29 escalations**, with 8 disputes frozen and 12 invoices deprioritised because
  the customer had a live promise on them.

Underneath, **what "recovered" counts for this engine specifically** — and it is
the most carefully hedged of the three: *the outstanding value of invoices that
settled against a commitment this engine read and tracked.* The payments are in
the ledger either way. What the engine did was identify which of them honoured a
promise. **That caveat ships inside the API response, not just in the docs.**

### 5.2 · Extraction accuracy

Reply-understanding scored against held-out annotations. **Read the caption: every
rate here is computed over model-handled replies only.** Abstentions are reported
separately and never folded in to flatter the denominator.

- **43 replies scored, out of 43.**
- Promise-detection F1, with its precision and recall.
- **Dates: 30 of 30 exact** — zero fabricated, zero missed.
- **Run abstentions** — where the model declined to answer and was routed to a
  human instead of guessing.

Then **three confusion matrices** — promise, dispute and conditional — laid out in
full, true positives through true negatives, so nothing hides behind an F1.

**By language: English 19/19. Hinglish 11/11.** Code-mixed replies are a
first-class case in this engine, not a nice-to-have — the prompt handles them
explicitly, and every reading is tagged `en`, `hinglish` or `mixed`.

**These are 100% scores, and 100% is a finding, not a result.** It is a statement
about a 20-template corpus that a modern model finds easy. There is normally a
panel here listing every reply the engine got wrong; on this run it is absent
because there were none — **which tells you as much about the corpus as about the
model**.

**The evidence that actually generalises is the adversarial probe** in the metrics
doc: twelve replies hand-written to bait a false positive, none of them from the
templates. **Zero fabricated promises, three honest abstentions**, it resolved
"next Friday" correctly from a Friday, and it correctly ignored a date a *third
party* had promised. That is better material than the 100%.

### 5.3 · Ageing buckets

Recovery by ageing bucket. Older invoices recover worse — not a surprise, but it
is reported **per bucket rather than blended**, so the shape is visible rather than
asserted. The chart, then the same data as a table: invoices, outstanding,
recovered, rate, reminders sent, escalations, per bucket.

### 5.4 · Ladder and suppressions

**Left — what the ladder decided.** Send a reminder, escalate, freeze a dispute,
deprioritise, do nothing. **This adds up to the worklist exactly**, which is the
quickest way to confirm no invoice fell through a branch and was silently dropped.

**Right — which rule withheld a message.** Eight, and every one of them a disputed
invoice: `policy-bounds:RL4`, a dispute freezes the chase. **You do not keep
dunning somebody who has told you the invoice is wrong.**

**Beside it, the escalation triggers as badges** — because a permitted escalation
is not a refusal and does not belong in the same bar chart. 17 where the ladder was
exhausted, 8 disputes, 4 broken promises. 29 in total.

### 5.5 · The ranked worklist

Highest priority first, 46 invoices ranked.

**The score is not a black box.** Click any score cell and every term opens
inline — outstanding value at weight 0.35, days overdue at 0.30, customer
unreliability at 0.25, contact headroom at 0.10 — with the raw value, the weight
and the contribution, per term. `inv_0053` comes out at **0.767**.

**The reply column** tells you what the model read the customer's reply as, and in
which language. **The next step** carries its authorising rule, and a red badge
where a rule suppressed the message.

**The grey rows are deprioritised, rule `PR2`:** the customer has committed to a
date that has not arrived yet, so they are pushed down the list. **They are still
on the list.** They are not filtered out, because *"we chose not to chase this, and
here is the rule"* is the claim worth making — and filtering would make that
decision invisible.

### 5.6 · The promise register

Every commitment the model read out of a customer's own words, and what became of
it: **the customer's actual sentence, verbatim**, in the second column. What was
committed and by when. The status — kept, broken, still active, superseded. The
rule that decided that status. And who read it, with the model's confidence.

**The split matters.** The **commitment** was read by a model. The **status** is
ruled arithmetic — did money arrive by the committed date, past the grace window,
yes or no. Which is why each row carries the extraction's provenance rather than
just saying "deterministic". **Those are two different claims, and they are kept
apart.**

### 5.7 · One invoice, end to end — the whole argument

**`inv_0053` — Greenfield Infra Pvt Ltd. The single best screen in the product**,
because the whole thing is visible in one scroll.

**The header.** ₹8,41,000 outstanding, due back in March, 159 days overdue, 90+
bucket. Three of three ladder rungs already used. Priority rank 2, score 0.767.
Next step, when, the rule, and the escalation state.

**What the customer actually said.** Verbatim, in their own words, timestamped:

> *"Sir humne payment the 18th ko schedule kar di hai, accounts team confirm kar
> degi."*

Hinglish. No date format. A relative reference. And the model read it as a
promise. **The reply is shown *above* the reading, deliberately**, so the
extraction can be checked against the text rather than taken on trust from an
accuracy score.

**The commitment, and what became of it.** Status **broken**. Committed to the
**18th of April** — and "the 18th" was resolved against *the date the customer sent
the reply*, the 13th of April, **not** against the run clock. The model's
reasoning, verbatim, says so.

**The status itself.** Broken, under `policy-bounds:PP1` — **and that is not a
judgment call, it is arithmetic**. They committed to a date, nothing arrived, and
we are past the grace window. The grace window exists so a couple of days of
settlement lag is not mistaken for somebody breaking a promise.

**The reminders that were drafted.** Three rungs, each with its own tone, each
passing the same tone gate a model's output would. **None dispatched.**

**Every decision in order, at the bottom.** Reminder goes out. Customer replies in
Hinglish. A model reads a commitment and a date out of free text. **Arithmetic —
not judgment — decides the promise is broken.** And it escalates, under a rule that
*permits* the escalation and states its evidence in words.

> **Four things a reviewer can check on that one screen:** the Hinglish reading, a
> relative date resolved against the right anchor, a broken promise found by an
> explicit check with a named rule, and an escalation citing what allowed it. And
> the customer's own text is in the trail, so you are free to disagree with the
> reading.

---

## 6 · The dispute path

**`inv_0063` — Vardhman Foods** (or `inv_0041`, Shakti Retail) shows the other
shape.

This customer replied saying the amount is wrong and the GST split does not match
their purchase order. The model read that as a **dispute** — not a promise, and
not a refusal to pay — and **the chase froze immediately**, under
`policy-bounds:RL4`. No further reminders; escalated to a human to sort out.

That is 8 of the 8 suppressed messages on this run. **Continuing to dun somebody
who has told you your invoice is wrong is the fastest way to lose the customer
*and* the money.**

---

## 7 · `/audit` — the audit trail

**Everything above came out of this.** One append-only trail, 348 entries, and the
only mutation the system permits is resolving a pending outcome.

### 7.1 · The filters

Seven: batch run, engine, entity type, a specific entity id, the action, the
outcome, and decision source.

Read the line above them — **every one of these is applied by the API, not in the
browser**. So the count it gives back means what it says, rather than being "the
first 300 rows, filtered".

### 7.2 · Policy denials only

**One click, top right, and you are looking at only the things this system decided
*not* to do.** 95 of them, each naming the rule that stopped it, each with its
reason code underneath.

Scroll it: block attempt, block attempt, halt schedule, escalate — with RBI rule
citations, quiet hours, spent retry budgets, dispute freezes. **Every refusal is
evidence that the stopping rules are real**, which is why they get a first-class
view here rather than something you would have to go digging for.

### 7.3 · Decision source

The whole trail sliced by who decided: **90 entries where a model produced the
judgment, 258 where a rule did**. They are reported separately everywhere, and
never blended into one figure that would imply the model earned more than it did.

### 7.4 · One row, expanded

Any row opens in place: the plain-English rationale, the model's reasoning
verbatim where there was one, then the entry id, the batch id, the attempt number
and attempts remaining, and every metadata field on the record.

**That is the level a compliance reviewer would need. And it is the same level the
dashboard used to draw every number above** — because none of those numbers are
stored. There is no counter and no snapshot. **Every figure is recomputed from this
trail at read time.**

---

## 8 · Proving it

**The run audits itself.** Before it prints anything, it recomputes every headline
metric a fourth time straight from the raw audit rows, using arithmetic that
shares no code path with the summary or the dashboard — and compares them.

**Four checks:**

1. **Schema** — every entry has a reason code, a citation that resolves to a real
   named rule, complete provenance, and integer paise.
2. **Cross-source agreement** — the raw recomputation, the batch summary and the
   dashboard's overview report identical figures, per batch, per engine, per
   source.
3. **No orphan actions** — every recovery action cites an actual policy decision.
4. **No untraced money** — every recovered rupee was booked at risk first, and
   sits on an entry that could have recovered it.

All four pass, and the command **exits non-zero if any of them do not**. And each
one is **negatively tested**: the defect is injected and the check is asserted to
catch it, because a validator that has only ever seen clean data is a validator
nobody has tested.

```bash
python scripts/audit_check.py --latest --integrity
```

### The claim least likely to be believed

```bash
python scripts/unified_demo.py --seed 42 --deterministic
```

**Same command, no API key at all.** Every reasoning task falls back to its
registered deterministic implementation. The run completes, integrity passes —
and **Engines 1 and 2 recover exactly the same money**, because the model never
decides a debit there. **Engine 3 recovers zero**, because reading a customer's
sentence is the entire task.

Both runs are honest; they just measure different things. **And both say which
mode they are in on the first line, unprompted.**

---

## 9 · The decision loop, in one pass

Every one of the three engines runs the same loop, in the same shared code.

| Stage | Module | What happens |
|---|---|---|
| **Data foundry** | `data/generators` | A seeded batch. One id, one seed, byte-reproducible |
| **Detection** | `detection.py` | Statistical segmentation and testing — **no model in this step, on purpose** |
| **Gemini agent** | `llm_agent.py` | The real reasoning call, with a registered deterministic fallback and a confidence floor |
| **Policy engine** | `policy_engine.py` | **A completely separate thing from reasoning.** It can narrow what the model asked for; it can never widen it |
| **Recovery actions** | `recovery.py` · `ladder.py` | Bounded actions only, through `razorpay_client.py`, test mode |
| **Audit trail** | `audit_trail.py` | Append-only, written *before* the side effect, citing the rule that authorised it |

**The model supplies judgment. The policy engine supplies permission.** Every one
of those stages wrote an audit row citing the rule that let it happen, and §8
recomputes the whole thing from those rows.

---

## Sources

Every Vasooli figure in this document comes from [`RESULTS.md`](RESULTS.md), run
`unified-s42-c0b13b0b`, seed 42. **That file is the single source of truth** — if
this document and `RESULTS.md` disagree, this document is wrong.

| Go deeper | What is in it |
| --- | --- |
| [`REVIEWER-GUIDE.md`](REVIEWER-GUIDE.md) | Run it from a clean clone in five minutes |
| [`RESULTS.md`](RESULTS.md) | Every measured number and exactly how it was produced |
| [`metrics/`](metrics/) | Per-engine detail, including what underperformed |
| [`architecture.md`](architecture.md) | How the shared core is put together |
| [`adr/`](adr/) | Why each significant decision was made |
| [`failure-paths.md`](failure-paths.md) | Ten rehearsed failure paths and how each degrades |
| [`../.claude/skills/`](../.claude/skills/) | Every named rule the policy engine cites, with its rationale |

---

*All data in this project is synthetic and seeded. Nothing here touches a real
customer, a real invoice, or a real rupee. No message is ever dispatched, and
Razorpay runs in simulated test mode with test-mode credentials only.*
