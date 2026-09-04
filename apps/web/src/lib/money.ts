/**
 * The one money formatter. Paise in, rupees out, nowhere else.
 *
 * Money is integer paise everywhere in this project — never a float — and is
 * converted to rupees at display only. That conversion happens here and in no
 * other file: a second formatter is how a lakh becomes a crore on camera, and
 * it is the kind of bug that is invisible until the exact moment it is not.
 *
 * All formatting uses the `en-IN` locale, so separators are the Indian grouping
 * (1,00,000 rather than 100,000) that a judge in this market reads without
 * having to count digits.
 */

const RUPEE = "₹";

/** 100 paise to the rupee. Stated once so the constant is not retyped. */
const PAISE_PER_RUPEE = 100;

/** Indian-format grouping, no decimals. `formatPaise` handles the conversion. */
const WHOLE = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});

const WITH_PAISE = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export interface MoneyOptions {
  /** Show the paise component. Off by default: recovery figures are large. */
  paise?: boolean;
  /** Drop the ₹ sign, for a column that already has one in its header. */
  bare?: boolean;
}

/**
 * Integer paise -> a rupee string.
 *
 * Rounds toward zero on a non-integer input rather than silently carrying a
 * float through, because a fractional paise has no meaning and the arithmetic
 * that produced it is the actual bug.
 */
export function formatPaise(paise: number, options: MoneyOptions = {}): string {
  const { paise: showPaise = false, bare = false } = options;
  const safe = Number.isFinite(paise) ? Math.trunc(paise) : 0;
  const negative = safe < 0;
  const rupees = Math.abs(safe) / PAISE_PER_RUPEE;
  const body = showPaise ? WITH_PAISE.format(rupees) : WHOLE.format(Math.trunc(rupees));
  const sign = negative ? "-" : "";
  return bare ? `${sign}${body}` : `${sign}${RUPEE}${body}`;
}

/**
 * A compact rupee figure for a headline, in the units Indian finance speaks:
 * thousands, lakh, crore. `₹2,28,27,113` is correct and unreadable at a glance;
 * `₹2.28 Cr` is the number a judge takes in during a three-second look.
 *
 * The precise value always appears beside it — this is a reading aid, never a
 * replacement, because a rounded headline with no exact figure anywhere is the
 * kind of number nobody can check.
 */
export function formatPaiseCompact(paise: number): string {
  const safe = Number.isFinite(paise) ? Math.trunc(paise) : 0;
  const negative = safe < 0;
  const rupees = Math.abs(safe) / PAISE_PER_RUPEE;
  const sign = negative ? "-" : "";

  const scale = (value: number, unit: string) =>
    `${sign}${RUPEE}${value.toFixed(value >= 100 ? 0 : 2).replace(/\.00$/, "")} ${unit}`;

  if (rupees >= 1_00_00_000) return scale(rupees / 1_00_00_000, "Cr");
  if (rupees >= 1_00_000) return scale(rupees / 1_00_000, "L");
  if (rupees >= 1_000) return scale(rupees / 1_000, "K");
  return `${sign}${RUPEE}${WHOLE.format(Math.trunc(rupees))}`;
}

/**
 * A rate the API already computed, as a percentage string.
 *
 * Takes the API's fraction and multiplies by 100 — that is presentation, not
 * computation. Nothing in this app derives a rate from a numerator and a
 * denominator: the backend does that, so there is exactly one place a recovery
 * rate can be wrong.
 */
export function formatRate(rate: number, fractionDigits = 2): string {
  const safe = Number.isFinite(rate) ? rate : 0;
  return `${(safe * 100).toFixed(fractionDigits)}%`;
}

/** Paise -> whole rupees, for chart axes that need a number rather than a string. */
export function paiseToRupees(paise: number): number {
  const safe = Number.isFinite(paise) ? Math.trunc(paise) : 0;
  return safe / PAISE_PER_RUPEE;
}
