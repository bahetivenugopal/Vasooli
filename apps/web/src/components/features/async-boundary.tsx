"use client";

/**
 * The three states every fetched surface can be in, rendered once.
 *
 * §5.6 asks for loading and error states *everywhere* and empty states that say
 * how to get data. That only actually happens if it is less work to comply than
 * to skip, so every page wraps its content in `<AsyncBoundary>` and gets all
 * three for free.
 */

import type { ReactNode } from "react";
import { AlertTriangle, DatabaseZap, RotateCw } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function LoadingState({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-3", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }).map((_, index) => (
        <Skeleton key={index} className="h-16 w-full" />
      ))}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle aria-hidden />
      <AlertTitle>Could not load this view</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>{message}</p>
        {onRetry && (
          <Button size="sm" variant="outline" onClick={onRetry}>
            <RotateCw aria-hidden />
            Retry
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}

/**
 * The empty state says how to get data, not just that there is none. A bare
 * empty table in a demo looks like a bug; "run a batch" looks like a next step.
 */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-12 text-center">
      <DatabaseZap className="size-6 text-muted-foreground" aria-hidden />
      <p className="font-medium">{title}</p>
      <div className="max-w-prose text-sm text-muted-foreground">{hint}</div>
      {action}
    </div>
  );
}

export interface AsyncBoundaryProps<T> {
  state: { data: T | null; error: string | null; loading: boolean; reload: () => void };
  /** True when the fetch succeeded but there is nothing to show. */
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  loadingRows?: number;
  children: (data: T) => ReactNode;
}

export function AsyncBoundary<T>({
  state,
  isEmpty,
  empty,
  loadingRows,
  children,
}: AsyncBoundaryProps<T>) {
  if (state.loading && state.data === null) return <LoadingState rows={loadingRows} />;
  if (state.error) return <ErrorState message={state.error} onRetry={state.reload} />;
  if (state.data === null) return <LoadingState rows={loadingRows} />;
  if (isEmpty?.(state.data) && empty) return <>{empty}</>;
  return <>{children(state.data)}</>;
}
