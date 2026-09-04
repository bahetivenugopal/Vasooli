"use client";

/**
 * Corridor timeline — one degradation, from detection to what followed.
 *
 * The corridor is the one entity type whose story is mostly a single decision:
 * the statistics that triggered it, the model's diagnosis in its own words, and
 * the gate's answer. So the head of this page is the evidence and the diagnosis,
 * and the timeline below it is every audit entry filed against that corridor.
 */

import type { AuditEntryRead, CorridorDetectionRead } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AsyncBoundary } from "@/components/features/async-boundary";
import { Field, MetricCard, Money } from "@/components/features/metric-card";
import { PageHeader } from "@/components/features/app-shell";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { Timeline, TimelineLegend } from "@/components/features/timeline";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";
import { formatRate } from "@/lib/money";

export default function CorridorTimelinePage({
  params,
}: {
  // A plain object, not a promise: Next 14's App Router passes route params
  // synchronously. Declaring it as a promise type-checks and then throws at
  // request time, which a build cannot catch — the routes are exercised in
  // `docs/smoke-checklist.md` for exactly this reason.
  params: { detectionId: string };
}) {
  const { detectionId } = params;
  const detection = useApi(() => api.rootCause.detection(detectionId), [detectionId]);

  return (
    <>
      <PageHeader
        title="Corridor timeline"
        description="One degradation episode: the statistics that triggered it, what the reasoning layer concluded, and what the policy engine allowed to happen next."
      />
      <AsyncBoundary state={detection} loadingRows={3}>
        {(row: CorridorDetectionRead) => (
          <div className="space-y-6">
            <DetectionHead detection={row} />
            <CorridorEntries corridorKey={row.corridor_key} batchId={row.batch_id} />
          </div>
        )}
      </AsyncBoundary>
    </>
  );
}

function DetectionHead({ detection }: { detection: CorridorDetectionRead }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Corridor"
          value={
            <span className="text-lg">
              {[detection.issuer, detection.method, detection.route_id]
                .filter(Boolean)
                .join(" × ") || detection.corridor_level}
            </span>
          }
          hint={`${detection.corridor_level} · ${detection.affected_attempts} attempts affected`}
        />
        <MetricCard
          label="Observed vs baseline"
          value={
            <>
              <span className="text-at-risk">{formatRate(detection.observed_success_rate, 1)}</span>
              <span className="text-muted-foreground"> vs </span>
              <span>{formatRate(detection.baseline_success_rate, 1)}</span>
            </>
          }
          hint={`${detection.window_attempts} in window / ${detection.baseline_attempts} baseline`}
        />
        <MetricCard
          label="Statistical confidence"
          value={formatRate(detection.confidence, 2)}
          hint={`p = ${detection.p_value.toExponential(3)}. Never mixed with the model's own confidence.`}
        />
        <MetricCard
          label="Value at risk"
          value={<Money paise={detection.value_at_risk_paise} tone="at-risk" compact />}
          tone="at-risk"
          hint="Recorded in metadata — the money itself lives on the payment entries"
        />
      </div>

      <Card className="border-reasoned/30 bg-reasoned-muted/30">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">What the reasoning layer concluded</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={detection.determination === "systemic" ? "atRisk" : "reasoned"}>
              {humanise(detection.determination)}
            </Badge>
            <Badge variant="muted">{humanise(detection.hypothesis)}</Badge>
            <Badge variant="muted">recommended: {humanise(detection.recommended_action)}</Badge>
            <ProvenanceBadge
              provenance={detection.provenance}
              confidence={detection.model_confidence}
            />
          </div>
          {detection.diagnosis_reasoning && (
            <blockquote className="rounded-md border-l-2 border-reasoned bg-background/70 px-3 py-2 text-sm italic">
              {detection.diagnosis_reasoning}
            </blockquote>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">What the policy engine allowed</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Decision">
              <Badge variant={detection.policy_allowed ? "recovery" : "atRisk"}>
                {detection.policy_allowed ? "Permitted" : "Refused"}
              </Badge>
            </Field>
            <Field label="Authorised action">{humanise(detection.authorised_action)}</Field>
            <Field label="Authorising rule">
              <RuleBadge rule={detection.authorising_rule} />
            </Field>
            <Field label="Overruled recommendation">
              {detection.overridden_recommendation ? (
                <span className="text-at-risk">
                  {humanise(detection.overridden_recommendation)}
                </span>
              ) : (
                "—"
              )}
            </Field>
            <Field label="Window">
              {formatDateTime(detection.window_start)} → {formatDateTime(detection.window_end)}
            </Field>
            <Field label="Failing customers">{detection.distinct_failing_customers}</Field>
            <Field label="Decline mix" className="sm:col-span-2">
              <div className="flex flex-wrap gap-1">
                {Object.entries(detection.decline_mix ?? {}).map(([code, count]) => (
                  <Badge key={code} variant="muted" className="font-mono">
                    {code} ×{String(count)}
                  </Badge>
                ))}
              </div>
            </Field>
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}

function CorridorEntries({ corridorKey, batchId }: { corridorKey: string; batchId: string }) {
  const state = useApi(() => api.entityTimeline("corridor", corridorKey), [corridorKey]);
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">Every decision on this corridor</h2>
      <TimelineLegend />
      <AsyncBoundary state={state}>
        {(entries: AuditEntryRead[]) => (
          <Timeline entries={entries.filter((entry) => entry.batch_id === batchId)} />
        )}
      </AsyncBoundary>
    </section>
  );
}
