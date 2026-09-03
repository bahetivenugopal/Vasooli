---
name: corridor-detection
description: Engine 1's corridor definition, the statistical thresholds that decide when a payment corridor counts as degraded, and the bounds on the recovery actions a corridor-level diagnosis may trigger. These are Vasooli product decisions, each named so `detection.py` and `policy_engine.py` can cite it. Load BEFORE writing or changing any corridor segmentation, degradation-detection threshold, reroute bound, or root-cause diagnosis code.
---

# Corridor Detection (Engine 1 bounds)

## Why this file exists

`CLAUDE.md` non-negotiable #1: **no invented thresholds.** `decline-taxonomy`
owns retry budgets, `rbi-mandate-rules` owns the regulatory constraints, and
`policy-bounds` owns the engine-agnostic product bounds. None of them says how
wide a payment corridor is, how many attempts must be inside a window before a
success-rate drop means anything, or how long a reroute may last.

Those numbers are Engine 1's, and they are the ones a judge will push hardest
on, because a detector that fires on noise is worse than no detector at all.
This file names them, values them, and argues them.

> **These are ours, not regulation.** Nothing on this page claims outside
> authority. Read it as the Engine 1 counterpart to `policy-bounds`.

## Rule ids

Cited as `corridor-detection:<id>`, e.g. `corridor-detection:MV1`.

The detection rules (CS/BW/MV/ST/DX) are cited by `app/engines/root_cause/`; the
recovery rules (RR/ES) are registered in `policy_engine.py` and appear as
`authorising_rule` on audit entries.

---

## CS — Corridor segmentation

### CS1 — A corridor is a *level*, not a single tuple

A payment corridor is evaluated at two configured levels, both at once:

| Level | Key | Catches |
| --- | --- | --- |
| `issuer_method` | (issuing bank, method) | An issuing bank's rail degrading — HDFC's UPI going bad while every other bank's UPI is fine |
| `method_route` | (method, route/acquirer) | An acquirer or route degrading across all issuers — card acquirer B timing out |

**Rationale.** These are the two shapes a real corridor failure takes, and they
are not nested: an issuer outage shows up on every route, a route outage shows
up for every issuer. Segmenting only one way makes the other invisible or
mis-localised. A third level, `issuer_method_route`, is deliberately **off** by
default: at this project's data volume it splits the stream into ~37 buckets
that individually never clear MV2, so it detects nothing and only inflates the
false-positive surface. It stays in the config so the granularity is a decision
someone can argue with, not an assumption.

**Detection never reads the ground-truth manifest.** The corridor key is built
from record fields only (`issuer`, `method`, `route_id`). A test asserts this.

---

## BW — Baseline and window

### BW1 — Rolling window **6 hours**, stride **2 hours**

Success rates are computed over a 6-hour window, advanced in 2-hour steps.
Overlapping windows on the same corridor are merged into one detection episode.

**Rationale.** Six hours is short enough to sit inside a half-day rail outage
and still contain enough attempts to say something, and long enough that Indian
payment traffic's diurnal swing (a measured 48× peak-to-trough in this
project's own data) does not turn a quiet night into a "detection". The 2-hour
stride bounds detection latency to at most one stride past the window that first
contains enough evidence.

### BW2 — Baseline is the corridor's **own prior history**

A window is compared against the same corridor's success rate over every attempt
that happened **before the window opened** — never against a global constant and
never against data from the future.

**Rationale.** Corridors legitimately differ: a wallet at 74% and a netbanking
rail at 95% are both healthy. A global threshold would fire constantly on the
first and never on the second. Using only prior attempts keeps the detector
honest about what it could actually have known at the time, which is what makes
the reported detection latency mean anything.

---

## MV — Minimum volume (the false-positive guard)

> This is the single most important guard in the engine. Everything else tunes
> sensitivity; these two decide whether the detector is defensible.

### MV1 — Minimum in-window attempts: **6**

A window with fewer than 6 attempts is never flagged, whatever its success rate.

**Rationale.** At 5 attempts, a corridor that is genuinely running at 90% shows
zero successes roughly once in a hundred thousand windows — but with ~30
corridors × ~30 windows each, "once in a hundred thousand" starts happening. Six
is the smallest number at which a drop from a 95% baseline to 50% survives the
ST1 significance test at all, so it is the smallest guard that does not simply
disable detection on this project's data volume.

### MV2 — Minimum baseline attempts: **20**

A corridor with fewer than 20 prior attempts has no usable baseline and is never
flagged.

**Rationale.** This is what makes the Phase 2 decoy corridor not fire. That
corridor (a low-volume wallet) accumulates 14 attempts across the whole dataset,
so ordinary variance regularly produces windows with no successes at all —
exactly the shape of a real outage, with none of the evidence. Twenty prior
attempts is the point at which a baseline rate has a standard error under ~10
points, below which "the corridor got worse" is not a statement anyone can
defend.

