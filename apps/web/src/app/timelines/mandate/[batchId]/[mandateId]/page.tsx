"use client";

/**
 * Mandate timeline — one recurring charge, from failure to next permitted step.
 *
 * Everything on this page comes from one API call. The backend's
 * `GET /mandate-recovery/runs/{batch}/mandates/{id}` route was built to return
 * the narrative rather than three joins the caller assembles, and this page is
 * why: a story reconstructed from three endpoints is three chances for the demo
 * to show a half-loaded screen.
 *
 * The messages are shown in full and are labelled as never dispatched, because
 * "we drafted this and did not send it" is a different and more honest claim
 * than showing a message and letting a reader assume it went out.
 */

import type { MandateTimeline as MandateTimelineData } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AsyncBoundary } from "@/components/features/async-boundary";
import { Field, Money } from "@/components/features/metric-card";
import { PageHeader } from "@/components/features/app-shell";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { Timeline, TimelineLegend } from "@/components/features/timeline";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";

export default function MandateTimelinePage({
  params,
}: {
  // A plain object, not a promise — Next 14 passes route params synchronously.
  params: { batchId: string; mandateId: string };
}) {
  const { batchId, mandateId } = params;
  const state = useApi(
    () => api.mandateRecovery.timeline(decodeURIComponent(batchId), decodeURIComponent(mandateId)),
    [batchId, mandateId],
  );

  return (
    <>
      <PageHeader
        title="Mandate timeline"
        description="One recurring charge: what failed, how it was classified, which compliance gate spoke, what the model drafted, and what the system is permitted to do next."
      />
      <AsyncBoundary state={state} loadingRows={3}>
        {(data: MandateTimelineData) => (
          <div className="space-y-6">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {data.state.customer_name}{" "}
                  <span className="font-mono text-sm font-normal text-muted-foreground">
                    {data.mandate_id}
                  </span>
                </CardTitle>
                <p className="text-sm">{data.next_step_summary}</p>
              </CardHeader>
              <CardContent>
                <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <Field label="Amount">
                    <Money paise={data.state.amount_paise} />
                    <div className="text-xs text-muted-foreground">
                      {data.state.afa_side === "above"
                        ? "above the ₹15,000 AFA threshold"
                        : "at or below the AFA threshold"}
                    </div>
                  </Field>
                  <Field label="Mandate status">
                    <Badge variant={data.state.mandate_status === "active" ? "recovery" : "atRisk"}>
                      {humanise(data.state.mandate_status)}
                    </Badge>
                    <div className="text-xs text-muted-foreground">
                      AFA registered: {data.state.afa_registered ? "yes" : "no"}
                    </div>
                  </Field>
                  <Field label="Failure">
                    {data.state.decline_code ? (
                      <>
                        <span className="font-mono text-xs">{data.state.decline_code}</span>
                        <div>
                          <Badge variant="muted">{data.state.decline_class}</Badge>{" "}
                          <Badge variant="muted">{humanise(data.state.failure_route)}</Badge>
                        </div>
                      </>
                    ) : (
                      "no failure in the current cycle"
                    )}
                  </Field>
                  <Field label="Pre-debit notice">
                    <Badge variant={data.state.notice_status === "on_time" ? "recovery" : "atRisk"}>
                      {humanise(data.state.notice_status)}
                    </Badge>
                    <div className="text-xs text-muted-foreground">
                      {data.state.notice_lead_hours === null ||
                      data.state.notice_lead_hours === undefined
                        ? "never sent"
                        : `${data.state.notice_lead_hours.toFixed(1)}h lead`}
                    </div>
                  </Field>
                  <Field label="Next step">{humanise(data.state.next_step)}</Field>
                  <Field label="Scheduled for">
                    {data.state.scheduled_for ? formatDateTime(data.state.scheduled_for) : "—"}
                  </Field>
                  <Field label="Authorising rule">
                    <RuleBadge rule={data.state.authorising_rule} />
                  </Field>
                  <Field label="Attempts remaining">
                    {data.state.attempts_remaining ?? "—"}
                    {data.state.compliance_blocked && (
                      <div>
                        <Badge variant="atRisk">compliance block</Badge>
                      </div>
                    )}
                  </Field>
                </dl>
              </CardContent>
            </Card>

            {data.communications.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-lg font-semibold tracking-tight">
                  Messages drafted for this customer
                </h2>
                <p className="text-sm text-muted-foreground">
                  <span className="font-medium text-foreground">None were dispatched.</span> Nothing
                  in this project sends a message to a real person — the drafts are shown so the
                  tone gate&apos;s work is visible.
                </p>
                {data.communications.map((message) => (
                  <Card key={message.id}>
                    <CardContent className="space-y-2 p-4">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="muted">{humanise(message.kind)}</Badge>
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
                        <ProvenanceBadge
                          provenance={message.provenance}
                          confidence={message.model_confidence}
                        />
                      </div>
                      <p className="font-medium">{message.subject}</p>
                      <p className="whitespace-pre-wrap text-sm text-muted-foreground">
                        {message.body}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        Call to action: {humanise(message.call_to_action)}
                      </p>
                      {message.model_reasoning && (
                        <blockquote className="rounded-md border-l-2 border-reasoned bg-reasoned-muted/40 px-3 py-2 text-sm italic">
                          <span className="mb-1 block text-[11px] font-medium uppercase not-italic tracking-wide text-reasoned">
                            Model reasoning, verbatim
                          </span>
                          {message.model_reasoning}
                        </blockquote>
                      )}
                      {message.overridden_recommendation && (
                        <p className="text-sm text-at-risk">
                          The model recommended{" "}
                          <span className="font-medium">
                            {humanise(message.overridden_recommendation)}
                          </span>{" "}
                          and the policy engine refused it. The draft was replaced with the
                          deterministic template rather than the customer being left with nothing.
                        </p>
                      )}
                      {Array.isArray(message.tone_violations) &&
                        message.tone_violations.length > 0 && (
                          <p className="text-sm text-at-risk">
                            Tone gate rejections: {JSON.stringify(message.tone_violations)}
                          </p>
                        )}
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
