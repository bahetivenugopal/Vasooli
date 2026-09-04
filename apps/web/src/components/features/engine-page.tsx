"use client";

/**
 * The engine-page frame: pick a run, show its headline, trigger a new one.
 *
 * All three engine views need exactly this, so it is written once. Each view
 * supplies only what is specific to it — its worklist, its charts, its own
 * story — through the render prop.
 *
 * The headline strip here reads the *audit trail's* batch summary rather than
 * the engine's `RunSummary`, because the engine summary is only returned from
 * `POST /runs` and is never stored. That is the honest source anyway: it is
 * recomputed from the entries every time it is asked for.
 */

import type { ReactNode } from "react";
import { useCallback, useState } from "react";

import type { BatchRunRead, BatchSummary, Engine } from "@vasooli/shared-types";

import { Card, CardContent } from "@/components/ui/card";
import { AsyncBoundary, EmptyState } from "@/components/features/async-boundary";
import { MetricCard, Money } from "@/components/features/metric-card";
import { PageHeader } from "@/components/features/app-shell";
import { RunPicker } from "@/components/features/run-picker";
import { RunTrigger, type RunEngine } from "@/components/features/run-trigger";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatRate } from "@/lib/money";

export function EnginePage({
  engine,
  runEngine,
  title,
  description,
  children,
}: {
  engine: Engine;
  runEngine: RunEngine;
  title: string;
  description: ReactNode;
  children: (batchId: string) => ReactNode;
}) {
  const [batchId, setBatchId] = useState<string | null>(null);
  const runs = useApi(() => api.batches({ engine, limit: 50 }), [engine]);

  const reloadRuns = runs.reload;
  const onRunComplete = useCallback(
    (newBatchId: string) => {
      setBatchId(newBatchId);
      reloadRuns();
    },
    [reloadRuns],
  );

  return (
    <>
      <PageHeader title={title} description={description} actions={null} />

      <AsyncBoundary
        state={runs}
        loadingRows={2}
        isEmpty={(data: BatchRunRead[]) => data.length === 0}
        empty={
          <div className="space-y-4">
            <EmptyState
              title={`No ${runEngine} runs yet`}
              hint="Trigger one below. Every run is seeded, so the same seed reproduces the same numbers exactly."
            />
            <RunTrigger engine={runEngine} onComplete={onRunComplete} />
          </div>
        }
      >
        {(data) => {
          const selected = batchId ?? data[0].batch_id;
          return (
            <div className="space-y-6">
              <Card>
                <CardContent className="space-y-4 p-4">
                  <RunPicker runs={data} value={selected} onChange={setBatchId} />
                  <RunTrigger engine={runEngine} onComplete={onRunComplete} />
                </CardContent>
              </Card>

              <RunHeadline batchId={selected} />
              {children(selected)}
            </div>
          );
        }}
      </AsyncBoundary>
    </>
  );
}

/**
 * The four figures every engine reports the same way, so a reader moving
 * between engine views does not have to relearn where the headline is.
 */
function RunHeadline({ batchId }: { batchId: string }) {
  const state = useApi(() => api.batchSummary(batchId), [batchId]);
  return (
    <AsyncBoundary state={state} loadingRows={1}>
      {(summary: BatchSummary) => (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Recovered"
            value={<Money paise={summary.amount_recovered_paise} tone="recovery" compact />}
            tone="recovery"
            hint={`${formatRate(summary.recovery_rate)} of the value at risk`}
          />
          <MetricCard
            label="At risk"
            value={<Money paise={summary.amount_at_risk_paise} tone="at-risk" compact />}
            tone="at-risk"
            hint="Booked once per entity, on one entry"
          />
          <MetricCard
            label="Audited decisions"
            value={summary.entries}
            hint={`${summary.blocked_count} blocked · ${summary.halted_count} halted · ${summary.escalated_count} escalated`}
          />
          <MetricCard
            label="Schema violations"
            value={summary.policy_violations}
            hint="Entries that broke the audit schema's own rules. Should be zero."
          />
        </div>
      )}
    </AsyncBoundary>
  );
}
