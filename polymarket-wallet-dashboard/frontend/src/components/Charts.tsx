import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
  CartesianGrid,
} from "recharts";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import type { Category } from "@/lib/api";

const EMERALD = "#10b981";
const ROSE = "#f43f5e";
const SKY = "#0ea5e9";

export function Charts({ categories }: { categories: Category[] }) {
  const pnlData = categories.map((c) => ({ name: c.category, pnl: c.total_pnl }));
  const winData = categories
    .filter((c) => c.win_rate !== null)
    .map((c) => ({ name: c.category, win: Math.round((c.win_rate ?? 0) * 100) }));

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card>
        <CardContent>
          <CardTitle className="mb-3">P&L por categoria (US$)</CardTitle>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={pnlData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-20} textAnchor="end" height={50} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 12 }}
                formatter={(v: number) => [`$${v.toFixed(2)}`, "P&L"]}
              />
              <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
                {pnlData.map((d, i) => (
                  <Cell key={i} fill={d.pnl >= 0 ? EMERALD : ROSE} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <CardTitle className="mb-3">Win rate por categoria (%)</CardTitle>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={winData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-20} textAnchor="end" height={50} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
              <Tooltip
                contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 12 }}
                formatter={(v: number) => [`${v}%`, "Win rate"]}
              />
              <Bar dataKey="win" radius={[4, 4, 0, 0]} fill={SKY} />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </div>
  );
}
