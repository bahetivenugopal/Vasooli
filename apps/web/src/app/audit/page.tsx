"use client";

/**
 * The audit trail viewer — the proof surface.
 *
 * §5.5: a filterable table over the full trail, with each row expanding to show
 * the full reasoning and the authorising rule, and a **preset for policy denials
 * only**. That preset is the strongest demo moment in the dashboard: one click
 * and a judge is looking at every action the system refused to take, each one
 * naming the rule that refused it.
 *
 * The filters are all server-side query parameters — the API already supports
 * every one of them, so nothing here filters a fetched list in the browser and
 * then reports a count that means something narrower than it appears.
 */

import { useCallback, useState } from "react";
import { Filter, ShieldX, X } from "lucide-react";

import type { AuditEntryRead, BatchRunRead } from "@vasooli/shared-types";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AsyncBoundary, EmptyState } from "@/components/features/async-boundary";
import { PageHeader } from "@/components/features/app-shell";
import { RowLimitFooter, useRowLimit } from "@/components/features/row-limit";
import { Money } from "@/components/features/metric-card";
import { ProvenanceBadge, RuleBadge } from "@/components/features/provenance-badge";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { formatDateTime, humanise } from "@/lib/format";

const ENGINES = [
  { value: "root_cause", label: "Root cause" },
  { value: "mandate_recovery", label: "Mandate recovery" },
  { value: "receivables", label: "Receivables" },
  { value: "core", label: "Shared core" },
];

const OUTCOMES = [
  { value: "success", label: "Success" },
  { value: "failure", label: "Failure" },
  { value: "blocked", label: "Blocked" },
  { value: "halted", label: "Halted" },
  { value: "escalated", label: "Escalated" },
  { value: "scheduled", label: "Scheduled" },
  { value: "skipped", label: "Skipped" },
  { value: "pending", label: "Pending" },
];

const ENTITY_TYPES = [
  { value: "payment", label: "Payment" },
  { value: "mandate", label: "Mandate" },
  { value: "invoice", label: "Invoice" },
  { value: "corridor", label: "Corridor" },
  { value: "batch", label: "Batch" },
];

const ACTIONS = [
  "classify_decline",
  "diagnose_root_cause",
  "schedule_retry",
  "attempt_charge",
  "send_pre_debit_notice",
  "reroute_traffic",
  "send_dunning",
  "send_reminder",
  "record_promise_to_pay",
  "escalate",
  "block_attempt",
  "halt_schedule",
  "api_call",
].map((value) => ({ value, label: humanise(value) }));

const SOURCES = [
  { value: "model", label: "LLM reasoned" },
  { value: "deterministic", label: "Rule / fallback" },
];

interface Filters {
  batch_id: string;
  engine: string;
  entity_type: string;
  entity_id: string;
  action: string;
  outcome: string;
  source: string;
}

const EMPTY: Filters = {
  batch_id: "",
  engine: "",
  entity_type: "",
  entity_id: "",
  action: "",
  outcome: "",
  source: "",
};

