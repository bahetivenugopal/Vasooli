/**
 * Generated from the Vasooli API's OpenAPI schema. Do not edit by hand.
 *
 *     python scripts/generate_api_types.py
 *
 * Every type here is the shape the backend actually serialises. Hand-editing one
 * makes the dashboard compile against a promise the API never made.
 */

/**
 * What was done, or refused. `audit-schema` -> `action` values.
 *
 * `block_attempt` and `halt_schedule` are the compliance-evidence actions: a
 * refusal recorded with the rule that refused it is the proof that the
 * stopping rules are real, not decorative.
 */
export type Action =
  | "classify_decline"
  | "diagnose_root_cause"
  | "schedule_retry"
  | "attempt_charge"
  | "send_pre_debit_notice"
  | "reroute_traffic"
  | "send_dunning"
  | "send_reminder"
  | "record_promise_to_pay"
  | "escalate"
  | "block_attempt"
  | "halt_schedule"
  | "api_call";

/**
 * Both sides of the A3 threshold, so the branch is visible not asserted.
 */
export interface AfaBranchBreakdown {
  mandates?: number;
  amount_at_risk_paise?: number;
  amount_recovered_paise?: number;
  debits_attempted?: number;
  authentication_requests?: number;
  blocked_for_fresh_afa?: number;
}

/**
 * Recovery per ageing bucket. Older buckets recover worse; say so.
 */
export interface AgeingBreakdown {
  invoices?: number;
  amount_outstanding_paise?: number;
  amount_recovered_paise?: number;
  recovery_rate?: number;
  reminders_sent?: number;
  escalations?: number;
}

/**
 * Read shape for the API and the dashboard.
 *
 * `provenance` is re-validated on the way out, so a hand-edited row cannot
 * present itself as something the provenance contract would have rejected.
 */
export interface AuditEntryRead {
  id: number;
  timestamp: string;
  batch_id: string;
  engine: Engine;
  entity_type: EntityType;
  entity_id: string;
  action: Action;
  outcome: Outcome;
  reason_code: string;
  authorising_rule: string;
  rationale: string;
  provenance: Provenance;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  currency: string;
  attempt_number?: number | null;
  attempts_remaining?: number | null;
  model_confidence?: number | null;
  metadata?: Record<string, unknown> | null;
}

export interface BatchRunRead {
  batch_id: string;
  engine: Engine;
  seed: number;
  status: BatchStatus;
  started_at: string;
  completed_at?: string | null;
  summary?: Record<string, unknown> | null;
  notes?: Record<string, unknown> | null;
}

/**
 * Lifecycle of one reproducible batch run.
 */
export type BatchStatus = "running" | "completed" | "failed";

/**
 * The headline number, computed honestly from the log.
 *
 * Never from a separately maintained counter — a counter can drift from the
 * trail it claims to summarise, and then the demo's headline figure and its
 * evidence disagree.
 */
export interface BatchSummary {
  batch_id: string;
  entries: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  action_counts: Record<string, number>;
  outcome_counts: Record<string, number>;
  engine_counts: Record<string, number>;
  by_source: Record<string, SourceBreakdown>;
  policy_violations: number;
  blocked_count: number;
  halted_count: number;
  escalated_count: number;
  abstained_count: number;
}

/**
 * A 2x2 over one binary extraction claim, plus the derived rates.
 *
 * Reported in full rather than as headline accuracy: precision and recall move
 * in opposite directions here, and which one matters depends on the claim —
 * a false promise suppresses a legitimate chase, a missed promise chases
 * someone who was cooperating.
 */
export interface ConfusionMatrix {
  true_positives?: number;
  false_positives?: number;
  true_negatives?: number;
  false_negatives?: number;
  precision?: number;
  recall?: number;
  f1?: number;
  accuracy?: number;
}

