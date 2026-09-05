# How AI was used to build this

This is an AI-builder submission, so how the repo was built is itself evidence.
The whole thing was built with **Claude Code**, and the tooling that shaped it is
committed in [`.claude/`](../.claude/) rather than left in a chat history.

---

## The work was phased

Eight phase files, one at a time, each ending in a verification step and a
commit — shared core, synthetic data, then one engine per phase, then the
dashboard, then the integration pass. Building ahead was explicitly disallowed,
because the point of phasing is verifiable, committable progress.

[`docs/phases/PHASE-LOG.md`](phases/PHASE-LOG.md) is the record: what shipped,
what deviated from the plan and why, and the traps a later phase would otherwise
have walked into. It is written **after** each phase, never before, and it is
where the real technical obstacles are recorded.

## Skills carry the domain knowledge

[`.claude/skills/`](../.claude/skills/) holds the decline taxonomy, the RBI
mandate rules, Vasooli's own policy bounds, the corridor-detection thresholds,
the audit schema, the reasoning-provider contract and the Razorpay reference.

| Skill | Covers |
| --- | --- |
| `decline-taxonomy` | soft/hard classification, retry budgets per code |
| `rbi-mandate-rules` | AFA, the pre-debit notice window, the ₹15,000 threshold, revocation |
| `policy-bounds` | attempt ceiling, backoff, quiet hours, contact caps, the chase ladder, escalation triggers |
| `corridor-detection` | corridor definition, detection thresholds, minimum volume, reroute expiry |
| `audit-schema` | the one record shape every engine action writes |
| `llm-provider` | the provider interface, fallback contract, and provenance object |
| `razorpay-api` | auth, endpoints, error shapes, test-mode credentials |

Each is loaded *before* touching related code, and each names every rule so
`policy_engine.py` can cite it. This is what makes **no invented thresholds**
enforceable rather than aspirational: a number with no home in a skill file
cannot be written, and adding one means arguing for it in the skill first. The
integrity audit verifies that every citation in a run resolves to a rule that
actually exists — a citation pointing at nothing is worse than no citation,
because it looks like rigour.

The skills also separate **regulatory facts from our own product choices**, so it
is always clear which numbers are imposed and which are ours.

## Agents are Role / Task / Context framed

[`.claude/agents/`](../.claude/agents/) — `policy-architect`,
`razorpay-integrator`, `data-synthesizer`, `frontend-builder`, `test-engineer` —
each with an explicit remit rather than vague instructions.

## Commands are the repeatable checks

[`.claude/commands/`](../.claude/commands/) — `/audit-check`, `/run-batch-demo`,
`/new-engine`. `/audit-check` began as a checklist a human walks and became
executable code, for the reason given in
[ADR 0013](adr/0013-metric-integrity-as-an-executable-audit.md): a checklist
cannot say which of its twelve validations it skipped.

## Gemini is the product's reasoning layer, not the build's

Three registered tasks — diagnose a degraded corridor, draft a dunning message,
understand a customer's reply — each behind a provider interface, each with a
registered deterministic fallback, each producing a `provenance` object that
travels with the result everywhere it appears.
([ADR 0002](adr/0002-gemini-provider-and-deterministic-fallbacks.md))

---

## The evidence that this is real

On the first live mandate run the model recommended retrying a **paused** mandate
at confidence 1.00, and retrying four authentication-blocked mandates at 0.95.
Eight refusals in twenty-four drafts, each citing the specific rule that refused.

The model is well calibrated when asked to *judge* and overconfident when asked
to *act* — which is exactly why permission lives somewhere it cannot reach.