export default function AuditPage() {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const set = useCallback(
    (key: keyof Filters, value: string) => setFilters((f) => ({ ...f, [key]: value })),
    [],
  );

  const runs = useApi(() => api.batches({ limit: 100 }), []);
  const entries = useApi(
    () =>
      api.auditEntries({
        batch_id: filters.batch_id || undefined,
        engine: (filters.engine || undefined) as never,
        entity_type: filters.entity_type || undefined,
        entity_id: filters.entity_id || undefined,
        action: filters.action || undefined,
        outcome: filters.outcome || undefined,
        source: filters.source || undefined,
        limit: 300,
      }),
    [
      filters.batch_id,
      filters.engine,
      filters.entity_type,
      filters.entity_id,
      filters.action,
      filters.outcome,
      filters.source,
    ],
  );

  const denialsOnly = filters.outcome === "blocked";
  const active = Object.values(filters).some(Boolean);

  return (
    <>
      <PageHeader
        title="Audit trail"
        description="Every decision the three engines made, and every one they refused. Each entry cites the rule that authorised it, carries the provenance of the judgment behind it, and is append-only — the single permitted mutation is resolving a pending outcome."
        actions={
          <>
            <Button
              variant={denialsOnly ? "default" : "outline"}
              onClick={() => setFilters((f) => ({ ...f, outcome: denialsOnly ? "" : "blocked" }))}
            >
              <ShieldX aria-hidden />
              {denialsOnly ? "Showing policy denials" : "Policy denials only"}
            </Button>
            {active && (
              <Button variant="ghost" onClick={() => setFilters(EMPTY)}>
                <X aria-hidden />
                Clear filters
              </Button>
            )}
          </>
        }
      />

      <Card className="mb-6">
        <CardContent className="p-4">
          <p className="mb-3 flex items-center gap-2 text-sm text-muted-foreground">
            <Filter className="size-4" aria-hidden />
            Every filter below is applied by the API, not in the browser — so a count here means
            what it says.
          </p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="f-batch">Batch run</Label>
              <AsyncBoundary state={runs} loadingRows={1}>
                {(data: BatchRunRead[]) => (
                  <Select
                    id="f-batch"
                    placeholder="Any run"
                    value={filters.batch_id}
                    onChange={(event) => set("batch_id", event.target.value)}
                    options={data.map((run) => ({
                      value: run.batch_id,
                      label: `${run.batch_id} (${run.engine})`,
                    }))}
                  />
                )}
              </AsyncBoundary>
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-engine">Engine</Label>
              <Select
                id="f-engine"
                placeholder="Any engine"
                value={filters.engine}
                onChange={(event) => set("engine", event.target.value)}
                options={ENGINES}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-entity-type">Entity type</Label>
              <Select
                id="f-entity-type"
                placeholder="Any entity"
                value={filters.entity_type}
                onChange={(event) => set("entity_type", event.target.value)}
                options={ENTITY_TYPES}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-entity-id">Entity id</Label>
              <Input
                id="f-entity-id"
                placeholder="inv_0001"
                className="font-mono"
                value={filters.entity_id}
                onChange={(event) => set("entity_id", event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-action">Action</Label>
              <Select
                id="f-action"
                placeholder="Any action"
                value={filters.action}
                onChange={(event) => set("action", event.target.value)}
                options={ACTIONS}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-outcome">Outcome</Label>
              <Select
                id="f-outcome"
                placeholder="Any outcome"
                value={filters.outcome}
                onChange={(event) => set("outcome", event.target.value)}
                options={OUTCOMES}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="f-source">Decision source</Label>
              <Select
                id="f-source"
                placeholder="Reasoned or ruled"
                value={filters.source}
                onChange={(event) => set("source", event.target.value)}
                options={SOURCES}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <AsyncBoundary
        state={entries}
        loadingRows={6}
        isEmpty={(data: AuditEntryRead[]) => data.length === 0}
        empty={
          <EmptyState
            title="No entries match these filters"
            hint={
              active
                ? "Clear a filter, or pick a different run. An empty result here is a real answer, not a missing one."
                : "No batch has been run yet. Trigger one from the overview page — every run is seeded and reproducible."
            }
          />
        }
      >
        {(data) => <EntryTable entries={data} denialsOnly={denialsOnly} />}
      </AsyncBoundary>
    </>
  );
}

function EntryTable({ entries, denialsOnly }: { entries: AuditEntryRead[]; denialsOnly: boolean }) {
  const rows = useRowLimit(entries, 50);
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        {entries.length} entr{entries.length === 1 ? "y" : "ies"}, newest first.
        {denialsOnly &&
          " Every one of these is an action the system proposed and its own rules stopped."}
      </p>
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Entity</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead>Authorising rule</TableHead>
                <TableHead>Decided by</TableHead>
                <TableHead>Money</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.visible.map((entry) => (
                <EntryRow key={entry.id} entry={entry} />
              ))}
            </TableBody>
          </Table>
          <RowLimitFooter
            expanded={rows.expanded}
            hidden={rows.hidden}
            total={rows.total}
            onToggle={rows.toggle}
            noun="entries"
          />
        </CardContent>
      </Card>
    </div>
  );
}