export interface CorridorDetectionRead {
  detection_id: string;
  batch_id: string;
  corridor_key: string;
  corridor_level: string;
  issuer: string | null;
  method: string | null;
  route_id: string | null;
  window_start: string;
  window_end: string;
  window_attempts: number;
  baseline_attempts: number;
  observed_success_rate: number;
  baseline_success_rate: number;
  p_value: number;
  confidence: number;
  affected_attempts: number;
  value_at_risk_paise: number;
  decline_mix: Record<string, unknown>;
  distinct_failing_customers: number;
  hypothesis: string | null;
  determination: string | null;
  recommended_action: string | null;
  diagnosis_reasoning: string | null;
  model_confidence: number | null;
  provenance: Record<string, unknown> | null;
  authorised_action: string | null;
  authorising_rule: string | null;
  policy_allowed: boolean | null;
  overridden_recommendation: string | null;
}

export interface CorridorRerouteRead {
  id: number;
  batch_id: string;
  detection_id: string;
  corridor_key: string;
  method: string;
  from_route_id: string | null;
  to_route_id: string;
  effective_from: string;
  expires_at: string;
  authorising_rule: string;
  expiry_rule: string;
  value_at_risk_paise: number;
}

/**
 * How often this customer's commitments hold.
 *
 * Feeds prioritisation on the next run and is the engine's one visible piece of
 * learning-from-behaviour. Reported as `None` rather than as a default when a
 * customer has made no resolved promise: a customer with no history is not the
 * same as a customer with a bad one, and a default would fabricate the
 * difference.
 */
export interface CustomerReliability {
  customer_id: string;
  promises_made?: number;
  kept?: number;
  broken?: number;
  reliability?: number | null;
}

/**
 * How well extracted dates match the ground truth, split by difficulty.
 *
 * `explicit` dates are stated in the text in one of several everyday formats;
 * `inferable` ones need an anchor ("end of the month") resolved against the
 * reply's own timestamp. Scoring them together would hide which half the engine
 * is actually good at.
 */
export interface DateAccuracy {
  scored?: number;
  exact?: number;
  within_one_day?: number;
  wrong?: number;
  missing?: number;
  spurious?: number;
  exact_rate?: number;
  by_difficulty?: Record<string, Record<string, number>>;
}

/**
 * Detection performance against the generator's ground truth.
 *
 * Computed by `scoring.py`, which is evaluation code and may read the manifest.
 * The detector may not, and does not.
 */
export interface DetectionScore {
  true_positives: number;
  false_positives: number;
  false_negatives: number;
  precision: number;
  recall: number;
  detection_latency_hours: Record<string, number>;
  matched: Array<Record<string, unknown>>;
  unmatched_detections: Array<Record<string, unknown>>;
  missed_degradations: Array<string>;
  decoy_false_positives: number;
  ground_truth_source: string;
}

/**
 * Which engine acted. Mirrors `apps/api/app/engines/<name>/`.
 */
export type Engine = "root_cause" | "mandate_recovery" | "receivables" | "core";

/**
 * One engine's share of the headline, and the run it came from.
 *
 * Carries the run's `batch_id` and `seed` because a reported number that cannot
 * be traced back to a reproducible run is not evidence.
 */
export interface EngineContribution {
  engine: Engine;
  label: string;
  batch_id: string;
  seed: number;
  status: BatchStatus;
  started_at: string;
  completed_at?: string | null;
  dataset_batch_id?: string | null;
  entries: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  by_source: Record<string, SourceBreakdown>;
  recovery_definition: string;
}

/**
 * What was acted on. `audit-schema` -> Required fields.
 */
export type EntityType =
  "payment" | "mandate" | "invoice" | "corridor" | "batch";

/**
 * Extraction accuracy against Phase 2's held-out annotations.
 *
 * Every rate here is computed **over model-handled replies only**, with
 * abstentions reported separately and never folded in. Blending them would
 * imply the model answered when it explicitly did not, which is the one thing
 * an abstention path must never be allowed to do to a metric.
 */
