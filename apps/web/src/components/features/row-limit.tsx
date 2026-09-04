"use client";

/**
 * Show the first N rows of a long list, with a control to show the rest.
 *
 * The worklists are the full run — 64 mandates, 70-odd invoices — and rendering
 * all of them makes a page tall enough that a reader scrolls past the metrics
 * to reach the footer, and tall enough that a full-page screenshot stitches
 * badly. Both are demo problems rather than correctness ones, so the fix is
 * presentational: the ranking still comes from the API in full, and the rows
 * below the fold are one click away rather than gone.
 *
 * The count of what is hidden is always stated. A silently truncated list is a
 * list somebody will quote the length of.
 */

import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

import { Button } from "@/components/ui/button";

export function useRowLimit<T>(rows: T[], preview = 25) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? rows : rows.slice(0, preview);
  return {
    visible,
    expanded,
    hidden: rows.length - visible.length,
    total: rows.length,
    toggle: () => setExpanded((value) => !value),
  };
}

export function RowLimitFooter({
  expanded,
  hidden,
  total,
  onToggle,
  noun,
  order = "in rank order",
}: {
  expanded: boolean;
  hidden: number;
  total: number;
  onToggle: () => void;
  noun: string;
  /** How the list is ordered, so the footer does not claim a rank it does not have. */
  order?: string;
}) {
  if (hidden === 0 && !expanded) return null;
  return (
    <div className="flex items-center justify-between border-t px-3 py-2 text-sm text-muted-foreground">
      <span>
        {expanded
          ? `Showing all ${total} ${noun}`
          : `Showing the first ${total - hidden} of ${total} ${noun}, ${order}`}
      </span>
      <Button variant="ghost" size="sm" onClick={onToggle}>
        {expanded ? <ChevronUp aria-hidden /> : <ChevronDown aria-hidden />}
        {expanded ? "Show fewer" : `Show all ${total}`}
      </Button>
    </div>
  );
}
