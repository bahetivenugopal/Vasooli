# Control tower smoke checklist

A click-through of every route, with data present and with data absent. Run it
before recording anything. It takes about ten minutes and it exists because the
things that break a demo are almost never the things the test suite covers —
they are a route that 500s, a chart with no bars, and a number that quietly
disagrees with the API.

> **Everything below was run and passed on 2026-09-04** against the seed-42
> sample batches. Where a step found a real defect, the defect is named — that is
> the useful part of a checklist, not the ticks.

---

## 0. Bring both services up

```bash
# terminal 1 — the API
cd apps/api
.venv/Scripts/python -m uvicorn app.main:app --reload

# terminal 2 — the dashboard
cd apps/web
npm run dev            # or: npm run build && npm run start
```

Then confirm the two can actually talk:

```bash
curl -s localhost:8000/api/v1/health
curl -s -D- -o /dev/null -H "Origin: http://localhost:3000" \
  localhost:8000/api/v1/health | grep -i access-control-allow-origin
```

- [ ] Health returns `200`.
- [ ] The CORS header comes back naming the origin the browser will actually use.

> **Trap, found the hard way.** `fetch` fails identically for "the API is down"
> and "the API refused this origin", and the browser console says `Failed to
fetch` for both. If the dashboard is served from `127.0.0.1:3000` but
> `CORS_ORIGINS` only lists `localhost:3000`, every page shows the error state
> while the API is perfectly healthy. `.env.example` now lists both. A second
> version of the same trap: some browsers resolve `localhost` to IPv6 first, and
> `uvicorn --host 127.0.0.1` is IPv4-only — if pages fail with the API clearly
> up, set `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000` and rebuild.

## 1. Data present

Needs one completed run per engine. **Prefer the unified run** — it makes all
three at once, under one run id, and prints the numbers the dashboard is about to
show, so a disagreement is visible before the browser is even open:

```bash
python scripts/unified_demo.py --seed 42
```

- [ ] The run's own integrity verdict is `PASS` on all four checks, and the
      command exited `0`.
- [ ] The console headline and `/` show the **same** at-risk, recovered and rate.
      They are the same object served two ways, so any difference is a defect,
      not a rounding artefact.
- [ ] The mode line at the top matches what you intended — live provider or
      deterministic. Recording a "live" demo that quietly ran on fallbacks is the
      exact dishonesty the mode line exists to prevent.

Per-engine runs still work, and give more detail on one engine:

```bash
python scripts/root_cause_demo.py  --batch-id smoke-rc
python scripts/mandate_demo.py     --batch-id smoke-mr
python scripts/receivables_demo.py --batch-id smoke-rcv
```

### Overview — `/`

- [ ] The headline recovery figure, the value at risk and the recovery rate all
      render, in recovery-green.
- [ ] **The headline matches the API exactly.** Compare against
      `curl -s localhost:8000/api/v1/overview | python -m json.tool | head -20`.
      Formatting must change the presentation and nothing else.
- [ ] Three engine contribution cards, each with its own recovery definition, its
      seed and its dataset batch id.
- [ ] The recovery-rate chart draws three bars.
- [ ] The trust strip shows seven counts, at least one of them non-zero.
- [ ] Clicking a trust tile expands the by-rule breakdown beneath it.
- [ ] "Recent decisions" contains entries from **all three** engines, not just one.
- [ ] Every recent entry names an authorising rule and a decision source.

### Engine views — `/engines/root-cause`, `/engines/mandate-recovery`, `/engines/receivables`

For each:

- [ ] The run picker lists the runs and names the selected run's seed and dataset.
- [ ] The four shared headline cards render, and `Schema violations` is **0**.
- [ ] Every chart draws bars, not just axes.
- [ ] The engine's own worklist table renders with the row-limit footer stating
      how many of how many are shown.
- [ ] Each row's "Timeline →" link opens the right entity.

Specifically:

- [ ] **Root cause:** precision, recall _and_ the false-positive count are all
      visible, with the unmatched detection printed in full.
- [ ] **Mandate recovery:** both denominators are shown — the blended rate and
      the addressable rate — with the addressable definition beneath them.
- [ ] **Mandate recovery:** the failure-class, AFA, compliance and notice tabs
      each switch and render.
- [ ] **Receivables:** the extraction panel shows the three confusion matrices
      and the by-language split.
- [ ] **Receivables:** the worklist score column expands to show every term.
- [ ] **Receivables:** the promise register shows the customer's words verbatim.

