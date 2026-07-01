import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export interface Kpi {
  label: string;
  value: string;
  sub?: string;
  tone?: "sky" | "emerald" | "violet" | "amber" | "rose" | "slate";
}

const TONES: Record<NonNullable<Kpi["tone"]>, string> = {
  sky: "from-sky-500/15 to-sky-500/5 text-sky-400",
  emerald: "from-emerald-500/15 to-emerald-500/5 text-emerald-400",
  violet: "from-violet-500/15 to-violet-500/5 text-violet-400",
  amber: "from-amber-500/15 to-amber-500/5 text-amber-500",
  rose: "from-rose-500/15 to-rose-500/5 text-rose-400",
  slate: "from-slate-500/15 to-slate-500/5 text-slate-400",
};

export function KpiCards({ items }: { items: Kpi[] }) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      {items.map((k) => (
        <Card key={k.label} className={cn("bg-gradient-to-b", TONES[k.tone ?? "slate"])}>
          <div className="p-4">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {k.label}
            </div>
            <div className="mt-1 text-2xl font-extrabold tracking-tight text-foreground">
              {k.value}
            </div>
            {k.sub && <div className="mt-0.5 text-xs text-muted-foreground">{k.sub}</div>}
          </div>
        </Card>
      ))}
    </div>
  );
}
