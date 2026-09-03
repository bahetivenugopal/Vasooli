# Architecture

> **Status: scaffolding.** This describes the intended structure and the
> constraints it must satisfy. Components marked *not built* do not exist yet.
> Sections get rewritten to describe real code as each phase lands — this file
> should never describe an aspiration as though it shipped.

## The shape

```
Synthetic data generator (transactions, mandates, invoices)
        │
        ├──► Root-cause engine ────┐
        ├──► Mandate engine ───────┤
        └──► Receivables engine ───┤
                                   │
                    ┌──────────────┼──────────────┐
                    │         Shared core         │
                    │  policy engine · Claude agent│
                    │       · audit trail          │
                    └──────────────┬──────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
        Razorpay test-mode API          Control tower dashboard
```

## The central constraint

One synthetic data layer feeds all three engines, and all three route every
decision through the same shared core.

An engine's own code is almost entirely about two things:

1. **What data it ingests**
2. **What recovery actions it is allowed to call**

The judgment — *is this retryable, what's the root cause, what's the next bounded
action, log it* — always routes through `app/services/`. An engine that
reimplements decision logic is a bug, not a variation.

This is what makes the pitch claim true rather than rhetorical: there genuinely
is **one** policy layer and **one** audit trail under payment failures,
subscriptions, and receivables.

## The shared core — `apps/api/app/services/`

*Not built yet. Each lands with its phase.*

| Module | Responsibility | Rule |
| --- | --- | --- |
| `policy_engine.py` | Is this allowed? Is it retryable? What's next? Has a stop fired? | The **only** thing permitted to answer those. Every branch cites a named rule from a skill |
| `claude_agent.py` | Genuine LLM judgment — root-cause diagnosis, ambiguous decline classification, message drafting, promise-to-pay extraction | Supplies judgment, never permission |
| `audit_trail.py` | The single append-only decision record | Every entry cites the rule that authorised it, refusals included |
| `razorpay_client.py` | Every Razorpay call, and error normalization | Engines never see a raw upstream string |

### Judgment vs permission

The split that makes this a bounded recovery system rather than "an agent that
tries stuff":

- **`claude_agent.py` proposes.** It diagnoses, classifies, drafts.
- **`policy_engine.py` disposes.** It authorises, or refuses.

A model response can never widen a bound. If the model recommends a fifth retry
where the budget is four, the answer is no — and the refusal is audited, citing
the rule that produced it. `decision_source` on every audit entry records which
of the two made the call, so the split is visible rather than asserted.

## Error normalization — the one-way boundary

Razorpay's `error.reason` (`insufficient_fund`, `card_declined`, …) is normalized
into a `decline-taxonomy` code (`INSUFFICIENT_FUNDS`, `DO_NOT_HONOUR`, …) exactly
once, inside `razorpay_client.py`.

Engines branch only on normalized codes. Unmapped reasons become `UNKNOWN` and
take the fail-safe path — classified by the model, and treated as **hard** if
confidence is low. The safe direction of error is "stop", never "keep retrying".

The verified mapping table lives in the `decline-taxonomy` skill and is checked
against Razorpay's error-scenario test cards, so each row is reproducible with a
specific card.

## Data flow, one entity

```
synthetic batch (seeded, batch_id)
   → engine ingest
   → engine asks policy_engine: is this action allowed?
        ├─ needs judgment? → claude_agent → back through policy_engine
        └─ decision object: action · reason_code · authorising_rule ·
                            attempts_remaining · rationale
   → audit_trail.write(...)          # BEFORE any external side effect
   → engine calls a bounded action from its own actions.py
        └─ razorpay_client → Razorpay test-mode API
   → audit_trail.write(outcome)
```

Two properties worth noting:

- **The audit entry is written before the side effect.** An action without a
  record is unprovable.
- **The recovery summary is computed from the audit trail**, not a parallel
  counter — so the headline number and the audit trail cannot disagree.

## Persistence

SQLite via SQLAlchemy 2.x, deliberately zero-setup. `DATABASE_URL` is the only
thing standing between this and Postgres; that swap is explicitly **not** part of
this build.

Two invariants hold everywhere:

- **Money is integer paise.** Never a float, in any layer, including the frontend.
- **Timestamps are tz-aware UTC.** Converted only at display.

The audit table is **append-only**. Entries are never updated or deleted; a
correction is a new entry.

## Frontend

Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui, Recharts.

- `src/components/ui/` — shadcn primitives, the **one** source of truth, managed
  by the shadcn CLI. Never duplicated.
- `src/components/features/` — Vasooli-specific composed components.

The dashboard's job is to make the audit trail legible: the headline recovery
number *with its batch id and seed*, all three engines in one view, the decision
trail for any entity, and blocked/halted actions shown as prominently as
successes — they are the compliance evidence.

## Deliberately not doing

- No Kubernetes, no Docker Compose
- No splitting engines into microservices — one FastAPI app, separated by folder
- No custom agent framework over the Anthropic API
- No Postgres migration
- No checkout drop-off recovery (out of scope)
- Hinglish voice only as an optional bonus channel on Engine 2's notification
  step, and only once all three engines are solid and demo-ready

All of these would be premature complexity with no payoff for judges in a ~30-hour
build. Reversing any of them requires an ADR in [`adr/`](adr/).
