"use client";

/**
 * The shell: a flat, four-item nav and the page frame.
 *
 * §5.1: "Navigation should be flat and obvious. A judge should never wonder
 * where they are." So there are no nested menus, no collapsible sidebar, and the
 * active route is marked rather than merely highlighted on hover.
 */

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity, ArrowLeft, Gauge, ScrollText, Workflow } from "lucide-react";
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

/**
 * Scroll to `#anchor` once the element it names actually exists.
 *
 * Every engine page fetches its data in the browser, so at the moment a
 * navigation lands the page is a few hundred pixels of skeleton and the anchor
 * does not exist yet. The browser's own hash handling — and its scroll
 * restoration — both fire against that short page, find nothing, and give up at
 * the top. So this retries while the tables render, then stops.
 *
 * Bounded on purpose: ~6s of polling, then it leaves the reader where they are
 * rather than yanking the page under them long after they started reading.
 */
function useScrollToHash() {
  const pathname = usePathname();
  React.useEffect(() => {
    const id = decodeURIComponent(window.location.hash.replace(/^#/, ""));
    if (!id) return;

    let cancelled = false;
    let attempts = 0;
    let release: (() => void) | undefined;

    //: Hold the anchor in place while the page finishes filling in around it.
    //: Charts and score panels *above* the table resolve after the table does,
    //: and each one pushes the anchor further down — so a single scroll lands
    //: the reader hundreds of pixels short. Re-assert on every height change,
    //: for a couple of seconds, and stop the moment the reader takes over.
    const hold = (target: HTMLElement) => {
      const toTarget = () => target.scrollIntoView({ block: "start" });
      toTarget();

      const observer = new ResizeObserver(toTarget);
      observer.observe(document.body);

      const done = () => {
        observer.disconnect();
        window.clearTimeout(timer);
        window.removeEventListener("wheel", done);
        window.removeEventListener("touchstart", done);
        window.removeEventListener("keydown", done);
      };
      const timer = window.setTimeout(done, 2500);
      //: A reader who scrolls, swipes or presses a key owns the viewport from
      //: then on. Nothing should yank it back under them.
      window.addEventListener("wheel", done, { passive: true, once: true });
      window.addEventListener("touchstart", done, { passive: true, once: true });
      window.addEventListener("keydown", done, { once: true });
      release = done;
    };

    const tick = () => {
      if (cancelled) return;
      const target = document.getElementById(id);
      if (target) {
        hold(target);
        return;
      }
      if (attempts++ < 60) window.setTimeout(tick, 100);
    };
    tick();

    return () => {
      cancelled = true;
      release?.();
    };
  }, [pathname]);
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  useScrollToHash();
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

/**
 * Return to wherever the user came from before opening a timeline.
 *
 * Every engine page ends in a long table whose rows drill into a timeline, and
 * without this the only way back is the header nav — which lands you at the top
 * of the engine page having lost the row you were reading. On a 64-row mandate
 * table that is a genuine dead end, and it is worst during a demo.
 *
 * The origin travels in the link as `?from=` (an app-relative path, anchored at
 * the table the row came from) plus `?fromLabel=` for the wording. That is used
 * in preference to `router.back()` because it behaves identically however the
 * timeline was reached — a click, a refresh, a pasted URL — and because the
 * anchor is what actually restores the reading position: the browser's own
 * scroll restoration fires while the page is still an empty skeleton and lands
 * at the top.
 *
 * `from` is read after mount rather than with `useSearchParams`, which would
 * force every timeline route behind a Suspense boundary at build time for no
 * benefit here.
 */
export function BackLink({
  fallbackHref,
  fallbackLabel,
}: {
  fallbackHref: string;
  fallbackLabel: string;
}) {
  const [origin, setOrigin] = React.useState<{ href: string; label: string } | null>(null);

  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const from = params.get("from");
    //: same-origin app paths only — never follow an absolute URL from a query string.
    if (!from || !from.startsWith("/") || from.startsWith("//")) return;
    setOrigin({ href: from, label: params.get("fromLabel") ?? fallbackLabel });
  }, [fallbackLabel]);

  return (
    <Link
      href={origin?.href ?? fallbackHref}
      className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
    >
      <ArrowLeft className="size-4" aria-hidden />
      Back to {origin?.label ?? fallbackLabel}
    </Link>
  );
}
