---
name: decline-taxonomy
description: Soft-vs-hard payment decline classification as a lookup table (normalized reason code -> class -> default action -> retry budget). Load BEFORE writing or changing any decline-handling, retry-eligibility, corridor-diagnosis, or dunning-routing code. Cited by policy_engine.py, Engine 1 (root cause) and Engine 2 (mandate recovery).
---

# Decline Taxonomy

## Why this file exists

The single most common and most expensive mistake real payment systems make is
**treating every decline identically**. A decline is not one thing. It is at
minimum two things that look nearly identical in the response payload but demand
opposite responses.

Roughly **80%+ of all declines are soft**. A system that dunns them all is
burning recoverable revenue. A system that retries them all is burning retry
attempts against dead instruments and tripping issuer-side fraud filters.

Every rule in `policy_engine.py` that branches on a decline **must** cite a row
in this table. No invented reason codes, no invented thresholds.

## The one rule

|                          | Soft decline                                   | Hard decline                                              |
| ------------------------ | ---------------------------------------------- | --------------------------------------------------------- |
| Nature                   | **Temporary**                                  | **Permanent**                                             |
| Instrument               | Still valid                                    | Dead / revoked / invalid                                  |
| Examples                 | Insufficient funds, timeout, issuer down       | Expired card, closed account, revoked mandate             |
| Correct action           | **Retry**, inside a bounded schedule           | **Stop retrying**, route to dunning / re-collection       |
| Cost of getting it wrong | Revenue silently lost; involuntary churn       | Wasted attempts plus issuer fraud-filter risk             |

## Normalized reason codes (canonical, internal)

Engines branch on **these** codes only. Never on raw upstream strings, which get
normalized at the boundary (see "Mapping from upstream" below).

### Class: SOFT — retry is appropriate

| Normalized code             | Meaning                                            | Retryable     | Default action                                                                                                              |
| --------------------------- | -------------------------------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `INSUFFICIENT_FUNDS`        | Payer balance below the debit amount               | Yes           | Schedule a retry in a later window. Payday-aware spacing beats immediate re-hammering                                       |
| `ISSUER_UNAVAILABLE`        | Issuing bank not responding / rail degraded        | Yes           | Retry, **and** emit a corridor-health signal to Engine 1. A spike here is the classic corridor-wide failure, not a customer problem |
| `GATEWAY_TIMEOUT`           | No response inside the timeout window              | Yes           | **Reconcile first.** The original attempt may have succeeded late. Retry only after confirming no capture                    |
| `NETWORK_ERROR`             | Transport failure between us and the gateway       | Yes           | Short backoff, then retry. Same reconcile-first rule as above                                                               |
| `TRANSACTION_LIMIT_EXCEEDED`| Per-transaction or daily cap hit on the payer side | Yes           | Retry in the **next** window, never same-day. A same-day retry hits the same cap                                            |
| `AUTH_TIMEOUT`              | Customer did not complete authentication in time   | Conditionally | Requires a **customer-present** attempt (fresh link/notification), never a silent server-side retry                          |
| `CUSTOMER_CANCELLED`        | Customer abandoned or cancelled the payment        | Conditionally | Customer-present retry only. Intent, not capability, is the blocker — a fresh payment link beats a silent re-charge          |

### Class: HARD — retrying is wrong

| Normalized code            | Meaning                                       | Retryable | Default action                                                          |
| -------------------------- | --------------------------------------------- | --------- | ------------------------------------------------------------------------ |
| `CARD_EXPIRED`             | Expiry date passed                            | **No**    | Dunning: request an updated instrument                                  |
| `CARD_LOST_OR_STOLEN`      | Reported lost or stolen                       | **No**    | Stop. Do **not** dun for the same instrument. Flag for review           |
| `ACCOUNT_CLOSED`           | Underlying account no longer exists           | **No**    | Dunning: request a new instrument                                       |
| `INVALID_ACCOUNT`          | Account/card number not valid at the issuer   | **No**    | Dunning: request corrected details                                      |
| `INTERNATIONAL_NOT_ALLOWED`| Instrument not permitted for this corridor    | **No**    | Do not retry the same corridor. Reroute, or request another instrument  |
| `CARD_DISABLED_ONLINE`     | Card blocked for online/e-commerce use        | **No**    | Retrying cannot help — only the cardholder's bank can lift it. Dunning: request another instrument |
| `FRAUD_SUSPECTED`          | Issuer or our own risk layer blocked it       | **No**    | **Terminal.** Never auto-retry, never auto-dun. Human review only       |

### Class: TERMINAL_MANDATE — hard stop, mandate lifecycle

Hard declines with an extra property: they invalidate the **schedule**, not just
the attempt. See the `rbi-mandate-rules` skill.

| Normalized code    | Meaning                            | Retryable | Default action                                                          |
| ------------------ | ---------------------------------- | --------- | ------------------------------------------------------------------------ |
| `MANDATE_REVOKED`  | Customer cancelled the mandate     | **Never** | Immediate hard stop. Cancel the entire retry schedule. This is absolute  |
| `MANDATE_EXPIRED`  | Mandate validity window ended      | **Never** | Hard stop. Re-registration with fresh AFA required                       |

### Class: AMBIGUOUS — deliberately its own class