### Timelines

- [ ] `/timelines/corridor/<detection_id>` — the statistics, the model's
      diagnosis verbatim, and the gate's decision.
- [ ] `/timelines/mandate/<batch>/<mandate>` — pick one with
      `?compliance_blocked=true` so an **unhappy path** is on screen.
- [ ] `/timelines/invoice/<batch>/<invoice>` — pick one with a **broken promise**
      (`/promises?status=broken`).
- [ ] On every timeline: reasoned steps are violet with a brain icon, ruled steps
      are grey with a shield icon, and the difference is obvious **without**
      reading the legend.
- [ ] Every step names its authorising rule, including the refusals.
- [ ] "Show decision metadata" expands on a step.

### Audit trail — `/audit`

- [ ] The table renders with the row-limit footer.
- [ ] Each of the seven filters changes the result set.
- [ ] **The denials-only preset**: one click shows only blocked entries, and the
      copy says so.
- [ ] Clicking a row expands the full rationale, the model reasoning where there
      is any, and the decision metadata.
- [ ] Clearing filters restores the full list.

## 2. Data absent

The empty states matter as much as the populated ones — a judge who sees a blank
table assumes a bug.

```bash
# Point the API at a scratch database so every list is genuinely empty.
DATABASE_URL=sqlite:///./empty.db .venv/Scripts/python -m uvicorn app.main:app
```

- [ ] `/` shows "No completed batch runs yet" with three run triggers, not a
      blank page or a stack trace.
- [ ] Each engine view shows "No <engine> runs yet" with a trigger.
- [ ] `/audit` shows an empty state explaining how to get data.

## 3. Failure rendering

- [ ] Stop the API. Reload `/`. A red error panel appears naming both possible
      causes and offering **Retry**; the nav still works.
- [ ] Start the API. Click Retry. The page populates without a reload.
- [ ] Open a timeline for an entity that does not exist. The API's own 404 detail
      is shown, not a blank page.

Captured as `docs/screenshots/09-failure-state-api-unreachable.png`.

## 4. Batch run from the UI

- [ ] On `/`, the three run triggers each carry a pre-filled unique batch id.
- [ ] Click **Run batch**. The button shows a spinner and an elapsed-seconds
      counter — the screen never looks frozen.
- [ ] On completion a green result strip appears with the batch id, the seed, the
      dataset id, at-risk, recovered, rate and policy denials.
- [ ] The overview refreshes and now reports the new run.
- [ ] Run again with the **same** batch id: the API's 409 is shown as a readable
      error, because reusing an id would merge two runs into one set of metrics.

## 5. Not-broken-on-a-narrow-window

Not mobile-perfect — just not broken.

- [ ] At ~1000px wide, the nav wraps rather than overflowing.
- [ ] Wide tables scroll inside their own container; the page body never scrolls
      sideways.

---

## Regenerating the artefacts this checklist depends on

```bash
# Types, after any change to a Pydantic model or a route
python scripts/generate_api_types.py

# The number-fidelity test fixture, after the API's overview shape changes
cd apps/api && .venv/Scripts/python -c "import json,pathlib; \
from fastapi.testclient import TestClient; from app.main import app; \
pathlib.Path('../web/src/lib/__fixtures__/overview.json').write_text( \
json.dumps(TestClient(app).get('/api/v1/overview',params={'recent_limit':9}).json(), \
indent=2, ensure_ascii=False)+'\n', encoding='utf-8')"

# The screenshots, with both servers running
cd apps/web && npm run screenshots
```

The screenshot script refuses to save a page that rendered an error state. That
guard exists because the first run of it quietly produced eight pictures of the
"could not reach the API" panel, and they looked entirely plausible in a
directory listing.

## The automated half

```bash
cd apps/web && npm run check     # prettier + eslint + vitest + next build
cd apps/api && .venv/Scripts/python -m pytest && .venv/Scripts/ruff check app
cd ../.. && python scripts/audit_check.py --latest --integrity
```

`npm run check` covers formatting, lint, the money-formatter boundary tests and
the number-fidelity tests, and the production build. `audit_check.py --integrity`
covers the twelve audit-schema validations, the cross-source metric comparison
and the two traceability checks, and exits non-zero on any violation.

Neither covers anything in section 1 through 5 above — that is what this document
is for. Phase 6's lesson stands: the production build once **type-checked three
pages that 500 on every request**, and only loading them caught it.
