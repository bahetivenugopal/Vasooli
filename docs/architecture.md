# Architecture

> **Status: shared core (Phase 1), synthetic data foundry (Phase 2), and all
> three engines (Phases 3–5) built.** The dashboard does not exist yet — the
> section describing it is marked *not built* and says what it will consume.
> This file describes real code; it should never present an aspiration as though
> it shipped.

## The shape

```
Synthetic data generator (transactions, mandates, invoices)     [BUILT]
        │
        ├──► Root-cause engine ────┐                           [BUILT]
        ├──► Mandate engine ───────┤                           [BUILT]
        └──► Receivables engine ───┤                           [BUILT]
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
| `tone.py` | TN1's forbidden-language scan, shared by every engine that writes to a person | Built |
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
6. Corridor reroute preconditions, Engine 1 only *(RR2, ES1, RR3)*
7. Mandate debit preconditions, Engine 2 only *(PartB.paused, A2, PartB.notice_staleness, PartB.mandate_cap, A1, A5, A3)*
8. Attempt cap → the strictest of ceiling, engine cap and class budget *(AC1/AC2, Part B, taxonomy budgets)*
9. Cooldown / escalating backoff *(CD1/CD2, Part B spacing)*
10. Quiet hours and contact cap, outreach only *(QH1, QH2)*
11. Otherwise permitted, inside a stated `permitted_actions` envelope

Steps 6 and 7 sit **above** the attempt cap deliberately. `AFA_REQUIRED` is
`POLICY_BLOCK` with a retry budget of 0, so a cap-first evaluation would refuse
every authentication failure citing a budget rule and the trail would never say
that authentication was the problem. ADR 0008 argues the ordering and names its
cost.

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

Engine 1 registers `diagnose_corridor`; Engine 2 registers
`draft_dunning_message`, whose fallback is a per-failure-class template rather
than an abstention — a message is one of the few places where the rule-based
answer is nearly as good as the reasoned one, and abstaining would cost a real
recovery to avoid a small quality loss. Engine 3 registers `understand_reply`,
whose fallback **abstains** — no regex, no keyword heuristic, no partial guess.
That asymmetry is the whole design: degrade to boring where boring is safe,
degrade to abstention where being wrong is expensive. A fabricated promise-to-pay
would suppress a legitimate chase and corrupt the promise register, so refusing
is the correct answer and the trail records it as a deliberate escalation.

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

## Engine 2 — Mandate & Subscription Recovery · `apps/api/app/engines/mandate_recovery/`

The engine where domain correctness matters more than cleverness. Anyone can
write a retry loop; this one respects the ₹15,000 AFA threshold, the 24–48h
pre-debit notification window, and an immediate hard stop on revocation.

| Module | Responsibility |
| --- | --- |
| `config.py` / `config.json` | Compliance windows and bounds, each saying whether it is regulatory or ours. Raises if it disagrees with the gate |
| `dataset.py` | Reads the mandates `.jsonl`. Refuses a manifest path outright |
| `classification.py` | Routes a recurring failure. Reuses the shared taxonomy; adds only what recurrence introduces |
| `scheduler.py` | The compliance-aware retry scheduler. **No model** |
| `dunning.py` | The drafting task, the tone gate, and the per-failure-class templates |
| `recovery.py` | The four bounded actions, each authorised before it happens |
| `runner.py` | The batch loop and the honest summary |

### The data flow

```
mandates .jsonl (records only — never the manifest)
   │
   ├─ classification.py ── shared decline_taxonomy.classify(), plus the routing a
   │                       mandate needs and a one-off payment does not:
   │                         AFA_REQUIRED             -> authentication (A5)
   │                         PRE_DEBIT_NOTICE_MISSING -> compliance     (A2)
   │                         MANDATE_REVOKED/EXPIRED  -> terminal       (A4)
   │                       an unrecognised code -> the shared
   │                       classify_unknown_decline task, else HARD
   │
   ├─ scheduler.py ─────── proposes a debit at the moment it would actually fire,
   │                       and asks policy_engine.evaluate() about *that* moment
   │                         A2 / staleness -> notify, reschedule beyond the window
   │                         A1 / A3 / A5   -> customer authentication
   │                         PartB.paused   -> block, schedule suspended
   │                         cap / spacing  -> dunning, or defer
   │                       → ScheduleExplanation: what next, when, which rule
   │
   ├─ dunning.py ───────── llm_agent.run("draft_dunning_message", context)
   │                       → channel · subject · body · call to action ·
   │                         recommended action (enumerated) · reasoning
   │                       ↳ policy-bounds TN1/TN2 validated against the text
   │                       ↳ fallback: per-failure-class template, slot-filled
   │
   ├─ recovery.py ──────── policy_engine.authorize(request, recommendation)
   │                       outreach is bounded by QH1/QH2, never by the debit
   │                       budget — see `scheduler.outreach_entity`
   │                       → attempt_charge · send_pre_debit_notice ·
   │                         send_dunning · escalate, each audited
   │
   └─ runner.py ────────── summary recomputed from `audit_entries`, reported by
                           failure class, by AFA branch, and against two stated
                           denominators