export interface ExtractionScore {
  ground_truth_source: string;
  replies_total?: number;
  replies_scored?: number;
  abstentions?: number;
  abstention_rate?: number;
  promise_detection?: ConfusionMatrix;
  dispute_detection?: ConfusionMatrix;
  conditional_detection?: ConfusionMatrix;
  date_accuracy?: DateAccuracy;
  intent_confusion?: Record<string, Record<string, number>>;
  by_language?: Record<string, ConfusionMatrix>;
  failures?: Array<Record<string, unknown>>;
  scoring_note?: string;
}

/**
 * Recovery, per failure class. Where the story lives.
 *
 * Soft declines recover at a very different rate from hard ones, and a single
 * blended rate hides exactly the fact that makes the engine worth building.
 */
export interface FailureClassBreakdown {
  mandates?: number;
  amount_at_risk_paise?: number;
  amount_recovered_paise?: number;
  recovery_rate?: number;
  debits_attempted?: number;
  debits_recovered?: number;
  retries_suppressed?: number;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  environment: string;
}

export interface HTTPValidationError {
  detail?: Array<ValidationError>;
}

export interface InvoiceChaseStateRead {
  batch_id: string;
  invoice_id: string;
  customer_id: string;
  customer_name: string;
  amount_paise: number;
  amount_paid_paise: number;
  currency: string;
  invoice_status: string;
  due_at: string;
  paid_at: string | null;
  days_overdue: number;
  ageing_bucket: string;
  priority_score: number;
  priority_rank: number;
  score_breakdown: Record<string, unknown>;
  deprioritised: boolean;
  reply_intent: string | null;
  reply_language: string | null;
  disputed: boolean;
  dispute_detail: string | null;
  abstained: boolean;
  rungs_used: number;
  current_rung: string | null;
  next_step: string;
  scheduled_for: string | null;
  authorising_rule: string;
  reason_code: string;
  explanation: string;
  attempts_remaining: number | null;
  escalated: boolean;
  escalation_trigger: string | null;
  reminder_sent: boolean;
  suppressed_rule: string | null;
  payment_link_id: string | null;
  amount_recovered_paise: number;
  customer_reliability: number | null;
}

export interface InvoiceCommunicationRead {
  id: number;
  batch_id: string;
  invoice_id: string;
  customer_id: string;
  created_at: string;
  rung: string;
  rung_number: number;
  channel: string;
  subject: string;
  body: string;
  call_to_action: string;
  payment_link_url: string | null;
  payment_link_id: string | null;
  status: string;
  scheduled_for: string | null;
  authorising_rule: string;
  held_rule: string | null;
  rationale: string;
  provenance: Record<string, unknown>;
  tone_violations: Array<unknown> | null;
}

/**
 * One invoice's complete story — the route the demo leans on.
 */
export interface InvoiceTimeline {
  invoice_id: string;
  batch_id: string;
  state: InvoiceChaseStateRead;
  entries: Array<AuditEntryRead>;
  promises: Array<PromiseToPayRead>;
  communications: Array<InvoiceCommunicationRead>;
  replies: Array<Record<string, unknown>>;
  next_step_summary: string;
}

export interface MandateCommunicationRead {
  id: number;
  batch_id: string;
  mandate_id: string;
  created_at: string;
  kind: string;
  channel: string;
  subject: string;
  body: string;
  call_to_action: string;
  decline_code: string | null;
  failure_route: string;
  status: string;
  scheduled_for: string | null;
  authorising_rule: string;
  held_rule: string | null;
  rationale: string;
  model_reasoning: string | null;
  model_confidence: number | null;
  provenance: Record<string, unknown>;
  recommended_action: string | null;
  overridden_recommendation: string | null;
  tone_violations: Array<unknown> | null;
}

export interface MandateRecoveryStateRead {
  batch_id: string;
  mandate_id: string;
  customer_id: string;
  customer_name: string;
  amount_paise: number;
  currency: string;
  frequency: string;
  mandate_status: string;
  mandate_cap_paise: number;
  afa_registered: boolean;
  afa_side: string;
  attempts_in_cycle: number;
  next_debit_at: string;
  next_debit_notice_sent_at: string | null;
  decline_code: string | null;
  decline_class: string | null;
  failure_route: string;
  classification_rule: string | null;
  notice_status: string;
  notice_lead_hours: number | null;
  next_step: string;
  scheduled_for: string | null;
  authorising_rule: string;
  reason_code: string;
  explanation: string;
  attempts_remaining: number | null;
  terminal: boolean;
  compliance_blocked: boolean;
  debit_attempted: boolean;
  amount_recovered_paise: number;
  retry_probability: number | null;
}

