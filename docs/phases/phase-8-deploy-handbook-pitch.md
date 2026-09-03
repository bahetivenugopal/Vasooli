# Phase 8 — Deployment, Handbook & Pitch

> The final phase. This one has a human in the loop throughout — deployment decisions, the demo run-through, and the video recording all need you. Claude Code produces the artifacts; you make the calls.

---

## 1. Goal

Ship it. Deploy the project so a judge can click a link, write the handbook that makes everything demonstrable, produce the 5-minute pitch script, and prepare every field of the submission form. By the end of this phase the submission is complete and nothing is left to last-minute chance.

---

## 2. Order of operations

Do these in order, and treat the ordering as deliberate:

1. **Handbook first.** Writing down how to demonstrate everything forces you to actually try it, which is when problems surface. Better now than on camera.
2. **Deployment second.** It's the piece most likely to consume unexpected time, and it's a nice-to-have. Time-box it.
3. **Script third.** Written after the handbook, so it describes what actually works rather than what was planned.
4. **Recording fourth.**
5. **Submission fields last**, assembled from everything above.

**If time runs short, drop deployment.** A polished local demo with a great video wins over a deployed app with a rushed one. Deployment is the only genuinely optional item in this phase.

---

## 3. Preconditions

- Phase 7 complete: unified run works, metrics verified, clean-clone path tested, README done.
- Screenshots captured in `docs/pitch/`.
- Phase 7's candid weak-points list in hand — it dictates what the demo avoids.

---

## Part A — The Project Handbook

Produce `docs/HANDBOOK.md`, the single document someone reads to understand, run, and demonstrate everything. Assume a reader who is technically capable but has never seen this project.

### A.1 What it must contain

**Getting running**
- Prerequisites with versions.
- Clone-to-running in numbered steps with real, copy-pasteable commands.
- Environment variables: each one, what it's for, and whether it's required or optional.
- **The no-keys path**, stated prominently: how to run the full project with no Gemini or Razorpay keys, what changes in that mode, and how the interface reports it. A judge who can't run it is a judge who only has your word for it.
- Verifying the install worked: the exact command and the expected output.

**Understanding the system**
- The shared-core model explained in a few paragraphs: one policy engine, one LLM wrapper, one audit trail, three engines on top.
- The one architectural rule that matters most: **the model recommends, policy authorizes.** Explain why that boundary exists and where it lives in the code.
- What each engine does and the specific revenue leak it addresses.
- Where things live — a repository map with the significant files called out.

**Demonstrating every feature**

This is the section that earns the document. For each capability, give the exact steps to show it, what the observer should see, and why it matters. Cover at minimum:

*Root cause engine* — running a batch; watching corridor degradation get detected; opening a detection to read the model's diagnosis and reasoning; seeing the systemic-versus-individual judgment; seeing an action authorized and taken; seeing the decoy corridor correctly not flagged; finding a false positive if one exists and discussing it honestly.

*Mandate engine* — a soft decline recovering successfully; a hard decline correctly suppressed with wasted attempts avoided; an attempt blocked by the pre-debit notification rule; the ₹15,000 AFA threshold branching both ways; a mandate hitting its cap and stopping; a revoked mandate hard-stopped; a dunning message drafted differently for different failure classes.

*Receivables engine* — the ranked worklist with score breakdown; a chase message with its Razorpay payment link; a reply parsed into a promise, including a Hinglish one; a relative date resolved correctly; a kept promise closing out; a broken promise escalating; a dispute freezing the chase; a message suppressed by the cross-invoice contact cap; the extraction confusion matrix.

*Cross-cutting* — the unified run and consolidated report; the overview headline; the trust strip; the audit trail's denials-only filter; a full entity timeline end to end; forcing an LLM failure and watching graceful degradation; reproducing a run from a seed.

**Interpreting the numbers**
- What each headline metric means and how it's computed.
- Why non-zero denials, suppressions, and escalations are *good* numbers.
- The methodology and its assumptions, stated plainly.
- The limitations, repeated here so anyone reading only the handbook still sees them.

**Troubleshooting**
- Every failure you actually hit while building, and its fix. Ports in use, missing env vars, empty database, rate limits, stale data. This is also raw material for the submission's technical-obstacles answer.

### A.2 Also produce

- `docs/DEMO_SCRIPT.md` — a tight, ordered click-path for the live demo, with the exact seed and commands, timed roughly, listing every screen in sequence. This is what you rehearse against.
- A pre-flight checklist to run immediately before recording: services up, data seeded, run completed, browser zoom set, notifications off, correct screen, terminal font readable.

---

## Part B — Deployment

Time-boxed and optional. Stop and move on if it fights back.

### B.1 Recommended approach

- **Frontend → Vercel.** Native Next.js support, connect the repo, deploy in minutes.
- **Backend → Railway or Render.** Either handles a FastAPI service with minimal configuration. Pick whichever authenticates faster and don't deliberate.
- **Database** — SQLite on a persistent volume is fine. Do not migrate to Postgres now; that is a Phase 7-style change arriving too late to be safe.

### B.2 What must be true

