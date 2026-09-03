# Phase 5 — Engine 3: Receivables Chaser & Promise-to-Pay Tracker

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the third engine: chase overdue B2B invoices with an escalating but bounded communication ladder, extract promise-to-pay commitments from free-text customer replies using the LLM, track those promises, and escalate only the ones that actually break — with measured extraction accuracy against Phase 2's held-out annotations.

---

## 2. Why this phase comes fifth

This engine completes the breadth claim — payment failures, subscriptions, and now receivables, all on one core — which is the project's central differentiation. It's also the engine where the LLM does something no rule engine could: reading a hedged, code-mixed, human reply and deciding whether it constitutes a commitment.

And it introduces the one measurement in the project that is a genuine ML metric with a held-out test set: promise extraction accuracy. That's worth taking seriously, because it's the kind of number a technically sharp panelist will trust more than a recovery percentage.

---

## 3. Preconditions

- Phases 1–4 complete, committed, acceptance criteria passed.
- Invoice dataset generates with all reply categories including Hinglish, hedged promises, disputes, and broken promises, with annotations held out of engine-visible records.
- Quiet hours and message-cap rules from Phase 1 are working — this engine leans on them hardest.

---

## 4. Scope

### In scope
- Invoice prioritization by recoverable value and risk.
- A bounded, escalating chase ladder.
- LLM-powered reply understanding: promise extraction, dispute detection, sentiment/intent.
- Promise-to-pay tracking with automatic broken-promise detection.
- Escalation triggered only by broken promises or ladder exhaustion.
- Batch runner with recovery metrics plus extraction accuracy against ground truth.
- API routes for runs, invoices, promises, and communications.

### Out of scope
- Real message dispatch. Same rule as Phase 4: generated, gated, logged, rendered — never sent to a real recipient.
- Legal or collections escalation beyond flagging for human review. The system hands off; it does not threaten.
- Payment execution beyond generating a Razorpay test-mode payment link for the invoice.
- UI — Phase 6.

---

## 5. Design specification

### 5.1 Prioritization

Deterministic scoring, not LLM-driven — this needs to be reproducible and explainable.

Rank overdue invoices by a transparent, config-weighted combination of: outstanding value, days overdue, customer payment history, and whether an active promise already exists. An invoice with a live, unbroken promise should be *deprioritized*, not chased — chasing someone who already committed is exactly the behavior that makes automated collections feel harassing, and avoiding it is a differentiator worth stating out loud.

Output a ranked worklist with the score breakdown visible per invoice.

### 5.2 The chase ladder

An escalating sequence of bounded steps, each authorized by policy.

- Define discrete rungs — for example: gentle reminder, firm follow-up, formal notice, human handoff. Each rung has its own tone, its own minimum interval since the previous contact, and its own preconditions.
- **The ladder only ascends on evidence**, never on a timer alone. A customer who replied with a credible promise does not get escalated just because a week passed.
- **Hard caps**: maximum contacts per invoice, maximum per customer per period (across invoices — a customer with eight overdue invoices must not receive eight messages), and quiet-hours enforcement. The cross-invoice cap is the one that's easy to miss and genuinely matters.
- **Dispute freeze**: once a reply is classified as a dispute, automated chasing stops immediately and the invoice routes to human review. Continuing to chase a disputed invoice is both bad practice and a compliance risk.
- Every rung transition is a policy decision with a named rule, audited.

### 5.3 Reply understanding (LLM)

The engine's most genuinely AI-dependent component. For each customer reply, a structured LLM task returns:

