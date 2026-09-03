---
description: Scaffold a new engine module matching the standard engines/<name>/ folder shape
argument-hint: <engine_name> (snake_case, e.g. checkout_recovery)
---

# Scaffold a new engine

Create a new engine module at `apps/api/app/engines/$1/`, matching the shape the
existing engines use.

## Before you start

Read an existing engine — `root_cause`, `mandate_recovery`, or `receivables` —
and match its structure. Consistency across engines is the point of this command.
If the existing engines have diverged from what's written below, **follow the
code and tell me the doc is stale.**

## The architectural rule this scaffold enforces

An engine's own code is almost entirely about two things:

1. **What data it ingests**
2. **What recovery actions it is allowed to call**

The actual judgment — is this retryable, what's the root cause, what's the next
bounded action, log it — **always** routes through the shared core:

- `services/policy_engine.py` — decides what is allowed
- `services/claude_agent.py` — supplies judgment
- `services/audit_trail.py` — records what happened

The hard part is built once. A new engine reuses it; it never reimplements it.

## Create

```
apps/api/app/engines/$1/
├── __init__.py
├── ingest.py      # load and normalize this engine's input data
├── engine.py      # orchestration: for each entity, ask policy, act, audit
├── actions.py     # the bounded set of recovery actions this engine may call
└── schemas.py     # Pydantic v2 models for this engine's entities
```

Plus a test module at `apps/api/app/tests/test_$1.py`.

## Hard requirements for the generated code

- **No decision logic in the engine.** No retry-eligibility branching, no
  threshold comparisons, no soft/hard classification. If you find yourself
  writing an `if` about whether an action is permitted, it belongs in
  `policy_engine.py`. Call it instead.
- **No direct Razorpay calls.** Everything goes through
  `services/razorpay_client.py`.
- **Every action writes an audit entry** through `services/audit_trail.py`,
  carrying `batch_id`, `reason_code`, and `authorising_rule` per the
  `audit-schema` skill.
- **Actions are an explicit, enumerable set** in `actions.py`. An engine that can
  call anything is not bounded, and boundedness is the product.
- **Pydantic v2** for all schemas. Money as integer paise. Timestamps tz-aware UTC.

## After scaffolding

1. Register any new routes under `apps/api/app/api/v1/routes/`.
2. Confirm `ruff check` is clean.
3. Report what was created and what still needs implementing — **do not**
   implement engine logic unless the current phase asks for it.

## Scope note

Building ahead of the current phase defeats the phased plan. Scaffold the module,
then stop and report.