```

### Three things that carry the weight

**The proposed debit time is what gets evaluated**, not the wall clock. A retry
is a scheduled future debit, so the notice window, the 24h Part B spacing and the
escalating backoff are all measured against the moment the debit would fire.

**A message is not a charge.** `outreach_entity()` presents the mandate to the
gate with the *message* count as its attempt count, so a customer message is
bounded by QH1/QH2 and not by the debit budget. Without it the dunning path would
be silent on exactly the mandates that most need it: a hard decline has spent its
debit budget by definition.

**Nothing is dispatched.** Communications are drafted, tone-validated,
policy-gated, persisted and rendered. `MandateCommunication` has no recipient
column and its `status` vocabulary has no `sent` value. See ADR 0009.

### Bounds this engine adds

All named in `rbi-mandate-rules` or `policy-bounds` and registered in
`policy_engine.py`:

| Rule | Bound | Kind |
| --- | --- | --- |
| `rbi-mandate-rules:A1` | AFA at registration, before any debit | Regulatory |
| `rbi-mandate-rules:A2` | Pre-debit notice 24–48h ahead of every debit | Regulatory |
| `rbi-mandate-rules:A3` | Above ₹15,000, fresh AFA each cycle | Regulatory |
| `rbi-mandate-rules:A5` | An `AFA_REQUIRED` decline never silent-retries | Regulatory |
| `rbi-mandate-rules:PartB.notice_staleness` | A notice over 48h old is stale | Ours |
| `rbi-mandate-rules:PartB.mandate_cap` | No debit above the registered cap | Ours |
| `rbi-mandate-rules:PartB.paused` | A paused mandate blocks; it does not terminate | Ours |
| `policy-bounds:TN1` / `TN2` / `TN3` | No threats or invented urgency; the remedy must fit the failure; a failing draft is replaced by the template | Ours |

ADR 0008 records how the constraints are encoded and which timings are regulation
rather than product judgment. Measured results, including what underperformed,
are in [`docs/metrics/engine-2-mandate-recovery.md`](metrics/engine-2-mandate-recovery.md).


## Engine 3 — Receivables Chaser & Promise-to-Pay Tracker · `apps/api/app/engines/receivables/`

The engine with the narrowest claim and the widest reasoning surface: *the system
does not chase people who are cooperating; it chases the ones who committed and
did not follow through.* Everything here exists to make that sentence checkable.

| Module | Responsibility |
| --- | --- |
| `config.py` / `config.json` | The ladder, the caps, the weights. Raises if it disagrees with the gate |
| `dataset.py` | Reads the invoices `.jsonl`. Refuses a manifest path outright |
| `prioritization.py` | The deterministic weighted worklist. **No model** |
| `understanding.py` | The reply-reading task, its coherence guard, and its **abstaining** fallback |
| `promises.py` | Kept / broken / active / superseded, each an explicit check with a citation |
| `ladder.py` | The bounded chase ladder and the gate every rung passes through |
| `reminders.py` | Per-rung templates, tone-validated against the same TN rules |
| `recovery.py` | The four bounded actions, each authorised before it happens |
| `scoring.py` | Extraction accuracy vs ground truth. The **only** module here that opens a manifest |
| `runner.py` | The batch loop and the honest summary |

### The data flow

```
invoices .jsonl (records only — never the manifest)
   │
   ├─ understanding.py ─── llm_agent.run("understand_reply", context)
   │                       → intent · promise? · committed date (absolute) ·
   │                         conditionality · dispute detail · language ·
   │                         recommended action (enumerated) · reasoning
   │                       ↳ the reply's OWN timestamp is in the prompt, so
   │                         "end of the month" resolves against the reply and
   │                         not against run time
   │                       ↳ a self-contradictory reading abstains rather than
   │                         being reconciled
   │                       ↳ fallback: ABSTAIN. No regex, no keyword heuristic,
   │                         no partial guess — a fabricated promise is worse
   │                         than no promise (llm-provider, ADR 0011)
   │
   ├─ promises.py ──────── an explicit check per commitment, every run:
   │                         paid inside date + 48h grace  -> kept    (PP1)
   │                         date + grace passed, unpaid   -> broken  (PP1)
   │                         conditional                   -> never broken (PP2)
   │                         no date                       -> 7-day horizon (PP3)
   │                         revised by a later reply      -> superseded (PP2)
   │                       → per-customer reliability, over resolved promises only
   │
   ├─ prioritization.py ── a transparent weighted sum, weights declared in config
   │                       and shown per invoice (PR1):
   │                         outstanding value · days overdue ·
   │                         customer unreliability · contact headroom
   │                       ↳ a live promise demotes the invoice (PR2) — it stays
   │                         on the worklist with its reason visible
   │
   ├─ ladder.py ────────── escalation evidence first (RL5), then the gate:
   │                         broken promise / ladder exhausted -> escalate
   │                         dispute       -> freeze, human review (RL4)
   │                         abstention    -> human review (HE1)
   │                         otherwise     -> the next rung, if one is left
   │                       policy_engine.authorize() on the REMINDER path only —
   │                       a broken promise is a fact the model never saw
   │
   ├─ recovery.py ──────── → classify_decline (the reading) ·
   │                         record_promise_to_pay · send_reminder · escalate
   │                       ↳ a Razorpay test-mode payment link per chased invoice;
   │                         a failure degrades the message, never the chase
   │
   └─ runner.py ────────── read replies → track promises → rank → chase, in rank
                           order, so the RL3 customer cap is spent on the most
                           important invoice first. Summary recomputed from
                           `audit_entries`; extraction score scored against the
                           manifest and persisted on the batch record
