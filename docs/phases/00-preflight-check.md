# Pre-Flight Check — Ready for Phase 1?

> Hand this to Claude Code before starting Phase 1. Run every check, record the actual result, and end with a clear verdict. Do not fix anything silently — report what was wrong and what you changed.

---

## How to run this

Work through the sections in order. For each check, run the command or inspect the file and record what you actually observed, not what you expect. A check that was skipped is a failed check.

Group findings into three buckets as you go:

- **Blocker** — Phase 1 cannot start until this is fixed
- **Fix now** — quick to correct, do it as part of this pass
- **Note** — worth knowing, doesn't stop anything

---

## 1. Environment and tooling

- [ ] Python version is 3.11 or compatible with what `requirements.txt` targets. Record the actual version.
- [ ] Node version supports Next.js 14. Record it.
- [ ] `git` is configured with a name and email, so commits attribute correctly. This matters — the commit history is part of the submission.
- [ ] The repo has a remote configured and `git push` works. Test it with an empty commit if nothing has been pushed yet.
- [ ] The GitHub repository is **public**. A private repo means the submission link fails for judges.
- [ ] Working tree is clean, or any uncommitted work is intentional and known.

---

## 2. Repository structure

- [ ] Every directory from the init specification exists: `.claude/` with `agents/`, `skills/`, `commands/`; `apps/web/`; `apps/api/`; `packages/shared-types/`; `data/generators/` and `data/samples/`; `docs/` with `adr/` and `pitch/`; `scripts/`.
- [ ] `apps/api/app/` contains `api/v1/routes/`, `core/`, `engines/` (with the three engine subfolders), `services/`, `models/`, `db/`, `tests/`.
- [ ] `apps/web/src/` contains `app/`, `components/ui/`, `components/features/`, `lib/`, `hooks/`, `types/`.
- [ ] No stray directories that aren't in the spec. If something extra exists, note why.

---

## 3. Backend boots

- [ ] Dependencies install cleanly from `requirements.txt`.
- [ ] FastAPI starts without errors. Record the command that works.
- [ ] The health-check route responds. Paste the actual response.
- [ ] Auto-generated OpenAPI docs load at `/docs`.
- [ ] `ruff check` runs and passes on `apps/api`.
- [ ] `pytest` runs without collection errors, even if there are no meaningful tests yet.
- [ ] SQLAlchemy is configured against SQLite and can create tables against the configured path.

---

## 4. Frontend builds

- [ ] Dependencies install cleanly.
- [ ] `npm run dev` starts and the default page loads.
- [ ] `npm run build` succeeds. A build that only works in dev will fail later at the worst time.
- [ ] Tailwind is applied — confirm a utility class actually takes effect, don't just check the config file exists.
- [ ] shadcn/ui is initialized and `components/ui/` is where its primitives will land.
- [ ] ESLint runs without configuration errors.

---

## 5. Configuration and secrets

- [ ] `.env.example` exists and lists exactly: Razorpay test key id and secret, `GEMINI_API_KEY`, `GEMINI_MODEL`, `LLM_DETERMINISTIC_ONLY`, `LLM_CACHE_PATH`, and the database path.
- [ ] A real local `.env` exists with values filled in.
- [ ] `.env` is gitignored and **is not tracked**. Verify with `git ls-files` — checking `.gitignore` alone is not sufficient, since a file committed before being ignored stays tracked.
- [ ] `.gitignore` covers: `node_modules`, `.next`, `__pycache__`, `.venv`, `.env`, `*.db`, `.pytest_cache`, `.ruff_cache`, and the LLM cache directory.
- [ ] No API key, secret, or credential appears anywhere in tracked files. Grep for the key prefixes specifically.
- [ ] No secret exists anywhere in git history, not just the working tree.

---

## 6. External services reachable

**Razorpay**

- [ ] Test-mode credentials are present and the key is a test key (`rzp_test_` prefix).
- [ ] A minimal live call against test mode succeeds — creating an order is the simplest proof. Paste the result.
- [ ] If this fails, record the exact error. Missing or unactivated test credentials is a blocker for Phases 3–5, though Phase 1 can proceed if the client wrapper is written and unit-tested against mocks.

**Gemini**

- [ ] `GEMINI_API_KEY` is present and `GEMINI_MODEL` is set to `gemini-3.1-flash-lite-preview`.
- [ ] A minimal live call succeeds and returns a response. Paste it.
- [ ] Confirm in AI Studio what your account's **actual current rate limits are** for this model — requests per minute and per day. Record the real numbers rather than assuming, since free-tier quotas have changed repeatedly. These numbers determine safe batch sizes in later phases, so they're worth knowing now.
- [ ] Confirm the model name is valid for your account. If it isn't, record which models *are* available and flag it — the model is config-driven, so switching is a one-line change, but the change must be made deliberately rather than discovered mid-phase.

