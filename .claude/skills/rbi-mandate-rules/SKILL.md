---
name: rbi-mandate-rules
description: RBI e-mandate / Additional Factor Authentication (AFA) constraints for recurring debits in India — AFA registration, the 24-48h pre-debit notification window, the per-transaction no-fresh-OTP threshold, and revocation as an absolute hard stop. Load BEFORE writing or changing anything in Engine 2 (mandate recovery), any retry scheduler, or any policy_engine rule touching recurring charges.
---

# RBI e-Mandate & AFA Rules

## What this file is for

Engine 2 does not "retry failed subscription charges". It executes a **bounded,
compliance-gated** retry schedule. The difference between those two sentences is
the entire credibility of the submission.

This file is the factual reference the policy engine cites. Two sections below
are kept rigorously separate, and code comments must preserve the distinction:

- **Part A — Regulatory facts.** Constraints imposed from outside. Not ours to tune.
- **Part B — Vasooli policy choices.** Bounds *we* chose. Auditable and arguable.

Blurring these two is how a demo starts claiming regulatory authority for numbers
someone made up. Do not do it.

---

## Part A — Regulatory facts

Source: project brief section 4 (research-sourced). These are load-bearing. Do
not paraphrase them into something weaker or stronger.

### A1. Mandate registration requires one-time AFA

A recurring payment mandate requires a **one-time Additional Factor
Authentication** (OTP-based) at registration. Until that registration succeeds,
there is no mandate and no debit is permissible.

### A2. Pre-debit notification: 24–48 hours before every debit

A **pre-debit notification must be sent to the customer 24 to 48 hours before
every scheduled debit**. Not just the first one — *every* one.

This applies to retries too. A retry is a scheduled debit. It carries its own
notification obligation.

### A3. The per-transaction AFA threshold

As of the RBI's 2026 update, recurring transactions **up to ₹15,000 per
transaction** can process **without a fresh OTP each cycle**, once the mandate is
registered.

Transactions **above ₹15,000**, or outside the enhanced categories, **still
require fresh AFA each cycle**. Those cannot be silently auto-debited — they need
a customer-present authentication step.

### A4. Revocation is absolute

Customers can **cancel or revoke a mandate at any time**. A revoked mandate is an
**immediate hard stop** and is **never retryable**. There is no grace attempt, no
"one more try", no exception.

### A5. Missing AFA is its own failure mode

If a mandate debit fails because required AFA was absent, **the issuing bank
declines it**. This is a **distinct, separately classifiable failure** — not the
same thing as an insufficient-funds soft decline, and it must not be normalized
into one. See `AFA_REQUIRED` in the `decline-taxonomy` skill.

Retrying an `AFA_REQUIRED` decline without obtaining authentication produces the
identical decline, forever. It burns retry budget for a guaranteed-zero return.

> **Currency and verification.** These reflect the framework as stated in the
> project brief. This is a hackathon build against **test-mode keys only**. Before
> anything here informs a production system, re-verify against the current RBI
> circulars and Razorpay's own recurring-payments documentation. Where code
> encodes a regulatory number, the comment must name the rule (`A1`–`A5`), not
> just restate the number.

---

## Part B — Vasooli policy choices

Ours. Chosen to be conservative and provable. Tunable, but only in one place:
`policy_engine.py`, with the change recorded in an ADR.

Each row carries an **id**, cited from `policy_engine.py` as
`rbi-mandate-rules:PartB.<id>`. A bound with no id cannot be cited, and a rule
that cannot be cited must not exist — see `CLAUDE.md` non-negotiable #1.