```

### Three things that carry the weight

**The fallback abstains, and that asymmetry is deliberate.** Engine 2's dunning
degrades to a template because a plain accurate message costs almost nothing.
Engine 3 refuses, because a fabricated promise would suppress a legitimate chase
*and then* escalate the customer for breaking a commitment they never made.
Degrade to boring where boring is safe; degrade to abstention where being wrong
is expensive.

**Escalation is gated on evidence, never on a timer.** `EscalationTrigger` is a
typed value and a receivables `escalate` carrying none is refused by the policy
engine. A live promise outranks an exhausted ladder: a customer who replied with
a credible commitment is not escalated merely because they had already had three
reminders. ADR 0010 argues the trade-offs.

**Accuracy is measured, not asserted.** The reply annotations live only in the
generator's manifest, `resolve_dataset()` raises when handed one, and `scoring.py`
is the single module permitted to open it. Every rate is over model-handled
replies with abstentions reported separately. ADR 0011 explains why, and
[`docs/metrics/engine-3-receivables.md`](metrics/engine-3-receivables.md) reports
the numbers — including why a perfect score on this corpus is a red flag.

### Bounds this engine adds

All named in `policy-bounds` and registered in `policy_engine.py`. None is
regulatory — this ladder is entirely product judgment, which is exactly why every
number has to be citable.

| Rule | Bound |
| --- | --- |
| `policy-bounds:RL1` | Three rungs — gentle reminder, firm follow-up, formal notice — then a human |
| `policy-bounds:RL2` | 72 hours minimum between rungs on one invoice |
| `policy-bounds:RL3` | **4 messages per customer per rolling 7 days, across every invoice they owe** |
| `policy-bounds:RL4` | A disputed invoice is never chased again automatically |
| `policy-bounds:RL5` | The ladder ascends on evidence — a broken promise or exhaustion — never on elapsed time |
| `policy-bounds:PP1` | A promise is broken only 48h past its committed date |
| `policy-bounds:PP2` | A conditional promise is never scored against a date it did not give |
| `policy-bounds:PP3` | A promise with no date deprioritises its invoice for 7 days, then the ladder resumes |
| `policy-bounds:PR1` | The worklist is deterministic and its weights are declared |
| `policy-bounds:PR2` | An invoice with a live, unbroken promise is deprioritised, not chased |

RL3 is the one that is easy to miss and the one that actually matters: QH2 bounds
the *conversation* about one invoice, RL3 bounds what one *recipient* hears in a
week. A customer with eight overdue invoices does not get eight messages.

ADR 0010 records why escalation is gated on broken promises rather than elapsed
time, and ADR 0011 how the extraction measurement is built.


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

POST /api/v1/mandate-recovery/runs                 run the loop over a book
GET  /api/v1/mandate-recovery/runs                 every run, with its seed
GET  /api/v1/mandate-recovery/runs/{batch}         one run's record
GET  /api/v1/mandate-recovery/runs/{batch}/mandates
                                                   filters: route, next_step,
                                                   afa_side, compliance_blocked
GET  /api/v1/mandate-recovery/runs/{batch}/mandates/{mandate_id}
                                                   the full timeline — every
                                                   decision, rule and message
GET  /api/v1/mandate-recovery/runs/{batch}/communications
                                                   filters: status, kind
GET  /api/v1/mandate-recovery/runs/{batch}/actions every action, with its rule
GET  /api/v1/mandate-recovery/config               windows, each citing a rule

POST /api/v1/receivables/runs                      run the loop over a ledger
GET  /api/v1/receivables/runs                      every run, with its seed
GET  /api/v1/receivables/runs/{batch}              one run's record
GET  /api/v1/receivables/runs/{batch}/extraction   confusion matrix vs ground
                                                   truth, with the abstention
                                                   rate reported separately
GET  /api/v1/receivables/runs/{batch}/worklist     ranked, with every score
                                                   breakdown. Filters:
                                                   deprioritised, ageing_bucket,
                                                   next_step
GET  /api/v1/receivables/runs/{batch}/promises     the register. Filters: status,
                                                   conditional, customer_id
GET  /api/v1/receivables/runs/{batch}/invoices/{invoice_id}
                                                   the full story — contacts, the
                                                   reply verbatim, what was read
                                                   from it, the promise, and the
                                                   rule behind every decision
GET  /api/v1/receivables/runs/{batch}/communications
                                                   filters: status, rung
GET  /api/v1/receivables/runs/{batch}/actions      every action, with its rule
GET  /api/v1/receivables/config                    ladder, caps and weights, each
                                                   citing a rule
```

