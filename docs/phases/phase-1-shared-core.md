# Phase 1 — The Shared Core

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the shared core that all three engines will sit on top of: the policy engine (bounded decisions + stopping rules), the audit trail (immutable, queryable log of every action and its justification), the LLM agent wrapper (the reasoning layer), the Razorpay test-mode client, the database models, and the API skeleton that wires them together.

Nothing in this phase is engine-specific. If you find yourself writing logic that only makes sense for mandates, or only for invoices, you are building the wrong thing — stop and reconsider whether it belongs in a later phase.

---

## 2. Why this phase comes first

The entire differentiation story of this project is *"one shared recovery infrastructure spanning three revenue-leak points, not three disconnected demos."* That claim is only true if the shared core genuinely exists before any engine does. Building engines first and extracting a "shared" core afterward produces three subtly divergent implementations and a core that's really just the intersection of accidents — which a technical panel will spot immediately in the diff history.

Getting this right also makes every later phase dramatically faster: an engine should end up being a thin, purpose-specific front door that ingests its own data shape and calls into machinery that already works.

---

## 3. Preconditions

The scaffolding phase must already be complete. Before starting, verify:

- The repo structure from `CLAUDE.md` exists, including `apps/api/app/services/`, `apps/api/app/models/`, `apps/api/app/db/`, `apps/api/app/api/v1/routes/`, and `apps/api/app/tests/`.
- All `.claude/agents/`, `.claude/skills/`, and `.claude/commands/` files exist and are populated (not stubs).
- `apps/api` runs — the FastAPI health-check route responds.
- `.env.example` exists covering Razorpay test keys, `GEMINI_API_KEY`, `GEMINI_MODEL`, the deterministic-only mode flag, the LLM cache path, and the DB path.

If any of these are missing, fix that first and note it in your commit, rather than working around it.

---

## 4. Scope

### In scope

- `apps/api/app/services/policy_engine.py` — the bounded decision layer
- `apps/api/app/services/audit_trail.py` — the audit log writer/reader
- `apps/api/app/services/llm_agent.py` — the Gemini provider wrapper used by all engines
- `apps/api/app/services/razorpay_client.py` — the test-mode Razorpay wrapper
- `apps/api/app/models/` — Pydantic schemas and SQLAlchemy models for the shared entities
- `apps/api/app/db/` — session/engine setup, table creation
- `apps/api/app/api/v1/routes/audit.py` — read-only endpoints to query the audit trail
- `apps/api/app/core/config.py` — settings loaded from env
- Pytest coverage for the policy engine and audit trail

### Out of scope — do not build these in this phase

- Any of the three engines (`engines/root_cause/`, `engines/mandate_recovery/`, `engines/receivables/`) — leave those folders empty
- Synthetic data generation (that is Phase 2)
- Any frontend work
- Any engine-specific routes
- Deployment, Docker, CI

---

## 5. Design specification

This section is prescriptive because these interfaces are consumed by every later phase. Deviating here creates rework across three engines.

### 5.1 The audit trail

This is the single most important artifact in the entire project — the competition's bar explicitly demands an audit trail, and it's what makes every claim about "bounded and explainable" verifiable rather than asserted.

Every entry must record at minimum:

| Field | Purpose |
|---|---|
| `id` | Primary key |
| `timestamp` | UTC, when the action was decided |
| `batch_id` | Groups all entries from one batch run — essential for reporting per-batch recovery metrics later |
| `engine` | Which engine acted (`root_cause`, `mandate_recovery`, `receivables`) |
| `entity_type` / `entity_id` | What was acted on (a payment, a mandate, an invoice) |
| `action` | What was done or decided (e.g. `retry_scheduled`, `retry_suppressed`, `escalated`, `halted`) |
| `decision_source` | Whether this came from a deterministic rule or from LLM reasoning — this distinction matters enormously for trust, and judges will care |
| `provenance` | The full provenance object from section 5.3.1: source, provider, model, cache hit, prompt version, abstention |
| `rule_id` | The specific policy rule that authorized or blocked the action |
| `reasoning` | Human-readable justification. For rule-based decisions, the rule's own explanation. For LLM decisions, the model's stated reasoning |
| `llm_metadata` | Tokens and latency — null for rule-based decisions |
| `outcome` | Result once known (`success`, `failure`, `pending`), updatable |
| `amount_at_risk` / `amount_recovered` | In paise (integer). These two fields are what every headline metric is later computed from |

