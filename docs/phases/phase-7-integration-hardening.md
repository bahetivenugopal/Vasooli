# Phase 7 — Integration, Hardening & Documentation

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Turn three separately-built engines and a dashboard into one coherent, demonstrable product: a single unified batch run across all three engines, a reproducible one-command demo path, verified metric integrity, and documentation good enough that a judge who only reads the repo still understands what was built and why.

---

## 2. Why this phase comes seventh

Everything works in isolation at this point. Nothing has been proven to work *together*, and the demo depends entirely on the together case. This phase exists to find the seams before a judge does.

It's also the phase where the repository itself becomes a deliverable rather than a byproduct. The repo is the artifact a panelist can study at their own pace, and it has to stand on its own.

---

## 3. Preconditions

- Phases 1–6 complete and committed.
- All three engines run their own batches successfully.
- Dashboard renders every route with real data.

---

## 4. Scope

### In scope
- A unified cross-engine batch run producing one consolidated report.
- One-command reproducible demo setup from a clean clone.
- Metric integrity verification across API, UI, and audit trail.
- Deliberate failure-path rehearsal.
- Full documentation pass: README, architecture, ADR completeness.
- Repository hygiene.

### Out of scope
- New features. If something is missing at this point, it stays missing unless it is genuinely broken. Adding capability now risks the demo for marginal gain.
- Deployment.
- Refactors that aren't fixing an actual defect. Elegance is not worth risk at this stage.

---

## 5. Design specification

### 5.1 The unified run

A single command that runs all three engines against their datasets and produces one consolidated result.

- One `batch_id` spanning all three engines, so the entire run is auditable as a single unit.
- A **consolidated report** with: total value at risk and recovered across all engines with the overall recovery rate; per-engine contribution; total policy denials, compliance blocks, suppressions, escalations, and fallbacks; and each engine's own headline metrics.
- **Every figure derived from the audit trail.** This is the last chance to catch a number computed twice in two places.
- Deterministic from a documented seed, so the same command produces the same report on any machine.
- The report renders both to the console and to the dashboard overview, and the two must agree exactly.

### 5.2 Reproducibility from a clean clone

Assume a judge clones the repo and follows the README with no prior context. That path must work.

- Setup must be genuinely minimal: install dependencies, copy `.env.example`, add keys, seed data, run. Every step in the README, in order, with real commands.
- **Test the whole thing on a clean clone in a fresh directory.** Not conceptually — actually do it. This step catches missing dependencies, undocumented steps, and files that only exist locally, which are the classic ways a "working" project fails in someone else's hands.
- The system must run without live API keys — using the deterministic simulation path from Phase 1 for Razorpay and the registered deterministic fallbacks for every reasoning task — while clearly reporting which mode it's in. A judge without keys must still see the project work. This is one of the most practically valuable properties in the entire build.

### 5.3 Metric integrity audit

A verification pass whose entire job is proving the numbers are consistent and honest:

- Recompute every headline metric directly from raw audit entries and assert it matches what the summary API reports and what the UI displays. Three sources, one answer.
- Assert no orphan actions: every recovery attempt traces to an authorizing policy decision, and every policy decision traces to a named rule.
- Assert no untraced money: every rupee counted as recovered links to a specific audited action.
- Run `/audit-check` across the full unified run.
- Confirm the run reproduces identically from the same seed on a second execution.

Any discrepancy found here is a defect to fix, not a rounding issue to explain away.

### 5.4 Failure-path rehearsal

The competition's bar explicitly rewards showing a failure handled gracefully. Rehearse these deliberately, confirm each degrades cleanly and is audited, and note which ones leave something observable behind:

- Gemini API unavailable mid-batch.
- Gemini rate limit exceeded beyond backoff.
- Deterministic-only mode forced by config across a full run.
- Malformed LLM output.
- Razorpay test-mode API error.
- An LLM recommendation that policy denies.
- A mandate revoked mid-sequence.
- A dispute arriving mid-chase.
- Empty or malformed input batch.
- Backend down while the dashboard is open.

The system should survive all of them without a crashed run, and the audit trail should explain what happened in each case.

### 5.5 Documentation pass

The README is the highest-traffic artifact in the submission. Structure it to be read by someone with limited time:

