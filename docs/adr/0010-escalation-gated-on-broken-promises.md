# ADR 0010 — Escalation is gated on broken promises, not on elapsed time

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 5 (Engine 3)

## Context

Every automated receivables system needs a rule for when to escalate. The
obvious one — and the one almost every dunning tool ships — is a timer: *no
payment after N days at this rung, move to the next rung.*

A timer has real advantages. It needs no reply, no reading, no model. It is
trivially explainable, it cannot be gamed by a customer who says the right
words, and it never stalls: every invoice eventually reaches a human.

It also has one specific, expensive failure. A customer who replies *"our
payment run is on the 12th, it's in that batch"* on the 5th has done exactly
what was asked of them. Under a timer, they receive a firmer message on the
8th anyway, because eight days passed. The escalation is not a response to
anything they did — it is a response to the calendar. That is the behaviour
that makes automated collections feel like harassment, and it is the reason
finance teams turn these systems off.

Phase 2's corpus was built to make the distinction measurable: 30 promises
across the ledger, of which 12 were kept and 8 were broken, plus 10 that named
no date at all. A timer treats all 30 identically.

## Decision

**The ladder ascends on evidence, never on elapsed time** —
`policy-bounds:RL5`. Two things count as evidence, and nothing else does:

1. **A broken promise** (`policy-bounds:PP1`). The customer committed to a date
   and nothing arrived within 48 hours of it.
2. **An exhausted ladder** (`policy-bounds:RL1`). All three rungs are spent and
   the invoice is still outstanding.

Two further triggers exist but are not "the ladder ascending": a dispute
(`RL4`) and an abstention (`HE1`) both route an invoice to a human immediately,
from wherever it was.

The complements are enforced as hard as the triggers:

- **An invoice with a live, unbroken promise is deprioritised and not chased**
  (`policy-bounds:PR2`). It stays on the worklist with its reason visible.
- **A live promise outranks an exhausted ladder.** A customer who replied with
  a credible commitment is not escalated merely because they had already had
  three reminders before replying.
- **A conditional promise can never break** (`policy-bounds:PP2`). "Once our
  client settles with us" is a commitment to intent, not to a date, so there is
  no date to miss. Its exit is ladder exhaustion, like any other invoice.
- **A promise with no extractable date buys seven days** (`policy-bounds:PP3`),
  then the ladder resumes. A pause, never a stop.

Escalation evidence is a typed value — `EscalationTrigger` — and a receivables
`escalate` with `escalation_trigger = None` is **refused by the policy engine**,
not merely absent from a report. The rule is a gate, not an intention.

## What this trades away, honestly

**The engine can be talked out of chasing.** A customer who replies with a
plausible commitment they have no intention of honouring buys 48 hours past a
date of their choosing, or seven days if they name no date. A timer cannot be
manipulated this way. We accept it because the cost is bounded — the promise
register records the commitment, the broken-promise check runs every batch, and
their reliability score follows them onto every other invoice they owe — and
because the alternative penalises the honest majority to deny the dishonest
minority a week.

**A quiet, cooperative-sounding customer can sit longer.** Under a timer, an
invoice moves regardless. Here, a promise inside its window holds it. The bound
that stops this becoming indefinite is PP3's seven-day horizon and RL1's three
rungs: nothing is parked forever, and every invoice still reaches a human.

**It costs a model call per reply.** A timer needs no reading. This engine's
whole escalation decision depends on correctly extracting a commitment from
free text, which is why that extraction is *measured* against held-out ground
truth (ADR 0011) rather than asserted, and why its fallback **abstains** rather
than guessing — a fabricated promise would suppress a legitimate chase and then
escalate the customer for breaking a commitment they never made.

**It is harder to explain than "14 days."** Mitigated by making every decision
state its own evidence: a permitted escalation cites `policy-bounds:RL5` and its
rationale opens with the trigger in words — *"Escalating on a broken promise:
the customer committed to a date and nothing arrived by it, past the grace
window."*

## Consequences

- On the seed-42 ledger, 12 invoices were deprioritised because their customer
  had a live promise. Under a timer, all 12 would have been chased.
- 4 escalations fired on broken promises, 17 on ladder exhaustion, 8 on
  disputes. The split is reported per cause, so the differentiator is visible
  as a number rather than as a claim.
- The 48-hour grace is load-bearing and arguable. It exists because NEFT and
  RTGS settle in batches — a Friday transfer can credit on Monday — and
  escalating that customer would be the most expensive false positive the engine
  can produce. A shorter grace would catch broken promises sooner at the cost of
  occasionally punishing settlement lag.
- **This engine cannot be scored on promise-then-renege-then-promise-again.**
  Phase 2's corpus carries one reply per invoice, so the `superseded` status is
  implemented and unit-tested but never fires in a batch. Reported as zero
  rather than quietly omitted.

## Alternatives considered

**Timer-based escalation.** Rejected above. Would have been considerably less
code and would have made the extraction accuracy in ADR 0011 unnecessary — which
is the point: without reading the reply there is nothing for this engine to be
better at than a cron job.

**Hybrid — a timer with a promise-shaped pause.** Escalate on a timer, but reset
the clock when a promise is detected. Rejected because it reintroduces the
failure it is meant to fix in the undateable case: a promise with no date has no
clock to reset, so the timer runs and the customer is escalated for being
honest about uncertainty. PP3's horizon handles the same case by naming it.

**Let the model decide when to escalate.** Rejected under ADR 0003. The model's
recommendation *is* consulted, through `authorize()`, and it can narrow the
outcome — but the evidence requirement is a gate the model cannot widen. A live
run demonstrated why this boundary matters: a reasonable `pause_chase`
recommendation, made when reading a credible-sounding reply, was narrowing a
*broken-promise* escalation down to doing nothing. The reply-level recommendation
is now consulted only on the reminder path, because a broken promise is a fact
discovered after the reply and the model never saw it.
