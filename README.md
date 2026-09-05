# Vasooli

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**One recovery engine pointed at three revenue leaks** — a degrading payment
corridor, a failed recurring charge, an overdue B2B invoice — with one policy
layer, one Gemini reasoning layer and one audit trail underneath all three. Every
recovery action is bounded by a named rule, explainable, and on the record.

Built for the **Razorpay Buildathon**, AI Revenue Recovery track.

> **The model supplies judgment. The policy engine supplies permission.**
> Gemini can narrow what happens next. It can never widen it. Every action —
> including every refusal — cites the named rule that authorised it.

---

## The result

> ### ₹94.90 lakh recovered of ₹2.41 crore at risk — **39.31%** — across **348** audited decisions with **zero** schema violations.

One seeded run of all three engines, from one command:

```bash
python scripts/unified_demo.py --seed 42
```

Every figure is recomputed from an append-only audit trail at read time. The run
then audits *itself*: it recomputes every headline metric from raw audit rows
using code that shares no path with the reporting layer, compares the sources,
runs twelve schema validations and two traceability checks, and **exits non-zero
if anything disagrees**.

It also runs **with no API keys at all**, on registered deterministic fallbacks,
and reports which mode it is in before any number appears.

| | Live provider | No provider |
| --- | ---: | ---: |
| Recovered / at risk | ₹94.90 L / ₹2.41 Cr | ₹1.96 L / ₹1.49 Cr |
| Rate | 39.31% | 1.32% |
| Integrity audit | PASS | PASS |

The data is synthetic and seeded, retry outcomes come from a documented
probability model, and no message is ever sent. Full methodology in
[`docs/RESULTS.md`](docs/RESULTS.md); everything we are not claiming in
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

---

## Quickstart

From a clean clone. No API keys required, about two minutes.

**On Windows, `run.bat` does all of it.** It checks everything the project
needs, repairs what it can, runs all three engines, verifies the numbers, and
starts both services — naming the problem *and* its fix for anything it cannot
repair itself:

```bat
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli
run.bat
```

`run.bat check` reports without changing anything, `run.bat demo` skips the
servers, and `run.bat stop` shuts them down. The manual path, which works
everywhere:

```bash
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli
cp .env.example .env                 # the defaults work as-is, with no keys

cd apps/api                          # Python 3.12 — see ADR 0001
python -m venv .venv
.venv\Scripts\activate               # Windows
source .venv/bin/activate            # macOS / Linux
pip install -r requirements.txt
cd ../..

python scripts/unified_demo.py --seed 42            # run all three engines
python scripts/audit_check.py --latest --integrity  # verify what it printed
```

The committed sample datasets are already in the repo, so nothing needs
generating first. To see it in a browser:

```bash
cd apps/api && uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs
cd apps/web && npm install && npm run dev         # http://127.0.0.1:3000
```

The dashboard's front page shows exactly the run you just made — same numbers,
because the console report and the API response are the same object.

Full command and environment-variable reference:
[`docs/RUNNING.md`](docs/RUNNING.md).

---

## The three engines

Three unified capabilities absorbing **five** of the track's seven example
directions:

| Engine | Does |
| --- | --- |
| **1 · Root-Cause Recovery** | Diagnoses whether a decline is *one customer's problem* (insufficient funds → dun that customer) or a *corridor-wide problem* (an issuing bank's rail degrading → reroute all affected traffic). Same decline code, opposite correct responses |
| **2 · Mandate & Subscription Recovery** | Classifies each failed recurring charge soft vs hard; retries soft declines inside RBI's compliance windows; sends hard declines straight to dunning instead of burning attempts |
| **3 · B2B Receivables Chaser** | Escalating-but-never-harassing reminders; extracts "I'll pay by ⟨date⟩" commitments from free-text replies (English and Hinglish); escalates only **broken** promises, on a capped ladder |

The distinction doing the most work is **soft vs hard decline**. A soft decline
is temporary — the instrument is still valid — and a well-timed retry can
succeed. A hard decline is permanent, and retrying wastes attempts while risking
issuer fraud filters. Roughly **80%+ of declines are soft**, and treating them
all identically is the most common and most expensive mistake real systems make.

