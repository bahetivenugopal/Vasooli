# ADR 0004 — Razorpay simulated mode alongside test mode

**Status:** Accepted
**Date:** 2026-09-03

## Context

Every recovery number Vasooli reports is supposed to be reproducible: a
`batch_id`, a seed, and an audit trail anyone can recompute the headline figure
from. A batch that hits the live Razorpay test API is not reproducible in that
sense, for three reasons:

1. **Test mode is still a network.** Latency, transient 5xx and rate limits vary
   run to run. A batch that fails halfway through proves nothing, and a demo
   recorded over a flaky connection proves less.
2. **Test-mode responses are not deterministic across runs.** Ids differ, and
   the failure distribution depends on which test cards were exercised rather
   than on a seed we control.
3. **A judge re-running the batch should get the same numbers we reported.**
   That is only possible if the payment side is a function of the input.

The alternative — mocking Razorpay inside the tests only — is not enough. The
demo batch is the artifact under scrutiny, and if that batch depends on live
network conditions, the reproducibility claim applies to the tests rather than
to the thing anyone will actually look at.

## Decision

`services/razorpay_client.py` supports two explicit modes, selected by
`RAZORPAY_MODE`:

| Mode | Behaviour |
| --- | --- |
| `simulated` (**default**) | Calls are fulfilled from local fixtures. Ids are derived by hashing the canonicalised request, so the same request produces the same id every run. Failures occur **only** when the request explicitly asks for one via `notes.simulate_failure`. |
| `test` | Real HTTP calls against Razorpay **test mode**, through the official SDK. |

There is no third mode, and there is deliberately no configuration that reaches
a live key: a key not starting with `rzp_test_` fails at client construction,
and a `rzp_live_` key fails earlier still, in settings validation.

Three properties make simulated mode a real mode rather than a stub:

- **It is never silent.** `razorpay_mode` is written into the metadata of every
  audit entry the client produces. A reader can always tell whether a figure
  came from fixtures or from a test-mode call. A simulation that hides that it
  is a simulation is a fabricated record.
- **Its fixtures are the verified error objects**, one per error-scenario test
  card in the `razorpay-api` skill, keyed by the same `error.reason` values a
  real test-mode response carries. Both modes therefore normalize through the
  identical `decline-taxonomy` mapping, so a bug in normalization cannot hide in
  one mode and not the other.
- **It never invents failures.** The synthetic data generator (Phase 2) chooses
  the failure distribution from its seed and states it in the request. A
  simulator that decided failures on its own would produce a recovery rate
  measured against nothing in particular.

## Consequences

- The demo runs offline, deterministically, and identically on any machine. The
  reproducibility claim covers the artifact judges actually see.
- Both modes share one code path up to dispatch, so the error-normalization
  boundary, the audit writing and the result shape are exercised by every test.
- The realism of simulated mode is bounded by the fixtures. It cannot surface an
  upstream behaviour nobody wrote down — which is the honest trade: a real
  test-mode run is the check on the fixtures, and `RAZORPAY_MODE=test` exists so
  that check is one environment variable away.
- Fixtures are a maintenance surface. Adding a scenario means verifying it
  against a live test-mode response first; a fixture written from recollection
  is a silent-wrong-answer generator, and the fixture file says so.
- An unrecognised `simulate_failure` scenario produces a failure through the
  `UNKNOWN` path rather than a silent success. A missing fixture must never make
  a batch look better than it was.
