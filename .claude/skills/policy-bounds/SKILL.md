---
name: policy-bounds
description: Vasooli's own engine-agnostic policy bounds — the hard attempt ceiling, escalating retry backoff, quiet hours and communication caps, amount-threshold hooks, and the human-escalation triggers. These are product decisions, not regulation, and each one is named so `policy_engine.py` can cite it. Load BEFORE adding or changing any rule in the policy engine that is not already covered by `decline-taxonomy` or `rbi-mandate-rules`.
---

# Policy Bounds (Vasooli product decisions)

## Why this file exists

`CLAUDE.md` non-negotiable #1: **no invented thresholds.** Every rule in
`policy_engine.py` cites a named rule in a skill.

Two skills already cover most of them — `decline-taxonomy` owns retry budgets by
decline class, `rbi-mandate-rules` owns the regulatory constraints and the
mandate-specific bounds. But the shared core also needs numbers neither one
covers: how long to wait between generic retries, when it is acceptable to
message a customer, and when the system must stop acting on its own.

Those numbers are **product decisions**. This file is where they are named,
justified, and made arguable. Nothing here claims regulatory authority.

> **Read this as the counterpart to `rbi-mandate-rules` Part A.** That file
> holds constraints imposed on us from outside. This one holds bounds we chose.
> Blurring the two is how a demo starts claiming a regulator mandated a number
> somebody picked. Keep them separate.

## Rule ids

Cited as `policy-bounds:<id>`, e.g. `policy-bounds:QH1`.

---

## AC — Attempt caps

### AC1 — Hard attempt ceiling: **4**

No entity, in any engine, under any configuration, ever receives more than
**4 recovery attempts in total**.

Per-engine caps may be *lower* — Engine 2's is 3, from `rbi-mandate-rules`
Part B — and a decline class may cut it lower still. Nothing may raise it.

**Rationale.** The ceiling matches the most permissive budget in
`decline-taxonomy` (SOFT = 4), so it can never contradict the taxonomy while
still being a single number a reader can hold in their head. It exists as a
separate rule because it is the bound that survives a configuration mistake: a
per-engine cap read from config could be set to 50, and this is what refuses.

### AC2 — Configured caps clamp, never override

A per-engine cap above AC1 is silently clamped down to AC1, and the clamp is
recorded. It is not an error, because failing a whole batch over a misconfigured
number helps nobody — but it is never honoured either.

**Rationale.** Fail closed. The safe direction of error is always "do less".

---

## CD — Cooldown and backoff

### CD1 — Minimum spacing between attempts: **6 hours**

The generic floor between two attempts on the same entity.

**Rationale.** Short enough that a same-day recovery is still possible when the
cause is transient (a topped-up balance, a rail that came back), long enough
that a batch loop cannot re-hammer an issuer inside a few minutes and trip
issuer-side velocity filters. Engines whose domain demands more must set more:
`rbi-mandate-rules` Part B requires **24 hours** for mandate debits, because
anything shorter cannot satisfy the A2 pre-debit notification window. The
stricter of the two always wins.

### CD2 — Escalating backoff: **doubling, capped at 72 hours**

Attempt *n* waits `CD1 × 2^(n-1)`: 6h, 12h, 24h, 48h — capped at **72 hours**.

**Rationale.** Repeated failure is evidence that the cause is not transient, so
each successive attempt should cost the customer relationship less and buy more
time for the underlying problem to resolve. The cap keeps a recovery schedule
inside a working week, past which the entity should be escalated or written off
rather than chased indefinitely.

---

## QH — Quiet hours and communication limits

These are what keep "chasing" from becoming harassment. They are a compliance
posture, not decoration: an escalation ladder with no ceiling on contact
frequency is a harassment engine with good intentions.

### QH1 — Outreach window: **09:00–21:00 IST**

No customer-facing communication — dunning, reminders, notifications — is sent
outside this window. Attempts inside quiet hours are **blocked and rescheduled**
to the next window opening, never dropped.

**Rationale.** A conventional daytime contact window in the payer's local
timezone. All Vasooli entities are INR/domestic, so IST is the payer's timezone;
an engine handling other geographies would need the window resolved per payer.
Blocking rather than dropping matters — the revenue is still recoverable, only
the timing was wrong, and the audit entry proves the difference.

> Silent, non-customer-facing actions — a server-side retry on an already
> authorised mandate, an internal reroute — are **not** outreach and are not
> subject to QH1. The gate applies to things a human receives.

### QH2 — Contact cap: **3 messages per entity per rolling 7 days**

Across every channel and every engine.

**Rationale.** Three touches is enough for a reminder, a follow-up and a final
notice — the standard shape of a receivables ladder — while making a fourth
message in the same week structurally impossible. Per *entity*, not per
customer, because a customer with three overdue invoices has three legitimate
conversations; a rolling window rather than a calendar week, so a Sunday–Monday
boundary cannot be used to send six messages in two days.

### QH3 — Escalation ladder is capped

The escalation ladder has a **final rung**. Once reached, the entity is handed
to a human (see HE2) rather than escalated further.

**Rationale.** "Escalate on failure" with no terminal rung is an unbounded loop
wearing a policy's clothes.

---

## HS — Hard stops

A hard stop is different in kind from a cap. A cap says "not now, and not more
than N times". A hard stop says "never again, in any code path, whatever else is
true". The policy engine evaluates these **first**, before anything else.

### HS1 — A terminal entity is terminal, irreversibly

