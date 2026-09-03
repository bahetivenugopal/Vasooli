---
description: Dump and validate the audit trail for a batch run — confirm every entry has a reason code and a citing rule
argument-hint: <batch_id> [--dump]
---

# Audit check

Validate the audit trail for batch `$1`. Pass `--dump` to print every entry in
full as well as the validation result.

Read the `audit-schema` skill first — it defines the required fields, the allowed
`action` and `outcome` values, and the `<skill>:<rule-id>` citation format.

## The rule being enforced

> **Every entry cites the rule that authorised it.**

An entry with a missing or hand-waved `authorising_rule` is a **bug, not a gap**.
This applies to actions that did not happen too — a blocked attempt is one of the
most valuable records in the system, because it is the evidence the stopping
rules are real.

## Validations — fail the run on any of these

1. **Required fields present** on every entry: `timestamp`, `batch_id`, `engine`,
   `entity_type`, `entity_id`, `action`, `outcome`, `reason_code`,
   `authorising_rule`, `provenance`, `rationale`.
2. **`reason_code` is non-empty** and is a known `decline-taxonomy` code or a
   defined policy reason code.
3. **`authorising_rule` is non-empty** and matches `<skill-or-module>:<rule-id>`.
4. **The cited rule exists.** Resolve each citation against the skill files —
   `rbi-mandate-rules:A4` must actually be a rule in that skill. A citation
   pointing at nothing is worse than no citation, because it looks like rigour.
5. **`action` and `outcome` are allowed values** from `audit-schema`.
6. **`provenance` is complete and internally consistent** on every entry:
   - `source` is `model` or `deterministic` — no other value, never absent
   - `provider`, `cache_hit`, `prompt_version` and `abstained` all present
   - `source: deterministic` ⇒ `latency_ms` and `tokens` are **null**, and
     `model` / `prompt_version` are null
   - `source: model` ⇒ `model` and `prompt_version` are populated, and
     `model_confidence` is present on the entry
   - `abstained: true` ⇒ `action` is `escalate` and `outcome` is `escalated`
7. **Timestamps are tz-aware UTC**, and non-decreasing within an entity's timeline.
8. **Money is integer paise** — any float in an amount field is a failure.
9. **No secrets**: no key material, no full card numbers, anywhere in the trail.

## Consistency checks

10. **Every action that changed money state has an entry.** Cross-check against
    the engine run — an unlogged action is unprovable and counts as a failure.
11. **Bounds were respected.** No entity exceeded its retry budget; where
    `attempts_remaining` is recorded, it decreases monotonically and never goes
    negative.
12. **Terminal means terminal.** After a `halt_schedule` entry, no later
    `attempt_charge` exists for that entity. This is the `MANDATE_REVOKED` hard
    stop — check it across every code path, not just the obvious one.

## Output

```
Batch: <batch_id>        Entries: <n>

PASS/FAIL: <result>

Violations: <n>
  <entry id> — <which validation failed> — <detail>
  ...

Rule citations used:
  rbi-mandate-rules:A4        x<n>
  decline-taxonomy:HARD.*     x<n>
  ...

Provenance split:
  model <n>  (cache hits <n>, live calls <n>)
  deterministic <n>  (abstentions <n>)
  by prompt_version: <version> x<n>, ...

Outcomes:
  success <n> · failure <n> · blocked <n> · halted <n> · scheduled <n> · ...
```

## Reporting rules

- **Report failures plainly, with the offending entries.** A passing audit check
  that skipped a validation is worse than a failing one.
- **Zero `blocked` entries across a whole batch is suspicious**, not good. Say so.
  Either the batch lacks edge cases or a gate is not firing.
- Do not fix violations silently as part of this command. Report them, then fix
  them as a separate, committed change.
