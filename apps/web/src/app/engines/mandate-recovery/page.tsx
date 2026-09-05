"use client";

/**
 * Engine 2 — Mandate & Subscription Recovery.
 *
 * §5.3 asks for recovery split by failure class, the above/below AFA threshold
 * breakdown, retries correctly suppressed with wasted attempts avoided, mandates
 * stopped at cap, and the upcoming retry schedule showing what will happen next
 * and which rule permits it.
 *
 * Two things this view is careful about:
 *
 * - **Both denominators, never one.** The blended recovery rate is low because
 *   most of the book sits above the AFA threshold or behind a hard stop, where
 *   no compliant system may auto-debit at all. The addressable rate is shown
 *   beside it with its definition, not instead of it.
 * - **The compliance blocks are the headline, not the footnote.** They are the
 *   clearest evidence in the project that the gate is structural.
 */

import Link from "next/link";
import { ArrowRight } from "lucide-react";

import type { MandateRecoveryStateRead, MandateRunSummary } from "@vasooli/shared-types";

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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AsyncBoundary, EmptyState } from "@/components/features/async-boundary";
import { CountBarChart, MoneyBarChart, countsToData } from "@/components/features/charts";
import { EnginePage } from "@/components/features/engine-page";
import { RowLimitFooter, useRowLimit } from "@/components/features/row-limit";
import { MetricCard, Money } from "@/components/features/metric-card";
import { RuleBadge } from "@/components/features/provenance-badge";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";
import { formatRate } from "@/lib/money";

export default function MandateRecoveryPage() {
  return (
    <EnginePage
      engine="mandate_recovery"
      runEngine="mandate-recovery"
      title="Mandate & subscription recovery"
      description="Classifies every failed recurring charge soft versus hard, retries soft declines inside RBI's actual compliance windows, and routes hard declines straight to dunning instead of burning retry attempts. The compliance gates are the point: an attempt refused is an attempt a blind retrier would have made."
    >
      {(batchId) => (
        <>
          <RunMetrics batchId={batchId} />
          <Mandates batchId={batchId} />
        </>
      )}
    </EnginePage>
  );
}

