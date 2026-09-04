# ADR 0008 — Encoding RBI e-mandate constraints as policy rules

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 4 (Engine 2)

## Context

Engine 2's credibility rests entirely on one thing: that the compliance
constraints it claims to enforce are actually enforced, by something a reader can
find and argue with, rather than described in a README and approximated in code.

The constraints come from two very different places, and a demo that blurs them
is claiming regulatory authority for numbers somebody picked:

- **Regulation.** The `rbi-mandate-rules` skill's Part A: one-time AFA at
  registration (A1), a pre-debit notification 24–48h before every debit including
  every retry (A2), the ₹15,000 per-transaction threshold above which fresh AFA is
  required each cycle (A3), revocation as an absolute hard stop (A4), and missing
  AFA as its own failure mode (A5).
- **Product judgment.** Part B: three attempts per billing cycle, 24h minimum
  spacing, the 48h staleness ceiling, the mandate-cap check, and how a paused
  mandate is treated.

## Decision

**Every mandate constraint is a registered rule in `policy_engine.py`, evaluated
inside `evaluate()`, and cited by id on the audit entry it produced.** Nothing is
checked inside the engine.

Concretely, five new gate inputs live on `PolicyRequest` — `mandate_paused`,
`afa_registered`, `notice_lead_hours`, `mandate_cap_paise`,
`fresh_afa_completed` — and `_mandate_debit_preconditions()` turns each into a
denial citing the rule that refused. Ten rules were registered:

| Rule | Kind | What it refuses |
| --- | --- | --- |
| `rbi-mandate-rules:A1` | Regulatory | A debit against a mandate whose registration AFA never completed |
| `rbi-mandate-rules:A2` | Regulatory | A debit with no notice, or one sent under 24h ahead |
| `rbi-mandate-rules:A3` | Regulatory | A silent auto-debit above ₹15,000 without fresh AFA |
| `rbi-mandate-rules:A4` | Regulatory | Anything at all on a revoked or expired mandate |
| `rbi-mandate-rules:A5` | Regulatory | A retry of an `AFA_REQUIRED` decline with no authentication since |
| `rbi-mandate-rules:PartB.max_retries` | **Ours** | A fourth attempt in a billing cycle |
| `rbi-mandate-rules:PartB.min_spacing` | **Ours** | An attempt under 24h after the last one |
| `rbi-mandate-rules:PartB.notice_staleness` | **Ours** | A debit against a notice over 48h old |
| `rbi-mandate-rules:PartB.mandate_cap` | **Ours** | A debit above the amount authorised at registration |
| `rbi-mandate-rules:PartB.paused` | **Ours** | A debit against a paused mandate |

Part B previously had no rule ids at all. It now does, one per row, because a
bound that cannot be cited must not exist.

### The engine config displays regulation; it does not set it

`engines/mandate_recovery/config.json` carries the notice window, the attempt cap
and the AFA threshold — and `config.py` **raises at load** if any of them
disagrees with what `policy_engine.py` enforces. The failure this prevents is a
config file reading `min_lead_hours: 6` while the gate still enforces 24, so the
engine looks configurable and is not. Regulatory values are displayed here and
enforced there; Vasooli values are genuinely read from the file.

## Which timings are regulation and which are ours

This is the distinction the ADR exists to record.

**Regulatory, not ours to tune:** the 24–48h notification window, the ₹15,000
threshold, AFA at registration, revocation as absolute.

**Product judgment, argued in Part B or `policy-bounds`:**

- **Three attempts per cycle.** Below the taxonomy's SOFT budget of 4, because
  recurring debits are more constrained than one-off payments. Arbitrary in the
  sense that 2 or 4 would also be defensible; deliberate in that it is *bounded*
  and provable.
- **24h minimum spacing.** Derived from A2 rather than mandated by it: any
  shorter interval cannot satisfy the notification window for the retry itself.
  A regulation-shaped consequence of a regulation, but the number is ours.
- **The doubling backoff on top of that floor** (`policy-bounds:CD2`, 24h → 48h →
  72h capped). Purely a product decision. The regulation sets a floor, not a
  schedule.
- **36h as the target lead** for a notice the engine schedules itself. The
  midpoint of the window. Ours; the window is not.
- **48h staleness.** A2 states a 24–48h window. Treating "too early" as a
  *violation* rather than a curiosity is our reading, and it is the conservative
  one: a notice sent five days ahead is not the notice A2 asks for.

## Two places the regulation was ambiguous, and what we chose

Both are worth being able to speak to, so neither is buried.

**1. What may a revoked mandate be told?** A4 is absolute about debits and silent
about communication. The phase brief allows "a single permitted notice" urging
re-authorization. `policy-bounds:HS1` allows none — *no further action of any
kind, in any code path*. **We chose zero.** The stricter bound wins, because
doing less is always the safe direction of error and a revoked mandate is exactly
the case where a system that chases anyway is the embarrassing failure. This
costs real recovery: seven mandates in the sample book are revoked, and the
engine says nothing to any of them. `test_a_revoked_mandate_receives_no_customer_message_either`
pins the choice so it cannot drift back by accident.

**2. Where does the notice check sit relative to the attempt cap?** The skill
lists preconditions in order — active, notice, cap amount, AFA, budget, spacing —
and marks only revocation-first as load-bearing. We follow that order, so a
mandate whose notice is missing *and* whose budget is spent is refused citing A2
rather than the cap.

The alternative ordering is genuinely arguable: the cap answers "should we act at
all", which arguably precedes "may this specific debit fire". We kept the skill's
order because it produces the *more specific* citation — `AFA_REQUIRED` is
`POLICY_BLOCK` with a retry budget of 0, so a cap-first evaluation would refuse
every authentication failure citing a budget rule, and the trail would never say
that authentication was the problem.

The cost is visible in the measured run: four of the seven cap-reached mandates
are refused by an earlier gate, so only three report `ATTEMPT_BUDGET_EXHAUSTED`.
The compensating behaviour is `_would_be_permitted_with_notice()`: before sending
a compliance notice the scheduler checks whether the debit it announces could
ever fire, and skips the notice when it could not. Sending a pre-debit notice for
a debit the cap forbids would spend a customer contact on an event that will not
happen.

## Consequences

**Good.** Every compliance refusal in the trail names a specific clause. The
40 compliance-blocked attempts in the measured run break down across five
distinct rules rather than one generic "blocked". A judge can pick any refused
debit and read the rule.

**Good.** The gate cannot be bypassed by an engine that forgets to check. The
preconditions live on the request, so an engine that omits `notice_lead_hours`
gets a denial, not a pass — `None` fails closed exactly as `False` does.

**Costly.** A3 is strict, and the measured consequence is stark: 26 of 64
mandates sit above ₹15,000, carrying ₹6,34,000 of the ₹7,92,000 at risk, and not
one of them can be auto-debited. That is not an engine failure — it is the
regulation — but it makes a single blended recovery rate read as failure, which
is why the run reports an addressable denominator alongside it.

**Deferred.** `fresh_afa_completed` is always `False`: nothing in this build
performs a customer authentication, so nothing can set it true. The field exists
because the gate needs it and because a real integration would flip it. Stated
here rather than left to be discovered.
