import { useQuery } from "@tanstack/react-query";
import { Activity, Target, TrendingUp } from "lucide-react";
import { api, type Sport } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { pct } from "@/lib/utils";

const f4 = (x: number | null | undefined) => (x === null || x === undefined ? "n/a" : x.toFixed(4));
const signed = (x: number | null | undefined) =>
  x === null || x === undefined ? "n/a" : `${x >= 0 ? "+" : ""}${x.toFixed(4)}`;

// Model validation: is the edge real before scaling (or paying for data)?
export function CalibrationPanel({ sport }: { sport: Sport }) {
  const { data, isLoading } = useQuery({
    queryKey: ["calibration", sport],
    queryFn: () => api.calibration(sport),
    staleTime: 0,
    refetchOnMount: "always",
  });

  if (isLoading || !data) return <div className="skeleton h-48" />;

  const a = data.all;
  const clv = data.clv;
  const clvPos = (clv.avg_clv ?? 0) > 0;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-violet-400" /> Calibração do modelo
        </CardTitle>
        <span className="text-xs font-normal text-muted-foreground">
          {data.logged} modelados · {data.settled} liquidados
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        {a.n === 0 ? (
          <p className="text-sm text-muted-foreground">
            Ainda sem jogos liquidados suficientes. As métricas aparecem conforme os jogos
            modelados terminam (inclui os não apostados, via feed de resultados).
          </p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <Metric icon={Target} label="Brier (todos)" value={f4(a.brier)}
                sub={`n=${a.n} · ideal → 0`} gradient="from-sky-500 to-indigo-600" />
              <Metric icon={Target} label="Log-loss (todos)" value={f4(a.log_loss)}
                sub="menor = melhor" gradient="from-violet-500 to-fuchsia-600" />
              <Metric icon={TrendingUp} label="CLV médio"
                value={clv.n > 0 ? signed(clv.avg_clv) : "n/a"}
                sub={clv.n > 0 ? `bate fech.: ${pct(clv.beat_close_pct)} (n=${clv.n})` : "capturando preços…"}
                gradient={clvPos ? "from-emerald-500 to-teal-600" : "from-rose-500 to-orange-600"}
                positive={clv.n > 0 ? clvPos : null} />
            </div>

            {/* Layer 2: ECE/MCE + Murphy Brier decomposition */}
            {(a.ece !== undefined || a.brier_decomposition) && (
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-xl border border-border bg-muted/30 p-3">
                  <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Erro de calibração
                  </div>
                  <div className="mt-1 flex gap-4 text-sm">
                    <span>ECE <span className="font-bold tabular-nums">{f4(a.ece)}</span></span>
                    <span>MCE <span className="font-bold tabular-nums">{f4(a.mce)}</span></span>
                  </div>
                  <div className="text-[10px] text-muted-foreground">média / pior desvio · ideal → 0</div>
                </div>
                {a.brier_decomposition && (
                  <div className="rounded-xl border border-border bg-muted/30 p-3">
                    <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Decomposição de Murphy
                    </div>
                    <div className="mt-1 text-xs tabular-nums">
                      conf. {f4(a.brier_decomposition.reliability)} (↓) − resol.{" "}
                      {f4(a.brier_decomposition.resolution)} (↑) + incerteza{" "}
                      {f4(a.brier_decomposition.uncertainty)}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div className="overflow-x-auto">
              <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Confiabilidade — P(ref) previsto vs. frequência real
              </p>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="py-1.5 pr-3">Faixa</th>
                    <th className="px-3 text-right">n</th>
                    <th className="px-3 text-right">previsto</th>
                    <th className="px-3 text-right">real</th>
                    <th className="px-3 text-right">desvio</th>
                  </tr>
                </thead>
                <tbody>
                  {a.reliability.map((b) => {
                    const diff = b.empirical - b.avg_pred;
                    return (
                      <tr key={b.bucket} className="border-b border-border/60">
                        <td className="py-1.5 pr-3 font-medium tabular-nums">{b.bucket}</td>
                        <td className="px-3 text-right tabular-nums">{b.n}</td>
                        <td className="px-3 text-right tabular-nums">{b.avg_pred.toFixed(3)}</td>
                        <td className="px-3 text-right tabular-nums">{b.empirical.toFixed(3)}</td>
                        <td className={"px-3 text-right tabular-nums " +
                          (Math.abs(diff) < 0.05 ? "text-emerald-400" : "text-amber-400")}>
                          {signed(diff)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
        <p className="text-xs text-muted-foreground">
          Bem calibrado = previsto ≈ real (desvio ~0). CLV positivo = entradas batem o preço de
          fechamento — o sinal mais forte de edge real. Lado de referência: OVER (totais) / YES (BTTS).
        </p>
      </CardContent>
    </Card>
  );
}

function Metric({ icon: Icon, label, value, sub, gradient, positive }: {
  icon: typeof Target; label: string; value: string; sub: string;
  gradient: string; positive?: boolean | null;
}) {
  return (
    <div className={`rounded-xl bg-gradient-to-br ${gradient} p-px`}>
      <div className="rounded-[11px] bg-card p-3">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
          <Icon className="h-3.5 w-3.5" /> {label}
        </div>
        <div className={"mt-1 text-2xl font-bold tabular-nums " +
          (positive === true ? "text-emerald-400" : positive === false ? "text-rose-400" : "")}>
          {value}
        </div>
        <div className="text-xs text-muted-foreground">{sub}</div>
      </div>
    </div>
  );
}