/**
 * What to run, and against what.
 *
 * `now` is exposed because the mandate book is anchored to a fixed `as_of` that
 * is not today, and because the outreach window makes the run clock genuinely
 * load-bearing: a batch executed at 03:00 IST correctly holds every customer
 * message, which is right and looks wrong. Left unset, the runner derives the
 * clock from the latest debit actually attempted in the book.
 */
export interface MandateRunRequest {
  /** Id for this engine run. Not the dataset's batch id. */
  batch_id: string;
  seed?: number;
  /** Path to a mandates .jsonl. Defaults to the committed sample. */
  dataset?: string | null;
  now?: string | null;
}

/**
 * The honest summary of one Engine 2 batch run.
 *
 * Every money and count figure is recomputed from the audit trail by
 * `runner.py`, never accumulated in a side counter that could drift from it.
 */
export interface MandateRunSummary {
  batch_id: string;
  dataset_batch_id: string;
  seed: number;
  now: string;
  mandates_ingested: number;
  mandates_with_failed_cycle: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  amount_addressable_paise: number;
  addressable_recovery_rate: number;
  addressable_definition: string;
  by_failure_class: Record<string, FailureClassBreakdown>;
  by_route: Record<string, number>;
  afa_branch: Record<string, AfaBranchBreakdown>;
  debits_attempted: number;
  debits_recovered: number;
  retries_suppressed: number;
  wasted_attempts_avoided: number;
  wasted_attempts_baseline: string;
  compliance_blocked: number;
  compliance_blocked_by_rule: Record<string, number>;
  notices_sent: number;
  notices_scheduled: number;
  notices_held: number;
  notice_status_counts: Record<string, number>;
  communications_drafted: number;
  communications_held: number;
  tone_rejections: number;
  mandates_at_attempt_cap: number;
  mandates_halted: number;
  human_escalations: number;
  action_counts: Record<string, number>;
  outcome_counts: Record<string, number>;
  policy_denials: number;
  denials_by_rule: Record<string, number>;
  llm_fallbacks: number;
  unknown_declines_classified: number;
  provenance: Record<string, unknown>;
}

/**
 * One mandate's complete history — the route the demo leans on.
 */
export interface MandateTimeline {
  mandate_id: string;
  batch_id: string;
  state: MandateRecoveryStateRead;
  entries: Array<AuditEntryRead>;
  communications: Array<MandateCommunicationRead>;
  next_step_summary: string;
}

/**
 * How it turned out. `audit-schema` -> `outcome` values.
 */
export type Outcome =
  | "success"
  | "failure"
  | "blocked"
  | "scheduled"
  | "skipped"
  | "pending"
  | "escalated"
  | "halted";

/**
 * The whole story in one response — the control tower's first screen.
 */
export interface OverviewSummary {
  generated_at: string;
  batch_ids: Record<string, string>;
  engines_reporting: Array<Engine>;
  engines_missing: Array<Engine>;
  entries: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  blended_caveat: string;
  by_engine: Array<EngineContribution>;
  by_source: Record<string, SourceBreakdown>;
  trust: Array<TrustMetric>;
  policy_violations: number;
  recent_activity: Array<AuditEntryRead>;
}

/**
 * One registered rule, as the dashboard and `/audit-check` see it.
 */
export interface PolicyRuleRead {
  rule_id: string;
  category: string;
  description: string;
  source: RuleSource;
  citation: string;
}

