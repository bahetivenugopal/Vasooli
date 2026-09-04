/**
 * The typed API client. Every network call in this app goes through here.
 *
 * Types come from `packages/shared-types/src/api.ts`, which is generated from
 * the backend's own OpenAPI schema (`python scripts/generate_api_types.py`).
 * Nothing in this file re-declares a response shape — a hand-written duplicate
 * is correct until the day a Pydantic field is renamed, and then it is a
 * dashboard rendering `undefined` in front of a judge.
 *
 * The client throws `ApiError` on any non-2xx, carrying the backend's own
 * `detail` string. Every caller renders that, because a blank screen during a
 * live demo is worse than an error message.
 */

import type {
  AuditEntryRead,
  BatchRunRead,
  BatchSummary,
  CorridorDetectionRead,
  CorridorRerouteRead,
  Engine,
  ExtractionScore,
  InvoiceChaseStateRead,
  InvoiceCommunicationRead,
  InvoiceTimeline,
  MandateCommunicationRead,
  MandateRecoveryStateRead,
  MandateRunSummary,
  MandateTimeline,
  OverviewSummary,
  PolicyRuleRead,
  PromiseToPayRead,
  ReceivablesRunSummary,
  RootCauseRunSummary,
} from "@vasooli/shared-types";

/**
 * Where the API lives. `127.0.0.1` rather than `localhost` on purpose.
 *
 * `uvicorn` binds IPv4 loopback by default, while some browsers resolve
 * `localhost` to `::1` first — which makes every page render its error state
 * against a perfectly healthy API. Naming the address removes the ambiguity.
 * Override with `NEXT_PUBLIC_API_BASE_URL` (it is inlined at build time).
 */
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly url: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type QueryValue = string | number | boolean | null | undefined;

/** This page's own origin, for the CORS half of a failed-fetch message. */
function origin(): string {
  return typeof window === "undefined" ? "this page's origin" : window.location.origin;
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(`${API_BASE_URL}/api/v1${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

async function request<T>(
  path: string,
  { query, ...init }: RequestInit & { query?: Record<string, QueryValue> } = {},
): Promise<T> {
  const url = buildUrl(path, query);
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    // `fetch` rejects identically for a refused connection and for a CORS
    // rejection, and the browser's own console message ("Failed to fetch") names
    // neither. Both causes are named here because guessing between them cost
    // real time during this phase: the API was up, and the page was served from
    // 127.0.0.1 while `CORS_ORIGINS` only listed localhost.
    throw new ApiError(
      `Could not reach the Vasooli API at ${API_BASE_URL}. Either it is not ` +
        "running — start it with `uvicorn app.main:app --reload` from apps/api — " +
        `or it is running and refused this page's origin (${origin()}). ` +
        "In that case add that origin to CORS_ORIGINS in .env and restart it.",
      0,
      url,
    );
  }
  if (!response.ok) {
    throw new ApiError(await readDetail(response), response.status, url);
  }
  return (await response.json()) as T;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail) return JSON.stringify(detail);
  } catch {
    /* fall through to the status line */
  }
  return `${response.status} ${response.statusText}`;
}

/** What a run trigger sends. Shared by all three engines' POST /runs. */
export interface RunTriggerBody {
  batch_id: string;
  seed?: number;
  dataset?: string | null;
  now?: string | null;
  score?: boolean;
}