- Environment variables set in the platform, never committed.
- CORS configured for the deployed frontend origin.
- The API base URL configurable by environment, not hardcoded.
- **The deployment must be pre-seeded with a completed batch run**, so a judge landing cold sees populated dashboards immediately rather than empty states.
- Test-mode enforcement still active in production. Verify it.
- A rate-limit or auth guard on any route that triggers a batch run, so a public URL can't be used to burn your Gemini quota.
- The deployed build verified end to end after deploying — every route, real data.

### B.3 If it fails

Abandon it without regret, note in the README that the project runs locally with a working quickstart, and put the time into rehearsal instead. Say so plainly in the submission rather than leaving a broken link — a dead demo URL is worse than no URL.

---

## Part C — The Pitch Video

### C.1 What this video has to do

Five minutes, watched by someone who has already watched many submissions today. It has to be *memorable*, not merely complete. That means it needs a narrative spine — a problem that lands emotionally, a reasoning process the viewer can follow, and a demonstration that pays off the setup. A feature tour fails because by minute four the viewer has forgotten minute one.

The organizing principle: **one story, told three times at increasing scale.** Establish a single concrete human situation, show the system handling it, then reveal that the same machinery handles two other classes of the same problem. That structure means the viewer only has to hold one idea in their head, and everything after reinforces it rather than competing with it.

### C.2 Structure

**0:00–0:40 — The problem, made concrete**

Do not open with market statistics. Open with one specific, ordinary situation: a subscriber's recurring payment fails — a bank-side hiccup, nothing to do with them. They never see it. Their access lapses. From the business's side it appears in a report as churn. Nobody chose this. Nobody meant for it to happen.

Then widen, briefly: this is not rare. Involuntary churn — customers lost to failed payments rather than decisions — runs at roughly 20–40% of all subscription churn, and a large majority of those customers never intended to leave. Most of them never retry on their own. State it once, cleanly, and move on. One statistic that lands beats five that blur.

Close the section with the framing the whole video hangs on: revenue like this doesn't disappear in one dramatic event. It leaks quietly, in several places at once, and each leak looks like somebody else's problem.

**0:40–1:30 — How we thought about it**

This is the section most submissions skip, and it's where a panel decides whether they're watching a builder or a demo.

Walk through the actual reasoning, honestly:

*The two lenses.* Looking at this as a business would — which leaks actually move a number a CFO looks at? — and as the person on the other end would. The subscriber locked out of a service they were paying for. The founder chasing an invoice at 11pm, dreading the awkward follow-up. Those two views agreed on something specific: the failures that hurt most are the ones nobody chose.

*The realization that shaped the architecture.* The track lists these as separate problems — payment failures, subscriptions, receivables. Looking closely, they're the same shape: money at risk, a reason it's at risk, a bounded set of things you're permitted to do about it, and an obligation to explain what you did. That observation is why this isn't three tools. It's one recovery engine with three front doors.

*The constraint that made it real.* Building this for India means the retry logic isn't a free choice. RBI's e-mandate framework sets the rules — pre-debit notification windows, the ₹15,000 AFA threshold, immediate stop on revocation. A generic dunning playbook would be non-compliant here. That constraint became the most interesting engineering in the project.

*The line we drew.* An agent that moves money needs a hard boundary. The model reasons, diagnoses, and drafts. Deterministic policy code decides what's permitted. The model can recommend something the rules forbid — and when it does, the rules win, and the refusal is logged. That inversion is structurally impossible in this codebase, by design.

**1:30–3:45 — The demonstration**

Show, don't describe. Follow one entity all the way through before showing any aggregate.

*Start with the story from minute zero.* That same failed subscription payment, now in the system. Show the timeline: the failure arrives, the classifier reads it as a soft decline, the policy engine authorizes a retry — naming the rule — and the retry succeeds. Then the important part: point at the compliance check that ran first, and the pre-debit notification rule that decided *when* the retry was allowed to happen. That's the difference between a retry loop and a payments product.

*Then the counter-example.* A hard decline — an expired card. The system doesn't retry. It suppresses, records why, and drafts a message asking the customer to update their card. Say the number: attempts saved by not retrying declines that could never have succeeded.

*Then the moment that sells the whole thing.* The model recommends an action. Policy denies it. Show the audit entry. Say plainly: the model wanted to act, the rules said no, and here's the log. Most agent demos show the agent succeeding. This one shows it being told no — and that's the more important capability.

*Then widen to the other two engines, briefly.* Root cause: a corridor degrading, the model's diagnosis distinguishing a bank-side outage from a cluster of ordinary customer failures, and the reroute that follows — bounded, with an expiry. Receivables: a customer replies in Hinglish with a hedged promise, the system extracts the commitment and the date, deprioritizes chasing them because they're cooperating, then escalates when the promise breaks. Keep both tight. They're proof of breadth, not the main story.

*Then the overview.* Total recovered across all three engines, in one number, with the trust strip beside it: denials, compliance blocks, suppressions, escalations, fallbacks. Say the thing that makes it credible: these numbers are non-zero on purpose, and a system that never refuses to act isn't bounded, it's just fast.