export interface PromiseToPayRead {
  id: number;
  batch_id: string;
  invoice_id: string;
  customer_id: string;
  reply_id: string;
  reply_received_at: string;
  reply_text: string;
  committed_date: string | null;
  committed_amount_paise: number | null;
  conditional: boolean;
  condition_detail: string | null;
  status: string;
  status_rule: string;
  status_rationale: string;
  review_at: string | null;
  superseded_by: number | null;
  model_confidence: number | null;
  provenance: Record<string, unknown>;
  model_reasoning: string | null;
}

/**
 * How a judgment was produced.
 *
 * Frozen on purpose. Provenance describes something that already happened; a
 * caller that can edit it after the fact can misreport how a number was made.
 */
export interface Provenance {
  source: ProvenanceSource;
  provider: string;
  model?: string | null;
  cache_hit?: boolean;
  prompt_version?: string | null;
  abstained?: boolean;
  latency_ms?: number | null;
  tokens?: number | null;
}

/**
 * Was this judgment *reasoned* or *ruled*?
 *
 * There is no third value. A lookup-table decision with no model involved is
 * `deterministic`, because it was ruled. See `audit-schema` -> Provenance.
 */
export type ProvenanceSource = "model" | "deterministic";

/**
 * What to run, and against what.
 *
 * `now` is exposed for the same reason Engine 2 exposes it, and it bites harder
 * here: this engine is *entirely* outreach, so a batch executed outside the
 * 09:00-21:00 IST window correctly holds every single reminder. That is the
 * rule working and it looks like a broken engine. Left unset, the runner
 * derives the clock from the latest observed event in the ledger.
 */
export interface ReceivablesRunRequest {
  /** Id for this engine run. Not the dataset's batch id. */
  batch_id: string;
  seed?: number;
  /** Path to an invoices .jsonl. Defaults to the committed sample. */
  dataset?: string | null;
  now?: string | null;
  /** Compare extractions against the dataset's held-out annotations. Needs a manifest beside the .jsonl. */
  score?: boolean;
}

/**
 * The honest summary of one Engine 3 batch run.
 *
 * Every money and count figure is recomputed from the audit trail by
 * `runner.py`, never accumulated in a side counter that could drift from it.
 */
export interface ReceivablesRunSummary {
  batch_id: string;
  dataset_batch_id: string;
  seed: number;
  now: string;
  invoices_ingested: number;
  invoices_overdue: number;
  invoices_worklisted: number;
  amount_outstanding_paise: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  recovery_definition: string;
  by_ageing_bucket: Record<string, AgeingBreakdown>;
  extraction: ExtractionScore;
  promises_made: number;
  promises_kept: number;
  promises_broken: number;
  promises_active: number;
  promises_superseded: number;
  promises_conditional: number;
  promises_undateable: number;
  customer_reliability: Record<string, CustomerReliability>;
  reminders_sent: number;
  reminders_held: number;
  reminders_by_rung: Record<string, number>;
  payment_links_created: number;
  payment_links_failed: number;
  chase_steps: Record<string, number>;
  escalations: number;
  escalations_by_trigger: Record<string, number>;
  disputes_frozen: number;
  messages_suppressed: number;
  suppressed_by_rule: Record<string, number>;
  invoices_deprioritised: number;
  action_counts: Record<string, number>;
  outcome_counts: Record<string, number>;
  policy_denials: number;
  denials_by_rule: Record<string, number>;
  llm_fallbacks: number;
  abstentions: number;
  provenance: Record<string, unknown>;
}

/**
 * What to run, and against what.
 *
 * `now` is exposed because Phase 2's datasets are anchored to a fixed `as_of`
 * that is not today. Left unset, the runner derives it from the last attempt in
 * the dataset, which is the right default and the one the demo uses.
 */
export interface RootCauseRunRequest {
  /** Id for this engine run. Not the dataset's batch id. */
  batch_id: string;
  seed?: number;
  /** Path to a payments .jsonl. Defaults to the committed sample. */
  dataset?: string | null;
  now?: string | null;
  /** Score detections against the generator's ground truth manifest. */
  score?: boolean;
}

/**
 * The honest summary of one batch run.
 *
 * Every money and count figure here is recomputed from the audit trail by
 * `runner.py`, never accumulated in a side counter that could drift from it.
 */
