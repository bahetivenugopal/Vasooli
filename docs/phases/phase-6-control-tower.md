# Phase 6 — The Control Tower Dashboard

> Drop this file into the repo and hand it to Claude Code. Do not proceed past the stop condition at the end.

---

## 1. Goal

Build the Next.js frontend that makes everything the three engines do visible: a headline recovery number across all three, per-engine drill-downs, single-entity timelines that tell a complete story, and a live audit trail viewer that proves every action was bounded and authorized.

---

## 2. Why this phase comes sixth

Three working engines that can only be observed through JSON responses will lose to a weaker project with a clear interface, because a five-minute video cannot show a terminal for five minutes and hold attention. This phase is where the work becomes legible.

But the ordering matters in the other direction too: building the UI last means it displays real measured numbers rather than placeholders that later need reconciling. Nothing in this dashboard should ever compute a metric — it reads what the audit trail already computed. If the UI ever calculates a number itself, there are now two sources of truth and one of them will eventually be wrong on camera.

---

## 3. Preconditions

- Phases 1–5 complete, committed, acceptance criteria passed.
- All three batch runners produce summaries and all API routes respond.
- `apps/web` scaffold from the init phase exists with Tailwind and shadcn/ui configured.

---

## 4. Scope

### In scope
- Overview page with the cross-engine headline metric.
- Three per-engine views with their specific metrics and worklists.
- Single-entity timeline views (corridor detection, mandate, invoice).
- Audit trail viewer with filtering.
- Batch-run triggering with progress feedback.
- Typed API client generated from or aligned to the backend's OpenAPI schema.

### Out of scope
- Authentication, multi-tenancy, user management. This is a demonstration console, not a SaaS product, and building auth would consume time that belongs to the demo.
- Real-time websockets. Polling during a run is sufficient and far more reliable to demo.
- Mobile-responsive perfection. Make it not break on a narrower window; do not spend hours on it. The video will be recorded on a desktop.
- Any metric computation client-side.

---

## 5. Design specification

### 5.1 Information architecture

Four surfaces, in descending order of importance to the demo:

1. **Overview** — the whole story in one screen.
2. **Engine views** — one per engine, each with its own worklist and metrics.
3. **Entity timelines** — the narrative surface where a single case unfolds step by step.
4. **Audit trail** — the proof surface.

Navigation should be flat and obvious. A judge should never wonder where they are.

### 5.2 Overview page

The single most important screen in the project. It must answer, within about three seconds of being seen: *how much money was at risk, how much came back, and can I trust it?*

- **Headline metric**, unmissable: total value recovered against total at risk, with the recovery rate, aggregated across all three engines. Use the recovery-green from the visual identity so the number reads as a positive outcome at a glance.
- **Per-engine contribution** — a breakdown showing how each engine contributed, so the breadth claim is visible rather than asserted.
- **Trust strip** — a compact row of the numbers that prove boundedness: policy denials, compliance-blocked actions, messages suppressed, human escalations raised, deterministic fallbacks handled, and abstentions routed to human review. These should be presented as *features*, with brief labels making clear that non-zero is good. Most dashboards hide their refusals; showing them is the point.
- **Recent activity** — the last several audit entries, each with its authorizing rule, linking through to the full trail.
- A visible **batch run trigger** so a live demo can start a run on camera.

Every number here comes from the batch summary API. No client-side arithmetic beyond formatting.

### 5.3 Engine views

Each engine gets a view shaped to its own story:

- **Root cause** — corridor health at a glance, detections with observed-versus-baseline rates, detection performance against ground truth including false positives shown honestly, and the actions taken per detection.
- **Mandate recovery** — recovery split by failure class, the above/below AFA threshold breakdown, retries correctly suppressed with wasted attempts avoided, mandates stopped at cap, and the upcoming retry schedule showing what will happen next and which rule permits it.
- **Receivables** — the ranked worklist with score breakdowns, the promise register with kept/broken/active statuses, extraction accuracy with its confusion matrix, and messages suppressed by caps or quiet hours.

Charts via Recharts, kept simple. A clear bar chart beats an elaborate visualization that needs explaining.

### 5.4 Entity timelines

This is the surface that will carry the video, because a single case unfolding chronologically is a story, while a dashboard of aggregates is a report.

Each timeline shows, in order: what happened, what the system decided, **which rule authorized it**, what the model reasoned (verbatim, where an LLM call was involved), what action followed, and what the outcome was. Rule-based steps and LLM-reasoned steps must be visually distinguishable at a glance — that distinction is the product's core safety claim and it should be readable without explanation. Each reasoning block carries a compact **provenance badge**: model name, whether it was served from cache, and — where the deterministic fallback produced it — a clearly distinct marker. A judge scrolling a timeline should be able to tell at a glance what was reasoned, what was ruled, and by what.

