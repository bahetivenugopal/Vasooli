# Architecture

> **Status: shared core (Phase 1), synthetic data foundry (Phase 2) and Engine 1
> (Phase 3) built.** Engines 2 and 3 and the dashboard do not exist yet —
> sections describing them are marked *not built* and say what they will consume.
> This file describes real code; it should never present an aspiration as though
> it shipped.

## The shape

```
Synthetic data generator (transactions, mandates, invoices)     [BUILT]
        │
        ├──► Root-cause engine ────┐                           [BUILT]
        ├──► Mandate engine ───────┤                           [not built]
        └──► Receivables engine ───┤                           [not built]
                                   │
                    ┌──────────────┼──────────────┐
                    │         Shared core         │            [BUILT]
                    │  policy engine · LLM agent  │
                    │       · audit trail         │
                    └──────────────┬──────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
        Razorpay test-mode API [BUILT]  Control tower dashboard [not built]
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

---

## The shared core — `apps/api/app/services/`

| Module | Responsibility | Status |
| --- | --- | --- |
| `policy_engine.py` | Is this allowed? Which rule says so? | Built |
| `audit_trail.py` | The single append-only decision record | Built |
| `llm_agent.py` | Gemini behind a thin provider interface | Built |
| `razorpay_client.py` | Every Razorpay call, and error normalization | Built |
| `decline_taxonomy.py` | The soft/hard/ambiguous lookup table as code | Built |
| `reasoning_tasks.py` | Shared reasoning tasks and their fallbacks | Built |
| `prompts/` | Versioned prompt files | Built |

### `policy_engine.py` — permission

The interface every engine consumes:

```python
decision = PolicyEngine().evaluate(
    PolicyRequest(
        engine=Engine.MANDATE_RECOVERY,
        entity=RecoverableEntity(...),      # id, amount_paise, attempt_count, terminal flag
        proposed_action=Action.ATTEMPT_CHARGE,
        now=datetime.now(UTC),
        decline_code=DeclineCode.INSUFFICIENT_FUNDS,
        is_outreach=False,                  # QH1 applies only to things a human receives
        messages_sent_in_window=0,
    )
)
# -> PolicyDecision(allowed, action, rule_id, reason_code, rationale, outcome,
#                   attempts_remaining, earliest_next_attempt_at,
#                   requires_human, terminal, permitted_actions)
```

`PolicyDecision.audit_fields()` maps straight onto `audit_trail.record()`. That
is deliberate: a decision the trail cannot record is a decision nobody can check.

**Evaluation order is load-bearing**, and hard stops short-circuit everything:

1. Unevaluable precondition → block (fail closed)
2. Terminal entity → halt *(policy-bounds HS1, rbi-mandate-rules A4)*
3. Terminal decline class → halt, schedule cancelled *(HS2, A4)*
4. Abstention → escalate to a human *(HE1)*
5. High-value hard failure → escalate *(HE3)*
6. Attempt cap → the strictest of ceiling, engine cap and class budget *(AC1/AC2, Part B, taxonomy budgets)*
7. Cooldown / escalating backoff *(CD1/CD2, Part B spacing)*
8. Quiet hours and contact cap, outreach only *(QH1, QH2)*
9. Otherwise permitted, inside a stated `permitted_actions` envelope

Every rule is declared in `RULE_REGISTRY` with an id, a description and a
citation; `_deny()` and `_permit()` refuse an unregistered id, so a decision
cannot cite authority it does not have. `GET /api/v1/audit/rules` exposes the
whole registry.

Rule sources are `decline-taxonomy`, `rbi-mandate-rules`, and — for the
engine-agnostic bounds neither of those covers — the **`policy-bounds` skill**,
added in this phase to keep the "no invented thresholds" rule true for quiet
hours, backoff, the hard attempt ceiling and the escalation triggers.

### Judgment vs permission

- **`llm_agent.py` proposes.** It diagnoses, classifies, drafts.
- **`policy_engine.py` disposes.** It authorises, or refuses.

`PolicyEngine.authorize(request, recommendation)` computes the envelope *before*
reading the recommendation, returns a denial as a denial, and has no access to
the private `_permit()` that is the only constructor of an allowed decision. A
recommendation can narrow — a different permitted action, a *later* retry — and
can never widen. Full reasoning in **ADR 0003**.

### `audit_trail.py` — the record

```python
trail = AuditTrail(db)
trail.start_batch(batch_id=..., engine=..., seed=...)
trail.record(batch_id, engine, entity_type, entity_id, action, outcome,
             reason_code, authorising_rule, rationale, provenance,
             amount_at_risk_paise, amount_recovered_paise, model_confidence, metadata)
