# Vasooli

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**AI-first revenue recovery.** Built for the Razorpay Buildathon — **AI Revenue
Recovery** track.

---

## The result

> ### ₹94.90 lakh recovered of ₹2.41 crore at risk — **39.31%** — across **348**
> ### audited decisions with **zero** schema violations.

One seeded run of all three engines, reproduced by one command:

```bash
python scripts/unified_demo.py --seed 42
```

**How that number is produced.** Every figure is recomputed from an append-only
audit trail at read time — no counters, no snapshots, no arithmetic in the
browser. The run then audits *itself*: it recomputes every headline metric a
fourth time from raw audit rows, using code that shares no path with the
reporting layer, compares the three sources, runs twelve schema validations and
two traceability checks, and **exits non-zero if anything disagrees**.

**What it is not.** The data is synthetic and seeded, retry outcomes are drawn
from a probability model we wrote and documented, and no message is ever actually
sent. The blended rate is a *breadth* figure across three leak points that
measure exposure differently — each engine ships its own definition of what its
recovery number means, inside the API response. Every assumption is in
[**Honest limitations**](#honest-limitations) below, and the full measured output
with its methodology is in [`docs/RESULTS.md`](docs/RESULTS.md).

**And it runs with no API keys at all**, on registered deterministic fallbacks,
reporting the mode on the first screen. See
[Running without keys](#running-without-keys).

| | Live provider | No provider |
| --- | ---: | ---: |
| Recovered / at risk | ₹94.90 L / ₹2.41 Cr | ₹1.96 L / ₹1.49 Cr |
| Rate | 39.31% | 1.32% |
| Audited decisions | 348 | 318 |
| Integrity audit | PASS | PASS |

---

## What it is

Businesses lose revenue in three distinct, quietly compounding ways: a payment
corridor degrades and nobody notices, a recurring mandate or subscription charge
fails and the customer never meant to churn, or a B2B invoice goes overdue and
just sits there. Vasooli is one shared recovery engine — one policy layer, one
Gemini-powered reasoning layer, one audit trail — applied to all three leak
points, so every recovery action is bounded, explainable, and provably compliant,
not just "an agent that tries stuff".

## Why this shape

Most entries will build one demo around one of the track's example directions.
Vasooli builds the **shared recovery infrastructure that spans the track's own
full breadth** — payment failures, subscriptions, receivables — with one policy
layer and one audit trail underneath all three.

Three unified capabilities absorbing **five** of the track's seven example
directions:

| Engine | Absorbs | Does |
| --- | --- | --- |
| **1 · Root-Cause Recovery** | payment degradation → root cause → recovery | Diagnoses whether a decline is *one customer's problem* (insufficient funds → dun that customer) or a *corridor-wide problem* (an issuing bank's rail degrading → reroute all affected traffic). Same decline code, opposite correct responses |
| **2 · Mandate & Subscription Recovery** | mandate retry sequencer + failed-subscription recovery | Classifies each failed recurring charge soft vs hard; retries soft declines inside RBI's compliance windows; sends hard declines straight to dunning instead of burning attempts |
| **3 · B2B Receivables Chaser** | receivables chaser + promise-to-pay tracker | Escalating-but-never-harassing reminders, extracts "I'll pay by ⟨date⟩" commitments from free-text replies, and escalates only **broken** promises, on a capped ladder |

The distinction doing the most work is **soft vs hard decline**. A soft decline is
temporary — the instrument is still valid — and a well-timed retry can succeed.
A hard decline is permanent, and retrying wastes attempts while risking issuer
fraud filters. Roughly **80%+ of declines are soft**, and treating them all
identically is the most common and most expensive mistake real systems make.

## Architecture

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

**The one structural fact:** an engine's own code is almost entirely about *what
data it ingests* and *what recovery actions it may call*. The judgment — is this
retryable, what's the root cause, what's the next bounded action, log it — always
routes through the same `policy_engine.py`, `llm_agent.py`, and
`audit_trail.py`. The hard part is built once.

More detail in [docs/architecture.md](docs/architecture.md).

## Stack

| Layer | Choice |
| --- | --- |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind, shadcn/ui, Recharts |
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| Database | SQLite via SQLAlchemy (zero-setup; connection-string swap for Postgres later) |
| Reasoning | Google Gemini (`gemini-3.1-flash-lite`) via `google-genai`, behind a thin provider interface, with a deterministic fallback per task |
| Payments | Official `razorpay` Python SDK — **test-mode keys only, always** |
| Testing | Pytest, focused on `policy_engine.py` |
| Lint | Ruff · ESLint + Prettier |

## Quickstart

Verified from a clean clone in an empty directory. Five steps, no API keys
required, about two minutes.

```bash
# 1. Clone
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli

# 2. Configure — the defaults work as-is, with no keys
cp .env.example .env

# 3. Install (Python 3.12 required — see .python-version and ADR 0001)
cd apps/api
python -m venv .venv
.venv\Scripts\activate         # Windows
source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
cd ../..

# 4. Run the whole product — all three engines, one consolidated report
python scripts/unified_demo.py --seed 42

# 5. Verify the numbers it just printed
python scripts/audit_check.py --latest --integrity
```

Step 4 prints the headline, the per-engine contributions, the trust strip, each
engine's own metrics, and its own integrity verdict. The committed sample
datasets are already in the repo, so nothing needs generating first.

To see it in a browser, start the API and the dashboard:

```bash
cd apps/api && uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
cd apps/web && npm install && npm run dev         # http://127.0.0.1:3000
```

The dashboard's front page shows exactly the run you just made — same numbers,
because the console report and the API response are the same object.

### Running without keys

**A full run completes with no credentials of any kind, and says so.** Leave
`GEMINI_API_KEY` blank (or set `LLM_DETERMINISTIC_ONLY=true`) and every reasoning
task takes its **registered deterministic fallback** instead of calling a
provider. Every fallback result is audited as `source: deterministic`, so it can
never be mistaken for model reasoning, and the run's first screen states which
mode it is in before any number appears.

This is a supported mode, not a degraded one — a reasoning task without a
registered fallback fails at **startup**, not mid-batch. Razorpay is likewise in
deterministic simulated mode by default (`RAZORPAY_MODE=simulated`, ADR 0004), so
no network call is needed anywhere.

What changes with a key: Engines 1 and 2 recover *exactly the same money* either
way — the model diagnoses and drafts there, and the policy engine decides. Engine
3 depends on it entirely, because reading a customer's sentence is the task.

Environment variables:

| Variable | Purpose |
| --- | --- |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Razorpay **test-mode** credentials |
| `RAZORPAY_WEBHOOK_SECRET` | Optional, webhook signature verification |
| `GEMINI_API_KEY` | Reasoning provider credential. Blank ⇒ deterministic mode |
| `GEMINI_MODEL` | Defaults to `gemini-3.1-flash-lite` |
| `LLM_DETERMINISTIC_ONLY` | Skip the provider entirely, use fallbacks throughout |
| `LLM_CACHE_PATH` | Where cached provider responses are persisted |
| `DATABASE_URL` | SQLite path; swap for Postgres later |
| `DEMO_SEED` | Default seed for the synthetic batch generator |
| `CORS_ORIGINS` | Comma-separated origins the dashboard may call the API from |
| `NEXT_PUBLIC_API_BASE_URL` | Where the dashboard looks for the API. Inlined at build time |

**All data in this project is synthetic.** Nothing here touches a real customer,
a real invoice, or a real rupee — Razorpay is used in test mode only.

### Commands

| Command | What it does |
| --- | --- |
| `python scripts/unified_demo.py --seed 42` | **The one command.** All three engines, one consolidated report, with its own integrity verdict. `--deterministic` forces every fallback; `--json <path>` also writes the machine-readable report |
| `python scripts/audit_check.py --latest --integrity` | The twelve audit-schema validations plus the cross-source metric comparison. Exits non-zero on any violation |
| `python scripts/root_cause_demo.py` · `mandate_demo.py` · `receivables_demo.py` | One engine at a time, with that engine's full detail |
| `python -m data.generators.cli all --seed 42` | Regenerate the synthetic datasets |
| `uvicorn app.main:app --reload` (from `apps/api/`) | The API — health at `/api/v1/health`, docs at `/docs` |
| `pytest` (from `apps/api/`) | The test suite |

The dashboard address is `127.0.0.1` rather than `localhost` on purpose. `uvicorn` binds
IPv4 loopback by default while some browsers resolve `localhost` to IPv6 first,
and the failure mode is every page showing its error state against a perfectly
healthy API. Point the dashboard elsewhere with `NEXT_PUBLIC_API_BASE_URL`, and
whatever origin the browser ends up using must appear in `CORS_ORIGINS`.

From `apps/web/`:

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server with hot reload |
| `npm run build` / `npm run start` | Production build and serve |
| `npm run check` | Prettier, ESLint, Vitest and the production build, in order |
| `npm run screenshots` | Capture every surface into `docs/screenshots/` |

**The dashboard never computes a metric.** Every figure it shows is recomputed
from the audit trail by the API and formatted for display — money formatting
lives in exactly one utility, and the cross-engine headline is summed
server-side in `app/services/overview.py`. Two places that compute a number are
two numbers that eventually disagree, and one of them is the wrong one.

Types come from the backend rather than being hand-written beside it:

```bash
python scripts/generate_api_types.py   # OpenAPI -> packages/shared-types/
```

To confirm the whole surface is behaving, walk
[`docs/smoke-checklist.md`](docs/smoke-checklist.md) — every route with data,
without data, and with the API down.

### What the control tower shows

| Route | Surface |
| --- | --- |
| `/` | The cross-engine headline, per-engine contributions, and the trust strip — the counts of what the system **refused** to do |
| `/engines/root-cause` | Corridor health, detections with observed-vs-baseline rates, and detection accuracy scored against ground truth with false positives shown in full |
| `/engines/mandate-recovery` | Recovery by failure class, both sides of the AFA threshold, compliance blocks by rule, and the upcoming retry schedule |
| `/engines/receivables` | Extraction accuracy with its confusion matrices, the ranked worklist with score breakdowns, and the promise register |
| `/timelines/...` | One corridor, mandate or invoice as a story — what happened, what was decided, **which rule authorised it**, what the model reasoned verbatim, and how it ended |
| `/audit` | The full trail, filterable by run, engine, entity, action, outcome and decision source, with a one-click **policy-denials-only** preset |

Rule-decided steps and LLM-reasoned steps are visually distinct everywhere they
appear, because "the model supplies judgment, the policy engine supplies
permission" is the product's central claim and a claim you have to explain is
not one a judge can check.

## The datasets

All three engines run on seeded, reproducible synthetic batches. Reference
batches are committed in [`data/samples/`](data/samples/) so anyone can inspect
the exact rows every reported number was computed from — and regenerate them:

```bash
# rewrite the committed samples (run from the repo root)
python -m data.generators.cli all --seed 42 --out data/samples

# or verify one without touching them
python -m data.generators.cli payments --seed 42 --out /tmp/check
diff /tmp/check/payments.jsonl data/samples/payments/payments.jsonl   # silent
```

Same seed + same config = byte-identical output. Every batch carries a manifest
with its seed, the resolved config, the generator version, a SHA-256 of the
output, the **measured** distributions, and the ground truth the engines are
scored against — which corridor was really degraded, which reply really
contained a promise. Ground truth lives in the manifest and never in a record,
so an engine cannot read the answer it is being tested on.

[`data/DATA_CARD.md`](data/DATA_CARD.md) documents every field, every
distribution and why it was chosen, the retry-success probability model, and the
limitations. Notably: **no dataset contains a retry outcome.** Whether a retry
would have worked is a documented, seeded probability model the engines sample at
run time ([ADR 0005](docs/adr/0005-simulated-retry-outcomes.md)), not a number
baked into the data.

## Repo layout

```
.claude/           CLAUDE.md (project instructions) + agents, skills
                   and commands — the build's own tooling
apps/api/          FastAPI backend
  app/engines/     one folder per engine
  app/services/    the shared core: razorpay_client · llm_agent
                   policy_engine · audit_trail · overview
                   unified_run · integrity · audit_validation
  app/tests/       pytest — policy_engine.py at 99% coverage
apps/web/          Next.js control tower dashboard
  src/components/ui/        shadcn primitives — never duplicated
  src/components/features/  Vasooli-specific composed components
  src/lib/money.ts          the one paise-to-rupee formatter
packages/shared-types/      TypeScript types generated from the OpenAPI schema
data/generators/   seeded, reproducible synthetic batches + configs
data/samples/      committed sample batches (data + manifest)
data/DATA_CARD.md  every distribution, its rationale, and the limitations
scripts/           unified_demo.py (the one command) · audit_check.py
                   · one demo script per engine
docs/RESULTS.md    the measured unified run and its methodology
docs/metrics/      per-engine measured results, underperformance included
docs/failure-paths.md    ten rehearsed failure paths and how each degrades
docs/adr/          architecture decision records
docs/phases/       the phase plans, and PHASE-LOG.md — the build's own record
docs/REVIEWER-GUIDE.md   run it from a clean clone, and what every screen means
docs/WALKTHROUGH.md      every panel on every screen, and why it exists
docs/screenshots/  interface screenshots of every surface
docs/smoke-checklist.md  the full click-through
```

## Safety and boundedness

The differentiator is not that an agent tries things. It is that **every action
is permitted by a named rule before it happens, and the permission is on the
record afterwards.** Five properties hold that up.

### 1. The model recommends; the policy engine authorises

`llm_agent.py` supplies judgment. `policy_engine.py` supplies permission. A model
recommendation can **narrow** an envelope and can never widen one — enforced
structurally, not by convention: an *allowed* decision can only be constructed by
a module-private function that `authorize()` cannot reach.
([ADR 0003](docs/adr/0003-policy-engine-final-authority.md))

The tell that something bypassed it is an audit entry citing no policy rule —
which the integrity audit now fails on.

**This is not theoretical.** On the first live mandate run the model recommended
retrying a **paused** mandate at confidence 1.00, and retrying four
authentication-blocked mandates at 0.95. Eight refusals in twenty-four drafts,
each citing the specific rule that refused. The model is well calibrated when
asked to *judge* and overconfident when asked to *act* — which is exactly why
permission lives somewhere it cannot reach.

### 2. No invented thresholds

Every bound cites a named rule in one of four skill files, each of which
separates regulatory facts from our own product choices:

| Skill | Covers |
| --- | --- |
| `decline-taxonomy` | soft/hard classification, retry budgets per code |
| `rbi-mandate-rules` | AFA, the pre-debit notice window, the ₹15,000 threshold, revocation |
| `policy-bounds` | attempt ceiling, backoff, quiet hours, contact caps, the chase ladder, escalation triggers |
| `corridor-detection` | corridor definition, detection thresholds, minimum volume, reroute expiry |

Needing a number none of them provides means **adding a named rule with its
rationale first**, then citing it. The audit now verifies that every citation in
a run resolves to a rule that actually exists — a citation pointing at nothing is
worse than no citation, because it looks like rigour.

### 3. Stopping rules, and evidence they fire

Hard attempt ceiling of 4, never raised — a configured override above it is
clamped, not honoured. RBI's mandate cap of 3 with 24h spacing. A 3-rung chase
ladder. Outreach only 09:00–21:00 IST, at most 3 contacts per rolling 7 days.
Revocation and dispute as absolute freezes.

The measured run refused **95** proposed actions, blocked **40** debits on
compliance rules, suppressed **28** retries and **8** messages. A run with *no*
refusals would be suspicious rather than clean, and the audit says so out loud.

### 4. Fail closed

A precondition that cannot be evaluated blocks the action. An unknown decline
below the confidence floor is treated as hard. A malformed dataset record fails
the run rather than being skipped — a run that silently drops what it could not
read reports a rate over a denominator nobody chose. The safe direction of error
is always *do less*.

### 5. Human escalation, and never dispatching

38 decisions in the measured run were handed to a person rather than acted on:
broken promises, exhausted ladders, disputes, high-value cases above ₹50,000.

And **no communication is ever sent to anyone.** Every dunning message and
reminder is drafted, tone-validated against named rules, gated, recorded — and
then stops. ([ADR 0009](docs/adr/0009-communications-are-never-dispatched.md))

## On the numbers

Every recovery figure this project reports is computed from its own **seeded,
reproducible** synthetic batch, and is always published with its `batch_id` and
seed so it can be regenerated and checked. No batch is cherry-picked. A number
that can be verified is worth more than a bigger one that cannot.

Figures are also reported **per source**. Every reasoning result records whether
it was *reasoned* by the model or *ruled* by a deterministic fallback, and the
two are never blended into a single figure that implies more than it delivers.

### Reproducing the reported metrics

```bash
python scripts/unified_demo.py --seed 42                  # the headline
python scripts/unified_demo.py --seed 42 --deterministic  # with no provider
python scripts/audit_check.py --latest --integrity        # verify it
```

The unified run id is derived from the seed, so two people comparing "seed 42"
are comparing the same run. Wall times and batch ids differ between runs; every
number is identical — asserted in the test suite against a fingerprint of every
metric, and verified by re-running the live path and diffing the reports.

Every figure and its methodology is in [`docs/RESULTS.md`](docs/RESULTS.md);
per-engine detail, including what underperformed, is in
[`docs/metrics/`](docs/metrics/); the ten rehearsed failure paths and how each
degrades are in [`docs/failure-paths.md`](docs/failure-paths.md).

## Honest limitations

Stated here rather than left for a reader to find. Every claim above should be
read against this list.

1. **All data is synthetic and seeded.** No real customer, invoice or rupee is
   involved anywhere. Razorpay runs in deterministic simulated mode by default,
   and only ever with test-mode credentials.
2. **Retry outcomes are simulated from a probability model we wrote.**
   `data/generators/retry_model.py`, documented in
   [ADR 0005](docs/adr/0005-simulated-retry-outcomes.md) and `data/DATA_CARD.md`.
   It is the single biggest lever on the recovery numbers, and it is an
   assumption rather than a measurement from production traffic.
3. **No communication is ever dispatched.** Messages are drafted, validated,
   gated and audited. Nothing is sent.
4. **A reroute recovers nothing.** Corridor rerouting is authorised, bounded,
   expiring and audited — but the retry model has no route dimension, so
   crediting rerouted traffic with an uplift would be inventing the headline.
5. **Engine 3's 100% extraction score is a statement about a 20-template
   corpus**, not a production claim. The adversarial probe (twelve hand-written
   replies designed to bait a false positive) is the evidence that generalises
   further — and it is still only twelve replies.
6. **One reply per invoice.** Promise → renege → promise-again does not exist in
   the corpus, so Engine 3 cannot be scored on it.
7. **Two contact caps are tested but never demonstrated.** The ledger arrives
   with most chase ladders already spent, so `QH2` and `RL3` are covered by unit
   tests against the policy engine rather than by a batch run.
8. **Engine 1 stamps its audit entries with wall-clock time** while Engines 2
   and 3 use their run clocks. A known defect: entry *timestamps* are therefore
   not reproducible, which is why determinism is asserted over metrics.
9. **The three engines do not share a run clock**, deliberately — each derives
   `now` from its own data, and each is right for its own dataset. The
   consequence is that a naive newest-first sort across all three would show one
   engine, and the dashboard works around it explicitly.
10. **This is a ~30-hour prototype.** No authentication, no multi-tenancy, no
    background workers, SQLite, one process, not deployed.

## How AI was used to build this

This is an AI-builder submission, so how the repo was built is itself evidence.
The whole thing was built with **Claude Code**, and the tooling that shaped it is
committed in [`.claude/`](.claude/) rather than left in a chat history.

**The work was phased.** Eight phase files, one at a time, each ending in a
verification step and a commit — shared core, synthetic data, then one engine per
phase, then the dashboard, then this integration pass. Building ahead was
explicitly disallowed, because the point of phasing is verifiable, committable
progress. [`docs/phases/PHASE-LOG.md`](docs/phases/PHASE-LOG.md) is the record:
what shipped, what deviated from the plan and why, and the traps a later phase
would otherwise have walked into. It is written **after** each phase, never
before, and it is where the real technical obstacles are recorded.

**Skills carry the domain knowledge.** [`.claude/skills/`](.claude/skills/) holds
the decline taxonomy, the RBI mandate rules, Vasooli's own policy bounds, the
corridor-detection thresholds, the audit schema, the reasoning-provider contract
and the Razorpay reference. Each is loaded *before* touching related code, and
each names every rule so `policy_engine.py` can cite it. This is what makes "no
invented thresholds" enforceable rather than aspirational: a number with no home
in a skill file cannot be written, and adding one means arguing for it in the
skill first.

**Agents are Role / Task / Context framed.** [`.claude/agents/`](.claude/agents/)
— `policy-architect`, `razorpay-integrator`, `data-synthesizer`,
`frontend-builder`, `test-engineer` — each with an explicit remit rather than
vague instructions.

**Commands are the repeatable checks.** `/audit-check`, `/run-batch-demo`,
`/new-engine`. `/audit-check` began as a checklist a human walks and became
executable code in this phase, for the reason given in
[ADR 0013](docs/adr/0013-metric-integrity-as-an-executable-audit.md): a checklist
cannot say which of its twelve validations it skipped.

**Gemini is the product's reasoning layer, not the build's.** Three registered
tasks — diagnose a degraded corridor, draft a dunning message, understand a
customer's reply — each behind a provider interface, each with a registered
deterministic fallback, each producing a `provenance` object that travels with
the result everywhere it appears.

## Compliance grounding

Engine 2 is bounded by India's RBI e-mandate / Additional Factor Authentication
framework, not by generic retry heuristics: one-time AFA at registration, a
pre-debit notification 24–48h before *every* scheduled debit, a ₹15,000
per-transaction threshold above which fresh AFA is required each cycle, and
revocation as an absolute, immediate hard stop.

These are encoded in [`.claude/skills/rbi-mandate-rules/`](.claude/skills/rbi-mandate-rules/SKILL.md),
which separates regulatory facts from Vasooli's own policy choices — so it is
always clear which numbers are imposed and which are ours.

## License

Not yet specified.