**The trade-off, stated plainly:** MV2 means a genuinely degraded low-volume
corridor is invisible to this engine. That is the intended direction of error. A
missed detection on a corridor carrying 14 attempts costs a handful of payments;
a false reroute of live traffic on the strength of three data points costs
trust, and is the failure mode that makes an operations team switch the system
off.

---

## ST — Significance

### ST1 — Three conditions, all required

A window is flagged only when **all three** hold:

| Condition | Value | Why it is there |
| --- | --- | --- |
| Absolute drop | ≥ **15** points below baseline | Kills the "89% vs 95%" noise that a relative test alone would flag on a high-baseline corridor |
| Relative drop | ≥ **35%** of baseline | Kills the mirror problem: a 15-point drop means something very different at a 95% baseline than at a 40% one |
| One-sided binomial p-value | ≤ **0.05** | The actual statistical claim: *given this corridor's own baseline rate, how surprising is this many successes in this many attempts?* Computed exactly, not approximated — window sizes here are small enough that a normal approximation would be the wrong tool |

**Rationale for requiring all three:** each one alone has a corridor shape it
fails on. Together they are what lets the detector be described to a judge as
statistical rather than heuristic.

### ST2 — Confidence is `1 − p`

The detection's confidence signal is one minus the binomial p-value. It is a
*statistical* confidence, entirely separate from the model confidence the
diagnosis layer reports, and the two are never averaged or combined.

---

## DX — Diagnosis

### DX1 — Diagnosis confidence floor: **0.6**

A root-cause diagnosis returned with confidence below 0.6 is discarded and the
registered deterministic classifier is used instead, audited as
`source: deterministic`.

**Rationale.** Same value as the unknown-decline floor in `decline-taxonomy`,
for the same reason and deliberately not a second number: "how sure is sure
enough" should not silently differ between two classification tasks in the same
system. A model that is unsure about a corridor should not be the thing that
reroutes it.

### DX2 — Abstention is a first-class answer

`insufficient_evidence` is one of the three determinations the diagnosis layer
may return, not an error path. It routes the corridor to human review under
`policy-bounds:HE1`.

**Rationale.** The alternative is a layer that always produces a confident
answer, which is a layer that will confidently be wrong. Making abstention
available and showing it firing is a trust feature.

---

## RR — Reroute bounds

*Registered in `policy_engine.py`. These appear as `authorising_rule` on audit
entries.*

### RR1 — A reroute expires. Maximum duration **6 hours**

Every reroute carries an explicit `expires_at`, set at most 6 hours after it is
authorised, and lapses automatically. There is no code path that creates an
open-ended one, and a configured duration above the maximum is clamped down,
never honoured.

**Rationale.** A reroute that never expires is a permanent configuration change
wearing a recovery action's clothes. Expiry is the entire difference between
"the agent responded to an outage" and "the agent silently rewrote our routing".
Six hours matches the detection window (BW1): the reroute lasts about as long as
the evidence that justified it, and if the corridor is still bad, the next
window re-detects it and authorises a fresh, separately audited reroute.

### RR2 — A reroute requires a **systemic** determination

`reroute_traffic` is permitted only when the diagnosis layer determined the
degradation is systemic. An `individual` or `insufficient_evidence`
determination can never authorise one, whatever action the model recommended.

**Rationale.** This is the whole thesis of the engine in one rule. A
concentration of insufficient-funds declines across many distinct customers is
not a corridor outage, and rerouting live traffic because of it is the expensive
mistake. Enforcing it in the policy engine rather than in the engine's own code
is what makes it a gate rather than an intention.

### RR3 — A reroute needs somewhere to go

A reroute may only be authorised when an alternate route is actually available
for the method. Where none exists, the precondition is unsatisfiable and the
action fails closed to human escalation.

**Rationale.** `CLAUDE.md` non-negotiable #3. "Reroute to nowhere" would be an
audit entry claiming an action that cannot have happened.

---

## ES — Escalation

### ES1 — Corridor autonomous-action ceiling: **₹2,00,000** (20,000,000 paise)

A corridor-level action whose value at risk exceeds ₹2,00,000 is escalated for
human confirmation instead of being executed autonomously.

**Rationale.** `policy-bounds:HE3` puts a single high-value *account* in front of
a human at ₹50,000. A corridor action is a bulk action affecting many payers at
once, so the ceiling is set an order of magnitude higher — otherwise every
corridor detection would escalate and the engine would never act on its own.
₹2,00,000 is roughly the point at which a wrong reroute stops being a recoverable
operational annoyance. Deliberately arguable, and registered as a named threshold
(`corridor_autonomous_action`) under `policy-bounds:AT1`.

---

## Changing anything in this file

Same rule as `policy-bounds`: these are tunable, but only here, and a change to
any of them is recorded in an ADR under `docs/adr/`. A threshold adjustable at a
call site is not a bound.

Detection values live in `apps/api/app/engines/root_cause/config.json`, which
cites the rule id beside each one. The config exists so granularity and
sensitivity can be discussed rather than assumed — not so they can be changed
without an argument.
