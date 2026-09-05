"use client";

/**
 * Engine 3 — B2B Receivables Chaser.
 *
 * §5.3 asks for the ranked worklist with score breakdowns, the promise register
 * with kept/broken/active statuses, extraction accuracy with its confusion
 * matrix, and messages suppressed by caps or quiet hours.
 *
 * The extraction score on this corpus is perfect, and a perfect score is a
 * finding rather than a result — so the accuracy panel leads with what the
 * corpus is (20 templates, held-out annotations) rather than with the number.
 */

import Link from "next/link";
import { ArrowRight, Info } from "lucide-react";

import type {
  ConfusionMatrix,
  InvoiceChaseStateRead,
  PromiseToPayRead,
  ReceivablesRunSummary,
} from "@vasooli/shared-types";

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
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDate, formatDateTime, humanise } from "@/lib/format";
import { formatRate } from "@/lib/money";

const PROMISE_VARIANT = {
  kept: "recovery",
  broken: "atRisk",
  active: "muted",
  superseded: "outline",
} as const;

export default function ReceivablesPage() {
  return (
    <EnginePage
      engine="receivables"
      runEngine="receivables"
      title="B2B receivables chaser"
      description="Ranks overdue invoices, chases them on a capped ladder that never harasses, reads free-text customer replies for promise-to-pay commitments, and escalates only broken promises. Reply understanding is the engine's genuinely AI-dependent component — with no provider at all, this engine recovers nothing, because no reading means no promise."
    >
      {(batchId) => (
        <>
          <RunMetrics batchId={batchId} />
          <Worklist batchId={batchId} />
          <Promises batchId={batchId} />
        </>
      )}
    </EnginePage>
  );
}

function RunMetrics({ batchId }: { batchId: string }) {
  const state = useApi(() => api.receivables.summary(batchId), [batchId]);
  return (
    <AsyncBoundary state={state} loadingRows={2}>
      {(summary: ReceivablesRunSummary) => (
        <div className="space-y-6">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard
              label="Invoices"
              value={summary.invoices_ingested}
              hint={`${summary.invoices_overdue} overdue · ${summary.invoices_worklisted} worklisted`}
            />
            <MetricCard
              label="Promises tracked"
              value={summary.promises_made}
              hint={`${summary.promises_kept} kept · ${summary.promises_broken} broken · ${summary.promises_active} active`}
            />
            <MetricCard
              label="Messages suppressed"
              value={summary.messages_suppressed}
              hint="Quiet hours, contact caps, dispute freezes — harassment avoided"
            />
            <MetricCard
              label="Escalations"
              value={summary.escalations}
              hint={`${summary.disputes_frozen} disputes frozen · ${summary.invoices_deprioritised} deprioritised on a live promise`}
            />
          </div>

          <Card>
            <CardContent className="p-4">
              <p className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>
                  <span className="font-medium text-foreground">
                    What &ldquo;recovered&rdquo; counts here:{" "}
                  </span>
                  {summary.recovery_definition}
                </span>
              </p>
            </CardContent>
          </Card>

          <Tabs defaultValue="extraction">
            <TabsList>
              <TabsTrigger value="extraction">Extraction accuracy</TabsTrigger>
              <TabsTrigger value="ageing">Ageing buckets</TabsTrigger>
              <TabsTrigger value="ladder">Ladder &amp; suppressions</TabsTrigger>
            </TabsList>

            <TabsContent value="extraction">
              <ExtractionPanel score={summary.extraction} abstentions={summary.abstentions} />
            </TabsContent>

            <TabsContent value="ageing">
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-base">Recovery by ageing bucket</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Older buckets recover worse. Reported per bucket rather than blended, so the
                    shape is visible rather than asserted.
                  </p>
                </CardHeader>
                <CardContent className="space-y-4">
                  <MoneyBarChart
                    data={Object.entries(summary.by_ageing_bucket ?? {}).map(([label, bucket]) => ({
                      label,
                      atRiskPaise: bucket.amount_outstanding_paise ?? 0,
                      recoveredPaise: bucket.amount_recovered_paise ?? 0,
                    }))}
                  />
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Bucket</TableHead>
                        <TableHead>Invoices</TableHead>
                        <TableHead>Outstanding</TableHead>
                        <TableHead>Recovered</TableHead>
                        <TableHead>Rate</TableHead>
                        <TableHead>Reminders</TableHead>
                        <TableHead>Escalations</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(summary.by_ageing_bucket ?? {}).map(([name, bucket]) => (
                        <TableRow key={name}>
                          <TableCell className="font-medium">{name}</TableCell>
                          <TableCell>{bucket.invoices ?? 0}</TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_outstanding_paise ?? 0} tone="at-risk" />
                          </TableCell>
                          <TableCell>
                            <Money paise={bucket.amount_recovered_paise ?? 0} tone="recovery" />
                          </TableCell>
                          <TableCell>{formatRate(bucket.recovery_rate ?? 0)}</TableCell>
                          <TableCell>{bucket.reminders_sent ?? 0}</TableCell>
                          <TableCell>{bucket.escalations ?? 0}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="ladder">
              <div className="grid gap-3 lg:grid-cols-2">
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">What the ladder decided</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      Adds up to the worklist exactly — the quickest way to see that no invoice fell
                      through a branch.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <CountBarChart
                      height={220}
                      data={countsToData(summary.chase_steps, humanise, (key) =>
                        key === "send_reminder"
                          ? "recovery"
                          : key === "escalate" || key === "freeze_dispute"
                            ? "at-risk"
                            : "neutral",
                      )}
                    />
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base">Which rule withheld a message</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      The direct measure of harassment avoided. Escalation triggers are shown beside
                      it — a permitted escalation is not a refusal.
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <CountBarChart
                      height={140}
                      data={countsToData(
                        summary.suppressed_by_rule,
                        (key) => key,
                        () => "at-risk",
                      )}
                    />
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(summary.escalations_by_trigger ?? {}).map(
                        ([trigger, count]) => (
                          <Badge key={trigger} variant="muted">
                            {humanise(trigger)} ×{count}
                          </Badge>
                        ),
                      )}
                    </div>
                  </CardContent>
                </Card>
              </div>
            </TabsContent>
          </Tabs>
        </div>
      )}
    </AsyncBoundary>
  );
}