Once an entity carries the terminal flag — a revoked mandate, a closed account,
an invoice marked resolved or written off — **no further action of any kind** is
permitted on it. Not a retry, not a reminder, not an escalation.

**Rationale.** The most embarrassing failure a recovery system can have is
chasing someone who already paid, or debiting a mandate the customer cancelled.
Both are one missed condition away in a system where terminality is merely a
strong preference. Making it the first check, and irreversible, is what turns
"we stop" from a claim into a structural property.

### HS2 — Terminal decline classes cancel the schedule

`MANDATE_REVOKED` and `MANDATE_EXPIRED` (`decline-taxonomy` TERMINAL_MANDATE,
`rbi-mandate-rules:A4`) and `FRAUD_SUSPECTED` do not merely fail the attempt —
they end the schedule. The entity is marked terminal and HS1 takes over from
there.

**Rationale.** Restated here because the distinction is easy to lose: these
codes are not "a hard decline that happened to be severe", they invalidate every
*future* attempt as well as this one.

---

## AT — Amount thresholds

### AT1 — Amount-threshold hook

Rules may branch on `amount_paise` against a **named** threshold. A threshold
used in a branch must be registered with an id and a source, exactly like any
other rule. Anonymous magic numbers in a comparison are forbidden.

**Rationale.** Value-dependent behaviour is legitimate — a ₹200 invoice and a
₹2,00,000 invoice do not deserve the same escalation path — but a bare `if
amount > X` is an invented threshold, whatever else it is dressed as.

Registered thresholds:

| Threshold | Value | Source |
| --- | --- | --- |
| `afa_fresh_auth` | ₹15,000 (1,500,000 paise) | `rbi-mandate-rules:A3` — **regulatory**, not ours |
| `high_value_review` | ₹50,000 (5,000,000 paise) | This file, HE3 |

---

## TN — Tone constraints on generated customer messages

**Tone is policy here, not prose preference.** A recovery system drafts messages
to people who owe money, which is precisely the situation where a fluent model
will reach for pressure it has no basis for. These bounds are what stop a
dunning engine from becoming a pressure engine, and they are validated against
the generated text rather than merely requested in the prompt — a constraint
that lives only in a prompt is a suggestion.

They apply to **every** customer-facing message any engine generates: Engine 2's
dunning, Engine 3's reminders, and any notification body.

### TN1 — No fabricated urgency, threats, or unreal consequences

A drafted message may not threaten, imply legal action, credit-bureau reporting,
service termination, collection agencies, or account penalties, and may not
manufacture a deadline that no rule actually imposes. Phrases asserting a
consequence the system cannot and will not carry out are forbidden outright.

**Rationale.** Every one of those is a claim about the future that Vasooli has
no standing to make, and several are regulated conduct in their own right. A
message that recovers revenue by asserting a falsehood has not recovered it
honestly. This is also the single most likely way an LLM makes a demo
indefensible in front of a payments panel.

### TN2 — Accurate statement of cause and remedy

A message must state **what actually happened** — the real failure class — and
**what the customer can actually do about it**. It may not attribute the failure
to the wrong cause, and its call to action must be one the customer can complete.

**Rationale.** Telling a customer whose card expired to "ensure sufficient
balance" wastes the contact, and burns the one message the QH2 cap allows. It is
also the tell that the drafting task is not really using the failure
classification it was given.

### TN3 — A draft that fails validation is replaced, never sent

Validation happens **before** the policy gate, and a failing draft is discarded
in favour of the registered deterministic template for that failure class. The
rejection is audited with the constraint it broke.

**Rationale.** The safe direction of error is a boring accurate message, not a
persuasive unvalidated one. Discarding also keeps the failure visible: a
rejected draft appears in the trail as a rejection, not as a silent edit.

---

## HE — Human escalation

Every bounded agent needs an exit hatch. These are Vasooli's.

### HE1 — Abstention routes to a human

When a reasoning task declines to answer rather than guess, the entity goes to
human review. Recorded as `action: escalate`, `outcome: escalated`,
`provenance.abstained: true`.

**Rationale.** Per `llm-provider`, abstention is a *successful* outcome for
tasks where being wrong is expensive. It only stays successful if something
actually picks the entity up.

### HE2 — Budget exhausted without recovery

An entity whose retry budget is spent, and which has not recovered, is handed to
a human rather than silently abandoned.

**Rationale.** "Stopped retrying" and "gave up on the revenue" are different
outcomes and the audit trail must show which one happened. The whole product
claim is bounded recovery, not bounded effort followed by silence.

### HE3 — High-value entities escalate early

An entity above the `high_value_review` threshold (₹50,000) escalates to a human
on its **first** hard-class failure, rather than running the full ladder.

**Rationale.** The cost of a human looking at one large account is small next to
the cost of automating away a ₹50,000 relationship. The number is a product
decision — chosen as a round order of magnitude above a typical consumer
subscription and comfortably above the regulatory AFA threshold, so the two
never collide — and is deliberately arguable.

### HE4 — Risk blocks are never automated

Anything classified `FRAUD_SUSPECTED` goes straight to a human. No auto-retry,
no auto-dunning, no exceptions.

**Rationale.** Inherited from `decline-taxonomy`, restated here because it is an
escalation trigger as much as a decline class.

---

## Changing anything in this file

These bounds are tunable, but only in one place — this file — and a change to
any of them is recorded in an ADR under `docs/adr/`. A threshold that can be
adjusted at a call site is not a bound.
