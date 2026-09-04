# Vasooli — project instructions

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

## What this is

Businesses lose revenue in three distinct, quietly compounding ways: a payment
corridor degrades and nobody notices, a recurring mandate or subscription charge
fails and the customer never meant to churn, or a B2B invoice goes overdue and
just sits there. Vasooli is one shared recovery engine — one policy layer, one
Gemini-powered reasoning layer, one audit trail — applied to all three leak
points, so every recovery action is bounded, explainable, and provably compliant,
not just "an agent that tries stuff".

Built for the Razorpay Buildathon, **AI Revenue Recovery** track.

## ⏰ Scope guardrail — read this before starting any task

**Deadline: noon, 5th September.**

Every decision in this project was made under that constraint. At every fork,
favour **working and provable** over **ambitious and half-finished**.

> **If a task looks like it will blow the remaining time budget, flag it and
> propose a smaller version. Do not silently build the full version.**

Say plainly what the smaller version gives up. Shipping less is the user's call
to make, not yours to make quietly by running out of time.

Related: **build only what the current phase asks for.** Work arrives as phase
files, one at a time, each followed by a check and a commit. Building ahead
defeats the point of phasing, which is verifiable, committable progress.

## The three engines

All three share one backbone. This is the differentiation story — three unified
capabilities absorbing **five** of the track's seven example directions:

