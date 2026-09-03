# Phase 2 — Synthetic Data Foundry

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the seeded, reproducible synthetic data generators that feed all three engines: a payments stream (with realistic decline-code distributions and an injectable corridor degradation event), a mandate/subscription book, and a B2B invoice ledger with customer reply text. Produce committed sample batches so a judge can inspect the exact data every reported number was computed from.

---

## 2. Why this phase comes second

Every metric this project reports is computed from this data, so the data's honesty *is* the project's honesty. If the batches look tuned to make the agent look good, every number afterwards is worthless — and a sharp panelist will probe exactly this. Building the generators before the engines also means the engines are developed against data they cannot have been overfit to, which is a genuinely stronger position than generating data after the fact to suit what was built.

There's a second reason: the failure-mode distribution defined here dictates what the engines must handle. Defining it first turns Phases 3–5 into "handle these known cases correctly" rather than open-ended design.

---

## 3. Preconditions

- Phase 1 is complete, committed, and its acceptance criteria all passed.
- The shared data models from Phase 1 exist (batch-run model, base recoverable-entity concept, money stored as integer paise).
- `.claude/skills/decline-taxonomy/SKILL.md` and `rbi-mandate-rules/SKILL.md` are populated — the generators must produce failure reasons that exactly match the taxonomy's vocabulary, or the engines will silently fail to classify them.

---

## 4. Scope

### In scope
- Generators for three datasets: payment attempts, mandates/subscriptions, B2B invoices (with customer replies).
- A shared seeding/config mechanism so any batch is exactly reproducible from a seed plus a config.
- Committed sample batches in `data/samples/`.
- A documented "data card" describing every distribution and why it was chosen.
- Tests proving reproducibility and distribution correctness.

### Out of scope
- Any recovery logic, detection logic, or scoring. Generators produce data; they never judge it.
- Ground-truth *recovery* outcomes. Whether a retry succeeds is decided at engine-run time (section 5.5), not pre-baked into the dataset.
- Frontend rendering of the data — Phase 6.

---

## 5. Design specification

### 5.1 Reproducibility contract

Non-negotiable, because it's the credibility backbone:

- Every generator takes an explicit integer seed. Same seed + same config = byte-identical output, always.
- Configs live as version-controlled files (YAML or JSON) in `data/generators/configs/`, not as function arguments buried in code. A judge should be able to read the config and know exactly what world was simulated.
- Every generated batch is written with a manifest: seed, config used, generator version, timestamp, row counts, and a hash of the output. The manifest travels with the data.
- Add a test that generates a batch twice from the same seed and asserts identical output.

### 5.2 Payments dataset (feeds Engine 1)

A time-ordered stream of payment attempts across a simulated merchant's traffic.

Each record needs at minimum: attempt id, timestamp, amount (paise), payment method (`upi`, `card`, `netbanking`, `wallet`), issuing bank / PSP handle, an acquirer or route identifier, status (`success` / `failed`), and — on failure — a decline reason drawn **strictly from the decline-taxonomy vocabulary**.

Distribution requirements:

- **Baseline success rate** in a realistic band (roughly 85–92% overall), varying by method — UPI and cards should not have identical baselines.
- **Failure mix dominated by soft declines**, consistent with the research grounding this project: soft declines should be the clear majority (~80%+) of all failures, with hard declines (expired card, closed account, revoked mandate) a smaller tail.
- **Traffic shape over time** — attempts should not be uniformly distributed across the day. Give it a plausible diurnal curve so that a naive "failure count spiked" detector would produce false positives, and only a rate-based detector works. This is a deliberate difficulty: it forces Engine 1 to be genuinely good rather than trivially correct.

**Corridor degradation injection** — the core feature of this dataset:

- The config must support injecting one or more degradation events: a specific (bank × method) or (route) combination whose success rate drops sharply for a bounded time window.
- The injected event is recorded in the manifest as **ground truth** — which corridor, which window, what the degraded rate was. Engine 1's detection is later scored against this. Ground truth must never be visible in the transaction records themselves, only in a separate manifest the engine does not read.
- Generate at least one **decoy**: a corridor with naturally low volume where random variation *looks* like degradation but isn't. If Engine 1 flags it, that's a false positive, and the project should report it honestly rather than pretend it doesn't happen.

### 5.3 Mandates dataset (feeds Engine 2)

A book of recurring mandates with scheduled debits and failures.

Each mandate needs: mandate id, customer id, amount per cycle (paise), frequency, registration date, AFA-registration status, mandate status (`active` / `paused` / `revoked`), next debit date, and a history of debit attempts with outcomes and failure reasons.

Requirements grounded in `rbi-mandate-rules`:

- **Amount distribution must straddle the ₹15,000 AFA threshold** — a meaningful population above and below it, so Engine 2's threshold branching is genuinely exercised rather than theoretically present.
- Include mandates in every state the policy engine must handle: healthy, first-failure, repeat-failure, revoked mid-sequence, and paused.
- **Pre-debit notification records** — each scheduled debit carries whether/when a pre-debit notification was sent. Include cases where it was sent correctly, sent late, and not sent at all, because "the notification window wasn't respected" is a real and distinct failure class Engine 2 must catch.
- Failure reasons must span the taxonomy: insufficient funds, bank timeout, AFA/authentication failure, mandate revoked, account closed. The soft/hard split should mirror the payments dataset's realism.
- Include at least a few mandates that will **hit the attempt cap** during a run, so the stopping rules are visibly exercised.

### 5.4 Invoices dataset (feeds Engine 3)

A B2B receivables ledger.

