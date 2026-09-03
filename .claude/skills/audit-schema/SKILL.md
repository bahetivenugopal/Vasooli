---
name: audit-schema
description: The single audit-trail record schema every Vasooli engine writes to — required fields, allowed action/outcome values, and the rule-citation requirement. Defined once, before any engine is built. Load BEFORE writing audit_trail.py, before adding any new engine action, and before changing anything that records a decision.
---

# Audit Trail Schema

## The claim this schema has to support

The submission claims every recovery action is **bounded, explainable, and
provably compliant**. That claim is worth exactly as much as the audit trail
behind it. If a judge picks any single action out of a batch run and asks "why
did it do that, and what stopped it going further?", the answer must come out of
one table, in one query, without anyone reconstructing it from logs.

So: **one schema, defined here, before any engine exists.** All three engines
write the same records through `services/audit_trail.py`. No engine gets its own
log format.

## The non-negotiable rule

> **Every entry cites the rule that authorised it.**

An entry with a null or hand-waved `authorising_rule` is a bug, not a gap. This
holds for actions that *did not happen* too: a blocked attempt is one of the most
valuable records in the system, because it is the evidence that the stopping
rules are real.

`/audit-check` validates exactly this and fails the run if any entry lacks a
reason code or a citing rule.

## Required fields

Minimum viable record. Adding fields is fine; removing any of these is not.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | int / uuid | yes | Primary key |
| `timestamp` | datetime (UTC, tz-aware) | yes | When the decision was made. UTC always — never naive local time |
| `batch_id` | str | yes | Ties the entry to one reproducible synthetic batch. Without this, no per-batch recovery number is defensible |
| `engine` | enum | yes | `root_cause` \| `mandate_recovery` \| `receivables` |
| `entity_type` | enum | yes | `payment` \| `mandate` \| `invoice` \| `corridor` |
| `entity_id` | str | yes | The transaction / mandate / invoice / corridor this concerns |
| `action` | enum | yes | What was done, or refused. See below |
| `outcome` | enum | yes | How it turned out. See below |
| `reason_code` | str | yes | Normalized code from `decline-taxonomy`, or a policy reason code |
| `authorising_rule` | str | yes | **The citation.** See "Rule citation format" |
| `provenance` | object | yes | How the decision was produced. See "Provenance" below. **Replaces the old `decision_source` field** — keep one, not both |
| `rationale` | text | yes | Human-readable why, one or two sentences |
| `amount_paise` | int | no | Integer paise. Never floats for money |
| `currency` | str | no | `INR` |
| `attempt_number` | int | no | Which attempt in the schedule this was |
| `attempts_remaining` | int | no | Budget left after this decision — makes boundedness visible at a glance |
| `model_confidence` | float | no | Required whenever `provenance.source` is `model`. Distinct from provenance: it describes the *answer*, not how it was produced |
| `metadata` | JSON | no | Engine-specific extras. Never put a required field in here |

### `action` values

| Value | Meaning |
| --- | --- |
| `classify_decline` | A decline was classified soft/hard/ambiguous |
| `diagnose_root_cause` | Engine 1 judged customer-level vs corridor-level |
| `schedule_retry` | A retry was placed on the schedule |
| `attempt_charge` | A debit/charge was actually attempted |
| `send_pre_debit_notice` | RBI pre-debit notification sent |
| `reroute_traffic` | Engine 1 rerouted a degraded corridor |
| `send_dunning` | A dunning/recovery message was sent |
| `send_reminder` | Engine 3 sent an invoice reminder |
| `record_promise_to_pay` | A payment commitment was extracted and stored |
| `escalate` | Escalation up the capped ladder |
| `block_attempt` | **A precondition failed and the action was refused** |
| `halt_schedule` | Terminal stop — no further attempts, ever |

### `outcome` values

`success` · `failure` · `blocked` · `scheduled` · `skipped` · `pending` · `escalated` · `halted`

`blocked` and `halted` are the compliance-evidence outcomes. They should appear
in every honest batch run. A run with zero blocks means either the batch had no
edge cases or the gate is not firing — both are worth investigating.

