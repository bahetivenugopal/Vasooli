/**
 * Money formatting at the boundaries.
 *
 * §7.2: "paise-to-rupee conversion correct at boundaries, including large values
 * with Indian-format separators." This is the cheapest possible insurance
 * against the most embarrassing possible bug — a headline that is off by a
 * factor of a hundred, in front of judges, with nobody in the room able to tell
 * because it is a plausible-looking number.
 */

import { describe, expect, it } from "vitest";

import { formatPaise, formatPaiseCompact, formatRate, paiseToRupees } from "./money";

describe("formatPaise", () => {
  it("converts paise to rupees, not the other way round", () => {
    expect(formatPaise(100)).toBe("₹1");
    expect(formatPaise(1)).toBe("₹0");
    expect(formatPaise(99)).toBe("₹0");
  });

  it("uses Indian grouping, not thousands separators", () => {
    // The whole reason this matters: 1,00,000 and 100,000 are the same number
    // and only one of them is read correctly at a glance in this market.
    expect(formatPaise(1_00_000_00)).toBe("₹1,00,000");
    expect(formatPaise(1_00_00_000_00)).toBe("₹1,00,00,000");
  });

  it("handles the real figures from the committed sample batches", () => {
    // Engine 1's Rs 5,25,206 at risk, Engine 2's Rs 7,92,346, Engine 3's
    // Rs 2,28,27,113. Writing this test caught the author expecting the last one
    // to be a hundred times larger, which is precisely the failure it is for.
    expect(formatPaise(52_520_600)).toBe("₹5,25,206");
    expect(formatPaise(79_234_600)).toBe("₹7,92,346");
    expect(formatPaise(2_282_711_300)).toBe("₹2,28,27,113");
  });

  it("shows paise only when asked", () => {
    expect(formatPaise(123_456)).toBe("₹1,234");
    expect(formatPaise(123_456, { paise: true })).toBe("₹1,234.56");
  });

  it("can drop the sign for a column that carries one in its header", () => {
    expect(formatPaise(250_000, { bare: true })).toBe("2,500");
  });

  it("keeps zero as zero rather than an em dash or a blank", () => {
    expect(formatPaise(0)).toBe("₹0");
  });

  it("survives a negative, a float and a NaN without lying", () => {
    expect(formatPaise(-250_000)).toBe("-₹2,500");
    // A fractional paise has no meaning; the arithmetic that produced it is the
    // bug, so the formatter truncates rather than rounding it into existence.
    expect(formatPaise(150.9)).toBe("₹1");
    expect(formatPaise(Number.NaN)).toBe("₹0");
  });
});

describe("formatPaiseCompact", () => {
  it("speaks in the units Indian finance uses", () => {
    expect(formatPaiseCompact(2_282_711_300)).toBe("₹2.28 Cr");
    expect(formatPaiseCompact(52_520_600)).toBe("₹5.25 L");
    expect(formatPaiseCompact(1_50_000)).toBe("₹1.50 K");
    expect(formatPaiseCompact(45_000)).toBe("₹450");
  });

  it("switches unit exactly at the boundary, not near it", () => {
    expect(formatPaiseCompact(99_000_00)).toBe("₹99 K"); // Rs 99,000
    expect(formatPaiseCompact(1_00_000_00)).toBe("₹1 L"); // Rs 1,00,000
    expect(formatPaiseCompact(99_00_000_00)).toBe("₹99 L"); // Rs 99,00,000
    expect(formatPaiseCompact(1_00_00_000_00)).toBe("₹1 Cr"); // Rs 1,00,00,000
  });

  it("rounds up to the next unit's edge rather than silently crossing it", () => {
    // Rs 99,999 renders as "Rs 100 K" — rounding, not a unit error. The exact
    // figure is always on the element's title, which is why the compact form is
    // allowed to round at all.
    expect(formatPaiseCompact(99_99_900)).toBe("₹100 K"); // Rs 99,999
  });
});

describe("formatRate", () => {
  it("presents the API's fraction as a percentage without deriving it", () => {
    expect(formatRate(0.3475)).toBe("34.75%");
    expect(formatRate(0.0169)).toBe("1.69%");
    expect(formatRate(1)).toBe("100.00%");
    expect(formatRate(0)).toBe("0.00%");
  });

  it("does not invent a rate from a bad value", () => {
    expect(formatRate(Number.NaN)).toBe("0.00%");
  });
});

describe("paiseToRupees", () => {
  it("gives charts a number, at the same scale as the formatter", () => {
    expect(paiseToRupees(52_520_600)).toBe(525_206);
    expect(paiseToRupees(0)).toBe(0);
  });
});
