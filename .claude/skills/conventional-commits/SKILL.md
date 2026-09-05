---
name: conventional-commits
description: Commit message format for this repo — type(scope) subject lines, the allowed type and scope lists, and the cadence rule (commit after every meaningful working increment, not once per phase). Load before making any commit.
---

# Conventional Commits

## Why this matters more than usual here

The record of what this project actually went through is its **git history and
its ADRs** — never an invented narrative written afterwards.

That makes the git log a **first-class artifact**, not housekeeping. A history of
twelve commits saying `update` is a history that can produce nothing honest. Write
each message as though someone will read it back to you in an interview, because
they might.

## Format

```
<type>(<scope>): <subject>

<optional body — the why, wrapped at 72 chars>

<optional footer>
```

### Types

| Type | Use for |
| --- | --- |
| `feat` | New capability |
| `fix` | Bug fix |
| `test` | Adding or correcting tests |
| `docs` | Documentation, README, ADRs |
| `refactor` | Restructuring with no behaviour change |
| `chore` | Tooling, deps, config, scaffolding |
| `perf` | Performance work |
| `ci` | Pipeline / automation |
| `revert` | Reverting a prior commit |

### Scopes

Match the architecture, so the log reads as a map of the system:

`root-cause` · `mandate` · `receivables` · `policy` · `agent` · `audit` ·
`razorpay` · `data` · `api` · `web` · `claude` · `repo`

`policy`, `agent` and `audit` are the shared core. A commit touching those
affects all three engines — say so in the body.

### Subject line rules

- Imperative mood: "add", not "added" or "adds"
- Lowercase after the colon
- No trailing period
- Aim for ≤ 72 characters
- Say what changed, not that something changed

## Cadence — the rule that actually matters

> **Commit after every meaningful, working increment. Not once per phase.**

"Working" means the thing you just added actually runs, and its tests pass if it
has any. A phase is typically **several** commits.

Good increments to commit on:

- A skill or agent file authored
- The policy engine gaining one new rule, with its test
- One engine's ingest path working end to end
- The audit writer persisting its first record
- A dashboard component rendering real data

Do **not** bundle unrelated changes. If the subject line needs an "and", it is
probably two commits.

## Never commit

- `.env` or any real key. **Test-mode keys only, and never committed.**
- `*.db` / SQLite files
- `node_modules/`, `.next/`, `__pycache__/`, `.venv/`

If a secret does get committed, rotating the key is the fix. Deleting the file in
a later commit is not — it stays in history.

## Examples

Good:

```
feat(policy): add pre-debit notice window gate

Blocks any debit whose notice is missing or older than 48h, per
rbi-mandate-rules A2. Blocked attempts write a `blocked` audit entry
citing the rule rather than failing silently.
```

```
fix(razorpay): map card_declined to AMBIGUOUS, not SOFT

Razorpay returns card_declined with no issuer reason, so it can be
either a funds problem or a dead card. Treating it as SOFT was
spending the full 4-attempt budget on cards that were never going to
clear. Reduced budget per decline-taxonomy AMBIGUOUS.
```

```
test(policy): cover revoked-mandate hard stop across all paths
```

Bad:

```
update files
fix stuff
feat: did the mandate engine and also fixed the dashboard and readme
WIP
```

## Attribution

Commits created by Claude Code in this repo end with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

## Before committing

1. The increment actually works — you ran it.
2. `ruff check` clean for Python, `npm run lint` clean for TypeScript.
3. No secrets, no `.env`, no database files in the diff.
4. `git diff --staged` reviewed. You are about to sign your name to it.
