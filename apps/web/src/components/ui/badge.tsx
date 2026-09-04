import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * The badge primitive. Defined once; feature-specific badges compose it rather
 * than restyling a `<span>` of their own.
 *
 * The `reasoned` / `ruled` variants are not decoration: they are how the
 * dashboard makes "an LLM judged this" and "a rule judged this" distinguishable
 * at a glance, which is the product's core safety claim. Keeping them here means
 * the association between meaning and colour is defined in one place.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "text-foreground",
        muted: "border-transparent bg-muted text-muted-foreground",
        recovery: "border-transparent bg-recovery-muted text-recovery",
        atRisk: "border-transparent bg-at-risk-muted text-at-risk",
        reasoned: "border-reasoned/30 bg-reasoned-muted text-reasoned",
        ruled: "border-ruled/25 bg-ruled-muted text-ruled",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
