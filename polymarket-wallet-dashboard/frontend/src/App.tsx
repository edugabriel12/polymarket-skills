import { useRef, useState } from "react";
import { Moon, Sun, Wallet, Upload, Sparkles, AlertTriangle, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useTheme } from "@/components/ThemeProvider";
import { KpiCards } from "@/components/KpiCards";
import { ConfidenceBreakdown } from "@/components/ConfidenceBreakdown";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { Charts } from "@/components/Charts";
import { uploadCsv, fetchCsvDemo, type WalletReport } from "@/lib/api";
import { cn } from "@/lib/utils";

export default function App() {
  const { theme, toggle } = useTheme();
  const [data, setData] = useState<WalletReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const run = async (fn: () => Promise<WalletReport>) => {
    setLoading(true);
    setErr(null);
    setData(null);
    try {
      setData(await fn());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const onFile = (file?: File | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setErr("Envie um arquivo .csv");
      return;
    }
    run(() => uploadCsv(file));
  };

  return (
    <div className="min-h-screen">
      <header className="aurora border-b border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-6">
          <div className="flex items-center gap-3">
            <div className="rounded-2xl bg-white/10 p-2.5 backdrop-blur">
              <Wallet className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-black tracking-tight">Análise de Carteiras — CSV</h1>
              <p className="text-xs text-muted-foreground">
                Win rate, apostas, P&L e ROI — por categoria, sub-categoria e nível de confiança
              </p>
            </div>
          </div>
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
            {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-5 py-6">
        {/* CSV dropzone */}
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            onFile(e.dataTransfer.files?.[0]);
          }}
          className={cn(
            "flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed p-8 text-center transition",
            dragging ? "border-sky-500 bg-sky-500/5" : "border-border bg-card/40"
          )}
        >
          <Upload className="h-7 w-7 text-muted-foreground" />
          <div className="text-sm">
            Arraste o <strong>CSV de histórico</strong> aqui, ou
          </div>
          <div className="flex flex-wrap items-center justify-center gap-2">
            <Button onClick={() => inputRef.current?.click()} disabled={loading}>
              <FileText className="h-4 w-4" /> Selecionar CSV
            </Button>
            <Button variant="outline" onClick={() => run(fetchCsvDemo)} disabled={loading}>
              <Sparkles className="h-4 w-4" /> Ver demo
            </Button>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0])}
          />
          <div className="text-[11px] text-muted-foreground">
            Colunas esperadas: Data; Evento; Aposta; Conf.; Odd; Investido; ROI%; Lucro
          </div>
        </div>

        {/* States */}
        {loading && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="skeleton h-24" />
              ))}
            </div>
            <div className="skeleton h-60" />
          </div>
        )}

        {err && (
          <Card className="flex items-center gap-3 p-4 text-sm">
            <AlertTriangle className="h-5 w-5 text-rose-500" /> {err}
          </Card>
        )}

        {!loading && data?.error && (
          <Card className="flex items-center gap-3 p-4 text-sm">
            <AlertTriangle className="h-5 w-5 text-amber-500" /> {data.error}
          </Card>
        )}

        {!loading && data && !data.error && (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="rounded-full bg-muted px-2 py-1 font-mono">
                {data.filename ?? data.address ?? "carteira"}
              </span>
              {data.demo && (
                <span className="rounded-full bg-amber-500/15 px-2 py-1 font-bold text-amber-500">
                  dados de exemplo
                </span>
              )}
            </div>

            {data.n_markets === 0 ? (
              <Card className="p-6 text-center text-sm text-muted-foreground">
                Nenhuma aposta reconhecida no CSV.
              </Card>
            ) : (
              <>
                <KpiCards overall={data.overall} nMarkets={data.n_markets} nTrades={data.n_trades} />
                {data.by_confidence && data.by_confidence.length > 0 && (
                  <ConfidenceBreakdown buckets={data.by_confidence} />
                )}
                <Charts categories={data.by_category} />
                <CategoryBreakdown categories={data.by_category} />
              </>
            )}
          </div>
        )}

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Análise de histórico de apostas (CSV). Não é recomendação financeira.
        </footer>
      </main>
    </div>
  );
}
