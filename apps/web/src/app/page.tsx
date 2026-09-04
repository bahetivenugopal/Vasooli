"use client";

/**
 * The overview — the single most important screen in the project.
 *
 * §5.2: it has to answer, within about three seconds of being seen, *how much
 * money was at risk, how much came back, and can I trust it?* Everything on this
 * page is arranged around that sentence: the headline, then who contributed to
 * it, then the refusals that make it credible, then the last few decisions with
 * the rule behind each.
 *
 * **Nothing here computes a metric.** Every figure comes from
 * `GET /api/v1/overview`, which recomputes it from the audit trail on each
 * request. The only arithmetic in this file is `formatRate` turning the API's
 * fraction into a percentage for display.
 */

import Link from "next/link";
import { useCallback } from "react";
import { ArrowRight, Info } from "lucide-react";

import type { EngineContribution, OverviewSummary } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { AsyncBoundary, EmptyState } from "@/components/features/async-boundary";
import { RateBarChart } from "@/components/features/charts";
import { PageHeader } from "@/components/features/app-shell";
import { MetricCard, Money } from "@/components/features/metric-card";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { RunTrigger } from "@/components/features/run-trigger";
import { TrustStrip } from "@/components/features/trust-strip";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";
import { formatRate } from "@/lib/money";

const ENGINE_ROUTES: Record<string, string> = {
  root_cause: "/engines/root-cause",
  mandate_recovery: "/engines/mandate-recovery",
  receivables: "/engines/receivables",
};

export default function OverviewPage() {
  const state = useApi(() => api.overview({ recent_limit: 9 }), []);
  const reload = state.reload;
  const onRunComplete = useCallback(() => reload(), [reload]);

  return (
    <>
      <PageHeader
        title="Recovery overview"
        description="Three engines, one policy layer, one audit trail. Every figure on this page is recomputed from that trail by the API — this dashboard formats numbers, it never calculates them."
      />

      <AsyncBoundary
        state={state}
        loadingRows={4}
        isEmpty={(data: OverviewSummary) => data.by_engine.length === 0}
        empty={
          <div className="space-y-4">
            <EmptyState
              title="No completed batch runs yet"
              hint={
                <>
                  Run a batch to populate this dashboard. Start one below, or from the command line
                  with <code className="font-mono">python scripts/receivables_demo.py</code>. Every
                  run is seeded, so the same seed gives the same numbers.
                </>
              }
            />
            <div className="grid gap-3 lg:grid-cols-3">
              <RunTrigger engine="root-cause" onComplete={onRunComplete} />
              <RunTrigger engine="mandate-recovery" onComplete={onRunComplete} />
              <RunTrigger engine="receivables" onComplete={onRunComplete} />
            </div>
          </div>
        }
      >
        {(data) => (
          <div className="space-y-6">
            <Headline data={data} />

            <section className="space-y-3">
              <h2 className="text-lg font-semibold tracking-tight">How each engine contributed</h2>
              <div className="grid gap-3 lg:grid-cols-3">
                {data.by_engine.map((contribution) => (
                  <ContributionCard key={contribution.batch_id} contribution={contribution} />
                ))}
              </div>
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Recovery rate by engine</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Rates rather than rupees, because the three engines differ by three orders of
                    magnitude and a money chart would render two of them as a flat line. The
                    absolute figures are on the cards above — and each rate means what that
                    engine&apos;s own definition says it means.
                  </p>
                </CardHeader>
                <CardContent>
                  <RateBarChart
                    data={data.by_engine.map((contribution) => ({
                      label: contribution.label,
                      rate: contribution.recovery_rate,
                    }))}
                  />
                </CardContent>
              </Card>
            </section>

            <TrustStrip metrics={data.trust} />

            <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
              <RecentActivity data={data} />
              <div className="space-y-3">
                <h2 className="text-lg font-semibold tracking-tight">Run a batch</h2>
                <p className="text-sm text-muted-foreground">
                  Every run is seeded and reproducible. A batch id may only be used once — reusing
                  one would merge two runs into a single set of metrics.
                </p>
                <RunTrigger engine="root-cause" onComplete={onRunComplete} />
                <RunTrigger engine="mandate-recovery" onComplete={onRunComplete} />
                <RunTrigger engine="receivables" onComplete={onRunComplete} />
              </div>
            </div>
          </div>
        )}
      </AsyncBoundary>
    </>
  );
}

