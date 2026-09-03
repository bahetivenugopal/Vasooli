# Phase implementation log

Running notes from actually building each phase — written **after** the phase
lands, not before. One section per phase, appended in order, never rewritten.

**What belongs here:** what shipped and what the next phase can rely on, the
deviations from the phase file that only became visible while building, and the
traps a later phase would otherwise walk into.

**What does not:** a file-by-file changelog, anything the code or the ADRs
already say, or a restatement of the phase spec. If it is discoverable by
reading the repo, leave it out — this file is for the things that are not.

> Phase files themselves live alongside this one and are the *plan*. This file is
> the *record*. Where the two disagree, this file says why.

---

## Phase 1 — The Shared Core

**Status:** complete · **Date:** 2026-09-03
**Verified by:** 125 pytest passing, `ruff check` clean, `/docs` renders, core-loop
script runs, broken-key fallback confirmed against the real SDK.

### What now exists

Four services under `apps/api/app/services/`, plus the two supporting modules
that turned out to be genuinely shared rather than engine-specific:

- `policy_engine.py` — 28 registered rules, all six categories
- `audit_trail.py` — append-only writer/reader, batch summary
- `llm_agent.py` — Gemini behind a provider Protocol, cache, backoff, fallbacks
- `razorpay_client.py` — test-mode only, plus deterministic simulated mode
- `decline_taxonomy.py` — the taxonomy as a lookup table
- `reasoning_tasks.py` — the one shared reasoning task and its fallback

Also: `models/` (enums, provenance, audit, batch, recoverable-entity mixin),
`db/types.py`, six read-only audit routes, and `scripts/core_loop_demo.py`.

Engine folders are still empty, as the phase required.

### The interfaces every engine will consume

```python
decision = PolicyEngine().evaluate(PolicyRequest(
    engine, entity, proposed_action, now,
    decline_code=None, is_outreach=False, messages_sent_in_window=0,
    max_attempts_override=None, abstained=False, unevaluable_precondition=None,
))
decision = PolicyEngine().authorize(request, llm_recommendation)  # can only narrow

result = LLMAgent().run("task_name", context)   # never raises for provider problems
trail.record(..., provenance=result.provenance, model_confidence=result.confidence)
trail.resolve_outcome(entry_id, outcome=..., amount_recovered_paise=...)
trail.batch_summary(batch_id)                   # per-source, computed from the log
```

`PolicyDecision.audit_fields()` maps straight onto `audit_trail.record()`. Use it
rather than re-deriving the fields by hand.

### Deviations from the phase file, and why

| # | Deviation | Why it happened |
| --- | --- | --- |
| 1 | **Added a new skill, `policy-bounds`** | Quiet hours, escalating backoff, the hard attempt ceiling and the escalation triggers had no home in `decline-taxonomy` or `rbi-mandate-rules`. Writing them inline would have broken non-negotiable #1 (no invented thresholds). Each bound is now named (`AC1`, `CD2`, `QH1`…), valued, and argued in the skill, and cited by rule id from the engine. **Later phases must cite this skill for any bound the other two do not cover.** |
| 2 | **`amount_paise` split into `amount_at_risk_paise` + `amount_recovered_paise`** | The phase file requires both for headline metrics; the `audit-schema` skill had one field. The skill was updated to match what shipped, so the two do not diverge. |
| 3 | **Append-only got one documented exception** | §5.1 permits resolving a pending outcome; the `audit-schema` skill said entries are *never* updated. Resolved in favour of the phase file, and the skill now documents `resolve_outcome()` as the single guarded transition, with the reasoning (entries are written *before* side effects, so the outcome is genuinely unknown at write time). It refuses an already-resolved entry. |
| 4 | **No `decision_source` field** | The skill is explicit that `provenance` replaces it — "one field, not both". `provenance.source` carries it. The phase file's §5.1 table lists both; the skill won, as §5.1 itself instructs. |
| 5 | **Added `RAZORPAY_MODE` to config and `.env.example`** | §5.4 requires a simulated mode but the env template had no switch for it. Default is `simulated`. |
| 6 | **Fixed IST offset instead of `ZoneInfo("Asia/Kolkata")`** | `tzdata` is absent from a bare Windows Python and the import blew up. IST has observed no DST since 1945, so a fixed `+05:30` is exact and drops a dependency. |
| 7 | **Added `Engine.CORE` and `Action.API_CALL`** | The shared core acts before any engine exists — batch bookkeeping, a logged Razorpay call. Kept distinct so per-engine recovery metrics stay clean. Added to the `audit-schema` skill too. |
| 8 | **Model id stayed `gemini-3.1-flash-lite` (GA), not the `-preview` id the phase named** | Already settled in ADR 0002 before this phase; resolved from `GEMINI_MODEL`, so switching is a one-line config change. Noted here only so nobody "fixes" it later. |

