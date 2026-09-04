"use client";

/**
 * Engine 1 — Root-Cause Recovery.
 *
 * §5.3 asks this view for four things: corridor health at a glance, detections
 * with observed-versus-baseline rates, detection performance against ground
 * truth **including false positives shown honestly**, and the actions taken per
 * detection.
 *
 * The false positives are not a footnote here. A detector that reports only its
 * hits is a detector nobody should believe, so precision, the unmatched
 * detections and the decoy count sit beside recall at the same size.
 */

import Link from "next/link";
import { ArrowRight } from "lucide-react";

import type { CorridorDetectionRead, RootCauseRunSummary } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AsyncBoundary, EmptyState } from "@/components/features/async-boundary";
import { CountBarChart, countsToData } from "@/components/features/charts";
import { EnginePage } from "@/components/features/engine-page";
import { MetricCard, Money } from "@/components/features/metric-card";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";
import { formatRate } from "@/lib/money";

export default function RootCausePage() {
  return (
    <EnginePage
      engine="root_cause"
      runEngine="root-cause"
      title="Root-cause recovery"
      description="Watches payment attempts corridor by corridor. When one degrades, it diagnoses whether this is one customer's problem or the corridor's — the same decline code with opposite correct responses. Detection is purely statistical and never sees a model; the diagnosis is where the reasoning happens."
    >
      {(batchId) => (
        <>
          <RunMetrics batchId={batchId} />
          <Detections batchId={batchId} />
        </>
      )}
    </EnginePage>
  );
}

