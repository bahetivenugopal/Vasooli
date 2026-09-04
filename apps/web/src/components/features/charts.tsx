"use client";

/**
 * The chart primitives. Recharts, kept simple on purpose.
 *
 * §5.3: "A clear bar chart beats an elaborate visualization that needs
 * explaining." So there are two: a horizontal bar chart for counts, and a
 * grouped money chart for at-risk-versus-recovered. Everything else on the
 * dashboard is a table, which for a judge reading numbers is usually better.
 *
 * Colours come from the semantic tokens, so at-risk and recovered read the same
 * way in a chart as they do in a metric card.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatPaiseCompact, paiseToRupees } from "@/lib/money";

const AXIS = { fontSize: 11, fill: "hsl(var(--muted-foreground))" } as const;

/**
 * Recharts hands its formatters a loosely-typed value. Chart rows are built from
 * `paiseToRupees`, so anything that is not a finite number here means the row was
 * built wrong — which is a bug worth showing as zero rather than as `NaN`.
 */
function rupeesToPaise(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value * 100 : 0;
}

/**
 * Charts do not animate in.
 *
 * Recharts restarts its entry animation whenever `ResponsiveContainer` resizes,
 * and a full-page screenshot resizes the viewport — which produced charts with
 * correct axes and no bars at all. The same thing happens when a browser window
 * is resized mid-demo. Bars that are simply *there* are worth more here than
 * bars that grow.
 */
const NO_ANIMATION = { isAnimationActive: false } as const;

const TOOLTIP_STYLE = {
  backgroundColor: "hsl(var(--popover))",
  border: "1px solid hsl(var(--border))",
  borderRadius: "0.5rem",
  fontSize: "0.8rem",
  color: "hsl(var(--popover-foreground))",
} as const;

export interface CountDatum {
  label: string;
  value: number;
  /** Optional per-bar tone; defaults to the neutral chart colour. */
  tone?: "recovery" | "at-risk" | "reasoned" | "neutral";
}

const TONE_FILL: Record<NonNullable<CountDatum["tone"]>, string> = {
  recovery: "hsl(var(--recovery))",
  "at-risk": "hsl(var(--at-risk))",
  reasoned: "hsl(var(--reasoned))",
  neutral: "hsl(var(--ruled))",
};

/** A horizontal bar chart for counts. Horizontal so long labels stay readable. */
export function CountBarChart({ data, height = 240 }: { data: CountDatum[]; height?: number }) {
  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Nothing to chart.</p>;
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
        <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
        <XAxis type="number" tick={AXIS} allowDecimals={false} />
        <YAxis type="category" dataKey="label" tick={AXIS} width={190} />
        <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "hsl(var(--muted))" }} />
        <Bar dataKey="value" name="Count" radius={[0, 4, 4, 0]} {...NO_ANIMATION}>
          {data.map((datum) => (
            <Cell key={datum.label} fill={TONE_FILL[datum.tone ?? "neutral"]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface MoneyDatum {
  label: string;
  atRiskPaise: number;
  recoveredPaise: number;
}

/**
 * At-risk against recovered, grouped. The comparison is the whole point, so the
 * two bars sit side by side rather than stacked — a stack makes the recovered
 * share look like a portion of a whole, which invites reading it as a rate.
 *
 * Only use this where the rows are within an order of magnitude of each other,
 * which is true within an engine and false across them. `RateBarChart` is the
 * cross-engine comparison; see its note.
 */
export function MoneyBarChart({ data, height = 260 }: { data: MoneyDatum[]; height?: number }) {
  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Nothing to chart.</p>;
  }
  const rows = data.map((datum) => ({
    label: datum.label,
    "At risk": paiseToRupees(datum.atRiskPaise),
    Recovered: paiseToRupees(datum.recoveredPaise),
  }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
        <CartesianGrid vertical={false} stroke="hsl(var(--border))" />
        <XAxis dataKey="label" tick={AXIS} interval={0} />
        <YAxis
          tick={AXIS}
          width={70}
          tickFormatter={(value: number) => formatPaiseCompact(rupeesToPaise(value))}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ fill: "hsl(var(--muted))" }}
          formatter={(value) => formatPaiseCompact(rupeesToPaise(value))}
        />
        <Legend wrapperStyle={{ fontSize: "0.75rem" }} />
        <Bar dataKey="At risk" fill="hsl(var(--at-risk))" radius={[4, 4, 0, 0]} {...NO_ANIMATION} />
        <Bar
          dataKey="Recovered"
          fill="hsl(var(--recovery))"
          radius={[4, 4, 0, 0]}
          {...NO_ANIMATION}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface RateDatum {
  label: string;
  /** The API's own fraction. Never derived here from a numerator and a denominator. */
  rate: number;
}

/**
 * Recovery rate per engine, on a shared 0-100% axis.
 *
 * This exists because the obvious chart does not work. The three engines'
 * absolute figures span three orders of magnitude — Engine 2 recovered
 * Rs 13,362 against Engine 3's Rs 92.94 lakh — so a grouped money chart renders
 * two engines as a flat line, and a log axis renders all six bars at nearly the
 * same height, which is worse: it implies the engines are comparable in size
 * when the whole point is that they are not.
 *
 * Rates *are* comparable, so they are what gets charted. The absolute rupees sit
 * on the contribution cards immediately above, where the difference in scale is
 * legible as text rather than misrepresented as geometry.
 */
export function RateBarChart({ data, height = 220 }: { data: RateDatum[]; height?: number }) {
  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Nothing to chart.</p>;
  }
  const rows = data.map((datum) => ({ label: datum.label, Rate: datum.rate * 100 }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 32, top: 8, bottom: 8 }}>
        <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
        <XAxis
          type="number"
          domain={[0, 100]}
          tick={AXIS}
          tickFormatter={(value: number) => `${value}%`}
        />
        <YAxis type="category" dataKey="label" tick={AXIS} width={190} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ fill: "hsl(var(--muted))" }}
          formatter={(value) => `${Number(value).toFixed(2)}%`}
        />
        <Bar
          dataKey="Rate"
          name="Recovery rate"
          fill="hsl(var(--recovery))"
          radius={[0, 4, 4, 0]}
          {...NO_ANIMATION}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Turn an API-supplied `{key: count}` map into chart rows.
 *
 * This is the only reshaping the dashboard does, and it is not computation: no
 * value is added, divided or derived — the keys are relabelled and the rows are
 * sorted so the chart is readable.
 */
export function countsToData(
  counts: Record<string, number> | undefined | null,
  toLabel: (key: string) => string,
  tone?: (key: string) => CountDatum["tone"],
): CountDatum[] {
  return Object.entries(counts ?? {})
    .map(([key, value]) => ({ label: toLabel(key), value, tone: tone?.(key) }))
    .sort((a, b) => b.value - a.value);
}