Engine 2 is bounded by India's RBI e-mandate / AFA framework rather than generic
retry heuristics — one-time AFA at registration, a pre-debit notice 24–48h before
*every* debit, a ₹15,000 per-transaction threshold, and revocation as an absolute
hard stop. Encoded in
[`.claude/skills/rbi-mandate-rules/`](.claude/skills/rbi-mandate-rules/SKILL.md).

---

## How it works

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
routes through the same `policy_engine.py`, `llm_agent.py` and `audit_trail.py`.
The hard part is built once.

Full detail in [`docs/architecture.md`](docs/architecture.md).

### Repo layout

```
run.bat            Windows one-command launcher — checks, repairs, runs, verifies
apps/api/          FastAPI backend — engines/ (one folder each),
                   services/ (the shared core), tests/
apps/web/          Next.js control tower dashboard
packages/          TypeScript types generated from the OpenAPI schema
data/              seeded synthetic generators, committed samples, DATA_CARD.md
scripts/           unified_demo.py (the one command) · audit_check.py
                   · preflight.py (the doctor) · per-engine demos
docs/              results, architecture, ADRs, phase log, walkthroughs
.claude/           the build's own tooling — agents, skills, commands
```

### Stack

| Layer | Choice |
| --- | --- |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind, shadcn/ui, Recharts |
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| Database | SQLite via SQLAlchemy |
| Reasoning | Google Gemini (`gemini-3.1-flash-lite`) behind a provider interface, with a deterministic fallback per task |
| Payments | Official `razorpay` Python SDK — **test-mode keys only, always** |
| Testing | Pytest, focused on `policy_engine.py` (99% coverage) |

---

## Why the numbers hold up

The differentiator is not that an agent tries things. It is that **every action
is permitted by a named rule before it happens, and the permission is on the
record afterwards.**

- **The model recommends; the policy engine authorises.** A model recommendation
  can narrow an envelope and never widen one — enforced structurally, not by
  convention. ([ADR 0003](docs/adr/0003-policy-engine-final-authority.md))
- **No invented thresholds.** Every bound cites a named rule in one of four skill
  files. The integrity audit fails a citation that resolves to nothing.
- **Stopping rules, with evidence they fire.** The measured run refused 95
  proposed actions, blocked 40 debits on compliance rules, suppressed 28 retries
  and 8 messages, and escalated 38 decisions to a person. A run with *no*
  refusals would be suspicious rather than clean.
- **Fail closed.** A precondition that cannot be evaluated blocks the action. An
  unknown decline below the confidence floor is treated as hard. A malformed
  record fails the run rather than being silently skipped.
- **Nothing is ever dispatched.** Every message is drafted, tone-validated,
  gated, recorded — and then stops.
  ([ADR 0009](docs/adr/0009-communications-are-never-dispatched.md))
- **Reported per source, never blended.** Every result records whether it was
  *reasoned* by the model or *ruled* by a fallback, and carries its `batch_id`
  and seed so it can be regenerated and checked.

---

## Documentation

**Start here**

| Document | What is in it |
| --- | --- |
| [`docs/REVIEWER-GUIDE.md`](docs/REVIEWER-GUIDE.md) | **Run it and judge it** — clean clone to running product, then every screen in order |
| [`docs/RESULTS.md`](docs/RESULTS.md) | Every measured number and exactly how it was produced |
| [`docs/RUNNING.md`](docs/RUNNING.md) | Every command, every environment variable, and the no-keys mode |

**Going deeper**

| Document | What is in it |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | How the shared core and each engine are put together |
| [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) | Every panel on every screen, and why it exists |
| [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) | Everything we know is weak, stated plainly |
| [`docs/metrics/`](docs/metrics/) | Per-engine measured results, underperformance included |
| [`docs/failure-paths.md`](docs/failure-paths.md) | Ten rehearsed failure paths and how each degrades |
| [`data/DATA_CARD.md`](data/DATA_CARD.md) | Every field, every distribution, and why it was chosen |
| [`docs/adr/`](docs/adr/) | Why each significant decision was made |
| [`docs/BUILT-WITH-AI.md`](docs/BUILT-WITH-AI.md) | How this repo was built with Claude Code, and the tooling that shaped it |
| [`docs/phases/PHASE-LOG.md`](docs/phases/PHASE-LOG.md) | The build's own record — what shipped, what deviated, and why |

---

## License

Not yet specified.

---

*All data in this project is synthetic. Nothing here touches a real customer, a
real invoice, or a real rupee — Razorpay is used in test mode only.*