---

## 7. The `.claude/` layer

This is what makes the rest of the build work correctly, so inspect the contents rather than just confirming files exist.

**Agents** — all six present: `policy-architect`, `razorpay-integrator`, `data-synthesizer`, `frontend-builder`, `test-engineer`, `docs-and-pitch-writer`.

- [ ] Each has an explicit Role, Task, and Context section. A file that's a vague paragraph is not done.
- [ ] `policy-architect` explicitly forbids inventing thresholds and requires citing the domain skills.
- [ ] `frontend-builder` explicitly forbids duplicating primitives that exist in `components/ui/`.
- [ ] `test-engineer` explicitly names the policy engine as the highest-priority coverage target.
- [ ] `docs-and-pitch-writer` explicitly forbids inventing build challenges.

**Skills** — all six present, including `llm-provider`.

- [ ] `decline-taxonomy` contains an actual lookup table mapping decline reasons to soft or hard and a default action. This is used by Phase 1's classifier and Phase 2's generators, so if it's thin, **fix it now** — a vague taxonomy will produce two incompatible implementations.
- [ ] `rbi-mandate-rules` contains the specific facts: AFA registration, the pre-debit notification window, the ₹15,000 threshold and what it governs, and revocation as a hard stop. Phase 4 depends entirely on this being concrete.
- [ ] `audit-schema` defines the full entry schema **including** the provenance fields: `source`, `provider`, `model`, `cache_hit`, `prompt_version`, `abstained`.
- [ ] `llm-provider` covers Gemini configuration, structured-output prompting, rate limits and backoff, caching, and the deterministic fallback contract.
- [ ] `razorpay-api` contains real endpoint shapes, not placeholders.
- [ ] `conventional-commits` defines the format and commit cadence.

**Commands** — `new-engine`, `run-batch-demo`, `audit-check` all present and describing something concrete.

**`CLAUDE.md`**

- [ ] Contains the pitch, the tech stack, folder conventions, a pointer to the agents, and the scope guardrail.
- [ ] The tech stack section names Gemini, not Anthropic.
- [ ] It is at the path Claude Code actually reads. Confirm it loads at session start rather than assuming.

---

## 8. Consistency after the provider migration

The change request touched many files. Verify it landed completely.

- [ ] Grep the entire repo for `claude_agent`, `ClaudeAgent`, `ANTHROPIC_API_KEY`, and `anthropic`. The only permitted matches are references to Claude Code as the development tool. Any runtime-provider reference is a **fix now**.
- [ ] `requirements.txt` has the Gemini SDK and no Anthropic dependency.
- [ ] The audit schema in `.claude/skills/audit-schema/SKILL.md` and any schema already written in code agree exactly. If they disagree, the skill file wins.
- [ ] `README.md`, `docs/architecture.md`, and `CLAUDE.md` all describe the same stack.
- [ ] `.env.example` matches what the config module actually reads. A variable in one and not the other is a silent failure waiting to happen.

---

## 9. Documentation baseline

- [ ] `README.md` exists with the project name, pitch, and architecture diagram.
- [ ] `docs/architecture.md` exists, even if thin.
- [ ] `docs/adr/` exists and contains the provider ADR from the migration.
- [ ] All eight phase files are in the repo where they can be referenced.

---

## 10. Phase 1 readiness

Confirm the specific things Phase 1 will immediately need:

- [ ] `apps/api/app/services/` exists and is empty or contains only the renamed wrapper.
- [ ] `apps/api/app/models/`, `db/`, and `tests/` exist.
- [ ] Phase 1's spec is available and its preconditions have been read.
- [ ] Nothing from Phase 2 or later has been built ahead of schedule. If engine logic or generators already exist, flag it — building ahead breaks the verify-and-commit rhythm the whole plan depends on.

---

## Verdict

End with one of these, stated plainly:

**GO** — every blocker clear, Phase 1 can start. List anything in the "note" bucket so it isn't forgotten.

**GO WITH CONDITIONS** — Phase 1 can start, but specific items must be resolved before a later phase. Name each one and which phase it blocks. The most likely case here is Razorpay test credentials not yet working: Phase 1 can proceed with a mocked client, but this becomes a blocker at Phase 3.

**NO GO** — something prevents Phase 1 from starting. For each blocker, state exactly what's wrong, the specific fix, and re-run the affected checks after fixing.

For anything you corrected during this pass, list what changed and commit it as `chore: pre-flight fixes before phase 1` before starting Phase 1.

---

## After the verdict

If GO or GO WITH CONDITIONS: commit any fixes, push, then begin Phase 1 by reading its phase file in full. Do not start Phase 1 in the same breath as reporting this check — report first, let the result be reviewed, then start.
