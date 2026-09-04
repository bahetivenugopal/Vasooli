import { cn } from "@/lib/utils";

/**
 * The loading placeholder. One shape, used everywhere something is being
 * fetched, so a slow API reads as "loading" rather than as "broken".
 */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}

export { Skeleton };