function RunMetrics({ batchId }: { batchId: string }) {
  const state = useApi(() => api.rootCause.summary(batchId), [batchId]);
  return (
    <AsyncBoundary state={state} loadingRows={2}>
      {(summary: RootCauseRunSummary) => (
        <div className="space-y-6">
          <section className="space-y-3">
            <h2 className="text-lg font-semibold tracking-tight">Corridor health</h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard
                label="Attempts ingested"
                value={summary.attempts_ingested}
                hint={`${summary.failed_attempts} failed`}
              />
              <MetricCard
                label="Degradations detected"
                value={summary.detections}
                hint="Statistical only — no model involved in detection"
              />
              <MetricCard
                label="Reroutes authorised"
                value={summary.reroutes_authorised}
                hint="Bounded and expiring. They contribute zero to recovery."
              />
              <MetricCard
                label="Policy denials"
                value={summary.policy_denials}
                hint={`${summary.human_escalations} escalated to a human`}
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard
                label="Retries suppressed"
                value={summary.retries_suppressed}
                hint="Permanent refusals — a dead instrument, a spent budget"
              />
              <MetricCard
                label="Retries deferred"
                value={summary.retries_deferred}
                hint="Timing only. Not a suppression: the revenue is still recoverable."
              />
              <MetricCard
                label="Deterministic fallbacks"
                value={summary.llm_fallbacks}
                hint="Reasoning calls that degraded and still completed"
              />
              <MetricCard
                label="Run clock"
                value={<span className="text-base">{formatDateTime(summary.now)}</span>}
                hint="Derived from the last attempt in the dataset, not wall-clock time"
              />
            </div>
          </section>

          {summary.detection_score && <DetectionScoreCard score={summary.detection_score} />}

          <div className="grid gap-3 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Diagnoses by determination</CardTitle>
                <p className="text-sm text-muted-foreground">
                  The central judgment: systemic, individual, or not enough evidence to say. The
                  third is a first-class answer — a layer that always sounds confident will
                  confidently be wrong.
                </p>
              </CardHeader>
              <CardContent>
                <CountBarChart
                  height={180}
                  data={countsToData(summary.diagnoses_by_determination, humanise, (key) =>
                    key === "systemic" ? "at-risk" : key === "individual" ? "reasoned" : "neutral",
                  )}
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">Denials by rule</CardTitle>
                <p className="text-sm text-muted-foreground">
                  Which stopping rule refused, and how often.
                </p>
              </CardHeader>
              <CardContent>
                <CountBarChart
                  height={180}
                  data={countsToData(
                    summary.denials_by_rule,
                    (key) => key,
                    () => "at-risk",
                  )}
                />
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </AsyncBoundary>
  );
}

function DetectionScoreCard({
  score,
}: {
  score: NonNullable<RootCauseRunSummary["detection_score"]>;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Detection performance against the generator&apos;s ground truth
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          Scored by strict corridor match: a detection on the right method but the wrong issuer is a
          false positive, not partial credit. The unmatched detections below are shown in full,
          because a detector that reports only its hits is one nobody should believe.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <MetricCard label="Recall" value={formatRate(score.recall)} tone="recovery" />
          <MetricCard label="Precision" value={formatRate(score.precision)} />
          <MetricCard label="True positives" value={score.true_positives} />
          <MetricCard
            label="False positives"
            value={score.false_positives}
            tone={score.false_positives > 0 ? "at-risk" : "neutral"}
            hint="Shown honestly, not netted out"
          />
          <MetricCard
            label="Decoy false positives"
            value={score.decoy_false_positives}
            hint="A corridor the manifest marks as bait. Should be zero."
          />
        </div>

        {score.missed_degradations && score.missed_degradations.length > 0 && (
          <div>
            <h3 className="mb-1 text-sm font-medium">Missed degradations</h3>
            <div className="flex flex-wrap gap-2">
              {score.missed_degradations.map((label) => (
                <Badge key={label} variant="atRisk" className="font-mono">
                  {label}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {score.unmatched_detections && score.unmatched_detections.length > 0 && (
          <div>
            <h3 className="mb-1 text-sm font-medium">Detections the ground truth does not back</h3>
            <ul className="space-y-1 text-xs">
              {score.unmatched_detections.map((row, index) => (
                <li key={index} className="rounded border bg-muted/40 px-2 py-1 font-mono">
                  {JSON.stringify(row)}
                </li>
              ))}
            </ul>
          </div>
        )}

        <p className="text-xs text-muted-foreground">
          Ground truth source: <span className="font-mono">{score.ground_truth_source}</span>
        </p>
      </CardContent>
    </Card>
  );
}

function Detections({ batchId }: { batchId: string }) {
  const state = useApi(() => api.rootCause.detections(batchId), [batchId]);
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">Detections and what followed</h2>
      <AsyncBoundary
        state={state}
        isEmpty={(data: CorridorDetectionRead[]) => data.length === 0}
        empty={
          <EmptyState
            title="No corridor degradations detected in this run"
            hint="That is a legitimate outcome — the minimum-volume guard refuses to call a degradation on too few attempts. Try a run with a different seed."
          />
        }
      >
        {(detections) => (
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Corridor</TableHead>
                    <TableHead>Observed vs baseline</TableHead>
                    <TableHead>Window</TableHead>
                    <TableHead>At risk</TableHead>
                    <TableHead>Diagnosis</TableHead>
                    <TableHead>Action &amp; rule</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {detections.map((detection) => (
                    <TableRow key={detection.detection_id}>
                      <TableCell>
                        <div className="font-medium">
                          {[detection.issuer, detection.method, detection.route_id]
                            .filter(Boolean)
                            .join(" × ") || detection.corridor_level}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {detection.corridor_level} · {detection.affected_attempts} affected ·{" "}
                          {detection.distinct_failing_customers} customers
                        </div>
                      </TableCell>
                      <TableCell>
                        <span className="text-at-risk">
                          {formatRate(detection.observed_success_rate, 1)}
                        </span>
                        <span className="text-muted-foreground"> vs </span>
                        <span>{formatRate(detection.baseline_success_rate, 1)}</span>
                        <div className="text-xs text-muted-foreground">
                          {detection.window_attempts} in window / {detection.baseline_attempts}{" "}
                          baseline · p={detection.p_value.toExponential(2)}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {formatDateTime(detection.window_start)}
                        <br />
                        {formatDateTime(detection.window_end)}
                      </TableCell>
                      <TableCell>
                        <Money paise={detection.value_at_risk_paise} tone="at-risk" />
                      </TableCell>
                      <TableCell className="space-y-1">
                        <Badge
                          variant={
                            detection.determination === "systemic"
                              ? "atRisk"
                              : detection.determination === "individual"
                                ? "reasoned"
                                : "muted"
                          }
                        >
                          {humanise(detection.determination)}
                        </Badge>
                        <div className="text-xs text-muted-foreground">
                          {humanise(detection.hypothesis)}
                        </div>
                        <ProvenanceBadge
                          provenance={detection.provenance}
                          confidence={detection.model_confidence}
                        />
                      </TableCell>
                      <TableCell className="space-y-1">
                        <Badge variant={detection.policy_allowed ? "recovery" : "atRisk"}>
                          {detection.policy_allowed ? "Permitted" : "Refused"}
                        </Badge>
                        <div className="text-xs">{humanise(detection.authorised_action)}</div>
                        <RuleBadge rule={detection.authorising_rule} />
                        {detection.overridden_recommendation && (
                          <div className="text-xs text-at-risk">
                            Overruled: {humanise(detection.overridden_recommendation)}
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        <Link
                          href={`/timelines/corridor/${encodeURIComponent(detection.detection_id)}`}
                          className="inline-flex items-center gap-1 text-sm font-medium hover:underline"
                        >
                          Timeline
                          <ArrowRight className="size-3.5" aria-hidden />
                        </Link>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}
      </AsyncBoundary>
    </section>
  );
}