export interface RootCauseRunSummary {
  batch_id: string;
  dataset_batch_id: string;
  seed: number;
  now: string;
  attempts_ingested: number;
  failed_attempts: number;
  detections: number;
  diagnoses_by_determination: Record<string, number>;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  action_counts: Record<string, number>;
  outcome_counts: Record<string, number>;
  retries_suppressed: number;
  retries_deferred: number;
  reroutes_authorised: number;
  policy_denials: number;
  denials_by_rule: Record<string, number>;
  human_escalations: number;
  llm_fallbacks: number;
  provenance: Record<string, unknown>;
  detection_score?: DetectionScore | null;
}

/**
 * Where a policy rule's authority comes from.
 *
 * Every rule in the registry cites one of these. `PRODUCT_DECISION` is a
 * deliberate escape hatch, but it still requires a named entry in the
 * `policy-bounds` skill — it is not a licence to invent a number inline.
 */
export type RuleSource =
  | "decline-taxonomy"
  | "rbi-mandate-rules"
  | "policy-bounds"
  | "corridor-detection"
  | "product-decision";

/**
 * Per-source figures.
 *
 * Metrics are reported *per source*, never blended: a recovery rate where half
 * the decisions came from static templates is not false, but as one figure it
 * implies more than it delivers.
 */
export interface SourceBreakdown {
  entries?: number;
  amount_at_risk_paise?: number;
  amount_recovered_paise?: number;
  recovery_rate?: number;
}

/**
 * One count that proves the system's bounds are real.
 *
 * `meaning` is part of the payload rather than dashboard copy: a refusal count
 * with no explanation of why non-zero is *good* reads as a defect list.
 */
export interface TrustMetric {
  key: string;
  label: string;
  value: number;
  meaning: string;
  by_rule?: Record<string, number>;
}

export interface ValidationError {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

/** Every path the API serves, so a typo in a URL is a compile error. */
export const API_PATHS = [
  "/",
  "/api/v1/audit/batches",
  "/api/v1/audit/batches/{batch_id}/summary",
  "/api/v1/audit/entities/{entity_type}/{entity_id}/timeline",
  "/api/v1/audit/entries",
  "/api/v1/audit/entries/{entry_id}",
  "/api/v1/audit/rules",
  "/api/v1/health",
  "/api/v1/mandate-recovery/config",
  "/api/v1/mandate-recovery/runs",
  "/api/v1/mandate-recovery/runs/{batch_id}",
  "/api/v1/mandate-recovery/runs/{batch_id}/actions",
  "/api/v1/mandate-recovery/runs/{batch_id}/communications",
  "/api/v1/mandate-recovery/runs/{batch_id}/mandates",
  "/api/v1/mandate-recovery/runs/{batch_id}/mandates/{mandate_id}",
  "/api/v1/mandate-recovery/runs/{batch_id}/summary",
  "/api/v1/overview",
  "/api/v1/receivables/config",
  "/api/v1/receivables/runs",
  "/api/v1/receivables/runs/{batch_id}",
  "/api/v1/receivables/runs/{batch_id}/actions",
  "/api/v1/receivables/runs/{batch_id}/communications",
  "/api/v1/receivables/runs/{batch_id}/extraction",
  "/api/v1/receivables/runs/{batch_id}/invoices/{invoice_id}",
  "/api/v1/receivables/runs/{batch_id}/promises",
  "/api/v1/receivables/runs/{batch_id}/summary",
  "/api/v1/receivables/runs/{batch_id}/worklist",
  "/api/v1/root-cause/config",
  "/api/v1/root-cause/detections/{detection_id}",
  "/api/v1/root-cause/runs",
  "/api/v1/root-cause/runs/{batch_id}",
  "/api/v1/root-cause/runs/{batch_id}/actions",
  "/api/v1/root-cause/runs/{batch_id}/detections",
  "/api/v1/root-cause/runs/{batch_id}/reroutes",
  "/api/v1/root-cause/runs/{batch_id}/summary",
] as const;