function RunMetrics({ batchId }: { batchId: string }) {
  const state = useApi(() => api.mandateRecovery.summary(batchId), [batchId]);
  return (
    <AsyncBoundary state={state} loadingRows={2}>
      {(summary: MandateRunSummary) => (
        <div className="space-y-6">
          <Card className="border-recovery/30">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Two denominators, both reported</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricCard
                  label="Blended recovery rate"
                  value={formatRate(summary.recovery_rate)}
                  hint={
                    <>
                      <Money paise={summary.amount_recovered_paise} tone="recovery" compact /> of{" "}
                      <Money paise={summary.amount_at_risk_paise} tone="at-risk" compact /> at risk
                    </>
                  }
                />
                <MetricCard
                  label="Addressable recovery rate"
                  value={formatRate(summary.addressable_recovery_rate)}
                  tone="recovery"
                  hint={
                    <>
                      of <Money paise={summary.amount_addressable_paise} compact /> the engine was
                      permitted to debit
                    </>
                  }
                />
                <MetricCard
                  label="Compliance-blocked attempts"
                  value={summary.compliance_blocked}
                  tone="at-risk"
                  hint={`across ${Object.keys(summary.compliance_blocked_by_rule ?? {}).length} distinct rules`}
                />
              </div>
              <p className="text-xs leading-relaxed text-muted-foreground">
                <span className="font-medium text-foreground">Addressable means: </span>
                {summary.addressable_definition}
              </p>
            </CardContent>
          </Card>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard
              label="Retries suppressed"
              value={summary.retries_suppressed}
              hint="Hard and terminal declines a blind system would have retried"
            />
            <MetricCard
              label="Wasted attempts avoided"
              value={summary.wasted_attempts_avoided}
              hint={summary.wasted_attempts_baseline}
            />
            <MetricCard
              label="Mandates at the attempt cap"
              value={summary.mandates_at_attempt_cap}
              hint={`${summary.mandates_halted} halted outright`}
            />
            <MetricCard
              label="Messages drafted / held"
              value={`${summary.communications_drafted} / ${summary.communications_held}`}
              hint={`${summary.tone_rejections} drafts rejected by the tone gate. None were sent.`}
            />
          </div>

          <Tabs defaultValue="failure-class">
            <TabsList>
              <TabsTrigger value="failure-class">By failure class</TabsTrigger>
              <TabsTrigger value="afa">AFA threshold</TabsTrigger>
              <TabsTrigger value="compliance">Compliance blocks</TabsTrigger>
              <TabsTrigger value="notices">Pre-debit notices</TabsTrigger>
            </TabsList>

            <TabsContent value="failure-class">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Recovery by failure class</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Where the story lives. Soft declines recover at a completely different rate from
                    hard ones, and a single blended figure hides the fact that makes this engine
                    worth building.
                  </p>
                </CardHeader>
                <CardContent className="space-y-4">
                  <MoneyBarChart
                    data={Object.entries(summary.by_failure_class ?? {}).map(([label, bucket]) => ({
                      label,
                      atRiskPaise: bucket.amount_at_risk_paise ?? 0,
                      recoveredPaise: bucket.amount_recovered_paise ?? 0,
                    }))}
                  />
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Class</TableHead>
                        <TableHead>Mandates</TableHead>
                        <TableHead>At risk</TableHead>
                        <TableHead>Recovered</TableHead>
                        <TableHead>Rate</TableHead>
                        <TableHead>Debits</TableHead>
                        <TableHead>Suppressed</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(summary.by_failure_class ?? {}).map(([name, bucket]) => (
                        <TableRow key={name}>
                          <TableCell className="font-medium">{name}</TableCell>
                          <TableCell>{bucket.mandates ?? 0}</TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_at_risk_paise ?? 0} tone="at-risk" />
                          </TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_recovered_paise ?? 0} tone="recovery" />
                          </TableCell>
                          <TableCell>{formatRate(bucket.recovery_rate ?? 0)}</TableCell>
                          <TableCell>
                            {bucket.debits_recovered ?? 0} / {bucket.debits_attempted ?? 0}
                          </TableCell>
                          <TableCell>{bucket.retries_suppressed ?? 0}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="afa">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">
                    Above and below the ₹15,000 AFA threshold
                  </CardTitle>
                  <p className="text-sm text-muted-foreground">
                    A regulatory branch, not a product one (`rbi-mandate-rules:A3`). Above the
                    threshold a debit needs fresh authentication, so no compliant system can simply
                    retry it — showing both sides is what stops the blended rate reading as failure.
                  </p>
                </CardHeader>
                <CardContent>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Side</TableHead>
                        <TableHead>Mandates</TableHead>
                        <TableHead>At risk</TableHead>
                        <TableHead>Recovered</TableHead>
                        <TableHead>Debits attempted</TableHead>
                        <TableHead>Auth requests</TableHead>
                        <TableHead>Blocked for fresh AFA</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(summary.afa_branch ?? {}).map(([name, bucket]) => (
                        <TableRow key={name}>
                          <TableCell className="font-medium">{humanise(name)}</TableCell>
                          <TableCell>{bucket.mandates ?? 0}</TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_at_risk_paise ?? 0} tone="at-risk" />
                          </TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_recovered_paise ?? 0} tone="recovery" />
                          </TableCell>
                          <TableCell>{bucket.debits_attempted ?? 0}</TableCell>
                          <TableCell>{bucket.authentication_requests ?? 0}</TableCell>
                          <TableCell>{bucket.blocked_for_fresh_afa ?? 0}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="compliance">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Which compliance rule refused</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Every bar is an auto-debit the engine declined to attempt because a precondition
                    was not met. Non-zero is the point.
                  </p>
                </CardHeader>
                <CardContent>
                  <CountBarChart
                    height={200}
                    data={countsToData(
                      summary.compliance_blocked_by_rule,
                      (key) => key,
                      () => "at-risk",
                    )}
                  />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="notices">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Pre-debit notice status</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Derived from the book&apos;s own timestamps against the A2 window. The engine
                    never reads the generator&apos;s labels — it reaches the same conclusion
                    unaided, which is what makes this a measurement.
                  </p>
                </CardHeader>
                <CardContent className="space-y-3">
                  <CountBarChart
                    height={180}
                    data={countsToData(summary.notice_status_counts, humanise, (key) =>
                      key === "on_time" ? "recovery" : "at-risk",
                    )}
                  />
                  <p className="text-sm text-muted-foreground">
                    {summary.notices_sent} notices marked for delivery · {summary.notices_scheduled}{" "}
                    scheduled · {summary.notices_held} held outside the outreach window.
                  </p>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      )}
    </AsyncBoundary>
  );
}