### Things a later phase will otherwise get wrong

**The `_permit()` privacy is the safety property.** `authorize()` cannot
construct an allowed decision — only `evaluate()` can, through a module-private
constructor. An engine that reads `llm_agent` output and acts on it *without*
going through `authorize()` bypasses ADR 0003 silently. The tell in the audit
trail is an entry citing no policy rule.

**Reasoning tasks must be registered from the app lifespan**, following the
`register_core_tasks()` pattern in `reasoning_tasks.py`. Startup enforcement only
sees tasks that have actually been registered — a task registered lazily at first
call defeats the whole point of checking at boot.

**Registration loads the prompt file eagerly.** A task whose prompt file is
missing or unversioned fails at boot, not mid-batch. Write the prompt file first.

**Simulated Razorpay failures never happen on their own.** A failure fires only
when the request carries `notes.simulate_failure = "<razorpay reason>"`. Phase 2's
generator must set it from the seed — otherwise every simulated call succeeds and
the recovery rate means nothing. Valid scenario keys are the top-level keys of
`app/services/fixtures/razorpay/error_scenarios.json`; an unknown one produces a
failure through the `UNKNOWN` path rather than a silent success.

**Every Razorpay call needs `notes.batch_id`.** It is the only thing tying a
Razorpay-side object back to a reproducible batch.

**Naive datetimes are rejected at the database column**, not coerced. Use
`app.db.types.utcnow()`. A naive value raises on write.

**Money is integer paise in every signature.** `_rupees()` in the policy engine
is display-only and lives there for rationale strings.

### Bounds now fixed in code (change only via the skill + an ADR)

- Hard attempt ceiling **4** (`policy-bounds:AC1`) — nothing raises it; a
  configured override above it is clamped, not honoured
- Mandate cap **3** / spacing **24h** (`rbi-mandate-rules:PartB`)
- Receivables ladder **3 rungs**, aligned with the QH2 contact cap so the two
  bounds cannot disagree about how often a customer hears from us
- Generic cooldown **6h**, doubling, capped at **72h** (`CD1`/`CD2`)
- Outreach window **09:00–21:00 IST**, contact cap **3 per rolling 7 days**
- AFA threshold **₹15,000** (regulatory, `A3`); high-value human review
  **₹50,000** (ours, `HE3`)
- Unknown-decline classification confidence floor **0.6**

Engine 2's phase should confirm the mandate numbers still read correctly against
`rbi-mandate-rules` Part B before building on them.

### Observations worth carrying forward

- **The live model returned `confidence: 1.0`** on a clear-cut classification.
  If that overconfidence pattern holds, the 0.6 floor will rarely fire. Matters
  for Engine 1, whose fallback depends on ambiguity being *reported* honestly —
  worth checking the actual confidence distribution before trusting it as a
  routing signal.
- **The cache works and is worth relying on.** A repeated identical call was
  served from disk with `cache_hit: true` and no live call. Seeded batch re-runs
  should be nearly free; the pre-demo run does not need to re-spend quota.
- **`batch_summary()` is recomputed from entries every time**, including by the
  API route. The snapshot stored on `BatchRun.summary` is a dashboard
  convenience only — never quote it as a figure.
- **`policy_violations` should always be 0.** A non-zero count means a row
  reached `audit_entries` without going through the writer. Treat it as a bug
  hunt, not a metric.
- A batch with **zero `blocked`/`halted` entries** is suspicious rather than
  clean — it means the gate never fired.

### Not done in this phase (deliberately)

No engines, no synthetic data, no frontend, no engine-specific routes, no
deployment. Commits were left to the user.