Design constraints:

- **Append-only in spirit.** Entries are never deleted. The only mutation permitted is updating `outcome` and `amount_recovered` when a pending action resolves. Enforce this through the service API — do not expose a generic update.
- **Nothing bypasses it.** Every decision that any engine ever makes must go through this writer. If an action happens without an audit entry, that is a bug, and later phases will assert against it.
- Provide a query interface supporting: fetch by `batch_id`, filter by engine/entity/action, and a **batch summary** function returning total at-risk, total recovered, recovery rate, action counts, and a count of any policy violations. That summary function is what produces the headline number for the demo, so it must compute honestly from the log — never from a separately-maintained counter that could drift.

Use the `audit-schema` skill as the source of truth and update it if you refine the schema, so the two never diverge.

### 5.2 The policy engine

This is the "bounded, gated" guarantee made concrete. It answers one question: *given this situation, what is the system permitted to do right now, and which rule says so?*

Core principles:

- **Rules are declarative and centrally registered**, each with a stable `rule_id`, a human-readable description, and a citation back to its source (a `decline-taxonomy` entry, an `rbi-mandate-rules` clause, or an explicit product decision). A rule with no traceable justification should not exist.
- **The policy engine always has the final say.** The LLM agent may *recommend* an action; the policy engine decides whether it is permitted. Under no circumstances can LLM output authorize an action the rules forbid. This ordering is the entire safety story of the product — build it so that it is structurally impossible to invert, not merely conventionally observed.
- Every decision returns a structured result containing: allowed/denied, the deciding `rule_id`, the reasoning, and any constraint on the action (e.g. earliest permitted retry time). This object maps directly onto audit-trail fields.

Rule categories to implement in this phase (generic, engine-agnostic):

1. **Attempt caps** — maximum recovery attempts per entity, configurable per engine, with a hard ceiling that cannot be overridden.
2. **Cooldown/backoff** — minimum interval between attempts on the same entity, supporting escalating backoff.
3. **Hard-stop conditions** — a permanent state (revoked mandate, closed account, entity marked resolved) immediately and irreversibly halts all further action on that entity.
4. **Quiet-hours / communication limits** — no customer-facing outreach outside permitted hours; a cap on messages per entity per period. This is what keeps "chasing" from becoming harassment, and it is a genuine compliance posture, not decoration.
5. **Amount thresholds** — hooks for value-dependent branching (later used for the ₹15,000 AFA threshold in Engine 2), implemented generically here.
6. **Human-escalation trigger** — conditions under which the system must stop acting autonomously and hand off to a human. Every bounded agent needs an exit hatch, and being able to point at it is worth more than another feature.

Consult the `policy-architect` agent for this work, and require that every rule cites the `decline-taxonomy` or `rbi-mandate-rules` skill, or is explicitly labelled a product decision. Do not invent thresholds that sound plausible.

### 5.3 The LLM agent wrapper

The reasoning layer. Every engine calls the model through this single wrapper — never directly.

**Provider**: Google Gemini, model `gemini-3.1-flash-lite-preview`, read from config (`GEMINI_MODEL`) rather than hardcoded at call sites. Write it behind a small provider interface with one implementation — the abstraction costs almost nothing and keeps every engine decoupled from the SDK.

Requirements:

