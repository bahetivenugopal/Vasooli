/**
 * Number fidelity — the displayed figure and the API figure must be the same.
 *
 * §7.1: "a check that headline figures rendered match the batch summary API
 * exactly. Formatting must not alter values."
 *
 * The fixture is a **real captured response** from `GET /api/v1/overview`
 * against the committed seed-42 batches, not a hand-written stub. A stub would
 * pass whatever the formatter happened to do; this fails if the API's shape
 * changes or if a formatter starts rounding a value it should only be
 * presenting. Regenerate it whenever the response shape changes — the command is
 * in `docs/smoke-checklist.md`.
 *
 * There is deliberately no DOM here. The dashboard's own components are thin
 * wrappers over these two functions, so testing the functions against the real
 * payload catches the class of bug that matters (a value altered in transit)
 * without a browser test stack that would cost more than it protects.
 */

import { describe, expect, it } from "vitest";

import overview from "./__fixtures__/overview.json";
import { formatPaise, formatRate } from "./money";

/** `₹2,28,27,113` -> `2282711300` paise. The formatter's inverse. */
function parseBackToPaise(formatted: string): number {
  const digits = formatted.replace(/[₹,\s]/g, "");
  return Math.round(Number(digits) * 100);
}

describe("the overview headline", () => {
  it("renders the API's own figures without altering them", () => {
    expect(parseBackToPaise(formatPaise(overview.amount_recovered_paise))).toBe(
      overview.amount_recovered_paise,
    );
    expect(parseBackToPaise(formatPaise(overview.amount_at_risk_paise))).toBe(
      overview.amount_at_risk_paise,
    );
  });

  it("shows the API's recovery rate rather than deriving one", () => {
    // The dashboard must never divide recovered by at-risk itself. This asserts
    // the two would agree anyway — so if they ever stop agreeing, the bug is on
    // the API side where it can be fixed once, not in three page components.
    const derived = overview.amount_recovered_paise / overview.amount_at_risk_paise;
    expect(formatRate(overview.recovery_rate)).toBe(formatRate(Number(derived.toFixed(4))));
  });

  it("is the sum of the per-engine contributions, computed server-side", () => {
    const atRisk = overview.by_engine.reduce((sum, e) => sum + e.amount_at_risk_paise, 0);
    const recovered = overview.by_engine.reduce((sum, e) => sum + e.amount_recovered_paise, 0);
    expect(overview.amount_at_risk_paise).toBe(atRisk);
    expect(overview.amount_recovered_paise).toBe(recovered);
  });
});

describe("every engine contribution", () => {
  it("round-trips through the formatter unchanged", () => {
    for (const contribution of overview.by_engine) {
      expect(parseBackToPaise(formatPaise(contribution.amount_recovered_paise))).toBe(
        contribution.amount_recovered_paise,
      );
      expect(parseBackToPaise(formatPaise(contribution.amount_at_risk_paise))).toBe(
        contribution.amount_at_risk_paise,
      );
    }
  });

  it("carries the seed, the dataset and its own recovery definition", () => {
    // A reported number that cannot be traced back to a reproducible run is not
    // evidence, and a recovery figure quoted without its definition claims more
    // than the run demonstrates.
    for (const contribution of overview.by_engine) {
      expect(contribution.batch_id).toBeTruthy();
      expect(typeof contribution.seed).toBe("number");
      expect(contribution.dataset_batch_id).toBeTruthy();
      expect(contribution.recovery_definition.length).toBeGreaterThan(40);
    }
  });
});

describe("the trust strip", () => {
  it("carries all six kinds of refusal the overview page renders", () => {
    const keys = overview.trust.map((metric) => metric.key);
    expect(keys).toEqual([
      "policy_denials",
      "compliance_blocked",
      "retries_suppressed",
      "messages_suppressed",
      "human_escalations",
      "deterministic_fallbacks",
      "abstentions",
    ]);
  });

  it("fired at all — a run with no refusals means the gate never ran", () => {
    const denials = overview.trust.find((metric) => metric.key === "policy_denials");
    expect(denials?.value).toBeGreaterThan(0);
    expect(Object.keys(denials?.by_rule ?? {}).length).toBeGreaterThan(0);
  });

  it("explains why non-zero is good, in the payload rather than in page copy", () => {
    for (const metric of overview.trust) {
      expect(metric.label).toBeTruthy();
      expect(metric.meaning.length).toBeGreaterThan(40);
    }
  });
});

describe("the audit trail behind the headline", () => {
  it("reports zero schema violations", () => {
    // Non-zero means a row reached `audit_entries` without going through the
    // writer. It is a bug hunt, not a metric — and the overview shows it.
    expect(overview.policy_violations).toBe(0);
  });

  it("keeps reasoned and ruled recovery apart", () => {
    expect(Object.keys(overview.by_source).sort()).toEqual(["deterministic", "model"]);
    const total =
      overview.by_source.model.amount_recovered_paise +
      overview.by_source.deterministic.amount_recovered_paise;
    expect(total).toBe(overview.amount_recovered_paise);
  });

  it("gives every recent entry a citation and a provenance", () => {
    for (const entry of overview.recent_activity) {
      expect(entry.authorising_rule).toContain(":");
      expect(entry.reason_code).toBeTruthy();
      expect(entry.rationale.trim().length).toBeGreaterThan(0);
      expect(["model", "deterministic"]).toContain(entry.provenance.source);
    }
  });
});
