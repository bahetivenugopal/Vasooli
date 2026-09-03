# Phase 3 — Engine 1: Root-Cause Recovery

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the first engine: detect when a payment corridor is degrading, diagnose *why* using LLM reasoning, and execute a bounded recovery action — all through the shared core, with every step audited and measured against the ground truth injected in Phase 2.

---

## 2. Why this phase comes third

This is the engine that most justifies the word "agent" in the pitch. The others are strong workflows; this one requires genuine judgment under ambiguity. The central problem — *is this one customer's failure, or is an entire corridor down?* — looks identical from a single decline code and demands opposite responses. Getting this right is what separates the project from a retry loop with a chat interface on top.

Building it first among the engines also stress-tests the shared core hardest, surfacing any Phase 1 interface weaknesses while there's still time to fix them cleanly.

---

## 3. Preconditions

- Phases 1 and 2 complete, committed, acceptance criteria passed.
- Payments dataset generates cleanly, with injected degradation events and decoys recorded in the manifest.
- The policy engine's generic rule categories (attempt caps, cooldown, hard-stop, amount thresholds, human escalation) are working and tested.
- The LLM wrapper's structured-output, caching, and deterministic-fallback paths are verified.

---

## 4. Scope

### In scope
- Corridor-level degradation detection over the payments stream.
- LLM-powered root-cause diagnosis distinguishing systemic from individual failure.
- Bounded recovery actions: rerouting, retry scheduling, and suppression.
- Batch runner producing honest, measured results.
- Detection accuracy measured against Phase 2 ground truth, including false positives.
- API routes exposing runs, detections, and diagnoses.

### Out of scope
- Mandate/subscription-specific retry rules — Phase 4.
- Invoice/receivables logic — Phase 5.
- Any UI — Phase 6.
- Live traffic rerouting against real Razorpay infrastructure. Recovery actions execute against test mode or the deterministic simulation path from Phase 1; both must be explicit in the audit trail.

---

## 5. Design specification

### 5.1 The detection layer

Deterministic and statistical, not LLM-driven. The model explains and diagnoses; it does not detect. Keeping detection deterministic means it's reproducible, testable, and defensible when a judge asks how it works.

- Segment the payment stream into **corridors**: at minimum (issuing bank × method), plus route/acquirer where present. Corridor definition must be config-driven so its granularity can be discussed rather than assumed.
- Compute rolling success rates per corridor over a configurable window. Compare against that corridor's own recent baseline, not a global constant — corridors legitimately differ, and a global threshold would generate constant false positives.
- **Require a minimum volume before flagging.** This is the single most important guard against false positives, and it's what makes the Phase 2 decoy corridor not fire. State the threshold explicitly in config.
- Emit a detection object carrying: corridor identity, window, observed vs. baseline rate, affected attempt count, value at risk (paise), and a confidence signal.
- Detection must never read the ground-truth manifest. Add a test asserting this.

### 5.2 The diagnosis layer (LLM)

This is where the LLM earns its place. For each detection, call the LLM wrapper with a structured task that receives: the corridor's recent statistics, the distribution of decline reasons within the affected window, comparison against unaffected corridors in the same period, and the taxonomy classification of the reasons observed.

The model must return structured output containing:

- **A root-cause hypothesis** — issuer-side outage, method-level degradation, route/acquirer problem, a concentration of genuine customer-side failures, or insufficient evidence.
- **Systemic vs. individual determination** — the core judgment. If the decline mix is dominated by insufficient-funds across many distinct customers, that is *not* a corridor outage; if it's dominated by timeouts and gateway errors concentrated on one bank, it likely is.
- **A recommended action**, chosen from a fixed enumerated set — never free text. The model recommends; the policy engine authorizes.
- **Stated reasoning**, captured verbatim into the audit trail. This is what makes the demo compelling: the judge sees not just what happened but why the system believed it.
- **An explicit "insufficient evidence" option.** A model that always produces a confident diagnosis is a model that will confidently be wrong. Make abstention a first-class outcome and show it working — this is a genuine trust feature, not a limitation.

**This task's deterministic fallback**, registered per the Phase 1 contract: classify from the decline-code distribution alone — failures concentrated on one corridor and dominated by timeout/gateway-class reasons resolve to systemic; failures spread across many distinct customers with fund/account-class reasons resolve to individual; anything else resolves to **insufficient evidence and escalates to human review**. Biasing toward escalation under degraded reasoning is the correct behaviour, not a weakness. The fallback fires when the provider is unavailable, rate-limited beyond backoff, returns unparseable output twice, or when deterministic-only mode is configured. Every fallback is audited with `source: deterministic`. Demonstrating this working is one of the strongest things in the pitch — the competition explicitly rewards a failure handled gracefully.

### 5.3 The recovery layer

Actions available (enumerated, bounded):

