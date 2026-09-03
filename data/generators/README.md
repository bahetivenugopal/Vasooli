# Synthetic data generators

Three seeded generators — payments, mandates, invoices — plus the shared
seeding, manifest and hashing machinery they all use.

> **The distributions and their rationale live in [`../DATA_CARD.md`](../DATA_CARD.md).**
> This file is the operator's guide: how to run them, how they are put together,
> and what to be careful about when changing them.

## Run

From the **repo root**:

```bash
python -m data.generators.cli all      --seed 42 --out data/samples
python -m data.generators.cli payments --seed 42 --out /tmp/check
python -m data.generators.cli mandates --seed 7  --config data/generators/configs/mandates.default.json --out /tmp/big
```

Each run writes `<dataset>.jsonl` and `<dataset>.manifest.json`, and prints the
batch id and output hash. To verify a committed sample:

```bash
python -m data.generators.cli all --seed 42 --out /tmp/check
diff /tmp/check/payments/payments.jsonl data/samples/payments/payments.jsonl   # silent
```

## Layout

| File | What it is |
| --- | --- |
| `common.py` | Seeding, IST/UTC time, weighted draws, exact allocation, hashing, manifests, `GeneratedBatch` |
| `vocabulary.py` | The failure vocabulary — imported from the shared taxonomy, never redefined here |
| `payments.py` · `mandates.py` · `invoices.py` | The three generators |
| `replies.py` | The customer-reply corpus and its per-template ground-truth annotations |
| `retry_model.py` | The retry-success probability model the **engines** sample at run time |
| `configs/*.json` | The worlds being simulated, in version control |
| `cli.py` | One entry point for all three |

Generators live outside `apps/api/` because they are the evidence base, not part
of the service. `data/generators/__init__.py` puts `apps/api/` on `sys.path` so
the taxonomy can be imported from the shared core — the same bootstrap
`scripts/core_loop_demo.py` uses.

## Rules for changing anything in here

1. **Never invent a decline reason.** Every failure code is a `DeclineCode` from
   `app/services/decline_taxonomy.py`. If the data genuinely needs a reason the
   taxonomy lacks, add it to the `decline-taxonomy` **skill** first, then to the
   service, then use it. `validate_codes()` fails the run otherwise, before a
   single record is written.
2. **Never populate `razorpay_reason` with a string that is not doc-verified.**
   The eight verified reasons come from `REASON_MAP`; anything else normalizes to
   `UNKNOWN` and would mislabel the failure on replay. `null` is the correct
   value, and it means something. (`UNKNOWN`-class failures are the deliberate
   exception — see the data card, §7.)
3. **Never put ground truth in a record.** Cohort labels, degradation labels,
   reply annotations and archetypes go in the manifest. A test walks every nested
   field and fails on leakage.
4. **Never decide a retry outcome.** Generators produce the world; engines
   produce the outcome (ADR 0005). If you find yourself writing
   `retry_succeeded`, stop.
5. **Time is an input.** No `datetime.now()` in generation logic — the window
   comes from the config's `window_start` / `as_of`. The only clock read is the
   manifest's `nondeterministic.generated_at`.
6. **Draw from a labelled stream.** `rng(seed, dataset, "amounts")`, not a shared
   `Random`. Streams are separated so that adding a draw to one concern does not
   shift every value in the others — otherwise appending one field silently
   changes an entire batch and "regenerate and diff" stops being usable.
7. **Re-run the samples after any change that alters output**, and say so in the
   commit. `data/samples/` and the generators are one artifact; a sample that no
   longer regenerates is worse than no sample.

## Scaling a batch up

The committed samples are sized to stay browsable on GitHub. For a larger run,
copy a config and raise the counts — `attempts_per_day` / `days` for payments,
`mandate_count`, `invoice_count` — then pass `--config`. Nothing else changes:
the same seed with a different config is a different, equally reproducible world,
and the manifest records which one it was.

Bear in mind that the injected corridor degradations are defined by absolute
timestamps, so if you change `window_start` or `days`, move the degradation
windows to match — otherwise the event lands outside the generated window and the
batch quietly contains nothing to detect. The manifest's
`ground_truth.degradations[].observed_in_window.attempts` is where you would
notice.

## Tests

`apps/api/app/tests/test_data_generators.py` — reproducibility, vocabulary
conformance, distribution assertions, ground-truth separation, manifest
integrity, and the retry model's invariants.

```bash
cd apps/api && pytest app/tests/test_data_generators.py
```
