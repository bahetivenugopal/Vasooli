/**
 * The metric card and the money figure. Two components, defined once each.
 *
 * §5.6 makes reuse a rule rather than a preference, and money is the specific
 * case worth being strict about: `<Money>` is the only component that renders a
 * rupee value, and it delegates to the one formatter in `lib/money.ts`. A second
 * place that turns paise into rupees is how a lakh becomes a crore on camera.
 */

import type { ReactNode } from "react";

import { Card, CardContent } from "@/components/ui/card";
import { formatPaise, formatPaiseCompact } from "@/lib/money";
import { cn } from "@/lib/utils";

export type MoneyTone = "recovery" | "at-risk" | "neutral";

const TONE_CLASS: Record<MoneyTone, string> = {
  recovery: "text-recovery",
  "at-risk": "text-at-risk",
  neutral: "text-foreground",
};

export function Money({
  paise,
  tone = "neutral",
  compact = false,
  className,
}: {
  paise: number;
  tone?: MoneyTone;
  /** Show `₹2.28 Cr` instead of the exact figure. The exact one goes in `title`. */
  compact?: boolean;
  className?: string;
}) {
  const exact = formatPaise(paise);
  return (
    <span className={cn(TONE_CLASS[tone], className)} title={exact}>
      {compact ? formatPaiseCompact(paise) : exact}
    </span>
  );
}

export function MetricCard({
  label,
  value,
  hint,
  tone = "neutral",
  className,
  children,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: MoneyTone;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <Card className={className}>
      <CardContent className="space-y-1 p-4">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
        <p className={cn("text-2xl font-semibold tabular-nums", TONE_CLASS[tone])}>{value}</p>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
        {children}
      </CardContent>
    </Card>
  );
}

/** A label/value pair for the dense definition lists on timeline and detail views. */
export function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("space-y-0.5", className)}>
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="text-sm">{children}</dd>
    </div>
  );
}
