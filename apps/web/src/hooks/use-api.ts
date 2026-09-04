"use client";

/**
 * The one data-fetching hook. Every surface in this app loads through it, so
 * every surface gets the same three states without having to remember to.
 *
 * §5.6: "Loading and error states everywhere... A blank screen during a live
 * demo is worse than an error message." That is only true if it is cheap to
 * comply, so the hook returns `{ data, error, loading, reload }` and the
 * `<AsyncBoundary>` component renders the three cases from it.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Refetch on demand — used by the error state's retry and after a batch run. */
  reload: () => void;
}

/**
 * Run `fetcher` on mount and whenever `deps` change.
 *
 * `deps` is an explicit array rather than the fetcher's identity, because an
 * inline arrow function is a new identity on every render and would fetch in a
 * loop. Callers pass the values the fetch actually depends on.
 */
export function useApi<T>(fetcher: () => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  // The fetcher changes identity every render; keeping it in a ref lets the
  // effect depend on `deps` alone without lying to the linter about it.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetcherRef
      .current()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setData(null);
        setError(describe(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** Turn anything thrown into a sentence a person can act on. */
export function describe(caught: unknown): string {
  if (caught instanceof ApiError) return caught.message;
  if (caught instanceof Error) return caught.message;
  return String(caught);
}