trail.resolve_outcome(entry_id, outcome=..., amount_recovered_paise=...)
trail.query(...) / trail.entity_timeline(...) / trail.batch_summary(batch_id)
```

Guarantees the module enforces rather than documents:

- **No entry without a citation.** `authorising_rule` must be a
  `<skill>:<rule-id>` citation, `reason_code` and `rationale` must be present,
  and a `source: model` entry must carry `model_confidence`. Violations raise.
- **Append-only.** There is no `delete` and no generic `update` on the service.
  `resolve_outcome()` is the one mutation and it only moves an entry off
  `pending`; a resolved entry stays resolved, and a correction is a new entry.
- **The summary computes from the log**, never from a parallel counter, and
  reports **per provenance source**. Zero at risk reports a 0% rate, not 100%.
- **`policy_violations`** counts stored rows that break the schema's own rules,
  so a row that arrived by some other route is shown rather than summarised over.

Schema detail lives in the `audit-schema` skill, which this phase updated to
match what shipped.

### `llm_agent.py` — judgment

```python
result = LLMAgent().run("classify_unknown_decline", context)
# -> ReasoningResult(task, output, provenance, confidence, degraded, degradation_reason)
```

- **Structured output validated with Pydantic.** Unparseable output is retried
  once with the error fed back, then falls back.
- **Disk cache** keyed on resolved prompt + model + prompt version. A cache hit
  is still `source: model` with `cache_hit: true`.
- **429 backoff** at 1s / 2s / 4s, then fallback. Never indefinite.
- **Every task registers a deterministic fallback**, verified at app startup by
  `REGISTRY.verify_all_have_fallbacks()` in the FastAPI lifespan. Registration
  also loads the prompt file, so a missing prompt fails at boot rather than
  mid-batch.
- **`run()` never raises for a provider problem.** Every path out returns a
  result with provenance attached.

The one task the shared core owns is `classify_unknown_decline`, because
`decline-taxonomy` routes to it from three places. Its fallback treats an
unclassifiable decline as **HARD** — the taxonomy's fail-safe, since defaulting
to soft means defaulting to "keep charging this customer". A model answer below
0.6 confidence takes the same path.

Engine-specific tasks (root-cause diagnosis, dunning drafting, promise
extraction) register in their own phases, with the fallbacks ADR 0002 specifies.

### Provenance

Every reasoning result carries `source` (`model` | `deterministic`), `provider`,
`model`, `cache_hit`, `prompt_version`, `abstained`, `latency_ms`, `tokens`. The
`Provenance` model is frozen and validates the contract: a deterministic result
cannot report a model, a prompt version, latency or tokens, so a ruled result can
never be dressed up as a reasoned one.

**Metrics are reported per source, never blended.**

### `razorpay_client.py` — the boundary

Two modes, chosen by `RAZORPAY_MODE` and recorded on every audit entry:
`simulated` (deterministic, fixture-driven, the default) and `test` (real calls
against Razorpay test mode). A non-`rzp_test_` key fails at construction; a
`rzp_live_` key fails earlier, in settings validation. Full reasoning in
**ADR 0004**.

Every method returns a `RazorpayResult` and never raises for an API-level
problem. Failures become a `RazorpayFailure` carrying both the raw upstream
reason and its normalized `decline-taxonomy` code.

## Error normalization — the one-way boundary

Razorpay's `error.reason` (`insufficient_fund`, `card_declined`, …) becomes a
`decline-taxonomy` code (`INSUFFICIENT_FUNDS`, `DO_NOT_HONOUR`, …) exactly once,
in `razorpay_client.normalize_reason()`. Engines branch only on normalized codes.

Unmapped reasons become `UNKNOWN` and take the fail-safe path — classified by the
model, treated as **hard** if confidence is low or the provider is unavailable.
The safe direction of error is "stop", never "keep retrying".

## Data flow, one entity

```
synthetic batch (seeded, batch_id)
   → engine ingest
   → engine asks policy_engine: is this action allowed?
        ├─ needs judgment? → llm_agent (or its registered fallback)
        │                    → policy_engine.authorize(request, recommendation)
        └─ PolicyDecision: allowed · action · reason_code · authorising_rule ·
                           attempts_remaining · rationale · permitted_actions
   → audit_trail.record(...)          # BEFORE any external side effect
   → engine calls a bounded action from its own actions.py
        └─ razorpay_client → simulated fixtures, or the test-mode API
   → audit_trail.resolve_outcome(...) once the result is known
