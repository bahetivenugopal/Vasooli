# Running Vasooli

Every command, every environment variable, and the no-keys mode. The five-step
quickstart is in the [README](../README.md#quickstart); this is the full
reference behind it.

---

## The launcher — `run.bat` (Windows)

One command that gets a clean clone to a running product, and explains itself
when it cannot.

| Command | What it does |
| --- | --- |
| `run.bat` | Preflight, repair, run all three engines, verify the numbers, start the API and dashboard, open the browser |
| `run.bat demo` | The same up to and including verification. No servers |
| `run.bat check` | The doctor alone. Reports every problem and its fix, repairs nothing, starts nothing |
| `run.bat stop` | Stops the API and dashboard |

A `.bat` rather than a `.ps1` deliberately: PowerShell's execution policy blocks
unsigned scripts on a locked-down machine, and a reviewer should not have to
argue with their own laptop to run this.

### What it checks, and what it does about each

The checks live in [`scripts/preflight.py`](../scripts/preflight.py), which is
standard-library-only — it runs *before* dependencies exist, which is most of
its job. Run it directly on any platform: `python scripts/preflight.py --no-fix`.

| Check | If it fails |
| --- | --- |
| A Python 3.12 interpreter (`py -3.12`, else `python`) | **Blocks** — names the version found and why 3.12 is pinned ([ADR 0001](adr/0001-pin-python-3-12.md)) |
| `apps/api/.venv` exists *and runs* | **Repairs** — creates it; a venv that exists but errors is rebuilt rather than trusted |
| The 8 runtime dependencies import | **Repairs** — `pip install -r apps/api/requirements.txt`, announced because it is slow |
| `.env` exists | **Repairs** — copies `.env.example`, whose defaults need no keys |
| `CORS_ORIGINS` covers `http://127.0.0.1:3000` | **Warns** — the exact cause of every page showing its error state against a healthy API. Not auto-repaired: rewriting an edited `.env` is intrusive |
| The three sample datasets are present | **Repairs** — regenerates them at seed 42, byte-identical to the committed ones |
| Node and npm are on PATH | **Warns** — skips the dashboard and says so. Everything else runs without them |
| `apps/web/node_modules` | **Repairs** — `npm install`, announced because a first run takes minutes |
| Ports 8000 and 3000 are free | **Blocks** — names the owning PID and the `taskkill` line. Killing a stranger's process is not the launcher's to do |

Exit codes: `0` ready, `1` blocked, `2` ready without the dashboard.

### Two things it handles that are easy to get wrong

**The second run.** The unified run id is derived from the seed, so a second run
of seed 42 against a database that already holds it is refused by design — which
is exactly what a reviewer hits on their second launch. The launcher asks
`preflight.py --run-plan` what the database needs: the first run gets the
canonical id the docs quote, later ones get a `--salt`, and it says which and
why. Metrics are unchanged; only the id moves.

**Waiting for the services.** It polls the ports rather than sleeping a guessed
number of seconds, so the browser never opens on a socket that is not listening
yet. `run.bat stop` identifies the services by the port they hold, not by window
title — a launcher started without a console spawns processes with no title to
match — and only ever kills `python.exe` or `node.exe`, naming anything else and
leaving it alone.

### What it does not do

Closing the launcher window does not stop the services; the two spawned windows
own them. Close those, or run `run.bat stop`.

There is no macOS/Linux counterpart yet. `preflight.py` is platform-neutral and
runs anywhere, so the checks are available on every platform — only the
orchestration around them is Windows-only. The manual path below works
everywhere.

---

## Running without keys

**A full run completes with no credentials of any kind, and says so.** Leave
`GEMINI_API_KEY` blank (or set `LLM_DETERMINISTIC_ONLY=true`) and every reasoning
task takes its **registered deterministic fallback** instead of calling a
provider. Every fallback result is audited as `source: deterministic`, so it can
never be mistaken for model reasoning, and the run's first screen states which
mode it is in before any number appears.

This is a supported mode, not a degraded one — a reasoning task without a
registered fallback fails at **startup**, not mid-batch. Razorpay is likewise in
deterministic simulated mode by default (`RAZORPAY_MODE=simulated`,
[ADR 0004](adr/0004-razorpay-simulated-mode.md)), so no network call is needed
anywhere.

What changes with a key: Engines 1 and 2 recover *exactly the same money* either
way — the model diagnoses and drafts there, and the policy engine decides. Engine
3 depends on it entirely, because reading a customer's sentence is the task.

| | Live provider | No provider |
| --- | ---: | ---: |
| Recovered / at risk | ₹94.90 L / ₹2.41 Cr | ₹1.96 L / ₹1.49 Cr |
| Rate | 39.31% | 1.32% |
| Audited decisions | 348 | 318 |
| Integrity audit | PASS | PASS |

---

## Environment variables

Copy `.env.example` to `.env` — the committed defaults work as-is, with no keys.
Every variable is documented inline in that file; this is the summary.

| Variable | Purpose |
| --- | --- |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Razorpay **test-mode** credentials |
| `RAZORPAY_MODE` | `simulated` (default, offline) or `test` (real test-mode HTTP) |
| `RAZORPAY_WEBHOOK_SECRET` | Optional, webhook signature verification |
| `GEMINI_API_KEY` | Reasoning provider credential. Blank ⇒ deterministic mode |
| `GEMINI_MODEL` | Defaults to `gemini-3.1-flash-lite` |
| `LLM_DETERMINISTIC_ONLY` | Skip the provider entirely, use fallbacks throughout |
| `LLM_CACHE_PATH` | Where cached provider responses are persisted |
| `DATABASE_URL` | SQLite path; swap for Postgres later |
| `DEMO_SEED` | Default seed for the synthetic batch generator |
| `CORS_ORIGINS` | Comma-separated origins the dashboard may call the API from |
| `NEXT_PUBLIC_API_BASE_URL` | Where the dashboard looks for the API. Inlined at build time |

**Never commit `.env`, and never put a real credential in `.env.example`** — it
is a committed file. Test-mode keys only, always. If a live key ever appears
anywhere, stop and rotate it.

---

## Backend commands

Run from the repo root unless noted.

| Command | What it does |
| --- | --- |
| `python scripts/unified_demo.py --seed 42` | **The one command.** All three engines, one consolidated report, with its own integrity verdict. `--deterministic` forces every fallback; `--json <path>` also writes the machine-readable report |
| `python scripts/audit_check.py --latest --integrity` | The twelve audit-schema validations plus the cross-source metric comparison. Exits non-zero on any violation |
| `python scripts/root_cause_demo.py` · `mandate_demo.py` · `receivables_demo.py` | One engine at a time, with that engine's full detail |
| `python -m data.generators.cli all --seed 42` | Regenerate the synthetic datasets |
| `python scripts/generate_api_types.py` | Regenerate `packages/shared-types/` from the OpenAPI schema |
| `uvicorn app.main:app --reload` (from `apps/api/`) | The API — health at `/api/v1/health`, docs at `/docs` |
| `pytest` (from `apps/api/`) | The test suite — `policy_engine.py` sits at 99% coverage |

---

## Frontend commands

From `apps/web/`.

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server with hot reload |
| `npm run build` / `npm run start` | Production build and serve |
| `npm run check` | Prettier, ESLint, Vitest and the production build, in order |
| `npm run screenshots` | Capture every surface into `docs/screenshots/` |

**Why `127.0.0.1` and not `localhost`.** `uvicorn` binds IPv4 loopback by default
while some browsers resolve `localhost` to IPv6 first, and the failure mode is
every page showing its error state against a perfectly healthy API. Point the
dashboard elsewhere with `NEXT_PUBLIC_API_BASE_URL`, and whatever origin the
browser ends up using must appear in `CORS_ORIGINS`.

**The dashboard never computes a metric.** Every figure it shows is recomputed
from the audit trail by the API and formatted for display — money formatting
lives in exactly one utility, and the cross-engine headline is summed
server-side in `app/services/overview.py`. Two places that compute a number are
two numbers that eventually disagree, and one of them is the wrong one.

Types come from the backend rather than being hand-written beside it, so
regenerate them after any change to a Pydantic model or a route:

```bash
python scripts/generate_api_types.py   # OpenAPI -> packages/shared-types/
```

---

## Regenerating the datasets

All three engines run on seeded, reproducible synthetic batches. Reference
batches are committed in [`data/samples/`](../data/samples/) so anyone can
inspect the exact rows every reported number was computed from.

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
scored against. Ground truth lives in the manifest and never in a record, so an
engine cannot read the answer it is being tested on.

Full field-by-field documentation is in
[`data/DATA_CARD.md`](../data/DATA_CARD.md).

---

## Reproducing the reported metrics

```bash
python scripts/unified_demo.py --seed 42                  # the headline
python scripts/unified_demo.py --seed 42 --deterministic  # with no provider
python scripts/audit_check.py --latest --integrity        # verify it
```

The unified run id is derived from the seed, so two people comparing "seed 42"
are comparing the same run. Wall times and batch ids differ between runs; every
number is identical — asserted in the test suite against a fingerprint of every
metric, and verified by re-running the live path and diffing the reports.

To confirm the whole surface is behaving, walk
[`smoke-checklist.md`](smoke-checklist.md) — every route with data, without
data, and with the API down.

---

## When it doesn't run

Troubleshooting for the common failures is in
[`REVIEWER-GUIDE.md` §7](REVIEWER-GUIDE.md#7-when-it-doesnt-run).
