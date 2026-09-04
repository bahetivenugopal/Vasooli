/**
 * The provenance badge, and the rule badge. Defined once, used on every surface.
 *
 * These two components carry the product's central claim: *the model supplies
 * judgment, the policy engine supplies permission.* A judge scrolling a timeline
 * has to be able to tell, without being told, which steps a model reasoned and
 * which a rule decided — so the distinction is colour, shape and wording, not a
 * legend somewhere off screen.
 *
 * Three states, deliberately distinct rather than two:
 *
 * - **reasoned** — an LLM produced this. Names the model, and says when it came
 *   from cache.
 * - **fallback** — a reasoning task *degraded* to its registered deterministic
 *   fallback. Distinct from a plain rule, because "the model was asked and could
 *   not answer" is a different fact from "no model was involved".
 * - **ruled** — a rule decided it. No model was ever consulted.
 */

import { BookCheck, Brain, DatabaseZap, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { splitCitation } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The provenance object as the API serves it.
 *
 * Written structurally rather than imported from the generated types because
 * three of the read models store provenance in a JSON column, so the generated
 * type for those is `Record<string, unknown>`. Narrowing here keeps every call
 * site working off the same shape. (Noted for Phase 7: typing those columns as
 * `Provenance` on the Python side would remove the need for this.)
 */
export interface ProvenanceLike {
  source?: unknown;
  provider?: unknown;
  model?: unknown;
  cache_hit?: unknown;
  prompt_version?: unknown;
  abstained?: unknown;
  latency_ms?: unknown;
}

export interface ReadProvenance {
  source: "model" | "deterministic";
  provider: string;
  model: string | null;
  cacheHit: boolean;
  promptVersion: string | null;
  abstained: boolean;
  latencyMs: number | null;
}

export function readProvenance(raw: ProvenanceLike | null | undefined): ReadProvenance {
  return {
    source: raw?.source === "model" ? "model" : "deterministic",
    provider: typeof raw?.provider === "string" ? raw.provider : "deterministic",
    model: typeof raw?.model === "string" ? raw.model : null,
    cacheHit: raw?.cache_hit === true,
    promptVersion: typeof raw?.prompt_version === "string" ? raw.prompt_version : null,
    abstained: raw?.abstained === true,
    latencyMs: typeof raw?.latency_ms === "number" ? raw.latency_ms : null,
  };
}

/** Was this decision produced by a model, a degraded fallback, or a rule? */
export type DecisionSource = "reasoned" | "fallback" | "ruled";

/**
 * `degraded` comes from the audit entry's metadata, not from provenance: a task
 * that fell back records `deterministic` provenance *and* `metadata.degraded`,
 * and only the pair distinguishes "the model could not answer" from "no model
 * was ever involved here".
 */
export function decisionSource(
  provenance: ProvenanceLike | null | undefined,
  degraded = false,
): DecisionSource {
  const read = readProvenance(provenance);
  if (read.source === "model") return "reasoned";
  return degraded ? "fallback" : "ruled";
}

const SOURCE_LABEL: Record<DecisionSource, string> = {
  reasoned: "LLM reasoned",
  fallback: "Fallback",
  ruled: "Rule",
};

export function ProvenanceBadge({
  provenance,
  degraded = false,
  confidence,
  className,
}: {
  provenance: ProvenanceLike | null | undefined;
  degraded?: boolean;
  confidence?: number | null;
  className?: string;
}) {
  const read = readProvenance(provenance);
  const source = decisionSource(provenance, degraded);
  const Icon = source === "reasoned" ? Brain : source === "fallback" ? DatabaseZap : ShieldCheck;

  const details: string[] = [];
  if (source === "reasoned" && read.model) details.push(read.model);
  if (source === "reasoned" && read.cacheHit) details.push("cached");
  if (source === "fallback") details.push("deterministic");
  if (read.abstained) details.push("abstained");
  if (typeof confidence === "number") details.push(`conf ${confidence.toFixed(2)}`);

  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1", className)}>
      <Badge variant={source === "reasoned" ? "reasoned" : "ruled"}>
        <Icon className="size-3" aria-hidden />
        {SOURCE_LABEL[source]}
      </Badge>
      {details.map((detail) => (
        <Badge key={detail} variant="muted" className="font-mono">
          {detail}
        </Badge>
      ))}
    </span>
  );
}

/**
 * The authorising rule, split into its skill and its id.
 *
 * Every audit entry cites one — refusals especially — so this badge appears on
 * every row of every surface. Showing the skill separately makes it visible at a
 * glance whether a bound came from regulation (`rbi-mandate-rules`), from the
 * decline taxonomy, or from a product decision (`policy-bounds`).
 */
export function RuleBadge({
  rule,
  className,
}: {
  /**
   * Nullable because a few read models carry a rule that is only set once a
   * decision was reached. An entry in the audit trail always has one — the
   * writer rejects a citation-free entry — but a detection row can be read
   * before anything was authorised, and rendering "null" there would be worse
   * than saying nothing.
   */
  rule: string | null | undefined;
  className?: string;
}) {
  if (!rule) {
    return (
      <Badge variant="muted" className={cn(className)}>
        no rule cited yet
      </Badge>
    );
  }
  const parts = splitCitation(rule);
  if (!parts) {
    return (
      <Badge variant="outline" className={cn("font-mono", className)} title={rule}>
        <BookCheck className="size-3" aria-hidden />
        {rule}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className={cn("font-mono", className)} title={rule}>
      <BookCheck className="size-3" aria-hidden />
      <span className="text-muted-foreground">{parts.skill}</span>
      <span aria-hidden>:</span>
      <span className="font-semibold">{parts.id}</span>
    </Badge>
  );
}
