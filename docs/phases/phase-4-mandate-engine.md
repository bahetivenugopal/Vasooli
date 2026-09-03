# Phase 4 — Engine 2: Mandate & Subscription Recovery

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the engine that recovers failed recurring charges: classify each failure as soft or hard, retry soft declines inside RBI's actual e-mandate compliance windows, route hard declines straight to customer communication instead of wasting attempts, and stop cleanly at every hard boundary — with the whole sequence auditable.

---

## 2. Why this phase comes fourth

This is the engine where domain correctness matters more than cleverness. Anyone can write a retry loop; almost nobody writes one that respects the ₹15,000 AFA threshold, the pre-debit notification window, and immediate hard-stop on mandate revocation. That regulatory fluency is the clearest possible signal to an Indian fintech panel that this project understands the environment Razorpay actually operates in, rather than porting a generic Western dunning playbook.

It also targets the largest measurable money in the project: involuntary churn is a substantial share of all subscription churn, and most of those customers never intended to leave.

---

## 3. Preconditions

- Phases 1–3 complete, committed, acceptance criteria passed.
- Mandate dataset generates with amounts on both sides of the ₹15,000 threshold, plus revoked, paused, and cap-reaching cases.
- `.claude/skills/rbi-mandate-rules/SKILL.md` is complete and specific. If it is thin, **fix it before writing engine code** — every rule here must cite it.
- The policy engine's amount-threshold hook and hard-stop machinery from Phase 1 are working.

---

## 4. Scope

### In scope
- Soft/hard decline classification applied to recurring-payment failures.
- A compliance-aware retry scheduler enforcing RBI mandate constraints.
- Dunning message drafting via the LLM wrapper, gated by policy.
- Hard-stop and escalation handling.
- Batch runner with honest recovery measurement.
- API routes for runs, mandates, retry schedules, and communications.

### Out of scope
- Invoice/receivables workflows — Phase 5.
- Voice delivery — optional bonus, only after Phase 8's core work is complete, never before.
- Actually sending messages to real people. Communications are generated, policy-gated, logged, and rendered — never dispatched to a real recipient. State this plainly in the docs; simulated delivery honestly labelled is far better than a real send with no consent path.

---

## 5. Design specification

### 5.1 Failure classification

Reuse the shared classifier from Phase 1 rather than writing a second one. Extend it only where recurring payments introduce failure modes single payments don't:

- **AFA/authentication failure** — distinct from insufficient funds. Retrying without addressing authentication just fails again; this class must route to customer action, not blind retry.
- **Missing or late pre-debit notification** — a compliance failure, not a customer failure. The correct response is to send the notification and reschedule outside the notification window, never to hammer the retry.
- **Mandate revoked** — absolute hard stop. No retry, no communication urging re-authorization beyond a single permitted notice, no exceptions. Treat any code path that could retry a revoked mandate as a bug.

Every classification traces to the decline taxonomy. If a mandate-specific reason isn't in the taxonomy, add it to the skill file first.

### 5.2 The compliance-aware retry scheduler

The centerpiece. Every scheduling decision must be authorized by a named policy rule citing `rbi-mandate-rules`.

Constraints to enforce:

- **Pre-debit notification window** — no debit attempt may be scheduled unless a notification was sent the required interval in advance. If it wasn't, the engine's action is to send the notification and schedule beyond the window, not to attempt the debit.
- **AFA threshold branching** — mandates at or below ₹15,000 per transaction follow the no-fresh-OTP path; above it, fresh authentication is required, so a retry alone cannot succeed and the correct action is a customer-authentication request. Both branches must be exercised by the dataset and visible in the run summary.
- **Attempt caps** — a hard ceiling per mandate per cycle that cannot be overridden by any caller or by any LLM recommendation.
- **Backoff schedule** — escalating intervals between attempts, config-driven, with the reasoning documented. Where the timing is a product judgment rather than a regulation, label it as such in the ADR — do not dress a product decision up as a rule.
- **Hard-stop conditions** — revoked mandate, closed account, or terminal customer state halts everything immediately and irreversibly.
- **Quiet hours** — reuse Phase 1's communication limits for any customer-facing message.

The scheduler must be **explainable per decision**: for any mandate, it should be able to state what it will do next, when, and which rule permits it. This is what the dashboard visualizes in Phase 6 and what the video demonstrates, so build the explanation as a first-class output, not an afterthought.

### 5.3 Dunning communication (LLM)

For failures where communication is the right response, use the LLM wrapper to draft the message.

- Input: failure classification, mandate context, attempt history, customer archetype, and what specific action the customer needs to take.
- Output: a structured draft — channel, subject/opening, body, and the specific call to action — validated against a schema.
- **Tone constraints are policy, not prose preference.** No fabricated urgency, no threats, no implied consequences that aren't real. The message must accurately state what happened and what the customer can do. Encode these as explicit constraints the drafting task must satisfy, and validate the output against them.
- Content must differ meaningfully by failure class. An expired-card message and an insufficient-funds message asking the customer to do the same thing is a sign the LLM isn't actually being used for judgment — assert this differs in tests.
- Every draft is policy-gated before being marked for delivery: quiet hours, message caps per mandate per period, and hard-stop suppression all apply.
- Every draft is audited with its reasoning, its provenance, and the authorizing rule.
- **This task's deterministic fallback**, registered per the Phase 1 contract: a static per-failure-class message template with slot-filled variables. This is the safest fallback in the project — a plain, accurate message stating what happened and what the customer can do costs almost nothing in quality, so degrading to boring here is entirely acceptable.