- A clean interface for structured reasoning calls: takes a task type, a context payload, and a response schema; returns parsed, validated output plus provenance metadata.
- **Structured output enforced via Pydantic validation.** If the model returns something unparseable, retry once with the validation error fed back, then fall back deterministically. Never let malformed LLM output propagate into decision logic.
- **Response caching, keyed by a hash of the resolved prompt plus model plus prompt version.** This is not an optimization — it is what keeps repeated development runs and the pre-demo seed run from consuming the daily quota, and it is what makes a batch reproducible without re-spending it. Cache to disk under a configurable path, and record cache hits in provenance.
- **Exponential backoff on rate limits.** A 429 gets retried with increasing delay (1s, 2s, 4s) before being treated as a failure. Free-tier limits are per-minute as well as per-day, so a tight batch loop will hit them; handle it rather than letting it kill a run.
- **A deterministic fallback for every reasoning task.** No task may exist without one. When the model is unavailable, rate-limited beyond backoff, returns invalid output twice, or when deterministic-only mode is forced by config, the task falls back and the batch continues. The fallback is task-specific by design — see below.
- **Every call is auditable**, with full provenance (section 5.3.1) written to the audit trail, capturing the model's stated reasoning where it produced one.
- Prompts live in a dedicated location (e.g. `services/prompts/`), version-controlled as files rather than buried inline in Python strings, each carrying a version identifier that flows into provenance.

#### 5.3.1 Provenance

Every reasoning result carries a structured provenance object, propagated into the audit trail, API responses, and the dashboard:

| Field | Purpose |
|---|---|
| `source` | `model` or `deterministic` — the single most important field, since it tells any reader whether a judgment was reasoned or ruled |
| `provider` / `model` | Exactly what produced it |
| `cache_hit` | Whether this came from the response cache rather than a live call |
| `prompt_version` | Which prompt version produced it, so any metric traces back to its prompt |
| `abstained` | Whether the task declined to answer and routed to human review |
| `latency_ms` / `tokens` | Null for deterministic results |

Nothing derived from a model may appear anywhere in the system without this object attached. A number whose origin cannot be traced is a number nobody should trust.

#### 5.3.2 The deterministic fallback contract

Fallback behaviour is defined per task, and the differences are deliberate: degrade to something boring where boring is safe, degrade to abstention where being wrong is expensive. Implement the contract and its registration mechanism here; each engine defines its own fallback in its own phase.

- Where a safe, accurate default response exists (e.g. a templated message), the fallback produces it.
- Where the task requires genuine interpretation and a wrong answer would corrupt downstream state (e.g. inferring a commitment from free text), the fallback **abstains and routes to human review**. It must never guess.
- Where the task is a classification with a rule-based approximation available, the fallback applies the rule and biases toward escalation on ambiguity.

Every fallback writes an audit entry marked `source: deterministic`, so a run's mixed provenance is always visible. Metrics must be reported per source — never blend model-derived and rule-derived results into a single figure that implies more than it delivers.

### 5.4 The Razorpay client

A thin, well-typed wrapper over the official `razorpay` Python SDK, test mode only.

- Read keys from config; **fail loudly at startup if a key does not look like a test key** (`rzp_test_` prefix). Never allow a live key path to exist in this codebase.
- Wrap the operations later phases need: creating orders, creating payment links, fetching payment status, and subscription/mandate operations. Where the SDK doesn't cover something cleanly, use `httpx` directly, but keep the surface consistent.
- Normalize failures into a consistent internal error shape, mapping Razorpay's error codes into terms the decline taxonomy understands. Do not leak raw SDK exceptions into engine code.
- Every call and its result get logged to the audit trail.
- Include a mode where API calls can be simulated deterministically from local fixtures rather than hitting the network. This is not a shortcut — it is what makes batch runs reproducible for judges and keeps the demo from depending on live network conditions. Make the mode explicit and visible in the audit trail, never silent.

Consult the `razorpay-integrator` agent, grounded in the `razorpay-api` skill.

### 5.5 Data models

Define the shared entities every engine touches. At minimum: an audit-entry model, a batch-run model (id, engine, started/completed timestamps, summary metrics), and a base recoverable-entity concept carrying the fields all three engines share (identifier, amount in paise, currency, status, attempt count, last-attempt timestamp, terminal flag).

Engine-specific entities (payments, mandates, invoices) come in their own phases — define only what is genuinely shared here. Money is stored as integer paise throughout; never floats.

### 5.6 API skeleton

