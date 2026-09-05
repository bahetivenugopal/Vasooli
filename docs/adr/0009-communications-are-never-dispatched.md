# ADR 0009 — Communications are generated and logged, never dispatched

**Status:** Accepted · **Date:** 2026-09-04 · **Phase:** 4 (Engine 2)

## Context

Engine 2 drafts messages to customers whose recurring payment failed: a pre-debit
notification, a request to update a dead card, a request to authenticate a
high-value debit. Engine 3 will draft invoice reminders on the same machinery.

The tempting demo is one that actually sends. It looks more impressive, and the
plumbing is a few lines — an SMTP call, a WhatsApp Business API call, a
transactional SMS gateway.

The synthetic customers in `data/samples/mandates/mandates.jsonl` have names and
customer ids. They have no contact details, because we never generated any. If
they had, sending would mean transmitting to addresses that either do not exist
or — worse, and only one careless config away — belong to real people who never
consented to a hackathon project contacting them about a debt they do not owe.

## Decision

**Nothing in this repository sends a message to anyone.** Communications are
drafted, tone-validated, policy-gated, persisted and rendered. There is no send
path, no transport dependency, and no recipient column.

The decision is structural rather than a matter of restraint:

- `MandateCommunication` has **no recipient field**. Adding one would be a
  visible, reviewable change rather than a config flip.
- `status` is one of `marked_for_delivery`, `held` or `suppressed`. **`sent` is
  not a value.** The status vocabulary cannot express a claim that is not true,
  which matters because this is one of the tables a judge is most likely to read.
- Every audit entry for a communication carries `simulated_delivery: true` in its
  metadata, so the trail says so on every row rather than only in a document.
- The API route that lists them documents it in its own description, because
  someone reading `/docs` should not have to find this file.

Everything up to the send is real: the drafting is a genuine model call, the tone
constraints are enforced against the produced text, quiet hours and the contact
cap are evaluated at the moment the message would go out, and a held message
carries the time it becomes permissible.

## Why this is stated loudly rather than quietly

A simulated send that is honestly labelled is far better than a real send with no
consent path — but only if it is *labelled*. A demo that shows a message and lets
a viewer assume it went out has made the same claim as one that sent it, without
the accountability.

There is also a narrower reason. `policy-bounds:QH1` and `QH2` are compliance
claims: the outreach window and the contact cap. Those claims are worth
something because the gate is evaluated at the real proposed send time and the
refusal is recorded. They are worth nothing if the system then sends anyway
through some path nobody audited. Having no path at all is the cheapest way to
make the claim structurally true.

## What a real integration would need

Named here so the gap is a known gap rather than an oversight:

1. **Consent and preference storage** per customer, per channel, checked before
   the policy gate — not after.
2. **Recipient resolution** with its own validation, and a suppression list.
3. **Delivery receipts** flowing back so `status` can move to `sent` and then to
   `delivered` or `bounced`, each transition audited.
4. **DLT registration** for Indian SMS, and template approval for WhatsApp
   Business. Neither is optional in this market, and both would change the
   drafting task's output shape — a message must fit an approved template rather
   than being freely composed.

Point 4 is the interesting one: a real deployment could not use free-composed
model output for SMS or WhatsApp at all. The drafting task as built would be
correct for email and for choosing *which approved template* to fire elsewhere.
That is a design consequence worth knowing before anyone quotes this as
production-ready.

## Consequences

**Good.** No possibility of contacting a real person. No transport dependency, no
credentials, no rate limits, nothing that can fail during a demo for reasons
unrelated to the product.

**Good.** The full message text is in the database, so the dashboard can show
exactly what a customer would have received, next to the rule that permitted it
and the failure that prompted it.

**Limitation, stated.** Delivery, deliverability and reply handling are entirely
untested. Any claim about recovery rates from messaging is a claim about drafting
and gating, never about anything a customer actually read.

**Limitation, stated.** The tone constraints are validated against generated
English text. A Hinglish or vernacular channel — the optional bonus in
`CLAUDE.md` — would need the TN patterns extended, and the current
`FORBIDDEN_PATTERNS` list would silently pass a threat written in Hindi.
