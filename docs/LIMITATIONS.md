# Honest limitations

The canonical list. Every claim this project makes should be read against it —
stated here rather than left for a reader to find.

Number-specific caveats are in [`RESULTS.md` §8](RESULTS.md#8-honest-limitations-on-these-numbers);
the reviewer-facing short version is in
[`REVIEWER-GUIDE.md` §9](REVIEWER-GUIDE.md#9-what-we-are-not-claiming).

---

## The data

1. **All data is synthetic and seeded.** No real customer, invoice or rupee is
   involved anywhere. Razorpay runs in deterministic simulated mode by default
   ([ADR 0004](adr/0004-razorpay-simulated-mode.md)), and only ever with
   test-mode credentials.

2. **Retry outcomes are simulated from a probability model we wrote.**
   `data/generators/retry_model.py`, documented in
   [ADR 0005](adr/0005-simulated-retry-outcomes.md) and
   [`data/DATA_CARD.md`](../data/DATA_CARD.md). It is the single biggest lever on
   the recovery numbers, and it is an assumption rather than a measurement from
   production traffic.

3. **A reroute recovers nothing.** Corridor rerouting is authorised, bounded,
   expiring and audited — but the retry model has no route dimension, so
   crediting rerouted traffic with an uplift would be inventing the headline.

## The engines

4. **No communication is ever dispatched.** Every dunning message and reminder is
   drafted, tone-validated against named rules, gated, recorded — and then stops.
   Nothing is sent to anyone.
   ([ADR 0009](adr/0009-communications-are-never-dispatched.md))

5. **Engine 3's 100% extraction score is a statement about a 20-template
   corpus**, not a production claim. The adversarial probe (twelve hand-written
   replies designed to bait a false positive) is the evidence that generalises
   further — and it is still only twelve replies.

6. **One reply per invoice.** Promise → renege → promise-again does not exist in
   the corpus, so Engine 3 cannot be scored on it.

7. **Two contact caps are tested but never demonstrated.** The ledger arrives
   with most chase ladders already spent, so `QH2` and `RL3` are covered by unit
   tests against the policy engine rather than by a batch run.

8. **`classify_unknown_decline` fires zero times on this book.** It is wired and
   unit-tested; every failure code in the corpus is already in the taxonomy.

## Known defects and deliberate choices

9. **Engine 1 stamps its audit entries with wall-clock time** while Engines 2 and
   3 use their run clocks. A known defect: entry *timestamps* are therefore not
   reproducible, which is why determinism is asserted over metrics rather than
   over rows.

10. **The three engines do not share a run clock**, deliberately — each derives
    `now` from its own data, and each is right for its own dataset. The
    consequence is that a naive newest-first sort across all three would show one
    engine, and the dashboard works around it explicitly.

## The build

11. **This is a ~30-hour prototype.** No authentication, no multi-tenancy, no
    background workers, SQLite, one process, not deployed.