- **Intent classification** — promise, partial promise, dispute, request for information, refusal, non-response, out-of-office.
- **Promise extraction** — whether a commitment exists, the committed date (normalized to an absolute date, resolving relative expressions like "end of month" or "next week" against the reply's timestamp), any committed amount if partial, and a confidence signal.
- **Conditionality flag** — "once our client pays us" is a conditional promise and must be tracked differently from a firm one. Conflating the two is the most common way this feature would be wrong.
- **Dispute detail** — what is being disputed, when a dispute is detected.
- **Stated reasoning**, captured to the audit trail.
- **Explicit abstention** — an "unclear, needs human review" outcome. Forcing a classification on an ambiguous reply is worse than admitting uncertainty, and showing the abstention path working is a trust feature.

Requirements:

- Must handle **Hinglish and code-mixed replies** correctly. This is an explicit test case, not an aspiration.
- Relative date resolution must be tested — this is where extraction most commonly goes subtly wrong.
- **This task's deterministic fallback**, registered per the Phase 1 contract: **abstain**. Mark the reply for human review and record `abstained: true`. There is no regex approximation, no keyword heuristic, no partial guess. Fabricating a promise is the worst failure mode in the system — it would suppress a legitimate chase and corrupt the promise register — so this task degrades to abstention rather than to a cheaper answer. That asymmetry against Engine 2's templated fallback is deliberate: degrade to boring where boring is safe, degrade to abstention where being wrong is expensive.

### 5.4 Promise-to-pay tracking

- Persist every extracted promise with: invoice, committed date, committed amount, conditionality, confidence, source reply, and status (`active`, `kept`, `broken`, `superseded`).
- A promise is **kept** when payment arrives by the committed date, **broken** when the date passes without payment, and **superseded** when a later reply revises it.
- **Broken-promise detection runs as an explicit check**, and only a broken promise (or ladder exhaustion) advances escalation. This is the engine's core differentiation — say it in the pitch: *the system does not chase people who are cooperating; it chases the ones who committed and didn't follow through.*
- Track and report **promise reliability per customer** — this feeds prioritization on subsequent runs and demonstrates the system learning from behavior over time.

### 5.5 Payment path

For any invoice being chased, generate a Razorpay test-mode payment link and include it in the communication. This makes the chase actionable rather than just a nag, and it exercises a real Razorpay API surface. Link creation is audited; failures degrade to a message without a link rather than aborting.

### 5.6 Batch runner

Ingests an invoice ledger and runs: prioritize → chase → ingest replies → extract → track → detect broken promises → escalate → audit → summarize.

Summary must report:

- Value outstanding, value recovered, recovery rate.
- Recovery by ageing bucket — expect and honestly report that older buckets recover worse.
- **Promise extraction accuracy against Phase 2 ground truth**: precision and recall on promise detection, date-accuracy on extracted dates, dispute-detection accuracy, and the abstention rate. Report the confusion matrix, not just headline accuracy. Report accuracy **only over model-handled replies**, with abstentions stated separately — never blend abstained items into an accuracy figure, since that would imply the model answered when it didn't.
- Promises made, kept, broken, superseded.
- Escalations, split by cause (broken promise vs. ladder exhaustion).
- Messages suppressed by caps, quiet hours, or dispute freeze — a direct measure of harassment avoided.
- Invoices deprioritized due to active promises.
- LLM fallbacks and abstentions.

### 5.7 API routes

Trigger run, fetch summary, ranked worklist with score breakdown, single-invoice timeline (every contact, reply, extraction, promise, and decision), promise register with statuses, and drafted communications. The single-invoice timeline is the demo surface — make it tell a complete story on its own.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Prioritization and ladder rules | `policy-architect` | `audit-schema` |
| Contact caps, quiet hours, dispute freeze | `policy-architect` | — |
| Reply-understanding task and schema | `policy-architect` (abstention as policy) | `audit-schema` |
| Extraction accuracy measurement | `data-synthesizer` (ground-truth comparison) | — |
| Payment link creation | `razorpay-integrator` | `razorpay-api` |
| Tests | `test-engineer` | — |
| ADRs and docs | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

---

## 7. Testing requirements

1. **Extraction accuracy** — measured against held-out annotations, reported as precision/recall, not asserted as "works."
2. **Hinglish replies** — explicitly tested, with cases in the suite.
3. **Relative date resolution** — "next Friday", "end of month", "in 10 days" resolve correctly against the reply timestamp.
4. **Conditional promises** — tracked distinctly from firm ones.
5. **No fabricated promises** — an ambiguous or empty reply must never yield a confident promise. Test adversarially with replies designed to bait a false positive.
6. **Dispute freeze** — a dispute halts chasing immediately; assert no subsequent contact is authorized.
7. **Cross-invoice contact cap** — a customer with many overdue invoices does not receive a message per invoice.
8. **Quiet hours** — held, not sent, and audited as held.
9. **Escalation gating** — a customer with an active unbroken promise is not escalated; escalation fires only on break or exhaustion.
10. **Audit completeness** — every action has an authorizing rule.

---

## 8. Acceptance criteria

- [ ] `ruff check` clean; `pytest` passes.
- [ ] Full batch run completes and prints the section 5.6 summary.
- [ ] Extraction accuracy reported with a confusion matrix against held-out ground truth, including the abstention rate. **Report the real numbers even if they disappoint** — an honest 84% with a discussion of failure cases is far stronger than a suspicious 99%.
- [ ] Run demonstrates: a kept promise, a broken promise escalating, a dispute freezing the chase, a message suppressed by cap or quiet hours, and an invoice deprioritized due to an active promise.
- [ ] At least one Hinglish reply correctly handled, visible in the trail.
- [ ] At least one abstention routed to human review.
- [ ] Razorpay test-mode payment links successfully created and audited.
- [ ] `/audit-check` passes.
- [ ] Reproducible from the same seed.

---

## 9. Documentation to produce

- ADR: why escalation is gated on broken promises rather than elapsed time, and what that trades off.
- ADR: how extraction accuracy is measured and why the ground truth is trustworthy.
- Update `docs/architecture.md` with Engine 3's flow.
- Record honest extraction metrics including the specific categories where the model struggled — those failure cases are genuinely interesting material for both the video and the interview.

---

## 10. Commit checklist

- `feat(receivables): add deterministic invoice prioritization`
- `feat(receivables): add bounded escalation ladder with contact caps`
- `feat(receivables): add LLM reply understanding with promise extraction and abstention fallback`
- `feat(receivables): add promise-to-pay tracking and broken-promise detection`
- `feat(receivables): add Razorpay test-mode payment links to communications`
- `feat(receivables): add batch runner with extraction accuracy metrics`
- `feat(api): expose receivables runs, worklist, timelines, and promise register`
- `test(receivables): cover extraction accuracy, dispute freeze, and contact caps`
- `docs: add ADRs for escalation gating and extraction measurement`

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: the batch summary, the extraction confusion matrix with honest commentary on where it failed, an example single-invoice timeline showing a full chase-to-recovery or chase-to-escalation story, and the count of messages suppressed by caps.

All three engines are now built. Also report: anything the shared core made awkward across all three, since that's the honest technical-obstacle material the submission form asks for.

Do not begin Phase 6. The next phase file will be provided separately.
