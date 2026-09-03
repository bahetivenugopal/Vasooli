# Vasooli

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**AI-first revenue recovery.** Built for the Razorpay Buildathon — **AI Revenue
Recovery** track.

> ### 🚧 Status: shared core, data foundry and Engine 1 built
>
> The shared core — policy engine, reasoning layer, audit trail, Razorpay client
> — the seeded synthetic data generators with committed sample batches, and
> **Engine 1 (Root-Cause Recovery)** are in place. Its measured results, false
> positives included, are in
> [`docs/metrics/engine-1-root-cause.md`](docs/metrics/engine-1-root-cause.md).
> Engines 2 and 3 and the dashboard are **not built yet**; they land as separate
> phases, each with its own check and commit. Nothing in this README claims a
> capability that exists only as a plan.

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

## Getting started

```bash
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli
cp .env.example .env          # then fill in your test-mode keys
```

**You can run this with no API keys at all.** Set `LLM_DETERMINISTIC_ONLY=true`
(or just leave `GEMINI_API_KEY` blank) and every reasoning task takes its
registered deterministic fallback instead of calling the provider. A full batch
run completes either way, and every fallback result is audited as
`source: deterministic` so it is never mistaken for model reasoning.

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

**All data in this project is synthetic.** Nothing here touches a real customer,
a real invoice, or a real rupee — Razorpay is used in test mode only.

**API** (from `apps/api/`) — requires **Python 3.12**:

```bash
python -m venv .venv
.venv\Scripts\activate         # Windows
source .venv/bin/activate      # macOS / Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

→ health check at `http://localhost:8000/api/v1/health`, docs at `/docs`.

**Web** (from `apps/web/`):

```bash
npm install
npm run dev
```

→ `http://localhost:3000`

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
                   policy_engine · audit_trail
apps/web/          Next.js control tower dashboard
data/generators/   seeded, reproducible synthetic batches + configs
data/samples/      committed sample batches (data + manifest)
data/DATA_CARD.md  every distribution, its rationale, and the limitations
docs/adr/          architecture decision records
docs/pitch/        video script and submission answers
```

## On the numbers

Every recovery figure this project reports is computed from its own **seeded,
reproducible** synthetic batch, and is always published with its `batch_id` and
seed so it can be regenerated and checked. No batch is cherry-picked. A number
that can be verified is worth more than a bigger one that cannot.

Figures are also reported **per source**. Every reasoning result records whether
it was *reasoned* by the model or *ruled* by a deterministic fallback, and the
two are never blended into a single figure that implies more than it delivers.

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
