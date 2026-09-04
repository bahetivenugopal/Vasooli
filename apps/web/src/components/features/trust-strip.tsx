"use client";

/**
 * The trust strip — the numbers that prove the bounds are real.
 *
 * §5.2: these are presented as *features*, with brief labels making clear that
 * non-zero is good. Most dashboards hide their refusals; showing them is the
 * point, and it is the single most differentiating thing on this screen.
 *
 * Every count and every explanation comes from the API's `trust` array. The
 * dashboard neither computes a count nor writes the sentence explaining it —
 * `services/overview.py` owns both, so the story the UI tells and the story the
 * trail supports cannot diverge.
 */

import { useState } from "react";
import { ShieldCheck } from "lucide-react";

import type { TrustMetric } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RuleBadge } from "@/components/features/provenance-badge";
import { cn } from "@/lib/utils";

export function TrustStrip({ metrics }: { metrics: TrustMetric[] }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const open = metrics.find((metric) => metric.key === openKey);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldCheck className="size-4 text-recovery" aria-hidden />
          What the system refused to do
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          Non-zero is the good outcome here. Each of these is an action the engines proposed and
          their own rules stopped, or a judgment the reasoning layer declined to make. A run with
          none of them means the gate never fired.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {metrics.map((metric) => {
            const active = metric.key === openKey;
            const hasRules = Object.keys(metric.by_rule ?? {}).length > 0;
            return (
              <button
                key={metric.key}
                type="button"
                onClick={() => setOpenKey(active ? null : metric.key)}
                aria-expanded={active}
                disabled={!hasRules}
                className={cn(
                  "rounded-lg border px-3 py-2 text-left transition-colors",
                  hasRules ? "hover:border-foreground/30 hover:bg-muted/50" : "cursor-default",
                  active && "border-foreground/40 bg-muted/60",
                )}
              >
                <p className="text-xl font-semibold tabular-nums">{metric.value}</p>
                <p className="text-xs font-medium">{metric.label}</p>
                <p className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground">
                  {metric.meaning}
                </p>
              </button>
            );
          })}
        </div>

        {open && (
          <div className="rounded-lg border bg-muted/40 p-3">
            <p className="text-sm">
              <span className="font-medium">{open.label}.</span>{" "}
              <span className="text-muted-foreground">{open.meaning}</span>
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(open.by_rule ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([rule, count]) => (
                  <span key={rule} className="inline-flex items-center gap-1">
                    <RuleBadge rule={rule} />
                    <Badge variant="muted">×{count}</Badge>
                  </span>
                ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