```

Two properties worth noting:

- **The audit entry is written before the side effect.** An action without a
  record is unprovable.
- **The recovery summary is computed from the audit trail**, not a parallel
  counter — so the headline number and the audit trail cannot disagree.

`scripts/core_loop_demo.py` runs exactly this loop and prints each step.

## Engine 1 — Root-Cause Recovery · `apps/api/app/engines/root_cause/`

The first engine, and the one that most justifies the word "agent": *is this one
customer's problem, or is an entire corridor down?* The same decline code appears
in both cases and demands opposite responses.

| Module | Responsibility |
| --- | --- |
| `config.py` / `config.json` | Corridor levels and thresholds, each citing a `corridor-detection` rule |
| `dataset.py` | Reads the payments `.jsonl`. Refuses a manifest path outright |
| `detection.py` | Corridor segmentation and statistical degradation detection. **No model** |
| `diagnosis.py` | The reasoning task, its prompt, and its deterministic fallback |
| `recovery.py` | The four bounded actions, each authorised before it happens |
| `scoring.py` | Detection accuracy against ground truth. **The only module that reads a manifest** |
| `runner.py` | The batch loop and the honest summary |

### The data flow

```
payments .jsonl (records only — never the manifest)
   │
   ├─ detection.py ── segment by (issuer × method) and (method × route)
   │                  rolling 6h window, 2h stride, corridor's OWN prior baseline
   │                  minimum volume 6 in-window / 20 baseline   ← the FP guard
   │                  exact one-sided binomial test, p <= 0.05
   │                  → Detection(rates, p_value, decline mix, value at risk)
   │
   ├─ diagnosis.py ── llm_agent.run("diagnose_corridor", context)
   │                  context = corridor stats + decline distribution +
   │                            peer corridors over the same window
   │                  → hypothesis · systemic|individual|insufficient_evidence ·
   │                    recommended action (enumerated) · reasoning · confidence
   │                  ↳ fallback: classify from the decline distribution alone,
   │                    bias to escalation. Audited `source: deterministic`
   │
   ├─ recovery.py ─── policy_engine.authorize(request, recommendation)
   │                  corridor-detection:RR2  systemic determination required
   │                  corridor-detection:RR3  an alternate route must exist
   │                  corridor-detection:ES1  value at risk under the ceiling
   │                  policy-bounds:HE1       abstention → human review
   │                  → reroute (expiring, RR1) · escalate · no corridor action
   │
   ├─ recovery.py ─── every failed payment, independently:
   │                  policy_engine.evaluate() against its decline class
   │                  → retry (sampled from the documented retry model,
   │                           executed through razorpay_client) · suppressed ·
   │                    deferred on cooldown
   │
   └─ runner.py ───── summary recomputed from `audit_entries`, plus
                      scoring.py's precision / recall / latency vs ground truth
