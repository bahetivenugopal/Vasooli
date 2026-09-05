# Vasooli — Reviewer's Guide

> *Revenue doesn't disappear. It goes missing. Vasooli brings it back.*

**Read this if you are about to run, click through and judge this project.** It
gets you from a fresh clone to a running product in about five minutes, walks you
through every screen in the order that makes sense, and tells you what you are
looking at and why it matters.

No architecture deep-dive here. Just: **run it, see everything it does, and
understand how it answers the brief.**

Track: **AI Revenue Recovery**, Razorpay Buildathon.

---

## Contents

1. [The 60-second version](#1-the-60-second-version)
2. [Run it locally](#2-run-it-locally)
3. [The story — one business, one Tuesday](#3-the-story--one-business-one-tuesday)
4. [The guided tour — every screen, in order](#4-the-guided-tour--every-screen-in-order)
5. [How to read the numbers](#5-how-to-read-the-numbers)
6. [Things that look broken and aren't](#6-things-that-look-broken-and-arent)
7. [When it doesn't run](#7-when-it-doesnt-run)
8. [Verify it yourself](#8-verify-it-yourself)
9. [What we are not claiming](#9-what-we-are-not-claiming)

---

## 1. The 60-second version

Businesses lose revenue in three quiet, separate ways:

| The leak | What it looks like | Today's response |
| --- | --- | --- |
| A **payment corridor degrades** | A bank's rail starts failing; success rate quietly drops | Nobody notices for hours. Support retries blindly |
| A **recurring charge fails** | A subscription or e-mandate debit bounces | Blind retries at midnight until the attempts run out, then churn |
| A **B2B invoice goes overdue** | ₹2.4 lakh, 45 days late | An AR executive emails when they remember |

Vasooli is **one recovery engine pointed at all three** — one policy layer, one
Gemini reasoning layer, one audit trail underneath every one of them.

The claim that matters, and the one you should try hardest to break:

> **The model supplies judgment. The policy engine supplies permission.**
> Gemini can narrow what happens next. It can never widen it. And every single
> action — including every refusal — is on the record citing the named rule that
> authorised it.

**The headline, from one seeded command:**

> **₹94.90 lakh recovered of ₹2.41 crore at risk — 39.31% — across 348 audited
> decisions, zero schema violations.**

And it runs **with no API keys at all**, on registered deterministic fallbacks,
and says on screen which mode it is in. That is a designed feature, not a
degraded mode — so you never have to take our word for a number you cannot
reproduce.

---

## 2. Run it locally

**You need:** Python 3.12, Node 18+, git. **You do not need:** a Gemini key, a
Razorpay key, a database, or an internet connection.

### The five steps

```bash
# 1. Clone
git clone https://github.com/bahetivenugopal/Vasooli.git
cd Vasooli

# 2. Configure — the committed defaults work as-is, with no keys
cp .env.example .env

# 3. Install the backend (Python 3.12)
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

Step 4 prints the headline, each engine's contribution, the trust strip, and its
own integrity verdict. **It exits non-zero if any number disagrees with any
other** — a report that prints FAIL and exits 0 is a report CI would wave
through, so this one does not.

Sample datasets are committed, so nothing needs generating first.

### Then open it in a browser

Two terminals:

```bash
# Terminal 1 — the API
cd apps/api && uvicorn app.main:app --reload      # http://127.0.0.1:8000/docs

# Terminal 2 — the dashboard
cd apps/web && npm install && npm run dev         # http://127.0.0.1:3000
```

Open **http://127.0.0.1:3000**. The front page shows exactly the run you just
made in the terminal — same numbers, because the console report and the API
response are literally the same object.

> **Use `127.0.0.1`, not `localhost`.** Uvicorn binds IPv4 loopback; some
> browsers resolve `localhost` to IPv6 first, and then every page shows an error
> state against a perfectly healthy API.

### With a Gemini key (optional)

Put `GEMINI_API_KEY=...` in `.env` and re-run. What changes:

- **Engines 1 and 2 recover exactly the same money either way.** They use the
  model to diagnose and to draft — never for the decision that moves money.
- **Engine 3 changes completely.** Reading a customer's sentence *is* the task,
  so with no provider it honestly abstains and recovers ₹0.

Both runs are real. The dashboard labels every result with which one produced it.

---

## 3. The story — one business, one Tuesday

Keep this story in your head as you click. Every screen in the tour is one beat
of it.

> Meet **Bombay Bloom** — a mid-size flowers-and-gifting company. They sell three
> ways: one-off orders on their website, a ₹1,499/month subscription paid by
> e-mandate, and bulk B2B supply to hotels and offices on 30-day invoices.
>
> Three ways to sell. Three ways to leak money. All of it happens on the same
> ordinary Tuesday, and nobody at Bombay Bloom notices any of it until it is late.

*(The story is an illustration. On screen you will see the synthetic equivalents —
same shapes, generated ids, seeded and reproducible.)*

### 3:40 pm — the corridor goes quiet

One issuing bank's netbanking rail starts failing. Not loudly — the success rate
on that one corridor slides from 92% to 61% while every other corridor is fine.

**Today:** nobody sees it. The dashboard shows "some failures". Support retries
the failed orders one by one, into a rail that is still broken, all evening.

**With Vasooli — Engine 1, Root-Cause Recovery:**

1. It segments traffic into **corridors** (issuer × method × route) and watches
   each one's success rate against its own baseline. Detection here is **purely
   statistical** — no model involved — and a minimum-volume guard refuses to call
   a degradation on a handful of attempts.
2. That corridor trips the threshold. Now the genuinely hard question:
   **is this one customer's problem, or the corridor's?** Both produce the same
   decline code, and the correct responses are *opposite*.
3. **This is the real Gemini call.** It reads the corridor's evidence and returns
   one of: *systemic*, *individual*, or **not enough evidence to say** — and the
   third is a first-class answer, not a failure.
4. Systemic → the policy engine authorises a **bounded, expiring reroute** for
   affected traffic. Individual (one customer, genuinely out of funds) → that one
   customer gets dunned, and the corridor is left alone.
5. On the run you will see, the model returned **confidence 0.45** on one
   genuinely ambiguous corridor — below the 0.60 floor — so the deterministic
   classifier took over and the result is stamped `source: deterministic`. Nobody
   arranged that. It is the safety net firing in front of you.

### 7:15 pm — three subscription debits bounce

Rohit's ₹1,499 debit fails on insufficient funds. Priya's fails because her card
was closed. A third customer revoked their mandate last week.

**Today:** the billing system retries all three, three times each, at midnight,
because that is what the cron does. Rohit might have paid on Thursday. Priya
never will. The revoked one should not have been touched at all.

**With Vasooli — Engine 2, Mandate & Subscription Recovery:**

1. Every failure is classified **soft vs hard**. Roughly 80% of declines are
   soft — temporary, the instrument is still good — and treating them all alike
   is the most expensive mistake real billing systems make.
2. **Rohit (soft):** eligible for retry — but only after the gates. Was the
   RBI-required **pre-debit notice** sent 24–48h before? Is the amount under the
   **₹15,000 AFA threshold**, above which no compliant system may auto-debit
   without fresh authentication? Is it at least 24h since the last attempt? Is
   the 3-attempt RBI cap intact? Pass all four → scheduled. Fail any → **blocked,
   citing the exact rule.**
3. **Priya (hard):** zero retries. Straight to dunning. Gemini drafts the
   message, the tone is validated, and then the outreach gate checks the clock —
   09:00–21:00 IST only, at most 3 contacts per rolling 7 days.
4. **The revoked mandate:** absolute hard stop. Not a preference — a rule.
5. On the measured run: **40 debits blocked on compliance rules** across five
   distinct rules, and **28 wasted attempts avoided**. Those are the numbers to
   look at on this engine.

### 9:00 pm — the hotel account is 45 days late

₹2.4 lakh, invoiced 45 days ago, no payment, no reply.

**Today:** an AR executive chases when they remember, in a tone that depends on
their mood, and nobody records what the customer actually said back.

**With Vasooli — Engine 3, B2B Receivables Chaser:**

1. Every overdue invoice is **scored and ranked** — amount, age, relationship,
   history — and you can see every term of the score.
2. The chase runs on a **capped 3-rung ladder**: polite, firm, final. It never
   goes to rung 4, ever. Each message carries a **Razorpay payment link** so
   paying is one tap.
3. The customer replies in their own words — often Hinglish:
   *"bhai next Friday tak kar denge, thoda cash flow issue hai"*.
4. **Gemini reads it.** Is that a promise? A dispute? A conditional? What date
   does "next Friday" actually resolve to, given the date the reply was sent?
   That is a real language-understanding task, and it is the one place in the
   product where the model is genuinely load-bearing.
5. A promise → the invoice is **deprioritised until that date**. Chasing someone
   who just committed to a date is exactly the harassment the ladder exists to
   prevent.
6. Then arithmetic, not judgment: on the date, was it **kept** or **broken**?
   Kept → closed out. **Broken** → escalate to a human. A **dispute** → the chase
   freezes immediately, no more messages.
7. On the measured run: **12 promises kept, 4 broken, 8 disputes frozen, 29
   escalations** — and **21 reminders withheld** by the caps.

### The thread that runs through all three

Every one of those steps — the reroute, the blocked debit, the held message, the
promise read out of Hinglish, the refusal to touch a revoked mandate — writes
**one audit row**, and every row names the rule that permitted it.

That is what makes this a *system* rather than three demos. Same policy engine.
Same reasoning wrapper. Same trail. Build the hard part once.

---

## 4. The guided tour — every screen, in order

Walk these surfaces in this order. It takes about ten minutes and you will have
seen everything the product does. A panel-by-panel version of this same tour —
every number, and the reason the panel exists — is in
[`WALKTHROUGH.md`](WALKTHROUGH.md).

The nav is deliberately flat — five items, no nesting, active page marked.

---

### 4.1 · `/` — **Recovery overview**

**What it is:** the one screen that has to answer, in three seconds, *how much
was at risk, how much came back, and can I trust it?*

**What you will see, top to bottom:**

| Block | What it tells you |
| --- | --- |
| **The headline** | Recovered / at risk / rate, plus audited decisions, engines reporting, and **schema violations** (should be 0 — non-zero means a row reached the trail without going through the writer, which is a bug hunt, not a metric) |
| **Blended caveat** | On screen, not in a footnote: this is a *breadth* figure across three leak points that measure exposure differently |
| **Batch id badges** | The exact run every number came from, reproducible from its seed |
| **Three contribution cards** | Per engine: recovered, at risk, rate, decisions — and **"What this counts"**, because each engine ships its own definition of recovery |
| **Recovery rate by engine** chart | Rates, not rupees, deliberately. The three engines differ by three orders of magnitude and a money chart renders two of them as a flat line |
| **The trust strip** | *The most important block on the page.* See below |
| **Recent decisions** | The latest entries per engine, each with its authorising rule and a **reasoned / ruled** provenance badge |
| **Run a batch** | Three buttons. Trigger any engine live, with a visible elapsed timer |

**The trust strip** — counts of what the system **refused** to do: policy
denials, compliance blocks, retries suppressed, messages suppressed, human
escalations, deterministic fallbacks, abstentions.

> **A recovery number with no refusals beside it is a system that never said no.**
> On the measured run: 95 denials, 40 compliance blocks, 28 retries suppressed,
> 8 messages suppressed, 38 escalations to a human. Non-zero is the *point*.

**Why it answers the brief:** the brief is revenue recovery. This is the money,
next to the evidence that it was recovered within bounds.

**Do this:** hit "Run a batch" on any engine and watch the headline change. Then
note the seed on the card — the same seed gives the same numbers, always.

---

### 4.2 · `/engines/root-cause` — **Root-cause recovery**

**The leak:** a corridor degrades and nobody notices.

**What you will see:**

- **Corridor health** — the counts across the run's corridors.
- **Diagnoses by determination** — systemic / individual / **insufficient
  evidence**. That third bar is a feature: a reasoning layer that always sounds
  confident will eventually be confidently wrong.
- **Denials by rule** — which stopping rule refused, and how often.
- **Detection performance against ground truth** — scored by **strict corridor
  match**: right method, wrong issuer is a *false positive*, not partial credit.
  On seed 42: **precision 66.7%, recall 100%** — 2 true positives, **1 false
  positive shown in full**, 0 missed. The generator also plants a deliberately
  noisy **decoy corridor**; it was never flagged.
- **Detections and what followed** — each detection with observed vs baseline
  rate, and a link into its timeline.

**Why it answers the brief:** this is the track's "payment degradation → root
cause → recovery" direction, and the systemic-vs-individual call is the single
most genuinely agentic judgment in the build. The same decline code, two opposite
correct answers, decided by a model and gated by a rule.

**Do this:** find the false positive. We report it rather than tuning it away —
the five-seed spread (64% / 70%) is in `docs/metrics/engine-1-root-cause.md`.

---

### 4.3 · `/timelines/corridor/[id]` — **Corridor timeline**

Click any detection to get here.

One corridor as a story: what was observed, what the model diagnosed **in its own
words**, at what confidence, what the policy engine then permitted or refused, and
how it ended.

**This is where you check the central claim.** Read the model's reasoning, then
read the rule that authorised the action. If the action is narrower than what the
model wanted, that is the boundary working — and on the live mandate run it
worked *against* the model twice.

---

### 4.4 · `/engines/mandate-recovery` — **Mandate & subscription recovery**

**The leak:** a recurring charge fails and the customer never meant to churn.

**Read this page in this order** — the compliance blocks first, the rate second.

- **Two denominators, both reported** — the top card, and the whole story of this
  engine. The blended rate is **1.69%**; the **addressable** rate is **30.48%**.
  About 80% of the money at risk sits above the AFA threshold or behind a hard
  stop, where *no compliant system may auto-debit at all*. Neither number is
  quotable without the other, which is why both ship inside the API response.
- **Recovery by failure class** — soft and hard declines recover at completely
  different rates, and a blended figure hides the fact that makes this engine
  work.
- **Above and below the ₹15,000 AFA threshold** — a *regulatory* branch, not a
  product one (`rbi-mandate-rules:A3`). Both sides shown.
- **Which compliance rule refused** — 40 debits, five rules: pre-debit notice
  window (21), AFA threshold (11), paused mandate (5), stale notice (2), AFA
  registration gap (1). **Every bar is an auto-debit a blind retrier would have
  made.**
- **Pre-debit notice status** — derived from the book's own timestamps against
  the RBI window. The engine never reads the generator's labels; it reaches the
  same conclusion independently.
- **The retry schedule** — every mandate, its decided next step, and the citation
  behind it. A refusal names its rule as specifically as a permission does.

**Why it answers the brief:** two of the track's directions at once — the mandate
retry sequencer and failed-subscription recovery — and it is the engine where
"bounded and compliant" stops being a slogan. India's RBI e-mandate framework is
encoded rule by rule, with regulatory facts kept separate from our own product
choices, so it is always clear which numbers are imposed and which are ours.

**Do this:** open any mandate's timeline and find an attempt that was **blocked**.
Read the rule. That is a retry that would have been burned, an issuer fraud
filter that was not tripped, and an attempt still available for when it can
actually work.

---

### 4.5 · `/engines/receivables` — **B2B receivables chaser**

**The leak:** an overdue invoice that just sits there.

- **Recovery by ageing bucket** — older buckets recover worse; reported per
  bucket rather than blended, so the shape is visible rather than asserted.
- **What the ladder decided** — adds up to the worklist exactly, so you can see
  no invoice fell through a branch.
- **Which rule withheld a message** — the direct measure of harassment avoided.
  Escalation triggers sit beside it, because a permitted escalation is not a
  refusal.
- **Reply-understanding accuracy** — three confusion matrices: promise, dispute,
  conditional. Rates are computed over **model-handled replies only**;
  abstentions are reported separately and never folded in.
- **The ranked worklist** — highest priority first, with **every term of the
  score visible**. Deprioritised invoices stay on the list rather than being
  filtered out: *"we chose not to chase this, and here is why."*
- **The promise register** — every commitment the model read out of a customer's
  own words, and what became of it. **The commitment was read by a model; the
  kept/broken status is ruled arithmetic.** Those are different things, and the
  page keeps them apart.

**Why it answers the brief:** the receivables chaser and the promise-to-pay
tracker, both track directions, in one loop — and it is the engine that depends
on real language understanding rather than pattern matching.

---

### 4.6 · `/timelines/invoice/[batch]/[invoice]` — **Invoice timeline**

**If you only open one page in this project, open this one.**

The entire product argument is visible in a single scroll:

> a reminder went out → the customer replied **in their own words** → a model read
> a commitment out of that free text → a rule decided whether the commitment held
> → a gate decided what could happen next.

The reply is shown **verbatim above** the model's reading of it, so you can check
the extraction against the text yourself rather than taking the score on trust.
Find a Hinglish one. Check the date it resolved "next Friday" to.

`/timelines/mandate/...` is the same idea for a mandate: every cycle, every
notice, every attempt, every refusal, in order.

---

### 4.7 · `/audit` — **Audit trail**

Every decision all three engines made, and every one they refused.

- Filter by **run, engine, entity type, entity id, action, outcome, and decision
  source (reasoned vs ruled)** — and **every filter is applied by the API, not in
  the browser**, so a count means what it says.
- **"Policy denials only"** — one click. This is the fastest way to audit the
  project: it shows you only the things the system decided *not* to do, each
  citing the rule that stopped it.
- Append-only. The single permitted mutation is resolving a pending outcome.

**Why it answers the brief:** the brief asks for recovery. Anyone can build an
agent that tries things. The differentiator is that **every action was permitted
by a named rule before it happened, and the permission is on the record
afterwards** — including, especially, the refusals.

**Do this:** click "Policy denials only", pick any row, and follow it. Every one
resolves to a real rule in a versioned rule file. The integrity audit fails the
run if a citation ever points at nothing.

---

## 5. How to read the numbers

**Every figure on every screen is recomputed from the audit trail by the API at
read time.** No counters, no snapshots. The dashboard does not perform a single
arithmetic operation on a metric — two places that compute a number are two
numbers that eventually disagree, and one of them is the wrong one.

Four things worth knowing before you judge a number:

**1. The headline is a breadth figure, not a like-for-like rate.** The three
engines measure exposure differently. It says "this system was pointed at three
leak points; here is what came back across all three". Quote the per-engine
figures, each with its own stated definition, for anything specific. Engine 3
dominates the total simply because a B2B invoice book is three orders of
magnitude larger than a mandate book.

**2. Reasoned and ruled are never blended.** Every result records whether the
model judged it or a deterministic fallback ruled it, and the two are reported
separately everywhere. On the measured run: model 90 entries at 74.91%,
deterministic 258 entries at 1.67%.

**3. Refusals are the good numbers.** Denials, compliance blocks, suppressions
and escalations are evidence the stopping rules are real. A run with zero
refusals would be suspicious, not clean.

**4. Every number carries its batch id and seed.** Nothing is cherry-picked. Seed
42 has been the committed default since the data generator was built.

---

## 6. Things that look broken and aren't

Flagged up front, because each one reads as a bug for about four seconds.

| What you see | Why it is correct |
| --- | --- |
| **Engine 2's rate is 1.69%** | ~80% of that money sits above the AFA threshold or behind a hard stop. **No compliant system may debit it at all.** The addressable rate — money a debit was actually permitted against — is **30.48%**, and both ship together |
| **Every dunning message is "held"** | The default run clock lands at 06:39 IST, outside the 09:00–21:00 outreach window. That is `policy-bounds:QH1` doing its job. To see a message go out: `python scripts/mandate_demo.py --now 2026-08-31T14:30` |
| **Engine 3 scores 100% on extraction** | That is a statement about a 20-template corpus, not a production claim, and we say so. The evidence that generalises is the **adversarial probe** — twelve hand-written replies designed to bait a false positive, where the model produced **zero fabricated promises and three honest abstentions**. It is in `docs/metrics/engine-3-receivables.md` |
| **A corridor detection is a false positive** | Reported, not tuned away. Precision 66.7% on seed 42, with the five-seed spread published |
| **A reroute recovers ₹0** | The reroute is authorised, bounded, expiring and audited — but our retry model has no route dimension, so crediting rerouted traffic with an uplift would be inventing the headline |
| **Engine 3 recovers ₹0 with no key** | Correct. Reading a customer's sentence *is* the task. With no provider it abstains rather than guessing |
| **Timestamps look out of order across engines** | The three engines deliberately do not share a run clock — each derives "now" from its own dataset, because forcing one clock would make two of them silently wrong. The dashboard works around it explicitly rather than sorting naively |

---

## 7. When it doesn't run

Every row below is something that **actually happened while building this**, and
its actual fix. Nothing here is hypothetical.

| What you see | Why | Fix |
| --- | --- | --- |
| **Every page shows "could not reach the API"** but the API is clearly healthy | Uvicorn binds IPv4 loopback; some browsers resolve `localhost` to `::1` first and never reach it | Use **`127.0.0.1:3000`**, not `localhost:3000`. This is the single most common one |
| Same symptom, and you *are* on `127.0.0.1` | A `fetch` rejection means "server down" **or** "CORS refused", and the browser reports neither | `lib/api.ts` names both causes and your page's origin in the error it renders. Read it — then add your origin to `CORS_ORIGINS` in `.env` |
| `ModuleNotFoundError: No module named 'data'` when triggering a run from the dashboard | All three engines lazily import `data.generators.retry_model` from the repo root. Scripts and pytest supply that path; uvicorn does not | Fixed in `core/repo_path.py`, invoked from the FastAPI lifespan. If you see it, you are running uvicorn from somewhere unexpected — start it from `apps/api` |
| Dashboard is empty, every surface says "no runs" | The database starts empty. Nothing is seeded at install time | `python scripts/unified_demo.py --seed 42`, then reload |
| `batch id already exists` on a run that never completed | A leg that died used to leave its `BatchRun` stuck `running` while still holding its id | Now marked `failed` automatically. If you hit it on an older checkout, use a different `--seed` or clear the database file |
| A live run pauses for several seconds, then reports an abstention | Gemini's free tier is **15 requests/minute** and the 43-reply corpus hits it on a cold cache | Working as designed — backoff is 1s/2s/4s, then the registered fallback. The batch completes either way. Re-run and the disk cache makes it near-instant |
| `AFC is enabled with max remote calls…` on stderr | A cosmetic advisory from the `google-genai` SDK, not from this project | Ignore it |
| `DatasetError` naming a file and a line number | A malformed record in a `.jsonl` | Intentional: the run **fails rather than skipping the line**, because a run that silently drops what it could not read reports a rate over a denominator nobody chose |
| A naive-datetime error on write | `db/types.py::UTCDateTime` rejects naive datetimes at the column rather than assuming UTC | Use `app.db.types.utcnow()` |
| Every dunning message shows as "held" | The default run clock lands at 06:39 IST, outside the 09:00–21:00 outreach window. That is `policy-bounds:QH1` doing its job | Not a bug — see §6. To watch one go out: `python scripts/mandate_demo.py --now 2026-08-31T14:30` |
| Prettier reformatted prompt files, manifests and ADRs | Prettier is configured in `apps/web/` and scoped there. Run from the repo root it reaches everything — and a prompt file is part of the LLM cache key, while a manifest is a reproducibility artefact | **Always `cd apps/web` first.** Same for `npm run check` |

---

## 8. Verify it yourself

Everything above is checkable in five commands.

```bash
# The headline, live provider
python scripts/unified_demo.py --seed 42

# The same run with no provider at all — a different, equally honest story
python scripts/unified_demo.py --seed 42 --deterministic

# 12 schema validations + a cross-source metric comparison. Non-zero exit on any violation
python scripts/audit_check.py --latest --integrity

# The datasets are byte-reproducible from their seed
python -m data.generators.cli payments --seed 42 --out /tmp/check
diff /tmp/check/payments.jsonl data/samples/payments/payments.jsonl   # silent

# The test suite — policy_engine.py sits at 99% coverage
cd apps/api && pytest
```

**How the run audits itself.** After producing the report, it recomputes every
headline metric a *fourth* time straight from raw audit rows, using code that
deliberately shares no path with the reporting layer (sharing code would make the
comparison a tautology that passes forever). It compares the sources, runs twelve
schema validations and two traceability checks, and exits non-zero on any
disagreement. Every check is also **negatively tested** — each defect is injected
and the check asserted to catch it, because a validator that has only ever seen
clean data might be returning `passed=True` unconditionally.

One command. One seed. One consolidated report that fails loudly.

---

## 9. What we are not claiming

Stated here rather than left for you to find.

1. **All data is synthetic and seeded.** No real customer, invoice or rupee.
   Razorpay runs in deterministic simulated mode by default, and only ever with
   test-mode credentials.
2. **Retry outcomes are simulated** from a probability model we wrote and
   documented. It is the single biggest lever on the recovery numbers, and it is
   an assumption, not a measurement from production traffic.
3. **No communication is ever dispatched.** Every message is drafted,
   tone-validated, gated, recorded — and then stops. Nothing is sent to anyone.
4. **Engine 3's extraction score is a 20-template result.** The adversarial probe
   is the better evidence, and it is still only twelve replies.
5. **One reply per invoice** in the corpus, so promise → renege → promise-again
   cannot be scored.
6. **This is a ~30-hour prototype.** No auth, no multi-tenancy, no background
   workers, SQLite, one process.

The full list, with everything else we know is weak, is in the README's *Honest
limitations* and in `docs/RESULTS.md` §7.

---

## Where to go deeper

Only if you want to — none of it is needed to run or judge the product.

| Document | What is in it |
| --- | --- |
| [`docs/WALKTHROUGH.md`](WALKTHROUGH.md) | The exhaustive tour — every panel on every screen, and why it exists |
| [`README.md`](../README.md) | The full project overview, stack, and safety argument |
| [`docs/RESULTS.md`](RESULTS.md) | Every measured number and exactly how it was produced |
| [`docs/metrics/`](metrics/) | Per-engine detail, including what underperformed |
| [`docs/architecture.md`](architecture.md) | How the shared core is put together |
| [`docs/adr/`](adr/) | Why each significant decision was made |
| [`docs/failure-paths.md`](failure-paths.md) | Ten rehearsed failure paths and how each degrades |
| [`.claude/skills/`](../.claude/skills/) | Every named rule the policy engine cites, with its rationale |
| [`docs/phases/PHASE-LOG.md`](phases/PHASE-LOG.md) | The build's own record — what shipped, what deviated, and why |

---

*All data in this project is synthetic. Nothing here touches a real customer, a
real invoice, or a real rupee.*