### 5.4 Batch runner

Ingests a mandate batch, processes every failed debit through classify → authorize → schedule or communicate → simulate outcome → audit → summarize.

Summary must report:

- Value at risk and value recovered (paise), recovery rate.
- Breakdown by failure class — this is where the story lives, because it shows soft declines recovering at a very different rate from hard ones.
- **Retries correctly suppressed on hard declines** — attempts saved, and an estimate of what blind retrying would have cost in wasted attempts. This is one of the most persuasive numbers in the whole project.
- Above/below AFA threshold breakdown.
- Compliance-blocked actions: how many attempts the engine refused to make because the notification window or another rule forbade it. Non-zero is the point.
- Mandates that hit the attempt cap and stopped.
- Human escalations raised.
- LLM fallbacks.

### 5.5 API routes

Trigger run, fetch summary, list mandates with current recovery state, fetch one mandate's full timeline (every attempt, decision, rule, and message), and list drafted communications. The single-mandate timeline route is the one the demo will lean on — make it complete and readable.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Retry scheduler and all compliance rules | `policy-architect` | `rbi-mandate-rules`, `decline-taxonomy` |
| Failure classification extensions | `policy-architect` | `decline-taxonomy` |
| Dunning drafting task and constraints | `policy-architect` (tone as policy) | `audit-schema` |
| Subscription/mandate API calls | `razorpay-integrator` | `razorpay-api` |
| Tests | `test-engineer` | `rbi-mandate-rules`, `decline-taxonomy` |
| ADRs and docs | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

Every rule in this phase must cite a specific clause in `rbi-mandate-rules`, or be explicitly labelled a product decision in the ADR. No plausible-sounding invented thresholds.

---

## 7. Testing requirements

1. **Revoked mandate is untouchable** — no code path produces a retry. Test it directly and adversarially.
2. **Notification window enforcement** — a debit scheduled without a compliant prior notification is denied, and the denial is audited.
3. **AFA threshold branching** — above and below ₹15,000 produce different, correct action paths.
4. **Attempt cap is absolute** — cannot be exceeded, including when a caller or an LLM recommendation tries to force it.
5. **Backoff enforcement** — a retry inside the cooldown is denied.
6. **Quiet hours** — a message drafted for a blocked window is held, not sent, and audited as held.
7. **Hard declines never retry** — they route to communication only.
8. **Message differentiation** — drafts differ meaningfully across failure classes.
9. **LLM fallback** — drafting failure degrades to the per-failure-class template, audited with `source: deterministic`, batch continues.
10. **Audit completeness** — every action across a full batch has an authorizing rule.

---

## 8. Acceptance criteria

- [ ] `ruff check` clean; `pytest` passes.
- [ ] Full batch run completes and prints the section 5.4 summary.
- [ ] Run demonstrates all of: a successful soft-decline recovery, a suppressed hard decline, a compliance-blocked attempt, a mandate hitting its cap, and a revoked mandate hard-stopped.
- [ ] Both AFA threshold branches exercised, visible in the summary.
- [ ] At least one policy denial of an LLM recommendation in the trail.
- [ ] At least one demonstrated LLM fallback.
- [ ] A single-mandate timeline route returns a complete, human-readable history.
- [ ] `/audit-check` passes over the run.
- [ ] Results reproducible from the same seed.

---

## 9. Documentation to produce

- ADR: how RBI e-mandate constraints are encoded as policy rules, with citations, and which timings are regulation versus product judgment.
- ADR: why communications are generated and logged but never dispatched to real recipients.
- Update `docs/architecture.md` with Engine 2's flow.
- Record honest run metrics, including anything that recovered poorly and why.

---

## 10. Commit checklist

- `feat(mandate): extend decline classification for recurring failure modes`
- `feat(mandate): add compliance-aware retry scheduler with RBI-cited rules`
- `feat(mandate): add policy-gated dunning drafting with templated fallback`
- `feat(mandate): add batch runner with failure-class recovery breakdown`
- `feat(api): expose mandate runs, timelines, and communications`
- `test(mandate): cover compliance boundaries, caps, and hard stops`
- `docs: add ADRs for mandate compliance encoding and simulated delivery`

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: the batch summary with the failure-class breakdown, the wasted-attempts-avoided figure, an example single-mandate timeline showing a full recovery sequence, an example compliance-blocked action, and any place where the RBI rules were ambiguous enough that you had to make a judgment call — flag those explicitly, since they're worth being able to speak to in the interview.

Do not begin Phase 5. The next phase file will be provided separately.