```

### Two separations that carry the weight

**Detection never sees ground truth.** `dataset.py` refuses a manifest path;
`scoring.py` is the only module that opens one, and it runs after detection is
finished. Asserted by parsing the engine's imports, not by convention. Without
this the reported precision and recall would be statements about nothing. See
ADR 0007.

**Detection never uses a model; diagnosis always does.** Reproducible where
reproducibility is the point, reasoned where judgment is the point. See ADR 0006.

### Bounds this engine adds

All named in the `corridor-detection` skill and registered in `policy_engine.py`:

| Rule | Bound |
| --- | --- |
| `corridor-detection:MV1` / `MV2` | Minimum window (6) and baseline (20) volume before anything is flagged |
| `corridor-detection:ST1` | Absolute ≥ 15pt **and** relative ≥ 35% **and** p ≤ 0.05 |
| `corridor-detection:DX1` | Diagnosis confidence floor 0.6, below which the fallback applies |
| `corridor-detection:RR1` | A reroute expires. Maximum 6 hours, clamped, never open-ended |
| `corridor-detection:RR2` | A reroute requires a systemic determination |
| `corridor-detection:RR3` | A reroute needs an alternate route to exist, or it fails closed |
| `corridor-detection:ES1` | Corridor actions above ₹2,00,000 of value at risk go to a human |

Measured results, including the false positive, are in
[`docs/metrics/engine-1-root-cause.md`](metrics/engine-1-root-cause.md).

## API surface

```
GET /api/v1/health
GET /api/v1/audit/entries                          filters: batch_id, engine,
                                                   entity_type, entity_id,
                                                   action, outcome, source
GET /api/v1/audit/entries/{id}                     full reasoning + provenance
GET /api/v1/audit/batches                          every run, with its seed
GET /api/v1/audit/batches/{batch_id}/summary       headline metrics, per source
GET /api/v1/audit/entities/{type}/{id}/timeline    the decision trail
GET /api/v1/audit/rules                            the whole policy registry

POST /api/v1/root-cause/runs                       run the loop over a batch
GET  /api/v1/root-cause/runs                       every run, with its seed
GET  /api/v1/root-cause/runs/{batch_id}            one run's record
GET  /api/v1/root-cause/runs/{id}/detections       filters: determination, allowed
GET  /api/v1/root-cause/detections/{detection_id}  full diagnosis + reasoning
GET  /api/v1/root-cause/runs/{id}/actions          every action, with its rule
GET  /api/v1/root-cause/runs/{id}/reroutes         filter: active_at
GET  /api/v1/root-cause/config                     thresholds, each citing a rule
```

The audit routes stay read-only. `POST /root-cause/runs` writes, but only by
delegating the whole loop to `RootCauseRunner` — there is no route that can
produce an audit entry without a decision behind it.

Read-only by design: the trail is written by services and nothing else. Engine
routers mount onto `api_router` as each phase lands. OpenAPI docs at `/docs`.

## Persistence

SQLite via SQLAlchemy 2.x, deliberately zero-setup. `DATABASE_URL` is the only
thing standing between this and Postgres; that swap is explicitly **not** part of
this build.

Two invariants hold everywhere:

- **Money is integer paise.** Never a float, in any layer, including the frontend.
- **Timestamps are tz-aware UTC.** `db/types.py::UTCDateTime` enforces this at
  the column: naive datetimes are rejected on write rather than silently assumed
  to be UTC, and everything read back is tz-aware.

Tables: `audit_entries`, `batch_runs`, `corridor_detections`, `corridor_reroutes`.
The last two hold Engine 1's artefacts and are **not** metric sources: every
reported figure is recomputed from `audit_entries`, so a stale row in either can
never become a number anyone quotes. `RecoverableEntityMixin` carries the
fields all three engines share (identifier, amount in paise, currency, status,
attempt count, last-attempt timestamp, terminal flag) for the engine-specific
tables that arrive in later phases.

## Frontend

*Not built.* Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui, Recharts.

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
- No custom agent framework over the provider SDK
- No provider registry or plugin machinery — one interface, one implementation
- No Postgres migration
- No checkout drop-off recovery (out of scope)
- Hinglish voice only as an optional bonus channel on Engine 2's notification
  step, and only once all three engines are solid and demo-ready

All of these would be premature complexity with no payoff for judges in a ~30-hour
build. Reversing any of them requires an ADR in [`adr/`](adr/).
