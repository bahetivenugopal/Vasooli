# Interface screenshots

Captured from a live run against the committed seed-42 sample batches, with

```bash
cd apps/web && npm run screenshots
```

Every one is a real page rendering real data from a completed batch — the
capture script refuses to save a page that rendered an error state, because the
first run of it quietly produced eight plausible-looking pictures of the "could
not reach the API" panel.

| File | Surface | Why it is here |
| --- | --- | --- |
| `01-overview.png` | Overview | The headline, the per-engine contributions with each engine's own recovery definition, and the trust strip |
| `02-engine-root-cause.png` | Engine 1 | Detection scored against ground truth — recall 100%, precision 66.7%, and the one false positive printed in full |
| `03-engine-mandate-recovery.png` | Engine 2 | Both denominators, the AFA branch, and compliance blocks by rule |
| `04-engine-receivables.png` | Engine 3 | Extraction confusion matrices, the ranked worklist with score breakdowns, and the promise register in the customer's own words |
| `05-audit-trail.png` | Audit trail | Every filter, and the policy-denials-only preset |
| `06-timeline-corridor.png` | Corridor timeline | The statistics, the model's diagnosis verbatim, and the gate's answer |
| `07-timeline-mandate-compliance-blocked.png` | Mandate timeline | **An unhappy path** — a debit an RBI precondition refused |
| `08-timeline-invoice-broken-promise.png` | Invoice timeline | **The whole argument on one screen** — reminder, reply verbatim, the model's reading, the promise, its breach, the escalation |
| `09-failure-state-api-unreachable.png` | Failure state | The API down. A visible, actionable error rather than a blank page |
| `10-empty-state-no-runs.png` | Empty state | No completed runs: how to get data, not a bare empty table |
| `11-empty-state-audit.png` | Empty state | The audit trail with nothing in it |
| `12-empty-state-engine.png` | Empty state | An engine view with no runs |
| `13-run-batch-in-progress.png` | Batch trigger | A run in flight, with an elapsed counter — the screen never looks frozen |
| `14-run-batch-complete.png` | Batch trigger | The completed run's own figures, and the honest "1 / 3 engines reporting" line while the other two have no run yet |
| `15-run-batch-duplicate-id-refused.png` | Batch trigger | Reusing a batch id is refused, because it would merge two runs into one set of metrics |

The unhappy paths are deliberate. A gallery of successes is exactly the
cherry-picking the competition's bar warns against.
