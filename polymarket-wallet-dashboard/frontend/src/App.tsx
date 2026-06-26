import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Moon, Sun, Wallet, Search, Sparkles, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useTheme } from "@/components/ThemeProvider";
import { KpiCards } from "@/components/KpiCards";
import { CategoryBreakdown } from "@/components/CategoryBreakdown";
import { Charts } from "@/components/Charts";
import { fetchWallet } from "@/lib/api";

export default function App() {
  const { theme, toggle } = useTheme();
  const [input, setInput] = useState("");
  const [address, setAddress] = useState<string>("");   // the submitted address

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ["wallet", address],
    queryFn: () => fetchWallet(address),
    enabled: address.length > 0,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });

  const submit = (addr: string) => {
    const a = addr.trim();
    setInput(a);
    setAddress(a);
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
              <h1 className="text-xl font-black tracking-tight">Análise de Carteiras Polymarket</h1>
              <p className="text-xs text-muted-foreground">
                Win rate, apostas, P&L e ROI — geral, por categoria e sub-categoria
              </p>
            </div>
          </div>
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
            {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-6 px-5 py-6">
        {/* Address search */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit(input);
          }}
          className="flex flex-wrap items-center gap-2"
        >
          <div className="relative min-w-[260px] flex-1">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Endereço da carteira (0x…)"
              spellCheck={false}
              className="h-10 w-full rounded-xl border border-border bg-card pl-9 pr-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40"
            />
          </div>
          <Button type="submit" disabled={isFetching || !input.trim()}>
            <Search className="h-4 w-4" /> Analisar
          </Button>
          <Button type="button" variant="outline" onClick={() => submit("demo")} disabled={isFetching}>
            <Sparkles className="h-4 w-4" /> Ver demo
          </Button>
        </form>

        {/* States */}
        {isFetching && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="skeleton h-24" />
              ))}
            </div>
            <div className="skeleton h-60" />
          </div>
        )}

        {isError && (
          <Card className="flex items-center gap-3 p-4 text-sm">
            <AlertTriangle className="h-5 w-5 text-rose-500" />
            Falha ao buscar: {(error as Error)?.message}
          </Card>
        )}

        {!isFetching && data?.error && (
          <Card className="flex items-center gap-3 p-4 text-sm">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            {data.error}
          </Card>
        )}

        {!isFetching && data && !data.error && (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="rounded-full bg-muted px-2 py-1 font-mono">{data.address}</span>
              {data.demo && (
                <span className="rounded-full bg-amber-500/15 px-2 py-1 font-bold text-amber-500">
                  dados de exemplo
                </span>
              )}
            </div>

            {data.n_markets === 0 ? (
              <Card className="p-6 text-center text-sm text-muted-foreground">
                Nenhum mercado encontrado para esta carteira.
              </Card>
            ) : (
              <>
                <KpiCards overall={data.overall} nMarkets={data.n_markets} nTrades={data.n_trades} />
                <Charts categories={data.by_category} />
                <CategoryBreakdown categories={data.by_category} />
              </>
            )}
          </div>
        )}

        {!isFetching && !data && !isError && (
          <Card className="p-6 text-center text-sm text-muted-foreground">
            Cole um endereço público da Polymarket e clique em <strong>Analisar</strong> — ou veja a
            <strong> demo</strong>. Read-only, sem chave privada.
          </Card>
        )}

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Análise read-only de carteiras públicas. Não é recomendação financeira.
        </footer>
      </main>
    </div>
  );
}