Include the unhappy paths prominently: a denial, a suppression, a fallback, an escalation. A timeline that only shows success is exactly the cherry-picking the competition's bar warns against.

### 5.5 Audit trail viewer

A filterable table over the full trail: by batch run, engine, entity, action, and decision source. Each row expands to show the full reasoning and the authorizing rule.

Include a filter preset for "policy denials only" — being able to click one control and show a judge every action the system refused to take is a genuinely strong demo moment.

### 5.6 Engineering constraints

- **Component reuse is enforced, not encouraged.** shadcn primitives live in `components/ui/` and are never duplicated. Composed, feature-specific components live in `components/features/`. Metric cards, timeline rows, rule badges, and money formatters are each defined once. If a second near-identical component appears, that's a defect to fix, not a shortcut to accept.
- **One money formatter.** Paise-to-rupee conversion and formatting happens in exactly one utility, used everywhere. Money formatting bugs on camera are avoidable and embarrassing.
- **Types come from the backend.** Generate or hand-align types from the FastAPI OpenAPI schema into `packages/shared-types/`. Do not hand-write drifting duplicates.
- **Loading and error states everywhere.** A batch run takes time; the UI must show progress rather than appearing frozen. Every fetch has a visible failure state. A blank screen during a live demo is worse than an error message.
- **Empty states** that explain how to get data (run a batch) rather than showing a bare empty table.
- Follow the visual identity: neutral base, grey/red for at-risk, teal/green for recovered, used consistently enough that color alone communicates state.

---

## 6. Agents and skills to use

| Component | Agent | Skills it must consult |
|---|---|---|
| All frontend work | `frontend-builder` | — |
| API client and type alignment | `frontend-builder` | — |
| Verifying displayed numbers match audit-derived values | `test-engineer` | `audit-schema` |
| Docs and screenshots | `docs-and-pitch-writer` | — |
| Commits | — | `conventional-commits` |

The `frontend-builder` agent's standing constraint applies throughout: never create a new primitive if one exists in `components/ui/` — reuse or extend.

---

## 7. Testing requirements

Frontend testing is deliberately light here; the backend carries the correctness burden. Focus on what would break a demo:

1. **Number fidelity** — a check that headline figures rendered match the batch summary API exactly. Formatting must not alter values.
2. **Money formatting** — paise-to-rupee conversion correct at boundaries, including large values with Indian-format separators.
3. **Manual smoke checklist** — a documented click-through covering every route with data present and with data absent, committed to the repo so it can be re-run before recording.
4. **Failure rendering** — API down produces a visible error state, not a blank page.

---

## 8. Acceptance criteria

- [ ] `npm run build` succeeds; ESLint and Prettier clean.
- [ ] Every route renders with real data from a completed batch run.
- [ ] Overview headline matches the API's summary values exactly.
- [ ] The trust strip displays policy denials, suppressions, escalations, and fallbacks.
- [ ] Each engine view renders its specific metrics, including honestly-shown false positives and extraction accuracy.
- [ ] At least one timeline of each type renders a complete story including an unhappy path.
- [ ] Rule-based and LLM-reasoned steps are visually distinguishable without explanation.
- [ ] The audit viewer filters correctly, including the denials-only preset.
- [ ] A batch run can be triggered from the UI with visible progress and a completed result.
- [ ] No duplicated components; no client-side metric computation; one money formatter.
- [ ] Smoke checklist passes end to end.

---

## 9. Documentation to produce

- Update `README.md` with frontend setup and run instructions.
- Commit the manual smoke checklist to `docs/`.
- Capture screenshots of each major surface into `docs/pitch/` — these feed the video and the README, and having them before Phase 8 saves time under pressure.

---

## 10. Commit checklist

- `feat(web): add typed API client and shared types from OpenAPI`
- `feat(web): add overview page with cross-engine headline and trust strip`
- `feat(web): add root-cause engine view`
- `feat(web): add mandate recovery engine view`
- `feat(web): add receivables engine view`
- `feat(web): add entity timeline views with rule and reasoning attribution`
- `feat(web): add filterable audit trail viewer`
- `feat(web): add batch run trigger with progress states`
- `docs(web): add smoke checklist and interface screenshots`

---

## 11. Stop condition

When acceptance criteria pass and commits are pushed, **stop and report back**: which routes exist, confirmation that displayed numbers match API values, screenshots captured, and any place where the API made the UI awkward — that's useful signal for Phase 7's integration pass.

Do not begin Phase 7. The next phase file will be provided separately.
