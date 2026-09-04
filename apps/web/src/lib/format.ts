/**
 * Display formatting that is not money. Money lives in `money.ts`, alone.
 *
 * Everything here converts a value the API already produced into something
 * readable. Nothing derives, aggregates or recomputes: the dashboard's job is to
 * show what the audit trail computed, and the moment it starts calculating there
 * are two sources of truth and one of them is eventually wrong on camera.
 */

/** Timestamps arrive tz-aware UTC and are converted at display only. */
const IST = "Asia/Kolkata";

const DATE_TIME = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST,
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const TIME = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const DATE = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST,
  day: "2-digit",
  month: "short",
  year: "numeric",
});

/** `2026-09-04T09:00:00Z` -> `04 Sep 2026, 14:30 IST`. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${DATE_TIME.format(date)} IST`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return DATE.format(date);
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${TIME.format(date)} IST`;
}

/** `send_pre_debit_notice` -> `Send pre debit notice`. */
export function humanise(value: string | null | undefined): string {
  if (!value) return "—";
  const spaced = value.replace(/[_-]+/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A plain integer with Indian grouping. Counts, not money. */
export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-IN").format(Number.isFinite(value) ? value : 0);
}

/**
 * Split a rule citation into its skill and its rule id.
 *
 * The format is `<skill>:<rule-id>` — `rbi-mandate-rules:A2`, `policy-bounds:QH1`.
 * Anything that does not parse is shown whole rather than mangled: an
 * unexpected citation is worth seeing, not hiding.
 */
export function splitCitation(rule: string): { skill: string; id: string } | null {
  const index = rule.indexOf(":");
  if (index <= 0 || index === rule.length - 1) return null;
  return { skill: rule.slice(0, index), id: rule.slice(index + 1) };
}
