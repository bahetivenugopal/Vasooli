# Failure paths — rehearsed, not hoped for

Ten ways this system is meant to survive, each triggered on purpose, each
asserted in `apps/api/app/tests/test_failure_paths.py`. Run them:

```bash
cd apps/api && pytest app/tests/test_failure_paths.py -v
```

Two things are asserted for every path, because either alone is insufficient:

1. **The run completed.** A batch that dies halfway proves nothing.
2. **The trail explains what happened.** Surviving quietly is worse than failing
   loudly — a system that degrades without recording it reports fallback output
   as though it were reasoning.

The right-hand column is the one Phase 8 needs: **is this worth pointing a camera
at?**

| # | Failure | How it degrades | On camera? |
| --- | --- | --- | --- |
| 1 | Gemini unavailable mid-batch | reasoned reads before the fault stay `source: model`; everything after takes its registered fallback and is audited `source: deterministic` with `degradation_reason: "provider unavailable: …"`. The batch completes. | **Yes** — the mixed-provenance trail is the clearest single picture of the fallback contract |
| 2 | Rate limit beyond backoff | 1s / 2s / 4s backoff is actually attempted, then the task falls back. Reason recorded as `rate-limited beyond backoff`. | Mention, don't show — it is four seconds of nothing happening |
| 3 | Deterministic-only across a full run | the whole unified run completes with **zero** model entries; the report's first screen says `DETERMINISTIC ONLY` before any number appears | **Yes** — "here it is with no API key at all" is the most practically valuable property in the build |
| 4 | Malformed LLM output | reprompted once with the validation error fed back; a second failure falls back with `unparseable output twice` | Optional |
| 5 | Razorpay test-mode API error | the error object is normalised through the same mapping table a real test-mode response uses; the failure is audited with the taxonomy code and the taxonomy's own rule id, and never raised | Optional |
| 6 | An LLM recommendation policy denies | the gate refuses and the trail cites the **specific** rule that refused (never a generic authority string); for a dunning draft the template is substituted so the customer still hears something accurate | **Yes — the single best thing in the project.** See below |
| 7 | Mandate revoked mid-sequence | `rbi-mandate-rules:A4` halts the schedule; the validator's terminal-means-terminal check confirms no `attempt_charge` follows a `halt_schedule` on any path | **Yes** — pairs naturally with #6 |
| 8 | Dispute arriving mid-chase | `policy-bounds:RL4` freezes that invoice's ladder immediately; no reminder is sent to a customer who disputed; other customers keep being chased | **Yes** — the "never harassing" claim, demonstrated |
| 9 | Empty or malformed input batch | an empty file and a malformed record are both refused with a `DatasetError` naming the file and the line, before any batch row is opened | No |
| 10 | Backend down while the dashboard is open | the browser half is in [`smoke-checklist.md`](smoke-checklist.md) and screenshotted; the API half is asserted — a crashed leg is marked `failed`, and the overview reads **completed** runs only, so a partial trail can never become the headline | Show the browser half only |

---

## Path 6 in detail — the one to actually show

This is the best evidence in the project that "the policy engine has final
authority" is structural rather than aspirational, and it is better evidence than
anything the accuracy numbers provide.

Forced in the rehearsal, and observed unprompted on Phase 4's first live run:
the model recommended `schedule_retry` on a **paused** mandate at confidence
**1.00**, and on four mandates blocked on authentication at 0.95. Eight refusals
in twenty-four drafts. The gate refused every one, and each refusal names the
rule that did it.

What makes it demonstrable rather than merely true:

- The refusal is **on the record**, not in a log. It is an audit entry with an
  outcome of `blocked` and a citation like `rbi-mandate-rules:PartB.paused`.
- The citation is **specific**. Phase 3 fixed a defect where every overruled
  denial cited the same generic `llm_authority.denied`, which made the trail
  unable to answer "which rule stopped this?".
- The customer is **not silenced**. An inadmissible recommendation substitutes
  the deterministic template rather than holding the message — the model had
  written text announcing a retry that is not coming, so sending it would have
  promised something false and holding it would have left the customer with
  nothing.

## What confidence is, and is not, good for

Worth knowing before relying on it in a demo:

- **The model is well calibrated when asked to judge.** On a genuinely ambiguous
  corridor it returned 0.45 — below the 0.60 floor — and the deterministic
  classifier took over. On the adversarial reply probe it returned 0.20 on an
  empty reply and 0.50 on a date range, both correctly abstained.
- **It is overconfident when asked to act.** Every overruled recommendation in
  Phase 4 arrived at 0.95–1.00.

So `min_confidence` is a useful signal on judgment-shaped tasks and is **not** a
safety mechanism for action-shaped ones. The policy engine is the safety
mechanism. That distinction is the reason the design puts permission somewhere
the model cannot reach.

## Known rough edges in these paths

Stated because Phase 8 should know what not to point at:

- **Path 2 has no visible artefact.** The backoff is real and asserted, but on
  screen it is a pause.
- **Path 5's fixtures cover eight verified Razorpay reasons.** Everything else
  normalises through `UNKNOWN`, correctly and by design — but a demo that picks
  an unlisted scenario shows the fail-closed path rather than the mapping.
- **Path 8 is forced in the test** by making every reply a dispute. The corpus's
  eight genuine disputes only surface with a live provider, which is fine for the
  demo and worth knowing when reading the test.
- **Path 10's browser half is not automated.** It is a manual step in the smoke
  checklist with a screenshot beside it. Automating it would need a browser
  harness this project does not have.
