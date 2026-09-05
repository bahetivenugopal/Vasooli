# Vasooli — Project Context & Initialization

**Read this entire file before doing anything.** This is the full context transfer for a project that already has a locked scope, tech stack, and architecture — your job right now is narrowly defined in the "Your task right now" section near the bottom. Do not start building product features yet; that happens in separate phase files that will be added to this repo one at a time.

---

## 1. What this project is

**Vasooli** is an AI-first revenue recovery platform built for a Razorpay-run student hackathon (the "Buildathon"). Selection is signal-based: no resume screen, no test — a public GitHub repo and an architecture walkthrough are submitted, and if the panel sees genuine signal, the builder goes straight to an interview for a paid **AI Builder Intern** role. **Deadline: noon, 5th September.** Every decision in this document was made under that time constraint — favor working and provable over ambitious and half-finished at every fork.

**Tagline**: *"Revenue doesn't disappear. It goes missing. Vasooli brings it back."*

**One-paragraph summary**: Businesses lose revenue in three distinct, quietly compounding ways — a payment corridor degrades and nobody notices, a recurring mandate/subscription charge fails and the customer never meant to churn, or a B2B invoice goes overdue and just sits there. Vasooli is one shared recovery engine — one policy layer, one LLM-powered reasoning layer, one audit trail — applied to all three leak points, so every recovery action is bounded, explainable, and provably compliant, not just "an agent that tries stuff."

---

## 2. The competition track this was built for

**Track: AI Revenue Recovery.** ("Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables.")

**The bar we are explicitly building to clear**: don't just identify the problem — show **measured money recovered across a batch**, with **compliant escalation**, **stopping rules**, and an **audit trail**. Every number Vasooli reports must be honestly computed from its own synthetic batch, never cherry-picked.

**Submission requires**: Project name/title, project objectives (what it solves), GitHub repo URL, and a "Build Challenges & Technical Obstacles" write-up (what issues came up while building and how they were solved — this should be written from real git history/ADRs later, not invented).

---

## 3. Product scope — the three engines

We deliberately chose **3 unified capabilities that absorb 5 of the track's 7 example directions**, sharing one backbone, rather than building one shallow demo per example direction. This is the core differentiation story — say it explicitly in the README: *"most teams will build one demo around one example. We built the shared recovery infrastructure that spans the track's own full breadth — payment failures, subscriptions, receivables — with one policy layer and one audit trail underneath all three."*

### Engine 1 — Root-Cause Recovery Engine
Covers example direction: *payment degradation → root cause → recovery*.
Watches a stream of payment attempts. When a specific corridor (a bank, a payment method, a route) starts failing at an abnormal rate, it diagnoses *why* — is this one customer's problem (e.g. insufficient funds) or a corridor-wide problem (e.g. a specific issuing bank's UPI rail degrading for everyone routed through it)? Those two situations look similar from a single decline code but demand opposite responses: dunning one customer vs. instantly rerouting all affected traffic. This diagnosis is the most genuinely agentic, judgment-requiring part of the whole build — it should be a real LLM reasoning call, not a hardcoded if/else.

### Engine 2 — Mandate & Subscription Recovery Engine
Covers example directions: *mandate retry sequencer* + *failed-subscription recovery* (treated as one, because in India they are the same event: a failed recurring charge is, mechanically, a mandate/AFA event).
For every failed recurring charge: classify **soft decline** (temporary — insufficient funds, timeout, bank hiccup; card/mandate still valid, retry can succeed) vs. **hard decline** (permanent — expired card, closed account, revoked mandate; retrying is not just useless, it risks tripping issuer fraud filters). Soft declines get retried inside RBI's actual compliance windows; hard declines skip straight to a dunning/communication sequence instead of wasting retry attempts. Every retry respects explicit stopping rules (see section 4).

### Engine 3 — B2B Receivables Chaser with Promise-to-Pay Tracking
Covers example directions: *B2B receivables chaser* + *promise-to-pay tracker* (treated as one workflow, since a chaser without PTP tracking is incomplete and PTP tracking without a chase workflow is meaningless on its own).
For a batch of overdue invoices: draft an escalating-but-never-harassing reminder sequence, extract any "I'll pay by [date]" commitment the customer makes in their reply (a genuine LLM text-understanding task), track it, and only escalate the promises that get **broken** — with a capped, compliant escalation ladder.

