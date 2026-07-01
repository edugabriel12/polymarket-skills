import { useQuery } from "@tanstack/react-query";
import { Moon, Sun, Wallet, ListChecks, BarChart3, Copy } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ThemeProvider";
import { PortfolioBar } from "@/components/PortfolioBar";
import { WalletsTab } from "@/components/WalletsTab";
import { EntriesTab } from "@/components/EntriesTab";
import { ResultsTab } from "@/components/ResultsTab";
import { api } from "@/lib/api";

export default function App() {
  const { theme, toggle } = useTheme();
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: api.config });

  return (
    <div className="min-h-screen">
      <header className="aurora border-b border-border">
        <div className="mx-auto max-w-6xl px-5 py-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-gradient-to-r from-sky-500 to-violet-500 text-white shadow-lg">
                <Copy size={20} />
              </div>
              <div>
                <h1 className="text-xl font-extrabold tracking-tight">Copy-Trade · Paper</h1>
                <p className="text-xs text-muted-foreground">
                  Copia compras e vendas de carteiras públicas · slippage ≤{" "}
                  {cfg ? `${(cfg.slippage_cap * 100).toFixed(0)}%` : "20%"} · teto $
                  {cfg?.max_usd ?? 100} / piso ${cfg?.min_usd ?? 5}
                </p>
              </div>
            </div>
            <Button variant="outline" size="icon" onClick={toggle} title="Tema">
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-5 px-5 py-6">
        <PortfolioBar />

        <Tabs defaultValue="wallets">
          <TabsList>
            <TabsTrigger value="wallets">
              <Wallet size={16} /> Carteiras
            </TabsTrigger>
            <TabsTrigger value="entries">
              <ListChecks size={16} /> Entradas
            </TabsTrigger>
            <TabsTrigger value="results">
              <BarChart3 size={16} /> Resultados
            </TabsTrigger>
          </TabsList>

          <TabsContent value="wallets" className="mt-5">
            <WalletsTab />
          </TabsContent>
          <TabsContent value="entries" className="mt-5">
            <EntriesTab />
          </TabsContent>
          <TabsContent value="results" className="mt-5">
            <ResultsTab />
          </TabsContent>
        </Tabs>

        <p className="pt-4 text-center text-xs text-muted-foreground">
          {cfg?.disclaimer ??
            "Paper trading simulation — not financial advice. Real trading involves risk of loss."}
        </p>
      </main>
    </div>
  );
}
