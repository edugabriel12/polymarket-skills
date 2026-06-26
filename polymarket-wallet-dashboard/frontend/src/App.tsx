import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/components/ThemeProvider";
import { CarteirasTab } from "@/components/CarteirasTab";
import { SeparatedResultsTab } from "@/components/SeparatedResultsTab";
import { Moon, Sun, Wallet, Trophy } from "lucide-react";

export default function App() {
  const { theme, toggle } = useTheme();

  return (
    <div className="min-h-screen">
      <header className="aurora border-b border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-6">
          <div className="flex items-center gap-3">
            <div className="rounded-2xl bg-white/10 p-2.5 backdrop-blur">
              <Wallet className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-black tracking-tight">Wallet Dashboard</h1>
              <p className="text-xs text-muted-foreground">
                Carteiras vigiadas + modelo · resultados por categoria e confiança
              </p>
            </div>
          </div>
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Alternar tema">
            {theme === "dark" ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-6">
        <Tabs defaultValue="carteiras">
          <TabsList className="mb-6">
            <TabsTrigger value="carteiras">
              <Wallet className="h-4 w-4" /> Carteiras
            </TabsTrigger>
            <TabsTrigger value="resultados">
              <Trophy className="h-4 w-4" /> Resultados
            </TabsTrigger>
          </TabsList>
          <TabsContent value="carteiras">
            <CarteirasTab />
          </TabsContent>
          <TabsContent value="resultados">
            <SeparatedResultsTab />
          </TabsContent>
        </Tabs>

        <footer className="mt-10 border-t border-border pt-4 text-center text-xs text-muted-foreground">
          Análise de carteiras e modelo. Não é recomendação financeira.
        </footer>
      </main>
    </div>
  );
}