**Deliberately out of scope for this build**: checkout drop-off recovery (too generic/marketing-flavored, least payments-native, weakest differentiation) and Hinglish voice recovery as a *core* engine (reframed as an optional bonus delivery channel bolted onto Engine 2's notification step, only if time remains after the three engines above are solid and demo-ready — never build this before the core three are done).

---

## 4. Domain facts the engines must be grounded in (do not invent these — use these)

These are load-bearing facts from real research. The `policy-architect` agent and the relevant skills (section 6) must encode these, not approximations.

**Soft decline vs. hard decline.** A soft decline is temporary — the payment method is still valid, the failure is something like insufficient funds, a timeout, or a network hiccup — and a well-timed retry can succeed. A hard decline is permanent — expired card, lost/stolen, closed account, revoked mandate — and retrying wastes attempts and risks triggering issuer-side fraud filters. Roughly **80%+ of all declines are soft**, and the single most common, most expensive mistake real systems make is treating every decline identically instead of routing soft → retry and hard → dunning/escalation immediately. This distinction is the backbone of Engine 1 and Engine 2 both.

**Why this matters at scale.** Involuntary churn — a customer lost to a *failed payment*, not a decision to leave — is estimated at **20–40% of all subscription churn industry-wide**, with global failed-payment losses estimated around **$129B in 2025**. Up to **70% of involuntary churn is customers who never meant to leave**, and separately, **62% of users who hit a payment error never retry it themselves**. This is the emotional core of why the "recovery" framing matters, not just the money.

**RBI's e-mandate / Additional Factor Authentication (AFA) framework** — the regulatory reality Engine 2 must respect, not just retry blindly against:
- A recurring payment mandate requires a one-time AFA (OTP-based) registration.
- A **pre-debit notification** must be sent to the customer 24–48 hours before every scheduled debit.
- As of the RBI's 2026 update, recurring transactions **up to ₹15,000 per transaction** can process without a fresh OTP each cycle, once the mandate is registered; transactions above that threshold, or outside enhanced categories, still require fresh AFA each cycle.
- Customers can cancel/revoke a mandate at any time — a revoked mandate is an immediate hard-stop, never retryable.
- If a mandate fails due to missing AFA, the transaction is declined by the issuing bank — this is itself a distinct, classifiable failure mode from a simple insufficient-funds soft decline.

Engine 2's retry scheduler must be provably bounded by these constraints (max retry attempts, respecting the pre-debit notification window, immediate hard-stop on revocation, different handling above/below the ₹15,000 AFA threshold) — this is what "compliant escalation, stopping rules" concretely means for this build, not a vague policy.

---

## 5. Tech stack (final — do not deviate without a documented reason in an ADR)

| Layer | Choice |
|---|---|
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui, Recharts |
| Backend | Python 3.11, FastAPI, Pydantic v2 |
| Database/ORM | SQLite via SQLAlchemy (deliberately zero-setup for the hackathon; connection string swap gets you Postgres later, don't build that now) |
| Agent/reasoning layer | Google Gemini (`gemini-3.1-flash-lite-preview`), accessed through a provider interface in `services/llm_agent.py`. This is the actual product intelligence (root-cause diagnosis, decline explanations, dunning/receivables message drafting, promise-to-pay extraction from free text) and must be genuine LLM judgment, not rules dressed up as AI. Every reasoning task has a defined deterministic fallback so a batch run never fails |
| Payments | Official `razorpay` Python SDK; direct `httpx` calls only for anything the SDK doesn't cover; **test-mode keys only, always** |
| Testing | Pytest, focused heavily on `policy_engine.py` — this is the credibility backbone of the whole submission |
| Lint/format | Ruff (Python), ESLint + Prettier (TS) |
| Deployment | Stretch goal only, after all 3 engines work locally — Vercel (frontend) + Railway/Render (backend) |

Rationale in one line: this stack is the one Claude Code has the deepest, most reliable fluency in — fewer hallucinated APIs, faster correct-on-first-try generation — while giving up nothing a "better" stack would offer at this scale and time budget.

**Explicitly not doing**: no Kubernetes/Docker-Compose, no splitting the three engines into separate microservices (one FastAPI app, cleanly separated by folder), no custom agent framework layered on top of the provider SDK. All three would be premature complexity with zero payoff for judges in a ~30-hour build.

---

## 6. System architecture

One synthetic data layer feeds all three engines. All three engines route every decision through the same shared core — this is the single most important structural fact about this codebase:

```
Synthetic data generator (transactions, mandates, invoices)
        │
        ├──► Root-cause engine ───┐
        ├──► Mandate engine ──────┤
        └──► Receivables engine ──┘
                                   │
                    ┌──────────────▼──────────────┐
                    │         Shared core          │
                    │  policy engine · LLM agent   │
                    │       · audit trail           │
                    └──────────────┬──────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                             ▼
        Razorpay test-mode API          Control tower dashboard
```

An engine's own code should almost entirely be about *what data it ingests* and *what recovery actions it's allowed to call* — the actual judgment (is this retryable, what's the root cause, what's the next bounded action, log it) always routes through the same `policy_engine.py`, the same `llm_agent.py` wrapper, and the same `audit_trail.py` writer in `apps/api/app/services/`. Build the hard part once.

---

## 7. Repo structure (create exactly this — do not restructure without discussion)

```
vasooli/
├── .claude/
│   ├── CLAUDE.md
│   ├── agents/
│   │   ├── policy-architect.md
│   │   ├── razorpay-integrator.md
│   │   ├── data-synthesizer.md
│   │   ├── frontend-builder.md
│   │   └── test-engineer.md
│   ├── skills/
│   │   ├── razorpay-api/SKILL.md
│   │   ├── llm-provider/SKILL.md
│   │   ├── decline-taxonomy/SKILL.md
│   │   ├── rbi-mandate-rules/SKILL.md
│   │   ├── audit-schema/SKILL.md
│   │   └── conventional-commits/SKILL.md
│   └── commands/
│       ├── new-engine.md
│       ├── run-batch-demo.md
│       └── audit-check.md
├── apps/
│   ├── web/
│   │   └── src/
│   │       ├── app/
│   │       ├── components/
│   │       │   ├── ui/            # shadcn primitives — the ONE source of truth, never duplicate
│   │       │   └── features/
│   │       ├── lib/
│   │       ├── hooks/
│   │       └── types/
│   └── api/
│       └── app/
│           ├── main.py
│           ├── api/v1/routes/
│           ├── core/
│           ├── engines/
│           │   ├── root_cause/
│           │   ├── mandate_recovery/
│           │   └── receivables/
│           ├── services/          # the shared core, literally: razorpay_client.py, llm_agent.py, policy_engine.py, audit_trail.py
│           ├── models/
│           ├── db/
│           └── tests/
├── packages/
│   └── shared-types/
├── data/
│   ├── generators/
│   └── samples/
├── docs/
│   ├── architecture.md
│   └── adr/
├── scripts/
├── .env.example
├── .gitignore
└── README.md
```

---

## 8. The `.claude/` layer — what to author, and how

Every agent must be written with an explicit **Role / Task / Context** structure — not vague instructions. When you create each file below, use the specs here as the source of truth (they draw on sections 3 and 4 above — use those facts, don't invent new ones).

### Agents (`.claude/agents/*.md`)

- **`policy-architect.md`** — Role: senior payments risk engineer. Task: design and maintain the bounded decision + stopping-rule logic shared by all three engines (`policy_engine.py`). Context: every rule must cite the `decline-taxonomy` and `rbi-mandate-rules` skills — no invented thresholds, ever.
- **`razorpay-integrator.md`** — Role: Razorpay API integration specialist. Task: write and maintain every Razorpay SDK/API call across all engines. Context: test-mode only; every call must handle and log failure gracefully per the `razorpay-api` skill.
- **`data-synthesizer.md`** — Role: synthetic data engineer. Task: generate realistic, **seeded/reproducible** batches of transactions, mandates, and invoices with controlled, documented distributions (decline-code mix, ageing, mandate windows). Context: judges must be able to trust the batch wasn't cherry-picked — reproducibility is non-negotiable.
- **`frontend-builder.md`** — Role: product-minded frontend engineer. Task: build the control tower dashboard. Context: never create a new primitive if one already exists in `components/ui/` — reuse or extend, don't duplicate.
- **`test-engineer.md`** — Role: QA engineer obsessive about edge cases. Task: write pytest coverage for every stopping rule and decline-classification path. Context: treat `policy_engine.py` coverage as the single highest-priority code in the repo — it's the proof behind "bounded and gated."

### Skills (`.claude/skills/*/SKILL.md`)

Reference knowledge loaded before touching related code — same pattern as this project's own document-generation skills.

- **`razorpay-api`** — exact endpoints, auth headers, request/response shapes for Orders, Payment Links, Subscriptions, and relevant test-mode card/UPI test credentials. Populate from Razorpay's official test-mode docs.
- **`decline-taxonomy`** — the soft-vs-hard decline classification from section 4, as a lookup table (decline reason → soft/hard → default action).
- **`rbi-mandate-rules`** — the AFA/pre-debit/threshold/revocation facts from section 4, as a factual reference the policy engine cites directly.
- **`llm-provider`** — Gemini configuration and usage: the model in use, how to call it, structured-output prompting, free-tier rate limits and exponential backoff on 429s, the response-caching strategy, and the deterministic fallback contract every reasoning task must define. Any code that calls a model reads this first.
- **`audit-schema`** — the single audit-trail log schema every engine writes to: at minimum, timestamp, engine name, entity id, action taken, the rule/reasoning that authorized it, outcome, and the provenance fields (`source`, `provider`, `model`, `cache_hit`, `prompt_version`, `abstained`). Define this schema once, here, before any engine is built.
- **`conventional-commits`** — commit message format (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, scoped like `feat(root-cause): ...`) and guidance to commit after every meaningful, working increment — not once per phase.

### Commands (`.claude/commands/*.md`)

- **`new-engine.md`** — scaffolds a new engine module matching the standard `engines/<name>/` folder shape.
- **`run-batch-demo.md`** — runs a synthetic batch end-to-end through a chosen engine and prints the recovery-rate summary.
- **`audit-check.md`** — dumps and validates the audit trail for a given batch run, confirming every entry has a reason code and a citing rule.

### `CLAUDE.md` (project root)

Must contain, concisely: the one-paragraph summary (section 1), the tech stack table (section 5), the folder-naming conventions (section 7), a note that agents are RTC-framed and where to find them, and an explicit **scope guardrail**: state the noon-5th-September deadline plainly and instruct that if a task looks like it will blow the remaining time budget, flag it and propose a smaller version rather than silently building the full version.

---

## 9. Your task right now

Do the following, in this order, and nothing beyond it:

1. Create the full repo structure from section 7 (empty folders are fine where no content is specified yet).
2. Write `CLAUDE.md` per the spec in section 8.
3. Write all 6 agent files, all 5 skill files, and all 3 command files per the specs in section 8, using the domain facts in sections 3 and 4 as source material — these should be complete and useful, not stubs.
4. Initialize `apps/web` as a Next.js 14 + TypeScript + Tailwind + shadcn/ui project (base scaffold from the framework's own tooling, no custom pages yet beyond the default).
5. Initialize `apps/api` as a FastAPI project with a minimal `main.py` (a health-check route is enough), `requirements.txt` pinned to reasonable versions, Pydantic v2, SQLAlchemy configured against a local SQLite file, and Ruff configured.
6. Write `.env.example` covering: Razorpay test-mode key id/secret, `GEMINI_API_KEY`, `GEMINI_MODEL` (defaulting to `gemini-3.1-flash-lite-preview`), a flag to force deterministic-only mode, an LLM response cache path, and the DB path.
7. Write a `.gitignore` covering both Node and Python (node_modules, .next, __pycache__, .venv, .env, *.db, etc).
8. Write a first-pass `README.md`: project name, one-paragraph summary, the architecture diagram (ASCII from section 6 is fine for now), and a "status: scaffolding complete, build in progress" note.
9. Stop. Do not build any engine logic, any product routes beyond the health check, or any dashboard pages. That work comes in separate phase files that will be added to this repo next, one at a time, each followed by a check and a commit.

**Do not skip step 9.** The whole point of phasing this project is verifiable, committable progress — building ahead of the current phase defeats that.
