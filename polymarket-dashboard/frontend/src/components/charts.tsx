import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
} from "recharts";
import type { PerfBlock } from "@/lib/api";

const C = {
  win: "#10b981",
  loss: "#f43f5e",
  over: "#0ea5e9",
  under: "#8b5cf6",
  grid: "rgba(148,163,184,0.25)",
};

export function WinRateDonut({ block }: { block: PerfBlock }) {
  const data = [
    { name: "Acerto", value: block.counts.acerto, color: C.win },
    { name: "Erro", value: block.counts.erro, color: C.loss },
  ];
  const total = block.counts.acerto + block.counts.erro;
  const hasData = total > 0;
  const slices = hasData ? data : [{ name: "—", value: 1, color: "#334155" }];
  return (
    <div className="relative h-48 [&_*]:outline-none">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={slices}
            innerRadius={58}
            outerRadius={80}
            paddingAngle={hasData ? 3 : 0}
            dataKey="value"
            stroke="none"
            isAnimationActive={hasData}
          >
            {slices.map((d, i) => (
              <Cell key={i} fill={d.color} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-2xl font-black">
          {block.win_rate === null ? "—" : `${(block.win_rate * 100).toFixed(0)}%`}
        </span>
        <span className="text-xs text-muted-foreground">{total} liquidadas</span>
      </div>
    </div>
  );
}

export function PnlBar({ data }: { data: { date: string; pnl: number }[] }) {
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, left: -16, bottom: 0 }}>
          <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#94a3b8" }} tickFormatter={(d) => d.slice(5)} />
          <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} />
          <ReferenceLine y={0} stroke={C.grid} />
          <Tooltip
            cursor={{ fill: "rgba(148,163,184,0.1)" }}
            contentStyle={{ background: "#0f172a", border: "1px solid #334155", borderRadius: 12, color: "#fff" }}
            formatter={(v: number) => [`$${v.toFixed(2)}`, "P&L"]}
          />
          <Bar dataKey="pnl" radius={[6, 6, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.pnl >= 0 ? C.win : C.loss} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function OverUnderSplit({ block }: { block: PerfBlock }) {
  const rows = [
    { name: "Over", rate: block.win_rate_over, color: C.over },
    { name: "Under", rate: block.win_rate_under, color: C.under },
  ];
  return (
    <div className="space-y-4 py-2">
      {rows.map((r) => (
        <div key={r.name}>
          <div className="mb-1 flex justify-between text-sm">
            <span className="font-semibold" style={{ color: r.color }}>
              {r.name}
            </span>
            <span className="tabular-nums text-muted-foreground">
              {r.rate === null ? "—" : `${(r.rate * 100).toFixed(0)}%`}
            </span>
          </div>
          <div className="h-2.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${(r.rate ?? 0) * 100}%`, background: r.color }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