Each invoice: invoice id, customer id, customer name, amount (paise), issue date, due date, days overdue, payment terms, current status, and a communication history.

Requirements:

- **Ageing buckets** spread realistically (current, 1–30, 31–60, 61–90, 90+ days overdue), weighted so most overdue value sits in the earlier buckets.
- **Customer archetypes** that differ in behavior, not just in numbers — a reliably-slow-but-always-pays account, a first-time-late account, a chronically-delinquent account, a disputing account. Engine 3's escalation decisions should be able to differ meaningfully between them.
- **Customer reply text** — this is the most important part of this dataset, because it is what Engine 3's Claude-powered promise-to-pay extraction operates on. Generate realistic free-text replies covering:
  - Explicit promises with clear dates ("we'll process this on the 12th")
  - Vague or hedged promises ("should be sorted by end of month", "next week sometime")
  - Conditional promises ("once our client pays us")
  - Disputes ("this was already paid", "the amount is wrong")
  - Non-responses and out-of-office replies
  - Replies in **Hinglish / code-mixed English**, since this is an Indian B2B context and it makes the extraction task genuinely non-trivial rather than a regex exercise
- Each reply carries a hidden ground-truth annotation (promise: yes/no, extracted date if any, is-dispute flag) in the manifest — not in the record the engine reads. This lets Engine 3's extraction accuracy be measured honestly.
- Include **broken promises**: replies promising a date, where the payment does not arrive by that date. Engine 3's whole differentiation is escalating only broken promises, so the data must contain them.

### 5.5 What generators must NOT decide

Generators produce the *world*; engines produce the *outcome*. Specifically:

- Do not pre-decide whether a retry will succeed. Instead, the config defines a **retry success probability model** — e.g. a soft decline of type X retried after Y hours succeeds with probability Z — which the engine's simulated execution samples from at run time, seeded. This keeps the engine's measured recovery rate an honest consequence of its decisions rather than a number baked into the data.
- Document this probability model explicitly in the data card. It is a modelling assumption, and stating it plainly is far stronger than hiding it. A judge asking "how do you know the retry would have worked?" should get a straight answer: it's a documented, seeded simulation, and here are the assumptions.

### 5.6 Sample batches and the data card

- Commit at least one reference batch per dataset to `data/samples/`, small enough to browse in GitHub (a few hundred rows), plus the manifest for each.
- Write `data/DATA_CARD.md` documenting: every dataset, every field, every distribution and its justification, the injected degradation events, the retry-success probability model, and an explicit statement of what is synthetic and what real-world sources informed the distributions. State limitations honestly — a data card that admits its assumptions is more credible than one that doesn't.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| All three generators | `data-synthesizer` | `decline-taxonomy` (failure vocabulary), `rbi-mandate-rules` (mandate states, thresholds, notification windows) |
| Reproducibility tests, distribution tests | `test-engineer` | — |
| Data card | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

The `data-synthesizer` agent must not invent decline reasons. Every failure string it emits must exist in the decline taxonomy. If the taxonomy lacks a reason the data genuinely needs, extend the skill file first, then use it.

---

## 7. Testing requirements

1. **Reproducibility** — same seed twice produces identical output, for all three generators. Non-negotiable.
2. **Vocabulary conformance** — every failure reason emitted appears in the decline taxonomy. Fail the test if any unknown string appears.
3. **Distribution assertions** — soft/hard decline ratio within expected bounds; mandate amounts populate both sides of the ₹15,000 threshold; invoice ageing buckets non-empty; overall success rate within the configured band.
4. **Ground-truth separation** — assert that degradation ground truth and reply annotations are absent from the records the engines will read. A test that catches leakage here prevents an embarrassing "your agent already knew the answer" question later.
5. **Manifest integrity** — hash in manifest matches the generated output.

---

## 8. Acceptance criteria

- [ ] `ruff check` clean; `pytest` passes.
- [ ] Each of the three generators runs from a documented CLI command and writes output plus manifest.
- [ ] Regenerating any committed sample from its manifest's seed reproduces it exactly.
- [ ] Payments dataset contains at least one injected corridor degradation and at least one decoy, both recorded in the manifest only.
- [ ] Mandates dataset spans both sides of the ₹15,000 threshold and includes revoked, paused, and cap-reaching cases.
- [ ] Invoices dataset includes all reply categories listed in 5.4, including Hinglish and broken promises, with annotations held out of the engine-visible records.
- [ ] `data/DATA_CARD.md` exists and documents every distribution and the retry-success probability model.
- [ ] Sample batches are committed and browsable in GitHub.

---

## 9. Documentation to produce

- `data/DATA_CARD.md` as specified above.
- An ADR recording the decision to simulate retry outcomes via a documented probability model rather than pre-baking them, and why that is the more honest choice.
- Update `README.md` with a short section on how to regenerate the datasets.

---

## 10. Commit checklist

- `feat(data): add seeded payments generator with corridor degradation injection`
- `feat(data): add mandate book generator with AFA threshold coverage`
- `feat(data): add invoice ledger generator with customer reply text`
- `test(data): assert reproducibility, vocabulary conformance, and ground-truth separation`
- `docs(data): add data card and regeneration instructions`

Commit sample batches alongside the generator that produced them. Confirm no `.env`, no `*.db`, and no oversized files are staged.

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: what each dataset contains, the actual measured distributions (not the intended ones), where ground truth lives, and any assumption you had to make that isn't yet documented in the data card.

Do not begin Phase 3 and do not write any detection or recovery logic. The next phase file will be provided separately.
