"use client";

/**
 * The shell: a flat, four-item nav and the page frame.
 *
 * §5.1: "Navigation should be flat and obvious. A judge should never wonder
 * where they are." So there are no nested menus, no collapsible sidebar, and the
 * active route is marked rather than merely highlighted on hover.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, Gauge, ScrollText, Workflow } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Match the path exactly. Only the overview needs it — "/" prefixes everything. */
  exact?: boolean;
}

const NAV: readonly NavItem[] = [
  { href: "/", label: "Overview", icon: Gauge, exact: true },
  { href: "/engines/root-cause", label: "Root cause", icon: Activity },
  { href: "/engines/mandate-recovery", label: "Mandates", icon: Workflow },
  { href: "/engines/receivables", label: "Receivables", icon: Workflow },
  { href: "/audit", label: "Audit trail", icon: ScrollText },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 sm:px-6 lg:flex-row lg:items-center lg:justify-between">
          <Link href="/" className="flex items-baseline gap-2">
            <span className="text-lg font-semibold tracking-tight">Vasooli</span>
            <span className="hidden text-xs text-muted-foreground sm:inline">
              Control tower — revenue recovery, bounded and audited
            </span>
          </Link>
          <nav className="flex flex-wrap gap-1" aria-label="Primary">
            {NAV.map((item) => {
              const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    active
                      ? "bg-secondary text-secondary-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  <Icon className="size-4" aria-hidden />
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">{children}</main>
      <footer className="mx-auto max-w-7xl px-4 pb-10 text-xs text-muted-foreground sm:px-6">
        All data is synthetic and every batch is seeded. Every figure on these pages is recomputed
        from the audit trail by the API — nothing on this dashboard calculates a metric.
      </footer>
    </div>
  );
}

/** The standard page header: a title, a sentence of context, and page actions. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && (
          <div className="max-w-3xl text-sm text-muted-foreground">{description}</div>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
    </div>
  );
}