## Rule citation format

`authorising_rule` is a stable string that points at a *specific* named rule,
never a vague description.

```
<skill-or-module>:<rule-id>
```

Examples:

- `rbi-mandate-rules:A4` — revoked mandate, terminal stop
- `rbi-mandate-rules:A2` — pre-debit notice window
- `rbi-mandate-rules:PartB.max_retries` — Vasooli-chosen retry ceiling
- `decline-taxonomy:HARD.CARD_EXPIRED` — hard decline, route to dunning
- `decline-taxonomy:UNKNOWN.fail_safe` — low-confidence classification, treated as hard
- `policy_engine:promise_to_pay.grace_window` — Engine 3 grace before escalating

If no rule id exists for a decision, the correct fix is to **add a named rule to
the relevant skill**, not to invent a citation string at the call site.

## Provenance

Every entry carries a `provenance` object. Full contract in the `llm-provider`
skill — **the two files must agree exactly.**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `source` | enum | yes | `model` \| `deterministic`. **The most important field in the entry** — it says whether a judgment was *reasoned* or *ruled* |
| `provider` | str | yes | Exactly what produced it, e.g. `gemini`. `deterministic` for fallbacks |
| `model` | str | yes | Exactly which model. Null for deterministic results |
| `cache_hit` | bool | yes | Whether it came from cache rather than a live call. A cached result is still `source: model` |
| `prompt_version` | str | yes | Which prompt produced it. Null for deterministic results |
| `abstained` | bool | yes | Whether the task declined to answer and routed to human review |
| `latency_ms` | int | no | **Null for deterministic results** |
| `tokens` | int | no | **Null for deterministic results** |

> **Nothing model-derived may appear anywhere in the system without this object
> attached** — not in an audit entry, not in an API response, not on the
> dashboard, not in a reported metric.

### Purely mechanical decisions

A decision taken straight from a lookup table, with no model involved and no
fallback invoked, is still `source: deterministic` with `provider:
deterministic`. There is no third value. The question the field answers is
"was this reasoned or ruled?", and a table lookup was ruled.

### Abstention

When a task declines to answer rather than guess — the Engine 3 promise-extraction
fallback being the designed case — record it as `action: escalate`,
`outcome: escalated`, `provenance.abstained: true`, `source: deterministic`.

An abstention is a *successful* outcome for that task, not a failure. Fabricating
a promise-to-pay would suppress a legitimate chase and corrupt the promise
register, so refusing is the correct behaviour and the trail should show it as
deliberate.

### Reporting rule

**Metrics are reported per source.** Never blend model-derived and rule-derived
results into a single figure that implies more than it delivers.

A recovery rate where half the decisions came from static templates is not a
false number, but presented as one figure it is a misleading one. Split it.

### The split that matters

Whatever the source, `authorising_rule` still points at the policy rule that
**permitted** the action, and at the bound it operated inside.

The model is never the authority for whether an action was *allowed*. It supplies
judgment; `policy_engine.py` supplies permission. The audit trail must make that
split visible — it is the difference between "an agent that tries stuff" and a
bounded recovery system, and it holds regardless of which provider is behind the
reasoning layer.

## Writing rules

- **Write-once, append-only.** Audit entries are never updated or deleted. A
  correction is a new entry.
- **Write before acting**, not after, for anything with an external side effect.
  An action that happened without a record is unprovable; a record for an action
  that then failed is just an entry with `outcome = failure`.
- **Money is integer paise.** Never a float, anywhere in this table.
- **Timestamps are tz-aware UTC.**
- The writer lives at `apps/api/app/services/audit_trail.py`. Engines never touch
  the audit table directly.

## Consumers

- `/audit-check` — validates every entry has a reason code and a citing rule
- `/run-batch-demo` — computes the recovery-rate summary from these entries, so
  the headline number and the audit trail cannot disagree. Reports **per
  `provenance.source`**, never blended
- The control tower dashboard — renders the per-entity decision timeline,
  showing `provenance.source` on every decision