| Normalized code     | Meaning                                                                                                                                              | Retryable | Default action                                                                            |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- | ------------------------------------------------------------------------------------------ |
| `DO_NOT_HONOUR`     | Issuer declined **without stating why**. The classic trap: it can mean "no funds" (soft) or "this card is dead" (hard), and the payload does not say which | Limited   | Treat as soft but on a **reduced** retry budget. If it repeats across attempts, reclassify as hard |
| `TECHNICAL_DECLINE` | Generic issuer-side technical refusal                                                                                                                | Limited   | Same reduced budget as above                                                              |

### Class: POLICY_BLOCK — not an issuer decline at all

| Normalized code            | Meaning                                                                                                                          | Action                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `AFA_REQUIRED`             | The debit needed fresh Additional Factor Authentication and was declined for lacking it. **Not** a plain soft decline: a silent retry fails identically, forever | Trigger a customer-present authentication flow. Never silent-retry. See `rbi-mandate-rules`          |
| `PRE_DEBIT_NOTICE_MISSING` | **Our own** precondition failed. We never sent the required pre-debit notification                                               | Never attempt the debit. Send the notice, wait out the window, then attempt. See `rbi-mandate-rules` |

## Retry budgets by class

These are **Vasooli policy choices**, not regulatory numbers. They are bounded
deliberately so that "stopping rules" is a provable claim rather than a slogan.

| Class             | Max attempts (incl. original) | Notes                                                                    |
| ----------------- | ----------------------------- | ------------------------------------------------------------------------ |
| SOFT              | 4                             | Spaced, never bunched. Mandate flows are additionally bounded by `rbi-mandate-rules` |
| AMBIGUOUS         | 2                             | Reduced on purpose: we do not know whether the instrument is alive       |
| HARD              | 1                             | The original attempt only. Zero retries                                  |
| TERMINAL_MANDATE  | 1                             | Original only, and the whole schedule is cancelled                       |
| POLICY_BLOCK      | 0                             | We should not have attempted at all                                      |

An attempt consumes budget **whether or not it reaches the issuer**. Budgets are
enforced in `policy_engine.py`, and every decrement is written to the audit trail
(see `audit-schema`).

## Handling unknown reasons — fail safe, not fail open

An unrecognized reason string **must never default to SOFT**. Defaulting to soft
means defaulting to "keep charging this customer", which is exactly the failure
mode this taxonomy exists to prevent.

Procedure:

1. Normalize to `UNKNOWN`.
2. Route to `claude_agent.py` for classification **with this table in context**.
   This is a genuine judgment call, and one of the places the LLM earns its place
   in the system.
3. Write to the audit trail: the assigned class, the model's stated confidence,
   and an explicit flag that the classification was model-derived rather than
   table-derived.
4. **If confidence is low, treat as HARD.** The safe direction of error is
   "stop", never "keep retrying".

## Mapping from upstream

Razorpay returns a structured error object with `code`, `description`, `field`,
`source`, `step`, `reason`, and `metadata` (exact shape in the `razorpay-api`
skill). **Branch on `reason`** — it is the machine-readable one. `source` and
`step` vary by payment method and Razorpay does not publish an exhaustive list,
so never build exhaustive branching on them.

Normalization happens **once**, at the boundary, in
`services/razorpay_client.py`. Engines never see raw upstream strings.

### Verified mapping (doc-checked 2026-09-03)

Every `reason` below is confirmed against Razorpay's error-scenario test cards,
so each row is reproducible with a specific test card listed in `razorpay-api`.

| Razorpay `error.reason` | Normalized code | Class | Note |
| --- | --- | --- | --- |
| `insufficient_fund` | `INSUFFICIENT_FUNDS` | SOFT | Spacing matters more than speed |
| `payment_timed_out` | `GATEWAY_TIMEOUT` | SOFT | Reconcile before retrying |
| `gateway_technical_error` | `NETWORK_ERROR` | SOFT | Razorpay's own copy says any debited amount is refunded — so the debit **may have landed**. Reconcile first. Also a corridor-health signal for Engine 1 |
| `payment_cancelled` | `CUSTOMER_CANCELLED` | SOFT | Customer-present retry only |
| `card_declined` | `DO_NOT_HONOUR` | **AMBIGUOUS** | Bank declined with **no** stated reason. Three separate test-card pairs return this. Reduced budget — do not treat as plain SOFT |
| `card_disabled_for_online_payments` | `CARD_DISABLED_ONLINE` | HARD | Only the cardholder's bank can lift it |
| `card_number_invalid` | `INVALID_ACCOUNT` | HARD | |
| `authentication_failed` | `AFA_REQUIRED` | POLICY_BLOCK | Incorrect OTP / verification. A silent retry reproduces it forever |

Anything not in this table hits the `UNKNOWN` path above **by design**.

> **Verify, do not remember.** This table was checked against Razorpay's
> documentation on the date above. Any *additional* `reason` string must be
> confirmed against live test-mode responses before being added — a mapping built
> from recollection is a silent-wrong-answer generator.

## Where the numbers come from

- Soft/hard split, the ~80%-soft figure, and the "treating declines identically"
  failure mode: project brief section 4 (research-sourced).
- Retry budgets in this file: Vasooli policy choices, documented here so they are
  auditable and can be challenged.
- Mandate-specific bounds: `rbi-mandate-rules`, which overrides anything here for
  recurring flows.