Wire the services into FastAPI: config loading, DB session dependency, table creation on startup, and read-only audit routes (fetch batch summary, list entries with filters, fetch a single entry with full reasoning). Keep routes thin — all logic lives in services. Confirm the auto-generated OpenAPI docs render correctly at `/docs`, since that documentation is part of what makes the repo read as professionally built.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Policy engine | `policy-architect` | `decline-taxonomy`, `rbi-mandate-rules` |
| Audit trail | `policy-architect` | `audit-schema` |
| LLM wrapper | `policy-architect` (for the decision-authority boundary) | `llm-provider`, `audit-schema` |
| Razorpay client | `razorpay-integrator` | `razorpay-api` |
| Tests | `test-engineer` | `decline-taxonomy`, `rbi-mandate-rules` |
| Commits | — | `conventional-commits` |

If any skill file turns out to be thin or missing detail needed here, improve the skill file as part of this phase rather than working around it. The skills are project infrastructure, not documentation debris.

---

## 7. Testing requirements

Pytest coverage in `apps/api/app/tests/`, prioritized in this order:

1. **Policy engine** — every rule category from 5.2, including the boundary cases: attempt cap reached exactly, cooldown not yet elapsed, hard-stop overriding an otherwise-permitted action, quiet-hours boundaries, and the human-escalation trigger firing. Explicitly test that **an LLM recommendation cannot override a policy denial** — this is the single most important test in the repository.
2. **Audit trail** — entries are written correctly, outcomes update correctly, deletion is not possible through the service API, and the batch summary computes correctly (including a case with zero recoveries, so the metric can't be accidentally hardcoded optimistic).
3. **LLM wrapper** — mocked provider responses: valid structured output parses; malformed output triggers retry-then-fallback; API failure, timeout, and 429-beyond-backoff each fall back deterministically with a logged degradation; cache hits are served without a live call and marked in provenance; forced deterministic-only mode bypasses the provider entirely.
4. **Razorpay client** — mocked/fixture responses: test-key validation rejects a non-test key, error normalization maps correctly, simulated mode is deterministic.

No live network calls in tests.

---

## 8. Acceptance criteria

The phase is done when all of the following hold, verified by actually running them:

- `pytest` passes with all of section 7 covered.
- `ruff check` passes clean.
- The API starts, `/docs` renders, and the audit routes respond correctly against a seeded audit entry.
- A short throwaway script (not committed, or committed under `scripts/` if useful) can: create a batch run, request a decision from the policy engine, have it denied by a rule, and show the resulting audit entry containing the denying `rule_id` and its reasoning. **This is the core loop of the entire product — if it works here, everything downstream is assembly.**
- Every policy rule in the registry has a description and a traceable source.
- Deliberately break the Gemini API key and confirm the fallback path activates, logs the degradation with `source: deterministic`, and does not crash.

---

## 9. Documentation to produce

- An ADR in `docs/adr/` recording the decision that the policy engine has final authority over LLM recommendations, and why. This is the project's central design decision and the panel will find it convincing that it was made deliberately and early.
- A second ADR recording the simulated-vs-live Razorpay mode decision and its reproducibility rationale.
- Update `docs/architecture.md` with the shared core's actual interfaces as built.

Keep ADRs short — a few paragraphs covering context, decision, and consequences. They exist to show deliberate trade-offs, not to be exhaustive.

---

## 10. Commit checklist

Commit incrementally as working pieces land, not in one lump at the end. Follow the `conventional-commits` skill. A reasonable sequence:

- `feat(core): add audit trail schema and service`
- `feat(core): add policy engine with bounded rule registry`
- `feat(core): add LLM agent wrapper with structured output, caching, and deterministic fallback`
- `feat(core): add Razorpay test-mode client`
- `feat(api): wire shared core into FastAPI with audit routes`
- `test(core): cover policy rules, audit trail, and degradation paths`
- `docs: add ADRs for policy authority and Razorpay simulation mode`

Leave `main` runnable after each commit.

---

## 11. Stop condition

When the acceptance criteria in section 8 all pass and the commits are pushed, **stop and report back**. Summarize what was built, the interfaces the engines will consume, anything that deviated from this spec and why, and anything discovered that later phases should account for.

Do not begin Phase 2, do not generate synthetic data, and do not scaffold any engine. The next phase file will be provided separately.
