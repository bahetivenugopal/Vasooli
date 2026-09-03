# Vasooli

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**AI-first revenue recovery.** Built for the Razorpay Buildathon — **AI Revenue
Recovery** track.

> ### 🚧 Status: scaffolding complete, build in progress
>
> Repo structure, agent/skill/command layer, and both app scaffolds are in place.
> The three engines, the shared policy core, and the dashboard are **not built
> yet** — they land as separate phases, each with its own check and commit.
> Nothing in this README claims a capability that exists only as a plan.

---

## What it is

Businesses lose revenue in three distinct, quietly compounding ways: a payment
corridor degrades and nobody notices, a recurring mandate or subscription charge
fails and the customer never meant to churn, or a B2B invoice goes overdue and
just sits there. Vasooli is one shared recovery engine — one policy layer, one
Claude-powered reasoning layer, one audit trail — applied to all three leak
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
                    │  policy engine · Claude agent│
                    │       · audit trail          │
                    └──────────────┬──────────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
        Razorpay test-mode API          Control tower dashboard
```

**The one structural fact:** an engine's own code is almost entirely about *what
data it ingests* and *what recovery actions it may call*. The judgment — is this
retryable, what's the root cause, what's the next bounded action, log it — always
routes through the same `policy_engine.py`, `claude_agent.py`, and
`audit_trail.py`. The hard part is built once.

More detail in [docs/architecture.md](docs/architecture.md).

## Stack

| Layer | Choice |
| --- | --- |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind, shadcn/ui, Recharts |
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| Database | SQLite via SQLAlchemy (zero-setup; connection-string swap for Postgres later) |
| Reasoning | Anthropic Claude, Messages API, real tool-calling |
| Payments | Official `razorpay` Python SDK — **test-mode keys only, always** |
| Testing | Pytest, focused on `policy_engine.py` |
| Lint | Ruff · ESLint + Prettier |

## Getting started

```bash
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli
cp .env.example .env          # then fill in your test-mode keys
```

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

## Repo layout

```
.claude/           CLAUDE.md (project instructions) + agents, skills
                   and commands — the build's own tooling
apps/api/          FastAPI backend
  app/engines/     one folder per engine
  app/services/    the shared core: razorpay_client · claude_agent
                   policy_engine · audit_trail
apps/web/          Next.js control tower dashboard
data/generators/   seeded, reproducible synthetic batches
data/samples/      committed sample batches
docs/adr/          architecture decision records
docs/pitch/        video script and submission answers
```

## On the numbers

Every recovery figure this project reports is computed from its own **seeded,
reproducible** synthetic batch, and is always published with its `batch_id` and
seed so it can be regenerated and checked. No batch is cherry-picked. A number
that can be verified is worth more than a bigger one that cannot.

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