**3:45–4:30 — How it was built**

The role being hired is an AI builder intern, so the process is part of the pitch.

- Built in phases with Claude Code, each phase specified, verified, and committed before the next began — visible in the commit history.
- A `.claude/` layer of role-framed agents and domain skills so that a payments-risk rule is written by an agent grounded in the actual RBI documentation rather than plausible-sounding guesses.
- AI in the product, not just in the workflow: root-cause diagnosis, promise extraction from unstructured Hinglish replies, and message drafting are all genuine model judgment calls — the parts no rule engine could do. Every one of them has a defined deterministic fallback, so a batch run completes regardless of what the provider does.
- One honest technical obstacle, told properly: the real problem, what it cost, how it was solved. Pick from Phase 7's list. A real problem told well is more convincing than a claim of a smooth build.

**4:30–5:00 — Close**

- Restate the shape in one line: three revenue leaks, one bounded recovery engine, every action explainable.
- State the limitations directly: synthetic data, documented assumptions, simulated outcomes, communications generated but never dispatched, a prototype rather than a production system. Say it plainly. Ending on candor is a stronger note than ending on a claim, and it's the thing that makes a panel trust the numbers you just showed them.
- Close by returning to where you started: the subscriber who never meant to leave, and the fact that most of this money isn't lost — it's just unattended.

### C.3 Production notes

- **Rehearse before recording.** The demo path is the riskiest part; run it twice.
- **Pre-seed all data.** Nothing waits on a live batch unless you're deliberately showing progress, and even then, use a small run.
- **Have a recorded fallback clip** of the demo working, in case a live segment misbehaves during recording.
- Full screen, clean desktop, notifications off, terminal font large enough to read on a laptop.
- Speak at a measured pace. Five minutes is more than it sounds when you're not rushing.
- Do not read the script verbatim — know the beats and speak naturally. Stiff narration undercuts good content.
- Cut ruthlessly. If a segment doesn't advance the story, it goes, however much work it represents.

---

## Part D — Submission Fields

Prepare all five fields as `docs/SUBMISSION.md`, ready to paste.

**Project name** — Vasooli. Worth one line on why: it's the Hindi word for recovering money owed, and the product is built specifically for Indian payments constraints.

**Objectives / what it solves** — A few tight paragraphs: the three revenue leaks, the shared-core insight, the compliance grounding, and the headline measured result with its methodology named. Mention the boundedness model explicitly; it's what separates this from an agent that just acts.

**GitHub repository URL** — Public, README rendering correctly, deploy link if live.

**Pitch video link** — Accessible without login. Verify in an incognito window before submitting.

**Build challenges & technical obstacles** — Assembled by `docs-and-pitch-writer` from the real ADRs, Phase 7's weak-points list, and the git history. **Nothing invented.** Aim for three or four genuine obstacles, each with the problem, why it was hard, what was tried, and how it resolved. Include at least one you'd still solve differently with more time — that answer distinguishes a builder with judgment from one performing competence.

---

## 4. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| Handbook and demo script | `docs-and-pitch-writer` | — |
| Deployment configuration | `razorpay-integrator` (test-mode enforcement in production) | `razorpay-api` |
| Deployed build verification | `test-engineer` | — |
| Pitch script and submission fields | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

The `docs-and-pitch-writer` agent's standing constraint is critical here: pull challenges from real ADRs and git history. Never invent one for narrative effect. A fabricated obstacle is the single easiest thing for an interviewer to catch, and it would cost far more than it gains.

---

## 5. Acceptance criteria

- [ ] `docs/HANDBOOK.md` complete, covering setup, architecture, every feature demonstration, metric interpretation, and troubleshooting.
- [ ] `docs/DEMO_SCRIPT.md` written and rehearsed at least once end to end.
- [ ] Pre-flight checklist written.
- [ ] Deployment live and verified, **or** consciously skipped and noted in the README.
- [ ] If deployed: pre-seeded with data, test-mode enforced, batch trigger guarded, every route verified.
- [ ] Video script finalized with the beats in section C.2.
- [ ] Video recorded, under five minutes, uploaded, link verified in incognito.
- [ ] `docs/SUBMISSION.md` complete with all five fields.
- [ ] Technical-obstacles answer traceable to real ADRs and commits.
- [ ] Repository public, README rendering, no secrets in history.
- [ ] Final full test suite green.

---

## 6. Commit checklist

- `docs: add project handbook with full feature walkthrough`
- `docs: add demo script and pre-flight checklist`
- `chore(deploy): add deployment configuration for vercel and railway`
- `docs: add pitch script and storyboard`
- `docs: add submission answers with real technical obstacles`
- `chore: final pre-submission verification pass`

---

## 7. Stop condition

When every acceptance criterion is met, the submission is complete. Report: the deployed URLs if any, the video link, the final consolidated metrics as stated in the video, and confirmation that every submission field is filled and traceable to real artifacts.

Then stop. Resist the urge to add one more feature before the deadline — a broken demo an hour before submission is the only genuinely unrecoverable failure mode left at this point.
