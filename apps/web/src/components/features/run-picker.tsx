"use client";

/**
 * Pick which run an engine view is reporting on.
 *
 * Every number on an engine page belongs to exactly one batch, and a page that
 * silently shows "the latest" is a page whose figures change under the presenter
 * mid-demo. So the run is always named on screen, with its seed, and switching
 * is explicit.
 */

import type { BatchRunRead } from "@vasooli/shared-types";

import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { formatDateTime } from "@/lib/format";

export function RunPicker({
  runs,
  value,
  onChange,
}: {
  runs: BatchRunRead[];
  value: string;
  onChange: (batchId: string) => void;
}) {
  const selected = runs.find((run) => run.batch_id === value);
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="min-w-64 space-y-1">
        <Label htmlFor="run-picker">Run</Label>
        <Select
          id="run-picker"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          options={runs.map((run) => ({
            value: run.batch_id,
            label: `${run.batch_id} · seed ${run.seed} · ${run.status}`,
          }))}
        />
      </div>
      {selected && (
        <p className="pb-2 text-xs text-muted-foreground">
          Started {formatDateTime(selected.started_at)}
          {selected.notes?.dataset_batch_id ? (
            <>
              {" "}
              · dataset <span className="font-mono">{String(selected.notes.dataset_batch_id)}</span>
            </>
          ) : null}
        </p>
      )}
    </div>
  );
}