| Id | Bound | Value | Rationale |
| --- | --- | --- | --- |
| `max_retries` | Max retry attempts per billing cycle (soft decline) | **3** | Bounded so "stopping rules" is provable. Below the `decline-taxonomy` SOFT budget of 4, because mandate flows are more constrained than one-off payments |
| `min_spacing` | Minimum spacing between retries | **24 hours** | Any shorter cannot satisfy the A2 notification window for the retry itself |
| `notice_staleness` | Pre-debit notice staleness ceiling | **48 hours** | Per A2. A notice older than this is stale — re-notify rather than debit against it |
| `mandate_cap` | Debit above the amount authorised at registration | **blocked** | The registration the customer authenticated (A1) fixes a maximum. A debit above it is not covered by what they agreed to, so it is refused rather than attempted — even when every other precondition holds |
| `paused` | Debits against a **paused** mandate | **blocked**, schedule suspended not cancelled | A pause is a customer instruction to stop collecting, so no debit may fire. Unlike revocation (A4) it is reversible, so it blocks the attempt rather than terminating the mandate — conflating the two would either resume collecting on a paused mandate or permanently kill one the customer only paused |
| — | Attempts after a hard decline | **0** | Per `decline-taxonomy` |
| — | Attempts after `MANDATE_REVOKED` / `MANDATE_EXPIRED` | **0**, schedule cancelled | Per A4 |
| — | Attempts after `FRAUD_SUSPECTED` | **0**, human review | Never auto-anything on a risk block |

---

## Preconditions: the gate every debit passes through

`policy_engine.py` evaluates **all** of these before *any* debit attempt,
including every retry. Any single failure blocks the attempt and writes a
`blocked` audit entry naming the rule that blocked it. Fail closed, never open.

1. **Mandate is active.** Not revoked, not expired, not paused. *(A4)*
   Failure → `MANDATE_REVOKED` / `MANDATE_EXPIRED`, terminal, cancel schedule.
2. **Pre-debit notice was sent, and the timing is right.** *(A2)*
   Require `24h <= (scheduled_debit_at - notice_sent_at) <= 48h`.
   - Notice missing, or gap `< 24h` → `PRE_DEBIT_NOTICE_MISSING`. Block, send the
     notice, reschedule.
   - Gap `> 48h` → notice is stale. Block, re-notify, reschedule.
3. **Amount is within the registered mandate cap.** A debit above the amount the
   customer authorised at registration is not covered by the mandate.
   Failure → block, do not attempt.
4. **AFA status matches the amount.** *(A3)*
   - `amount <= ₹15,000` and mandate registered → auto-debit permitted.
   - `amount > ₹15,000` → **fresh AFA required this cycle.** Do not silently
     auto-debit. Route to a customer-present authentication flow.
   Failure → `AFA_REQUIRED`.
5. **Retry budget remains.** *(Part B)*
   Failure → stop, route to dunning.
6. **Spacing satisfied.** At least 24h since the previous attempt. *(Part B)*
   Failure → reschedule, do not attempt now.

Note the ordering: **A4 revocation is checked first**, before anything else. A
revoked mandate short-circuits every other consideration.

## Stopping rules, stated plainly

Engine 2 halts — permanently, for that mandate — on any of:

- `MANDATE_REVOKED` or `MANDATE_EXPIRED` *(A4)*
- Any HARD-class decline from `decline-taxonomy`
- `FRAUD_SUSPECTED` (and raises for human review)
- Retry budget exhausted (3 attempts in the cycle)
- The billing cycle ending

On halt, Engine 2 hands off to the dunning/communication path rather than
silently giving up. "Stopped retrying" and "gave up on the revenue" are different
outcomes, and the audit trail must show which one happened.

## What this buys the demo

Every one of these is an auditable, testable assertion:

- No debit ever fires without a valid pre-debit notice in its window.
- No debit above ₹15,000 ever auto-fires without fresh AFA.
- No mandate ever sees a 4th retry in a cycle.
- A revoked mandate never sees another attempt, in any code path.

`test-engineer` treats each of these as its own pytest case. They are the proof
behind the phrase "compliant escalation with stopping rules" — see the
`audit-schema` skill for how each blocked attempt gets recorded.
