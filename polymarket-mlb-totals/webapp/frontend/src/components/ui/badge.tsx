import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold ring-1 ring-inset",
  {
    variants: {
      tone: {
        over: "bg-sky-500/15 text-sky-400 ring-sky-500/30",
        under: "bg-violet-500/15 text-violet-400 ring-violet-500/30",
        win: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
        loss: "bg-rose-500/15 text-rose-400 ring-rose-500/30",
        pending: "bg-amber-500/15 text-amber-500 ring-amber-500/30",
        voidc: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
        neutral: "bg-muted text-muted-foreground ring-border",
      },
    },
    defaultVariants: { tone: "neutral" },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