The audit routes stay read-only. The three `POST .../runs` routes write, but only
by delegating the whole loop to their runner — there is no route that can produce
an audit entry without a decision behind it.

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

Tables: `audit_entries`, `batch_runs`, `corridor_detections`, `corridor_reroutes`,
`mandate_recovery_states`, `mandate_communications`, `invoice_chase_states`,
`promises_to_pay`, `invoice_communications`. Everything after `batch_runs` holds
an engine's artefacts and is **not** a metric source: every reported figure is
recomputed from `audit_entries`, so a stale row in any of them can never become a
number anyone quotes. The one exception is Engine 3's extraction score, which
needs the generator's manifest and so is persisted on the batch record's `notes`
rather than recomputed — nothing serving an API request should reach for a
manifest.

The per-run tables are keyed by `(batch_id, …)` rather than by entity id, because
one dataset feeds many runs and a table that could hold only one run's view of an
entity would overwrite the previous run's evidence. `RecoverableEntityMixin`
carries the fields all three engines share (identifier, amount in paise, currency,
status, attempt count, last-attempt timestamp, terminal flag), but the per-run
snapshot tables deliberately do not use it — its `entity_id` is unique, which is
right for a live entity table and wrong for a snapshot.

`promises_to_pay` is the one place in the project where a **model-derived fact
becomes durable state**, which is why it carries `provenance` and
`model_confidence` as columns rather than only in the trail. An abstention never
reaches it.

## Frontend

*Not built.* Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui, Recharts.

- `src/components/ui/` — shadcn primitives, the **one** source of truth, managed
  by the shadcn CLI. Never duplicated.
- `src/components/features/` — Vasooli-specific composed components.

The dashboard's job is to make the audit trail legible: the headline recovery
number *with its batch id and seed*, all three engines in one view, the decision
trail for any entity, and blocked/halted actions shown as prominently as
successes — they are the compliance evidence. Engine 3 adds two surfaces worth
rendering directly: the ranked worklist with each invoice's score breakdown, and
the promise register with its statuses.

## Deliberately not doing

- No Kubernetes, no Docker Compose
- No splitting engines into microservices — one FastAPI app, separated by folder
- No custom agent framework over the provider SDK
- No provider registry or plugin machinery — one interface, one implementation
- No Postgres migration
- No checkout drop-off recovery (out of scope)
- Hinglish voice only as an optional bonus channel on Engine 2's notification
  step, and only once all three engines are solid and demo-ready. Note that
  Engine 3 *reads* Hinglish and only ever writes English; `services/tone.py`'s
  forbidden-language patterns are English-only, so a vernacular outreach channel
  would need that list extended before it shipped

All of these would be premature complexity with no payoff for judges in a ~30-hour
build. Reversing any of them requires an ADR in [`adr/`](adr/).
