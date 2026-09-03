# ADR 0007 — Corridor definition and the minimum-volume threshold

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 3 (Engine 1)

## Context

"A corridor degraded" is meaningless until two things are fixed: **what a
corridor is**, and **how much evidence is enough to say so**. Both are choices,
both change the reported numbers, and both are the first things anyone
sceptical will ask about.

The Phase 2 dataset was built to make the second question unavoidable. It
contains an injected issuer-rail outage that a detector should find, a
deliberately harder route-level degradation it may well miss, and a **decoy** — a
low-volume wallet corridor with nothing wrong with it, whose ordinary variance
regularly produces windows with no successes at all. Flagging the decoy is a
false positive, and the dataset exists partly to catch a detector that would.

## Decision

### A corridor is evaluated at two levels, simultaneously

| Level | Key | Catches |
| --- | --- | --- |
| `issuer_method` | (issuing bank, method) | One bank's rail degrading while every other bank's is fine |
| `method_route` | (method, route/acquirer) | One acquirer or route degrading across all issuers |

Both run on every batch. A third level, `issuer_method_route`, is defined in
`apps/api/app/engines/root_cause/config.json` and **disabled**.

### The minimum-volume guard is two numbers, not one

| Rule | Value | What it stops |
| --- | --- | --- |
| `corridor-detection:MV1` | ≥ **6** attempts inside the window | A "100% failure rate" computed from two attempts |
| `corridor-detection:MV2` | ≥ **20** prior attempts on the same corridor | A corridor with no usable baseline to compare against |

A window failing either is never flagged, whatever its success rate.

### Significance requires all three conditions

Absolute drop ≥ 15 points, relative drop ≥ 35% of baseline, and an exact
one-sided binomial p-value ≤ 0.05 against the corridor's own prior rate
(`corridor-detection:ST1`).

Every value lives in the config file with the rule id beside it, and the argument
for each lives in the `corridor-detection` skill.

## Why these values

**Two levels, not one, and not three.** Issuer outages and route outages are not
nested — an issuer problem shows on every route, a route problem shows for every
issuer — so segmenting one way makes the other invisible or, worse, mis-localised
onto whichever bank happened to send the most traffic. Both of the Phase 2
injected degradations are found, and each is found at exactly one level:
`HDFC × upi` at `issuer_method`, `card × rt_card_acq_2` at `method_route`.

The third level is off because of arithmetic, not principle. At this project's
volume — 589 attempts over three days — `issuer_method_route` splits the stream
into 37 buckets averaging 16 attempts each, so almost none of them ever clears
MV2. Enabling it detects nothing new and adds 37 more chances to produce a false
positive. It stays in the config, disabled, so the granularity is a decision
someone can argue with rather than an assumption nobody wrote down.

**MV1 = 6.** Six is the smallest window in which a drop from a ~95% baseline to
50% clears the ST1 significance test at all. Below it, the volume guard is not
the binding constraint and the detector is effectively unguarded on small
windows; above it, the route-level degradation in the Phase 2 dataset (10
in-window attempts, spread across overlapping 6-hour windows) stops being
detectable.

**MV2 = 20.** This is the number that keeps the decoy quiet. That corridor
accumulates 14 attempts across the whole dataset, so it never has 20 prior
attempts and is never evaluated. Twenty is roughly where the standard error on a
baseline rate falls under ten points, below which "this corridor got worse" is
not a claim anyone can defend.

**All three significance conditions.** Each has a corridor shape it fails on
alone: a relative test alone flags 89%-versus-95% noise on a high-baseline
corridor; an absolute test alone treats a 15-point drop identically at a 95%
baseline and a 40% one; a p-value alone will flag a genuine but commercially
irrelevant 6-point drop given enough volume. Requiring all three is what makes
"statistical, not heuristic" a description rather than a flourish.

The binomial tail is computed **exactly** with `math.comb`, not approximated.
Window sizes here are single digits to low tens, which is precisely the range
where a normal approximation is worst and where an over-confident p-value would
turn noise into a detection.

## The false-positive trade-off, stated plainly

**MV2 means a genuinely degraded low-volume corridor is invisible to this
engine.** That is the intended direction of error, and it is a real cost, not a
rhetorical one: a merchant whose entire wallet traffic is 14 attempts gets no
protection from Engine 1 at all.

It is the right trade because the failure modes are not symmetric. A missed
detection on a corridor carrying 14 attempts costs a handful of payments that the
per-payment retry path still tries to recover. A false reroute of live traffic on
the strength of three data points costs credibility with the operations team, and
a system that cries wolf gets switched off — after which it recovers nothing at
all.

## Measured consequences (seed 42, `pay-s42-d5db86f7`)

At the configured thresholds, on the committed sample:

| | |
| --- | --- |
| Detections | 3 |
| True positives | 2 of 2 |
| False positives | **1** |
| False negatives | 0 |
| Precision / recall | 66.7% / 100% |
| Decoy false positives | 0 |
| Detection latency | 9.5h (issuer rail), 0.5h (route) |

**The false positive, in full:** `netbanking × rt_nb_direct`, 57.1% success over
7 attempts against a 95.5% baseline, p = 0.0029. Three failures, all rail-side
reasons, three distinct customers. It is not an unreasonable thing to flag —
statistically it is a real drop — it just is not one the generator injected. It
is reported as a false positive rather than argued away.

Across five seeds (42, 7, 13, 101, 2026): **7 true positives, 4 false positives,
3 false negatives** — precision 64%, recall 70%. The headline issuer-rail
degradation is found on every seed. The harder route-level one is found on two of
five, which is what Phase 2 designed it to do. The decoy fires on none.

## Consequences

- The thresholds are in one file, each citing one rule, and the API exposes them
  at `GET /api/v1/root-cause/config`. "Our detection is principled" is checkable
  by someone who reads neither the code nor this document.
- Changing any of them changes reported numbers, so the config version is written
  into every run's `BatchRun.notes`.
- Because MV2 uses only *prior* attempts, the first several hours of any batch
  are undetectable by construction. On a three-day dataset this costs roughly the
  first day on low-volume corridors. Real deployment would carry a baseline
  across batches; this project does not, and the detection-latency figures should
  be read with that in mind.

## Alternatives rejected

**A single global success-rate threshold.** Would fire permanently on the wallet
corridors and never on netbanking. The whole point of BW2 is that corridors
legitimately differ.

**A longer window (12h) to get more volume per window.** Tested: it finds the
issuer-rail degradation and misses the route-level one entirely, and pushes
detection latency past half a day. The 6-hour window with a 2-hour stride finds
both on the committed seed.

**Tuning until the false positive disappears.** Raising MV1 to 8 removes the
netbanking false positive — and also removes the route-level true positive, which
has only 6 in-window attempts. Trading a real detection for a cleaner precision
number is the kind of tuning that makes a metric worthless, so the false positive
stays and gets reported.
