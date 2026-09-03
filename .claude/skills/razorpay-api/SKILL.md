---
name: razorpay-api
description: Razorpay API reference for this build — auth, base URL, the endpoints Vasooli uses (Orders, Payments, Payment Links, Plans/Subscriptions, Invoices, Customers), the exact error-object shape, and the verified test-mode card/UPI credentials including the error-scenario cards that trigger specific decline reasons. Load BEFORE writing or changing any Razorpay call.
---

# Razorpay API

**Test mode only, always.** Every key used in this repo is a `rzp_test_` key. No
live key is ever placed in `.env`, in a test fixture, in a commit, or in a demo
recording. If a live key appears anywhere, stop and rotate it.

## Verification status

Doc-verified against Razorpay's documentation on **2026-09-03**:

- Auth model and base URL — conventional, stable
- Endpoint paths listed below — **verified**
- Error object field names — **verified**
- Test cards, error-scenario cards, test UPI IDs — **verified verbatim**

**Not** verified at field level: the request/response body fields in the
"Entity shapes" section. Those are written from working knowledge and are marked
individually. **Confirm each against the API reference or an actual test-mode
response before depending on it.** A remembered field name that is subtly wrong
produces a silent bug, so treat that section as a starting point, not truth.

## Auth and base URL

```
Base URL:  https://api.razorpay.com/v1
Auth:      HTTP Basic — username = key_id, password = key_secret
Header:    Authorization: Basic base64(key_id + ":" + key_secret)
```

The official `razorpay` Python SDK handles this:

```python
import razorpay
client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
```

Use the SDK first. Drop to direct `httpx` calls **only** for endpoints the SDK
does not cover, and keep those in `services/razorpay_client.py` alongside
everything else — engines never call Razorpay directly.

**Money is integer paise.** `amount: 50000` means ₹500.00. Never send a float,
never send rupees. This matches the integer-paise rule in `audit-schema`.

## Endpoints (verified)

### Orders — Engine 1 (payment attempts)

```
POST   /v1/orders
GET    /v1/orders
GET    /v1/orders/:id
PATCH  /v1/orders/:id
GET    /v1/orders/:id/payments
GET    /v1/orders?expand[]=payments
GET    /v1/orders?expand[]=payments.card
```

The `expand[]=payments` form matters for Engine 1: it pulls attempts and their
failure detail in one call instead of N+1 fetches across a batch.

### Payments

```
GET    /v1/payments/:id
POST   /v1/payments/:id/capture
```

### Payment Links — Engines 2 and 3 (recovery + receivables)

```
POST   /v1/payment_links
GET    /v1/payment_links
GET    /v1/payment_links/:id
POST   /v1/payment_links/:id/notify_by/:medium     # medium: sms | email
POST   /v1/payment_links/:id/cancel
```

The primary recovery instrument: when a mandate charge halts or an invoice needs
chasing, a payment link is the bounded, customer-present way to collect.

### Plans and Subscriptions — Engine 2

```
POST   /v1/plans
GET    /v1/plans
GET    /v1/plans/:id

POST   /v1/subscriptions
GET    /v1/subscriptions
GET    /v1/subscriptions/:id
PATCH  /v1/subscriptions/:id
POST   /v1/subscriptions/:id/cancel
POST   /v1/subscriptions/:id/pause
POST   /v1/subscriptions/:id/resume
GET    /v1/subscriptions/:id/retrieve_scheduled_changes
POST   /v1/subscriptions/:id/cancel_scheduled_changes
```

`pause` / `resume` are the compliant way to stop a schedule without destroying
the mandate. `cancel` is terminal. Engine 2 must choose deliberately between them
and record which, per `audit-schema`.

### Invoices — Engine 3

```
POST   /v1/invoices
GET    /v1/invoices
GET    /v1/invoices/:id
POST   /v1/invoices/:id/issue
POST   /v1/invoices/:id/notify_by/:medium
POST   /v1/invoices/:id/cancel
```

### Customers

```
POST   /v1/customers
GET    /v1/customers/:id
```

## Error object shape (verified)

A failed call returns an `error` object with these exact fields:

```json
{
  "error": {
    "code": "BAD_REQUEST_ERROR",
    "description": "Your payment could not be completed due to insufficient account balance. Try another card or payment method.",
    "field": null,
    "source": "customer",
    "step": "payment_authentication",
    "reason": "insufficient_fund",
    "metadata": {}
  }
}
```

| Field | Notes |
| --- | --- |
| `code` | Broad category, e.g. `BAD_REQUEST_ERROR` |
| `description` | Customer-facing message. Useful for dunning copy, useless for branching |
| `field` | The offending request field, when applicable |
| `source` | Where it originated — values vary by payment method (`customer`, and others) |
| `step` | Which stage failed, e.g. `payment_authentication`. Varies by method |
| `reason` | **The one to branch on.** Machine-readable, e.g. `insufficient_fund` |
| `metadata` | Extra context such as related ids |

> Razorpay's docs state that `source` and `step` values vary by payment method
> and do not enumerate a complete list. **Do not build exhaustive branching on
> `source` or `step`.** Branch on `reason`, normalized through
> `decline-taxonomy`, and treat unrecognised values via its `UNKNOWN` path.