function Mandates({ batchId }: { batchId: string }) {
  const state = useApi(() => api.mandateRecovery.mandates(batchId), [batchId]);
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">
        The retry schedule — what happens next, and which rule permits it
      </h2>
      <p className="text-sm text-muted-foreground">
        Every mandate in the run with its decided next step and the citation behind it. A refusal
        names the rule that refused just as specifically as a permission does.
      </p>
      <AsyncBoundary
        state={state}
        isEmpty={(data: MandateRecoveryStateRead[]) => data.length === 0}
        empty={<EmptyState title="No mandates in this run" hint="Trigger a run above." />}
      >
        {(mandates) => <MandateTable batchId={batchId} mandates={mandates} />}
      </AsyncBoundary>
    </section>
  );
}

function MandateTable({
  batchId,
  mandates,
}: {
  batchId: string;
  mandates: MandateRecoveryStateRead[];
}) {
  const rows = useRowLimit(mandates);
  return (
    <Card id="retry-schedule">
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Mandate</TableHead>
              <TableHead>Amount</TableHead>
              <TableHead>Failure</TableHead>
              <TableHead>Notice</TableHead>
              <TableHead>Next step</TableHead>
              <TableHead>When</TableHead>
              <TableHead>Rule</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.visible.map((mandate) => (
              <TableRow key={mandate.mandate_id}>
                <TableCell>
                  <div className="font-medium">{mandate.customer_name}</div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {mandate.mandate_id} · {mandate.mandate_status}
                  </div>
                </TableCell>
                <TableCell>
                  <Money paise={mandate.amount_paise} />
                  <div className="text-xs text-muted-foreground">
                    {mandate.afa_side === "above" ? "above AFA" : "at/below AFA"}
                  </div>
                </TableCell>
                <TableCell>
                  {mandate.decline_code ? (
                    <>
                      <div className="font-mono text-xs">{mandate.decline_code}</div>
                      <Badge variant="muted">{humanise(mandate.failure_route)}</Badge>
                    </>
                  ) : (
                    <span className="text-xs text-muted-foreground">no failure</span>
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant={mandate.notice_status === "on_time" ? "recovery" : "atRisk"}>
                    {humanise(mandate.notice_status)}
                  </Badge>
                </TableCell>
                <TableCell>
                  <div className="font-medium">{humanise(mandate.next_step)}</div>
                  {mandate.compliance_blocked && <Badge variant="atRisk">compliance block</Badge>}
                  {mandate.terminal && <Badge variant="muted">terminal</Badge>}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {mandate.scheduled_for ? formatDateTime(mandate.scheduled_for) : "—"}
                  <div>
                    {mandate.attempts_remaining === null
                      ? ""
                      : `${mandate.attempts_remaining} attempts left`}
                  </div>
                </TableCell>
                <TableCell>
                  <RuleBadge rule={mandate.authorising_rule} />
                  <div className="mt-1 max-w-xs text-xs text-muted-foreground">
                    {mandate.explanation}
                  </div>
                </TableCell>
                <TableCell>
                  <Link
                    href={`/timelines/mandate/${encodeURIComponent(batchId)}/${encodeURIComponent(mandate.mandate_id)}?from=%2Fengines%2Fmandate-recovery%23retry-schedule&fromLabel=the+retry+schedule`}
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
        <RowLimitFooter
          expanded={rows.expanded}
          hidden={rows.hidden}
          total={rows.total}
          onToggle={rows.toggle}
          noun="mandates"
        />
      </CardContent>
    </Card>
  );
}
