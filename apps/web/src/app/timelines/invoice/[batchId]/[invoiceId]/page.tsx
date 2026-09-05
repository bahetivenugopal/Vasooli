"use client";

/**
 * Invoice timeline — the surface that carries the whole argument.
 *
 * This is the one page where the whole product argument is visible in a single
 * scroll: a reminder went out, the customer replied in their own words, a model
 * read a commitment out of that free text, a rule decided whether the commitment
 * held, and the gate decided what could happen next. The reply is shown verbatim
 * above the model's reading of it, so a viewer can check the extraction against
 * the text themselves rather than taking the score on trust.
 */

import type { InvoiceTimeline as InvoiceTimelineData } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AsyncBoundary } from "@/components/features/async-boundary";
import { Field, Money } from "@/components/features/metric-card";
import { BackLink, PageHeader } from "@/components/features/app-shell";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { Timeline, TimelineLegend } from "@/components/features/timeline";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDate, formatDateTime, humanise } from "@/lib/format";

const PROMISE_VARIANT = {
  kept: "recovery",
  broken: "atRisk",
  active: "muted",
  superseded: "outline",
} as const;

export default function InvoiceTimelinePage({
  params,
}: {
  // A plain object, not a promise — Next 14 passes route params synchronously.
  params: { batchId: string; invoiceId: string };
}) {
  const { batchId, invoiceId } = params;
  const state = useApi(
    () => api.receivables.timeline(decodeURIComponent(batchId), decodeURIComponent(invoiceId)),
    [batchId, invoiceId],
  );

  return (
    <>
      <BackLink fallbackHref="/engines/receivables" fallbackLabel="the worklist" />
      <PageHeader
        title="Invoice timeline"
        description="One overdue invoice as a story: what we sent, what the customer said back, what the model read in it, what became of the commitment, and the rule behind every step — including the ones that refused."
      />
      <AsyncBoundary state={state} loadingRows={3}>
        {(data: InvoiceTimelineData) => (
          <div className="space-y-6">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {data.state.customer_name}{" "}
                  <span className="font-mono text-sm font-normal text-muted-foreground">
                    {data.invoice_id}
                  </span>
                </CardTitle>
                <p className="text-sm">{data.next_step_summary}</p>
              </CardHeader>
              <CardContent>
                <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <Field label="Outstanding">
                    <Money
                      paise={data.state.amount_paise - data.state.amount_paid_paise}
                      tone="at-risk"
                    />
                    <div className="text-xs text-muted-foreground">
                      of <Money paise={data.state.amount_paise} /> invoiced
                    </div>
                  </Field>
                  <Field label="Ageing">
                    <Badge variant="muted">{data.state.ageing_bucket}</Badge>
                    <div className="text-xs text-muted-foreground">
                      {data.state.days_overdue}d overdue · due {formatDate(data.state.due_at)}
                    </div>
                  </Field>
                  <Field label="Ladder">
                    {data.state.rungs_used} of 3 rungs used
                    <div className="text-xs text-muted-foreground">
                      current: {data.state.current_rung ?? "—"}
                    </div>
                  </Field>
                  <Field label="Priority">
                    #{data.state.priority_rank} · {data.state.priority_score.toFixed(3)}
                    {data.state.deprioritised && (
                      <div>
                        <Badge variant="muted">deprioritised on a live promise</Badge>
                      </div>
                    )}
                  </Field>
                  <Field label="Next step">{humanise(data.state.next_step)}</Field>
                  <Field label="Scheduled for">
                    {data.state.scheduled_for ? formatDateTime(data.state.scheduled_for) : "—"}
                  </Field>
                  <Field label="Authorising rule">
                    <RuleBadge rule={data.state.authorising_rule} />
                  </Field>
                  <Field label="Escalation">
                    {data.state.escalated ? (
                      <Badge variant="atRisk">
                        {humanise(data.state.escalation_trigger ?? "escalated")}
                      </Badge>
                    ) : (
                      "—"
                    )}
                    {data.state.suppressed_rule && (
                      <div>
                        <Badge variant="atRisk">
                          message suppressed by {data.state.suppressed_rule}
                        </Badge>
                      </div>
                    )}
                  </Field>
                </dl>
              </CardContent>
            </Card>

            {data.replies.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-lg font-semibold tracking-tight">
                  What the customer actually said
                </h2>
                {data.replies.map((reply, index) => (
                  <Card key={index} className="border-reasoned/30 bg-reasoned-muted/30">
                    <CardContent className="space-y-2 p-4">
                      <p className="text-xs text-muted-foreground">
                        {formatDateTime(String(reply.received_at ?? ""))} ·{" "}
                        <span className="font-mono">{String(reply.reply_id ?? "")}</span>
                      </p>
                      <blockquote className="border-l-2 border-reasoned pl-3 text-base italic">
                        &ldquo;{String(reply.text ?? "")}&rdquo;
                      </blockquote>
                      <p className="text-sm">
                        <span className="text-muted-foreground">The model read this as </span>
                        <Badge variant="reasoned">{humanise(String(reply.read_as ?? ""))}</Badge>
                        {reply.abstained === true && (
                          <>
                            {" "}
                            <Badge variant="atRisk">
                              abstained — routed to a human under policy-bounds:HE1
                            </Badge>
                          </>
                        )}
                      </p>
                    </CardContent>
                  </Card>
                ))}
              </section>
            )}

            {data.promises.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-lg font-semibold tracking-tight">
                  The commitment, and what became of it
                </h2>
                {data.promises.map((promise) => (
                  <Card key={promise.id}>
                    <CardContent className="space-y-2 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge
                          variant={
                            PROMISE_VARIANT[promise.status as keyof typeof PROMISE_VARIANT] ??
                            "muted"
                          }
                        >
                          {humanise(promise.status)}
                        </Badge>
                        <span className="text-sm">
                          {promise.committed_date
                            ? `committed to ${formatDate(promise.committed_date)}`
                            : "no date could be extracted"}
                        </span>
                        {promise.conditional && <Badge variant="muted">conditional</Badge>}
                        <RuleBadge rule={promise.status_rule} />
                        <ProvenanceBadge
                          provenance={promise.provenance}
                          confidence={promise.model_confidence}
                        />
                      </div>
                      <p className="text-sm text-muted-foreground">{promise.status_rationale}</p>
                      {promise.condition_detail && (
                        <p className="text-sm text-muted-foreground">
                          Condition: {promise.condition_detail}
                        </p>
                      )}
                      {promise.model_reasoning && (
                        <blockquote className="rounded-md border-l-2 border-reasoned bg-reasoned-muted/40 px-3 py-2 text-sm italic">
                          <span className="mb-1 block text-[11px] font-medium uppercase not-italic tracking-wide text-reasoned">
                            Model reasoning, verbatim
                          </span>
                          {promise.model_reasoning}
                        </blockquote>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </section>
            )}

            {data.communications.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-lg font-semibold tracking-tight">Reminders drafted</h2>
                <p className="text-sm text-muted-foreground">
                  <span className="font-medium text-foreground">None were dispatched.</span> Each
                  rung has its own tone, and every draft passes the same TN1/TN2 gate a model&apos;s
                  output would.
                </p>
                {data.communications.map((message) => (
                  <Card key={message.id}>
                    <CardContent className="space-y-2 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="muted">
                          rung {message.rung_number} · {message.rung}
                        </Badge>
                        <Badge variant="muted">{message.channel}</Badge>
                        <Badge
                          variant={message.status === "marked_for_delivery" ? "recovery" : "atRisk"}
                        >
                          {humanise(message.status)}
                        </Badge>
                        <RuleBadge rule={message.authorising_rule} />
                        {message.held_rule && (
                          <Badge variant="atRisk">held by {message.held_rule}</Badge>
                        )}
                        <ProvenanceBadge provenance={message.provenance} />
                      </div>
                      <p className="font-medium">{message.subject}</p>
                      <p className="whitespace-pre-wrap text-sm text-muted-foreground">
                        {message.body}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        Call to action: {humanise(message.call_to_action)}
                        {message.payment_link_url ? ` · ${message.payment_link_url}` : ""}
                      </p>
                    </CardContent>
                  </Card>
                ))}
              </section>
            )}

            <section className="space-y-3">
              <h2 className="text-lg font-semibold tracking-tight">Every decision, in order</h2>
              <TimelineLegend />
              <Timeline entries={data.entries} />
            </section>
          </div>
        )}
      </AsyncBoundary>
    </>
  );
}
