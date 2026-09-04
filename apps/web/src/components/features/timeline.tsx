"use client";

/**
 * The entity timeline. One component, used by all three timeline types.
 *
 * §5.4: this is the surface that carries the video, because a single case
 * unfolding chronologically is a story while a dashboard of aggregates is a
 * report. Every step shows, in order: what happened, what the system decided,
 * **which rule authorised it**, what the model reasoned (verbatim), what action
 * followed, and how it turned out.
 *
 * The one thing this component must get right is that rule-based and
 * LLM-reasoned steps are distinguishable *without explanation*. That is done
 * three ways at once — the rail colour, the icon, and the provenance badge — so
 * the distinction survives a compressed video and a projector.
 */

import { useState } from "react";
import {
  ArrowRightLeft,
  Ban,
  Brain,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleSlash,
  Clock,
  OctagonX,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

import type { AuditEntryRead } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Money } from "@/components/features/metric-card";
import { ProvenanceBadge, RuleBadge, decisionSource } from "@/components/features/provenance-badge";
import { formatDateTime, humanise } from "@/lib/format";
import { cn } from "@/lib/utils";

const OUTCOME_ICON = {
  success: CircleCheck,
  scheduled: Clock,
  pending: Clock,
  failure: TriangleAlert,
  blocked: Ban,
  halted: OctagonX,
  escalated: ArrowRightLeft,
  skipped: CircleSlash,
} as const;

type Outcome = keyof typeof OUTCOME_ICON;

/**
 * Outcome -> tone. Refusals read as at-risk red rather than as errors, because
 * a refusal is the system working; the colour marks "money did not move", not
 * "something broke".
 */
const OUTCOME_TONE: Record<Outcome, string> = {
  success: "text-recovery",
  scheduled: "text-muted-foreground",
  pending: "text-muted-foreground",
  failure: "text-at-risk",
  blocked: "text-at-risk",
  halted: "text-at-risk",
  escalated: "text-reasoned",
  skipped: "text-muted-foreground",
};

function outcomeOf(entry: AuditEntryRead): Outcome {
  return (entry.outcome in OUTCOME_ICON ? entry.outcome : "pending") as Outcome;
}

/** Reasoning the model produced, pulled off the entry's metadata. */
function reasoningOf(entry: AuditEntryRead): string | null {
  const metadata = entry.metadata ?? {};
  for (const key of ["model_reasoning", "reasoning", "diagnosis_reasoning"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function isDegraded(entry: AuditEntryRead): boolean {
  return (entry.metadata ?? {}).degraded === true;
}

export function TimelineStep({ entry, index }: { entry: AuditEntryRead; index: number }) {
  const [open, setOpen] = useState(false);
  const outcome = outcomeOf(entry);
  const OutcomeIcon = OUTCOME_ICON[outcome];
  const source = decisionSource(entry.provenance, isDegraded(entry));
  const reasoning = reasoningOf(entry);
  const metadata = entry.metadata ?? {};
  const extras = Object.entries(metadata).filter(
    ([key]) => !["model_reasoning", "reasoning", "diagnosis_reasoning"].includes(key),
  );

  return (
    <li className="relative pl-8">
      {/* The rail. Violet for a reasoned step, slate for a ruled one — the
          distinction is legible before a single word has been read. */}
      <span
        aria-hidden
        className={cn(
          "absolute left-[11px] top-6 h-[calc(100%-1rem)] w-px",
          source === "reasoned" ? "bg-reasoned/40" : "bg-border",
        )}
      />
      <span
        aria-hidden
        className={cn(
          "absolute left-0 top-3 flex size-6 items-center justify-center rounded-full border-2 bg-background",
          source === "reasoned" ? "border-reasoned text-reasoned" : "border-ruled/50 text-ruled",
        )}
      >
        {source === "reasoned" ? <Brain className="size-3" /> : <ShieldCheck className="size-3" />}
      </span>

      <Card className={cn(source === "reasoned" && "border-reasoned/30 bg-reasoned-muted/30")}>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">#{index + 1}</span>
            <span className="font-medium">{humanise(entry.action)}</span>
            <span className={cn("inline-flex items-center gap-1 text-sm", OUTCOME_TONE[outcome])}>
              <OutcomeIcon className="size-4" aria-hidden />
              {humanise(entry.outcome)}
            </span>
            <span className="ml-auto text-xs text-muted-foreground">
              {formatDateTime(entry.timestamp)}
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <RuleBadge rule={entry.authorising_rule} />
            <Badge variant="muted" className="font-mono">
              {entry.reason_code}
            </Badge>
            <ProvenanceBadge
              provenance={entry.provenance}
              degraded={isDegraded(entry)}
              confidence={entry.model_confidence}
            />
          </div>

          <p className="text-sm text-muted-foreground">{entry.rationale}</p>

          {reasoning && (
            <blockquote
              className={cn(
                "rounded-md border-l-2 border-reasoned bg-background/70 px-3 py-2 text-sm italic",
              )}
            >
              <span className="mb-1 block text-[11px] font-medium uppercase not-italic tracking-wide text-reasoned">
                Model reasoning, verbatim
              </span>
              {reasoning}
            </blockquote>
          )}

          {(entry.amount_at_risk_paise > 0 || entry.amount_recovered_paise > 0) && (
            <div className="flex flex-wrap gap-4 text-sm">
              {entry.amount_at_risk_paise > 0 && (
                <span>
                  <span className="text-muted-foreground">At risk </span>
                  <Money paise={entry.amount_at_risk_paise} tone="at-risk" />
                </span>
              )}
              {entry.amount_recovered_paise > 0 && (
                <span>
                  <span className="text-muted-foreground">Recovered </span>
                  <Money paise={entry.amount_recovered_paise} tone="recovery" />
                </span>
              )}
            </div>
          )}

          {extras.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                aria-expanded={open}
              >
                {open ? (
                  <ChevronDown className="size-3" aria-hidden />
                ) : (
                  <ChevronRight className="size-3" aria-hidden />
                )}
                {open ? "Hide" : "Show"} decision metadata ({extras.length})
              </button>
              {open && (
                <dl className="mt-2 grid gap-x-6 gap-y-1 sm:grid-cols-2">
                  {extras.map(([key, value]) => (
                    <div key={key} className="flex gap-2 text-xs">
                      <dt className="shrink-0 font-mono text-muted-foreground">{key}</dt>
                      <dd className="break-all font-mono">{renderValue(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </li>
  );
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function Timeline({ entries }: { entries: AuditEntryRead[] }) {
  return (
    <ol className="space-y-3">
      {entries.map((entry, index) => (
        <TimelineStep key={entry.id} entry={entry} index={index} />
      ))}
    </ol>
  );
}

/**
 * The legend. Small, and next to the timeline rather than in a help page —
 * §5.4 requires the distinction to be readable without explanation, and this is
 * the belt to that braces: someone who does not intuit the colours still has the
 * key in view without leaving the story.
 */
export function TimelineLegend() {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <span className="flex size-5 items-center justify-center rounded-full border-2 border-reasoned text-reasoned">
          <Brain className="size-2.5" aria-hidden />
        </span>
        An LLM produced this judgment
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="flex size-5 items-center justify-center rounded-full border-2 border-ruled/50 text-ruled">
          <ShieldCheck className="size-2.5" aria-hidden />
        </span>
        A policy rule decided it
      </span>
      <span>Every step names the rule that authorised it — refusals included.</span>
    </div>
  );
}