function Headline({ data }: { data: OverviewSummary }) {
  return (
    <Card className="border-recovery/30 bg-recovery-muted/40">
      <CardContent className="p-6">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Recovered across all three engines
            </p>
            <p className="mt-1 text-5xl font-semibold tracking-tight text-recovery">
              <Money paise={data.amount_recovered_paise} tone="recovery" compact />
            </p>
            <p className="mt-2 text-sm text-muted-foreground">
              of <Money paise={data.amount_at_risk_paise} tone="at-risk" compact /> at risk ·{" "}
              <span className="font-semibold text-foreground">
                {formatRate(data.recovery_rate)} recovery rate
              </span>
            </p>
          </div>
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Audited decisions
              </dt>
              <dd className="text-lg font-semibold tabular-nums">{data.entries}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Engines reporting
              </dt>
              <dd className="text-lg font-semibold tabular-nums">
                {data.engines_reporting.length} / 3
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                Schema violations
              </dt>
              <dd className="text-lg font-semibold tabular-nums">{data.policy_violations}</dd>
            </div>
          </dl>
        </div>

        {data.engines_missing.length > 0 && (
          <p className="mt-4 text-sm text-at-risk">
            No completed run yet for{" "}
            {data.engines_missing.map((engine) => humanise(engine)).join(", ")} — this headline is
            missing their contribution.
          </p>
        )}

        <Separator className="my-4" />
        <p className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {data.blended_caveat}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(data.batch_ids).map(([engine, batchId]) => (
            <Badge key={engine} variant="muted" className="font-mono">
              {humanise(engine)}: {batchId}
            </Badge>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function ContributionCard({ contribution }: { contribution: EngineContribution }) {
  const href = ENGINE_ROUTES[contribution.engine];
  return (
    <MetricCard
      label={contribution.label}
      value={<Money paise={contribution.amount_recovered_paise} tone="recovery" compact />}
      tone="recovery"
      hint={
        <>
          of <Money paise={contribution.amount_at_risk_paise} tone="at-risk" compact /> at risk ·{" "}
          {formatRate(contribution.recovery_rate)} · {contribution.entries} decisions
        </>
      }
    >
      <div className="space-y-2 pt-2">
        <p className="text-[11px] leading-snug text-muted-foreground">
          <span className="font-medium text-foreground">What this counts: </span>
          {contribution.recovery_definition}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="muted" className="font-mono">
            seed {contribution.seed}
          </Badge>
          {contribution.dataset_batch_id && (
            <Badge variant="muted" className="font-mono">
              {contribution.dataset_batch_id}
            </Badge>
          )}
        </div>
        {href && (
          <Link
            href={href}
            className="inline-flex items-center gap-1 text-sm font-medium text-foreground hover:underline"
          >
            Open engine view
            <ArrowRight className="size-3.5" aria-hidden />
          </Link>
        )}
      </div>
    </MetricCard>
  );
}

function RecentActivity({ data }: { data: OverviewSummary }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Recent decisions</CardTitle>
        <p className="text-sm text-muted-foreground">
          The latest entries from each engine, each naming the rule that authorised it. Per engine
          rather than simply newest-first: the three do not share a clock, so a plain sort would
          show only whichever engine ran last.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.recent_activity.map((entry) => (
          <div key={entry.id} className="space-y-1.5 border-b pb-3 last:border-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-medium">{humanise(entry.action)}</span>
              <Badge
                variant={
                  entry.outcome === "success"
                    ? "recovery"
                    : ["blocked", "halted", "failure"].includes(entry.outcome)
                      ? "atRisk"
                      : "muted"
                }
              >
                {humanise(entry.outcome)}
              </Badge>
              <span className="font-mono text-xs text-muted-foreground">{entry.entity_id}</span>
              <span className="ml-auto text-xs text-muted-foreground">
                {formatDateTime(entry.timestamp)}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <RuleBadge rule={entry.authorising_rule} />
              <ProvenanceBadge
                provenance={entry.provenance}
                degraded={(entry.metadata ?? {}).degraded === true}
                confidence={entry.model_confidence}
              />
            </div>
            <p className="text-xs text-muted-foreground">{entry.rationale}</p>
          </div>
        ))}
        <Link
          href="/audit"
          className="inline-flex items-center gap-1 text-sm font-medium hover:underline"
        >
          Open the full audit trail
          <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </CardContent>
    </Card>
  );
}