## Verified test-mode credentials

Any random CVV. Any future expiry date. On the mock bank page, an OTP of **4–10
digits succeeds**; an OTP of **fewer than 4 digits fails**.

### Success cards — domestic

| Network | Number |
| --- | --- |
| Visa (debit) | `4100 2800 0000 1007` |
| Mastercard (credit) | `5555 5100 0008 1006` |
| Mastercard (prepaid) | `5180 2872 0009 1001` |
| RuPay (credit) | `6527 6589 0000 1005` |
| Diners | `3608 280009 1007` |
| Amex | `3402 560004 01007` |

### Success cards — subscriptions (Engine 2)

| Network | Number |
| --- | --- |
| Visa domestic | `4718 6091 0820 4366` |
| Mastercard international (credit) | `5104 0155 5555 5558` |
| Mastercard international (debit) | `5104 0600 0000 0008` |

### Error-scenario cards — the important ones

These trigger **specific** `error.reason` values, which makes them the backbone
of realistic decline testing. `data-synthesizer` should mirror this distribution
and `test-engineer` should assert against these exact reasons.

| `error.reason` | Visa | Mastercard | Maps to (`decline-taxonomy`) |
| --- | --- | --- | --- |
| `payment_timed_out` | `4100 2800 0009 0000` | `5305 6200 0006 0000` | `GATEWAY_TIMEOUT` — SOFT |
| `insufficient_fund` | `4100 2800 0008 0001` | `5305 6200 0005 0001` | `INSUFFICIENT_FUNDS` — SOFT |
| `payment_cancelled` | `4100 2800 0007 0002` | `5305 6200 0004 0002` | `CUSTOMER_CANCELLED` — SOFT, customer-present |
| `card_declined` | `4100 2800 0006 0003` | `5305 6200 0003 0003` | `DO_NOT_HONOUR` — **AMBIGUOUS** |
| `card_declined` | `4100 2800 0005 0004` | `5305 6200 0002 0004` | `DO_NOT_HONOUR` — **AMBIGUOUS** |
| `card_declined` | `4100 2800 0004 0005` | `5305 6200 0001 0005` | `DO_NOT_HONOUR` — **AMBIGUOUS** |
| `card_disabled_for_online_payments` | `4100 2800 0003 0006` | `5305 6200 0000 0006` | `CARD_DISABLED_ONLINE` — HARD |
| `card_number_invalid` | `4100 2800 0001 0008` | `5305 6200 0008 0008` | `INVALID_ACCOUNT` — HARD |
| `gateway_technical_error` | `4100 2800 0002 0007` | `5305 6200 0009 0007` | `NETWORK_ERROR` — SOFT, reconcile first |
| `authentication_failed` | `4100 2800 0000 0009` | `5305 6200 0007 0009` | `AFA_REQUIRED` — POLICY_BLOCK |

Note that `card_declined` has **three** card pairs and carries no issuer reason —
"declined by the bank" and nothing more. That is precisely why it normalizes to
`DO_NOT_HONOUR` on the reduced AMBIGUOUS retry budget rather than to SOFT.

`gateway_technical_error` says any debited amount is refunded in 4–5 business
days, which means the debit may have **landed**. Reconcile before retrying.

### Test UPI

| VPA | Outcome |
| --- | --- |
| `success@razorpay` | Payment succeeds |
| `failure@razorpay` | Payment fails |

> **Test-mode quirk worth knowing:** in test mode, UPI payment *cancellation*
> results in a **successful** payment. Cancellation can only be exercised in live
> mode. Do not build a demo assertion that depends on cancelling a test UPI
> payment — it will pass for the wrong reason.

## Entity shapes — UNVERIFIED, confirm before use

Working-knowledge starting points. **Verify each field against the API reference
or a real test-mode response before relying on it.**

Create an order:

```python
client.order.create({
    "amount": 50000,          # integer paise
    "currency": "INR",
    "receipt": "rcpt_batch_001",
    "notes": {"batch_id": batch_id, "engine": "root_cause"},
})
```

Expected back: an `id` prefixed `order_`, plus `amount`, `amount_paid`,
`amount_due`, `currency`, `receipt`, `status`, `attempts`, `created_at`.

Entity id prefixes are a reliable convention: `order_`, `pay_`, `plink_`,
`plan_`, `sub_`, `inv_`, `cust_`.

**Always set `notes`** with at least `batch_id`. It is the only way to tie a
Razorpay-side object back to a reproducible synthetic batch, which is what makes
the recovery numbers defensible.

## Non-negotiables for every call

1. **Never let a Razorpay exception escape uncaught.** Catch, normalize the
   error, write an audit entry, return a typed result. A crashed batch run proves
   nothing.
2. **Normalize at the boundary.** `reason` becomes a `decline-taxonomy` code
   inside `services/razorpay_client.py`. Engines never see raw upstream strings.
3. **Every call that changes money state writes an audit entry** before the call,
   per `audit-schema`.
4. **Idempotency.** Never retry a charge without first reconciling whether the
   original succeeded — see `GATEWAY_TIMEOUT` and `gateway_technical_error`.
5. **Timeouts and bounded retries on transport errors.** A hung HTTP call must
   not stall a batch run.
6. **Never log key material**, and never log full card numbers.