function EntryRow({ entry }: { entry: AuditEntryRead }) {
  const [open, setOpen] = useState(false);
  const metadata = entry.metadata ?? {};
  const degraded = metadata.degraded === true;
  const reasoning =
    typeof metadata.model_reasoning === "string"
      ? metadata.model_reasoning
      : typeof metadata.reasoning === "string"
        ? metadata.reasoning
        : null;

  return (
    <>
      <TableRow
        onClick={() => setOpen((value) => !value)}
        className="cursor-pointer"
        aria-expanded={open}
      >
        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
          {formatDateTime(entry.timestamp)}
        </TableCell>
        <TableCell>
          <div className="font-mono text-xs">{entry.entity_id}</div>
          <div className="text-xs text-muted-foreground">
            {entry.entity_type} · {humanise(entry.engine)}
          </div>
        </TableCell>
        <TableCell className="text-sm">{humanise(entry.action)}</TableCell>
        <TableCell>
          <Badge
            variant={
              entry.outcome === "success"
                ? "recovery"
                : ["blocked", "halted", "failure"].includes(entry.outcome)
                  ? "atRisk"
                  : "muted"
            }
          >
            {humanise(entry.outcome)}
          </Badge>
        </TableCell>
        <TableCell>
          <RuleBadge rule={entry.authorising_rule} />
          <div className="mt-1 font-mono text-[11px] text-muted-foreground">
            {entry.reason_code}
          </div>
        </TableCell>
        <TableCell>
          <ProvenanceBadge
            provenance={entry.provenance}
            degraded={degraded}
            confidence={entry.model_confidence}
          />
        </TableCell>
        <TableCell className="whitespace-nowrap text-sm">
          {entry.amount_at_risk_paise > 0 && (
            <div>
              <Money paise={entry.amount_at_risk_paise} tone="at-risk" />
            </div>
          )}
          {entry.amount_recovered_paise > 0 && (
            <div>
              <Money paise={entry.amount_recovered_paise} tone="recovery" />
            </div>
          )}
          {entry.amount_at_risk_paise === 0 && entry.amount_recovered_paise === 0 && (
            <span className="text-muted-foreground">—</span>
          )}
        </TableCell>
      </TableRow>
      {open && (
        <TableRow>
          <TableCell colSpan={7} className="bg-muted/40">
            <div className="space-y-3 py-2">
              <p className="text-sm">{entry.rationale}</p>
              {reasoning && (
                <blockquote className="rounded-md border-l-2 border-reasoned bg-background px-3 py-2 text-sm italic">
                  <span className="mb-1 block text-[11px] font-medium uppercase not-italic tracking-wide text-reasoned">
                    Model reasoning, verbatim
                  </span>
                  {reasoning}
                </blockquote>
              )}
              <dl className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
                <div className="flex gap-2">
                  <dt className="font-mono text-muted-foreground">entry_id</dt>
                  <dd className="font-mono">{entry.id}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="font-mono text-muted-foreground">batch_id</dt>
                  <dd className="font-mono">{entry.batch_id}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="font-mono text-muted-foreground">attempt</dt>
                  <dd className="font-mono">
                    {entry.attempt_number ?? "—"} / remaining {entry.attempts_remaining ?? "—"}
                  </dd>
                </div>
                {Object.entries(metadata)
                  .filter(([key]) => !["model_reasoning", "reasoning"].includes(key))
                  .map(([key, value]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="shrink-0 font-mono text-muted-foreground">{key}</dt>
                      <dd className="break-all font-mono">
                        {typeof value === "object" && value !== null
                          ? JSON.stringify(value)
                          : String(value)}
                      </dd>
                    </div>
                  ))}
              </dl>
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