function ExtractionPanel({
  score,
  abstentions,
}: {
  score: ReceivablesRunSummary["extraction"];
  abstentions: number;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Reply-understanding accuracy against held-out annotations
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          {score.scoring_note ||
            "Every rate below is computed over model-handled replies only; abstentions are reported separately and never folded in."}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Replies scored"
            value={`${score.replies_scored ?? 0} / ${score.replies_total ?? 0}`}
            hint={`${score.abstentions ?? 0} abstentions (${formatRate(score.abstention_rate ?? 0)})`}
          />
          <MetricCard
            label="Promise detection F1"
            value={formatRate(score.promise_detection?.f1 ?? 0)}
            hint={`precision ${formatRate(score.promise_detection?.precision ?? 0)} · recall ${formatRate(score.promise_detection?.recall ?? 0)}`}
          />
          <MetricCard
            label="Dates exact"
            value={`${score.date_accuracy?.exact ?? 0} / ${score.date_accuracy?.scored ?? 0}`}
            hint={`${score.date_accuracy?.spurious ?? 0} fabricated · ${score.date_accuracy?.missing ?? 0} missed`}
          />
          <MetricCard
            label="Run abstentions"
            value={abstentions}
            hint="Routed to human review rather than guessed at"
          />
        </div>

        <div className="grid gap-3 lg:grid-cols-3">
          <ConfusionCard title="Promise detected" matrix={score.promise_detection} />
          <ConfusionCard title="Dispute detected" matrix={score.dispute_detection} />
          <ConfusionCard title="Conditional detected" matrix={score.conditional_detection} />
        </div>

        {Object.keys(score.by_language ?? {}).length > 0 && (
          <div>
            <h3 className="mb-2 text-sm font-medium">By language</h3>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Language</TableHead>
                  <TableHead>Precision</TableHead>
                  <TableHead>Recall</TableHead>
                  <TableHead>F1</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(score.by_language ?? {}).map(([language, matrix]) => (
                  <TableRow key={language}>
                    <TableCell className="font-medium">{language}</TableCell>
                    <TableCell>{formatRate(matrix.precision ?? 0)}</TableCell>
                    <TableCell>{formatRate(matrix.recall ?? 0)}</TableCell>
                    <TableCell>{formatRate(matrix.f1 ?? 0)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {score.failures && score.failures.length > 0 && (
          <div>
            <h3 className="mb-2 text-sm font-medium">
              Every reply the engine got wrong — the interesting part
            </h3>
            <ul className="space-y-1 text-xs">
              {score.failures.map((failure, index) => (
                <li key={index} className="rounded border bg-muted/40 px-2 py-1 font-mono">
                  {JSON.stringify(failure)}
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

function ConfusionCard({ title, matrix }: { title: string; matrix?: ConfusionMatrix }) {
  const cells = matrix ?? {};
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <tbody>
            <tr>
              <td className="py-1 text-muted-foreground">True positives</td>
              <td className="py-1 text-right font-medium">{cells.true_positives ?? 0}</td>
            </tr>
            <tr>
              <td className="py-1 text-muted-foreground">False positives</td>
              <td className="py-1 text-right font-medium text-at-risk">
                {cells.false_positives ?? 0}
              </td>
            </tr>
            <tr>
              <td className="py-1 text-muted-foreground">False negatives</td>
              <td className="py-1 text-right font-medium text-at-risk">
                {cells.false_negatives ?? 0}
              </td>
            </tr>
            <tr>
              <td className="py-1 text-muted-foreground">True negatives</td>
              <td className="py-1 text-right font-medium">{cells.true_negatives ?? 0}</td>
            </tr>
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function Worklist({ batchId }: { batchId: string }) {
  const state = useApi(() => api.receivables.worklist(batchId, { limit: 100 }), [batchId]);
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">The ranked worklist</h2>
      <p className="text-sm text-muted-foreground">
        Highest priority first, with every term of the score visible. Deprioritised invoices stay on
        the list rather than being filtered out — &ldquo;we chose not to chase this, and here is the
        rule&rdquo; is the claim worth making, and filtering would make the decision invisible.
      </p>
      <AsyncBoundary
        state={state}
        isEmpty={(data: InvoiceChaseStateRead[]) => data.length === 0}
        empty={
          <EmptyState title="No invoices worklisted in this run" hint="Trigger a run above." />
        }
      >
        {(invoices) => <WorklistTable batchId={batchId} invoices={invoices} />}
      </AsyncBoundary>
    </section>
  );
}

function WorklistTable({
  batchId,
  invoices,
}: {
  batchId: string;
  invoices: InvoiceChaseStateRead[];
}) {
  const rows = useRowLimit(invoices);
  return (
    <Card id="worklist">
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>#</TableHead>
              <TableHead>Invoice</TableHead>
              <TableHead>Outstanding</TableHead>
              <TableHead>Ageing</TableHead>
              <TableHead>Score</TableHead>
              <TableHead>Reply read as</TableHead>
              <TableHead>Next step &amp; rule</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.visible.map((invoice) => (
              <TableRow key={invoice.invoice_id}>
                <TableCell className="text-muted-foreground">{invoice.priority_rank}</TableCell>
                <TableCell>
                  <div className="font-medium">{invoice.customer_name}</div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {invoice.invoice_id}
                  </div>
                </TableCell>
                <TableCell>
                  <Money paise={invoice.amount_paise - invoice.amount_paid_paise} tone="at-risk" />
                  {invoice.amount_recovered_paise > 0 && (
                    <div className="text-xs">
                      <Money paise={invoice.amount_recovered_paise} tone="recovery" /> back
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant="muted">{invoice.ageing_bucket}</Badge>
                  <div className="text-xs text-muted-foreground">
                    {invoice.days_overdue}d overdue
                  </div>
                </TableCell>
                <TableCell>
                  <ScoreBreakdown
                    score={invoice.priority_score}
                    breakdown={invoice.score_breakdown}
                  />
                  {invoice.deprioritised && <Badge variant="muted">deprioritised (PR2)</Badge>}
                </TableCell>
                <TableCell>
                  {invoice.reply_intent ? (
                    <>
                      <Badge variant={invoice.disputed ? "atRisk" : "reasoned"}>
                        {humanise(invoice.reply_intent)}
                      </Badge>
                      <div className="text-xs text-muted-foreground">
                        {invoice.reply_language}
                        {invoice.abstained ? " · abstained" : ""}
                      </div>
                    </>
                  ) : (
                    <span className="text-xs text-muted-foreground">no reply</span>
                  )}
                </TableCell>
                <TableCell>
                  <div className="font-medium">{humanise(invoice.next_step)}</div>
                  <RuleBadge rule={invoice.authorising_rule} />
                  {invoice.suppressed_rule && (
                    <div className="mt-1">
                      <Badge variant="atRisk">suppressed by {invoice.suppressed_rule}</Badge>
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <Link
                    href={`/timelines/invoice/${encodeURIComponent(batchId)}/${encodeURIComponent(invoice.invoice_id)}?from=%2Fengines%2Freceivables%23worklist&fromLabel=the+worklist`}
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
          noun="invoices"
        />
      </CardContent>
    </Card>
  );
}

/**
 * The score with its terms. The API ships every component's raw value, weight
 * and contribution, so this renders them rather than recomputing anything.
 */
function ScoreBreakdown({
  score,
  breakdown,
}: {
  score: number;
  breakdown: Record<string, unknown>;
}) {
  const components = Array.isArray(breakdown?.components)
    ? (breakdown.components as Array<Record<string, unknown>>)
    : [];
  return (
    <details className="group">
      <summary className="cursor-pointer list-none font-medium tabular-nums">
        {score.toFixed(3)}
        <span className="ml-1 text-xs text-muted-foreground group-open:hidden">terms</span>
      </summary>
      <dl className="mt-1 space-y-0.5 text-xs">
        {components.map((component, index) => (
          <div key={index} className="flex gap-2">
            <dt className="text-muted-foreground">{String(component.name)}</dt>
            <dd className="tabular-nums">
              {Number(component.contribution).toFixed(3)}
              <span className="text-muted-foreground"> (w {String(component.weight)})</span>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function Promises({ batchId }: { batchId: string }) {
  const state = useApi(() => api.receivables.promises(batchId), [batchId]);
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">The promise register</h2>
      <p className="text-sm text-muted-foreground">
        Every commitment the model read out of a customer&apos;s own words, and what became of it.
        The status is ruled arithmetic; the commitment it is a status about was read by a model —
        which is why each row carries the extraction&apos;s provenance rather than
        &ldquo;deterministic&rdquo;.
      </p>
      <AsyncBoundary
        state={state}
        isEmpty={(data: PromiseToPayRead[]) => data.length === 0}
        empty={
          <EmptyState
            title="No promises extracted in this run"
            hint="With no provider configured the engine abstains rather than guessing, so a deterministic-only run has an empty register by design."
          />
        }
      >
        {(promises) => <PromiseTable promises={promises} />}
      </AsyncBoundary>
    </section>
  );
}

function PromiseTable({ promises }: { promises: PromiseToPayRead[] }) {
  const rows = useRowLimit(promises, 15);
  return (
    <Card>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Invoice</TableHead>
              <TableHead>The customer&apos;s words</TableHead>
              <TableHead>Committed</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Rule</TableHead>
              <TableHead>Read by</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.visible.map((promise) => (
              <TableRow key={promise.id}>
                <TableCell className="font-mono text-xs">
                  {promise.invoice_id}
                  <div className="text-muted-foreground">{promise.customer_id}</div>
                </TableCell>
                <TableCell className="max-w-md">
                  <p className="text-sm italic">&ldquo;{promise.reply_text}&rdquo;</p>
                  <p className="text-xs text-muted-foreground">
                    {formatDateTime(promise.reply_received_at)}
                  </p>
                </TableCell>
                <TableCell>
                  {promise.committed_date ? formatDate(promise.committed_date) : "no date"}
                  {promise.committed_amount_paise !== null &&
                    promise.committed_amount_paise !== undefined && (
                      <div className="text-xs">
                        <Money paise={promise.committed_amount_paise} />
                      </div>
                    )}
                  {promise.conditional && <Badge variant="muted">conditional</Badge>}
                </TableCell>
                <TableCell>
                  <Badge
                    variant={
                      PROMISE_VARIANT[promise.status as keyof typeof PROMISE_VARIANT] ?? "muted"
                    }
                  >
                    {humanise(promise.status)}
                  </Badge>
                  {promise.review_at && (
                    <div className="text-xs text-muted-foreground">
                      review {formatDate(promise.review_at)}
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <RuleBadge rule={promise.status_rule} />
                  <div className="mt-1 max-w-xs text-xs text-muted-foreground">
                    {promise.status_rationale}
                  </div>
                </TableCell>
                <TableCell>
                  <ProvenanceBadge
                    provenance={promise.provenance}
                    confidence={promise.model_confidence}
                  />
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
          noun="promises"
        />
      </CardContent>
    </Card>
  );
}