1. **Reroute** — mark subsequent attempts on a degraded corridor for an alternate route/method, for a bounded duration with automatic expiry. A reroute that never expires is a permanent config change, not a recovery action; expiry is what keeps it bounded.
2. **Schedule retry** — for individual soft-decline failures, schedule a retry respecting cooldown and attempt caps from the policy engine.
3. **Suppress retry** — for hard declines, explicitly do nothing and record why. Suppression is a real action and must be audited as one; the count of correctly suppressed hard declines is a number worth showing, since it represents wasted attempts avoided.
4. **Escalate to human** — when the diagnosis is insufficient-evidence, the value at risk crosses a configured threshold, or repeated recovery attempts fail.

Every action passes through the policy engine before execution, without exception. Every action produces an audit entry naming the authorizing rule. Where an action results in an actual Razorpay test-mode call, that call and its result are audited too.

### 5.4 The batch runner

A single entry point that ingests a generated payments batch and runs the full loop: detect → diagnose → authorize → act → audit → summarize.

The summary must report honestly:

- Total value at risk and total recovered (paise), and the recovery rate.
- Detection performance against ground truth: true positives, false positives, false negatives, and detection latency (how long after degradation onset it fired).
- Action counts by type, including suppressions.
- Count of LLM fallbacks that occurred.
- Count of policy denials — actions the LLM recommended that the rules refused. **This number being non-zero is a feature**, and the pitch should say so plainly: it's proof the gate is real.
- Any human escalations raised.

All figures computed from the audit trail, never from side-counters that could drift.

### 5.5 API routes

Thin routes over the runner and its results: trigger a batch run, fetch run summary, list detections for a run, fetch a single detection with its full diagnosis and reasoning, and list actions with their authorizing rules. Keep logic in services.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Detection layer | `policy-architect` (thresholds and guards) | `decline-taxonomy` |
| Diagnosis prompts and schema | `policy-architect` (for the recommend-vs-authorize boundary) | `decline-taxonomy`, `audit-schema` |
| Recovery actions, Razorpay calls | `razorpay-integrator` | `razorpay-api` |
| Batch runner metrics | `data-synthesizer` (ground-truth comparison) | — |
| Tests | `test-engineer` | `decline-taxonomy` |
| ADR and docs | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

---

## 7. Testing requirements

1. **Detection correctness** — fires on the injected degradation; does *not* fire on the decoy; respects the minimum-volume guard. Test with several seeds, not one.
2. **Ground-truth isolation** — the detector cannot access the manifest.
3. **Systemic vs. individual** — a synthetic window dominated by insufficient-funds across many customers must not be classified as a corridor outage.
4. **Policy supremacy** — an LLM recommendation for an action the rules forbid must be denied and audited as a denial. This is the most important test in this phase.
5. **Hard-decline suppression** — hard declines never receive a retry authorization.
6. **Reroute expiry** — a reroute automatically lapses; assert it cannot persist indefinitely.
7. **LLM failure paths** — API error, timeout, rate limit beyond backoff, and malformed output each fall back to the deterministic classifier, are audited with `source: deterministic`, and do not abort the batch.
8. **Audit completeness** — every action in a run has a corresponding audit entry with a `rule_id`. Assert programmatically over a full batch.

---

## 8. Acceptance criteria

- [ ] `ruff check` clean; `pytest` passes.
- [ ] A full batch run completes end to end and prints the summary from section 5.4.
- [ ] Detection metrics reported against ground truth, **including at least one honestly reported false positive or a clear statement that none occurred at the configured threshold**.
- [ ] At least one audit trail entry shows an LLM recommendation denied by policy.
- [ ] At least one run demonstrates graceful LLM fallback (force it deliberately if it doesn't occur naturally).
- [ ] At least one human escalation is raised and visible in the trail.
- [ ] `/audit-check` passes over the run: every action has an authorizing rule.
- [ ] Recovery figures are reproducible from the same seed.

---

## 9. Documentation to produce

- ADR: why detection is deterministic and diagnosis is LLM-driven, and why that boundary was drawn there.
- ADR: corridor definition and the minimum-volume threshold, with the false-positive trade-off stated explicitly.
- Update `docs/architecture.md` with Engine 1's data flow.
- Record the run's honest metrics in the docs — including anything that underperformed. A metric that is quietly omitted is worse than one that is reported with context.

---

## 10. Commit checklist

- `feat(root-cause): add corridor segmentation and degradation detection`
- `feat(root-cause): add LLM diagnosis with structured output, abstention, and deterministic fallback`
- `feat(root-cause): add bounded recovery actions with policy authorization`
- `feat(root-cause): add batch runner with ground-truth scored metrics`
- `feat(api): expose root-cause runs, detections, and diagnoses`
- `test(root-cause): cover detection accuracy, policy supremacy, and fallback paths`
- `docs: add ADRs for detection/diagnosis split and corridor thresholds`

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: the actual batch summary numbers, detection performance against ground truth including false positives, an example diagnosis with its reasoning, an example policy denial, and anything about the shared core that felt limiting while building this engine — that feedback matters before two more engines are built on it.

Do not begin Phase 4. The next phase file will be provided separately.