1. **Root-Cause Recovery** — watches payment attempts; when a corridor (bank,
   method, route) degrades, diagnoses whether this is *one customer's problem*
   (insufficient funds → dun that customer) or a *corridor-wide problem* (an
   issuing bank's rail degrading → reroute all affected traffic). Same decline
   code, opposite correct responses. This is the most genuinely agentic call in
   the build and must be a real model reasoning call, never a hardcoded if/else.
2. **Mandate & Subscription Recovery** — classifies every failed recurring charge
   soft vs hard; retries soft declines inside RBI's actual compliance windows;
   routes hard declines straight to dunning instead of burning retry attempts.
3. **B2B Receivables Chaser** — escalating-but-never-harassing reminders, extracts
   promise-to-pay commitments from free-text replies (a real model
   text-understanding task), and escalates only **broken** promises, on a capped
   ladder.

**Out of scope:** checkout drop-off recovery, and Hinglish voice as a *core*
engine (optional bonus channel on Engine 2's notification step, only if all three
engines are solid and demo-ready first — never before).

## Architecture — the one structural fact that matters

```
Synthetic data generator (transactions, mandates, invoices)
        │
        ├──► Root-cause engine ────┐
        ├──► Mandate engine ───────┤
        └──► Receivables engine ───┤
                                   │
                    ┌──────────────┼──────────────┐
                    │         Shared core         │
                    │  policy engine · LLM agent  │
                    │       · audit trail         │
                    └──────────────┬──────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
        Razorpay test-mode API          Control tower dashboard
```

An engine's own code is almost entirely about **what data it ingests** and **what
recovery actions it may call**. The judgment — is this retryable, what's the root
cause, what's the next bounded action, log it — **always** routes through
`services/policy_engine.py`, `services/llm_agent.py`, and
`services/audit_trail.py`.

**Build the hard part once.** An engine that reimplements decision logic is a bug.

## Tech stack

Final. Do not deviate without a documented reason in `docs/adr/`.

| Layer | Choice |
| --- | --- |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, shadcn/ui, Recharts |
| Backend | **Python 3.12** (pinned — see `.python-version` and ADR 0001), FastAPI, Pydantic v2 |
| Database/ORM | SQLite via SQLAlchemy (zero-setup; a connection-string swap gets you Postgres later — don't build that now) |
| Agent/reasoning | **Google Gemini** (`gemini-3.1-flash-lite`) via `google-genai`, behind a thin provider interface — the actual product intelligence, and it must be genuine LLM judgment, not rules dressed up as AI. Every reasoning task has a registered deterministic fallback, so a batch always completes. See the `llm-provider` skill and ADR 0002 |
| Payments | Official `razorpay` Python SDK; direct `httpx` only for what the SDK doesn't cover; **test-mode keys only, always** |
| Testing | Pytest, focused heavily on `policy_engine.py` — the credibility backbone of the submission |
| Lint/format | Ruff (Python), ESLint + Prettier (TypeScript) |
| Deployment | Stretch goal only, after all 3 engines work locally and the video is recorded — Vercel + Railway/Render |

**Explicitly not doing:** no Kubernetes or Docker Compose, no splitting engines
into microservices (one FastAPI app, separated by folder), no custom agent
framework over the provider SDK. All three are premature complexity with zero
payoff for judges in a ~30-hour build.

## Folder conventions

```
apps/web/src/components/ui/        shadcn primitives — the ONE source of truth, never duplicate
apps/web/src/components/features/  Vasooli-specific composed components
apps/api/app/engines/<name>/       one folder per engine, same internal shape
apps/api/app/services/             the shared core. The four that carry the
                                   decision loop:
                                     razorpay_client.py · llm_agent.py
                                     policy_engine.py   · audit_trail.py
                                   plus the cross-engine reporting layer:
                                     overview.py · unified_run.py
                                     integrity.py · audit_validation.py
apps/api/app/services/prompts/     versioned prompt files
apps/api/app/tests/                pytest
data/generators/                   seeded synthetic data
data/samples/                      committed sample batches
docs/adr/                          architecture decision records
docs/pitch/                        video script + submission answers
```

- Python: `snake_case` files, engine folders `snake_case`.
- TypeScript: `PascalCase` components, `camelCase` utils.
- **Money is integer paise everywhere.** Never a float. Format to ₹ at display only.
- **Timestamps are tz-aware UTC.** Convert at display only.

## Agents and skills

**Agents are RTC-framed** — every agent file states an explicit **Role / Task /
Context** rather than vague instructions.

`.claude/agents/` — `policy-architect` · `razorpay-integrator` ·
`data-synthesizer` · `frontend-builder` · `test-engineer` · `docs-and-pitch-writer`

`.claude/skills/` — reference knowledge to load **before** touching related code:

| Skill | Load before |
| --- | --- |
| `razorpay-api` | any Razorpay call |
| `decline-taxonomy` | any decline handling, retry eligibility, or dunning routing |
| `rbi-mandate-rules` | anything in Engine 2, any retry scheduler, any compliance gate |
| `policy-bounds` | any policy rule the other two skills don't cover — quiet hours, backoff, attempt ceiling, escalation triggers, Engine 3's chase ladder and promise-to-pay bounds |
| `corridor-detection` | Engine 1 — corridor segmentation, detection thresholds, the minimum-volume guard, reroute expiry |
| `audit-schema` | `audit_trail.py`, or adding any engine action |
| `llm-provider` | any code that calls a model, registers a reasoning task, or reads a reasoning result |
| `conventional-commits` | any commit |

`.claude/commands/` — `/new-engine` · `/run-batch-demo` · `/audit-check`

## Non-negotiables

1. **No invented thresholds.** Every policy rule cites a named rule in
   `decline-taxonomy` or `rbi-mandate-rules`. Need a number neither provides? Add
   a named rule to the skill first, with rationale, then cite it.
2. **Every audit entry cites the rule that authorised it.** Including — especially
   — refusals. A blocked attempt is evidence the stopping rules are real.
3. **Fail closed.** A precondition that can't be evaluated blocks the action. An
   unknown decline with low confidence is treated as hard. The safe direction of
   error is always "do less".
4. **The model supplies judgment; the policy engine supplies permission.** A model
   response never widens a bound. True whatever the provider is.
5. **Every reasoning task registers a deterministic fallback.** A task without one
   fails at **startup**, not at runtime. The project must complete a full batch
   run with no API key at all — that is a feature, not a degraded mode.
6. **Nothing model-derived appears anywhere without a `provenance` object**, and
   metrics are reported **per source**. Never blend reasoned and ruled results
   into one figure that implies more than it delivers.
7. **Test-mode Razorpay keys only.** Never commit `.env`, and never put a real
   credential in `.env.example` — it is a committed file. If a live key ever
   appears anywhere, stop and rotate it.
8. **Reproducibility.** Every batch is seeded; every reported number carries its
   `batch_id` and seed. Never cherry-pick a batch — an honest smaller number beats
   an unverifiable bigger one.
9. **Report honestly.** If tests fail, say so with output. If a step was skipped,
   say that. The "Build Challenges" write-up comes from real git history — which
   makes the git log a submission artifact, so commit messages must be true.