export const api = {
  // --- cross-engine ----------------------------------------------------

  /**
   * The overview headline. Computed server-side, on purpose — see
   * `apps/api/app/services/overview.py`. Nothing here adds it up again.
   */
  overview: (query?: {
    root_cause?: string;
    mandate_recovery?: string;
    receivables?: string;
    recent_limit?: number;
  }) => request<OverviewSummary>("/overview", { query }),

  // --- audit -----------------------------------------------------------

  auditEntries: (query?: {
    batch_id?: string;
    engine?: Engine;
    entity_type?: string;
    entity_id?: string;
    action?: string;
    outcome?: string;
    source?: string;
    limit?: number;
    offset?: number;
  }) => request<AuditEntryRead[]>("/audit/entries", { query }),

  auditEntry: (entryId: number) => request<AuditEntryRead>(`/audit/entries/${entryId}`),

  batches: (query?: { engine?: Engine; limit?: number }) =>
    request<BatchRunRead[]>("/audit/batches", { query }),

  batchSummary: (batchId: string) =>
    request<BatchSummary>(`/audit/batches/${encodeURIComponent(batchId)}/summary`),

  entityTimeline: (entityType: string, entityId: string) =>
    request<AuditEntryRead[]>(
      `/audit/entities/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/timeline`,
    ),

  rules: () => request<PolicyRuleRead[]>("/audit/rules"),

  // --- engine 1 --------------------------------------------------------

  rootCause: {
    runs: (query?: { limit?: number }) => request<BatchRunRead[]>("/root-cause/runs", { query }),
    run: (batchId: string) =>
      request<BatchRunRead>(`/root-cause/runs/${encodeURIComponent(batchId)}`),
    summary: (batchId: string) =>
      request<RootCauseRunSummary>(`/root-cause/runs/${encodeURIComponent(batchId)}/summary`),
    detections: (batchId: string, query?: { determination?: string; allowed?: boolean }) =>
      request<CorridorDetectionRead[]>(
        `/root-cause/runs/${encodeURIComponent(batchId)}/detections`,
        { query },
      ),
    detection: (detectionId: string) =>
      request<CorridorDetectionRead>(`/root-cause/detections/${encodeURIComponent(detectionId)}`),
    reroutes: (batchId: string) =>
      request<CorridorRerouteRead[]>(`/root-cause/runs/${encodeURIComponent(batchId)}/reroutes`),
    actions: (batchId: string, query?: { limit?: number; offset?: number }) =>
      request<AuditEntryRead[]>(`/root-cause/runs/${encodeURIComponent(batchId)}/actions`, {
        query,
      }),
    config: () => request<Record<string, unknown>>("/root-cause/config"),
    trigger: (body: RunTriggerBody) =>
      request<RootCauseRunSummary>("/root-cause/runs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },

  // --- engine 2 --------------------------------------------------------

  mandateRecovery: {
    runs: (query?: { limit?: number }) =>
      request<BatchRunRead[]>("/mandate-recovery/runs", { query }),
    run: (batchId: string) =>
      request<BatchRunRead>(`/mandate-recovery/runs/${encodeURIComponent(batchId)}`),
    summary: (batchId: string) =>
      request<MandateRunSummary>(`/mandate-recovery/runs/${encodeURIComponent(batchId)}/summary`),
    mandates: (
      batchId: string,
      query?: {
        route?: string;
        next_step?: string;
        afa_side?: string;
        compliance_blocked?: boolean;
      },
    ) =>
      request<MandateRecoveryStateRead[]>(
        `/mandate-recovery/runs/${encodeURIComponent(batchId)}/mandates`,
        { query },
      ),
    timeline: (batchId: string, mandateId: string) =>
      request<MandateTimeline>(
        `/mandate-recovery/runs/${encodeURIComponent(batchId)}/mandates/${encodeURIComponent(mandateId)}`,
      ),
    communications: (batchId: string, query?: { status?: string; kind?: string }) =>
      request<MandateCommunicationRead[]>(
        `/mandate-recovery/runs/${encodeURIComponent(batchId)}/communications`,
        { query },
      ),
    actions: (batchId: string, query?: { limit?: number; offset?: number }) =>
      request<AuditEntryRead[]>(`/mandate-recovery/runs/${encodeURIComponent(batchId)}/actions`, {
        query,
      }),
    config: () => request<Record<string, unknown>>("/mandate-recovery/config"),
    trigger: (body: RunTriggerBody) =>
      request<MandateRunSummary>("/mandate-recovery/runs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },

  // --- engine 3 --------------------------------------------------------

  receivables: {
    runs: (query?: { limit?: number }) => request<BatchRunRead[]>("/receivables/runs", { query }),
    run: (batchId: string) =>
      request<BatchRunRead>(`/receivables/runs/${encodeURIComponent(batchId)}`),
    summary: (batchId: string) =>
      request<ReceivablesRunSummary>(`/receivables/runs/${encodeURIComponent(batchId)}/summary`),
    worklist: (
      batchId: string,
      query?: {
        deprioritised?: boolean;
        ageing_bucket?: string;
        next_step?: string;
        limit?: number;
      },
    ) =>
      request<InvoiceChaseStateRead[]>(
        `/receivables/runs/${encodeURIComponent(batchId)}/worklist`,
        {
          query,
        },
      ),
    promises: (
      batchId: string,
      query?: { status?: string; conditional?: boolean; customer_id?: string },
    ) =>
      request<PromiseToPayRead[]>(`/receivables/runs/${encodeURIComponent(batchId)}/promises`, {
        query,
      }),
    extraction: (batchId: string) =>
      request<ExtractionScore>(`/receivables/runs/${encodeURIComponent(batchId)}/extraction`),
    timeline: (batchId: string, invoiceId: string) =>
      request<InvoiceTimeline>(
        `/receivables/runs/${encodeURIComponent(batchId)}/invoices/${encodeURIComponent(invoiceId)}`,
      ),
    communications: (batchId: string, query?: { status?: string; rung?: string }) =>
      request<InvoiceCommunicationRead[]>(
        `/receivables/runs/${encodeURIComponent(batchId)}/communications`,
        { query },
      ),
    actions: (batchId: string, query?: { limit?: number; offset?: number }) =>
      request<AuditEntryRead[]>(`/receivables/runs/${encodeURIComponent(batchId)}/actions`, {
        query,
      }),
    config: () => request<Record<string, unknown>>("/receivables/config"),
    trigger: (body: RunTriggerBody) =>
      request<ReceivablesRunSummary>("/receivables/runs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
} as const;
