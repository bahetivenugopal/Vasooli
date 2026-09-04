"use client";

/**
 * The batch run trigger. One component, all three engines.
 *
 * §5.2 wants a visible trigger so a live demo can start a run on camera, and
 * §5.6 wants visible progress rather than a frozen screen. The backend's
 * `POST /runs` is synchronous and can take tens of seconds against a live
 * provider, so this shows an elapsed timer and the stage it is in while the
 * request is open — the honest version of a progress bar, since the API reports
 * no intermediate progress to poll for.
 *
 * The engine's own `RunSummary` comes straight back from the POST, so the result
 * strip below the button is the API's own numbers, not a recomputation.
 */

import { useEffect, useRef, useState } from "react";
import { CheckCircle2, CirclePlay, Loader2, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Money } from "@/components/features/metric-card";
import { api, type RunTriggerBody } from "@/lib/api";
import { describe } from "@/hooks/use-api";
import { formatRate } from "@/lib/money";
import { cn } from "@/lib/utils";

export type RunEngine = "root-cause" | "mandate-recovery" | "receivables";

const TRIGGERS = {
  "root-cause": api.rootCause.trigger,
  "mandate-recovery": api.mandateRecovery.trigger,
  receivables: api.receivables.trigger,
} as const;

const LABELS: Record<RunEngine, string> = {
  "root-cause": "root-cause",
  "mandate-recovery": "mandate-recovery",
  receivables: "receivables",
};

/** What every engine's summary has in common, and all this component reads. */
interface CommonSummary {
  batch_id: string;
  dataset_batch_id: string;
  seed: number;
  amount_at_risk_paise: number;
  amount_recovered_paise: number;
  recovery_rate: number;
  policy_denials: number;
}

function defaultBatchId(engine: RunEngine): string {
  const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return `${LABELS[engine].slice(0, 3)}-ui-${stamp}`;
}

export function RunTrigger({
  engine,
  onComplete,
  className,
}: {
  engine: RunEngine;
  /** Called with the new run's batch id so the page can switch to it. */
  onComplete?: (batchId: string) => void;
  className?: string;
}) {
  const [batchId, setBatchId] = useState(() => defaultBatchId(engine));
  const [seed, setSeed] = useState("42");
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CommonSummary | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
    },
    [],
  );

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    setElapsed(0);
    timer.current = setInterval(() => setElapsed((value) => value + 1), 1000);

    const body: RunTriggerBody = { batch_id: batchId.trim(), seed: Number(seed) || 42 };
    try {
      const summary = (await TRIGGERS[engine](body)) as unknown as CommonSummary;
      setResult(summary);
      onComplete?.(summary.batch_id);
      // A fresh id for the next run: reusing one is a 409 by design, because two
      // runs sharing an id would merge into one set of metrics.
      setBatchId(defaultBatchId(engine));
    } catch (caught) {
      setError(describe(caught));
    } finally {
      if (timer.current) clearInterval(timer.current);
      setRunning(false);
    }
  }

  return (
    <Card className={className}>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-48 flex-1 space-y-1">
            <Label htmlFor={`batch-${engine}`}>Batch id</Label>
            <Input
              id={`batch-${engine}`}
              value={batchId}
              onChange={(event) => setBatchId(event.target.value)}
              disabled={running}
              spellCheck={false}
              className="font-mono"
            />
          </div>
          <div className="w-24 space-y-1">
            <Label htmlFor={`seed-${engine}`}>Seed</Label>
            <Input
              id={`seed-${engine}`}
              value={seed}
              onChange={(event) => setSeed(event.target.value)}
              disabled={running}
              inputMode="numeric"
              className="font-mono"
            />
          </div>
          <Button onClick={run} disabled={running || !batchId.trim()}>
            {running ? (
              <Loader2 className="animate-spin" aria-hidden />
            ) : (
              <CirclePlay aria-hidden />
            )}
            {running ? "Running…" : "Run batch"}
          </Button>
        </div>

        {running && (
          <p
            className="flex items-center gap-2 text-sm text-muted-foreground"
            role="status"
            aria-live="polite"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Ingesting, deciding and auditing the {LABELS[engine]} batch — {elapsed}s elapsed. A
            live-provider run makes one reasoning call per case, so the first run on a cold cache is
            the slow one.
          </p>
        )}

        {error && (
          <p className="flex items-start gap-2 text-sm text-at-risk" role="alert">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            {error}
          </p>
        )}

        {result && (
          <div className={cn("flex flex-wrap items-center gap-x-6 gap-y-1 text-sm")} role="status">
            <span className="inline-flex items-center gap-1.5 font-medium text-recovery">
              <CheckCircle2 className="size-4" aria-hidden />
              Run complete
            </span>
            <span className="font-mono text-xs text-muted-foreground">
              {result.batch_id} · seed {result.seed} · dataset {result.dataset_batch_id}
            </span>
            <span>
              <span className="text-muted-foreground">At risk </span>
              <Money paise={result.amount_at_risk_paise} tone="at-risk" />
            </span>
            <span>
              <span className="text-muted-foreground">Recovered </span>
              <Money paise={result.amount_recovered_paise} tone="recovery" />
            </span>
            <span>
              <span className="text-muted-foreground">Rate </span>
              {formatRate(result.recovery_rate)}
            </span>
            <span>
              <span className="text-muted-foreground">Policy denials </span>
              {result.policy_denials}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