- Project name, tagline, and the one-paragraph summary.
- The headline result up front — measured recovery across the unified batch, stated with its methodology and a plain acknowledgment that the data is synthetic and the assumptions are documented. Leading with an honest number framed honestly is more persuasive than burying it.
- Architecture diagram and a short explanation of the shared-core design.
- What each engine does, in a few lines each.
- Quickstart that actually works from a clean clone.
- How to reproduce the reported metrics.
- A section on the safety and boundedness model: policy authority, LLM-recommends-policy-authorizes, audit trail, stopping rules, human escalation.
- Honest limitations. State plainly that data is synthetic, that retry outcomes are simulated from a documented probability model, that communications are generated but never dispatched, and that this is a prototype. **This section makes every other claim more credible, not less** — a panelist who finds an unacknowledged limitation discounts everything; one who sees it stated up front trusts the rest.
- Tech stack and repository map.
- An explicit note on how AI was used to build the project: the `.claude/` agents, skills, and commands, and how the work was phased. This is an AI-builder role — the meta-story of how the repo was built is itself relevant evidence.

Also: complete `docs/architecture.md` with the final flows, verify every ADR from Phases 1–6 exists and is accurate, and confirm every skill file matches what the code actually does.

### 5.6 Repository hygiene

- No secrets in any tracked file or anywhere in git history. Check the history, not just the working tree.
- No committed databases, caches, or build artifacts.
- `.env.example` complete and accurate.
- Lint clean across both apps.
- Full test suite green, with the count and coverage of `policy_engine.py` recorded.
- Commit history readable and conventional — this is visible to judges and is part of the evidence that the project was built incrementally over the window rather than assembled at the end.
- Delete genuinely dead code; leave working code alone.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Unified runner and consolidated report | `policy-architect` | `audit-schema` |
| Metric integrity audit | `test-engineer` | `audit-schema` |
| Failure-path rehearsal | `test-engineer` | — |
| README, architecture, ADR completeness | — | — |
| Clean-clone verification | `test-engineer` | — |
| Commits | — | `conventional-commits` |

---

## 7. Testing requirements

1. **Unified run** completes end to end and produces the consolidated report.
2. **Cross-source metric consistency** — audit-derived, API-reported, and UI-displayed values match exactly.
3. **Traceability** — no orphan actions, no untraced recovered money.
4. **Determinism** — identical results from the same seed across repeated runs.
5. **All eight failure paths** from 5.4 degrade gracefully and are audited.
6. **Clean-clone setup** verified by actually performing it.
7. **No-keys mode** produces a complete run with the mode clearly reported.
8. Full suite green; policy engine coverage recorded.

---

## 8. Acceptance criteria

- [ ] One command runs all three engines and produces a consolidated report.
- [ ] Console report and dashboard overview agree exactly.
- [ ] Metric integrity audit passes with no discrepancies.
- [ ] All eight failure paths rehearsed, each degrading gracefully and audited.
- [ ] Clean clone in a fresh directory works following only the README.
- [ ] The project runs without live API keys and reports the mode clearly.
- [ ] Same seed reproduces identical results.
- [ ] README complete, including the honest limitations section and the AI-usage section.
- [ ] `docs/architecture.md` final; all ADRs present and accurate.
- [ ] No secrets in the working tree or git history.
- [ ] Lint clean, tests green, coverage of `policy_engine.py` recorded.

---

## 9. Documentation to produce

- Final `README.md` per 5.5.
- Final `docs/architecture.md`.
- A short `docs/RESULTS.md` with the unified run's full measured output and methodology — this is the citable evidence behind every claim in the repo, and the answer to any judge who asks "where does that number come from?"
- `docs/adr/` complete.

---

## 10. Commit checklist

- `feat(core): add unified cross-engine batch runner and consolidated report`
- `fix: resolve integration defects found during unified runs`
- `test: add metric integrity audit across audit trail, API, and UI`
- `test: rehearse and verify graceful degradation paths`
- `docs: complete README with results, safety model, and limitations`
- `docs: finalize architecture and results documentation`
- `chore: repository hygiene and lint pass`

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: the full consolidated report numbers, confirmation the clean-clone path works, the list of failure paths rehearsed with how each behaved, and a candid list of anything still weak or unpolished.

That last item matters — the technical-obstacles write-up should be built from real problems, not invented ones.
